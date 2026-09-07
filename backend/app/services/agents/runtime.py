"""The agent loop.

Standard tool-use cycle: send the conversation with the role-scoped tool set,
execute whatever the model asks for, feed every result back in a single user
message, repeat until the model stops asking for tools.

Three things make it safe to point at a production database:

  Bounded.      A hard iteration cap. A model that loops on a failing tool costs
                a bounded number of calls, not an unbounded bill.
  Gated.        Mutating tools return a confirmation request instead of running.
                The user approves in the UI, the approval comes back as a token,
                and only then does the handler execute.
  Transcribed.  Every turn, tool call and result is persisted, so an operator can
                audit exactly what the agent did and why.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Topic, bus
from app.core.logging import get_logger
from app.models.automation import AgentConversation, AgentMessage
from app.models.user import User
from app.services.agents.tools import TOOLS_BY_NAME, ToolContext, tools_for_role
from app.services.genai.client import claude

log = get_logger(__name__)

MAX_ITERATIONS = 6

DRIVER_SYSTEM = """You are the SmartPark assistant, helping a driver with their parking.

You can find their vehicle, quote charges, check their wallet, give directions to
their slot, and search for parking.

How to behave:
- Answer from tool results. Never invent a slot number, an amount, or a balance.
- Be brief. This is often spoken aloud in a car — two or three sentences, no lists
  unless the user asked for one, no markdown.
- Say amounts naturally: "eighty-five rupees", not "INR 85.00".
- Slot codes are read out character by character: "B one four" for B014.
- If a tool reports an error, say plainly what went wrong and what they can do.
- Never ask the user for a password, card number, or OTP. SmartPark will never
  request those through this assistant.
- If asked something outside parking, say that is not something you can help with.

Anything that spends money needs the user's explicit go-ahead first. Say what it
will do and what it will cost, then wait."""

OWNER_SYSTEM = """You are the SmartPark operations copilot for a parking facility operator.

You can read live occupancy, revenue, demand forecasts and open anomalies, and
you can change pricing when the operator explicitly approves it.

How to behave:
- Ground every claim in a tool result. Quote the actual numbers.
- Lead with the answer, then the supporting figure. Keep it under six sentences
  unless a report was requested.
- When something looks wrong, say so directly and suggest the specific action.
- Distinguish measurement from prediction. A forecast is a forecast — say so, and
  mention its confidence when the tool reports one.
- If a forecast came from the seasonal baseline rather than the trained model,
  tell the operator that, because it is materially less reliable.

