"""Public smart-city open-data feed.

Unauthenticated by design: this is the surface a municipal dashboard, a traffic
management centre, or a navigation app consumes. It carries counts, rates and
coordinates — never plates, sessions, or anything that identifies a person.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import DbSession
from app.core.config import settings
from app.services.analytics import analytics_service

router = APIRouter(prefix="/city", tags=["smart-city"])


@router.get("/availability", response_model=dict, summary="Live availability across all facilities")
async def availability(db: DbSession, response: Response) -> dict:
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return await analytics_service.city_feed(db)


@router.get("/availability.geojson", response_model=dict, summary="The same feed as GeoJSON")
async def availability_geojson(db: DbSession, response: Response) -> dict:
    """GeoJSON so the feed drops straight onto any mapping stack."""
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["Access-Control-Allow-Origin"] = "*"
    feed = await analytics_service.city_feed(db)

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [facility["lon"], facility["lat"]],
            },
            "properties": {
                key: value
                for key, value in facility.items()
                if key not in ("lat", "lon")
            },
        }
        for facility in feed["facilities"]
        if facility.get("lat") is not None and facility.get("lon") is not None
    ]
    return {
        "type": "FeatureCollection",
        "generated_at": feed["generated_at"],
        "operator_id": feed["operator_id"],
        "license": feed["license"],
        "features": features,
    }


@router.get("/manifest", response_model=dict)
async def manifest(response: Response) -> dict:
    """Discovery document describing the feed, in the style of GBFS."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    return {
        "operator_id": settings.city_feed_operator_id,
        "name": "SmartPark open parking availability",
        "license": "CC-BY-4.0",
        "ttl_seconds": 60,
        "contains_personal_data": False,
        "feeds": [
            {"name": "availability", "url": f"{settings.api_v1_prefix}/city/availability"},
            {"name": "availability_geojson", "url": f"{settings.api_v1_prefix}/city/availability.geojson"},
        ],
        "fields": {
            "capacity": "total active bays",
            "free_slots": "bays currently empty",
            "occupancy_pct": "percentage of bays occupied",
            "rate_minor_per_hour": "current occupancy-adjusted rate, in minor currency units",
            "ev_bays": "bays with an EV charger",
        },
    }
