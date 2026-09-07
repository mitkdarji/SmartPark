"""Thin wrapper around the Anthropic SDK.

Everything generative in SmartPark goes through here so that model selection,
retries, token accounting, error mapping and — most importantly — *graceful
degradation* live in exactly one place.

Degradation is a first-class feature, not an afterthought: with no API key the
client reports `enabled = False` and every caller falls back to a deterministic
local implementation. The platform stays fully demonstrable offline, which is
what makes it safe to depend on AI in the request path at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Non-streaming default. Long analytical answers use `stream=True` instead.
DEFAULT_MAX_TOKENS = 16_000


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    raw_content: list[Any] = field(default_factory=list)
    degraded: bool = False
    error: str | None = None

    @property
    def used_tools(self) -> bool:
        return self.stop_reason == "tool_use"


class ClaudeClient:
    def __init__(self) -> None:
        self._async_client: anthropic.AsyncAnthropic | None = None
        self._sync_client: anthropic.Anthropic | None = None

    @property
    def enabled(self) -> bool:
        return settings.genai_enabled

    def _async(self) -> anthropic.AsyncAnthropic:
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key, max_retries=3, timeout=60.0
            )
        return self._async_client

    def _sync(self) -> anthropic.Anthropic:
        if self._sync_client is None:
            self._sync_client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key, max_retries=2, timeout=30.0
            )
        return self._sync_client

    def model_for(self, fast: bool) -> str:
        return settings.genai_fast_model if fast else settings.genai_model

    # ── Text completion ───────────────────────────────────────

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int | None = None,
        fast: bool = False,
        tools: list[dict] | None = None,
        effort: str | None = None,
        thinking: bool = False,
    ) -> LLMResponse:
        if not self.enabled:
            return LLMResponse(
                text="", model="unavailable", degraded=True,
                error="ANTHROPIC_API_KEY is not configured",
            )

        model = self.model_for(fast)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if thinking:
            # Adaptive thinking: the model decides how much reasoning a turn
            # needs. Used for the analytics copilot, not for short lookups.
            kwargs["thinking"] = {"type": "adaptive"}
        if effort:
            kwargs["output_config"] = {"effort": effort}

        try:
            response = await self._async().messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            log.warning("claude rate limited", extra={"error": str(exc)})
            return LLMResponse(text="", model=model, degraded=True, error="rate_limited")
        except anthropic.APIStatusError as exc:
            log.error("claude api error", extra={"status": exc.status_code, "error": str(exc)})
            return LLMResponse(
                text="", model=model, degraded=True, error=f"api_error_{exc.status_code}"
            )
        except anthropic.APIConnectionError as exc:
            log.error("claude connection error", extra={"error": str(exc)})
            return LLMResponse(text="", model=model, degraded=True, error="connection_error")

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})

        return LLMResponse(
            text="\n".join(text_parts).strip(),
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            raw_content=list(response.content),
        )

    async def complete_text(
        self, prompt: str, *, system: str = "", fast: bool = True, max_tokens: int = 1024
    ) -> str:
        """Single-shot helper for short, self-contained generations."""
        response = await self.complete(
            system=system or "You are a concise, factual assistant.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            fast=fast,
        )
        return response.text

    # ── Vision ────────────────────────────────────────────────

    def complete_with_image_sync(
        self,
        *,
        prompt: str,
        image_b64: str,
        media_type: str = "image/png",
        max_tokens: int = 64,
        fast: bool = True,
    ) -> str:
        """Blocking image read, called from the synchronous ANPR pipeline.

        Kept synchronous on purpose: the CV pipeline runs in a worker thread and
        wrapping this in an event loop would only add complexity.
        """
        if not self.enabled:
            return ""
        try:
            response = self._sync().messages.create(
                model=self.model_for(fast),
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except anthropic.APIError as exc:
            log.warning("claude vision call failed", extra={"error": str(exc)})
            return ""

        return "".join(b.text for b in response.content if b.type == "text").strip()

    # ── Streaming ─────────────────────────────────────────────

    async def stream(
        self, *, system: str, messages: list[dict], max_tokens: int = 64_000, fast: bool = False
    ):
        """Yield text deltas. Used by the copilot so long answers appear live."""
        if not self.enabled:
            yield ""
            return
        try:
            async with self._async().messages.stream(
                model=self.model_for(fast),
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            log.error("claude stream failed", extra={"error": str(exc)})
            yield f"\n[the assistant is unavailable: {exc.__class__.__name__}]"

    async def health(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "reason": "no API key configured"}
        try:
            await asyncio.wait_for(
                self._async().messages.create(
                    model=settings.genai_fast_model,
                    max_tokens=8,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=15,
            )
            return {"enabled": True, "model": settings.genai_model, "reachable": True}
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": str(exc)}


claude = ClaudeClient()
