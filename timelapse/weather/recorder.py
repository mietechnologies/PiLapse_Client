"""
Weather recorder.

A background thread fetches current conditions from OpenWeatherMap every
`weather.refresh` and keeps the latest reading in memory.  The capture
pipeline asks for `latest()` and stores it with each photo.

Weather never blocks capture: if the key is missing or the network is down,
`latest()` returns None (or a reading flagged stale) and photos are taken as
normal.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from timelapse.config.models import LocationConfig, WeatherConfig

logger = logging.getLogger(__name__)

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# A reading older than this many refresh periods is not attached to photos
STALE_AFTER_REFRESHES = 3


@dataclass(frozen=True)
class WeatherReading:
    fetched_at: str
    units: str  # metric (°C, m/s) | imperial (°F, mph)
    temperature: Optional[float] = None
    feels_like: Optional[float] = None
    humidity: Optional[int] = None
    pressure_hpa: Optional[int] = None
    clouds_pct: Optional[int] = None
    wind_speed: Optional[float] = None
    wind_deg: Optional[int] = None
    condition: Optional[str] = None
    description: Optional[str] = None
    source: str = "openweathermap"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_owm(data: dict, units: str, fetched_at: datetime) -> WeatherReading:
    """Turn an OWM /data/2.5/weather response into a WeatherReading."""
    main = data.get("main", {})
    wind = data.get("wind", {})
    weather = (data.get("weather") or [{}])[0]
    return WeatherReading(
        fetched_at=fetched_at.isoformat(),
        units=units,
        temperature=main.get("temp"),
        feels_like=main.get("feels_like"),
        humidity=main.get("humidity"),
        pressure_hpa=main.get("pressure"),
        clouds_pct=(data.get("clouds") or {}).get("all"),
        wind_speed=wind.get("speed"),
        wind_deg=wind.get("deg"),
        condition=weather.get("main"),
        description=weather.get("description"),
    )


class WeatherRecorder:
    def __init__(self, config: WeatherConfig, location: Optional[LocationConfig]) -> None:
        self._cfg = config
        self._location = location
        self._latest: Optional[WeatherReading] = None
        self._latest_at: Optional[datetime] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def usable(self) -> bool:
        return bool(self._cfg.enabled and self._location and self._api_key())

    def _api_key(self) -> str:
        return os.environ.get(self._cfg.api_key_env, "").strip()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if self._location is None:
            logger.warning("Weather recording is on but no location is set; skipping.")
            return
        if not self._api_key():
            logger.warning(
                "Weather recording is on but $%s is not set; photos will have no weather.",
                self._cfg.api_key_env,
            )
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
        self._thread.start()
        logger.info("Weather recorder started (every %s).", self._cfg.refresh)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.fetch_once()
            self._stop_event.wait(self._cfg.refresh_seconds)

    # ── fetching ───────────────────────────────────────────────────────────

    def fetch_once(self) -> Optional[WeatherReading]:
        if self._location is None:
            return None
        try:
            import requests

            resp = requests.get(
                OWM_URL,
                params={
                    "lat": self._location.latitude,
                    "lon": self._location.longitude,
                    "appid": self._api_key(),
                    "units": self._cfg.units,
                },
                timeout=10,
            )
            if resp.status_code == 401:
                logger.warning("OpenWeatherMap rejected the API key (401).")
                return None
            resp.raise_for_status()
            now = datetime.now(timezone.utc)
            reading = parse_owm(resp.json(), self._cfg.units, now)
        except Exception as exc:
            # Don't log the request URL: it contains the API key.
            logger.warning("Weather fetch failed: %s", type(exc).__name__)
            return None
        with self._lock:
            self._latest = reading
            self._latest_at = now
        logger.debug("Weather: %s", reading.as_dict())
        return reading

    def latest(self, now: Optional[datetime] = None) -> Optional[WeatherReading]:
        """Latest reading, or None if there isn't a recent one."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            if self._latest is None or self._latest_at is None:
                return None
            max_age = timedelta(seconds=self._cfg.refresh_seconds * STALE_AFTER_REFRESHES)
            if now - self._latest_at > max_age:
                return None
            return self._latest
