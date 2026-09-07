"""Consensus voting across OCR backends, preprocessing variants, and priors.

Any single reader is unreliable on gate imagery. The ensemble treats each read
as a weighted vote for a *repaired* plate string, then adds two priors:

  format prior   a string matching the Indian plate grammar is far more likely
                 to be a correct read than one that does not.
  context prior  at an exit gate the vehicle is almost certainly one of the
                 handful currently parked inside, so agreement with that set is
                 strong evidence.

The winning string carries a calibrated confidence that the caller compares
against `ANPR_MIN_CONFIDENCE` to decide auto-open vs. human review.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.services.anpr.ocr_backends import OCRRead
from app.services.anpr.plate_utils import (
    PlateFormat,
    best_match,
    classify_plate,
    normalize_plate,
    repair_plate,
)

# Trust weights, reflecting each reader's measured reliability on plate crops.
BACKEND_WEIGHT = {
    "easyocr": 1.00,
    "vision_llm": 0.95,
    "tesseract": 0.75,
    "segmentation": 0.60,
}

FORMAT_BONUS = {
    PlateFormat.STANDARD: 0.30,
    PlateFormat.BHARAT: 0.25,
    PlateFormat.LOOSE: 0.02,
    PlateFormat.INVALID: -0.25,
}

CONTEXT_BONUS = 0.35        # maximum credit for matching a known on-site plate
CONTEXT_THRESHOLD = 0.82


@dataclass(slots=True)
class Candidate:
    plate: str
    score: float
    confidence: float
    format: str
    votes: list[str] = field(default_factory=list)
    repaired_from: str | None = None
    matched_known: str | None = None

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "format": self.format,
            "votes": self.votes,
            "repaired_from": self.repaired_from,
            "matched_known": self.matched_known,
        }


@dataclass(slots=True)
class EnsembleResult:
    best: Candidate | None
    candidates: list[Candidate]

    @property
    def plate(self) -> str:
        return self.best.plate if self.best else ""

    @property
    def confidence(self) -> float:
        return self.best.confidence if self.best else 0.0


def _length_plausibility(plate: str) -> float:
    """Indian plates are 9-10 characters; penalise reads far from that."""
    n = len(plate)
    if 9 <= n <= 10:
        return 1.0
    if n in (8, 11):
        return 0.7
    if n in (6, 7):
        return 0.45
    return 0.2


def reconcile(reads: list[OCRRead], known_plates: list[str] | None = None) -> EnsembleResult:
    known_plates = known_plates or []

    # Accumulate weighted votes per repaired plate string.
    buckets: dict[str, dict] = defaultdict(
        lambda: {"weight": 0.0, "conf": [], "votes": [], "raw": set()}
    )

    for read in reads:
        raw = normalize_plate(read.text)
        if len(raw) < 4:
            continue
        repair = repair_plate(raw)
        key = repair.plate
        weight = BACKEND_WEIGHT.get(read.backend, 0.5) * max(read.confidence, 0.01)
        if repair.changed:
            # A repaired read is good evidence, but slightly less than a clean one.
            weight *= 0.92
        bucket = buckets[key]
        bucket["weight"] += weight
        bucket["conf"].append(read.confidence)
        bucket["votes"].append(f"{read.backend}:{read.text}({read.confidence:.2f})")
        bucket["raw"].add(raw)

    if not buckets:
        return EnsembleResult(best=None, candidates=[])

    total_weight = sum(b["weight"] for b in buckets.values()) or 1.0

    candidates: list[Candidate] = []
    for plate, bucket in buckets.items():
        fmt = classify_plate(plate)
        share = bucket["weight"] / total_weight
        mean_conf = sum(bucket["conf"]) / len(bucket["conf"])

        score = share
        score += FORMAT_BONUS.get(fmt, 0.0)
        score *= _length_plausibility(plate)

        matched, sim = best_match(plate, known_plates, threshold=CONTEXT_THRESHOLD)
        if matched:
            score += CONTEXT_BONUS * sim

        # Independent agreement across distinct backends is the strongest signal.
        distinct_backends = {v.split(":", 1)[0] for v in bucket["votes"]}
        if len(distinct_backends) > 1:
            score *= 1.0 + 0.15 * (len(distinct_backends) - 1)

        # Confidence blends how much of the vote mass this plate won with how
        # sure the readers themselves were, then is nudged by the priors.
        confidence = 0.55 * share + 0.45 * mean_conf
        if fmt in (PlateFormat.STANDARD, PlateFormat.BHARAT):
            confidence = min(1.0, confidence + 0.15)
        else:
            confidence *= 0.7
        if matched:
            confidence = min(1.0, confidence + 0.20 * sim)

        raw_forms = bucket["raw"]
        candidates.append(
            Candidate(
                plate=plate,
                score=round(score, 4),
                confidence=round(min(1.0, max(0.0, confidence)), 4),
                format=fmt,
                votes=bucket["votes"],
                repaired_from=next(iter(raw_forms)) if plate not in raw_forms else None,
                matched_known=matched,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]

    # If the winner is malformed but closely matches a plate we know is on site,
    # prefer the known plate — an exit scan must settle the right session.
    if best.format == PlateFormat.INVALID and best.matched_known:
        best = Candidate(
            plate=best.matched_known,
            score=best.score,
            confidence=best.confidence,
            format=classify_plate(best.matched_known),
            votes=best.votes,
            repaired_from=best.plate,
            matched_known=best.matched_known,
        )
        candidates.insert(0, best)

    return EnsembleResult(best=best, candidates=candidates[:6])
