"""WebSocket fan-out for the live occupancy map and operations console.

Clients subscribe to a channel — `facility:{id}` for the owner's live map,
`user:{id}` for a driver's own session updates. The manager mirrors the event
bus onto those channels so the React map animates the moment a gate fires.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.core.events import Event, Topic, bus
from app.core.logging import get_logger

log = get_logger(__name__)

# Topics pushed to a facility's operations channel.
_FACILITY_TOPICS = {
    Topic.VEHICLE_ENTERED, Topic.VEHICLE_EXITED, Topic.ENTRY_DENIED,
    Topic.SLOT_ALLOCATED, Topic.SLOT_RELEASED, Topic.SLOT_STATE_CHANGED,
    Topic.OCCUPANCY_CHANGED, Topic.PRICE_CHANGED, Topic.ANOMALY_DETECTED,
    Topic.OVERSTAY_DETECTED, Topic.AUTOMATION_ACTION,
}

# Topics pushed to an individual driver's channel.
_USER_TOPICS = {
    Topic.SLOT_ALLOCATED, Topic.VEHICLE_EXITED, Topic.WALLET_DEBITED,
    Topic.WALLET_CREDITED, Topic.PAYMENT_FAILED, Topic.OVERSTAY_DETECTED,
    Topic.NOTIFICATION, Topic.AGENT_ACTION,
}


class ConnectionManager:
    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._channels[channel].add(websocket)
        log.info("ws connected", extra={"channel": channel, "clients": len(self._channels[channel])})

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        async with self._lock:
            self._channels[channel].discard(websocket)
            if not self._channels[channel]:
                self._channels.pop(channel, None)

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._channels.get(channel, ()))
        if not targets:
            return
        text = json.dumps(message, default=str)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._channels[channel].discard(ws)

    def client_count(self, channel: str | None = None) -> int:
        if channel:
            return len(self._channels.get(channel, ()))
        return sum(len(v) for v in self._channels.values())


ws_manager = ConnectionManager()


async def _mirror_event(event: Event) -> None:
    message = event.as_dict()
    if event.facility_id is not None and event.topic in _FACILITY_TOPICS:
        await ws_manager.broadcast(f"facility:{event.facility_id}", message)
    if event.user_id is not None and event.topic in _USER_TOPICS:
        await ws_manager.broadcast(f"user:{event.user_id}", message)
    await ws_manager.broadcast("firehose", message)


def register_realtime_bridge() -> None:
    bus.subscribe("*", _mirror_event)
