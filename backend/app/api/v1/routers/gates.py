"""Gate operations — the endpoints a barrier controller actually calls."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, UploadFile

from app.api.deps import DbSession, PublicFacility, resolve_gate
from app.core.errors import Conflict
from app.models.enums import VehicleType
from app.schemas.parking import GateScanRequest
from app.services.parking import parking_service

router = APIRouter(prefix="/gates", tags=["gates"])

MAX_IMAGE_BYTES = 12 * 1024 * 1024


async def _read_image(file: UploadFile | None) -> bytes | None:
    if file is None:
        return None
    payload = await file.read()
    if not payload:
        return None
    if len(payload) > MAX_IMAGE_BYTES:
        raise Conflict("Camera frame is larger than the 12 MB limit.")
    return payload


@router.post("/entry", summary="Register a vehicle entry from a camera frame")
async def entry_from_image(
    db: DbSession,
    facility_id: int = Form(...),
    gate_id: int | None = Form(default=None),
    vehicle_type: str | None = Form(default=None),
    needs_charger: bool = Form(default=False),
    plate: str | None = Form(default=None, description="Manual override; skips ANPR."),
    image: UploadFile | None = File(default=None),
) -> dict:
    from app.api.deps import get_public_facility

    facility = await get_public_facility(facility_id, db)
    gate = await resolve_gate(db, facility, gate_id)
    result = await parking_service.handle_entry(
        db, facility,
        gate=gate,
        image_bytes=await _read_image(image),
        plate_override=plate,
        vehicle_type=vehicle_type or None,
        needs_charger=needs_charger,
    )
    return result.as_dict()


@router.post("/exit", summary="Settle and release a vehicle from a camera frame")
async def exit_from_image(
    db: DbSession,
    facility_id: int = Form(...),
    gate_id: int | None = Form(default=None),
    plate: str | None = Form(default=None, description="Manual override; skips ANPR."),
    image: UploadFile | None = File(default=None),
) -> dict:
    from app.api.deps import get_public_facility

    facility = await get_public_facility(facility_id, db)
    gate = await resolve_gate(db, facility, gate_id)
    result = await parking_service.handle_exit(
        db, facility, gate=gate,
        image_bytes=await _read_image(image),
        plate_override=plate,
    )
    return result.as_dict()


@router.post("/scan/entry", summary="Register an entry by plate number")
async def entry_by_plate(payload: GateScanRequest, db: DbSession) -> dict:
    from app.api.deps import get_public_facility

    facility = await get_public_facility(payload.facility_id, db)
    gate = await resolve_gate(db, facility, payload.gate_id)
    result = await parking_service.handle_entry(
        db, facility, gate=gate,
        plate_override=payload.plate,
        vehicle_type=payload.vehicle_type or VehicleType.HATCHBACK,
        needs_charger=payload.needs_charger,
    )
    return result.as_dict()


@router.post("/scan/exit", summary="Settle a session by plate number")
async def exit_by_plate(payload: GateScanRequest, db: DbSession) -> dict:
    from app.api.deps import get_public_facility

    facility = await get_public_facility(payload.facility_id, db)
    gate = await resolve_gate(db, facility, payload.gate_id)
    result = await parking_service.handle_exit(
        db, facility, gate=gate, plate_override=payload.plate
    )
    return result.as_dict()


@router.post("/{facility_id}/simulate", summary="Run a synthetic gate event end to end")
async def simulate_gate(
    facility: PublicFacility,
    db: DbSession,
    direction: str = Query(default="entry", pattern="^(entry|exit)$"),
    plate: str | None = Query(default=None),
    difficulty: float = Query(default=0.3, ge=0.0, le=1.0),
    gate_id: int | None = Query(default=None),
) -> dict:
    """Generate a synthetic camera frame and push it through the real pipeline.

    This is the demo path: it exercises detection, OCR, the ensemble, allocation
    and billing exactly as a physical camera would, with no hardware attached.
    """
    from sqlalchemy import select

    from app.models.enums import SessionStatus
    from app.models.parking import ParkingSession
    from app.services.anpr.synthetic import encode_png, synth_capture

    if direction == "exit" and not plate:
        # Pick a vehicle that is genuinely inside, so the demo settles a real bill.
        parked = (
            await db.execute(
                select(ParkingSession.plate_normalized)
                .where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.status == SessionStatus.ACTIVE,
                )
                .order_by(ParkingSession.entry_at)
                .limit(1)
            )
        ).scalars().first()
        if parked is None:
            raise Conflict("No vehicles are currently parked here, so none can exit.")
        plate = parked

    frame, ground_truth = synth_capture(plate, difficulty=difficulty)
    gate = await resolve_gate(db, facility, gate_id)
    image_bytes = encode_png(frame)

    if direction == "entry":
        result = await parking_service.handle_entry(
            db, facility, gate=gate, image_bytes=image_bytes
        )
    else:
        result = await parking_service.handle_exit(
            db, facility, gate=gate, image_bytes=image_bytes
        )

    payload = result.as_dict()
    payload["simulation"] = {
        "ground_truth_plate": ground_truth,
        "difficulty": difficulty,
        "read_correct": payload.get("plate") == ground_truth,
    }
    return payload
