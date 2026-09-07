from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    facility_id: int | None = None
    channel: str = Field(default="chat", pattern="^(chat|voice|sms)$")
    # Set when the user approves a previously proposed mutating action.
    confirm_tool: str | None = None
    confirm_arguments: dict | None = None


class AgentChatResponse(BaseModel):
    reply: str
    conversation_id: int | None
    tool_calls: list[dict] = Field(default_factory=list)
    pending_confirmation: dict | None = None
    iterations: int = 0
    latency_ms: float = 0.0
    usage: dict = Field(default_factory=dict)
    degraded: bool = False
    error: str | None = None
    speakable: str | None = None


class VoiceConverseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    facility_id: int | None = None
    want_audio: bool = False


class VoiceConverseResponse(BaseModel):
    transcript: str
    reply: str
    speakable: str
    intent: str | None = None
    intent_confidence: float = 0.0
    fast_path: bool = False
    conversation_id: int | None = None
    latency_ms: float = 0.0
    audio_base64: str | None = None
    audio_error: str | None = None
    degraded: bool = False


class ConversationOut(ORMModel):
    id: int
    user_id: int | None
    facility_id: int | None
    channel: str
    persona: str
    title: str
    is_open: bool
    total_tokens: int
    created_at: datetime


class AgentMessageOut(ORMModel):
    id: int
    role: str
    content: str
    tool_calls: list
    model: str
    latency_ms: float
    created_at: datetime


class BriefingResponse(BaseModel):
    title: str
    body: str
    highlights: list[str]
    generated_by: str
    facts: dict
    generated_at: datetime


class AutomationRuleIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    trigger: str
    conditions: dict = Field(default_factory=dict)
    actions: list[dict] = Field(default_factory=list)
    enabled: bool = True
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    priority: int = Field(default=100, ge=1, le=1000)
    facility_id: int | None = None


class AutomationRuleOut(ORMModel):
    id: int
    facility_id: int | None
    name: str
    description: str
    trigger: str
    conditions: dict
    actions: list
    enabled: bool
    cooldown_seconds: int
    priority: int
    created_by_ai: bool
    last_fired_at: datetime | None
    fire_count: int
    created_at: datetime


class AutomationRunOut(ORMModel):
    id: int
    rule_id: int
    facility_id: int | None
    status: str
    trigger_event: str
    actions_taken: list
    detail: str
    duration_ms: float
    created_at: datetime


class BenchmarkRequest(BaseModel):
    strategies: list[str] | None = None
    trials: int = Field(default=5, ge=1, le=20)
    vehicles: int = Field(default=800, ge=50, le=5000)
    zones: int = Field(default=5, ge=1, le=12)
    slots_per_zone: int = Field(default=24, ge=4, le=200)
    hours: float = Field(default=14.0, ge=1, le=24)
    ghost_rate: float = Field(
        default=0.0, ge=0.0, le=0.5,
        description="Share of arrivals that park unlogged, making the occupancy map stale.",
    )
