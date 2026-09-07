"""SmartPark application entry point."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.v1.router import api_router, ws_router
from app.core.config import BACKEND_ROOT, settings
from app.core.errors import SmartParkError
from app.core.logging import configure_logging, get_logger, set_request_id
from app.db.session import healthcheck, init_db
from app.schemas.common import HealthResponse
from app.services.automation.engine import register_automation_subscribers
from app.services.realtime.manager import register_realtime_bridge, ws_manager
from app.workers.scheduler import start_scheduler, stop_scheduler

configure_logging()
log = get_logger(__name__)

DESCRIPTION = """
**SmartPark** — intelligent parking allocation and automated billing.

A vehicle is recognised at the gate by its number plate, assigned a bay by a
recency-aware allocation policy, guided there turn by turn, and billed
automatically from a wallet when it leaves.

* `/gates` — the endpoints a barrier controller calls (ANPR entry and exit)
* `/facilities` — the digital layout builder: levels, bays, gates, access lists
* `/anpr` — plate recognition on its own, plus a labelled synthetic-frame generator
* `/analytics` — KPIs, demand forecasting, per-bay heat maps, sustainability
* `/agent`, `/voice` — the AI assistant and the spoken interface
* `/automation` — declarative operational rules
* `/city` — a public, personal-data-free open availability feed
* `/benchmark` — the allocation and ANPR evaluation harnesses
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    register_realtime_bridge()
    register_automation_subscribers()
    scheduler = start_scheduler()
    app.state.scheduler_running = scheduler is not None
    log.info(
        "smartpark started",
        extra={
            "version": __version__,
            "env": settings.app_env,
            "genai": settings.genai_enabled,
            "scheduler": app.state.scheduler_running,
        },
    )
    try:
        yield
    finally:
        stop_scheduler()
        log.info("smartpark stopped")


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Process-Time", "X-Ground-Truth-Plate"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a correlation id and record the server-side latency."""
    request_id = set_request_id(request.headers.get("X-Request-ID"))
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{elapsed_ms:.2f}ms"
    if elapsed_ms > 1500:
        log.warning(
            "slow request",
            extra={"path": request.url.path, "method": request.method, "ms": round(elapsed_ms, 1)},
        )
    return response


@app.exception_handler(SmartParkError)
async def domain_error_handler(request: Request, exc: SmartParkError) -> JSONResponse:
    """One place maps every domain error onto its HTTP status."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "The request payload was not valid.",
            "code": "validation_error",
            "details": {"fields": exc.errors()},
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "unhandled exception",
        extra={"path": request.url.path, "error": str(exc)},
        exc_info=exc,
    )
    # Never leak internals to a client; the correlation id ties the response to
    # the full stack trace in the logs.
    return JSONResponse(
        status_code=500,
        content={
            "error": "An unexpected error occurred.",
            "code": "internal_error",
            "details": {"request_id": request.headers.get("X-Request-ID", "")},
        },
    )


@app.get("/", tags=["meta"], summary="Service banner")
async def root() -> dict:
    return {
        "name": settings.app_name,
        "version": __version__,
        "description": "Intelligent parking allocation and automated billing.",
        "docs": "/docs",
        "health": "/health",
        "api": settings.api_v1_prefix,
        "open_data": f"{settings.api_v1_prefix}/city/availability",
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Deep health check — every subsystem reports its own readiness."""
    from app.services.anpr.pipeline import anpr
    from app.services.voice import pipeline as voice

    db_ok = await healthcheck()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        environment=settings.app_env,
        database=db_ok,
        genai={"enabled": settings.genai_enabled, "model": settings.genai_model},
        anpr=anpr.status(),
        voice=voice.status(),
        scheduler=bool(getattr(app.state, "scheduler_running", False)),
        websocket_clients=ws_manager.client_count(),
        timestamp=datetime.now(UTC),
    )


app.include_router(api_router, prefix=settings.api_v1_prefix)
app.include_router(ws_router)

# Captured frames and uploaded floor plans, served for the operator console.
app.mount("/media", StaticFiles(directory=str(settings.data_dir)), name="media")


def _mount_frontend() -> None:
    """Serve the built SPA when it is present (the Docker image bundles it).

    In development Vite serves the frontend and proxies here, so this directory
    does not exist and the mount is skipped. `html=True` makes StaticFiles fall
    back to index.html, which is what a client-side router needs for deep links.
    """
    static_dir = BACKEND_ROOT / "static"
    if not (static_dir / "index.html").exists():
        log.info("no bundled frontend — run the Vite dev server separately")
        return
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")
    log.info("serving bundled frontend", extra={"path": str(static_dir)})


_mount_frontend()
