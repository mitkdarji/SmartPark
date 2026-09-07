from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    role: UserRole = UserRole.DRIVER
    # A driver may register their vehicle in the same step.
    plate: str | None = Field(default=None, max_length=20)

    @field_validator("role")
    @classmethod
    def _no_self_service_admin(cls, v: UserRole) -> UserRole:
        if v == UserRole.ADMIN:
            raise ValueError("admin accounts cannot be self-registered")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str
    phone: str | None = None
    role: str
    locale: str
    is_active: bool
    created_at: datetime


class WalletOut(ORMModel):
    id: int
    balance_minor: int
    held_minor: int
    currency: str
    auto_reload_enabled: bool
    auto_reload_threshold_minor: int
    auto_reload_amount_minor: int


TokenResponse.model_rebuild()
