"""AI surfaces: the agent, the voice channel, and generative reporting."""

from __future__ import annotations

import base64

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OwnedFacility
from app.core.errors import NotFound
from app.models.automation import AgentConversation, AgentMessage
from app.schemas.ai import (
    AgentChatRequest,
    AgentChatResponse,
    AgentMessageOut,
    BriefingResponse,
    ConversationOut,
    VoiceConverseRequest,
    VoiceConverseResponse,
)
from app.services.agents.runtime import agent_runtime
from app.services.agents.tools import TOOLS_BY_NAME, ToolContext, tools_for_role
from app.services.genai.client import claude
from app.services.genai.copilot import copilot
from app.services.voice import pipeline as voice

router = APIRouter(tags=["ai"])


# ── Agent ─────────────────────────────────────────────────────

@router.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(
    payload: AgentChatRequest, user: CurrentUser, db: DbSession
) -> AgentChatResponse:
    turn = await agent_runtime.run(
        db, user, payload.message,
        conversation_id=payload.conversation_id,
        facility_id=payload.facility_id,
        channel=payload.channel,
        confirm_tool=payload.confirm_tool,
        confirm_args=payload.confirm_arguments,
    )
    data = turn.as_dict()
    data["speakable"] = voice.speakable(turn.reply)
    return AgentChatResponse(**data)


@router.get("/agent/tools", response_model=dict)
async def list_agent_tools(user: CurrentUser) -> dict:
    """What this account's agent is allowed to do — the transparency surface."""
    tools = tools_for_role(user.role)
    return {
        "role": user.role,
        "generative_ai_enabled": claude.enabled,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": t.mutating,
                "parameters": list((t.schema.get("properties") or {}).keys()),
            }
            for t in tools
        ],
    }


