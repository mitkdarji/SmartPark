"""ANPR pipeline: detection, OCR ensemble, and the context prior."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.anpr.ensemble import reconcile
from app.services.anpr.ocr_backends import OCRRead, backend_status
from app.services.anpr.pipeline import anpr
from app.services.anpr.plate_utils import plate_similarity
from app.services.anpr.preprocess import binarize, segment_characters, upscale, variants
from app.services.anpr.synthetic import encode_png, render_plate, synth_capture


def test_the_builtin_reader_is_always_available():
    """The platform must never hard-depend on an optional OCR runtime."""
    assert backend_status()["segmentation"] is True


def test_segmentation_finds_every_glyph_on_a_clean_plate():
    plate = render_plate("MH12AB1234")
    glyphs = segment_characters(binarize(upscale(plate)))
    assert len(glyphs) == 10


def test_preprocessing_produces_several_distinct_views():
    frame, _ = synth_capture(difficulty=0.2, seed=1)
    views = variants(frame)
    assert len(views) >= 4
    assert {name for name, _ in views} >= {"gray", "enhanced", "binary"}


@pytest.mark.parametrize("difficulty", [0.0, 0.2, 0.4])
def test_recognition_reads_synthetic_frames(difficulty):
    hits = 0
    for seed in range(8):
        frame, truth = synth_capture(difficulty=difficulty, seed=seed)
        result = anpr.recognize(frame, persist=False)
        if plate_similarity(result.plate, truth) >= 0.9:
            hits += 1
    # Deliberately loose: this asserts the pipeline works, not a headline number.
    # The measured accuracy figure lives in the benchmark endpoint.
    assert hits >= 6, f"only {hits}/8 frames read at difficulty {difficulty}"


def test_recognition_accepts_raw_bytes():
    frame, truth = synth_capture(difficulty=0.15, seed=3)
    result = anpr.recognize_bytes(encode_png(frame), persist=False)
    assert result.plate == truth
    assert result.confidence > 0.5
    assert result.processing_ms > 0


def test_undecodable_input_fails_cleanly():
    result = anpr.recognize_bytes(b"this is not an image", persist=False)
    assert not result.accepted
    assert result.needs_review
    assert result.error


def test_ensemble_prefers_the_backend_with_more_agreement():
    reads = [
        OCRRead("MH12AB1234", 0.90, "easyocr"),
        OCRRead("MH12AB1234", 0.70, "tesseract"),
        OCRRead("MH12A81234", 0.65, "segmentation"),
    ]
    result = reconcile(reads)
    assert result.plate == "MH12AB1234"
    assert result.confidence > 0.6


def test_ensemble_repairs_a_positional_ocr_error():
    result = reconcile([OCRRead("MHI2AB1234", 0.8, "easyocr")])
    assert result.plate == "MH12AB1234"


def test_context_prior_rescues_a_noisy_exit_read():
    """The plates parked inside are a small known set — use them."""
    without_context = reconcile([OCRRead("GJ0IAB1Z34", 0.55, "segmentation")])
    with_context = reconcile(
        [OCRRead("GJ0IAB1Z34", 0.55, "segmentation")], known_plates=["GJ01AB1234"]
    )
    assert with_context.plate == "GJ01AB1234"
    assert with_context.confidence > without_context.confidence


def test_ensemble_handles_no_readable_text():
    assert reconcile([]).best is None
    assert reconcile([OCRRead("A", 0.9, "easyocr")]).best is None


def test_synthetic_generator_is_reproducible():
    a, plate_a = synth_capture(difficulty=0.3, seed=11)
    b, plate_b = synth_capture(difficulty=0.3, seed=11)
    assert plate_a == plate_b
    assert (a == b).all()


def test_synthetic_generator_honours_a_requested_plate():
    _, truth = synth_capture("KA05MN9090", difficulty=0.2, seed=1)
    assert truth == "KA05MN9090"


def test_pipeline_reports_backend_status():
    status = anpr.status()
    assert "segmentation" in status["backends"]
    assert status["min_confidence"] > 0
    assert status["active"]


# ─────────────────────────────────────────────────────────────
# The limitation of the built-in reader, pinned as a test.
#
# The synthetic benchmark scores near-perfectly, which is easy to mistake for a
# recognition-accuracy result. It is not: the generator and the built-in reader
# share a font family, so the benchmark measures the *pipeline*. These tests make
# that explicit, so nobody — including a future reader of the README — mistakes
# one for the other.
# ─────────────────────────────────────────────────────────────

REAL_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def _plate_in_font(text: str, font_path: str = REAL_FONT) -> np.ndarray:
    """Render a plate with a TrueType font the template matcher has never seen."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (440, 100), (238, 240, 238))
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, 435, 95], outline=(18, 18, 20), width=3)
    font = ImageFont.truetype(font_path, 58)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((440 - (box[2] - box[0])) // 2, (100 - (box[3] - box[1])) // 2 - 8),
        text, font=font, fill=(18, 18, 20),
    )
    plate = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    frame = np.full((540, 960, 3), 70, np.uint8)
    cv2.rectangle(frame, (80, 100), (880, 520), (90, 95, 105), -1)
    height, width = 110, 484
    frame[330 : 330 + height, 240 : 240 + width] = cv2.resize(plate, (width, height))
    return cv2.GaussianBlur(frame, (3, 3), 0)


@pytest.mark.skipif(
    not Path(REAL_FONT).exists(), reason="needs a system TrueType font to render with"
)
def test_builtin_reader_does_not_generalise_beyond_its_own_font():
    """The headline synthetic score does not transfer to an unseen font.

    This is the single most important caveat about the recognition numbers, so it
    is asserted rather than only written down. If a future backend makes the
    built-in reader genuinely font-independent, this test fails and the README
    claim should be revisited — which is exactly the intent.
    """
    plates = ["GJ01AB1234", "MH12XY5678", "KA05MN9012", "DL03CD4567", "TN09PQ3321"]

    own_font = sum(
        anpr.recognize(synth_capture(p, difficulty=0.2, seed=1)[0], persist=False).plate == p
        for p in plates
    )
    unseen_font = sum(
        anpr.recognize(_plate_in_font(p), persist=False).plate == p for p in plates
    )

    assert own_font >= 4, f"pipeline regression: only {own_font}/5 on its own font"
    assert unseen_font < own_font, (
        f"the built-in reader scored {unseen_font}/5 on an unseen font vs "
        f"{own_font}/5 on its own. If that gap has closed, the recognition caveat "
        f"in README.md and docs/EVALUATION.md is now wrong and must be updated."
    )


@pytest.mark.skipif(
    not Path(REAL_FONT).exists(), reason="needs a system TrueType font to render with"
)
def test_a_failed_read_is_reported_as_needing_review_not_guessed():
    """Failing is fine; failing *silently* is not.

    A gate that opens on a confident wrong answer is far worse than one that
    flags for review, so an unreadable frame must come back low-confidence.
    """
    result = anpr.recognize(_plate_in_font("MH12XY5678"), persist=False)
    if result.plate != "MH12XY5678":
        assert result.needs_review or not result.accepted, (
            "a misread was returned as accepted with high confidence"
        )
