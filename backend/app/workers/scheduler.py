"""Background scheduler.

Three jobs, each doing the thing that has to happen whether or not a request
arrives:

  automation tick    evaluates every `schedule:tick` rule
  occupancy snapshot writes the time-series point the forecaster trains on
  nightly retrain    refits the demand, dwell and anomaly models

AsyncIOScheduler runs inside the API process, which is right for a single-node
deployment. At multiple nodes this moves to a dedicated worker with a database
lock so the jobs do not run N times; the job functions themselves would not
change.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging import get_logger
from app.services.automation.engine import automation_engine

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _automation_tick() -> None:
    result = await automation_engine.tick()
    fired = result.get("fired", [])
    if fired:
        log.info("automation tick", extra={"rules_fired": len(fired)})


async def _occupancy_snapshot() -> None:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.facility import Facility
    from app.services.analytics import analytics_service

    async with SessionLocal() as db:
        facilities = (
            await db.execute(select(Facility).where(Facility.is_active.is_(True)))
        ).scalars().all()
        for facility in facilities:
            await analytics_service.snapshot(db, facility)
        await db.commit()
        log.info("occupancy snapshot written", extra={"facilities": len(facilities)})


async def _nightly_retrain() -> None:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.facility import Facility
    from app.services.analytics import analytics_service

    async with SessionLocal() as db:
        facilities = (
            await db.execute(select(Facility).where(Facility.is_active.is_(True)))
        ).scalars().all()
        for facility in facilities:
            result = await analytics_service.train_models(db, facility)
            log.info("models retrained", extra={"facility_id": facility.id, "result": result})
        await db.commit()


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if not settings.automation_enabled:
        log.info("automation disabled — scheduler not started")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _automation_tick,
        IntervalTrigger(seconds=settings.automation_tick_seconds),
        id="automation_tick", max_instances=1, coalesce=True, replace_existing=True,
    )
    scheduler.add_job(
        _occupancy_snapshot,
        IntervalTrigger(minutes=15),
        id="occupancy_snapshot", max_instances=1, coalesce=True, replace_existing=True,
    )
    scheduler.add_job(
        _nightly_retrain,
        CronTrigger(hour=3, minute=15),
        id="nightly_retrain", max_instances=1, coalesce=True, replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "scheduler started",
        extra={"jobs": [job.id for job in scheduler.get_jobs()]},
    )
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
