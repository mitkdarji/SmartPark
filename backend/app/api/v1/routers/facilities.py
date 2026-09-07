"""Facility management and the digital layout builder."""

from __future__ import annotations

import re
import uuid
from datetime import UTC

from fastapi import APIRouter, File, Query, UploadFile, status
from sqlalchemy import delete, func, select

from app.api.deps import DbSession, OwnedFacility, OwnerUser, PublicFacility
from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.core.events import Topic, bus
from app.models.enums import SlotStatus
from app.models.facility import AuthorizedVehicle, Facility, Gate, Level, Slot
from app.models.parking import ParkingSession
from app.schemas.facility import (
    AuthorizedVehicleCreate,
    AuthorizedVehicleOut,
    FacilityCreate,
    FacilityOut,
    FacilitySummary,
    FacilityUpdate,
    GateCreate,
    GateOut,
    GateUpdate,
    LevelCreate,
    LevelOut,
    SlotBulkUpsert,
    SlotOut,
    SlotStatusUpdate,
)
from app.services.anpr.plate_utils import normalize_plate
from app.services.billing.pricing import pricing_engine
from app.services.layout import layout_service
from app.services.parking import parking_service

router = APIRouter(prefix="/facilities", tags=["facilities"])


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def _summarise(db, facility: Facility) -> FacilitySummary:
    total, occupied, ratio = await parking_service.occupancy(db, facility.id)
    rate, _, _ = pricing_engine.effective_rate(facility, occupancy=ratio)
    active = (
        await db.execute(
            select(func.count(ParkingSession.id)).where(
                ParkingSession.facility_id == facility.id,
                ParkingSession.status == "active",
            )
        )
    ).scalar_one()
    summary = FacilitySummary.model_validate(facility)
    summary.capacity = total
    summary.occupied = occupied
    summary.free = total - occupied
    summary.occupancy_pct = round(ratio * 100, 1)
    summary.effective_rate_minor = rate
    summary.active_sessions = int(active)
    return summary


# ── Facilities ────────────────────────────────────────────────

@router.post("", response_model=FacilityOut, status_code=status.HTTP_201_CREATED)
async def create_facility(
    payload: FacilityCreate, db: DbSession, user: OwnerUser
) -> FacilityOut:
    facility = Facility(
        owner_id=user.id, slug=_slugify(payload.name), **payload.model_dump()
    )
    db.add(facility)
    await db.flush()

    # Every facility needs at least one level and one gate to be usable at all.
    level = Level(facility_id=facility.id, name="Ground", order_index=0)
    db.add(level)
    await db.flush()
    db.add(
        Gate(
            facility_id=facility.id, level_id=level.id, name="Main Entry",
            kind="bidirectional", x=0.0, y=0.0, is_primary=True,
        )
    )
    await db.commit()
    await db.refresh(facility)
    return FacilityOut.model_validate(facility)


@router.get("", response_model=list[FacilitySummary])
async def list_my_facilities(db: DbSession, user: OwnerUser) -> list[FacilitySummary]:
    facilities = (
        await db.execute(
            select(Facility).where(Facility.owner_id == user.id).order_by(Facility.id)
        )
    ).scalars().all()
    return [await _summarise(db, f) for f in facilities]


