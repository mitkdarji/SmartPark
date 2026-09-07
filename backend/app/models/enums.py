"""Domain enumerations. Stored as VARCHAR for portability across SQLite/Postgres."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    OWNER = "owner"          # parking facility operator
    DRIVER = "driver"        # vehicle owner
    ADMIN = "admin"          # platform / city administrator


class AccessMode(StrEnum):
    PUBLIC = "public"        # any vehicle is logged and billed
    PRIVATE = "private"      # only pre-authorised plates may enter


class SlotType(StrEnum):
    STANDARD = "standard"
    COMPACT = "compact"
    LARGE = "large"
    EV = "ev"
    ACCESSIBLE = "accessible"
    TWO_WHEELER = "two_wheeler"


class SlotStatus(StrEnum):
    EMPTY = "empty"
    OCCUPIED = "occupied"
    RESERVED = "reserved"
    BLOCKED = "blocked"
    MAINTENANCE = "maintenance"


class VehicleType(StrEnum):
    HATCHBACK = "hatchback"
    SEDAN = "sedan"
    SUV = "suv"
    TWO_WHEELER = "two_wheeler"
    EV = "ev"
    TRUCK = "truck"


class GateKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    BIDIRECTIONAL = "bidirectional"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DENIED = "denied"
    ABANDONED = "abandoned"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    WAIVED = "waived"


class TxnType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    HOLD = "hold"
    RELEASE = "release"
    REFUND = "refund"


class AllocationStrategy(StrEnum):
    """Slot-assignment policies compared in the evaluation harness."""

    NEAREST = "nearest"                # baseline: closest empty slot to the gate
    RECENCY = "recency"                # proposed: most-recently vacated (LRU-style)
    RANDOM = "random"                  # baseline: uniform random empty slot
    FIRST_FIT = "first_fit"            # baseline: lowest slot code
    BALANCED = "balanced"              # spread load across zones
    HYBRID = "hybrid"                  # recency, tie-broken by distance + ML demand


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"
    EMAIL = "email"
    PUSH = "push"
    VOICE = "voice"


class AnomalyKind(StrEnum):
    PLATE_MISMATCH = "plate_mismatch"
    TAILGATING = "tailgating"
    IMPOSSIBLE_DWELL = "impossible_dwell"
    LOW_CONFIDENCE_READ = "low_confidence_read"
    DUPLICATE_PLATE = "duplicate_plate"
    OVERSTAY = "overstay"
    PAYMENT_FAILURE = "payment_failure"
    UNAUTHORISED_ENTRY = "unauthorised_entry"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
