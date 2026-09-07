"""Registration, sign-in, and profile."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.errors import AuthenticationFailed, Conflict
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User, Vehicle, Wallet
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut, WalletOut
from app.services.anpr.plate_utils import normalize_plate, pretty_plate

router = APIRouter(prefix="/auth", tags=["auth"])

# Every new driver starts with a demo balance so the exit-billing flow is
# immediately exercisable without a payment integration.
WELCOME_CREDIT_MINOR = 100_000


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DbSession) -> TokenResponse:
    existing = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("An account with this email already exists.")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        role=payload.role,
    )
    db.add(user)
    await db.flush()

    wallet = Wallet(user_id=user.id, balance_minor=WELCOME_CREDIT_MINOR)
    db.add(wallet)

    if payload.plate:
        plate = normalize_plate(payload.plate)
        clash = (
            await db.execute(select(Vehicle).where(Vehicle.plate_normalized == plate))
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"Vehicle {pretty_plate(plate)} is already registered.")
        db.add(
            Vehicle(
                owner_id=user.id, plate=pretty_plate(plate), plate_normalized=plate
            )
        )

    await db.commit()
    await db.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    # Same message for both failure modes — do not confirm which emails exist.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationFailed("Incorrect email or password.")
    if not user.is_active:
        raise AuthenticationFailed("This account has been deactivated.")

    user.last_login_at = datetime.now(UTC)
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/me/wallet", response_model=WalletOut)
async def my_wallet(user: CurrentUser, db: DbSession) -> WalletOut:
    from app.services.billing.wallet import wallet_service

    wallet = await wallet_service.get_or_create(db, user.id)
    await db.commit()
    return WalletOut.model_validate(wallet)
