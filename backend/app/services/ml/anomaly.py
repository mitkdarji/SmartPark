"""Anomaly and fraud detection over parking sessions.

Two complementary layers, because each catches what the other misses:

  Rules      encode what we already know is wrong — an exit with no matching
             entry, a dwell of eleven seconds, a plate that is somehow parked
             in two facilities at once, a low-confidence read that still opened
             a barrier. Precise, explainable, immediately actionable.

  Model      an IsolationForest over session features catches the patterns
             nobody wrote a rule for. It is unsupervised, because labelled
             parking fraud does not exist at project scale, and its output is
             advisory: it raises a review flag, it never blocks a barrier.

Every finding carries a severity and a human-readable summary, because an alert
an operator cannot act on is noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
from sklearn.ensemble import IsolationForest

from app.core.logging import get_logger
from app.models.enums import AnomalyKind, Severity

log = get_logger(__name__)

MIN_TRAINING_SESSIONS = 60
IMPOSSIBLE_DWELL_SECONDS = 45
TAILGATE_WINDOW_SECONDS = 8


@dataclass(slots=True)
class Finding:
    kind: str
    severity: str
    score: float
    summary: str
    detail: dict = field(default_factory=dict)
    session_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "severity": self.severity, "score": round(self.score, 4),
            "summary": self.summary, "detail": self.detail, "session_id": self.session_id,
        }


class AnomalyDetector:
    def __init__(self) -> None:
        self._models: dict[int, IsolationForest] = {}
        self._stats: dict[int, dict] = {}

    # ── Rule layer ────────────────────────────────────────────

    def check_entry(
        self,
        *,
        plate: str,
        confidence: float,
        min_confidence: float,
        recent_entries: list[dict],
        open_sessions_for_plate: int,
        authorised: bool | None = None,
    ) -> list[Finding]:
        findings: list[Finding] = []
        now = datetime.now(UTC)

        if confidence < min_confidence:
            findings.append(
                Finding(
                    kind=AnomalyKind.LOW_CONFIDENCE_READ,
                    severity=Severity.MEDIUM,
                    score=1.0 - confidence,
                    summary=(
                        f"Plate {plate or '(unread)'} recognised at only "
                        f"{confidence:.0%} confidence — below the {min_confidence:.0%} threshold."
                    ),
                    detail={"plate": plate, "confidence": confidence},
                )
            )

        if open_sessions_for_plate > 0:
            findings.append(
                Finding(
                    kind=AnomalyKind.DUPLICATE_PLATE,
                    severity=Severity.HIGH,
                    score=0.9,
                    summary=(
                        f"Plate {plate} already has an open session. Either the exit was "
                        f"missed or this plate is cloned."
                    ),
                    detail={"plate": plate, "open_sessions": open_sessions_for_plate},
                )
            )

        # Two entries within a few seconds is the classic tailgating signature:
        # one barrier lift, two vehicles.
        for entry in recent_entries:
            entered_at = entry.get("entry_at")
            if isinstance(entered_at, str):
                entered_at = datetime.fromisoformat(entered_at)
            if entered_at is None:
                continue
            gap = abs((now - entered_at).total_seconds())
            if gap <= TAILGATE_WINDOW_SECONDS and entry.get("plate_normalized") != plate:
                findings.append(
                    Finding(
                        kind=AnomalyKind.TAILGATING,
                        severity=Severity.MEDIUM,
                        score=0.75,
                        summary=(
                            f"Two vehicles entered {gap:.1f} s apart — possible tailgating "
                            f"behind {entry.get('plate_normalized')}."
                        ),
                        detail={"gap_seconds": round(gap, 2), "preceding": entry.get("plate_normalized")},
                    )
                )
                break

        if authorised is False:
            findings.append(
                Finding(
                    kind=AnomalyKind.UNAUTHORISED_ENTRY,
                    severity=Severity.HIGH,
                    score=0.95,
                    summary=f"Plate {plate} is not on the authorised list for this facility.",
                    detail={"plate": plate},
                )
            )
        return findings

    def check_exit(
        self,
        *,
        session_id: int,
        plate: str,
        dwell_seconds: float,
        entry_confidence: float,
        exit_confidence: float,
        amount_minor: int,
        overstay_hours: int,
    ) -> list[Finding]:
        findings: list[Finding] = []

        if dwell_seconds < IMPOSSIBLE_DWELL_SECONDS:
            findings.append(
                Finding(
                    kind=AnomalyKind.IMPOSSIBLE_DWELL,
                    severity=Severity.MEDIUM,
                    score=0.8,
                    summary=(
                        f"Session lasted only {dwell_seconds:.0f} s — likely a duplicate scan "
                        f"or a vehicle that turned around at the barrier."
                    ),
                    detail={"dwell_seconds": round(dwell_seconds, 1)},
                    session_id=session_id,
                )
            )

        if dwell_seconds > overstay_hours * 3600:
            findings.append(
                Finding(
                    kind=AnomalyKind.OVERSTAY,
                    severity=Severity.LOW,
                    score=min(1.0, dwell_seconds / (overstay_hours * 3600) - 1.0),
                    summary=(
                        f"Vehicle {plate} stayed {dwell_seconds / 3600:.1f} h, beyond the "
                        f"{overstay_hours} h overstay threshold."
                    ),
                    detail={"dwell_hours": round(dwell_seconds / 3600, 2)},
                    session_id=session_id,
                )
            )

        # A confident entry paired with a shaky exit read is the pattern behind
        # plate-swap fraud, so it is worth a human look.
        if entry_confidence > 0.85 and exit_confidence < 0.6:
            findings.append(
                Finding(
                    kind=AnomalyKind.PLATE_MISMATCH,
                    severity=Severity.MEDIUM,
                    score=entry_confidence - exit_confidence,
                    summary=(
                        f"Entry read {entry_confidence:.0%} confident but exit only "
                        f"{exit_confidence:.0%} — verify the vehicle matches."
                    ),
                    detail={
                        "entry_confidence": entry_confidence,
                        "exit_confidence": exit_confidence,
                    },
                    session_id=session_id,
                )
            )
        return findings

    # ── Model layer ───────────────────────────────────────────

    @staticmethod
    def _vector(session: dict) -> list[float]:
        entry_at = session.get("entry_at")
        if isinstance(entry_at, str):
            entry_at = datetime.fromisoformat(entry_at)
        hour = entry_at.hour if entry_at else 12
        return [
            float(session.get("duration_minutes") or 0.0),
            float(session.get("total_minor") or 0) / 100.0,
            float(session.get("entry_confidence") or 0.0),
            float(session.get("exit_confidence") or 0.0),
            float(hour),
            float(session.get("walk_distance_m") or 0.0),
        ]

    def train(self, facility_id: int, sessions: list[dict]) -> dict:
        if len(sessions) < MIN_TRAINING_SESSIONS:
            return {
                "trained": False,
                "sessions": len(sessions),
                "reason": f"need at least {MIN_TRAINING_SESSIONS} completed sessions",
            }
        matrix = np.array([self._vector(s) for s in sessions], dtype=float)
        model = IsolationForest(
            n_estimators=200, contamination=0.03, random_state=42, n_jobs=1
        )
        model.fit(matrix)
        self._models[facility_id] = model
        self._stats[facility_id] = {
            "sessions": len(sessions),
            "trained_at": datetime.now(UTC).isoformat(),
            "feature_means": [round(float(v), 3) for v in matrix.mean(axis=0)],
        }
        log.info("anomaly model trained", extra={"facility_id": facility_id, "n": len(sessions)})
        return {"trained": True, **self._stats[facility_id]}

    def score(self, facility_id: int, session: dict) -> Finding | None:
        model = self._models.get(facility_id)
        if model is None:
            return None
        vector = np.array([self._vector(session)], dtype=float)
        raw = float(model.decision_function(vector)[0])
        is_outlier = bool(model.predict(vector)[0] == -1)
        if not is_outlier:
            return None
        # decision_function is negative for outliers; map to a 0-1 severity.
        score = min(1.0, max(0.0, -raw * 2))
        return Finding(
            kind="model_outlier",
            severity=Severity.MEDIUM if score < 0.6 else Severity.HIGH,
            score=score,
            summary=(
                "Session is statistically unusual for this facility "
                f"(outlier score {score:.2f}) — flagged for review."
            ),
            detail={"raw_score": round(raw, 4), "features": self._vector(session)},
            session_id=session.get("id"),
        )

    def status(self, facility_id: int) -> dict:
        return {
            "facility_id": facility_id,
            "model_loaded": facility_id in self._models,
            "stats": self._stats.get(facility_id, {}),
            "min_training_sessions": MIN_TRAINING_SESSIONS,
        }


detector = AnomalyDetector()