Pricing changes affect every future customer. State the current value, the
proposed value, and the effect, then wait for explicit approval."""


@dataclass(slots=True)
class AgentTurn:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)
    pending_confirmation: dict | None = None
    conversation_id: int | None = None
    iterations: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "reply": self.reply,
            "tool_calls": self.tool_calls,
            "pending_confirmation": self.pending_confirmation,
            "conversation_id": self.conversation_id,
            "iterations": self.iterations,
            "latency_ms": round(self.latency_ms, 1),
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            "degraded": self.degraded,
            "error": self.error,
        }


class AgentRuntime:
    async def run(
        self,
        db: AsyncSession,
        user: User,
        message: str,
        *,
        conversation_id: int | None = None,
        facility_id: int | None = None,
        channel: str = "chat",
        confirm_tool: str | None = None,
        confirm_args: dict | None = None,
    ) -> AgentTurn:
        started = time.perf_counter()

        conversation = await self._conversation(
            db, user, conversation_id, facility_id=facility_id, channel=channel
        )
        system = OWNER_SYSTEM if user.is_owner else DRIVER_SYSTEM
        tools = tools_for_role(user.role)

        if not claude.enabled:
            reply = await self._fallback(db, user, message, facility_id=facility_id)
            await self._persist(db, conversation, "user", message)
            await self._persist(db, conversation, "assistant", reply, model="rule_based_fallback")
            await db.commit()
            return AgentTurn(
                reply=reply, conversation_id=conversation.id, degraded=True,
                latency_ms=(time.perf_counter() - started) * 1000,
                error="generative AI is not configured — answered from a rule-based fallback",
            )

        history = await self._history(db, conversation)
        history.append({"role": "user", "content": message})
        await self._persist(db, conversation, "user", message)

        ctx = ToolContext(db=db, user=user, facility_id=facility_id)
        # An approval from the previous turn unlocks exactly that one tool.
        if confirm_tool:
            ctx.confirmed_tokens.add(confirm_tool)

        executed: list[dict] = []
        pending: dict | None = None
        total_in = total_out = 0
        iterations = 0

        # If the user just approved a gated action, run it before asking the model
        # anything — the approval refers to the concrete call we already proposed.
        if confirm_tool and confirm_tool in TOOLS_BY_NAME:
            result = await self._execute(ctx, confirm_tool, confirm_args or {})
            executed.append(result)
            history.append(
                {
                    "role": "user",
                    "content": (
                        f"[system] The user approved `{confirm_tool}`. It has been executed. "
                        f"Result: {json.dumps(result['result'], default=str)}. "
                        f"Confirm the outcome to the user."
                    ),
                }
            )

        while iterations < MAX_ITERATIONS:
            iterations += 1
            response = await claude.complete(
                system=system,
                messages=history,
                tools=[t.declaration() for t in tools],
                max_tokens=2048,
                fast=not user.is_owner,
            )
            total_in += response.input_tokens
            total_out += response.output_tokens

            if response.degraded:
                reply = await self._fallback(db, user, message, facility_id=facility_id)
                await self._persist(db, conversation, "assistant", reply, model="fallback")
                await db.commit()
                return AgentTurn(
                    reply=reply, conversation_id=conversation.id, degraded=True,
                    error=response.error, iterations=iterations,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )

            if not response.used_tools:
                reply = response.text or "I could not work that out. Could you rephrase?"
                await self._persist(
                    db, conversation, "assistant", reply,
                    tool_calls=executed, model=response.model,
                )
                conversation.total_tokens += total_in + total_out
                await db.commit()
                return AgentTurn(
                    reply=reply, tool_calls=executed, conversation_id=conversation.id,
                    iterations=iterations, input_tokens=total_in, output_tokens=total_out,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )

            # Echo the assistant turn back verbatim, then answer every tool_use
            # block in one user message — splitting them teaches the model to
            # stop batching parallel calls.
            history.append({"role": "assistant", "content": response.raw_content})

            tool_results: list[dict] = []
            for call in response.tool_calls:
                tool = TOOLS_BY_NAME.get(call["name"])
                if tool is None:
                    tool_results.append(
                        {
                            "type": "tool_result", "tool_use_id": call["id"],
                            "content": json.dumps({"error": f"unknown tool {call['name']}"}),
                            "is_error": True,
                        }
                    )
                    continue

                if tool.mutating and tool.name not in ctx.confirmed_tokens:
                    pending = {
                        "tool": tool.name,
                        "arguments": call["input"],
                        "description": tool.description,
                    }
                    tool_results.append(
                        {
                            "type": "tool_result", "tool_use_id": call["id"],
                            "content": json.dumps(
                                {
                                    "status": "confirmation_required",
                                    "message": (
                                        "This action changes money or pricing. Tell the user "
                                        "exactly what will happen and ask them to confirm. "
                                        "Do not retry the tool this turn."
                                    ),
                                }
                            ),
                        }
                    )
                    continue

                outcome = await self._execute(ctx, tool.name, call["input"])
                executed.append(outcome)
                tool_results.append(
                    {
                        "type": "tool_result", "tool_use_id": call["id"],
                        "content": json.dumps(outcome["result"], default=str),
                        "is_error": bool(outcome.get("error")),
                    }
                )

            history.append({"role": "user", "content": tool_results})

            if pending:
                # Let the model phrase the confirmation request, then stop.
                final = await claude.complete(
                    system=system, messages=history, max_tokens=512,
                    fast=not user.is_owner,
                )
                total_in += final.input_tokens
                total_out += final.output_tokens
                reply = final.text or "I need your confirmation before I can do that."
                await self._persist(
                    db, conversation, "assistant", reply,
                    tool_calls=executed, model=final.model,
                )
                await db.commit()
                return AgentTurn(
                    reply=reply, tool_calls=executed, pending_confirmation=pending,
                    conversation_id=conversation.id, iterations=iterations,
                    input_tokens=total_in, output_tokens=total_out,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )

        reply = (
            "I wasn't able to finish that request — it needed more steps than I'm "
            "allowed to take. Could you narrow it down?"
        )
        await self._persist(db, conversation, "assistant", reply, tool_calls=executed)
        await db.commit()
        return AgentTurn(
            reply=reply, tool_calls=executed, conversation_id=conversation.id,
            iterations=iterations, input_tokens=total_in, output_tokens=total_out,
            latency_ms=(time.perf_counter() - started) * 1000,
            error="iteration limit reached",
        )

    # ── Internals ─────────────────────────────────────────────

    async def _execute(self, ctx: ToolContext, name: str, args: dict) -> dict:
        tool = TOOLS_BY_NAME[name]
        started = time.perf_counter()
        try:
            result = await tool.handler(ctx, args or {})
            error = None
        except Exception as exc:
            log.error("agent tool failed", extra={"tool": name, "error": str(exc)})
            result = {"error": f"{name} failed: {exc}"}
            error = str(exc)

        elapsed = (time.perf_counter() - started) * 1000
        await bus.emit(
            Topic.AGENT_ACTION,
            {"tool": name, "arguments": args, "ok": error is None, "ms": round(elapsed, 1)},
            user_id=ctx.user.id, facility_id=ctx.facility_id,
        )
        return {
            "tool": name, "arguments": args, "result": result,
            "error": error, "latency_ms": round(elapsed, 1),
        }

    async def _conversation(
        self, db: AsyncSession, user: User, conversation_id: int | None,
        *, facility_id: int | None, channel: str,
    ) -> AgentConversation:
        if conversation_id:
            conversation = await db.get(AgentConversation, conversation_id)
            if conversation and conversation.user_id == user.id:
                return conversation
        conversation = AgentConversation(
            user_id=user.id, facility_id=facility_id, channel=channel,
            persona="owner" if user.is_owner else "driver",
            locale=user.locale, title="",
        )
        db.add(conversation)
        await db.flush()
        return conversation

    async def _history(self, db: AsyncSession, conversation: AgentConversation) -> list[dict]:
        """Replay the last few turns as plain text.

        Tool blocks are deliberately not replayed: they would need their exact
        `tool_use_id` pairing to remain valid, and the summary in the assistant
        text carries the information the next turn actually needs.
        """
        await db.refresh(conversation, ["messages"])
        recent = [m for m in conversation.messages if m.role in ("user", "assistant")][-8:]
        return [{"role": m.role, "content": m.content} for m in recent if m.content]

    async def _persist(
        self, db: AsyncSession, conversation: AgentConversation, role: str, content: str,
        *, tool_calls: list[dict] | None = None, model: str = "", latency_ms: float = 0.0,
    ) -> AgentMessage:
        message = AgentMessage(
            conversation_id=conversation.id, role=role, content=content,
            tool_calls=tool_calls or [], model=model, latency_ms=latency_ms,
        )
        db.add(message)
        if role == "user" and not conversation.title:
            conversation.title = content[:120]
        await db.flush()
        return message

    async def _fallback(
        self, db: AsyncSession, user: User, message: str, *, facility_id: int | None
    ) -> str:
        """Keyword routing used when the model is unavailable.

        Deliberately narrow: it answers the handful of questions that make up most
        of the traffic and otherwise says plainly that it cannot help, rather than
        guessing and sounding authoritative while wrong.
        """
        text = message.lower()
        ctx = ToolContext(db=db, user=user, facility_id=facility_id)

        if any(k in text for k in ("where", "find my", "my car", "my vehicle", "parked")):
            result = await ctx_safe(_call, ctx, "find_my_vehicle")
            if result.get("parked"):
                return (
                    f"Your vehicle {result['plate']} is in slot {result['slot_code']}, "
                    f"zone {result['zone']} at {result['facility']}. "
                    f"It has been there for {result['parked_for_minutes']:.0f} minutes."
                )
            return "You have no vehicle parked at the moment."

        if any(k in text for k in ("balance", "wallet", "money", "funds")):
            result = await ctx_safe(_call, ctx, "get_wallet_balance")
            if "balance" in result:
                return f"Your wallet balance is {result['balance']}."

        if any(k in text for k in ("cost", "charge", "bill", "owe", "how much")):
            result = await ctx_safe(_call, ctx, "get_current_charges")
            if result.get("active"):
                return (
                    f"You have been parked for {result['parked_for_minutes']:.0f} minutes. "
                    f"The charge so far is {result['amount_so_far']}."
                )
            return "You have no active parking session, so there is nothing to pay."

        if any(k in text for k in ("direction", "navigate", "how do i get", "route")):
            result = await ctx_safe(_call, ctx, "get_directions_to_my_slot")
            if "instructions" in result:
                return " ".join(result["instructions"])

        return (
            "The AI assistant is not configured on this deployment, so I can only "
            "answer a few set questions — where your vehicle is, your wallet balance, "
            "your current charges, or directions to your slot."
        )


async def _call(ctx: ToolContext, name: str) -> dict:
    return await TOOLS_BY_NAME[name].handler(ctx, {})


async def ctx_safe(fn, ctx: ToolContext, name: str) -> dict:
    try:
        return await fn(ctx, name)
    except Exception as exc:
        log.warning("fallback tool failed", extra={"tool": name, "error": str(exc)})
        return {}


agent_runtime = AgentRuntime()
