"""Image conditioning applied to a cropped plate before OCR."""

from __future__ import annotations

import cv2
import numpy as np

TARGET_HEIGHT = 96  # OCR engines are calibrated around ~32-100px glyph height


def to_gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def upscale(image: np.ndarray, target_height: int = TARGET_HEIGHT) -> np.ndarray:
    h, w = image.shape[:2]
    if h == 0 or w == 0:
        return image
    scale = target_height / h
    if 0.95 < scale < 1.05:
        return image
    interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(image, (max(1, int(w * scale)), target_height), interpolation=interp)


def enhance(image: np.ndarray) -> np.ndarray:
    """Contrast-normalise and denoise. Returns a grayscale image."""
    gray = to_gray(image)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    gray = cv2.fastNlMeansDenoising(gray, None, h=9, templateWindowSize=7, searchWindowSize=21)
    return cv2.filter2D(gray, -1, np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]]))


def binarize(image: np.ndarray) -> np.ndarray:
    """Otsu threshold, normalised so glyphs are always white on black."""
    gray = to_gray(image)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # Plates are dark-on-light; if the border dominates as white, invert.
    if binary.mean() > 127:
        binary = cv2.bitwise_not(binary)
    return binary


def variants(plate_roi: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Several conditioned views of the same crop.

    Running the ensemble over multiple preprocessings and voting is markedly
    more robust than betting on one pipeline: glare kills the thresholded view
    while motion blur kills the sharpened one, and they rarely fail together.
    """
    base = upscale(plate_roi)
    gray = to_gray(base)
    enhanced = enhance(base)
    return [
        ("raw", base),
        ("gray", gray),
        ("enhanced", enhanced),
        ("binary", binarize(enhanced)),
        ("adaptive", cv2.adaptiveThreshold(
            cv2.GaussianBlur(gray, (5, 5), 0), 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 9)),
    ]


def _strip_border(binary: np.ndarray) -> np.ndarray:
    """Remove the plate frame if it survived thresholding as one big blob.

    A rectangle spanning nearly the whole crop is the plate's own border, and
    with RETR_EXTERNAL it swallows every glyph inside it.
    """
    h, w = binary.shape[:2]
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw > 0.92 * w and ch > 0.90 * h:
            inset_x = max(3, int(0.02 * w))
            inset_y = max(3, int(0.06 * h))
            x0, y0 = x + inset_x, y + inset_y
            x1, y1 = x + cw - inset_x, y + ch - inset_y
            if x1 - x0 > 20 and y1 - y0 > 12:
                return binary[y0:y1, x0:x1]
    return binary


def _split_wide(box: tuple[int, int, int, int], unit_w: float) -> list[tuple[int, int, int, int]]:
    """Cut a blob that merged several touching glyphs into equal columns."""
    x, y, w, h = box
    if unit_w <= 0:
        return [box]
    parts = int(round(w / unit_w))
    if parts <= 1:
        return [box]
    parts = min(parts, 6)
    step = w / parts
    return [(int(x + i * step), y, int(step), h) for i in range(parts)]


def segment_characters(binary: np.ndarray, *, min_count: int = 4) -> list[np.ndarray]:
    """Split a binarised plate into individual glyph images, left to right.

    Rejects blobs that are too short, too wide, or off the text baseline (screws,
    the state emblem, the plate frame). Blobs that merged adjacent characters are
    split on the median glyph width rather than discarded.
    """
    if binary.size == 0:
        return []
    binary = _strip_border(binary)
    h, w = binary.shape[:2]
    if h < 12 or w < 24:
        return []

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    raw: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if ch < 0.33 * h or ch > 0.97 * h:
            continue
        if cw < 0.012 * w or cw > 0.34 * w:
            continue
        if cw / max(ch, 1) > 3.2:          # far too wide to be glyphs at all
            continue
        raw.append((x, y, cw, ch))

    if len(raw) < 2:
        return []

    # Median width of the clearly-single glyphs sets the splitting unit.
    singles = [b[2] for b in raw if b[2] / max(b[3], 1) <= 1.15]
    unit_w = float(np.median(singles)) if singles else float(np.median([b[2] for b in raw]))

    boxes: list[tuple[int, int, int, int]] = []
    for box in raw:
        if box[2] / max(box[3], 1) > 1.25:
            boxes.extend(_split_wide(box, unit_w))
        else:
            boxes.append(box)

    # Keep only blobs sitting on the common text baseline.
    centers = [b[1] + b[3] / 2 for b in boxes]
    median_center = float(np.median(centers))
    boxes = [b for b in boxes if abs(b[1] + b[3] / 2 - median_center) <= 0.22 * h]

    boxes.sort(key=lambda b: b[0])

    # Drop boxes fully contained in a previous one (holes inside 8, B, 0).
    merged: list[tuple[int, int, int, int]] = []
    for box in boxes:
        x, y, cw, ch = box
        if merged:
            px, py, pw, ph = merged[-1]
            if x >= px and x + cw <= px + pw:
                continue
        merged.append(box)

    if len(merged) < min_count:
        return []

    return [binary[y : y + ch, x : x + cw] for x, y, cw, ch in merged]
