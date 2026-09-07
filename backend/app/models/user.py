"""Identity, vehicles, and the wallet ledger."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import NotificationChannel, TxnType, UserRole, VehicleType

if TYPE_CHECKING:
    from app.models.facility import Facility
    from app.models.parking import ParkingSession


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.DRIVER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    locale: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    # Spoken 4-digit PIN used to authenticate a caller in the voice channel.
    voice_pin: Mapped[str | None] = mapped_column(String(8))
    preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    wallet: Mapped[Wallet] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    vehicles: Mapped[list[Vehicle]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    facilities: Mapped[list[Facility]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_owner(self) -> bool:
        return self.role in (UserRole.OWNER, UserRole.ADMIN)


class Wallet(Base, TimestampMixin):
    """Balance is authoritative and stored in minor units (paise) — never floats."""

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    balance_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    held_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    auto_reload_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_reload_threshold_minor: Mapped[int] = mapped_column(Integer, default=10_000, nullable=False)
    auto_reload_amount_minor: Mapped[int] = mapped_column(Integer, default=50_000, nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="wallet")
    transactions: Mapped[list[WalletTransaction]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan", order_by="WalletTransaction.id.desc()"
    )

    @property
    def available_minor(self) -> int:
        return self.balance_minor - self.held_minor


class WalletTransaction(Base, TimestampMixin):
    """Append-only ledger. `idempotency_key` makes gate retries safe."""

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wallet_txn_idempotency"),
        Index("ix_wallet_txn_wallet_created", "wallet_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("parking_sessions.id", ondelete="SET NULL")
    )
    txn_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="wallet", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")

    @property
    def signed_minor(self) -> int:
        return -self.amount_minor if self.txn_type in (TxnType.DEBIT, TxnType.HOLD) else self.amount_minor


class Vehicle(Base, TimestampMixin):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("plate_normalized", name="uq_vehicle_plate"),
        Index("ix_vehicle_owner", "owner_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plate: Mapped[str] = mapped_column(String(20), nullable=False)
    # Uppercased, punctuation-stripped form — the only key ANPR ever matches on.
    plate_normalized: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    make: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(40))
    color: Mapped[str | None] = mapped_column(String(24))
    vehicle_type: Mapped[str] = mapped_column(String(20), default=VehicleType.HATCHBACK, nullable=False)
    is_ev: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_accessible_permit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    owner: Mapped[User] = relationship(back_populates="vehicles")
    sessions: Mapped[list[ParkingSession]] = relationship(back_populates="vehicle")


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default=NotificationChannel.IN_APP, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="sent", nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="notifications")


class ApiKey(Base, TimestampMixin):
    """Machine credentials for gate controllers and city open-data consumers."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
