"""Parking sessions, invoices, and the recognition audit trail."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, OwnedFacility
from app.core.errors import NotFound, PermissionDenied
from app.models.enums import SessionStatus, UserRole
from app.models.facility import Facility, Slot
from app.models.parking import Anomaly, ParkingSession, RecognitionEvent
from app.schemas.common import Page
from app.schemas.parking import AnomalyOut, RecognitionEventOut, SessionDetail
from app.services.billing.pricing import pricing_engine
from app.services.parking import parking_service

router = APIRouter(tags=["sessions"])


async def _detail(db, session: ParkingSession) -> SessionDetail:
    detail = SessionDetail.model_validate(session)
    facility = await db.get(Facility, session.facility_id)
    slot = await db.get(Slot, session.slot_id) if session.slot_id else None
    detail.facility_name = facility.name if facility else None
    detail.slot_code = slot.code if slot else None
    detail.zone = slot.zone if slot else None

    now = datetime.now(UTC)
    detail.duration_minutes = round(session.elapsed_minutes(session.exit_at or now), 1)

    if session.status == SessionStatus.ACTIVE and facility:
        _, _, occupancy = await parking_service.occupancy(db, facility.id)
        quote = pricing_engine.quote(
            facility,
            raw_minutes=detail.duration_minutes,
            occupancy=occupancy,
            slot_type=slot.slot_type if slot else "standard",
        )
        detail.live_amount_minor = quote.total_minor
    return detail


@router.get("/sessions/me", response_model=Page[SessionDetail])
async def my_sessions(
    user: CurrentUser,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[SessionDetail]:
    query = select(ParkingSession).where(ParkingSession.user_id == user.id)
    if status_filter:
        query = query.where(ParkingSession.status == status_filter)

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(ParkingSession.entry_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[await _detail(db, s) for s in rows],
        total=int(total), limit=limit, offset=offset,
    )


@router.get("/sessions/active", response_model=SessionDetail | None)
async def my_active_session(user: CurrentUser, db: DbSession) -> SessionDetail | None:
    session = (
        await db.execute(
            select(ParkingSession)
            .where(
                ParkingSession.user_id == user.id,
                ParkingSession.status == SessionStatus.ACTIVE,
            )
            .order_by(ParkingSession.entry_at.desc())
        )
    ).scalars().first()
    return await _detail(db, session) if session else None


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: int, user: CurrentUser, db: DbSession) -> SessionDetail:
    session = await db.get(ParkingSession, session_id)
    if session is None:
        raise NotFound(f"Session {session_id} was not found.")

    if session.user_id != user.id and user.role != UserRole.ADMIN:
        facility = await db.get(Facility, session.facility_id)
        if facility is None or facility.owner_id != user.id:
            raise PermissionDenied("You do not have access to this parking session.")
    return await _detail(db, session)


@router.get("/sessions/{session_id}/explain", response_model=dict)
async def explain_bill(session_id: int, user: CurrentUser, db: DbSession) -> dict:
    """Plain-language explanation of a bill — the first line of dispute handling."""
    from app.services.genai.copilot import copilot

    session = await db.get(ParkingSession, session_id)
    if session is None:
        raise NotFound(f"Session {session_id} was not found.")
    if session.user_id != user.id and user.role != UserRole.ADMIN:
        facility = await db.get(Facility, session.facility_id)
        if facility is None or facility.owner_id != user.id:
            raise PermissionDenied("You do not have access to this parking session.")
    return await copilot.explain_bill(db, session)


@router.get("/facilities/{facility_id}/sessions", response_model=Page[SessionDetail])
async def facility_sessions(
    facility: OwnedFacility,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    plate: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[SessionDetail]:
    query = select(ParkingSession).where(ParkingSession.facility_id == facility.id)
    if status_filter:
        query = query.where(ParkingSession.status == status_filter)
    if plate:
        from app.services.anpr.plate_utils import normalize_plate

        query = query.where(
            ParkingSession.plate_normalized.like(f"%{normalize_plate(plate)}%")
        )

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(ParkingSession.entry_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[await _detail(db, s) for s in rows],
        total=int(total), limit=limit, offset=offset,
    )


@router.get("/facilities/{facility_id}/recognitions", response_model=list[RecognitionEventOut])
async def recognition_log(
    facility: OwnedFacility,
    db: DbSession,
    needs_review: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RecognitionEventOut]:
    query = select(RecognitionEvent).where(RecognitionEvent.facility_id == facility.id)
    if needs_review is not None:
        query = query.where(RecognitionEvent.needs_review.is_(needs_review))
    rows = (
        await db.execute(query.order_by(RecognitionEvent.created_at.desc()).limit(limit))
    ).scalars().all()
    return [RecognitionEventOut.model_validate(row) for row in rows]


@router.post("/facilities/{facility_id}/recognitions/{event_id}/correct", response_model=RecognitionEventOut)
async def correct_recognition(
    event_id: int,
    plate: str,
    facility: OwnedFacility,
    db: DbSession,
) -> RecognitionEventOut:
    """Record the operator's correction of a misread plate.

    Corrections are the labelled data the OCR ensemble is evaluated against, so
    they are stored alongside the original read rather than overwriting it.
    """
    from app.services.anpr.plate_utils import normalize_plate

    event = await db.get(RecognitionEvent, event_id)
    if event is None or event.facility_id != facility.id:
        raise NotFound(f"Recognition event {event_id} was not found.")

    event.corrected_plate = normalize_plate(plate)
    event.needs_review = False
    event.decision = "operator_corrected"
    await db.commit()
    await db.refresh(event)
    return RecognitionEventOut.model_validate(event)


@router.get("/facilities/{facility_id}/anomalies", response_model=list[AnomalyOut])
async def list_anomalies(
    facility: OwnedFacility,
    db: DbSession,
    resolved: bool | None = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AnomalyOut]:
    query = select(Anomaly).where(Anomaly.facility_id == facility.id)
    if resolved is not None:
        query = query.where(Anomaly.resolved.is_(resolved))
    rows = (
        await db.execute(query.order_by(Anomaly.created_at.desc()).limit(limit))
    ).scalars().all()
    return [AnomalyOut.model_validate(row) for row in rows]


@router.post("/facilities/{facility_id}/anomalies/{anomaly_id}/resolve", response_model=AnomalyOut)
async def resolve_anomaly(
    anomaly_id: int,
    facility: OwnedFacility,
    db: DbSession,
    note: str = Query(default=""),
) -> AnomalyOut:
    anomaly = await db.get(Anomaly, anomaly_id)
    if anomaly is None or anomaly.facility_id != facility.id:
        raise NotFound(f"Anomaly {anomaly_id} was not found.")
    anomaly.resolved = True
    anomaly.resolved_at = datetime.now(UTC)
    anomaly.resolution_note = note
    await db.commit()
    await db.refresh(anomaly)
    return AnomalyOut.model_validate(anomaly)
