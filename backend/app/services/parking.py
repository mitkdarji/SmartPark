"""The parking lifecycle — the service the whole platform exists to provide.

    entry:  recognise plate → authorise → allocate slot → route the driver
    exit:   recognise plate → price the stay → settle from the wallet → free the bay

Everything else (analytics, forecasting, automation, the agent) reads what these
two methods write. Three details are worth calling out:

  Context-primed exit reads.  The exit scan is given the list of plates currently
  parked inside as a prior. That set is small and known, so a noisy read snaps
  onto the right vehicle instead of inventing a phantom session.

  Idempotent settlement.  The wallet debit is keyed on the session id. A gate
  controller that retries after a timeout settles the bill exactly once.

  Billing survives payment failure.  If the wallet cannot cover the charge the
  session still closes, the bay is still freed, and the debt is recorded as
  `payment_status = failed`. A barrier that will not open because a wallet is
  empty is worse than an unpaid invoice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AccessDenied, Conflict, NotFound, RecognitionFailed
from app.core.events import Topic, bus
from app.core.logging import get_logger
from app.models.enums import (
    AnomalyKind,
    PaymentStatus,
    SessionStatus,
    Severity,
    SlotStatus,
    VehicleType,
)
from app.models.facility import AuthorizedVehicle, Facility, Gate, Slot
from app.models.parking import Anomaly, ParkingSession, RecognitionEvent
from app.models.user import User, Vehicle
from app.services.allocation.engine import allocator
from app.services.anpr.pipeline import RecognitionResult, anpr
from app.services.anpr.plate_utils import normalize_plate, pretty_plate
from app.services.billing.pricing import pricing_engine
from app.services.billing.wallet import wallet_service
from app.services.ml.anomaly import detector

log = get_logger(__name__)


@dataclass(slots=True)
class EntryResult:
    allowed: bool
    session: ParkingSession | None = None
    slot: Slot | None = None
    plate: str = ""
    confidence: float = 0.0
    reason: str = ""
    route: dict | None = None
    allocation: dict | None = None
    recognition: dict | None = None
    anomalies: list[dict] = field(default_factory=list)
    estimated_rate_minor: int = 0
    occupancy_pct: float = 0.0
    processing_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "session_id": self.session.id if self.session else None,
            "plate": self.plate,
            "plate_pretty": pretty_plate(self.plate),
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "slot": (
                {
                    "id": self.slot.id, "code": self.slot.code, "zone": self.slot.zone,
                    "level_id": self.slot.level_id, "type": self.slot.slot_type,
                    "x": self.slot.x, "y": self.slot.y,
                }
                if self.slot else None
            ),
            "route": self.route,
            "allocation": self.allocation,
            "recognition": self.recognition,
            "anomalies": self.anomalies,
            "estimated_rate_minor": self.estimated_rate_minor,
            "occupancy_pct": round(self.occupancy_pct * 100, 1),
            "processing_ms": round(self.processing_ms, 1),
        }


@dataclass(slots=True)
class ExitResult:
    settled: bool
    session: ParkingSession | None = None
    plate: str = ""
    confidence: float = 0.0
    duration_minutes: float = 0.0
    amount_minor: int = 0
    payment_status: str = PaymentStatus.PENDING
    quote: dict | None = None
    recognition: dict | None = None
    anomalies: list[dict] = field(default_factory=list)
    wallet_balance_minor: int | None = None
    reason: str = ""
    processing_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "settled": self.settled,
            "session_id": self.session.id if self.session else None,
            "plate": self.plate,
            "plate_pretty": pretty_plate(self.plate),
            "confidence": round(self.confidence, 4),
            "duration_minutes": round(self.duration_minutes, 1),
            "amount_minor": self.amount_minor,
            "payment_status": self.payment_status,
            "invoice_no": self.session.invoice_no if self.session else None,
            "quote": self.quote,
            "recognition": self.recognition,
            "anomalies": self.anomalies,
            "wallet_balance_minor": self.wallet_balance_minor,
            "reason": self.reason,
            "processing_ms": round(self.processing_ms, 1),
        }


class ParkingService:
    # ── Entry ─────────────────────────────────────────────────

    async def handle_entry(
        self,
        db: AsyncSession,
        facility: Facility,
        *,
        gate: Gate | None = None,
        image_bytes: bytes | None = None,
        plate_override: str | None = None,
        vehicle_type: str | None = None,
        needs_charger: bool = False,
    ) -> EntryResult:
        started = time.perf_counter()

        recognition = await self._recognise(
            db, facility, gate,
            image_bytes=image_bytes, plate_override=plate_override,
            direction="entry", known_plates=[],
        )
        plate = recognition.plate
        if not plate:
            raise RecognitionFailed(
                "No readable number plate was found in the frame.",
                details=recognition.as_dict(),
            )

        # Resolve the vehicle and its owner. An unknown plate is still logged and
        # billed as a guest at a public facility — that is the whole point of ANPR.
        vehicle = (
            await db.execute(select(Vehicle).where(Vehicle.plate_normalized == plate))
        ).scalar_one_or_none()
        owner: User | None = await db.get(User, vehicle.owner_id) if vehicle else None

        open_count = (
            await db.execute(
                select(func.count(ParkingSession.id)).where(
                    ParkingSession.plate_normalized == plate,
                    ParkingSession.status == SessionStatus.ACTIVE,
                )
            )
        ).scalar_one()

        authorised: bool | None = None
        if facility.is_private:
            authorised = await self._is_authorised(db, facility.id, plate)

        recent = (
            await db.execute(
                select(ParkingSession.plate_normalized, ParkingSession.entry_at)
                .where(ParkingSession.facility_id == facility.id)
                .order_by(ParkingSession.entry_at.desc())
                .limit(3)
            )
        ).all()

        findings = detector.check_entry(
            plate=plate,
            confidence=recognition.confidence,
            min_confidence=facility_min_confidence(facility),
            recent_entries=[{"plate_normalized": p, "entry_at": t} for p, t in recent],
            open_sessions_for_plate=int(open_count),
            authorised=authorised,
        )

        # Private facility, unauthorised plate → refuse and record the attempt.
        if facility.is_private and not authorised:
            session = ParkingSession(
                facility_id=facility.id, plate=pretty_plate(plate), plate_normalized=plate,
                entry_at=datetime.now(UTC), entry_gate_id=gate.id if gate else None,
                status=SessionStatus.DENIED, entry_confidence=recognition.confidence,
                entry_image_path=recognition.image_path,
                denial_reason="vehicle is not on the authorised list",
                vehicle_id=vehicle.id if vehicle else None,
                user_id=owner.id if owner else None,
            )
            db.add(session)
            await db.flush()
            await self._record_anomalies(db, facility.id, session.id, findings)
            await db.commit()

            await bus.emit(
                Topic.ENTRY_DENIED,
                {
                    "plate": plate, "facility": facility.name,
                    "reason": "not authorised", "session_id": session.id,
                },
                facility_id=facility.id,
            )
            raise AccessDenied(
                f"Vehicle {pretty_plate(plate)} is not authorised to enter {facility.name}.",
                details={"plate": plate, "facility_id": facility.id},
            )

        if open_count:
            raise Conflict(
                f"Vehicle {pretty_plate(plate)} already has an open parking session.",
                details={"plate": plate, "open_sessions": int(open_count)},
            )

        resolved_type = vehicle_type or (vehicle.vehicle_type if vehicle else VehicleType.HATCHBACK)
        outcome = await allocator.allocate(
            db, facility,
            gate=gate,
            vehicle_type=resolved_type,
            is_ev=bool(vehicle and vehicle.is_ev),
            needs_charger=needs_charger,
            accessible=bool(vehicle and vehicle.is_accessible_permit),
            plate_normalized=plate,
        )
        if not outcome.ok or outcome.slot is None:
            await db.commit()
            raise Conflict(
                f"{facility.name} is full — no slot could be allocated.",
                details={"reason": outcome.reason, "considered": outcome.considered},
            )

        slot = outcome.slot
        occupancy = await self.occupancy(db, facility.id)
        rate, _, _ = pricing_engine.effective_rate(
            facility, occupancy=occupancy[2], slot_type=slot.slot_type
        )

        session = ParkingSession(
            facility_id=facility.id,
            vehicle_id=vehicle.id if vehicle else None,
            user_id=owner.id if owner else None,
            slot_id=slot.id,
            entry_gate_id=gate.id if gate else None,
            plate=pretty_plate(plate),
            plate_normalized=plate,
            entry_at=datetime.now(UTC),
            status=SessionStatus.ACTIVE,
            allocation_strategy=outcome.strategy,
            allocation_reason=outcome.reason,
            allocation_latency_ms=outcome.latency_ms,
            walk_distance_m=outcome.walk_distance_m,
            candidates_considered=outcome.considered,
            entry_confidence=recognition.confidence,
            entry_image_path=recognition.image_path,
            navigation_path=outcome.route.as_dict() if outcome.route else {},
            currency=facility.currency,
        )
        db.add(session)
        await db.flush()

        await self._link_recognition(db, recognition, session.id, decision="entry_allowed")
        await self._record_anomalies(db, facility.id, session.id, findings)
        await db.commit()

        await bus.emit(
            Topic.VEHICLE_ENTERED,
            {
                "session_id": session.id, "plate": plate, "slot_id": slot.id,
                "slot_code": slot.code, "zone": slot.zone,
                "confidence": recognition.confidence, "strategy": outcome.strategy,
            },
            facility_id=facility.id, user_id=owner.id if owner else None,
        )
        await bus.emit(
            Topic.SLOT_ALLOCATED,
            {
                "session_id": session.id, "slot_id": slot.id, "slot_code": slot.code,
                "zone": slot.zone, "level_id": slot.level_id, "plate": plate,
                "route": outcome.route.as_dict() if outcome.route else None,
                "reason": outcome.reason,
            },
            facility_id=facility.id, user_id=owner.id if owner else None,
        )
        await self._emit_occupancy(db, facility)

        return EntryResult(
            allowed=True, session=session, slot=slot, plate=plate,
            confidence=recognition.confidence, reason=outcome.reason,
            route=outcome.route.as_dict() if outcome.route else None,
            allocation=outcome.as_dict(), recognition=recognition.as_dict(),
            anomalies=[f.as_dict() for f in findings],
            estimated_rate_minor=rate, occupancy_pct=occupancy[2],
            processing_ms=(time.perf_counter() - started) * 1000,
        )

    # ── Exit ──────────────────────────────────────────────────

    async def handle_exit(
        self,
        db: AsyncSession,
        facility: Facility,
        *,
        gate: Gate | None = None,
        image_bytes: bytes | None = None,
        plate_override: str | None = None,
    ) -> ExitResult:
        started = time.perf_counter()

        # Prime the reader with the plates actually inside the facility.
        parked = (
            await db.execute(
                select(ParkingSession.plate_normalized).where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.status == SessionStatus.ACTIVE,
                )
            )
        ).scalars().all()

        recognition = await self._recognise(
            db, facility, gate,
            image_bytes=image_bytes, plate_override=plate_override,
            direction="exit", known_plates=list(parked),
        )
        plate = recognition.plate
        if not plate:
            raise RecognitionFailed(
                "No readable number plate was found at the exit gate.",
                details=recognition.as_dict(),
            )

        session = (
            await db.execute(
                select(ParkingSession)
                .where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.plate_normalized == plate,
                    ParkingSession.status == SessionStatus.ACTIVE,
                )
                .order_by(ParkingSession.entry_at.desc())
            )
        ).scalars().first()

        if session is None:
            raise NotFound(
                f"No active parking session found for {pretty_plate(plate)} at {facility.name}.",
                details={"plate": plate, "read_confidence": recognition.confidence},
            )

        # The plate may have been registered *after* this session began. Re-resolve
        # the owner now so the stay is settled from their wallet rather than being
        # written off as an uncollected guest visit.
        if session.user_id is None:
            registered = (
                await db.execute(
                    select(Vehicle).where(Vehicle.plate_normalized == plate)
                )
            ).scalar_one_or_none()
            if registered is not None:
                session.user_id = registered.owner_id
                session.vehicle_id = registered.id
                log.info(
                    "resolved a guest session to a now-registered owner",
                    extra={"session_id": session.id, "plate": plate},
                )

        now = datetime.now(UTC)
        duration_minutes = session.elapsed_minutes(now)
        slot = await db.get(Slot, session.slot_id) if session.slot_id else None
        _, _, occupancy = await self.occupancy(db, facility.id)

        waive = False
        if facility.is_private:
            auth = (
                await db.execute(
                    select(AuthorizedVehicle).where(
                        AuthorizedVehicle.facility_id == facility.id,
                        AuthorizedVehicle.plate_normalized == plate,
                    )
                )
            ).scalar_one_or_none()
            waive = bool(auth and auth.waive_charges)

        quote = pricing_engine.quote(
            facility,
            raw_minutes=duration_minutes,
            occupancy=occupancy,
            entry_at=session.entry_at,
            exit_at=now,
            slot_type=slot.slot_type if slot else "standard",
            waive=waive,
        )

        session.exit_at = now
        session.exit_gate_id = gate.id if gate else None
        session.exit_confidence = recognition.confidence
        session.exit_image_path = recognition.image_path
        session.status = SessionStatus.COMPLETED
        session.billable_minutes = quote.billable_minutes
        session.subtotal_minor = quote.subtotal_minor
        session.discount_minor = quote.discount_minor
        session.tax_minor = quote.tax_minor
        session.total_minor = quote.total_minor
        session.rate_breakdown = quote.as_dict()
        session.invoice_no = f"SP-{facility.id:03d}-{session.id:06d}"

        wallet_balance: int | None = None
        payment_reason = ""

        if quote.total_minor == 0:
            session.payment_status = PaymentStatus.WAIVED if waive else PaymentStatus.PAID
            payment_reason = quote.explanation
        elif session.user_id is None:
            # A guest vehicle has no wallet to charge; the operator collects at the
            # gate. Recording it as pending keeps the reconciliation honest.
            session.payment_status = PaymentStatus.PENDING
            payment_reason = "Guest vehicle — no linked wallet; collect at the gate."
        else:
            wallet = await wallet_service.get_or_create(db, session.user_id)
            try:
                await wallet_service.debit(
                    db, wallet, quote.total_minor,
                    description=f"Parking at {facility.name} ({quote.billable_minutes} min)",
                    session_id=session.id,
                    idempotency_key=f"session-exit:{session.id}",
                )
                session.payment_status = PaymentStatus.PAID
                wallet_balance = wallet.balance_minor
                payment_reason = quote.explanation
            except Exception as exc:
                # Never trap a vehicle behind a barrier over a payment problem.
                session.payment_status = PaymentStatus.FAILED
                wallet_balance = wallet.balance_minor
                payment_reason = (
                    f"Payment could not be collected ({exc}). The charge is recorded "
                    f"as outstanding on the account."
                )
                log.warning(
                    "exit payment failed",
                    extra={"session_id": session.id, "error": str(exc)},
                )

        if slot is not None:
            await allocator.release(
                db, slot.id, occupied_minutes=int(duration_minutes)
            )

        findings = detector.check_exit(
            session_id=session.id,
            plate=plate,
            dwell_seconds=duration_minutes * 60,
            entry_confidence=session.entry_confidence,
            exit_confidence=recognition.confidence,
            amount_minor=quote.total_minor,
            overstay_hours=facility.overstay_after_hours,
        )
        if session.payment_status == PaymentStatus.FAILED:
            from app.services.ml.anomaly import Finding

            findings.append(
                Finding(
                    kind=AnomalyKind.PAYMENT_FAILURE, severity=Severity.HIGH, score=1.0,
                    summary=f"Charge of {quote.total_minor / 100:.2f} could not be collected from {plate}.",
                    detail={"amount_minor": quote.total_minor}, session_id=session.id,
                )
            )
        model_finding = detector.score(
            facility.id,
            {
                "id": session.id, "duration_minutes": duration_minutes,
                "total_minor": quote.total_minor, "entry_confidence": session.entry_confidence,
                "exit_confidence": recognition.confidence, "entry_at": session.entry_at,
                "walk_distance_m": session.walk_distance_m,
            },
        )
        if model_finding:
            findings.append(model_finding)

        await self._link_recognition(db, recognition, session.id, decision="exit_settled")
        await self._record_anomalies(db, facility.id, session.id, findings)
        await db.commit()

        await bus.emit(
            Topic.VEHICLE_EXITED,
            {
                "session_id": session.id, "plate": plate,
                "duration_minutes": round(duration_minutes, 1),
                "amount_minor": quote.total_minor,
                "payment_status": session.payment_status,
                "invoice_no": session.invoice_no,
            },
            facility_id=facility.id, user_id=session.user_id,
        )
        if slot is not None:
            await bus.emit(
                Topic.SLOT_RELEASED,
                {
                    "slot_id": slot.id, "slot_code": slot.code, "zone": slot.zone,
                    "level_id": slot.level_id, "vacated_at": now.isoformat(),
                },
                facility_id=facility.id,
            )
        await self._emit_occupancy(db, facility)

        return ExitResult(
            settled=session.payment_status in (PaymentStatus.PAID, PaymentStatus.WAIVED),
            session=session, plate=plate, confidence=recognition.confidence,
            duration_minutes=duration_minutes, amount_minor=quote.total_minor,
            payment_status=session.payment_status, quote=quote.as_dict(),
            recognition=recognition.as_dict(),
            anomalies=[f.as_dict() for f in findings],
            wallet_balance_minor=wallet_balance, reason=payment_reason,
            processing_ms=(time.perf_counter() - started) * 1000,
        )

    # ── Shared helpers ────────────────────────────────────────

    async def occupancy(self, db: AsyncSession, facility_id: int) -> tuple[int, int, float]:
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
        total, occupied = int(total), int(occupied)
        return total, occupied, (occupied / total if total else 0.0)

    async def _recognise(
        self,
        db: AsyncSession,
        facility: Facility,
        gate: Gate | None,
        *,
        image_bytes: bytes | None,
        plate_override: str | None,
        direction: str,
        known_plates: list[str],
    ) -> RecognitionResult:
        """Read a plate from an image, or accept one supplied directly.

        `plate_override` is how the gate-simulator, the operator console's manual
        correction, and the test suite inject a plate without an image.
        """
        if plate_override:
            plate = normalize_plate(plate_override)
            result = RecognitionResult(
                plate=plate, plate_pretty=pretty_plate(plate), confidence=1.0,
                accepted=True, needs_review=False, backend="manual",
            )
        elif image_bytes:
            result = anpr.recognize_bytes(
                image_bytes, known_plates=known_plates, tag=direction
            )
        else:
            raise RecognitionFailed("Provide either a camera frame or a plate number.")

        event = RecognitionEvent(
            facility_id=facility.id,
            gate_id=gate.id if gate else None,
            direction=direction,
            plate_raw=result.plate,
            plate_normalized=result.plate,
            confidence=result.confidence,
            backend=result.backend,
            candidates=result.candidates,
            plate_bbox=result.bbox,
            image_path=result.image_path,
            processing_ms=result.processing_ms,
            decision="pending",
            needs_review=result.needs_review,
            meta={"escalated": result.escalated, "detections": result.detections},
        )
        db.add(event)
        await db.flush()
        result.event_id = event.id
        return result

    async def _link_recognition(
        self, db: AsyncSession, result: RecognitionResult, session_id: int, *, decision: str
    ) -> None:
        if not result.event_id:
            return
        event = await db.get(RecognitionEvent, result.event_id)
        if event:
            event.session_id = session_id
            event.decision = decision

    async def _is_authorised(self, db: AsyncSession, facility_id: int, plate: str) -> bool:
        auth = (
            await db.execute(
                select(AuthorizedVehicle).where(
                    AuthorizedVehicle.facility_id == facility_id,
                    AuthorizedVehicle.plate_normalized == plate,
                )
            )
        ).scalar_one_or_none()
        return bool(auth and auth.is_valid_at(datetime.now(UTC)))

    async def _record_anomalies(
        self, db: AsyncSession, facility_id: int, session_id: int | None, findings: list
    ) -> None:
        for finding in findings:
            db.add(
                Anomaly(
                    facility_id=facility_id,
                    session_id=finding.session_id or session_id,
                    kind=finding.kind, severity=finding.severity, score=finding.score,
                    summary=finding.summary, detail=finding.detail,
                )
            )
            await bus.emit(
                Topic.ANOMALY_DETECTED,
                {
                    "kind": finding.kind, "severity": finding.severity,
                    "summary": finding.summary, "session_id": session_id,
                },
                facility_id=facility_id,
            )
        if findings:
            await db.flush()

    async def _emit_occupancy(self, db: AsyncSession, facility: Facility) -> None:
        total, occupied, ratio = await self.occupancy(db, facility.id)
        rate, multiplier, components = pricing_engine.effective_rate(
            facility, occupancy=ratio
        )
        await bus.emit(
            Topic.OCCUPANCY_CHANGED,
            {
                "capacity": total, "occupied": occupied, "free": total - occupied,
                "occupancy_pct": round(ratio * 100, 1),
                "effective_rate_minor": rate, "multiplier": round(multiplier, 3),
                "components": components,
            },
            facility_id=facility.id,
        )


async def claim_open_sessions(db: AsyncSession, vehicle: Vehicle) -> int:
    """Attach any in-progress session for this plate to its newly-registered owner.

    A driver who parks first and signs up second — which is what people actually
    do — would otherwise see "not parked" while their car is demonstrably inside,
    and be billed as a guest at the exit gate despite having a funded wallet. The
    owner link is captured once at entry, so without this it is never revisited.

    Only ACTIVE sessions are claimed. Completed ones stay with whoever they were
    recorded against: retroactively attaching a settled visit would hand a
    previous keeper's parking history to whoever registers the plate next.

    Note the trust boundary. Registering a plate already grants every *future*
    session for it, so claiming the current one adds no new exposure — but it
    does make the gap visible: nothing here proves the registrant owns the
    vehicle. Production needs real verification (an RC lookup, or an OTP to the
    mobile number on the registration) before this endpoint can be trusted.
    """
    result = await db.execute(
        update(ParkingSession)
        .where(
            ParkingSession.plate_normalized == vehicle.plate_normalized,
            ParkingSession.status == SessionStatus.ACTIVE,
            ParkingSession.user_id.is_(None),
        )
        .values(user_id=vehicle.owner_id, vehicle_id=vehicle.id)
    )
    claimed = int(result.rowcount or 0)
    if claimed:
        log.info(
            "claimed in-progress sessions for a newly registered vehicle",
            extra={"plate": vehicle.plate_normalized, "owner_id": vehicle.owner_id,
                   "sessions": claimed},
        )
    return claimed


def facility_min_confidence(facility: Facility) -> float:
    from app.core.config import settings

    return float((facility.pricing_config or {}).get(
        "anpr_min_confidence", settings.anpr_min_confidence
    ))


parking_service = ParkingService()
