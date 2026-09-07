"""Declarative automation: triggers, conditions, actions.

Rules are rows, not code. An owner writes one in the dashboard (or accepts one
the copilot drafted) and it takes effect immediately — no deploy. That is the
whole reason the engine is data-driven rather than a pile of `if` statements.

Two trigger kinds:
  event      any topic on the event bus — a vehicle entering, occupancy moving,
             a payment failing. Reacts within milliseconds.
  schedule   `schedule:tick`, evaluated on a fixed cadence for the things that
             are about state rather than events — overstays, stale holds,
             nightly reconciliation, model retraining.

Actions are looked up in a registry. A rule can only ever name an action that
exists in that registry, which is what makes it safe for the AI copilot to
propose rules: the worst it can produce is a rule that gets rejected.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, Topic, bus
from app.core.logging import get_logger
from app.models.automation import AutomationRule, AutomationRun
from app.models.enums import (
    AnomalyKind,
    NotificationChannel,
    PaymentStatus,
    SessionStatus,
    Severity,
    SlotStatus,
)
from app.models.facility import Facility, Slot
from app.models.parking import Anomaly, ParkingSession
from app.models.user import User, Wallet
from app.services.analytics import analytics_service
from app.services.integrations.notifications import notifier

log = get_logger(__name__)

TRIGGERS = [
    "schedule:tick",
    Topic.VEHICLE_ENTERED,
    Topic.VEHICLE_EXITED,
    Topic.ENTRY_DENIED,
    Topic.OCCUPANCY_CHANGED,
    Topic.PAYMENT_FAILED,
    Topic.ANOMALY_DETECTED,
    Topic.SLOT_RELEASED,
]

ActionFn = Callable[[AsyncSession, "ActionContext"], Awaitable[dict]]


class ActionContext:
    def __init__(
        self,
        *,
        rule: AutomationRule,
        facility: Facility | None,
        event: Event | None,
        params: dict[str, Any],
    ):
        self.rule = rule
        self.facility = facility
        self.event = event
        self.params = params


# ── Conditions ────────────────────────────────────────────────

def evaluate_conditions(conditions: dict, event: Event | None, now: datetime) -> bool:
    """Small, deliberately limited predicate language.

    Not a general expression evaluator — a fixed set of comparators over event
    payload fields and clock values. There is no `eval` here and there never
    will be: rules can be authored by an AI, so the language must be one where
    nothing dangerous is expressible.
    """
    if not conditions:
        return True

    payload = (event.payload if event else {}) or {}

    for key, expected in conditions.items():
        if key == "hour":
            if now.hour != int(expected):
                return False
        elif key == "weekday":
            if now.weekday() != int(expected):
                return False
        elif key.endswith("_gte"):
            field = key[:-4]
            if float(payload.get(field, 0)) < float(expected):
                return False
        elif key.endswith("_lte"):
            field = key[:-4]
            if float(payload.get(field, 0)) > float(expected):
                return False
        elif key.endswith("_eq"):
            field = key[:-3]
            if payload.get(field) != expected:
                return False
        elif key in ("overstay_hours", "min_confidence", "threshold_minor"):
            continue  # parameters consumed by the action, not gates on it
        elif key in payload and payload[key] != expected:
            return False
    return True


# ── Actions ───────────────────────────────────────────────────

async def action_notify_user(db: AsyncSession, ctx: ActionContext) -> dict:
    user_id = ctx.params.get("user_id") or (ctx.event.user_id if ctx.event else None)
    if not user_id:
        return {"skipped": "no user in scope"}
    user = await db.get(User, user_id)
    if user is None:
        return {"skipped": f"user {user_id} not found"}

    title = ctx.params.get("title", "SmartPark update")
    body = ctx.params.get("body", "")
    if not body and ctx.event:
        body = f"{ctx.event.topic}: {ctx.event.payload}"
    await notifier.send(
        db, user, title=title, body=body,
        channel=ctx.params.get("channel", NotificationChannel.IN_APP),
        meta={"rule_id": ctx.rule.id},
    )
    return {"notified_user": user_id, "title": title}


async def action_flag_overstays(db: AsyncSession, ctx: ActionContext) -> dict:
    now = datetime.now(UTC)
    threshold_hours = int(
        ctx.params.get("hours")
        or (ctx.facility.overstay_after_hours if ctx.facility else 12)
    )
    cutoff = now - timedelta(hours=threshold_hours)

    query = select(ParkingSession).where(
        ParkingSession.status == SessionStatus.ACTIVE,
        ParkingSession.entry_at <= cutoff,
    )
    if ctx.facility:
        query = query.where(ParkingSession.facility_id == ctx.facility.id)

    sessions = (await db.execute(query)).scalars().all()
    flagged = []
    for session in sessions:
        existing = (
            await db.execute(
                select(Anomaly).where(
                    Anomaly.session_id == session.id, Anomaly.kind == AnomalyKind.OVERSTAY
                )
            )
        ).scalar_one_or_none()
        if existing:
            continue

        hours = session.elapsed_minutes(now) / 60
        db.add(
            Anomaly(
                facility_id=session.facility_id, session_id=session.id,
                kind=AnomalyKind.OVERSTAY, severity=Severity.LOW,
                score=min(1.0, hours / max(threshold_hours, 1) - 1.0),
                summary=f"{session.plate} has been parked {hours:.1f} h (threshold {threshold_hours} h).",
                detail={"hours": round(hours, 2), "threshold_hours": threshold_hours},
            )
        )
        flagged.append(session.plate)

        if session.user_id:
            user = await db.get(User, session.user_id)
            if user:
                await notifier.send(
                    db, user,
                    title="Your vehicle has been parked a long time",
                    body=(
                        f"{session.plate} has been at the facility for {hours:.1f} hours. "
                        f"Charges continue to accrue."
                    ),
                    meta={"session_id": session.id, "rule_id": ctx.rule.id},
                )
        await bus.emit(
            Topic.OVERSTAY_DETECTED,
            {"session_id": session.id, "plate": session.plate, "hours": round(hours, 2)},
            facility_id=session.facility_id, user_id=session.user_id,
        )

    await db.flush()
    return {"flagged": len(flagged), "plates": flagged[:10]}


async def action_release_stale_reservations(db: AsyncSession, ctx: ActionContext) -> dict:
    now = datetime.now(UTC)
    query = select(Slot).where(
        Slot.status == SlotStatus.RESERVED,
        Slot.reserved_until.is_not(None),
        Slot.reserved_until < now,
    )
    if ctx.facility:
        query = query.where(Slot.facility_id == ctx.facility.id)

    slots = (await db.execute(query)).scalars().all()
    for slot in slots:
        slot.status = SlotStatus.EMPTY
        slot.reserved_until = None
        slot.last_vacated_at = now
        await bus.emit(
            Topic.SLOT_RELEASED,
            {"slot_id": slot.id, "slot_code": slot.code, "reason": "reservation expired"},
            facility_id=slot.facility_id,
        )
    await db.flush()
    return {"released": len(slots), "codes": [s.code for s in slots[:10]]}


async def action_recompute_pricing(db: AsyncSession, ctx: ActionContext) -> dict:
    from app.services.billing.pricing import pricing_engine
    from app.services.parking import parking_service

    facilities = [ctx.facility] if ctx.facility else (
        await db.execute(select(Facility).where(Facility.is_active.is_(True)))
    ).scalars().all()

    updates = []
    for facility in facilities:
        if facility is None:
            continue
        _, _, ratio = await parking_service.occupancy(db, facility.id)
        rate, multiplier, components = pricing_engine.effective_rate(
            facility, occupancy=ratio
        )
        await bus.emit(
            Topic.PRICE_CHANGED,
            {
                "facility_id": facility.id, "effective_rate_minor": rate,
                "multiplier": round(multiplier, 3), "occupancy_pct": round(ratio * 100, 1),
                "components": components,
            },
            facility_id=facility.id,
        )
        updates.append({"facility_id": facility.id, "rate_minor": rate})
    return {"repriced": updates}


async def action_snapshot_occupancy(db: AsyncSession, ctx: ActionContext) -> dict:
    facilities = [ctx.facility] if ctx.facility else (
        await db.execute(select(Facility).where(Facility.is_active.is_(True)))
    ).scalars().all()
    taken = 0
    for facility in facilities:
        if facility is None:
            continue
        await analytics_service.snapshot(db, facility)
        taken += 1
    return {"snapshots": taken}


async def action_reconcile_wallets(db: AsyncSession, ctx: ActionContext) -> dict:
    from app.services.billing.wallet import wallet_service

    wallet_ids = (await db.execute(select(Wallet.id))).scalars().all()
    drifted = []
    for wallet_id in wallet_ids:
        report = await wallet_service.reconcile(db, wallet_id)
        if not report["balanced"]:
            drifted.append(report)
            log.error("wallet drift detected", extra=report)
    return {"checked": len(wallet_ids), "drifted": len(drifted), "details": drifted[:5]}


async def action_retrain_models(db: AsyncSession, ctx: ActionContext) -> dict:
    facilities = [ctx.facility] if ctx.facility else (
        await db.execute(select(Facility).where(Facility.is_active.is_(True)))
    ).scalars().all()
    results = []
    for facility in facilities:
        if facility is None:
            continue
        results.append(await analytics_service.train_models(db, facility))
    return {"retrained": len(results), "results": results}


async def action_close_abandoned_sessions(db: AsyncSession, ctx: ActionContext) -> dict:
    """Close sessions whose exit scan was clearly missed.

    A vehicle that has been 'parked' for days has almost certainly left through
    a failed read. The bay is freed and the session marked abandoned rather than
    completed, so it is visible as a data-quality problem instead of being
    quietly folded into the revenue figures.
    """
    hours = int(ctx.params.get("after_hours", 48))
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    query = select(ParkingSession).where(
        ParkingSession.status == SessionStatus.ACTIVE,
        ParkingSession.entry_at <= cutoff,
    )
    if ctx.facility:
        query = query.where(ParkingSession.facility_id == ctx.facility.id)

    sessions = (await db.execute(query)).scalars().all()
    now = datetime.now(UTC)
    for session in sessions:
        session.status = SessionStatus.ABANDONED
        session.exit_at = now
        session.payment_status = PaymentStatus.PENDING
        session.notes = (
            f"Automatically closed after {hours} h with no exit scan. "
            f"Likely a missed read at the exit gate."
        )
        if session.slot_id:
            await db.execute(
                update(Slot)
                .where(Slot.id == session.slot_id)
                .values(status=SlotStatus.EMPTY, last_vacated_at=now)
            )
    await db.flush()
    return {"closed": len(sessions)}


async def action_open_barrier_alert(db: AsyncSession, ctx: ActionContext) -> dict:
    """Escalate to the on-site operator. Wired to SMS when Twilio is configured."""
    if ctx.facility is None:
        return {"skipped": "no facility in scope"}
    owner = await db.get(User, ctx.facility.owner_id)
    if owner is None:
        return {"skipped": "no owner"}
    message = ctx.params.get("message") or (
        f"Attention needed at {ctx.facility.name}: {ctx.event.topic if ctx.event else 'automation rule fired'}"
    )
    await notifier.send(
        db, owner, title="SmartPark operations alert", body=message,
        channel=ctx.params.get("channel", NotificationChannel.IN_APP),
        meta={"rule_id": ctx.rule.id},
    )
    return {"alerted_owner": owner.id}


ACTION_REGISTRY: dict[str, ActionFn] = {
    "notify_user": action_notify_user,
    "flag_overstays": action_flag_overstays,
    "release_stale_reservations": action_release_stale_reservations,
    "recompute_pricing": action_recompute_pricing,
    "snapshot_occupancy": action_snapshot_occupancy,
    "reconcile_wallets": action_reconcile_wallets,
    "retrain_models": action_retrain_models,
    "close_abandoned_sessions": action_close_abandoned_sessions,
    "alert_operator": action_open_barrier_alert,
}


class AutomationEngine:
    def __init__(self) -> None:
        self._last_fired: dict[int, float] = {}

    async def handle_event(self, event: Event) -> None:
        """Event-bus subscriber. Opens its own session — the publisher's may be gone."""
        from app.db.session import SessionLocal

        async with SessionLocal() as db:
            try:
                await self.run_rules(db, trigger=event.topic, event=event)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                log.error(
                    "automation event handling failed",
                    extra={"topic": event.topic, "error": str(exc)},
                )

    async def tick(self) -> dict:
        """Scheduled pass. Called by the APScheduler job."""
        from app.db.session import SessionLocal

        async with SessionLocal() as db:
            try:
                result = await self.run_rules(db, trigger="schedule:tick", event=None)
                await db.commit()
                return result
            except Exception as exc:
                await db.rollback()
                log.error("automation tick failed", extra={"error": str(exc)})
                return {"error": str(exc)}

    async def run_rules(
        self, db: AsyncSession, *, trigger: str, event: Event | None
    ) -> dict:
        now = datetime.now(UTC)
        query = select(AutomationRule).where(
            AutomationRule.trigger == trigger, AutomationRule.enabled.is_(True)
        )
        if event and event.facility_id is not None:
            # A rule with no facility is global; one with a facility is scoped.
            query = query.where(
                (AutomationRule.facility_id == event.facility_id)
                | (AutomationRule.facility_id.is_(None))
            )
        rules = (await db.execute(query.order_by(AutomationRule.priority))).scalars().all()

        fired: list[dict] = []
        for rule in rules:
            if (
                rule.cooldown_seconds
                and time.monotonic() - self._last_fired.get(rule.id, 0.0) < rule.cooldown_seconds
            ):
                continue
            if not evaluate_conditions(rule.conditions or {}, event, now):
                continue

            facility_id = rule.facility_id or (event.facility_id if event else None)
            facility = await db.get(Facility, facility_id) if facility_id else None

            started = time.perf_counter()
            actions_taken: list[dict] = []
            status = "success"
            detail = ""

            for spec in rule.actions or []:
                name = spec.get("action")
                handler = ACTION_REGISTRY.get(name)
                if handler is None:
                    actions_taken.append({"action": name, "error": "unknown action"})
                    status = "partial"
                    continue
                ctx = ActionContext(
                    rule=rule, facility=facility, event=event, params=spec.get("params") or {}
                )
                try:
                    outcome = await handler(db, ctx)
                    actions_taken.append({"action": name, "result": outcome})
                except Exception as exc:
                    log.error(
                        "automation action failed",
                        extra={"rule": rule.name, "action": name, "error": str(exc)},
                    )
                    actions_taken.append({"action": name, "error": str(exc)})
                    status = "failed"
                    detail = str(exc)

            rule.last_fired_at = now
            rule.fire_count += 1
            self._last_fired[rule.id] = time.monotonic()

            duration_ms = (time.perf_counter() - started) * 1000
            db.add(
                AutomationRun(
                    rule_id=rule.id, facility_id=facility_id, status=status,
                    trigger_event=trigger, actions_taken=actions_taken,
                    detail=detail, duration_ms=duration_ms,
                )
            )
            fired.append(
                {
                    "rule": rule.name, "rule_id": rule.id, "status": status,
                    "actions": actions_taken, "duration_ms": round(duration_ms, 2),
                }
            )
            await bus.emit(
                Topic.AUTOMATION_ACTION,
                {"rule": rule.name, "trigger": trigger, "status": status,
                 "actions": [a.get("action") for a in actions_taken]},
                facility_id=facility_id,
            )

        await db.flush()
        return {"trigger": trigger, "rules_evaluated": len(rules), "fired": fired}


automation_engine = AutomationEngine()


def register_automation_subscribers() -> None:
    for topic in TRIGGERS:
        if topic != "schedule:tick":
            bus.subscribe(topic, automation_engine.handle_event)
