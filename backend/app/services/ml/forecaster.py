"""Occupancy demand forecasting.

A gradient-boosted regressor predicts occupancy one to several hours ahead from
lagged occupancy, cyclical time features and weather. Two design choices matter:

  Honest fallback.  With fewer than ~120 observations the model is not trained
  at all; a seasonal-naive baseline (same hour, same weekday, recent average)
  serves instead, and the response says which produced the number. A forecast
  that hides its own unreliability is worse than no forecast.

  Time-ordered validation.  The holdout is the *tail* of the series, never a
  random split. Random splits leak the future into training through the lag
  features and produce flattering, meaningless scores.

Forecasts drive pre-emptive dynamic pricing, staffing suggestions in the owner
console, and the "lot will be full by 7pm" nudge in the driver app.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import joblib
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score

from app.core.config import settings
from app.core.logging import get_logger
from app.services.ml.features import (
    OCCUPANCY_FEATURES,
    build_occupancy_frame,
    occupancy_inference_row,
)

log = get_logger(__name__)

MIN_TRAINING_ROWS = 120
HOLDOUT_FRACTION = 0.2


@dataclass(slots=True)
class Forecast:
    facility_id: int
    horizon_hours: int
    points: list[dict] = field(default_factory=list)
    model: str = "seasonal_naive"
    mae: float | None = None
    r2: float | None = None
    trained_rows: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "facility_id": self.facility_id,
            "horizon_hours": self.horizon_hours,
            "model": self.model,
            "mae": round(self.mae, 4) if self.mae is not None else None,
            "r2": round(self.r2, 4) if self.r2 is not None else None,
            "trained_rows": self.trained_rows,
            "note": self.note,
            "points": self.points,
        }


class OccupancyForecaster:
    def __init__(self) -> None:
        self._models: dict[int, HistGradientBoostingRegressor] = {}
        self._metrics: dict[int, dict] = {}

    def _path(self, facility_id: int) -> Path:
        return settings.model_dir / f"occupancy_forecaster_{facility_id}.joblib"

    def _metrics_path(self, facility_id: int) -> Path:
        return settings.model_dir / f"occupancy_forecaster_{facility_id}.metrics.json"

    # ── Training ──────────────────────────────────────────────

    def train(self, facility_id: int, records: list[dict], *, weather: dict | None = None) -> dict:
        frame = build_occupancy_frame(records, weather=weather)
        if len(frame) < MIN_TRAINING_ROWS:
            return {
                "trained": False,
                "rows": len(frame),
                "reason": (
                    f"need at least {MIN_TRAINING_ROWS} usable observations, have {len(frame)}"
                ),
            }

        X = frame[OCCUPANCY_FEATURES].to_numpy(dtype=float)
        y = frame["target"].to_numpy(dtype=float)

        # Chronological holdout — the last slice of time, never a random sample.
        split = int(len(frame) * (1 - HOLDOUT_FRACTION))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        model = HistGradientBoostingRegressor(
            max_iter=300, learning_rate=0.06, max_depth=6,
            min_samples_leaf=12, l2_regularization=0.1, random_state=42,
        )
        model.fit(X_train, y_train)

        predictions = model.predict(X_test)
        mae = float(mean_absolute_error(y_test, predictions))
        r2 = float(r2_score(y_test, predictions)) if len(set(y_test)) > 1 else 0.0

        # Persistence baseline: predict "same as an hour ago". A model that
        # cannot beat it has learned nothing worth deploying.
        baseline_mae = float(mean_absolute_error(y_test, X_test[:, OCCUPANCY_FEATURES.index("lag_1")]))

        self._models[facility_id] = model
        self._metrics[facility_id] = {
            "mae": mae, "r2": r2, "rows": len(frame),
            "baseline_mae": baseline_mae,
            "beats_baseline": mae < baseline_mae,
            "trained_at": datetime.now(UTC).isoformat(),
        }
        try:
            joblib.dump(model, self._path(facility_id))
            # Metrics live beside the model, not only in memory: a restarted
            # process that reports a forecast with no accuracy figures is worse
            # than useless, because the reader assumes it was never measured.
            self._metrics_path(facility_id).write_text(
                json.dumps(self._metrics[facility_id], indent=2)
            )
        except Exception as exc:
            log.warning("could not persist forecaster", extra={"error": str(exc)})

        log.info(
            "forecaster trained",
            extra={"facility_id": facility_id, "mae": round(mae, 4), "r2": round(r2, 4)},
        )
        return {"trained": True, **self._metrics[facility_id]}

    def _model(self, facility_id: int) -> HistGradientBoostingRegressor | None:
        if facility_id in self._models:
            return self._models[facility_id]
        path = self._path(facility_id)
        if path.exists():
            try:
                self._models[facility_id] = joblib.load(path)
                metrics_path = self._metrics_path(facility_id)
                if metrics_path.exists():
                    self._metrics[facility_id] = json.loads(metrics_path.read_text())
                return self._models[facility_id]
            except Exception as exc:
                log.warning("could not load forecaster", extra={"error": str(exc)})
        return None

    # ── Inference ─────────────────────────────────────────────

    def forecast(
        self,
        facility_id: int,
        history: list[dict],
        *,
        horizon_hours: int = 6,
        weather: dict | None = None,
        capacity: int | None = None,
    ) -> Forecast:
        series = [float(r["occupancy_pct"]) for r in history]
        start = datetime.now(UTC)

        model = self._model(facility_id)   # also restores metrics from disk
        metrics = self._metrics.get(facility_id, {})

        if model is None or len(series) < 24:
            return self._seasonal_forecast(
                facility_id, history, horizon_hours=horizon_hours, capacity=capacity,
                note=(
                    "not enough history for the learned model — using a "
                    "seasonal-naive baseline"
                ),
            )

        points: list[dict] = []
        rolling = list(series)
        for step in range(1, horizon_hours + 1):
            moment = start + timedelta(hours=step)
            row = occupancy_inference_row(rolling, moment, weather)
            if row is None:
                break
            value = float(model.predict(row)[0])
            value = max(0.0, min(1.0, value))
            rolling.append(value)
            points.append(
                {
                    "ts": moment.isoformat(),
                    "hour": moment.hour,
                    "occupancy_pct": round(value, 4),
                    "occupied_estimate": int(round(value * capacity)) if capacity else None,
                    # Uncertainty widens with the horizon; a 6-hour-out number is
                    # not as trustworthy as a 1-hour-out one and must not look it.
                    "confidence": round(max(0.35, 0.92 - 0.08 * step), 3),
                }
            )

        return Forecast(
            facility_id=facility_id,
            horizon_hours=horizon_hours,
            points=points,
            model="hist_gradient_boosting",
            mae=metrics.get("mae"),
            r2=metrics.get("r2"),
            trained_rows=metrics.get("rows", 0),
            note=(
                "learned model"
                + (
                    f", MAE {metrics['mae']:.3f} vs {metrics['baseline_mae']:.3f} for a "
                    f"persistence baseline"
                    if "mae" in metrics and "baseline_mae" in metrics
                    else ""
                )
                + ("" if metrics.get("beats_baseline", True) else " — DOES NOT beat the baseline")
            ),
        )

    def _seasonal_forecast(
        self,
        facility_id: int,
        history: list[dict],
        *,
        horizon_hours: int,
        capacity: int | None,
        note: str,
    ) -> Forecast:
        """Average occupancy at the same hour and weekday, blended with recency."""
        by_slot: dict[tuple[int, int], list[float]] = {}
        for record in history:
            ts = record["ts"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            by_slot.setdefault((ts.weekday(), ts.hour), []).append(float(record["occupancy_pct"]))

        recent = [float(r["occupancy_pct"]) for r in history[-6:]]
        current = statistics.fmean(recent) if recent else 0.3

        start = datetime.now(UTC)
        points = []
        for step in range(1, horizon_hours + 1):
            moment = start + timedelta(hours=step)
            seasonal = by_slot.get((moment.weekday(), moment.hour))
            if seasonal:
                # Blend toward the seasonal mean as the horizon grows.
                weight = min(1.0, step / 4.0)
                value = (1 - weight) * current + weight * statistics.fmean(seasonal)
            else:
                from app.services.allocation.simulator import DEMAND_CURVE

                value = current * 0.5 + 0.5 * DEMAND_CURVE[moment.hour] * 0.8
            value = max(0.0, min(1.0, value))
            points.append(
                {
                    "ts": moment.isoformat(),
                    "hour": moment.hour,
                    "occupancy_pct": round(value, 4),
                    "occupied_estimate": int(round(value * capacity)) if capacity else None,
                    "confidence": round(max(0.25, 0.7 - 0.06 * step), 3),
                }
            )

        return Forecast(
            facility_id=facility_id, horizon_hours=horizon_hours, points=points,
            model="seasonal_naive", trained_rows=len(history), note=note,
        )

    def status(self, facility_id: int) -> dict:
        return {
            "facility_id": facility_id,
            "model_loaded": self._model(facility_id) is not None,
            "metrics": self._metrics.get(facility_id, {}),
            "min_training_rows": MIN_TRAINING_ROWS,
        }


forecaster = OccupancyForecaster()
