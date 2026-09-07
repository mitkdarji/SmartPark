"""Importing this package registers every SQLAlchemy mapper."""

from app.models.automation import (
    AgentConversation,
    AgentMessage,
    AutomationRule,
    AutomationRun,
)
from app.models.enums import (
    AccessMode,
    AllocationStrategy,
    AnomalyKind,
    GateKind,
    NotificationChannel,
    PaymentStatus,
    SessionStatus,
    Severity,
    SlotStatus,
    SlotType,
    TxnType,
    UserRole,
    VehicleType,
)
from app.models.facility import (
    AuthorizedVehicle,
    Facility,
    Gate,
    Level,
    OccupancySnapshot,
    PriceSnapshot,
    Slot,
)
from app.models.parking import Anomaly, ParkingSession, RecognitionEvent
from app.models.user import (
    ApiKey,
    Notification,
    User,
    Vehicle,
    Wallet,
    WalletTransaction,
)

__all__ = [
    "AccessMode", "AgentConversation", "AgentMessage", "AllocationStrategy",
    "Anomaly", "AnomalyKind", "ApiKey", "AuthorizedVehicle", "AutomationRule",
    "AutomationRun", "Facility", "Gate", "GateKind", "Level", "Notification",
    "NotificationChannel", "OccupancySnapshot", "ParkingSession", "PaymentStatus",
    "PriceSnapshot", "RecognitionEvent", "SessionStatus", "Severity", "Slot",
    "SlotStatus", "SlotType", "TxnType", "User", "UserRole", "Vehicle",
    "VehicleType", "Wallet", "WalletTransaction",
]
