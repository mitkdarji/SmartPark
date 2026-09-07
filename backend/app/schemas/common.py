"""Shared response envelopes and pagination."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    detail: str


class ErrorResponse(BaseModel):
    error: str
    code: str
    details: dict = Field(default_factory=dict)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    database: bool
    genai: dict
    anpr: dict
    voice: dict
    scheduler: bool
    websocket_clients: int
    timestamp: datetime
