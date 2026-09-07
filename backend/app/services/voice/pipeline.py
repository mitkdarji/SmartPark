"""Voice interface: speech in, agent in the middle, speech out.

Two transport paths, because they suit different situations:

  Browser path (default).  The React client uses the Web Speech API for both
  recognition and synthesis, and sends SmartPark only text. Lowest latency, no
  audio ever leaves the device, and nothing to install.

  Server path.  For telephony, kiosks at the gate, and browsers without Web
  Speech, audio is posted here and transcribed with faster-whisper on-device.
  Speech is synthesised with pyttsx3. Both are optional imports: if neither is
  installed the endpoint says so explicitly instead of failing opaquely.

Ahead of the agent sits a small intent router. "Where is my car" is the single
most common utterance in a parking app; answering it from a database lookup in
~20 ms beats a model round trip on both latency and cost, and the agent still
handles everything the router is not confident about.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_whisper_model: Any = None
_whisper_checked = False


@dataclass(slots=True)
class Transcript:
    text: str
    confidence: float = 0.0
    language: str = "en"
    duration_s: float = 0.0
    backend: str = "none"
    error: str | None = None


@dataclass(slots=True)
class Intent:
    name: str
    confidence: float
    slots: dict = field(default_factory=dict)


# Patterns that map straight onto a tool, with no model call in between.
INTENT_PATTERNS: list[tuple[str, str, float]] = [
    (r"\b(where('?s| is)?\s+(my|the)\s+(car|vehicle|bike)|find my (car|vehicle)|"
     r"where did i park)\b", "find_my_vehicle", 0.95),
    (r"\b(how much (do i owe|is it|will it cost)|what('?s| is) my (bill|charge)|"
     r"current charge)\b", "get_current_charges", 0.9),
    (r"\b(wallet|balance|how much (money|credit) (do i have|is left))\b",
     "get_wallet_balance", 0.9),
    (r"\b(directions?|navigate|guide me|how do i get to my (slot|car|spot)|take me to)\b",
     "get_directions_to_my_slot", 0.88),
    (r"\b(recent (charges|transactions)|my (history|receipts)|last few charges)\b",
     "list_recent_charges", 0.85),
    (r"\b(find parking|somewhere to park|available parking|nearby parking)\b",
     "find_parking_nearby", 0.85),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), tool, conf) for p, tool, conf in INTENT_PATTERNS]


def classify_intent(text: str) -> Intent | None:
    """Return a high-confidence tool match, or None to defer to the agent."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    for pattern, tool, confidence in _COMPILED:
        if pattern.search(cleaned):
            return Intent(name=tool, confidence=confidence)
    return None


def _load_whisper():
    global _whisper_model, _whisper_checked
    if _whisper_checked:
        return _whisper_model
    _whisper_checked = True
    if settings.voice_stt_backend not in ("faster_whisper", "whisper_local"):
        return None
    try:
        from faster_whisper import WhisperModel  # type: ignore

        # `base` balances accuracy against CPU cost; a gate kiosk has no GPU.
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log.info("faster-whisper loaded")
    except Exception as exc:
        log.info("faster-whisper unavailable", extra={"error": str(exc)})
        _whisper_model = None
    return _whisper_model


def transcribe(audio: bytes, *, language: str | None = None) -> Transcript:
    model = _load_whisper()
    if model is None:
        return Transcript(
            text="", backend="unavailable",
            error=(
                "No server-side speech recogniser is installed. Install "
                "faster-whisper (pip install -r requirements-ai.txt), or use the "
                "browser's Web Speech API and post text instead of audio."
            ),
        )

    started = time.perf_counter()
    try:
        segments, info = model.transcribe(
            io.BytesIO(audio), language=language, beam_size=5, vad_filter=True
        )
        pieces, confidences = [], []
        for segment in segments:
            pieces.append(segment.text)
            # avg_logprob is a log probability; exponentiate for a 0-1 score.
            confidences.append(min(1.0, max(0.0, 2 ** segment.avg_logprob)))
    except Exception as exc:
        log.warning("transcription failed", extra={"error": str(exc)})
        return Transcript(text="", backend="faster_whisper", error=str(exc))

    text = " ".join(p.strip() for p in pieces).strip()
    return Transcript(
        text=text,
        confidence=round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        language=getattr(info, "language", language or "en"),
        duration_s=round(time.perf_counter() - started, 3),
        backend="faster_whisper",
    )


def synthesize(text: str) -> tuple[bytes | None, str | None]:
    """Render speech to WAV bytes. Returns (audio, error)."""
    if settings.voice_tts_backend != "pyttsx3":
        return None, "server-side speech synthesis is disabled"
    try:
        import tempfile

        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        engine.save_to_file(text, path)
        engine.runAndWait()
        with open(path, "rb") as handle:
            return handle.read(), None
    except Exception as exc:
        log.info("tts unavailable", extra={"error": str(exc)})
        return None, (
            "No server-side speech synthesiser is installed. Install pyttsx3, or "
            "let the browser speak the reply with speechSynthesis."
        )


def speakable(text: str) -> str:
    """Rewrite an answer so a speech synthesiser reads it naturally.

    Slot codes and plates are spelled out; markdown and currency codes are
    stripped. Without this, "B014" is read as "bee fourteen" and drivers walk to
    the wrong bay.
    """
    out = re.sub(r"[*_`#]+", "", text or "")
    out = out.replace("₹", " rupees ").replace("INR", "rupees")

    def spell(match: re.Match) -> str:
        code = match.group(0)
        return " ".join(
            ch if ch.isalpha() else str(int(ch)) for ch in code if ch.isalnum()
        )

    # Slot codes such as A007 / B14 — spoken character by character.
    out = re.sub(r"\b[A-Z]{1,2}\d{2,4}\b", spell, out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def status() -> dict:
    return {
        "stt_backend": settings.voice_stt_backend,
        "stt_available": _load_whisper() is not None,
        "tts_backend": settings.voice_tts_backend,
        "intent_router_patterns": len(_COMPILED),
        "browser_fallback": (
            "Use the Web Speech API in the browser and post text to /voice/converse."
        ),
    }
