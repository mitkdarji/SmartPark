"""Tools the SmartPark agent can call.

Each tool is a JSON-schema declaration plus an async handler bound to a
`ToolContext` (database session + authenticated user). Two rules govern the set:

  Scoped by role.  A driver's agent sees only driver tools, and every query it
  runs is filtered to that driver's own rows. The agent cannot widen its own
  scope, because the filter lives in the handler, not in the prompt.

  Mutations are gated.  Anything that moves money or changes a facility's
  configuration is marked `mutating`. The runtime refuses to execute those
  without an explicit confirmation token from the user. A language model must
  not be able to debit a wallet because a plausible sentence talked it into one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.models.enums import PaymentStatus, SessionStatus, SlotStatus, UserRole
from app.models.facility import Facility, Gate, Slot
from app.models.parking import Anomaly, ParkingSession
from app.models.user import User, Wallet, WalletTransaction
from app.services.allocation.navigation import route_to_slot
from app.services.billing.pricing import pricing_engine

log = get_logger(__name__)


@dataclass(slots=True)
class ToolContext:
    db: Any
    user: User
    facility_id: int | None = None
    confirmed_tokens: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[dict]]
    roles: tuple[str, ...] = (UserRole.DRIVER, UserRole.OWNER, UserRole.ADMIN)
    mutating: bool = False

    def declaration(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


def _money(minor: int, currency: str = "INR") -> str:
    symbol = {"INR": "₹", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
    return f"{symbol}{minor / 100:,.2f}"


async def _occupancy(db, facility_id: int) -> tuple[int, int, float]:
    total = (
        await db.execute(
            select(func.count(Slot.id)).where(
                Slot.facility_id == facility_id, Slot.is_active.is_(True)
            )
        )
    ).scalar_one()
    occupied = (
        await db.execute(
            select(func.count(Slot.id)).where(
                Slot.facility_id == facility_id,
                Slot.is_active.is_(True),
                Slot.status != SlotStatus.EMPTY,
            )
        )
    ).scalar_one()
    return int(total), int(occupied), (occupied / total if total else 0.0)


# ─────────────────────────────────────────────────────────────
# Driver tools
# ─────────────────────────────────────────────────────────────

async def _find_my_vehicle(ctx: ToolContext, _: dict) -> dict:
    session = (
        await ctx.db.execute(
            select(ParkingSession)
            .where(
                ParkingSession.user_id == ctx.user.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
            .order_by(ParkingSession.entry_at.desc())
        )
    ).scalars().first()

    if session is None:
        return {"parked": False, "message": "You have no vehicle parked right now."}

    slot = await ctx.db.get(Slot, session.slot_id) if session.slot_id else None
    facility = await ctx.db.get(Facility, session.facility_id)
    minutes = session.elapsed_minutes(datetime.now(UTC))

    return {
        "parked": True,
        "plate": session.plate,
        "facility": facility.name if facility else None,
        "slot_code": slot.code if slot else None,
        "zone": slot.zone if slot else None,
        "level_id": slot.level_id if slot else None,
        "parked_for_minutes": round(minutes, 1),
        "entry_at": session.entry_at.isoformat(),
        "directions": session.navigation_path,
    }


async def _current_charges(ctx: ToolContext, _: dict) -> dict:
    session = (
        await ctx.db.execute(
            select(ParkingSession)
            .where(
                ParkingSession.user_id == ctx.user.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
            .order_by(ParkingSession.entry_at.desc())
        )
    ).scalars().first()
    if session is None:
        return {"active": False, "message": "No active parking session."}

    facility = await ctx.db.get(Facility, session.facility_id)
    slot = await ctx.db.get(Slot, session.slot_id) if session.slot_id else None
    _, _, occupancy = await _occupancy(ctx.db, session.facility_id)
    minutes = session.elapsed_minutes(datetime.now(UTC))

    quote = pricing_engine.quote(
        facility,
        raw_minutes=minutes,
        occupancy=occupancy,
        slot_type=slot.slot_type if slot else "standard",
    )
    return {
        "active": True,
        "parked_for_minutes": round(minutes, 1),
        "amount_so_far": _money(quote.total_minor, facility.currency),
        "amount_so_far_minor": quote.total_minor,
        "current_rate_per_hour": _money(quote.effective_rate_minor, facility.currency),
        "free_minutes_remaining": max(0, facility.free_minutes - minutes),
        "explanation": quote.explanation,
    }


async def _estimate_cost(ctx: ToolContext, args: dict) -> dict:
    hours = float(args.get("hours", 2))
    facility_id = args.get("facility_id") or ctx.facility_id
    if facility_id is None:
        session = (
            await ctx.db.execute(
                select(ParkingSession).where(
                    ParkingSession.user_id == ctx.user.id,
                    ParkingSession.status == SessionStatus.ACTIVE,
                )
            )
        ).scalars().first()
        facility_id = session.facility_id if session else None
    if facility_id is None:
        return {"error": "No facility specified and you have no active session."}

    facility = await ctx.db.get(Facility, facility_id)
    if facility is None:
        return {"error": f"Facility {facility_id} not found."}

    _, _, occupancy = await _occupancy(ctx.db, facility_id)
    quote = pricing_engine.estimate(facility, hours=hours, occupancy=occupancy)
    return {
        "facility": facility.name,
        "hours": hours,
        "estimated_total": _money(quote.total_minor, facility.currency),
        "estimated_total_minor": quote.total_minor,
        "rate_per_hour": _money(quote.effective_rate_minor, facility.currency),
        "occupancy_pct": round(occupancy * 100, 1),
        "breakdown": quote.components,
    }


async def _wallet_balance(ctx: ToolContext, _: dict) -> dict:
    wallet = (
        await ctx.db.execute(select(Wallet).where(Wallet.user_id == ctx.user.id))
    ).scalar_one_or_none()
    if wallet is None:
        return {"error": "No wallet found for this account."}
    return {
        "balance": _money(wallet.balance_minor, wallet.currency),
        "balance_minor": wallet.balance_minor,
        "available": _money(wallet.available_minor, wallet.currency),
        "held_minor": wallet.held_minor,
        "auto_reload_enabled": wallet.auto_reload_enabled,
        "currency": wallet.currency,
    }


async def _recent_charges(ctx: ToolContext, args: dict) -> dict:
    limit = min(int(args.get("limit", 5)), 20)
    wallet = (
        await ctx.db.execute(select(Wallet).where(Wallet.user_id == ctx.user.id))
    ).scalar_one_or_none()
    if wallet is None:
        return {"transactions": []}
    rows = (
        await ctx.db.execute(
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet.id)
            .order_by(WalletTransaction.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "transactions": [
            {
                "date": t.created_at.strftime("%d %b %Y, %H:%M"),
                "type": t.txn_type,
                "amount": _money(t.amount_minor, t.currency),
                "description": t.description,
                "reference": t.reference,
            }
            for t in rows
        ]
    }


async def _top_up_wallet(ctx: ToolContext, args: dict) -> dict:
    from app.services.billing.wallet import wallet_service

    amount_minor = int(float(args.get("amount", 0)) * 100)
    if amount_minor <= 0:
        return {"error": "Amount must be greater than zero."}
    if amount_minor > 5_000_000:
        return {"error": "Single top-up is capped at ₹50,000."}

    wallet = await wallet_service.get_or_create(ctx.db, ctx.user.id)
    txn = await wallet_service.credit(
        ctx.db, wallet, amount_minor,
        description="Wallet top-up requested via the SmartPark assistant",
    )
    await ctx.db.commit()
    return {
        "success": True,
        "credited": _money(amount_minor, wallet.currency),
        "new_balance": _money(wallet.balance_minor, wallet.currency),
        "reference": txn.reference,
    }


async def _find_parking(ctx: ToolContext, args: dict) -> dict:
    city = (args.get("city") or "").strip()
    query = select(Facility).where(Facility.is_active.is_(True))
    if city:
        query = query.where(Facility.city.ilike(f"%{city}%"))
    facilities = (await ctx.db.execute(query.limit(10))).scalars().all()

    results = []
    for facility in facilities:
        total, occupied, occupancy = await _occupancy(ctx.db, facility.id)
        quote = pricing_engine.estimate(facility, hours=1, occupancy=occupancy)
        results.append(
            {
                "facility_id": facility.id,
                "name": facility.name,
                "address": facility.address,
                "city": facility.city,
                "access_mode": facility.access_mode,
                "free_slots": total - occupied,
                "capacity": total,
                "occupancy_pct": round(occupancy * 100, 1),
                "rate_per_hour": _money(quote.effective_rate_minor, facility.currency),
            }
        )
    results.sort(key=lambda r: (-r["free_slots"], r["occupancy_pct"]))
    return {"facilities": results}


async def _directions(ctx: ToolContext, _: dict) -> dict:
    session = (
        await ctx.db.execute(
            select(ParkingSession)
            .where(
                ParkingSession.user_id == ctx.user.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
            .order_by(ParkingSession.entry_at.desc())
        )
    ).scalars().first()
    if session is None or session.slot_id is None:
        return {"error": "You have no active parking session with an assigned slot."}

    slot = await ctx.db.get(Slot, session.slot_id)
    gate = await ctx.db.get(Gate, session.entry_gate_id) if session.entry_gate_id else None
    from app.models.facility import Level

    level = await ctx.db.get(Level, slot.level_id)
    route = route_to_slot(
        gate.position if gate else (0.0, 0.0),
        slot.center,
        aisles=level.aisles if level else [],
        slot_code=slot.code, zone=slot.zone, level_id=slot.level_id,
    )
    return {
        "slot_code": slot.code,
        "zone": slot.zone,
        "distance_m": round(route.distance_m, 1),
        "instructions": route.instructions,
    }


# ─────────────────────────────────────────────────────────────
# Owner / operator tools
# ─────────────────────────────────────────────────────────────

async def _facility_status(ctx: ToolContext, args: dict) -> dict:
    facility_id = args.get("facility_id") or ctx.facility_id
    facility = await ctx.db.get(Facility, facility_id) if facility_id else None
    if facility is None or (
        facility.owner_id != ctx.user.id and ctx.user.role != UserRole.ADMIN
    ):
        return {"error": "Facility not found or not accessible from this account."}

    total, occupied, occupancy = await _occupancy(ctx.db, facility.id)
    active = (
        await ctx.db.execute(
            select(func.count(ParkingSession.id)).where(
                ParkingSession.facility_id == facility.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
        )
    ).scalar_one()
    quote = pricing_engine.estimate(facility, hours=1, occupancy=occupancy)

    return {
        "facility": facility.name,
        "facility_id": facility.id,
        "access_mode": facility.access_mode,
        "allocation_strategy": facility.allocation_strategy,
        "capacity": total,
        "occupied": occupied,
        "free": total - occupied,
        "occupancy_pct": round(occupancy * 100, 1),
        "active_sessions": int(active),
        "current_rate_per_hour": _money(quote.effective_rate_minor, facility.currency),
        "dynamic_pricing": facility.dynamic_pricing_enabled,
    }


async def _revenue_report(ctx: ToolContext, args: dict) -> dict:
    facility_id = args.get("facility_id") or ctx.facility_id
    days = min(int(args.get("days", 7)), 90)
    facility = await ctx.db.get(Facility, facility_id) if facility_id else None
    if facility is None or (
        facility.owner_id != ctx.user.id and ctx.user.role != UserRole.ADMIN
    ):
        return {"error": "Facility not found or not accessible from this account."}

    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        await ctx.db.execute(
            select(
                func.count(ParkingSession.id),
                func.coalesce(func.sum(ParkingSession.total_minor), 0),
                func.coalesce(func.avg(ParkingSession.billable_minutes), 0),
                func.coalesce(func.avg(ParkingSession.walk_distance_m), 0),
            ).where(
                ParkingSession.facility_id == facility.id,
                ParkingSession.status == SessionStatus.COMPLETED,
                ParkingSession.exit_at >= since,
            )
        )
    ).one()
    count, revenue, avg_minutes, avg_walk = row

    unpaid = (
        await ctx.db.execute(
            select(func.count(ParkingSession.id)).where(
                ParkingSession.facility_id == facility.id,
                ParkingSession.payment_status == PaymentStatus.FAILED,
            )
        )
    ).scalar_one()

    return {
        "facility": facility.name,
        "period_days": days,
        "completed_sessions": int(count),
        "revenue": _money(int(revenue), facility.currency),
        "revenue_minor": int(revenue),
        "avg_stay_minutes": round(float(avg_minutes), 1),
        "avg_walk_distance_m": round(float(avg_walk), 1),
        "avg_revenue_per_session": _money(
            int(revenue / count) if count else 0, facility.currency
        ),
        "failed_payments": int(unpaid),
    }


async def _occupancy_forecast(ctx: ToolContext, args: dict) -> dict:
    from app.services.analytics import analytics_service

    facility_id = args.get("facility_id") or ctx.facility_id
    facility = await ctx.db.get(Facility, facility_id) if facility_id else None
    if facility is None or (
        facility.owner_id != ctx.user.id and ctx.user.role != UserRole.ADMIN
    ):
        return {"error": "Facility not found or not accessible from this account."}
    forecast = await analytics_service.forecast(
        ctx.db, facility, horizon_hours=int(args.get("hours", 6))
    )
    return forecast.as_dict()


async def _list_anomalies(ctx: ToolContext, args: dict) -> dict:
    facility_id = args.get("facility_id") or ctx.facility_id
    facility = await ctx.db.get(Facility, facility_id) if facility_id else None
    if facility is None or (
        facility.owner_id != ctx.user.id and ctx.user.role != UserRole.ADMIN
    ):
        return {"error": "Facility not found or not accessible from this account."}

    rows = (
        await ctx.db.execute(
            select(Anomaly)
            .where(Anomaly.facility_id == facility.id, Anomaly.resolved.is_(False))
            .order_by(Anomaly.created_at.desc())
            .limit(min(int(args.get("limit", 10)), 50))
        )
    ).scalars().all()
    return {
        "open_anomalies": [
            {
                "id": a.id, "kind": a.kind, "severity": a.severity,
                "summary": a.summary, "detected_at": a.created_at.isoformat(),
            }
            for a in rows
        ]
    }


async def _set_pricing(ctx: ToolContext, args: dict) -> dict:
    facility_id = args.get("facility_id") or ctx.facility_id
    facility = await ctx.db.get(Facility, facility_id) if facility_id else None
    if facility is None or (
        facility.owner_id != ctx.user.id and ctx.user.role != UserRole.ADMIN
    ):
        return {"error": "Facility not found or not accessible from this account."}

    changes: dict[str, Any] = {}
    if "base_rate" in args:
        new_rate = int(float(args["base_rate"]) * 100)
        if not 100 <= new_rate <= 100_000:
            return {"error": "Base rate must be between ₹1 and ₹1,000 per hour."}
        changes["base_rate_minor_per_hour"] = new_rate
        facility.base_rate_minor_per_hour = new_rate
    if "dynamic_pricing_enabled" in args:
        facility.dynamic_pricing_enabled = bool(args["dynamic_pricing_enabled"])
        changes["dynamic_pricing_enabled"] = facility.dynamic_pricing_enabled
    if "free_minutes" in args:
        minutes = int(args["free_minutes"])
        if not 0 <= minutes <= 240:
            return {"error": "Free period must be between 0 and 240 minutes."}
        facility.free_minutes = minutes
        changes["free_minutes"] = minutes

    if not changes:
        return {"error": "Nothing to change — specify base_rate, free_minutes, or dynamic_pricing_enabled."}

    await ctx.db.commit()
    return {"success": True, "facility": facility.name, "changes": changes}


# ─────────────────────────────────────────────────────────────

TOOLS: list[Tool] = [
    Tool(
        name="find_my_vehicle",
        description=(
            "Find where the user's vehicle is currently parked: facility, slot code, "
            "zone, level and how long it has been there. Use this whenever the user "
            "asks where their car is."
        ),
        schema={"type": "object", "properties": {}, "required": []},
        handler=_find_my_vehicle,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
    ),
    Tool(
        name="get_current_charges",
        description=(
            "Get the running cost of the user's active parking session, the current "
            "hourly rate and how it was calculated."
        ),
        schema={"type": "object", "properties": {}, "required": []},
        handler=_current_charges,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
    ),
    Tool(
        name="estimate_parking_cost",
        description=(
            "Estimate what parking will cost for a given number of hours at a facility, "
            "using the live occupancy-adjusted rate."
        ),
        schema={
            "type": "object",
            "properties": {
                "hours": {"type": "number", "description": "Planned duration in hours."},
                "facility_id": {
                    "type": "integer",
                    "description": "Facility to price. Defaults to the user's active session.",
                },
            },
            "required": ["hours"],
        },
        handler=_estimate_cost,
    ),
    Tool(
        name="get_wallet_balance",
        description="Get the user's SmartPark wallet balance and auto-reload setting.",
        schema={"type": "object", "properties": {}, "required": []},
        handler=_wallet_balance,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
    ),
    Tool(
        name="list_recent_charges",
        description="List the user's recent wallet transactions — parking charges and top-ups.",
        schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many, max 20."}},
            "required": [],
        },
        handler=_recent_charges,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
    ),
    Tool(
        name="top_up_wallet",
        description=(
            "Add money to the user's wallet. This moves real money, so it always "
            "requires explicit user confirmation before it runs."
        ),
        schema={
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Amount in rupees."}
            },
            "required": ["amount"],
        },
        handler=_top_up_wallet,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
        mutating=True,
    ),
    Tool(
        name="find_parking_nearby",
        description=(
            "Search SmartPark facilities and report live availability and rates. "
            "Use when the user is looking for somewhere to park."
        ),
        schema={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City to search in."}},
            "required": [],
        },
        handler=_find_parking,
    ),
    Tool(
        name="get_directions_to_my_slot",
        description="Turn-by-turn directions from the entry gate to the user's assigned slot.",
        schema={"type": "object", "properties": {}, "required": []},
        handler=_directions,
        roles=(UserRole.DRIVER, UserRole.ADMIN),
    ),
    Tool(
        name="get_facility_status",
        description=(
            "Live operational status of a facility the user owns: capacity, occupancy, "
            "active sessions, current rate and allocation strategy."
        ),
        schema={
            "type": "object",
            "properties": {"facility_id": {"type": "integer"}},
            "required": [],
        },
        handler=_facility_status,
        roles=(UserRole.OWNER, UserRole.ADMIN),
    ),
    Tool(
        name="get_revenue_report",
        description="Revenue, session count and average stay for a facility over recent days.",
        schema={
            "type": "object",
            "properties": {
                "facility_id": {"type": "integer"},
                "days": {"type": "integer", "description": "Look-back window, max 90."},
            },
            "required": [],
        },
        handler=_revenue_report,
        roles=(UserRole.OWNER, UserRole.ADMIN),
    ),
    Tool(
        name="get_occupancy_forecast",
        description=(
            "Predicted occupancy for the next few hours, from the trained demand model "
            "(or a seasonal baseline when there is not enough history)."
        ),
        schema={
            "type": "object",
            "properties": {
                "facility_id": {"type": "integer"},
                "hours": {"type": "integer", "description": "Horizon in hours, default 6."},
            },
            "required": [],
        },
        handler=_occupancy_forecast,
        roles=(UserRole.OWNER, UserRole.ADMIN),
    ),
    Tool(
        name="list_open_anomalies",
        description="List unresolved security and billing anomalies detected at a facility.",
        schema={
            "type": "object",
            "properties": {
                "facility_id": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
        handler=_list_anomalies,
        roles=(UserRole.OWNER, UserRole.ADMIN),
    ),
    Tool(
        name="update_facility_pricing",
        description=(
            "Change a facility's base hourly rate, free period, or dynamic-pricing "
            "toggle. Affects what every future customer is charged, so it always "
            "requires explicit user confirmation before it runs."
        ),
        schema={
            "type": "object",
            "properties": {
                "facility_id": {"type": "integer"},
                "base_rate": {"type": "number", "description": "New base rate in rupees per hour."},
                "free_minutes": {"type": "integer"},
                "dynamic_pricing_enabled": {"type": "boolean"},
            },
            "required": [],
        },
        handler=_set_pricing,
        roles=(UserRole.OWNER, UserRole.ADMIN),
        mutating=True,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def tools_for_role(role: str) -> list[Tool]:
    return [tool for tool in TOOLS if role in tool.roles]
