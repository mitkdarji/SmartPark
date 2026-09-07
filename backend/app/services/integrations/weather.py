"""Weather context for the demand forecaster and the operations copilot.

Rain measurably changes parking demand at a mall — more cars, longer stays. The
forecaster takes it as a feature; the copilot mentions it when explaining a
surge. Absent an API key this returns a deterministic seasonal estimate rather
than failing, so no downstream code needs a null branch.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_SECONDS = 900.0


async def current_weather(lat: float | None, lon: float | None) -> dict[str, Any]:
    if lat is None or lon is None:
        return _fallback()

    key = f"{lat:.2f},{lon:.2f}"
    now = datetime.now(UTC).timestamp()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    if not settings.openweather_api_key:
        result = _fallback()
        _CACHE[key] = (now, result)
        return result

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "lat": lat, "lon": lon,
                    "appid": settings.openweather_api_key, "units": "metric",
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        log.warning("weather lookup failed", extra={"error": str(exc)})
        return _fallback()

    condition = (data.get("weather") or [{}])[0].get("main", "Clear")
    result = {
        "source": "openweather",
        "temp_c": data.get("main", {}).get("temp"),
        "condition": condition,
        "is_rain": condition in ("Rain", "Drizzle", "Thunderstorm"),
        "humidity": data.get("main", {}).get("humidity"),
        "wind_kph": round((data.get("wind", {}).get("speed") or 0) * 3.6, 1),
    }
    _CACHE[key] = (now, result)
    return result


def _fallback() -> dict[str, Any]:
    """Seasonal estimate for Gujarat, where the reference facility sits."""
    now = datetime.now(UTC)
    day_of_year = now.timetuple().tm_yday
    temp = 28 + 7 * math.sin((day_of_year - 100) / 365 * 2 * math.pi)
    monsoon = 6 <= now.month <= 9
    return {
        "source": "seasonal_estimate",
        "temp_c": round(temp, 1),
        "condition": "Rain" if monsoon and day_of_year % 3 == 0 else "Clear",
        "is_rain": monsoon and day_of_year % 3 == 0,
        "humidity": 78 if monsoon else 45,
        "wind_kph": 12.0,
    }
