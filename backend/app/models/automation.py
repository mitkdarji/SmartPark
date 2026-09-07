"""Automation rules, their execution log, and AI agent conversation history."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime


class AutomationRule(Base, TimestampMixin):
    """Declarative when/if/then rule evaluated by the automation engine.

    `trigger` is an event-bus topic or the literal "schedule:tick";
    `conditions` is a small JSON expression tree; `actions` is a list of
    handler names with kwargs. Rules are data, so an owner can add one from the
    dashboard — or the AI copilot can draft one — without a deploy.
    """

    __tablename__ = "automation_rules"
    __table_args__ = (Index("ix_rule_facility_enabled", "facility_id", "enabled"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    actions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_by_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fire_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    runs: Mapped[list[AutomationRun]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )


class AutomationRun(Base, TimestampMixin):
    __tablename__ = "automation_runs"
    __table_args__ = (Index("ix_run_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False
    )
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    trigger_event: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    actions_taken: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    rule: Mapped[AutomationRule] = relationship(back_populates="runs")


class AgentConversation(Base, TimestampMixin):
    """A voice or chat session with the SmartPark agent."""

    __tablename__ = "agent_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    persona: Mapped[str] = mapped_column(String(24), default="driver", nullable=False)
    title: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    locale: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="AgentMessage.id"
    )


class AgentMessage(Base, TimestampMixin):
    __tablename__ = "agent_messages"
    __table_args__ = (Index("ix_agent_msg_conv", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Tool calls the agent made on this turn, with arguments and results.
    tool_calls: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    audio_path: Mapped[str | None] = mapped_column(String(255))
    transcript_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    model: Mapped[str] = mapped_column(String(48), default="", nullable=False)

    conversation: Mapped[AgentConversation] = relationship(back_populates="messages")