@router.get("/search", response_model=list[FacilitySummary])
async def search_facilities(
    db: DbSession,
    city: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Name or address fragment."),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[FacilitySummary]:
    """Public discovery — what a driver's app calls to find somewhere to park."""
    query = select(Facility).where(Facility.is_active.is_(True))
    if city:
        query = query.where(Facility.city.ilike(f"%{city}%"))
    if q:
        query = query.where(Facility.name.ilike(f"%{q}%") | Facility.address.ilike(f"%{q}%"))
    facilities = (await db.execute(query.limit(limit))).scalars().all()
    return [await _summarise(db, f) for f in facilities]


@router.get("/{facility_id}", response_model=FacilitySummary)
async def get_facility(facility: PublicFacility, db: DbSession) -> FacilitySummary:
    return await _summarise(db, facility)


@router.patch("/{facility_id}", response_model=FacilityOut)
async def update_facility(
    payload: FacilityUpdate, facility: OwnedFacility, db: DbSession
) -> FacilityOut:
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(facility, key, value)
    await db.commit()
    await db.refresh(facility)

    if {"base_rate_minor_per_hour", "dynamic_pricing_enabled", "pricing_config"} & changes.keys():
        _, _, ratio = await parking_service.occupancy(db, facility.id)
        rate, multiplier, components = pricing_engine.effective_rate(
            facility, occupancy=ratio
        )
        await bus.emit(
            Topic.PRICE_CHANGED,
            {
                "facility_id": facility.id, "effective_rate_minor": rate,
                "multiplier": round(multiplier, 3), "components": components,
                "reason": "operator updated the rate card",
            },
            facility_id=facility.id,
        )
    return FacilityOut.model_validate(facility)


@router.delete("/{facility_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_facility(facility: OwnedFacility, db: DbSession) -> None:
    active = (
        await db.execute(
            select(func.count(ParkingSession.id)).where(
                ParkingSession.facility_id == facility.id,
                ParkingSession.status == "active",
            )
        )
    ).scalar_one()
    if active:
        raise Conflict(
            f"{active} vehicles are still parked here. Close those sessions first."
        )
    await db.delete(facility)
    await db.commit()


# ── Levels ────────────────────────────────────────────────────

@router.post("/{facility_id}/levels", response_model=LevelOut, status_code=201)
async def create_level(
    payload: LevelCreate, facility: OwnedFacility, db: DbSession
) -> LevelOut:
    level = Level(facility_id=facility.id, **payload.model_dump())
    db.add(level)
    await db.commit()
    await db.refresh(level)
    return LevelOut.model_validate(level)


@router.get("/{facility_id}/levels", response_model=list[LevelOut])
async def list_levels(facility: PublicFacility, db: DbSession) -> list[LevelOut]:
    levels = (
        await db.execute(
            select(Level).where(Level.facility_id == facility.id).order_by(Level.order_index)
        )
    ).scalars().all()
    return [LevelOut.model_validate(level) for level in levels]


@router.patch("/{facility_id}/levels/{level_id}", response_model=LevelOut)
async def update_level(
    level_id: int, payload: LevelCreate, facility: OwnedFacility, db: DbSession
) -> LevelOut:
    level = await db.get(Level, level_id)
    if level is None or level.facility_id != facility.id:
        raise NotFound(f"Level {level_id} was not found at this facility.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(level, key, value)
    await db.commit()
    # Aisle changes move every slot's routed distance from the gate.
    await layout_service.recompute_distances(db, facility.id)
    await db.commit()
    await db.refresh(level)
    return LevelOut.model_validate(level)


# ── Layout / slots ────────────────────────────────────────────

@router.get("/{facility_id}/slots", response_model=list[SlotOut])
async def list_slots(
    facility: PublicFacility,
    db: DbSession,
    level_id: int | None = Query(default=None),
) -> list[SlotOut]:
    query = select(Slot).where(Slot.facility_id == facility.id)
    if level_id is not None:
        query = query.where(Slot.level_id == level_id)
    slots = (await db.execute(query.order_by(Slot.code))).scalars().all()
    return [SlotOut.model_validate(slot) for slot in slots]


@router.put("/{facility_id}/slots", response_model=list[SlotOut])
async def save_layout(
    payload: SlotBulkUpsert, facility: OwnedFacility, db: DbSession
) -> list[SlotOut]:
    """Save the whole level from the canvas editor.

    Insert, update and delete in one call, because that is what the editor's
    "Save layout" button means. Occupied bays are protected: removing one would
    orphan a parked vehicle, so the request is rejected instead.
    """
    level = await db.get(Level, payload.level_id)
    if level is None or level.facility_id != facility.id:
        raise NotFound(f"Level {payload.level_id} was not found at this facility.")

    existing = {
        slot.id: slot
        for slot in (
            await db.execute(
                select(Slot).where(
                    Slot.facility_id == facility.id, Slot.level_id == level.id
                )
            )
        ).scalars().all()
    }

    codes = [s.code for s in payload.slots]
    if len(codes) != len(set(codes)):
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        raise Conflict(f"Duplicate slot codes in the layout: {', '.join(duplicates)}")

    submitted_ids = {s.id for s in payload.slots if s.id}
    removed = [slot for sid, slot in existing.items() if sid not in submitted_ids]
    blocked = [s.code for s in removed if s.status == SlotStatus.OCCUPIED]
    if blocked:
        raise Conflict(
            f"Cannot delete occupied bays: {', '.join(sorted(blocked))}. "
            f"Wait for those vehicles to leave."
        )

    for slot in removed:
        await db.execute(delete(Slot).where(Slot.id == slot.id))

    saved: list[Slot] = []
    for item in payload.slots:
        if item.id and item.id in existing:
            slot = existing[item.id]
            for key, value in item.model_dump(exclude={"id"}).items():
                setattr(slot, key, value)
        else:
            slot = Slot(
                facility_id=facility.id, level_id=level.id,
                **item.model_dump(exclude={"id"}),
            )
            db.add(slot)
        saved.append(slot)

    await db.flush()
    await layout_service.recompute_distances(db, facility.id)
    await db.commit()

    for slot in saved:
        await db.refresh(slot)
    return [SlotOut.model_validate(slot) for slot in sorted(saved, key=lambda s: s.code)]


@router.post("/{facility_id}/slots/generate", response_model=list[SlotOut], status_code=201)
async def generate_grid(
    facility: OwnedFacility,
    db: DbSession,
    level_id: int = Query(...),
    rows: int = Query(default=4, ge=1, le=40),
    columns: int = Query(default=12, ge=1, le=60),
    zone_prefix: str = Query(default="A", max_length=2),
    ev_every: int = Query(default=0, ge=0, le=50, description="Make every Nth bay an EV bay."),
) -> list[SlotOut]:
    """Lay out a regular grid — the fast path for a rectangular deck."""
    slots = await layout_service.generate_grid(
        db, facility, level_id=level_id, rows=rows, columns=columns,
        zone_prefix=zone_prefix, ev_every=ev_every,
    )
    await db.commit()
    return [SlotOut.model_validate(slot) for slot in slots]


@router.patch("/{facility_id}/slots/{slot_id}/status", response_model=SlotOut)
async def set_slot_status(
    slot_id: int, payload: SlotStatusUpdate, facility: OwnedFacility, db: DbSession
) -> SlotOut:
    """Manual override — take a bay out of service, or free a stuck one."""
    slot = await db.get(Slot, slot_id)
    if slot is None or slot.facility_id != facility.id:
        raise NotFound(f"Slot {slot_id} was not found at this facility.")

    previous = slot.status
    slot.status = payload.status
    if payload.status == SlotStatus.EMPTY and previous != SlotStatus.EMPTY:
        from datetime import datetime

        slot.last_vacated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(slot)

    await bus.emit(
        Topic.SLOT_STATE_CHANGED,
        {
            "slot_id": slot.id, "slot_code": slot.code, "zone": slot.zone,
            "from": previous, "to": slot.status, "manual": True,
        },
        facility_id=facility.id,
    )
    return SlotOut.model_validate(slot)


@router.post("/{facility_id}/floorplan", response_model=dict)
async def upload_floorplan(
    facility: OwnedFacility,
    db: DbSession,
    level_id: int = Query(...),
    file: UploadFile = File(...),
    detect: bool = Query(default=True, description="Also propose slots from the image."),
) -> dict:
    """Upload a floor plan and optionally detect bay rectangles in it.

    Detection targets clean, structured plans — the Phase-3 stretch goal from the
    project scope. Results are *proposals*: they land in the canvas editor for
    the owner to accept, adjust or discard, never straight into the database.
    """
    level = await db.get(Level, level_id)
    if level is None or level.facility_id != facility.id:
        raise NotFound(f"Level {level_id} was not found at this facility.")

    payload = await file.read()
    if not payload:
        raise Conflict("The uploaded file is empty.")

    folder = settings.upload_dir / "floorplans" / str(facility.id)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = (file.filename or "plan.png").split(".")[-1][:5]
    path = folder / f"level_{level_id}_{uuid.uuid4().hex[:8]}.{suffix}"
    path.write_bytes(payload)

    level.floorplan_path = str(path.relative_to(settings.data_dir))
    await db.commit()

    result: dict = {
        "floorplan_path": level.floorplan_path,
        "size_bytes": len(payload),
        "detected_slots": [],
    }
    if detect:
        result.update(layout_service.detect_slots_from_image(payload))
    return result


# ── Gates ─────────────────────────────────────────────────────

@router.post("/{facility_id}/gates", response_model=GateOut, status_code=201)
async def create_gate(
    payload: GateCreate, facility: OwnedFacility, db: DbSession
) -> GateOut:
    if payload.is_primary:
        for gate in (
            await db.execute(select(Gate).where(Gate.facility_id == facility.id))
        ).scalars().all():
            gate.is_primary = False

    gate = Gate(facility_id=facility.id, **payload.model_dump())
    db.add(gate)
    await db.flush()
    await layout_service.recompute_distances(db, facility.id)
    await db.commit()
    await db.refresh(gate)
    return GateOut.model_validate(gate)


@router.get("/{facility_id}/gates", response_model=list[GateOut])
async def list_gates(facility: PublicFacility, db: DbSession) -> list[GateOut]:
    gates = (
        await db.execute(
            select(Gate).where(Gate.facility_id == facility.id).order_by(Gate.id)
        )
    ).scalars().all()
    return [GateOut.model_validate(gate) for gate in gates]


@router.patch("/{facility_id}/gates/{gate_id}", response_model=GateOut)
async def update_gate(
    gate_id: int, payload: GateUpdate, facility: OwnedFacility, db: DbSession
) -> GateOut:
    gate = await db.get(Gate, gate_id)
    if gate is None or gate.facility_id != facility.id:
        raise NotFound(f"Gate {gate_id} was not found at this facility.")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_primary"):
        # Exactly one primary gate: it is the origin every stored distance is
        # measured from, so two of them would make those distances meaningless.
        for other in (
            await db.execute(
                select(Gate).where(Gate.facility_id == facility.id, Gate.id != gate_id)
            )
        ).scalars().all():
            other.is_primary = False

    for key, value in changes.items():
        setattr(gate, key, value)
    await db.flush()

    if {"x", "y", "is_primary", "is_active", "level_id"} & changes.keys():
        await layout_service.recompute_distances(db, facility.id)

    await db.commit()
    await db.refresh(gate)
    return GateOut.model_validate(gate)


@router.delete("/{facility_id}/gates/{gate_id}", status_code=204, response_model=None)
async def delete_gate(gate_id: int, facility: OwnedFacility, db: DbSession) -> None:
    gate = await db.get(Gate, gate_id)
    if gate is None or gate.facility_id != facility.id:
        raise NotFound(f"Gate {gate_id} was not found at this facility.")

    remaining = (
        await db.execute(
            select(Gate).where(Gate.facility_id == facility.id, Gate.id != gate_id)
        )
    ).scalars().all()
    if not remaining:
        raise Conflict(
            "A facility needs at least one gate — vehicles have no way in otherwise."
        )

    await db.delete(gate)
    await db.flush()
    # Promote a successor if the primary was removed, then re-measure distances.
    if gate.is_primary:
        remaining[0].is_primary = True
        await db.flush()
    await layout_service.recompute_distances(db, facility.id)
    await db.commit()


# ── Authorised vehicles (private mode) ────────────────────────

@router.post("/{facility_id}/authorized", response_model=AuthorizedVehicleOut, status_code=201)
async def add_authorized_vehicle(
    payload: AuthorizedVehicleCreate, facility: OwnedFacility, db: DbSession
) -> AuthorizedVehicleOut:
    plate = normalize_plate(payload.plate)
    existing = (
        await db.execute(
            select(AuthorizedVehicle).where(
                AuthorizedVehicle.facility_id == facility.id,
                AuthorizedVehicle.plate_normalized == plate,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(f"{payload.plate} is already on the authorised list.")

    record = AuthorizedVehicle(
        facility_id=facility.id,
        plate_normalized=plate,
        **payload.model_dump(exclude={"plate"}),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return AuthorizedVehicleOut.model_validate(record)


@router.get("/{facility_id}/authorized", response_model=list[AuthorizedVehicleOut])
async def list_authorized_vehicles(
    facility: OwnedFacility, db: DbSession
) -> list[AuthorizedVehicleOut]:
    rows = (
        await db.execute(
            select(AuthorizedVehicle)
            .where(AuthorizedVehicle.facility_id == facility.id)
            .order_by(AuthorizedVehicle.plate_normalized)
        )
    ).scalars().all()
    return [AuthorizedVehicleOut.model_validate(row) for row in rows]


@router.delete("/{facility_id}/authorized/{record_id}", status_code=204, response_model=None)
async def remove_authorized_vehicle(
    record_id: int, facility: OwnedFacility, db: DbSession
) -> None:
    record = await db.get(AuthorizedVehicle, record_id)
    if record is None or record.facility_id != facility.id:
        raise NotFound("That authorised-vehicle record was not found.")
    await db.delete(record)
    await db.commit()
