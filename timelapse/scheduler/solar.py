"""
Solar time calculations.

Primary source:  OpenWeatherMap One Call API (requires API key).
Fallback:        `astral` library — offline, pure Python, very accurate.

If the OWM call fails the fallback is used transparently.
If neither is available, the solar schedule is skipped for that day.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ─── public data class ───────────────────────────────────────────────────────


class SolarTimes:
    __slots__ = ("sunrise", "solar_noon", "sunset", "source")

    def __init__(
        self,
        sunrise: datetime,
        solar_noon: datetime,
        sunset: datetime,
        source: str = "unknown",
    ) -> None:
        self.sunrise = sunrise
        self.solar_noon = solar_noon
        self.sunset = sunset
        self.source = source

    def as_dict(self) -> dict[str, str]:
        return {
            "sunrise": self.sunrise.isoformat(),
            "solar_noon": self.solar_noon.isoformat(),
            "sunset": self.sunset.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "SolarTimes":
        return cls(
            sunrise=datetime.fromisoformat(d["sunrise"]),
            solar_noon=datetime.fromisoformat(d["solar_noon"]),
            sunset=datetime.fromisoformat(d["sunset"]),
            source=d.get("source", "cache"),
        )


# ─── OWM provider ────────────────────────────────────────────────────────────


def _fetch_owm(lat: float, lon: float, api_key: str) -> Optional[SolarTimes]:
    """
    Fetch today's solar times from OpenWeatherMap One Call 3.0 / 2.5 API.
    Returns None on any network or API error.
    """
    try:
        import requests

        # Try One Call 3.0 first, fall back to the free 2.5 endpoint
        url = (
            f"https://api.openweathermap.org/data/3.0/onecall"
            f"?lat={lat}&lon={lon}&exclude=minutely,hourly,daily,alerts"
            f"&appid={api_key}"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 401:
            # 3.0 may require subscription; try 2.5 weather endpoint for sunrise/sunset
            url25 = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?lat={lat}&lon={lon}&appid={api_key}"
            )
            resp = requests.get(url25, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            sys_data = data.get("sys", {})
            sunrise_ts = sys_data.get("sunrise")
            sunset_ts = sys_data.get("sunset")
            if not (sunrise_ts and sunset_ts):
                return None
            sunrise = datetime.fromtimestamp(sunrise_ts, tz=timezone.utc)
            sunset = datetime.fromtimestamp(sunset_ts, tz=timezone.utc)
            solar_noon = sunrise + (sunset - sunrise) / 2
            return SolarTimes(sunrise, solar_noon, sunset, source="owm-2.5")

        resp.raise_for_status()
        data = resp.json()
        current = data.get("current", {})
        sunrise_ts = current.get("sunrise")
        sunset_ts = current.get("sunset")
        if not (sunrise_ts and sunset_ts):
            return None
        sunrise = datetime.fromtimestamp(sunrise_ts, tz=timezone.utc)
        sunset = datetime.fromtimestamp(sunset_ts, tz=timezone.utc)
        solar_noon = sunrise + (sunset - sunrise) / 2
        return SolarTimes(sunrise, solar_noon, sunset, source="owm-3.0")

    except Exception as exc:
        logger.warning("OWM fetch failed: %s", exc)
        return None


# ─── astral fallback ─────────────────────────────────────────────────────────


def _fetch_astral(lat: float, lon: float, for_date: date) -> Optional[SolarTimes]:
    """Use the `astral` library for offline calculation."""
    try:
        from astral import LocationInfo
        from astral.sun import sun

        loc = LocationInfo(latitude=lat, longitude=lon)
        s = sun(loc.observer, date=for_date, tzinfo=timezone.utc)
        return SolarTimes(
            sunrise=s["sunrise"],
            solar_noon=s["noon"],
            sunset=s["sunset"],
            source="astral",
        )
    except ImportError:
        logger.warning("astral library not installed — cannot compute solar times offline.")
        return None
    except Exception as exc:
        logger.warning("astral calculation failed: %s", exc)
        return None


# ─── public API ──────────────────────────────────────────────────────────────


def get_solar_times(
    lat: float,
    lon: float,
    api_key_env: str,
    for_date: Optional[date] = None,
) -> Optional[SolarTimes]:
    """
    Get solar times for `for_date` (default: today UTC).

    1. Try OpenWeatherMap if API key is set.
    2. Fall back to astral.
    3. Return None if both fail (caller should skip solar captures).
    """
    if for_date is None:
        for_date = date.today()

    api_key = os.environ.get(api_key_env, "").strip()
    if api_key:
        result = _fetch_owm(lat, lon, api_key)
        if result:
            logger.debug("Solar times from OWM: %s", result.as_dict())
            return result
        logger.warning(
            "OWM failed; falling back to astral. "
            "Solar times may differ slightly from OWM."
        )

    result = _fetch_astral(lat, lon, for_date)
    if result:
        logger.debug("Solar times from astral: %s", result.as_dict())
        return result

    logger.error(
        "Could not obtain solar times (no API key, no astral, or both failed). "
        "Solar captures will be skipped today."
    )
    return None


def compute_solar_schedule(
    solar_times: SolarTimes,
    events: list,  # list[SolarEvent]
    local_tz,
) -> list[datetime]:
    """
    Given resolved SolarTimes and a list of SolarEvent configs,
    return a sorted list of aware datetimes for today's solar captures.
    """
    base_map = {
        "sunrise": solar_times.sunrise,
        "solar_noon": solar_times.solar_noon,
        "sunset": solar_times.sunset,
    }
    result: list[datetime] = []
    for event in events:
        base = base_map[event.type]
        dt = base + timedelta(minutes=event.offset_minutes)
        dt_local = dt.astimezone(local_tz)
        result.append(dt_local)
    return sorted(result)
