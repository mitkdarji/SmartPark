"""End-to-end ANPR: bytes in, a decided plate out.

    decode → detect regions → per-region preprocessing variants
           → run every active OCR backend → reconcile votes
           → escalate to Claude vision if the result is weak
           → persist an annotated crop + full audit record

The escalation step is what keeps the platform both cheap and accurate: the
free local readers handle the easy majority, and only genuinely ambiguous
frames pay for a model call.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from app.core.config import settings
from app.core.logging import get_logger
from app.services.anpr import detector, preprocess
from app.services.anpr.ensemble import Candidate, reconcile
from app.services.anpr.ocr_backends import OCRRead, active_backends, backend_status, get_backend
from app.services.anpr.plate_utils import normalize_plate, pretty_plate

log = get_logger(__name__)

# Below this the read is escalated to the vision model before deciding.
ESCALATION_THRESHOLD = 0.72


@dataclass(slots=True)
class RecognitionResult:
    plate: str
    plate_pretty: str
    confidence: float
    accepted: bool
    needs_review: bool
    backend: str
    candidates: list[dict] = field(default_factory=list)
    bbox: list[int] = field(default_factory=list)
    processing_ms: float = 0.0
    image_path: str | None = None
    annotated_path: str | None = None
    detections: int = 0
    reads: int = 0
    escalated: bool = False
    error: str | None = None
    # Set by the parking service once the audit row is persisted, so the
    # session can be linked back to the exact inference that produced it.
    event_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "plate_pretty": self.plate_pretty,
            "confidence": round(self.confidence, 4),
            "accepted": self.accepted,
            "needs_review": self.needs_review,
            "backend": self.backend,
            "candidates": self.candidates,
            "bbox": self.bbox,
            "processing_ms": round(self.processing_ms, 2),
            "image_path": self.image_path,
            "annotated_path": self.annotated_path,
            "detections": self.detections,
            "reads": self.reads,
            "escalated": self.escalated,
            "error": self.error,
        }


class ANPRPipeline:
    def __init__(self) -> None:
        self._min_confidence = settings.anpr_min_confidence

    # ── Public API ────────────────────────────────────────────

    def recognize_bytes(
        self,
        payload: bytes,
        *,
        known_plates: list[str] | None = None,
        persist: bool = True,
        tag: str = "capture",
    ) -> RecognitionResult:
        image = self._decode(payload)
        if image is None:
            return RecognitionResult(
                plate="", plate_pretty="", confidence=0.0, accepted=False,
                needs_review=True, backend="none", error="could not decode image",
            )
        return self.recognize(image, known_plates=known_plates, persist=persist, tag=tag)

    def recognize(
        self,
        image: np.ndarray,
        *,
        known_plates: list[str] | None = None,
        persist: bool = True,
        tag: str = "capture",
    ) -> RecognitionResult:
        started = time.perf_counter()
        known_plates = [normalize_plate(p) for p in (known_plates or []) if p]

        regions = detector.detect_plates(image)
        reads: list[OCRRead] = []
        backends = active_backends()

        for region_index, region in enumerate(regions[:3]):
            crop = detector.deskew(region.image)
            for variant_name, variant in preprocess.variants(crop):
                for backend in backends:
                    if backend.name == "vision_llm":
                        continue  # escalation only — never in the first pass
                    for read in backend.read(variant):
                        read.detail = {
                            **read.detail,
                            "variant": variant_name,
                            "region": region_index,
                            "region_score": region.score,
                        }
                        reads.append(read)

        result_ensemble = reconcile(reads, known_plates)
        escalated = False

        # Escalate a weak or empty consensus to the vision model.
        if (
            settings.genai_enabled
            and "vision_llm" in settings.anpr_ocr_backends
            and result_ensemble.confidence < ESCALATION_THRESHOLD
            and regions
        ):
            vision = get_backend("vision_llm")
            if vision and vision.available():
                escalated = True
                crop = detector.deskew(regions[0].image)
                vision_reads = vision.read(preprocess.upscale(crop, 160))
                if not vision_reads:
                    vision_reads = vision.read(image)
                if vision_reads:
                    reads.extend(vision_reads)
                    result_ensemble = reconcile(reads, known_plates)

        best: Candidate | None = result_ensemble.best
        elapsed_ms = (time.perf_counter() - started) * 1000

        if best is None:
            return RecognitionResult(
                plate="", plate_pretty="", confidence=0.0, accepted=False,
                needs_review=True, backend=",".join(b.name for b in backends),
                processing_ms=elapsed_ms, detections=len(regions), reads=len(reads),
                escalated=escalated, error="no readable text found",
            )

        accepted = best.confidence >= self._min_confidence
        top_region = regions[0] if regions else None

        image_path = annotated_path = None
        if persist:
            image_path, annotated_path = self._persist(image, top_region, best, tag)

        result = RecognitionResult(
            plate=best.plate,
            plate_pretty=pretty_plate(best.plate),
            confidence=best.confidence,
            accepted=accepted,
            needs_review=not accepted,
            backend=",".join(sorted({r.backend for r in reads})),
            candidates=[c.as_dict() for c in result_ensemble.candidates],
            bbox=top_region.bbox if top_region else [],
            processing_ms=elapsed_ms,
            image_path=image_path,
            annotated_path=annotated_path,
            detections=len(regions),
            reads=len(reads),
            escalated=escalated,
        )
        log.info(
            "anpr read",
            extra={
                "plate": result.plate, "confidence": result.confidence,
                "accepted": accepted, "ms": round(elapsed_ms, 1),
                "escalated": escalated, "reads": len(reads),
            },
        )
        return result

    def status(self) -> dict:
        return {
            "backends": backend_status(),
            "active": [b.name for b in active_backends()],
            "min_confidence": self._min_confidence,
            "escalation_threshold": ESCALATION_THRESHOLD,
            "vision_escalation_enabled": settings.genai_enabled,
            "region": settings.anpr_plate_region,
        }

    # ── Internals ─────────────────────────────────────────────

    @staticmethod
    def _decode(payload: bytes) -> np.ndarray | None:
        buffer = np.frombuffer(payload, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            return None
        # Cap the working resolution: gate frames arrive at up to 4K and the
        # morphology kernels are tuned for roughly 720p-1080p.
        max_w = 1600
        if image.shape[1] > max_w:
            scale = max_w / image.shape[1]
            image = cv2.resize(
                image, (max_w, int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA
            )
        return image

    @staticmethod
    def _persist(
        image: np.ndarray,
        region: detector.PlateCandidate | None,
        best: Candidate,
        tag: str,
    ) -> tuple[str | None, str | None]:
        try:
            stamp = datetime.now(UTC).strftime("%Y%m%d")
            folder: Path = settings.upload_dir / "captures" / stamp
            folder.mkdir(parents=True, exist_ok=True)
            uid = uuid.uuid4().hex[:10]

            raw_path = folder / f"{tag}_{uid}.jpg"
            cv2.imwrite(str(raw_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])

            annotated_path = None
            if region is not None:
                label = f"{pretty_plate(best.plate)} {best.confidence:.0%}"
                annotated = detector.annotate(image, region, label)
                ann = folder / f"{tag}_{uid}_annotated.jpg"
                cv2.imwrite(str(ann), annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                annotated_path = str(ann.relative_to(settings.data_dir))

            return str(raw_path.relative_to(settings.data_dir)), annotated_path
        except Exception as exc:
            log.warning("failed to persist capture", extra={"error": str(exc)})
            return None, None


anpr = ANPRPipeline()
