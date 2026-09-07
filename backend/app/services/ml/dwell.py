"""Dwell-time prediction — how long an arriving vehicle will stay.

Used for three things: telling a driver what their stay will probably cost,
letting the owner console project when bays will free up, and giving the
allocator a hint about which arrivals deserve a near bay (a quick errand should
not take the best slot for six hours).

Falls back to a per-hour empirical median when there is not enough data. As with
the forecaster, the response always says which produced the number.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

from app.core.logging import get_logger
from app.services.ml.features import VEHICLE_TYPE_CODES, dwell_row, time_features

log = get_logger(__name__)

MIN_TRAINING_SESSIONS = 80


class DwellPredictor:
    def __init__(self) -> None:
        self._models: dict[int, HistGradientBoostingRegressor] = {}
        self._fallback: dict[int, dict[int, float]] = {}
        self._metrics: dict[int, dict] = {}

    def train(self, facility_id: int, sessions: list[dict]) -> dict:
        usable = [s for s in sessions if (s.get("duration_minutes") or 0) > 1]
        by_hour: dict[int, list[float]] = {}
        for s in usable:
            entry_at = s.get("entry_at")
            if isinstance(entry_at, str):
                entry_at = datetime.fromisoformat(entry_at)
            if entry_at is None:
                continue
            by_hour.setdefault(entry_at.hour, []).append(float(s["duration_minutes"]))
        self._fallback[facility_id] = {
            hour: statistics.median(values) for hour, values in by_hour.items()
        }

        if len(usable) < MIN_TRAINING_SESSIONS:
            return {
                "trained": False, "sessions": len(usable),
                "reason": f"need at least {MIN_TRAINING_SESSIONS} completed sessions",
                "fallback_hours": len(self._fallback[facility_id]),
            }

        rows, targets = [], []
        for s in usable:
            entry_at = s.get("entry_at")
            if isinstance(entry_at, str):
                entry_at = datetime.fromisoformat(entry_at)
            features = time_features(entry_at)
            rows.append(
                [
                    features["hour_sin"], features["hour_cos"],
                    features["dow_sin"], features["dow_cos"], features["is_weekend"],
                    float(s.get("occupancy_pct") or 0.5),
                    1.0 if s.get("is_ev") else 0.0,
                    float(VEHICLE_TYPE_CODES.get(s.get("vehicle_type", "hatchback"), 0)),
                    float(bool(s.get("is_rain"))),
                ]
            )
            targets.append(float(s["duration_minutes"]))

        X = np.array(rows, dtype=float)
        y = np.array(targets, dtype=float)
        split = int(len(X) * 0.8)

        model = HistGradientBoostingRegressor(
            max_iter=250, learning_rate=0.07, max_depth=5, random_state=42
        )
        model.fit(X[:split], y[:split])
        mae = float(mean_absolute_error(y[split:], model.predict(X[split:]))) if len(X) - split > 2 else None

        self._models[facility_id] = model
        self._metrics[facility_id] = {
            "sessions": len(usable), "mae_minutes": mae,
            "median_dwell_minutes": float(statistics.median(y)),
            "trained_at": datetime.now(UTC).isoformat(),
        }
        log.info("dwell model trained", extra={"facility_id": facility_id, "mae": mae})
        return {"trained": True, **self._metrics[facility_id]}

    def predict(
        self,
        facility_id: int,
        *,
        moment: datetime | None = None,
        occupancy_pct: float = 0.5,
        vehicle_type: str = "hatchback",
        is_ev: bool = False,
        weather: dict | None = None,
    ) -> dict:
        moment = moment or datetime.now(UTC)
        model = self._models.get(facility_id)

        if model is not None:
            row = dwell_row(
                moment, occupancy_pct=occupancy_pct, vehicle_type=vehicle_type,
                is_ev=is_ev, weather=weather,
            )
            minutes = max(5.0, float(model.predict(row)[0]))
            metrics = self._metrics.get(facility_id, {})
            mae = metrics.get("mae_minutes")
            return {
                "minutes": round(minutes, 1),
                "model": "hist_gradient_boosting",
                "mae_minutes": round(mae, 1) if mae else None,
                "range_minutes": (
                    [round(max(5.0, minutes - mae), 1), round(minutes + mae, 1)] if mae else None
                ),
            }

        hourly = self._fallback.get(facility_id, {})
        minutes = hourly.get(moment.hour)
        if minutes is None:
            minutes = statistics.fmean(hourly.values()) if hourly else 95.0
        return {
            "minutes": round(float(minutes), 1),
            "model": "hourly_median" if hourly else "global_prior",
            "mae_minutes": None,
            "range_minutes": None,
        }


dwell_predictor = DwellPredictor()
