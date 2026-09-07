"""Synthetic gate-camera image generator.

Two jobs:
  1. The demo and test suite need plate images without shipping a dataset of
     real, personally-identifiable vehicle photographs.
  2. The ANPR evaluation harness needs *labelled* images with controllable
     degradation, so accuracy can be reported as a function of blur, glare,
     skew and noise instead of as a single unfalsifiable number.
"""

from __future__ import annotations

import random

import cv2
import numpy as np

from app.services.anpr.plate_utils import STATE_CODES, normalize_plate

PLATE_W, PLATE_H = 440, 100
SERIES_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # visually unambiguous subset


def random_plate(rng: random.Random | None = None) -> str:
    rng = rng or random
    state = rng.choice(sorted(STATE_CODES))
    rto = f"{rng.randint(1, 99):02d}"
    series = "".join(rng.choice(SERIES_LETTERS) for _ in range(rng.choice([1, 2, 2, 2])))
    number = f"{rng.randint(1, 9999):04d}"
    return f"{state}{rto}{series}{number}"


def render_plate(plate: str, *, ev: bool = False) -> np.ndarray:
    """Draw a clean, front-on plate: white ground, black glyphs (green if EV)."""
    plate = normalize_plate(plate)
    bg = (24, 120, 40) if ev else (238, 240, 238)
    fg = (245, 245, 245) if ev else (18, 18, 20)

    img = np.full((PLATE_H, PLATE_W, 3), bg, dtype=np.uint8)
    cv2.rectangle(img, (4, 4), (PLATE_W - 5, PLATE_H - 5), fg, 3)

    scale, thickness = 2.4, 6
    (tw, th), _ = cv2.getTextSize(plate, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    while tw > PLATE_W - 40 and scale > 0.8:
        scale -= 0.1
        (tw, th), _ = cv2.getTextSize(plate, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)

    cv2.putText(img, plate, ((PLATE_W - tw) // 2, (PLATE_H + th) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, scale, fg, thickness, cv2.LINE_AA)
    return img


def _perspective(img: np.ndarray, strength: float, rng: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    jitter = strength * min(h, w) * 0.25
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[rng.uniform(0, jitter), rng.uniform(0, jitter)],
                      [w - rng.uniform(0, jitter), rng.uniform(0, jitter)],
                      [w - rng.uniform(0, jitter), h - rng.uniform(0, jitter)],
                      [rng.uniform(0, jitter), h - rng.uniform(0, jitter)]])
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _glare(img: np.ndarray, strength: float, rng: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    overlay = np.zeros((h, w), dtype=np.float32)
    cx, cy = rng.randint(0, w), rng.randint(0, h)
    radius = int(min(h, w) * rng.uniform(0.3, 0.8))
    cv2.circle(overlay, (cx, cy), radius, 1.0, -1)
    overlay = cv2.GaussianBlur(overlay, (0, 0), radius / 2.5)
    boost = (overlay[..., None] * 255 * strength).astype(np.int16)
    return np.clip(img.astype(np.int16) + boost, 0, 255).astype(np.uint8)


def synth_capture(
    plate: str | None = None,
    *,
    difficulty: float = 0.35,
    ev: bool = False,
    seed: int | None = None,
    width: int = 960,
    height: int = 540,
) -> tuple[np.ndarray, str]:
    """Render a full gate-camera frame containing one plate.

    `difficulty` in [0,1] scales blur, noise, skew, glare and plate size —
    0 is a studio-clean crop, 1 is a hard night-time read.
    Returns (BGR frame, ground-truth plate).
    """
    rng = random.Random(seed)
    plate = normalize_plate(plate) if plate else random_plate(rng)
    d = max(0.0, min(1.0, difficulty))

    # Vehicle-ish background: asphalt gradient plus a body-coloured slab.
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        shade = int(40 + 60 * (y / height))
        frame[y, :] = (shade, shade + 4, shade + 8)
    body_color = tuple(rng.randint(30, 200) for _ in range(3))
    cv2.rectangle(frame, (int(width * 0.08), int(height * 0.18)),
                  (int(width * 0.92), int(height * 0.95)), body_color, -1)
    cv2.rectangle(frame, (int(width * 0.18), int(height * 0.22)),
                  (int(width * 0.82), int(height * 0.45)),
                  tuple(max(0, c - 45) for c in body_color), -1)

    # Compose the plate onto the bumper.
    plate_img = render_plate(plate, ev=ev)
    target_w = int(width * (0.42 - 0.16 * d))
    target_h = int(target_w * PLATE_H / PLATE_W)
    plate_img = cv2.resize(plate_img, (target_w, target_h), interpolation=cv2.INTER_AREA)
    if d > 0.15:
        plate_img = _perspective(plate_img, d, rng)

    px = (width - target_w) // 2 + rng.randint(-int(20 * d), int(20 * d) + 1)
    py = int(height * 0.62) + rng.randint(-int(15 * d), int(15 * d) + 1)
    px = max(0, min(width - target_w, px))
    py = max(0, min(height - target_h, py))
    frame[py : py + target_h, px : px + target_w] = plate_img

    # Degradations, in the order a real camera applies them.
    if d > 0.05:
        k = int(1 + 2 * round(d * 3))
        frame = cv2.GaussianBlur(frame, (k, k), 0)
    if d > 0.4:
        frame = _glare(frame, (d - 0.4) * 0.9, rng)
    if d > 0.1:
        noise = np.random.default_rng(seed).normal(0, 14 * d, frame.shape)
        frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    if d > 0.3:
        brightness = 1.0 - 0.45 * (d - 0.3)
        frame = np.clip(frame.astype(np.float32) * brightness, 0, 255).astype(np.uint8)

    return frame, plate


def encode_png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("failed to encode image")
    return buf.tobytes()
