"""Classical computer-vision plate localisation.

A learned detector (YOLO) would score higher on unconstrained road imagery, but
it needs a GPU and a labelled dataset. Gate cameras are a *constrained* setting —
fixed mount, fixed distance, controlled lighting — where the morphological
pipeline below is fast (single-digit milliseconds on CPU), fully explainable,
and needs no training data.

Pipeline: blackhat morphology to isolate dark-on-light glyph strokes → Sobel
gradient magnitude → closing to merge glyphs into one blob → contour extraction
→ filter by aspect ratio, area, extent and glyph-count heuristics → rank.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# Indian plates: single-row ≈ 4.7:1, two-row ≈ 2:1. Accept a generous band.
MIN_ASPECT = 1.6
MAX_ASPECT = 7.0
MIN_AREA_RATIO = 0.0006   # plate must occupy at least this fraction of the frame
MAX_AREA_RATIO = 0.35


@dataclass(slots=True)
class PlateCandidate:
    """One proposed plate region, with the evidence behind its score."""

    x: int
    y: int
    w: int
    h: int
    score: float
    image: np.ndarray = field(repr=False)
    reasons: dict = field(default_factory=dict)

    @property
    def bbox(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]

    @property
    def aspect(self) -> float:
        return self.w / max(self.h, 1)


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]
    rect[3] = pts[np.argmax(d)]
    return rect


def deskew(image: np.ndarray) -> np.ndarray:
    """Flatten a plate photographed at an angle via perspective warp.

    Falls back to the input if no convincing quadrilateral is found — an
    over-eager warp is worse for OCR than a mildly skewed original.
    """
    if image.size == 0:
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(gray, 40, 160)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return image

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 0.25 * image.shape[0] * image.shape[1]:
        return image

    peri = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.02 * peri, True)
    if len(approx) != 4:
        return image

    rect = _order_corners(approx.reshape(4, 2).astype("float32"))
    (tl, tr, br, bl) = rect
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if width < 20 or height < 8:
        return image

    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, matrix, (width, height))


def _glyph_count(roi: np.ndarray) -> int:
    """Count plausible character blobs — a real plate has roughly 6-11."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    if gray.size == 0:
        return 0
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h = gray.shape[0]
    count = 0
    for c in contours:
        _, _, cw, ch = cv2.boundingRect(c)
        if 0.35 * h <= ch <= 0.95 * h and 0.08 * h <= cw <= 0.9 * h:
            count += 1
    return count


def detect_plates(image: np.ndarray, *, max_candidates: int = 5) -> list[PlateCandidate]:
    """Return ranked plate-region proposals, best first."""
    if image is None or image.size == 0:
        return []

    frame_h, frame_w = image.shape[:2]
    frame_area = float(frame_h * frame_w)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()

    # Normalise illumination so a sunlit plate and a shaded one look alike.
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    gray = cv2.bilateralFilter(gray, 11, 17, 17)

    # Blackhat highlights dark glyphs sitting on the bright plate background.
    rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 7))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, rect_kernel)

    grad_x = cv2.Sobel(blackhat, cv2.CV_32F, 1, 0, ksize=3)
    grad_x = np.absolute(grad_x)
    lo, hi = float(grad_x.min()), float(grad_x.max())
    grad_x = np.zeros_like(grad_x) if hi - lo < 1e-6 else (255 * (grad_x - lo) / (hi - lo))
    grad_x = grad_x.astype("uint8")

    grad_x = cv2.GaussianBlur(grad_x, (5, 5), 0)
    grad_x = cv2.morphologyEx(grad_x, cv2.MORPH_CLOSE, rect_kernel)
    _, thresh = cv2.threshold(grad_x, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    thresh = cv2.morphologyEx(
        thresh, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (21, 9))
    )
    thresh = cv2.dilate(thresh, None, iterations=2)
    thresh = cv2.erode(thresh, None, iterations=1)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[PlateCandidate] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 40 or h < 12:
            continue
        aspect = w / float(h)
        area_ratio = (w * h) / frame_area
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            continue
        if not (MIN_AREA_RATIO <= area_ratio <= MAX_AREA_RATIO):
            continue
        extent = cv2.contourArea(contour) / float(w * h)
        if extent < 0.35:
            continue

        pad_x, pad_y = int(w * 0.03), int(h * 0.12)
        x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
        x1, y1 = min(frame_w, x + w + pad_x), min(frame_h, y + h + pad_y)
        roi = image[y0:y1, x0:x1]
        if roi.size == 0:
            continue

        glyphs = _glyph_count(roi)

        # Score: prefer plate-like aspect, dense edges, and a believable glyph count.
        aspect_score = 1.0 - min(abs(aspect - 4.0) / 4.0, 1.0)
        glyph_score = 1.0 if 6 <= glyphs <= 12 else max(0.0, 1.0 - abs(glyphs - 9) / 9.0)
        size_score = min(area_ratio / 0.05, 1.0)
        score = 0.4 * aspect_score + 0.35 * glyph_score + 0.15 * extent + 0.10 * size_score

        candidates.append(
            PlateCandidate(
                x=x0, y=y0, w=x1 - x0, h=y1 - y0, score=round(float(score), 4), image=roi,
                reasons={
                    "aspect": round(aspect, 2), "extent": round(extent, 3),
                    "glyphs": glyphs, "area_ratio": round(area_ratio, 5),
                },
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        # Nothing tripped the morphology filter — hand the OCR ensemble the whole
        # frame rather than giving up. Gate cameras are often tightly framed.
        candidates = [
            PlateCandidate(
                x=0, y=0, w=frame_w, h=frame_h, score=0.15, image=image,
                reasons={"fallback": "full_frame"},
            )
        ]

    return candidates[:max_candidates]


def annotate(image: np.ndarray, candidate: PlateCandidate, label: str) -> np.ndarray:
    """Draw the accepted detection for the operator console / audit record."""
    out = image.copy()
    cv2.rectangle(out, (candidate.x, candidate.y),
                  (candidate.x + candidate.w, candidate.y + candidate.h), (0, 220, 90), 3)
    text_y = max(24, candidate.y - 10)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    cv2.rectangle(out, (candidate.x, text_y - th - 8),
                  (candidate.x + tw + 12, text_y + 6), (0, 220, 90), -1)
    cv2.putText(out, label, (candidate.x + 6, text_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 25, 15), 2, cv2.LINE_AA)
    return out
