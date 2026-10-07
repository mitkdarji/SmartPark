"""Analytics, forecasting, model training, and the evaluation harness."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession, OwnedFacility
from app.schemas.ai import BenchmarkRequest
from app.services.analytics import analytics_service

router = APIRouter(tags=["analytics"])


@router.get("/facilities/{facility_id}/analytics/kpis", response_model=dict)
async def kpis(
    facility: OwnedFacility,
    db: DbSession,
    days: int = Query(default=7, ge=1, le=365),
) -> dict:
    result = await analytics_service.kpis(db, facility, days=days)
    return result.as_dict()


@router.get("/facilities/{facility_id}/analytics/occupancy", response_model=dict)
async def occupancy_history(
    facility: OwnedFacility,
    db: DbSession,
    hours: int = Query(default=168, ge=1, le=24 * 90),
) -> dict:
    series = await analytics_service.occupancy_series(db, facility.id, hours=hours)
    return {"facility_id": facility.id, "hours": hours, "points": series}


@router.get("/facilities/{facility_id}/analytics/forecast", response_model=dict)
async def forecast(
    facility: OwnedFacility,
    db: DbSession,
    hours: int = Query(default=6, ge=1, le=48),
) -> dict:
    result = await analytics_service.forecast(db, facility, horizon_hours=hours)
    return result.as_dict()


@router.get("/facilities/{facility_id}/analytics/slots", response_model=dict)
async def slot_heatmap(facility: OwnedFacility, db: DbSession) -> dict:
    """Per-bay utilisation — the heat map over the layout."""
    return {
        "facility_id": facility.id,
        "slots": await analytics_service.slot_utilisation(db, facility.id),
    }


@router.get("/facilities/{facility_id}/analytics/allocation", response_model=dict)
async def allocation_performance(
    facility: OwnedFacility,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Measured strategy performance on this facility's real traffic."""
    return await analytics_service.allocation_performance(db, facility.id, days=days)


@router.get("/facilities/{facility_id}/analytics/sustainability", response_model=dict)
async def sustainability(
    facility: OwnedFacility,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    return await analytics_service.sustainability(db, facility, days=days)


@router.post("/facilities/{facility_id}/analytics/train", response_model=dict)
async def train_models(facility: OwnedFacility, db: DbSession) -> dict:
    """Refit the demand, dwell and anomaly models for this facility."""
    result = await analytics_service.train_models(db, facility)
    await db.commit()
    return result


@router.get("/facilities/{facility_id}/analytics/dwell", response_model=dict)
async def predict_dwell(
    facility: OwnedFacility,
    db: DbSession,
    vehicle_type: str = Query(default="hatchback"),
    is_ev: bool = Query(default=False),
) -> dict:
    from app.services.ml.dwell import dwell_predictor
    from app.services.parking import parking_service

    _, _, occupancy = await parking_service.occupancy(db, facility.id)
    return dwell_predictor.predict(
        facility.id, occupancy_pct=occupancy, vehicle_type=vehicle_type, is_ev=is_ev
    )


@router.post("/benchmark/allocation", response_model=dict, tags=["evaluation"])
async def benchmark_allocation(payload: BenchmarkRequest) -> dict:
    """Run the offline allocation-strategy comparison.

    Pure simulation — it touches no facility data, so it is safe to run at any
    time and reproducible from the request parameters alone.
    """
    from app.services.allocation.simulator import compare

    return compare(
        payload.strategies,
        trials=payload.trials,
        vehicles=payload.vehicles,
        zones=payload.zones,
        slots_per_zone=payload.slots_per_zone,
        hours=payload.hours,
        ghost_rate=payload.ghost_rate,
    )


@router.get("/benchmark/allocation/sweep", response_model=dict, tags=["evaluation"])
async def benchmark_sweep(
    trials: int = Query(default=3, ge=1, le=10),
    vehicles: int = Query(default=800, ge=100, le=3000),
) -> dict:
    """How each policy degrades as the occupancy map becomes less trustworthy."""
    from app.services.allocation.simulator import sweep_ghost_rates

    return sweep_ghost_rates(trials=trials, vehicles=vehicles)


@router.get("/benchmark/anpr", response_model=dict, tags=["evaluation"])
async def benchmark_anpr(
    samples: int = Query(default=20, ge=5, le=100),
    difficulty: float = Query(default=0.35, ge=0.0, le=1.0),
) -> dict:
    """Measure ANPR accuracy on freshly generated, labelled synthetic frames."""
    import time

    from app.services.anpr.pipeline import anpr
    from app.services.anpr.plate_utils import plate_similarity
    from app.services.anpr.synthetic import synth_capture

    exact = near = 0
    confidences: list[float] = []
    latencies: list[float] = []
    failures: list[dict] = []

    for seed in range(samples):
        frame, truth = synth_capture(difficulty=difficulty, seed=seed)
        started = time.perf_counter()
        result = anpr.recognize(frame, persist=False)
        latencies.append((time.perf_counter() - started) * 1000)
        confidences.append(result.confidence)

        if result.plate == truth:
            exact += 1
            near += 1
        else:
            similarity = plate_similarity(result.plate, truth)
            if similarity >= 0.9:
                near += 1
            if len(failures) < 8:
                failures.append(
                    {
                        "ground_truth": truth, "read": result.plate,
                        "similarity": similarity, "confidence": result.confidence,
                    }
                )

    return {
        "samples": samples,
        "difficulty": difficulty,
        "exact_match_rate": round(exact / samples, 4),
        "near_match_rate": round(near / samples, 4),
        "mean_confidence": round(sum(confidences) / samples, 4),
        "mean_latency_ms": round(sum(latencies) / samples, 2),
        "backends": anpr.status(),
        "failures": failures,
        "note": (
            "This measures the pipeline, not recognition accuracy. The frames are "
            "rendered by SmartPark and the built-in reader matches templates from "
            "the same font family, so it scores near-perfectly here and 0/5 on "
            "plates drawn in an unseen font. It is not fit for a gate camera: "
            "install easyocr or set ANTHROPIC_API_KEY for the backends built to "
            "read real photographs."
        ),
    }
