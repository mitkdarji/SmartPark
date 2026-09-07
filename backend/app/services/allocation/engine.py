"""Database-facing slot allocator.

Wraps the pure policies in the concerns a live system has: eligibility from
real vehicle records, zone load from live occupancy, reserved bays for
authorised vehicles, wayfinding, and — critically — *atomic claiming*.

Two cars can reach two gates in the same millisecond. The engine therefore
never trusts the read it used to pick a slot: it claims with a conditional
UPDATE and, if another transaction won the race, drops that slot from the pool
and re-runs the policy. That is a compare-and-swap loop, and it is why the
system cannot double-allocate a bay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import AllocationStrategy, SlotStatus, VehicleType
from app.models.facility import AuthorizedVehicle, Facility, Gate, Level, Slot
from app.services.allocation.navigation import Route, route_to_slot
from app.services.allocation.strategies import AllocationRequest, SlotView, choose_slot

log = get_logger(__name__)

MAX_CLAIM_ATTEMPTS = 5


@dataclass(slots=True)
class AllocationOutcome:
    slot: Slot | None
    route: Route | None
    strategy: str
    reason: str
    considered: int
    latency_ms: float
    walk_distance_m: float = 0.0
    contention_retries: int = 0
    scores: dict[int, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.slot is not None

    def as_dict(self) -> dict:
        return {
            "slot_id": self.slot.id if self.slot else None,
            "slot_code": self.slot.code if self.slot else None,
            "zone": self.slot.zone if self.slot else None,
            "level_id": self.slot.level_id if self.slot else None,
            "strategy": self.strategy,
            "reason": self.reason,
            "considered": self.considered,
            "latency_ms": round(self.latency_ms, 2),
            "walk_distance_m": round(self.walk_distance_m, 1),
            "contention_retries": self.contention_retries,
            "route": self.route.as_dict() if self.route else None,
        }


class AllocationEngine:
    async def allocate(
        self,
        db: AsyncSession,
        facility: Facility,
        *,
        gate: Gate | None = None,
        vehicle_type: str = VehicleType.HATCHBACK,
        is_ev: bool = False,
        needs_charger: bool = False,
        accessible: bool = False,
        preferred_zone: str | None = None,
        strategy: str | None = None,
        plate_normalized: str | None = None,
    ) -> AllocationOutcome:
        started = time.perf_counter()
        strategy = strategy or facility.allocation_strategy or AllocationStrategy.HYBRID
        entry_point = gate.position if gate else (0.0, 0.0)

        # An authorised vehicle with a dedicated bay skips the policy entirely.
        if plate_normalized and facility.is_private:
            reserved = await self._reserved_slot(db, facility.id, plate_normalized)
            if reserved is not None:
                claimed = await self._claim(db, reserved.id)
                if claimed:
                    route = await self._route(db, facility, gate, reserved)
                    return AllocationOutcome(
                        slot=reserved, route=route, strategy="reserved",
                        reason=f"dedicated bay {reserved.code} for authorised vehicle",
                        considered=1,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        walk_distance_m=route.distance_m if route else 0.0,
                    )

        free_slots = await self._free_slots(db, facility.id)
        zone_load = await self._zone_load(db, facility.id)
        primary_point = await self._primary_gate_point(db, facility.id)

        excluded: set[int] = set()
        retries = 0

        for _ in range(MAX_CLAIM_ATTEMPTS):
            pool = [s for s in free_slots if s.id not in excluded]
            if not pool:
                break

            request = AllocationRequest(
                vehicle_type=vehicle_type,
                is_ev=is_ev,
                needs_charger=needs_charger,
                accessible=accessible,
                entry_point=entry_point,
                preferred_zone=preferred_zone,
                now=datetime.now(UTC),
                zone_load=zone_load,
                primary_point=primary_point,
            )
            decision = choose_slot(pool, request, strategy)
            if decision.slot is None:
                break

            if await self._claim(db, decision.slot.id):
                slot = await db.get(Slot, decision.slot.id)
                route = await self._route(db, facility, gate, slot) if slot else None
                latency = (time.perf_counter() - started) * 1000
                log.info(
                    "slot allocated",
                    extra={
                        "facility_id": facility.id, "slot": slot.code if slot else None,
                        "strategy": strategy, "ms": round(latency, 2), "retries": retries,
                    },
                )
                return AllocationOutcome(
                    slot=slot, route=route, strategy=strategy, reason=decision.reason,
                    considered=decision.considered, latency_ms=latency,
                    walk_distance_m=route.distance_m if route else 0.0,
                    contention_retries=retries, scores=decision.scores,
                )

            # Lost the race — someone else took it. Retry without that slot.
            excluded.add(decision.slot.id)
            retries += 1

        return AllocationOutcome(
            slot=None, route=None, strategy=strategy,
            reason="facility full — no eligible slot could be claimed",
            considered=len(free_slots),
            latency_ms=(time.perf_counter() - started) * 1000,
            contention_retries=retries,
        )

    async def release(self, db: AsyncSession, slot_id: int, *, occupied_minutes: int = 0) -> Slot | None:
        """Free a slot and stamp `last_vacated_at` — the recency policy's input."""
        now = datetime.now(UTC)
        await db.execute(
            update(Slot)
            .where(Slot.id == slot_id)
            .values(
                status=SlotStatus.EMPTY,
                last_vacated_at=now,
                reserved_until=None,
                total_occupied_minutes=Slot.total_occupied_minutes + max(0, occupied_minutes),
            )
        )
        return await db.get(Slot, slot_id)

    # ── Internals ─────────────────────────────────────────────

    async def _claim(self, db: AsyncSession, slot_id: int) -> bool:
        """Conditional update: succeeds only if the slot is still empty."""
        now = datetime.now(UTC)
        result = await db.execute(
            update(Slot)
            .where(Slot.id == slot_id, Slot.status == SlotStatus.EMPTY, Slot.is_active.is_(True))
            .values(
                status=SlotStatus.OCCUPIED,
                last_occupied_at=now,
                total_uses=Slot.total_uses + 1,
            )
        )
        return bool(result.rowcount)

    async def _free_slots(self, db: AsyncSession, facility_id: int) -> list[SlotView]:
        rows = (
            await db.execute(
                select(Slot).where(
                    Slot.facility_id == facility_id,
                    Slot.status == SlotStatus.EMPTY,
                    Slot.is_active.is_(True),
                )
            )
        ).scalars().all()
        return [
            SlotView(
                id=s.id, code=s.code, zone=s.zone, level_id=s.level_id,
                x=s.center[0], y=s.center[1], slot_type=s.slot_type,
                distance_from_entry=s.distance_from_entry,
                has_ev_charger=s.has_ev_charger,
                last_vacated_at=s.last_vacated_at,
                total_uses=s.total_uses,
                price_multiplier=s.price_multiplier,
            )
            for s in rows
        ]

    async def _primary_gate_point(
        self, db: AsyncSession, facility_id: int
    ) -> tuple[float, float] | None:
        gate = (
            await db.execute(
                select(Gate)
                .where(Gate.facility_id == facility_id, Gate.is_active.is_(True))
                .order_by(Gate.is_primary.desc(), Gate.id)
            )
        ).scalars().first()
        return gate.position if gate else None

    async def _zone_load(self, db: AsyncSession, facility_id: int) -> dict[str, float]:
        rows = (
            await db.execute(
                select(Slot.zone, Slot.status).where(
                    Slot.facility_id == facility_id, Slot.is_active.is_(True)
                )
            )
        ).all()
        totals: dict[str, int] = {}
        occupied: dict[str, int] = {}
        for zone, status in rows:
            totals[zone] = totals.get(zone, 0) + 1
            if status != SlotStatus.EMPTY:
                occupied[zone] = occupied.get(zone, 0) + 1
        return {z: occupied.get(z, 0) / n for z, n in totals.items() if n}

    async def _reserved_slot(
        self, db: AsyncSession, facility_id: int, plate_normalized: str
    ) -> Slot | None:
        auth = (
            await db.execute(
                select(AuthorizedVehicle).where(
                    AuthorizedVehicle.facility_id == facility_id,
                    AuthorizedVehicle.plate_normalized == plate_normalized,
                    AuthorizedVehicle.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if auth is None or auth.reserved_slot_id is None:
            return None
        slot = await db.get(Slot, auth.reserved_slot_id)
        if slot and slot.status == SlotStatus.EMPTY and slot.is_active:
            return slot
        return None

    async def _route(
        self, db: AsyncSession, facility: Facility, gate: Gate | None, slot: Slot | None
    ) -> Route | None:
        if slot is None:
            return None
        level = await db.get(Level, slot.level_id)
        gate_point = gate.position if gate else (0.0, 0.0)
        return route_to_slot(
            gate_point,
            slot.center,
            aisles=level.aisles if level else [],
            slot_code=slot.code,
            zone=slot.zone,
            level_id=slot.level_id,
        )


allocator = AllocationEngine()
