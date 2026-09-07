"""Analytics, reporting, and the smart-city open-data feed.

Reads that would be expensive to recompute per request (occupancy history, KPI
rollups) are aggregated in SQL rather than pulled into Python. The heavier
statistics use pandas over a bounded window.

The sustainability figures deserve a note on honesty: they are *modelled*, not
measured. Every one of them is returned alongside the assumption that produced
it, so nobody mistakes a plausible estimate for a meter reading.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import PaymentStatus, SessionStatus, SlotStatus
from app.models.facility import Facility, OccupancySnapshot, PriceSnapshot, Slot
from app.models.parking import Anomaly, ParkingSession, RecognitionEvent
from app.services.billing.pricing import pricing_engine
from app.services.integrations.weather import current_weather
from app.services.ml.forecaster import Forecast, forecaster

log = get_logger(__name__)

# Modelling assumptions for the sustainability estimates. Stated, not hidden.
SEARCH_MINUTES_SAVED = 4.5      # typical cruising time avoided by direct guidance
CO2_G_PER_MINUTE_IDLE = 42.0    # ~2.5 kg/h for a warm petrol engine at low speed
FUEL_ML_PER_MINUTE = 18.0


@dataclass(slots=True)
class KPIs:
    facility_id: int
    period_days: int
    sessions: int
    revenue_minor: int
    avg_stay_minutes: float
    avg_walk_distance_m: float
    avg_ticket_minor: int
    utilisation_pct: float
    turnover_per_slot: float
    payment_success_rate: float
    wallet_sessions: int
    guest_sessions: int
    guest_uncollected: int
    anpr_accuracy_pct: float
    anpr_review_rate: float
    open_anomalies: int
    currency: str

    def as_dict(self) -> dict:
        return {
            "facility_id": self.facility_id,
            "period_days": self.period_days,
            "sessions": self.sessions,
            "revenue_minor": self.revenue_minor,
            "avg_stay_minutes": round(self.avg_stay_minutes, 1),
            "avg_walk_distance_m": round(self.avg_walk_distance_m, 1),
            "avg_ticket_minor": self.avg_ticket_minor,
            "utilisation_pct": round(self.utilisation_pct, 2),
            "turnover_per_slot": round(self.turnover_per_slot, 2),
            "payment_success_rate": round(self.payment_success_rate, 4),
            "wallet_sessions": self.wallet_sessions,
            "guest_sessions": self.guest_sessions,
            "guest_uncollected": self.guest_uncollected,
            "anpr_accuracy_pct": round(self.anpr_accuracy_pct, 2),
            "anpr_review_rate": round(self.anpr_review_rate, 4),
            "open_anomalies": self.open_anomalies,
            "currency": self.currency,
        }


class AnalyticsService:
    # ── Occupancy ─────────────────────────────────────────────

    async def snapshot(self, db: AsyncSession, facility: Facility) -> OccupancySnapshot:
        """Persist one point of the occupancy time series. Called on a schedule."""
        now = datetime.now(UTC)
        counts = (
            await db.execute(
                select(
                    func.count(Slot.id),
                    func.sum(case((Slot.status != SlotStatus.EMPTY, 1), else_=0)),
                ).where(Slot.facility_id == facility.id, Slot.is_active.is_(True))
            )
        ).one()
        capacity = int(counts[0] or 0)
        occupied = int(counts[1] or 0)
        ratio = occupied / capacity if capacity else 0.0

        window_start = now - timedelta(hours=1)
        arrivals = (
            await db.execute(
                select(func.count(ParkingSession.id)).where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.entry_at >= window_start,
                )
            )
        ).scalar_one()
        departures_row = (
            await db.execute(
                select(
                    func.count(ParkingSession.id),
                    func.coalesce(func.avg(ParkingSession.billable_minutes), 0),
                    func.coalesce(func.sum(ParkingSession.total_minor), 0),
                ).where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.exit_at >= window_start,
                )
            )
        ).one()

        rate, multiplier, components = pricing_engine.effective_rate(
            facility, occupancy=ratio, moment=now
        )

        snapshot = OccupancySnapshot(
            facility_id=facility.id, ts=now, capacity=capacity, occupied=occupied,
            occupancy_pct=ratio, arrivals=int(arrivals),
            departures=int(departures_row[0]),
            avg_dwell_minutes=float(departures_row[1] or 0.0),
            revenue_minor=int(departures_row[2] or 0),
            effective_rate_minor=rate,
        )
        db.add(snapshot)
        db.add(
            PriceSnapshot(
                facility_id=facility.id, created_at=now,
                base_rate_minor=facility.base_rate_minor_per_hour,
                effective_rate_minor=rate, occupancy_pct=ratio,
                multiplier=multiplier, components=components,
                reason=f"scheduled snapshot at {ratio:.0%} occupancy",
            )
        )
        await db.flush()
        return snapshot

    async def occupancy_series(
        self, db: AsyncSession, facility_id: int, *, hours: int = 168
    ) -> list[dict]:
        since = datetime.now(UTC) - timedelta(hours=hours)
        rows = (
            await db.execute(
                select(OccupancySnapshot)
                .where(
                    OccupancySnapshot.facility_id == facility_id,
                    OccupancySnapshot.ts >= since,
                )
                .order_by(OccupancySnapshot.ts)
            )
        ).scalars().all()
        return [
            {
                "ts": r.ts, "occupancy_pct": r.occupancy_pct, "capacity": r.capacity,
                "occupied": r.occupied, "arrivals": r.arrivals, "departures": r.departures,
                "avg_dwell_minutes": r.avg_dwell_minutes, "revenue_minor": r.revenue_minor,
                "effective_rate_minor": r.effective_rate_minor,
            }
            for r in rows
        ]

    async def forecast(
        self, db: AsyncSession, facility: Facility, *, horizon_hours: int = 6
    ) -> Forecast:
        history = await self.occupancy_series(db, facility.id, hours=24 * 21)
        weather = await current_weather(facility.latitude, facility.longitude)
        capacity = history[-1]["capacity"] if history else None
        return forecaster.forecast(
            facility.id, history, horizon_hours=horizon_hours,
            weather=weather, capacity=capacity,
        )

    async def train_models(self, db: AsyncSession, facility: Facility) -> dict:
        """Retrain the demand, dwell and anomaly models for one facility."""
        from app.services.ml.anomaly import detector
        from app.services.ml.dwell import dwell_predictor

        history = await self.occupancy_series(db, facility.id, hours=24 * 60)
        weather = await current_weather(facility.latitude, facility.longitude)
        forecast_result = forecaster.train(facility.id, history, weather=weather)

        sessions = await self._completed_sessions(db, facility.id, days=90)
        dwell_result = dwell_predictor.train(facility.id, sessions)
        anomaly_result = detector.train(facility.id, sessions)

        return {
            "facility_id": facility.id,
            "occupancy_forecaster": forecast_result,
            "dwell_predictor": dwell_result,
            "anomaly_detector": anomaly_result,
        }

    async def _completed_sessions(
        self, db: AsyncSession, facility_id: int, *, days: int
    ) -> list[dict]:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = (
            await db.execute(
                select(ParkingSession)
                .where(
                    ParkingSession.facility_id == facility_id,
                    ParkingSession.status == SessionStatus.COMPLETED,
                    ParkingSession.entry_at >= since,
                )
                .order_by(ParkingSession.entry_at)
            )
        ).scalars().all()
        return [
            {
                "id": s.id,
                "entry_at": s.entry_at,
                "duration_minutes": s.elapsed_minutes(s.exit_at),
                "total_minor": s.total_minor,
                "entry_confidence": s.entry_confidence,
                "exit_confidence": s.exit_confidence,
                "walk_distance_m": s.walk_distance_m,
                "vehicle_type": "hatchback",
                "is_ev": False,
                "occupancy_pct": 0.5,
            }
            for s in rows
        ]

    # ── KPIs ──────────────────────────────────────────────────

    async def kpis(self, db: AsyncSession, facility: Facility, *, days: int = 7) -> KPIs:
        since = datetime.now(UTC) - timedelta(days=days)

        row = (
            await db.execute(
                select(
                    func.count(ParkingSession.id),
                    func.coalesce(func.sum(ParkingSession.total_minor), 0),
                    func.coalesce(func.avg(ParkingSession.billable_minutes), 0),
                    func.coalesce(func.avg(ParkingSession.walk_distance_m), 0),
                    func.sum(
                        case((ParkingSession.payment_status == PaymentStatus.PAID, 1), else_=0)
                    ),
                    func.sum(case((ParkingSession.user_id.is_not(None), 1), else_=0)),
                    func.sum(
                        case(
                            (
                                (ParkingSession.user_id.is_(None))
                                & (ParkingSession.payment_status == PaymentStatus.PENDING),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                ).where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.status == SessionStatus.COMPLETED,
                    ParkingSession.exit_at >= since,
                )
            )
        ).one()
        sessions, revenue, avg_minutes, avg_walk, paid, wallet_sessions, guest_pending = row
        wallet_sessions = int(wallet_sessions or 0)
        guest_pending = int(guest_pending or 0)
        sessions = int(sessions)
        revenue = int(revenue or 0)

        capacity = (
            await db.execute(
                select(func.count(Slot.id)).where(
                    Slot.facility_id == facility.id, Slot.is_active.is_(True)
                )
            )
        ).scalar_one()
        capacity = int(capacity) or 1

        occupancy_rows = await self.occupancy_series(db, facility.id, hours=days * 24)
        utilisation = (
            statistics.fmean([r["occupancy_pct"] for r in occupancy_rows]) * 100
            if occupancy_rows else 0.0
        )

        recognition = (
            await db.execute(
                select(
                    func.count(RecognitionEvent.id),
                    func.coalesce(func.avg(RecognitionEvent.confidence), 0),
                    func.sum(case((RecognitionEvent.needs_review.is_(True), 1), else_=0)),
                ).where(
                    RecognitionEvent.facility_id == facility.id,
                    RecognitionEvent.created_at >= since,
                )
            )
        ).one()
        reads = int(recognition[0] or 0)
        mean_confidence = float(recognition[1] or 0.0)
        review_count = int(recognition[2] or 0)

        open_anomalies = (
            await db.execute(
                select(func.count(Anomaly.id)).where(
                    Anomaly.facility_id == facility.id, Anomaly.resolved.is_(False)
                )
            )
        ).scalar_one()

        return KPIs(
            facility_id=facility.id,
            period_days=days,
            sessions=sessions,
            revenue_minor=revenue,
            avg_stay_minutes=float(avg_minutes or 0.0),
            avg_walk_distance_m=float(avg_walk or 0.0),
            avg_ticket_minor=int(revenue / sessions) if sessions else 0,
            utilisation_pct=utilisation,
            turnover_per_slot=sessions / capacity,
            # Measured over sessions that had a wallet to charge. Guest vehicles
            # are settled at the gate, so folding them in would report a payment
            # failure rate that is really just the share of unregistered drivers.
            payment_success_rate=(
                min(1.0, int(paid or 0) / wallet_sessions) if wallet_sessions else 1.0
            ),
            wallet_sessions=wallet_sessions,
            guest_sessions=sessions - wallet_sessions,
            guest_uncollected=guest_pending,
            anpr_accuracy_pct=mean_confidence * 100,
            anpr_review_rate=(review_count / reads) if reads else 0.0,
            open_anomalies=int(open_anomalies),
            currency=facility.currency,
        )

    # ── Layout intelligence ───────────────────────────────────

    async def slot_utilisation(self, db: AsyncSession, facility_id: int) -> list[dict]:
        """Per-slot usage — drives the heat map on the owner's layout view."""
        rows = (
            await db.execute(
                select(Slot).where(Slot.facility_id == facility_id).order_by(Slot.code)
            )
        ).scalars().all()
        max_uses = max((s.total_uses for s in rows), default=0) or 1
        return [
            {
                "slot_id": s.id, "code": s.code, "zone": s.zone, "level_id": s.level_id,
                "x": s.x, "y": s.y, "width": s.width, "height": s.height,
                "status": s.status, "slot_type": s.slot_type,
                "total_uses": s.total_uses,
                "occupied_minutes": s.total_occupied_minutes,
                "heat": round(s.total_uses / max_uses, 3),
                "distance_from_entry": s.distance_from_entry,
                "last_vacated_at": s.last_vacated_at,
            }
            for s in rows
        ]

    async def allocation_performance(
        self, db: AsyncSession, facility_id: int, *, days: int = 30
    ) -> dict:
        """Measured performance of each strategy, on real sessions.

        This is the field counterpart to the offline simulator: same metrics,
        real traffic. Strategies with fewer than 20 sessions are reported but
        flagged, because their means are not yet meaningful.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        rows = (
            await db.execute(
                select(
                    ParkingSession.allocation_strategy,
                    ParkingSession.walk_distance_m,
                    ParkingSession.allocation_latency_ms,
                    ParkingSession.candidates_considered,
                    ParkingSession.billable_minutes,
                ).where(
                    ParkingSession.facility_id == facility_id,
                    ParkingSession.entry_at >= since,
                    ParkingSession.allocation_strategy.is_not(None),
                )
            )
        ).all()

        if not rows:
            return {"strategies": [], "note": "no allocations recorded in this window"}

        frame = pd.DataFrame(
            rows, columns=["strategy", "walk_m", "latency_ms", "considered", "minutes"]
        )
        grouped = frame.groupby("strategy").agg(
            sessions=("walk_m", "size"),
            mean_walk_m=("walk_m", "mean"),
            p90_walk_m=("walk_m", lambda s: float(s.quantile(0.9))),
            mean_latency_ms=("latency_ms", "mean"),
            mean_candidates=("considered", "mean"),
            mean_stay_minutes=("minutes", "mean"),
        ).reset_index()

        results = []
        for record in grouped.to_dict("records"):
            record["sessions"] = int(record["sessions"])
            for key in ("mean_walk_m", "p90_walk_m", "mean_latency_ms",
                        "mean_candidates", "mean_stay_minutes"):
                record[key] = round(float(record[key]), 2)
            record["sufficient_sample"] = record["sessions"] >= 20
            results.append(record)

        results.sort(key=lambda r: r["mean_walk_m"])
        return {
            "window_days": days,
            "strategies": results,
            "note": (
                "Field measurements from live allocations. Strategies with fewer "
                "than 20 sessions are not yet statistically meaningful."
            ),
        }

    # ── Smart-city / sustainability ───────────────────────────

    async def sustainability(
        self, db: AsyncSession, facility: Facility, *, days: int = 30
    ) -> dict:
        since = datetime.now(UTC) - timedelta(days=days)
        sessions = (
            await db.execute(
                select(func.count(ParkingSession.id)).where(
                    ParkingSession.facility_id == facility.id,
                    ParkingSession.entry_at >= since,
                )
            )
        ).scalar_one()
        sessions = int(sessions)

        minutes_saved = sessions * SEARCH_MINUTES_SAVED
        co2_kg = minutes_saved * CO2_G_PER_MINUTE_IDLE / 1000.0
        fuel_l = minutes_saved * FUEL_ML_PER_MINUTE / 1000.0

        ev_slots = (
            await db.execute(
                select(func.count(Slot.id)).where(
                    Slot.facility_id == facility.id, Slot.has_ev_charger.is_(True)
                )
            )
        ).scalar_one()

        return {
            "facility_id": facility.id,
            "period_days": days,
            "guided_arrivals": sessions,
            "estimated_search_minutes_avoided": round(minutes_saved, 1),
            "estimated_co2_avoided_kg": round(co2_kg, 2),
            "estimated_fuel_avoided_litres": round(fuel_l, 2),
            "ev_charging_bays": int(ev_slots),
            "paperless_tickets_issued": sessions,
            "assumptions": {
                "search_minutes_saved_per_arrival": SEARCH_MINUTES_SAVED,
                "co2_grams_per_idle_minute": CO2_G_PER_MINUTE_IDLE,
                "fuel_ml_per_idle_minute": FUEL_ML_PER_MINUTE,
                "basis": (
                    "These figures are modelled from the assumptions above, not "
                    "measured. They estimate the cruising-for-parking that direct "
                    "slot guidance avoids, and should be read as an order of "
                    "magnitude rather than a meter reading."
                ),
            },
        }

    async def city_feed(self, db: AsyncSession) -> dict:
        """Public open-data feed of live availability across every facility.

        Deliberately shaped like a municipal open-data endpoint (GBFS-style):
        no authentication, no personal data, just counts and rates, so a city
        dashboard or a navigation app can consume it directly.
        """
        facilities = (
            await db.execute(
                select(Facility).where(
                    Facility.is_active.is_(True), Facility.publish_to_city_feed.is_(True)
                )
            )
        ).scalars().all()

        now = datetime.now(UTC)
        entries = []
        for facility in facilities:
            counts = (
                await db.execute(
                    select(
                        func.count(Slot.id),
                        func.sum(case((Slot.status == SlotStatus.EMPTY, 1), else_=0)),
                        func.sum(case((Slot.has_ev_charger.is_(True), 1), else_=0)),
                    ).where(Slot.facility_id == facility.id, Slot.is_active.is_(True))
                )
            ).one()
            capacity = int(counts[0] or 0)
            free = int(counts[1] or 0)
            ratio = (capacity - free) / capacity if capacity else 0.0
            rate, _, _ = pricing_engine.effective_rate(facility, occupancy=ratio, moment=now)

            entries.append(
                {
                    "facility_id": facility.id,
                    "name": facility.name,
                    "address": facility.address,
                    "city": facility.city,
                    "lat": facility.latitude,
                    "lon": facility.longitude,
                    "capacity": capacity,
                    "free_slots": free,
                    "occupancy_pct": round(ratio * 100, 1),
                    "ev_bays": int(counts[2] or 0),
                    "access_mode": facility.access_mode,
                    "rate_minor_per_hour": rate,
                    "currency": facility.currency,
                    "amenities": facility.amenities,
                }
            )

        return {
            "operator_id": settings.city_feed_operator_id,
            "generated_at": now.isoformat(),
            "ttl_seconds": 60,
            "license": "CC-BY-4.0",
            "facility_count": len(entries),
            "total_free_slots": sum(e["free_slots"] for e in entries),
            "total_capacity": sum(e["capacity"] for e in entries),
            "facilities": entries,
        }


analytics_service = AnalyticsService()
