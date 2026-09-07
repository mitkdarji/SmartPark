from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import VehicleType
from app.schemas.common import ORMModel


class VehicleCreate(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    make: str | None = None
    model: str | None = None
    color: str | None = None
    vehicle_type: VehicleType = VehicleType.HATCHBACK
    is_ev: bool = False
    is_accessible_permit: bool = False


class VehicleOut(ORMModel):
    id: int
    owner_id: int
    plate: str
    plate_normalized: str
    make: str | None
    model: str | None
    color: str | None
    vehicle_type: str
    is_ev: bool
    is_accessible_permit: bool
    created_at: datetime


class GateScanRequest(BaseModel):
    """Plate-only scan. The image path uses multipart upload instead."""

    facility_id: int
    gate_id: int | None = None
    plate: str = Field(min_length=4, max_length=20)
    vehicle_type: VehicleType | None = None
    needs_charger: bool = False


class SessionOut(ORMModel):
    id: int
    facility_id: int
    vehicle_id: int | None
    user_id: int | None
    slot_id: int | None
    plate: str
    plate_normalized: str
    entry_at: datetime
    exit_at: datetime | None
    status: str
    allocation_strategy: str | None
    allocation_reason: str
    allocation_latency_ms: float
    walk_distance_m: float
    entry_confidence: float
    exit_confidence: float
    billable_minutes: int
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    payment_status: str
    invoice_no: str | None
    rate_breakdown: dict
    navigation_path: dict | list
    notes: str
    denial_reason: str | None


class SessionDetail(SessionOut):
    facility_name: str | None = None
    slot_code: str | None = None
    zone: str | None = None
    duration_minutes: float = 0.0
    live_amount_minor: int | None = None


class RecognitionEventOut(ORMModel):
    id: int
    facility_id: int | None
    gate_id: int | None
    session_id: int | None
    direction: str
    plate_raw: str
    plate_normalized: str
    confidence: float
    backend: str
    candidates: list
    plate_bbox: list
    image_path: str | None
    processing_ms: float
    decision: str
    needs_review: bool
    corrected_plate: str | None
    created_at: datetime


class AnomalyOut(ORMModel):
    id: int
    facility_id: int | None
    session_id: int | None
    kind: str
    severity: str
    score: float
    summary: str
    detail: dict
    resolved: bool
    resolved_at: datetime | None
    created_at: datetime


class TopUpRequest(BaseModel):
    amount_minor: int = Field(gt=0, le=5_000_000, description="Amount in paise.")
    reference: str | None = None


class WalletTransactionOut(ORMModel):
    id: int
    txn_type: str
    amount_minor: int
    balance_after_minor: int
    currency: str
    description: str
    reference: str | None
    session_id: int | None
    created_at: datetime


class AutoReloadUpdate(BaseModel):
    enabled: bool
    threshold_minor: int = Field(default=10_000, ge=0)
    amount_minor: int = Field(default=50_000, gt=0, le=5_000_000)
