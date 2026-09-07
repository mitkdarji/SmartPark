"""Facility topology: levels, slots, gates, access control, and time series."""

from __future__ import annotations

import math
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import (
    AccessMode,
    AllocationStrategy,
    GateKind,
    SlotStatus,
    SlotType,
)

if TYPE_CHECKING:
    from app.models.user import User


class Facility(Base, TimestampMixin):
    __tablename__ = "facilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    address: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    city: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(48), default="Asia/Kolkata", nullable=False)

    access_mode: Mapped[str] = mapped_column(String(12), default=AccessMode.PUBLIC, nullable=False)
    allocation_strategy: Mapped[str] = mapped_column(
        String(16), default=AllocationStrategy.HYBRID, nullable=False
    )

    # ── Pricing ───────────────────────────────────────────────
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    base_rate_minor_per_hour: Mapped[int] = mapped_column(Integer, default=4000, nullable=False)
    free_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    billing_increment_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    daily_cap_minor: Mapped[int] = mapped_column(Integer, default=40_000, nullable=False)
    tax_percent: Mapped[float] = mapped_column(Float, default=18.0, nullable=False)
    dynamic_pricing_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Surge curve + EV/accessible modifiers; see services/billing/pricing.py
    pricing_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # ── Operations ────────────────────────────────────────────
    overstay_after_hours: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    reservation_hold_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    publish_to_city_feed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    amenities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    owner: Mapped[User] = relationship(back_populates="facilities")
    levels: Mapped[list[Level]] = relationship(
        back_populates="facility", cascade="all, delete-orphan", order_by="Level.order_index"
    )
    slots: Mapped[list[Slot]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )
    gates: Mapped[list[Gate]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )
    authorized_vehicles: Mapped[list[AuthorizedVehicle]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )

    @property
    def is_private(self) -> bool:
        return self.access_mode == AccessMode.PRIVATE


class Level(Base, TimestampMixin):
    """One floor / deck. The canvas layout builder edits one level at a time."""

    __tablename__ = "levels"
    __table_args__ = (UniqueConstraint("facility_id", "name", name="uq_level_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Canvas dimensions in metres.
    # Extent in metres. The canvas editor scales these to pixels for drawing.
    canvas_width: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    canvas_height: Mapped[float] = mapped_column(Float, default=40.0, nullable=False)
    floorplan_path: Mapped[str | None] = mapped_column(String(255))
    # Drivable aisle polylines used by the wayfinding path builder.
    aisles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    facility: Mapped[Facility] = relationship(back_populates="levels")
    slots: Mapped[list[Slot]] = relationship(
        back_populates="level", cascade="all, delete-orphan", order_by="Slot.code"
    )


class Slot(Base, TimestampMixin):
    """A single parking bay.

    `last_vacated_at` is the field the recency (LRU-style) allocator sorts on —
    it is stamped on every release and is what makes the proposed policy work.
    """

    __tablename__ = "slots"
    __table_args__ = (
        UniqueConstraint("facility_id", "code", name="uq_slot_code"),
        Index("ix_slot_alloc", "facility_id", "status", "slot_type"),
        Index("ix_slot_recency", "facility_id", "status", "last_vacated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    zone: Mapped[str] = mapped_column(String(24), default="A", nullable=False)

    # Geometry in metres, relative to the level's own origin.
    x: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    y: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    width: Mapped[float] = mapped_column(Float, default=2.5, nullable=False)
    height: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    rotation: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    slot_type: Mapped[str] = mapped_column(String(16), default=SlotType.STANDARD, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=SlotStatus.EMPTY, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    has_ev_charger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_multiplier: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # Precomputed walking/driving distance from the primary entry gate, in metres.
    distance_from_entry: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Recency + utilisation bookkeeping
    last_vacated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    last_occupied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reserved_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    total_uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_occupied_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    facility: Mapped[Facility] = relationship(back_populates="slots")
    level: Mapped[Level] = relationship(back_populates="slots")

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def distance_to(self, point: tuple[float, float]) -> float:
        cx, cy = self.center
        return math.hypot(cx - point[0], cy - point[1])

    @property
    def is_allocatable(self) -> bool:
        return self.is_active and self.status == SlotStatus.EMPTY


class Gate(Base, TimestampMixin):
    """A physical lane with an ANPR camera."""

    __tablename__ = "gates"
    __table_args__ = (UniqueConstraint("facility_id", "name", name="uq_gate_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default=GateKind.BIDIRECTIONAL, nullable=False)
    x: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    y: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    camera_id: Mapped[str | None] = mapped_column(String(64))
    camera_stream_url: Mapped[str | None] = mapped_column(String(255))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    device_token_hash: Mapped[str | None] = mapped_column(String(255))
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    facility: Mapped[Facility] = relationship(back_populates="gates")

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)


class AuthorizedVehicle(Base, TimestampMixin):
    """Allow-list for facilities running in private / restricted mode."""

    __tablename__ = "authorized_vehicles"
    __table_args__ = (
        UniqueConstraint("facility_id", "plate_normalized", name="uq_authorized_plate"),
        Index("ix_authorized_lookup", "facility_id", "plate_normalized", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    plate_normalized: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(120))
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reserved_slot_id: Mapped[int | None] = mapped_column(ForeignKey("slots.id", ondelete="SET NULL"))
    waive_charges: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    facility: Mapped[Facility] = relationship(back_populates="authorized_vehicles")

    def is_valid_at(self, moment: datetime) -> bool:
        if not self.is_active:
            return False
        if self.valid_from and moment < self.valid_from:
            return False
        return not (self.valid_until and moment > self.valid_until)


class OccupancySnapshot(Base):
    """Time series feeding the demand forecaster and the analytics dashboard."""

    __tablename__ = "occupancy_snapshots"
    __table_args__ = (Index("ix_occupancy_facility_ts", "facility_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    occupied: Mapped[int] = mapped_column(Integer, nullable=False)
    occupancy_pct: Mapped[float] = mapped_column(Float, nullable=False)
    arrivals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    departures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_dwell_minutes: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    revenue_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    effective_rate_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PriceSnapshot(Base):
    """Audit trail of every dynamic-price recomputation — required for disputes."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_price_facility_ts", "facility_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    base_rate_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_rate_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    occupancy_pct: Mapped[float] = mapped_column(Float, nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    components: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
