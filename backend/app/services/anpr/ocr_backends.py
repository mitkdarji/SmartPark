"""Pluggable OCR backends behind one interface.

Four readers, deliberately different in their failure modes:

  `segmentation`  glyph segmentation + normalised template correlation. Pure
                  OpenCV/NumPy, no model download, ~2 ms. Always available, so
                  the platform never has a hard dependency on a heavy runtime.
  `easyocr`       CRNN deep model. Best on real-world photographs. Optional.
  `tesseract`     LSTM engine via pytesseract, constrained to a plate charset
                  and single-line page segmentation. Optional.
  `vision_llm`    Claude vision. Slowest and costs a call, but it reads plates
                  that are dirty, partially occluded, or oddly lit — the exact
                  cases the others fail on. Used as a tie-breaker/escalation.

Each returns `OCRRead`s; the ensemble in `ensemble.py` reconciles them.
"""

from __future__ import annotations

import base64
import string
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

import cv2
import numpy as np

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

CHARSET = string.ascii_uppercase + string.digits


@dataclass(slots=True)
class OCRRead:
    text: str
    confidence: float
    backend: str
    elapsed_ms: float = 0.0
    detail: dict = field(default_factory=dict)


class OCRBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self, image: np.ndarray) -> list[OCRRead]: ...


# ─────────────────────────────────────────────────────────────
# 1. Segmentation + template matching (always available)
# ─────────────────────────────────────────────────────────────

GLYPH_SIZE = (32, 48)  # (w, h) that every glyph and template is resized to


