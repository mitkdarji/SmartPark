"""WebSocket channels for the live occupancy map and driver session updates."""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.events import bus
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.enums import UserRole
from app.models.facility import Facility
from app.models.user import User
from app.services.realtime.manager import ws_manager

log = get_logger(__name__)

router = APIRouter(tags=["realtime"])


async def _authenticate(token: str | None) -> User | None:
    """WebSockets cannot carry an Authorization header, so the token is a query param."""
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None
    async with SessionLocal() as db:
        user = await db.get(User, int(payload["sub"]))
        return user if user and user.is_active else None


@router.websocket("/ws/facility/{facility_id}")
async def facility_stream(
    websocket: WebSocket,
    facility_id: int,
    token: str | None = Query(default=None),
) -> None:
    """Live operations feed for one facility. Operators only."""
    user = await _authenticate(token)
    if user is None:
        await websocket.close(code=4401, reason="authentication required")
        return

    async with SessionLocal() as db:
        facility = await db.get(Facility, facility_id)
        if facility is None:
            await websocket.close(code=4404, reason="facility not found")
            return
        if facility.owner_id != user.id and user.role != UserRole.ADMIN:
            await websocket.close(code=4403, reason="not your facility")
            return

    channel = f"facility:{facility_id}"
    await ws_manager.connect(websocket, channel)
    try:
        # Replay recent history so a reconnecting client is immediately current.
        for event in reversed(bus.recent(limit=20, facility_id=facility_id)):
            await websocket.send_json(event.as_dict())
        while True:
            # The client sends nothing meaningful; this keeps the socket open and
            # detects disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.info("facility ws closed", extra={"error": str(exc)})
    finally:
        await ws_manager.disconnect(websocket, channel)


@router.websocket("/ws/me")
async def user_stream(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """A driver's own feed: allocation, exit, wallet and notification events."""
    user = await _authenticate(token)
    if user is None:
        await websocket.close(code=4401, reason="authentication required")
        return

    channel = f"user:{user.id}"
    await ws_manager.connect(websocket, channel)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.info("user ws closed", extra={"error": str(exc)})
    finally:
        await ws_manager.disconnect(websocket, channel)
