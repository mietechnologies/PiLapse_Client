"""
Solar time calculations.

Computed offline with the `astral` library from the device location, so
sun-based rules never depend on the network.  Each event (sunrise, solar
noon, sunset) is computed on its own: at high latitudes in summer the sun
still rises and sets even though twilight never ends, and asking astral for
everything at once would fail the whole day.

On days with no sunrise or sunset (polar day / polar night) those events
are None and `polar` says which case it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolarTimes:
    sunrise: Optional[datetime]
    solar_noon: Optional[datetime]
    sunset: Optional[datetime]
    # "day" if the sun never sets, "night" if it never rises, else None
    polar: Optional[str] = None

    def get(self, event: str) -> Optional[datetime]:
        return getattr(self, event)

    def as_dict(self) -> dict[str, Optional[str]]:
        return {
            "sunrise": self.sunrise.isoformat() if self.sunrise else None,
            "solar_noon": self.solar_noon.isoformat() if self.solar_noon else None,
            "sunset": self.sunset.isoformat() if self.sunset else None,
            "polar": self.polar,
        }


def get_solar_times(lat: float, lon: float, for_date: date, tz) -> SolarTimes:
    """Sun times for the local calendar date `for_date` in timezone `tz`."""
    return _solar_times_cached(round(lat, 4), round(lon, 4), for_date, tz)


@lru_cache(maxsize=512)
def _solar_times_cached(lat: float, lon: float, for_date: date, tz) -> SolarTimes:
    from astral import Observer
    from astral.sun import elevation, noon, sunrise, sunset

    obs = Observer(latitude=lat, longitude=lon)

    def _event(fn) -> Optional[datetime]:
        try:
            return fn(obs, date=for_date, tzinfo=tz)
        except ValueError:
            # Sun never crosses the horizon on this date
            return None

    rise = _event(sunrise)
    set_ = _event(sunset)
    noon_dt = noon(obs, date=for_date, tzinfo=tz)

    polar = None
    if rise is None or set_ is None:
        polar = "day" if elevation(obs, noon_dt) > 0 else "night"

    return SolarTimes(sunrise=rise, solar_noon=noon_dt, sunset=set_, polar=polar)


def tz_location_mismatch_hours(lon: float, tz, on: Optional[date] = None) -> float:
    """
    How far (in hours) the timezone's standard offset is from what the
    longitude implies.  Large values mean the location or the system
    timezone is probably wrong.
    """
    on = on or date.today()
    probe = datetime(on.year, 1, 1, 12, tzinfo=tz)
    standard = (probe.utcoffset() or timedelta(0)) - (probe.dst() or timedelta(0))
    return abs(standard.total_seconds() / 3600 - lon / 15.0)
