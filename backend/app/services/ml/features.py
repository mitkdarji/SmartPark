"""Feature construction shared by training and inference.

Training and serving must build features identically or the model silently
degrades in production. Both paths call the functions here — that is the whole
point of keeping them in one module.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

OCCUPANCY_FEATURES = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend",
    "lag_1", "lag_2", "lag_3", "lag_24", "roll_mean_3", "roll_std_3",
    "is_rain", "temp_c",
]

DWELL_FEATURES = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend",
    "occupancy_pct", "is_ev", "vehicle_type_code", "is_rain",
]

VEHICLE_TYPE_CODES = {
    "hatchback": 0, "sedan": 1, "suv": 2, "two_wheeler": 3, "ev": 4, "truck": 5,
}


def cyclical(value: float, period: float) -> tuple[float, float]:
    """Encode a wrap-around quantity so 23:00 and 00:00 sit next to each other."""
    angle = 2 * math.pi * value / period
    return math.sin(angle), math.cos(angle)


def time_features(moment: datetime) -> dict[str, float]:
    hour_sin, hour_cos = cyclical(moment.hour + moment.minute / 60.0, 24)
    dow_sin, dow_cos = cyclical(moment.weekday(), 7)
    return {
        "hour_sin": hour_sin, "hour_cos": hour_cos,
        "dow_sin": dow_sin, "dow_cos": dow_cos,
        "is_weekend": 1.0 if moment.weekday() >= 5 else 0.0,
    }


def build_occupancy_frame(
    records: list[dict], *, weather: dict | None = None
) -> pd.DataFrame:
    """Turn an ordered occupancy time series into a supervised learning table.

    Rows whose lags are not yet available are dropped — never imputed. A model
    trained on invented history learns invented dynamics.
    """
    if not records:
        return pd.DataFrame(columns=[*OCCUPANCY_FEATURES, "target"])

    frame = pd.DataFrame(records).sort_values("ts").reset_index(drop=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)

    times = frame["ts"].dt
    hour_of_day = times.hour + times.minute / 60.0
    frame["hour_sin"] = np.sin(2 * np.pi * hour_of_day / 24)
    frame["hour_cos"] = np.cos(2 * np.pi * hour_of_day / 24)
    frame["dow_sin"] = np.sin(2 * np.pi * times.dayofweek / 7)
    frame["dow_cos"] = np.cos(2 * np.pi * times.dayofweek / 7)
    frame["is_weekend"] = (times.dayofweek >= 5).astype(float)

    occupancy = frame["occupancy_pct"].astype(float)
    for lag in (1, 2, 3, 24):
        frame[f"lag_{lag}"] = occupancy.shift(lag)
    frame["roll_mean_3"] = occupancy.shift(1).rolling(3).mean()
    frame["roll_std_3"] = occupancy.shift(1).rolling(3).std()

    weather = weather or {}
    frame["is_rain"] = float(bool(weather.get("is_rain", False)))
    frame["temp_c"] = float(weather.get("temp_c") or 28.0)

    frame["target"] = occupancy
    return frame.dropna(subset=[*OCCUPANCY_FEATURES, "target"]).reset_index(drop=True)


def occupancy_inference_row(
    history: list[float], moment: datetime, weather: dict | None = None
) -> np.ndarray | None:
    """Build the single feature row for a one-step-ahead forecast.

    `history` is the recent occupancy series, oldest first. Returns None when
    there is not enough history to fill the lags — the caller then falls back to
    the seasonal baseline instead of predicting from padding.
    """
    if len(history) < 24:
        return None

    weather = weather or {}
    base = time_features(moment)
    recent = history[-3:]
    row = {
        **base,
        "lag_1": history[-1],
        "lag_2": history[-2],
        "lag_3": history[-3],
        "lag_24": history[-24],
        "roll_mean_3": float(np.mean(recent)),
        "roll_std_3": float(np.std(recent)),
        "is_rain": float(bool(weather.get("is_rain", False))),
        "temp_c": float(weather.get("temp_c") or 28.0),
    }
    return np.array([[row[name] for name in OCCUPANCY_FEATURES]], dtype=float)


def dwell_row(
    moment: datetime,
    *,
    occupancy_pct: float,
    vehicle_type: str,
    is_ev: bool,
    weather: dict | None = None,
) -> np.ndarray:
    weather = weather or {}
    base = time_features(moment)
    row = {
        **base,
        "occupancy_pct": float(occupancy_pct),
        "is_ev": 1.0 if is_ev else 0.0,
        "vehicle_type_code": float(VEHICLE_TYPE_CODES.get(vehicle_type, 0)),
        "is_rain": float(bool(weather.get("is_rain", False))),
    }
    return np.array([[row[name] for name in DWELL_FEATURES]], dtype=float)
