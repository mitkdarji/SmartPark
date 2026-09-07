"""FastAPI dependencies: authentication, authorisation, and object lookup."""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationFailed, NotFound, PermissionDenied
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.facility import Facility, Gate
from app.models.user import User

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationFailed("Missing bearer token.")

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationFailed("Session expired — please sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationFailed("Invalid authentication token.") from exc

    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationFailed("Account not found or deactivated.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """For endpoints that personalise when signed in but work when not."""
    if not authorization:
        return None
    try:
        return await get_current_user(db, authorization)
    except AuthenticationFailed:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def require_owner(user: CurrentUser) -> User:
    if user.role not in (UserRole.OWNER, UserRole.ADMIN):
        raise PermissionDenied("This endpoint is for facility operators.")
    return user


OwnerUser = Annotated[User, Depends(require_owner)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN:
        raise PermissionDenied("Administrator access is required.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def get_owned_facility(
    facility_id: Annotated[int, Path()],
    db: DbSession,
    user: CurrentUser,
) -> Facility:
    """Load a facility, enforcing that the caller operates it."""
    facility = await db.get(Facility, facility_id)
    if facility is None:
        raise NotFound(f"Facility {facility_id} was not found.")
    if facility.owner_id != user.id and user.role != UserRole.ADMIN:
        raise PermissionDenied("You do not operate this facility.")
    return facility


OwnedFacility = Annotated[Facility, Depends(get_owned_facility)]


async def get_public_facility(
    facility_id: Annotated[int, Path()],
    db: DbSession,
) -> Facility:
    facility = await db.get(Facility, facility_id)
    if facility is None or not facility.is_active:
        raise NotFound(f"Facility {facility_id} was not found.")
    return facility


PublicFacility = Annotated[Facility, Depends(get_public_facility)]


async def resolve_gate(db: AsyncSession, facility: Facility, gate_id: int | None) -> Gate | None:
    """Named gate, or the facility's primary gate as the default."""
    from sqlalchemy import select

    if gate_id is not None:
        gate = await db.get(Gate, gate_id)
        if gate is None or gate.facility_id != facility.id:
            raise NotFound(f"Gate {gate_id} does not belong to this facility.")
        return gate

    return (
        await db.execute(
            select(Gate)
            .where(Gate.facility_id == facility.id, Gate.is_active.is_(True))
            .order_by(Gate.is_primary.desc(), Gate.id)
        )
    ).scalars().first()
