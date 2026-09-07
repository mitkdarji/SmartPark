"""Aggregate router for API v1."""

from fastapi import APIRouter

from app.api.v1.routers import (
    ai,
    analytics,
    anpr,
    auth,
    automation,
    city,
    facilities,
    gates,
    realtime,
    sessions,
    vehicles,
    wallet,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(vehicles.router)
api_router.include_router(wallet.router)
api_router.include_router(facilities.router)
api_router.include_router(gates.router)
api_router.include_router(sessions.router)
api_router.include_router(analytics.router)
api_router.include_router(anpr.router)
api_router.include_router(ai.router)
api_router.include_router(automation.router)
api_router.include_router(city.router)

# WebSockets mount at the application root, not under the versioned prefix.
ws_router = realtime.router