@router.get("/agent/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user: CurrentUser, db: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> list[ConversationOut]:
    rows = (
        await db.execute(
            select(AgentConversation)
            .where(AgentConversation.user_id == user.id)
            .order_by(AgentConversation.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [ConversationOut.model_validate(row) for row in rows]


@router.get("/agent/conversations/{conversation_id}", response_model=list[AgentMessageOut])
async def conversation_messages(
    conversation_id: int, user: CurrentUser, db: DbSession
) -> list[AgentMessageOut]:
    conversation = await db.get(AgentConversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise NotFound(f"Conversation {conversation_id} was not found.")
    rows = (
        await db.execute(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation_id)
            .order_by(AgentMessage.id)
        )
    ).scalars().all()
    return [AgentMessageOut.model_validate(row) for row in rows]


# ── Voice ─────────────────────────────────────────────────────

@router.get("/voice/status", response_model=dict)
async def voice_status() -> dict:
    return voice.status()


@router.post("/voice/transcribe", response_model=dict)
async def transcribe(audio: UploadFile = File(...)) -> dict:
    payload = await audio.read()
    result = voice.transcribe(payload)
    return {
        "text": result.text, "confidence": result.confidence,
        "language": result.language, "duration_s": result.duration_s,
        "backend": result.backend, "error": result.error,
    }


@router.post("/voice/converse", response_model=VoiceConverseResponse)
async def converse(
    payload: VoiceConverseRequest, user: CurrentUser, db: DbSession
) -> VoiceConverseResponse:
    """Text in, spoken-ready answer out.

    The intent router short-circuits the handful of utterances that dominate
    real traffic; everything else goes to the full agent. `fast_path` in the
    response says which happened, so the latency difference is explainable
    rather than mysterious.
    """
    import time

    started = time.perf_counter()
    intent = voice.classify_intent(payload.text)

    if intent and intent.name in TOOLS_BY_NAME:
        tool = TOOLS_BY_NAME[intent.name]
        if user.role in tool.roles and not tool.mutating:
            ctx = ToolContext(db=db, user=user, facility_id=payload.facility_id)
            result = await tool.handler(ctx, {})
            reply = _phrase(intent.name, result)
            if reply:
                spoken = voice.speakable(reply)
                audio_b64 = audio_error = None
                if payload.want_audio:
                    audio, audio_error = voice.synthesize(spoken)
                    audio_b64 = base64.b64encode(audio).decode() if audio else None
                return VoiceConverseResponse(
                    transcript=payload.text, reply=reply, speakable=spoken,
                    intent=intent.name, intent_confidence=intent.confidence,
                    fast_path=True, conversation_id=payload.conversation_id,
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    audio_base64=audio_b64, audio_error=audio_error,
                )

    turn = await agent_runtime.run(
        db, user, payload.text,
        conversation_id=payload.conversation_id,
        facility_id=payload.facility_id,
        channel="voice",
    )
    spoken = voice.speakable(turn.reply)
    audio_b64 = audio_error = None
    if payload.want_audio:
        audio, audio_error = voice.synthesize(spoken)
        audio_b64 = base64.b64encode(audio).decode() if audio else None

    return VoiceConverseResponse(
        transcript=payload.text, reply=turn.reply, speakable=spoken,
        intent=intent.name if intent else None,
        intent_confidence=intent.confidence if intent else 0.0,
        fast_path=False, conversation_id=turn.conversation_id,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        audio_base64=audio_b64, audio_error=audio_error, degraded=turn.degraded,
    )


def _phrase(tool_name: str, result: dict) -> str | None:
    """Deterministic phrasing for fast-path answers — no model call needed."""
    if result.get("error"):
        return str(result["error"])

    if tool_name == "find_my_vehicle":
        if not result.get("parked"):
            return "You have no vehicle parked at the moment."
        return (
            f"Your vehicle {result['plate']} is in slot {result['slot_code']}, "
            f"zone {result['zone']} at {result['facility']}. It has been there for "
            f"{result['parked_for_minutes']:.0f} minutes."
        )

    if tool_name == "get_current_charges":
        if not result.get("active"):
            return "You have no active parking session, so there is nothing to pay."
        return (
            f"You have been parked for {result['parked_for_minutes']:.0f} minutes. "
            f"The charge so far is {result['amount_so_far']}, at "
            f"{result['current_rate_per_hour']} per hour."
        )

    if tool_name == "get_wallet_balance":
        return f"Your wallet balance is {result['balance']}."

    if tool_name == "get_directions_to_my_slot":
        steps = result.get("instructions") or []
        return " ".join(steps) if steps else None

    if tool_name == "list_recent_charges":
        items = result.get("transactions") or []
        if not items:
            return "You have no recent transactions."
        first = items[0]
        return (
            f"Your most recent transaction was {first['amount']} on {first['date']}: "
            f"{first['description']}. You have {len(items)} recent entries in total."
        )

    if tool_name == "find_parking_nearby":
        facilities = result.get("facilities") or []
        if not facilities:
            return "I could not find any SmartPark facilities matching that."
        best = facilities[0]
        return (
            f"{best['name']} has {best['free_slots']} free bays out of {best['capacity']}, "
            f"at {best['rate_per_hour']} per hour."
        )
    return None


# ── Generative reporting ──────────────────────────────────────

@router.get("/facilities/{facility_id}/ai/briefing", response_model=BriefingResponse)
async def briefing(
    facility: OwnedFacility,
    db: DbSession,
    days: int = Query(default=7, ge=1, le=90),
) -> BriefingResponse:
    result = await copilot.daily_briefing(db, facility, days=days)
    return BriefingResponse(**result.as_dict())


@router.get("/facilities/{facility_id}/ai/triage", response_model=dict)
async def triage(facility: OwnedFacility, db: DbSession) -> dict:
    return await copilot.triage_anomalies(db, facility)


@router.get("/facilities/{facility_id}/ai/suggest-automations", response_model=dict)
async def suggest_automations(facility: OwnedFacility, db: DbSession) -> dict:
    return await copilot.suggest_automations(db, facility)


@router.get("/ai/health", response_model=dict)
async def ai_health() -> dict:
    return await claude.health()