@lru_cache(maxsize=1)
def _templates() -> dict[str, np.ndarray]:
    """Render one normalised bitmap per character of the plate alphabet."""
    out: dict[str, np.ndarray] = {}
    for ch in CHARSET:
        canvas = np.zeros((80, 60), dtype=np.uint8)
        (tw, th), _ = cv2.getTextSize(ch, cv2.FONT_HERSHEY_SIMPLEX, 2.0, 4)
        cv2.putText(canvas, ch, ((60 - tw) // 2, (80 + th) // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.0, 255, 4, cv2.LINE_AA)
        out[ch] = _normalize_glyph(canvas)
    return out


def _normalize_glyph(glyph: np.ndarray) -> np.ndarray:
    """Crop to ink, pad to a square-ish box, resize, and zero-mean.

    Zero-meaning makes the correlation score insensitive to stroke thickness,
    which varies a lot between fonts and after thresholding.
    """
    ys, xs = np.nonzero(glyph)
    if len(xs) == 0:
        return np.zeros(GLYPH_SIZE[::-1], dtype=np.float32)
    cropped = glyph[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    h, w = cropped.shape
    pad = max(2, int(0.1 * max(h, w)))
    padded = cv2.copyMakeBorder(cropped, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    resized = cv2.resize(padded, GLYPH_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)
    resized /= 255.0
    resized -= resized.mean()
    norm = np.linalg.norm(resized)
    return resized / norm if norm > 1e-6 else resized


class SegmentationBackend:
    name = "segmentation"

    def available(self) -> bool:
        return True

    def read(self, image: np.ndarray) -> list[OCRRead]:
        from app.services.anpr.preprocess import binarize, segment_characters, upscale

        started = time.perf_counter()
        binary = binarize(upscale(image))
        glyphs = segment_characters(binary)
        if not glyphs:
            return []

        templates = _templates()
        chars: list[str] = []
        scores: list[float] = []
        for glyph in glyphs[:12]:
            vector = _normalize_glyph(glyph).ravel()
            best_ch, best_score = "?", -1.0
            for ch, template in templates.items():
                score = float(np.dot(vector, template.ravel()))
                if score > best_score:
                    best_ch, best_score = ch, score
            chars.append(best_ch)
            # Correlation lives in [-1,1]; map to a probability-like [0,1].
            scores.append(max(0.0, min(1.0, (best_score + 1.0) / 2.0)))

        text = "".join(chars)
        confidence = float(np.mean(scores)) if scores else 0.0
        # Template matching is inherently weaker than a learned model; discount
        # its confidence so it never outvotes a deep backend on its own.
        confidence *= 0.85
        return [
            OCRRead(
                text=text,
                confidence=round(confidence, 4),
                backend=self.name,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                detail={"glyphs": len(glyphs), "per_char": [round(s, 3) for s in scores]},
            )
        ]


# ─────────────────────────────────────────────────────────────
# 2. EasyOCR
# ─────────────────────────────────────────────────────────────

class EasyOCRBackend:
    name = "easyocr"

    def __init__(self) -> None:
        self._reader = None
        self._checked = False

    def _load(self):
        if self._checked:
            return self._reader
        self._checked = True
        try:
            import easyocr  # type: ignore

            self._reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            log.info("easyocr backend loaded")
        except Exception as exc:
            log.info("easyocr unavailable", extra={"error": str(exc)})
            self._reader = None
        return self._reader

    def available(self) -> bool:
        return self._load() is not None

    def read(self, image: np.ndarray) -> list[OCRRead]:
        reader = self._load()
        if reader is None:
            return []
        started = time.perf_counter()
        try:
            results = reader.readtext(image, allowlist=CHARSET, detail=1, paragraph=False)
        except Exception as exc:
            log.warning("easyocr read failed", extra={"error": str(exc)})
            return []
        elapsed = round((time.perf_counter() - started) * 1000, 2)

        reads = [
            OCRRead(text=str(text), confidence=float(conf), backend=self.name, elapsed_ms=elapsed)
            for _box, text, conf in results
            if str(text).strip()
        ]
        # A two-row plate is returned as two fragments; the concatenation is
        # usually the correct full plate, so offer it as an extra candidate.
        if len(reads) > 1:
            joined = "".join(r.text for r in reads)
            reads.append(
                OCRRead(
                    text=joined,
                    confidence=float(np.mean([r.confidence for r in reads])),
                    backend=self.name, elapsed_ms=elapsed, detail={"merged": True},
                )
            )
        return reads


# ─────────────────────────────────────────────────────────────
# 3. Tesseract
# ─────────────────────────────────────────────────────────────

class TesseractBackend:
    name = "tesseract"

    def __init__(self) -> None:
        self._ok: bool | None = None

    def available(self) -> bool:
        if self._ok is None:
            try:
                import pytesseract  # type: ignore

                pytesseract.get_tesseract_version()
                self._ok = True
            except Exception as exc:
                log.info("tesseract unavailable", extra={"error": str(exc)})
                self._ok = False
        return self._ok

    def read(self, image: np.ndarray) -> list[OCRRead]:
        if not self.available():
            return []
        import pytesseract  # type: ignore

        started = time.perf_counter()
        config = f"--oem 3 --psm 7 -c tessedit_char_whitelist={CHARSET}"
        try:
            data = pytesseract.image_to_data(
                image, config=config, output_type=pytesseract.Output.DICT
            )
        except Exception as exc:
            log.warning("tesseract read failed", extra={"error": str(exc)})
            return []
        elapsed = round((time.perf_counter() - started) * 1000, 2)

        words, confs = [], []
        for text, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
            text = (text or "").strip()
            try:
                conf_val = float(conf)
            except (TypeError, ValueError):
                continue
            if text and conf_val >= 0:
                words.append(text)
                confs.append(conf_val / 100.0)
        if not words:
            return []
        return [
            OCRRead(
                text="".join(words),
                confidence=round(float(np.mean(confs)), 4),
                backend=self.name, elapsed_ms=elapsed,
            )
        ]


# ─────────────────────────────────────────────────────────────
# 4. Claude vision (generative-AI escalation path)
# ─────────────────────────────────────────────────────────────

VISION_PROMPT = (
    "You are reading a vehicle number plate from a parking-gate camera.\n"
    "Reply with ONLY the plate characters — uppercase letters and digits, no spaces, "
    "no punctuation, no explanation.\n"
    "If the plate is unreadable reply exactly: UNREADABLE\n"
    "Indian plates follow the pattern <2 letters><1-2 digits><1-3 letters><4 digits>, "
    "e.g. MH12AB1234. Use that grammar to disambiguate 0/O, 1/I, 8/B, 5/S, 2/Z."
)


class VisionLLMBackend:
    """Escalation reader. Only invoked when cheaper backends disagree or fail."""

    name = "vision_llm"

    def available(self) -> bool:
        return settings.genai_enabled

    def read(self, image: np.ndarray) -> list[OCRRead]:
        if not self.available():
            return []
        from app.services.genai.client import claude

        started = time.perf_counter()
        ok, buffer = cv2.imencode(".png", image)
        if not ok:
            return []
        b64 = base64.b64encode(buffer.tobytes()).decode()
        try:
            text = claude.complete_with_image_sync(
                prompt=VISION_PROMPT, image_b64=b64, media_type="image/png",
                max_tokens=24, fast=True,
            )
        except Exception as exc:
            log.warning("vision llm read failed", extra={"error": str(exc)})
            return []

        cleaned = "".join(ch for ch in (text or "").upper() if ch in CHARSET)
        if not cleaned or "UNREADABLE" in (text or "").upper():
            return []
        return [
            OCRRead(
                text=cleaned,
                # High but not absolute: a language model can hallucinate a
                # plausible plate, so it must still clear ensemble consensus.
                confidence=0.88,
                backend=self.name,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        ]


# ─────────────────────────────────────────────────────────────

_REGISTRY: dict[str, OCRBackend] = {
    SegmentationBackend.name: SegmentationBackend(),
    EasyOCRBackend.name: EasyOCRBackend(),
    TesseractBackend.name: TesseractBackend(),
    VisionLLMBackend.name: VisionLLMBackend(),
}

# `synthetic` is the legacy alias for the built-in reader used in config files.
_REGISTRY["synthetic"] = _REGISTRY[SegmentationBackend.name]


def get_backend(name: str) -> OCRBackend | None:
    return _REGISTRY.get(name)


def active_backends() -> list[OCRBackend]:
    """Configured backends that are actually loadable right now."""
    chosen: list[OCRBackend] = []
    seen: set[str] = set()
    for name in settings.anpr_ocr_backends:
        backend = _REGISTRY.get(name)
        if backend and backend.name not in seen and backend.available():
            chosen.append(backend)
            seen.add(backend.name)
    if not chosen:
        chosen = [_REGISTRY[SegmentationBackend.name]]
    return chosen


def backend_status() -> dict[str, bool]:
    return {
        name: backend.available()
        for name, backend in _REGISTRY.items()
        if name != "synthetic"
    }
