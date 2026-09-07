"""Plate normalisation, validation, repair and fuzzy matching."""

from __future__ import annotations

import pytest

from app.services.anpr.plate_utils import (
    PlateFormat,
    best_match,
    classify_plate,
    is_valid_plate,
    normalize_plate,
    plate_similarity,
    pretty_plate,
    repair_plate,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MH 12 AB 1234", "MH12AB1234"),
        ("mh12ab1234", "MH12AB1234"),
        ("MH-12-AB-1234", "MH12AB1234"),
        ("  gj01ab1234  ", "GJ01AB1234"),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize_plate(raw) == expected


def test_pretty_plate_round_trip():
    assert pretty_plate("MH12AB1234") == "MH 12 AB 1234"
    assert normalize_plate(pretty_plate("GJ01AB1234")) == "GJ01AB1234"


def test_classify():
    assert classify_plate("MH12AB1234") == PlateFormat.STANDARD
    assert classify_plate("21BH1234AA") == PlateFormat.BHARAT
    assert classify_plate("XX99ZZ0000") == PlateFormat.LOOSE  # not a real state code
    assert classify_plate("!!") == PlateFormat.INVALID


def test_valid_plate_strictness():
    assert is_valid_plate("MH12AB1234", strict=True)
    # A well-formed pattern with an unknown state code is loose, not strict.
    assert is_valid_plate("XX12AB1234") and not is_valid_plate("XX12AB1234", strict=True)


def test_repair_fixes_positional_ocr_confusions():
    # O read for 0 in the RTO digits, and S read for 5 in the number block.
    repair = repair_plate("MHI2AB1S34")
    assert repair.changed
    assert repair.plate == "MH12AB1534"
    assert repair.format == PlateFormat.STANDARD
    assert repair.edits


def test_repair_leaves_a_valid_plate_alone():
    repair = repair_plate("GJ01AB1234")
    assert not repair.changed
    assert repair.plate == "GJ01AB1234"


def test_similarity_is_confusion_aware():
    # 0/O and 1/I are the classic OCR confusions and must score very close.
    assert plate_similarity("MH12AB1234", "MHI2AB1234") > 0.9
    assert plate_similarity("MH12AB1234", "KA05XY9999") < 0.5
    assert plate_similarity("MH12AB1234", "MH12AB1234") == 1.0


def test_best_match_snaps_onto_a_known_plate():
    known = ["GJ01AB1234", "MH12XY5678"]
    matched, score = best_match("GJ0IAB1234", known)
    assert matched == "GJ01AB1234"
    assert score >= 0.82


def test_best_match_refuses_a_distant_plate():
    matched, score = best_match("TN09QQ0001", ["GJ01AB1234"])
    assert matched is None
    assert score < 0.82
