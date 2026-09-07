from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import AccessMode, AllocationStrategy, GateKind, SlotStatus, SlotType
from app.schemas.common import ORMModel


class FacilityCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    address: str = ""
    city: str = ""
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    access_mode: AccessMode = AccessMode.PUBLIC
    allocation_strategy: AllocationStrategy = AllocationStrategy.HYBRID
    base_rate_minor_per_hour: int = Field(default=4000, ge=0, le=1_000_000)
    free_minutes: int = Field(default=15, ge=0, le=1440)
    billing_increment_minutes: int = Field(default=15, ge=1, le=60)
    daily_cap_minor: int = Field(default=40_000, ge=0)
    tax_percent: float = Field(default=18.0, ge=0, le=50)
    dynamic_pricing_enabled: bool = True
    overstay_after_hours: int = Field(default=12, ge=1, le=168)
    contact_phone: str | None = None
    amenities: list[str] = Field(default_factory=list)
    pricing_config: dict = Field(default_factory=dict)
    publish_to_city_feed: bool = True


class FacilityUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    address: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    access_mode: AccessMode | None = None
    allocation_strategy: AllocationStrategy | None = None
    base_rate_minor_per_hour: int | None = Field(default=None, ge=0, le=1_000_000)
    free_minutes: int | None = Field(default=None, ge=0, le=1440)
    billing_increment_minutes: int | None = Field(default=None, ge=1, le=60)
    daily_cap_minor: int | None = Field(default=None, ge=0)
    tax_percent: float | None = Field(default=None, ge=0, le=50)
    dynamic_pricing_enabled: bool | None = None
    overstay_after_hours: int | None = Field(default=None, ge=1, le=168)
    contact_phone: str | None = None
    amenities: list[str] | None = None
    pricing_config: dict | None = None
    is_active: bool | None = None
    publish_to_city_feed: bool | None = None


class FacilityOut(ORMModel):
    id: int
    owner_id: int
    name: str
    slug: str
    description: str
    address: str
    city: str
    latitude: float | None
    longitude: float | None
    access_mode: str
    allocation_strategy: str
    currency: str
    base_rate_minor_per_hour: int
    free_minutes: int
    billing_increment_minutes: int
    daily_cap_minor: int
    tax_percent: float
    dynamic_pricing_enabled: bool
    overstay_after_hours: int
    amenities: list
    pricing_config: dict
    is_active: bool
    publish_to_city_feed: bool
    created_at: datetime


class FacilitySummary(FacilityOut):
    capacity: int = 0
    occupied: int = 0
    free: int = 0
    occupancy_pct: float = 0.0
    effective_rate_minor: int = 0
    active_sessions: int = 0


class LevelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    order_index: int = 0
    canvas_width: float = Field(default=60.0, gt=0, le=2000, description="Metres.")
    canvas_height: float = Field(default=40.0, gt=0, le=2000, description="Metres.")
    aisles: list[list[list[float]]] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class LevelOut(ORMModel):
    id: int
    facility_id: int
    name: str
    order_index: int
    canvas_width: float
    canvas_height: float
    floorplan_path: str | None
    aisles: list
    meta: dict


class SlotIn(BaseModel):
    """One bay from the canvas layout builder."""

    id: int | None = None
    code: str = Field(min_length=1, max_length=16)
    zone: str = Field(default="A", max_length=24)
    x: float = 0.0
    y: float = 0.0
    width: float = Field(default=2.5, gt=0, le=30, description="Metres.")
    height: float = Field(default=5.0, gt=0, le=30, description="Metres.")
    rotation: float = 0.0
    slot_type: SlotType = SlotType.STANDARD
    has_ev_charger: bool = False
    price_multiplier: float = Field(default=1.0, ge=0.1, le=5.0)
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class SlotOut(ORMModel):
    id: int
    facility_id: int
    level_id: int
    code: str
    zone: str
    x: float
    y: float
    width: float
    height: float
    rotation: float
    slot_type: str
    status: str
    is_active: bool
    has_ev_charger: bool
    price_multiplier: float
    distance_from_entry: float
    last_vacated_at: datetime | None
    total_uses: int


class SlotBulkUpsert(BaseModel):
    """Whole-level save from the canvas editor.

    Slots absent from `slots` are deleted, which is what "save my layout" means
    to the person drawing it. Occupied bays are never removed — the API rejects
    that rather than orphaning a parked vehicle.
    """

    level_id: int
    slots: list[SlotIn]


class SlotStatusUpdate(BaseModel):
    status: SlotStatus


class GateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: GateKind = GateKind.BIDIRECTIONAL
    level_id: int | None = None
    x: float = 0.0
    y: float = 0.0
    camera_id: str | None = None
    camera_stream_url: str | None = None
    is_primary: bool = False


class GateUpdate(BaseModel):
    """Partial update. Moving a gate re-ranks every bay, so the router
    recomputes routed distances whenever a position or primary flag changes."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    kind: GateKind | None = None
    level_id: int | None = None
    x: float | None = None
    y: float | None = None
    camera_id: str | None = None
    is_primary: bool | None = None
    is_active: bool | None = None


class GateOut(ORMModel):
    id: int
    facility_id: int
    level_id: int | None
    name: str
    kind: str
    x: float
    y: float
    camera_id: str | None
    is_primary: bool
    is_active: bool
    last_seen_at: datetime | None


class AuthorizedVehicleCreate(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    label: str = ""
    owner_name: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    reserved_slot_id: int | None = None
    waive_charges: bool = False


class AuthorizedVehicleOut(ORMModel):
    id: int
    facility_id: int
    plate_normalized: str
    label: str
    owner_name: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    reserved_slot_id: int | None
    waive_charges: bool
