"""Driver vehicle registration."""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import Conflict, NotFound
from app.models.user import Vehicle
from app.schemas.parking import VehicleCreate, VehicleOut
from app.services.anpr.plate_utils import is_valid_plate, normalize_plate, pretty_plate

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def add_vehicle(
    payload: VehicleCreate, user: CurrentUser, db: DbSession
) -> VehicleOut:
    plate = normalize_plate(payload.plate)
    if not is_valid_plate(plate):
        raise Conflict(
            f"'{payload.plate}' does not look like a valid registration number."
        )

    existing = (
        await db.execute(select(Vehicle).where(Vehicle.plate_normalized == plate))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(
            f"{pretty_plate(plate)} is already registered"
            + (" to your account." if existing.owner_id == user.id else " to another account.")
        )

    vehicle = Vehicle(
        owner_id=user.id,
        plate=pretty_plate(plate),
        plate_normalized=plate,
        **payload.model_dump(exclude={"plate"}),
    )
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return VehicleOut.model_validate(vehicle)


@router.get("", response_model=list[VehicleOut])
async def list_vehicles(user: CurrentUser, db: DbSession) -> list[VehicleOut]:
    rows = (
        await db.execute(
            select(Vehicle).where(Vehicle.owner_id == user.id).order_by(Vehicle.id)
        )
    ).scalars().all()
    return [VehicleOut.model_validate(row) for row in rows]


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_vehicle(vehicle_id: int, user: CurrentUser, db: DbSession) -> None:
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.owner_id != user.id:
        raise NotFound(f"Vehicle {vehicle_id} was not found on your account.")

    from app.models.enums import SessionStatus
    from app.models.parking import ParkingSession

    active = (
        await db.execute(
            select(ParkingSession).where(
                ParkingSession.vehicle_id == vehicle.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
        )
    ).scalars().first()
    if active is not None:
        raise Conflict("This vehicle is currently parked — it cannot be removed yet.")

    await db.delete(vehicle)
    await db.commit()
