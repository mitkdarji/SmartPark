"""Plate string normalisation, validation, and OCR error correction.

OCR on low-resolution plates confuses a small, well-known set of glyph pairs
(0/O, 1/I, 8/B, 5/S, 2/Z, 6/G). Indian plates have a rigid positional grammar —
`SS DD LL NNNN` — so we know at every character index whether a letter or a
digit belongs there. Combining the two turns most single-character OCR errors
into a deterministic repair rather than a failed read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Characters that OCR interchanges, keyed by what we might have read.
LETTER_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
                   "S": "5", "B": "8", "G": "6", "T": "7", "A": "4"}
DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G",
                   "7": "T", "4": "A"}

# Valid Indian state / UT registration prefixes.
STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ",
    "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP",
    "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UK",
    "UP", "WB",
}

# Standard format: two-letter state, one/two-digit RTO, one-to-three letters, four digits.
_IN_STANDARD = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$")
# Bharat series: two-digit year, "BH", four digits, two letters.
_IN_BHARAT = re.compile(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$")
# Older / military / vanity formats we accept but flag as non-standard.
_IN_LOOSE = re.compile(r"^[A-Z0-9]{6,11}$")

_STRIP = re.compile(r"[^A-Z0-9]")


class PlateFormat:
    STANDARD = "in_standard"
    BHARAT = "in_bharat"
    LOOSE = "loose"
    INVALID = "invalid"


def normalize_plate(raw: str) -> str:
    """Uppercase and strip everything that is not alphanumeric.

    `MH 12 AB-1234`, `mh12ab1234`, and `MH·12·AB·1234` all collapse to the same
    key, which is what every lookup in the system matches on.
    """
    return _STRIP.sub("", (raw or "").upper())


def pretty_plate(plate: str) -> str:
    """Render a normalised plate in the spaced form drivers recognise."""
    p = normalize_plate(plate)
    m = _IN_STANDARD.match(p)
    if m:
        return " ".join(m.groups())
    m = _IN_BHARAT.match(p)
    if m:
        return " ".join(m.groups())
    return p


def classify_plate(plate: str) -> str:
    p = normalize_plate(plate)
    if _IN_STANDARD.match(p) and p[:2] in STATE_CODES:
        return PlateFormat.STANDARD
    if _IN_BHARAT.match(p):
        return PlateFormat.BHARAT
    if _IN_LOOSE.match(p):
        return PlateFormat.LOOSE
    return PlateFormat.INVALID


def is_valid_plate(plate: str, *, strict: bool = False) -> bool:
    fmt = classify_plate(plate)
    if strict:
        return fmt in (PlateFormat.STANDARD, PlateFormat.BHARAT)
    return fmt != PlateFormat.INVALID


@dataclass(slots=True)
class Repair:
    plate: str
    changed: bool
    format: str
    edits: list[str]


def repair_plate(raw: str) -> Repair:
    """Apply positional glyph corrections to coax a read into a valid format.

    Returns the original string unchanged if no repair yields a valid plate, so
    the caller can decide whether to fall back to a fuzzy database match.
    """
    p = normalize_plate(raw)
    if not p:
        return Repair(p, False, PlateFormat.INVALID, [])

    fmt = classify_plate(p)
    if fmt in (PlateFormat.STANDARD, PlateFormat.BHARAT):
        return Repair(p, False, fmt, [])

    edits: list[str] = []

    # Standard Indian plates are 9-10 chars: LL DD LL(L) DDDD.
    if 9 <= len(p) <= 10:
        chars = list(p)
        # positions 0-1 must be letters
        for i in (0, 1):
            if chars[i].isdigit() and chars[i] in DIGIT_TO_LETTER:
                edits.append(f"{i}:{chars[i]}->{DIGIT_TO_LETTER[chars[i]]}")
                chars[i] = DIGIT_TO_LETTER[chars[i]]
        # positions 2-3 must be digits
        for i in (2, 3):
            if i < len(chars) and chars[i].isalpha() and chars[i] in LETTER_TO_DIGIT:
                edits.append(f"{i}:{chars[i]}->{LETTER_TO_DIGIT[chars[i]]}")
                chars[i] = LETTER_TO_DIGIT[chars[i]]
        # trailing four must be digits
        for i in range(len(chars) - 4, len(chars)):
            if chars[i].isalpha() and chars[i] in LETTER_TO_DIGIT:
                edits.append(f"{i}:{chars[i]}->{LETTER_TO_DIGIT[chars[i]]}")
                chars[i] = LETTER_TO_DIGIT[chars[i]]
        # the series block between RTO code and the number must be letters
        for i in range(4, len(chars) - 4):
            if chars[i].isdigit() and chars[i] in DIGIT_TO_LETTER:
                edits.append(f"{i}:{chars[i]}->{DIGIT_TO_LETTER[chars[i]]}")
                chars[i] = DIGIT_TO_LETTER[chars[i]]

        candidate = "".join(chars)
        cfmt = classify_plate(candidate)
        if cfmt in (PlateFormat.STANDARD, PlateFormat.BHARAT):
            return Repair(candidate, candidate != p, cfmt, edits)

    return Repair(p, False, classify_plate(p), [])


def plate_similarity(a: str, b: str) -> float:
    """Similarity in [0,1] that treats OCR-confusable glyphs as near-equal."""
    a, b = normalize_plate(a), normalize_plate(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def canon(s: str) -> str:
        # Collapse each confusion class to one representative glyph.
        table = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
                 "S": "5", "B": "8", "G": "6", "T": "7"}
        return "".join(table.get(ch, ch) for ch in s)

    exact = SequenceMatcher(None, a, b).ratio()
    fuzzy = SequenceMatcher(None, canon(a), canon(b)).ratio()
    # Weight the confusion-aware comparison higher; it is the OCR-realistic one.
    return round(0.4 * exact + 0.6 * fuzzy, 4)


def best_match(candidate: str, known: list[str], *, threshold: float = 0.82) -> tuple[str | None, float]:
    """Snap a noisy read onto the closest plate the facility already knows.

    Used at the exit gate, where a mis-read must not create a phantom vehicle:
    the set of plates currently parked inside is small, so a fuzzy match against
    it is both safe and highly effective.
    """
    best, score = None, 0.0
    for plate in known:
        s = plate_similarity(candidate, plate)
        if s > score:
            best, score = plate, s
    return (best, score) if score >= threshold else (None, score)
