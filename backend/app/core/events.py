"""In-process asynchronous event bus.

Every meaningful domain action (vehicle entered, slot freed, wallet debited,
anomaly detected) publishes an `Event`. Subscribers — the WebSocket broadcaster,
the automation rules engine, the analytics recorder, the notification fan-out —
react without the request path knowing they exist.

Swapping this for Redis Streams or Kafka in a multi-node deployment means
reimplementing `publish`/`subscribe` only; no call site changes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class Topic:
    VEHICLE_ENTERED = "vehicle.entered"
    VEHICLE_EXITED = "vehicle.exited"
    ENTRY_DENIED = "vehicle.entry_denied"
    SLOT_ALLOCATED = "slot.allocated"
    SLOT_RELEASED = "slot.released"
    SLOT_STATE_CHANGED = "slot.state_changed"
    OCCUPANCY_CHANGED = "facility.occupancy_changed"
    PRICE_CHANGED = "facility.price_changed"
    WALLET_DEBITED = "wallet.debited"
    WALLET_CREDITED = "wallet.credited"
    PAYMENT_FAILED = "wallet.payment_failed"
    ANOMALY_DETECTED = "security.anomaly_detected"
    OVERSTAY_DETECTED = "session.overstay"
    AUTOMATION_ACTION = "automation.action"
    NOTIFICATION = "notification.dispatched"
    AGENT_ACTION = "agent.action"


@dataclass(slots=True)
class Event:
    topic: str
    payload: dict[str, Any]
    facility_id: int | None = None
    user_id: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "ts": self.ts.isoformat(),
            "facility_id": self.facility_id,
            "user_id": self.user_id,
            "payload": self.payload,
        }


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []
        self._history: list[Event] = []
        self._history_limit = 500

    def subscribe(self, topic: str, handler: Handler) -> None:
        if topic == "*":
            self._wildcard.append(handler)
        else:
            self._subs[topic].append(handler)

    async def publish(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

        handlers = [*self._subs.get(event.topic, []), *self._wildcard]
        if not handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in handlers), return_exceptions=True
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                # A misbehaving subscriber must never fail the originating request.
                log.error(
                    "event handler failed",
                    extra={
                        "topic": event.topic,
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                        "error": str(result),
                    },
                )

    async def emit(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        facility_id: int | None = None,
        user_id: int | None = None,
    ) -> Event:
        event = Event(topic=topic, payload=payload, facility_id=facility_id, user_id=user_id)
        await self.publish(event)
        return event

    def recent(self, limit: int = 50, facility_id: int | None = None) -> list[Event]:
        items = self._history
        if facility_id is not None:
            items = [e for e in items if e.facility_id == facility_id]
        return list(reversed(items[-limit:]))


bus = EventBus()
