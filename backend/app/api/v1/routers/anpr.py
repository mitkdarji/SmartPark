"""Standalone ANPR endpoints — recognition without a gate attached."""

from __future__ import annotations

import base64

from fastapi import APIRouter, File, Query, Response, UploadFile

from app.api.deps import CurrentUser
from app.core.errors import Conflict
from app.services.anpr.pipeline import anpr
from app.services.anpr.plate_utils import (
    classify_plate,
    is_valid_plate,
    normalize_plate,
    plate_similarity,
    pretty_plate,
    repair_plate,
)
from app.services.anpr.synthetic import encode_png, synth_capture

router = APIRouter(prefix="/anpr", tags=["anpr"])

MAX_IMAGE_BYTES = 12 * 1024 * 1024


@router.get("/status", response_model=dict)
async def status() -> dict:
    """Which OCR backends are loadable right now, and the decision thresholds."""
    return anpr.status()


@router.post("/recognize", response_model=dict)
async def recognize(
    user: CurrentUser,
    image: UploadFile = File(...),
    known_plates: str = Query(default="", description="Comma-separated context prior."),
) -> dict:
    payload = await image.read()
    if not payload:
        raise Conflict("The uploaded image is empty.")
    if len(payload) > MAX_IMAGE_BYTES:
        raise Conflict("Image is larger than the 12 MB limit.")

    context = [p.strip() for p in known_plates.split(",") if p.strip()]
    result = anpr.recognize_bytes(payload, known_plates=context, persist=False)
    return result.as_dict()


@router.get("/sample", summary="Generate a labelled synthetic gate frame")
async def sample_frame(
    plate: str | None = Query(default=None),
    difficulty: float = Query(default=0.35, ge=0.0, le=1.0),
    seed: int | None = Query(default=None),
    ev: bool = Query(default=False),
) -> Response:
    """A PNG the gate simulator and the demo UI can feed straight back in."""
    frame, truth = synth_capture(plate, difficulty=difficulty, seed=seed, ev=ev)
    return Response(
        content=encode_png(frame),
        media_type="image/png",
        headers={"X-Ground-Truth-Plate": truth, "X-Difficulty": str(difficulty)},
    )


@router.get("/sample.json", response_model=dict)
async def sample_frame_json(
    plate: str | None = Query(default=None),
    difficulty: float = Query(default=0.35, ge=0.0, le=1.0),
    seed: int | None = Query(default=None),
) -> dict:
    """The same frame as base64, for clients that want to display and re-post it."""
    frame, truth = synth_capture(plate, difficulty=difficulty, seed=seed)
    return {
        "ground_truth_plate": truth,
        "pretty": pretty_plate(truth),
        "difficulty": difficulty,
        "image_base64": base64.b64encode(encode_png(frame)).decode(),
        "media_type": "image/png",
    }


@router.get("/validate", response_model=dict)
async def validate_plate(plate: str = Query(..., min_length=2, max_length=24)) -> dict:
    """Normalise, classify and repair a plate string — no image involved."""
    normalized = normalize_plate(plate)
    repair = repair_plate(normalized)
    return {
        "input": plate,
        "normalized": normalized,
        "pretty": pretty_plate(repair.plate),
        "format": classify_plate(normalized),
        "valid": is_valid_plate(normalized),
        "strictly_valid": is_valid_plate(normalized, strict=True),
        "repaired": repair.plate if repair.changed else None,
        "repair_edits": repair.edits,
        "repaired_format": repair.format,
    }


@router.get("/compare", response_model=dict)
async def compare_plates(a: str = Query(...), b: str = Query(...)) -> dict:
    """OCR-aware similarity between two plate strings."""
    return {
        "a": normalize_plate(a),
        "b": normalize_plate(b),
        "similarity": plate_similarity(a, b),
        "would_match_at_82": plate_similarity(a, b) >= 0.82,
    }
