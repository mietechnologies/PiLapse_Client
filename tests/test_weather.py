"""Unit tests for the weather recorder (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from timelapse.config.models import LocationConfig, WeatherConfig
from timelapse.weather.recorder import WeatherRecorder, parse_owm

OWM_SAMPLE = {
    "weather": [{"main": "Clouds", "description": "broken clouds"}],
    "main": {"temp": 12.3, "feels_like": 11.1, "humidity": 71, "pressure": 1016},
    "wind": {"speed": 3.6, "deg": 220},
    "clouds": {"all": 75},
}


def test_parse_owm():
    r = parse_owm(OWM_SAMPLE, "metric", datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert r.temperature == 12.3
    assert r.condition == "Clouds"
    assert r.clouds_pct == 75
    assert json.loads(json.dumps(r.as_dict()))["wind_deg"] == 220


def test_parse_owm_tolerates_missing_fields():
    r = parse_owm({}, "imperial", datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert r.temperature is None and r.condition is None


def test_not_usable_without_key(monkeypatch):
    monkeypatch.delenv("TIMELAPSE_OWM_KEY", raising=False)
    rec = WeatherRecorder(WeatherConfig(enabled=True), LocationConfig(latitude=1, longitude=2))
    assert not rec.usable
    rec.start()  # logs and returns; no thread
    assert rec.latest() is None


def test_latest_goes_stale(monkeypatch):
    monkeypatch.setenv("TIMELAPSE_OWM_KEY", "k")
    rec = WeatherRecorder(WeatherConfig(enabled=True), LocationConfig(latitude=1, longitude=2))

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return OWM_SAMPLE

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Resp())
    assert rec.fetch_once() is not None
    now = datetime.now(timezone.utc)
    assert rec.latest(now) is not None
    assert rec.latest(now + timedelta(minutes=31)) is None
