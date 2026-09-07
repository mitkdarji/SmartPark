"""The parking lifecycle: sessions, ANPR recognition audit trail, anomalies."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import PaymentStatus, SessionStatus, Severity

if TYPE_CHECKING:
    from app.models.facility import Facility, Slot
    from app.models.user import User, Vehicle


class ParkingSession(Base, TimestampMixin):
    """One vehicle visit, from entry scan to settled bill."""

    __tablename__ = "parking_sessions"
    __table_args__ = (
        Index("ix_session_active", "facility_id", "status"),
        Index("ix_session_plate", "plate_normalized", "status"),
        Index("ix_session_entry", "facility_id", "entry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("slots.id", ondelete="SET NULL"))
    entry_gate_id: Mapped[int | None] = mapped_column(ForeignKey("gates.id", ondelete="SET NULL"))
    exit_gate_id: Mapped[int | None] = mapped_column(ForeignKey("gates.id", ondelete="SET NULL"))

    # Plate is denormalised onto the session: a guest vehicle may have no account.
    plate: Mapped[str] = mapped_column(String(20), nullable=False)
    plate_normalized: Mapped[str] = mapped_column(String(20), index=True, nullable=False)

    entry_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    exit_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.ACTIVE, nullable=False)

    # ── Allocation telemetry (drives the evaluation harness) ──
    allocation_strategy: Mapped[str | None] = mapped_column(String(16))
    allocation_reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    allocation_latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    walk_distance_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    candidates_considered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Vision telemetry ──────────────────────────────────────
    entry_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    exit_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entry_image_path: Mapped[str | None] = mapped_column(String(255))
    exit_image_path: Mapped[str | None] = mapped_column(String(255))

    # ── Billing ───────────────────────────────────────────────
    billable_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    payment_status: Mapped[str] = mapped_column(
        String(16), default=PaymentStatus.PENDING, nullable=False
    )
    invoice_no: Mapped[str | None] = mapped_column(String(32), unique=True)
    # Frozen copy of the rate card + surge components at the moment of billing.
    rate_breakdown: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Turn-by-turn waypoints handed to the driver app after allocation.
    navigation_path: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    denial_reason: Mapped[str | None] = mapped_column(String(160))

    facility: Mapped[Facility] = relationship()
    slot: Mapped[Slot | None] = relationship()
    vehicle: Mapped[Vehicle | None] = relationship(back_populates="sessions")
    user: Mapped[User | None] = relationship()

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE

    def elapsed_minutes(self, until: datetime | None = None) -> float:
        """Minutes parked. Named to avoid colliding with the API's
        `duration_minutes` field, which Pydantic would otherwise bind to this
        method object instead of a number."""
        end = self.exit_at or until
        if end is None:
            return 0.0
        return max(0.0, (end - self.entry_at).total_seconds() / 60.0)


class RecognitionEvent(Base, TimestampMixin):
    """Every ANPR inference, kept whether or not it produced a session.

    This is the dataset the OCR ensemble is evaluated on, and the evidence trail
    when a driver disputes a charge.
    """

    __tablename__ = "recognition_events"
    __table_args__ = (
        Index("ix_recognition_facility_created", "facility_id", "created_at"),
        Index("ix_recognition_plate", "plate_normalized"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="CASCADE"))
    gate_id: Mapped[int | None] = mapped_column(ForeignKey("gates.id", ondelete="SET NULL"))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("parking_sessions.id", ondelete="SET NULL")
    )

    direction: Mapped[str] = mapped_column(String(8), default="entry", nullable=False)
    plate_raw: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    plate_normalized: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    backend: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    # Per-backend proposals with scores — how consensus was reached.
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    plate_bbox: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(255))
    processing_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    corrected_plate: Mapped[str | None] = mapped_column(String(20))
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Anomaly(Base, TimestampMixin):
    """Output of the anomaly detector and rule-based security checks."""

    __tablename__ = "anomalies"
    __table_args__ = (Index("ix_anomaly_facility_created", "facility_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="CASCADE"))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("parking_sessions.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), default=Severity.LOW, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    summary: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
