"""
Unit tests for config models and loader.

Tests are designed to run on any OS (no Raspberry Pi required).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from timelapse.config.models import (
    CaptureConfig,
    GlobalConfig,
    IntervalSchedule,
    ProfileConfig,
    RtmpConfig,
    SolarEvent,
    SolarSchedule,
    TimesSchedule,
    parse_interval_seconds,
    parse_hhmm,
)


# ─── parse_interval_seconds ──────────────────────────────────────────────────


class TestParseIntervalSeconds:
    def test_seconds(self):
        assert parse_interval_seconds("30s") == 30

    def test_minutes(self):
        assert parse_interval_seconds("5m") == 300

    def test_hours(self):
        assert parse_interval_seconds("2h") == 7200

    def test_days(self):
        assert parse_interval_seconds("1d") == 86400

    def test_case_insensitive(self):
        assert parse_interval_seconds("5M") == 300

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval_seconds("5x")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_interval_seconds("")


# ─── parse_hhmm ──────────────────────────────────────────────────────────────


class TestParseHHMM:
    def test_midnight(self):
        assert parse_hhmm("00:00") == (0, 0)

    def test_noon(self):
        assert parse_hhmm("12:00") == (12, 0)

    def test_evening(self):
        assert parse_hhmm("23:59") == (23, 59)

    def test_leading_zero(self):
        assert parse_hhmm("08:05") == (8, 5)

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_hhmm("25:00")

    def test_missing_colon(self):
        with pytest.raises(ValueError):
            parse_hhmm("1200")


# ─── IntervalSchedule ─────────────────────────────────────────────────────────


class TestIntervalSchedule:
    def test_valid(self):
        s = IntervalSchedule(interval="10m")
        assert s.interval_seconds == 600

    def test_default_interval(self):
        s = IntervalSchedule()
        assert s.interval == "5m"

    def test_invalid_interval(self):
        with pytest.raises(ValidationError):
            IntervalSchedule(interval="10x")


# ─── TimesSchedule ────────────────────────────────────────────────────────────


class TestTimesSchedule:
    def test_valid(self):
        s = TimesSchedule(times=["08:00", "17:00"])
        assert s.times == ["08:00", "17:00"]

    def test_sorted(self):
        s = TimesSchedule(times=["17:00", "08:00", "12:30"])
        assert s.times == ["08:00", "12:30", "17:00"]

    def test_invalid_time(self):
        with pytest.raises(ValidationError):
            TimesSchedule(times=["25:00"])

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            TimesSchedule(times=[])


# ─── SolarSchedule ────────────────────────────────────────────────────────────


class TestSolarSchedule:
    def test_valid(self):
        s = SolarSchedule(
            latitude=51.5,
            longitude=-0.12,
            events=[SolarEvent(type="sunrise", offset_minutes=10)],
        )
        assert s.latitude == 51.5

    def test_latitude_range(self):
        with pytest.raises(ValidationError):
            SolarSchedule(latitude=91, longitude=0, events=[SolarEvent(type="sunrise")])

    def test_no_duplicate_events(self):
        with pytest.raises(ValidationError, match="Duplicate"):
            SolarSchedule(
                latitude=51.5,
                longitude=-0.12,
                events=[
                    SolarEvent(type="sunrise", offset_minutes=0),
                    SolarEvent(type="sunrise", offset_minutes=0),
                ],
            )


# ─── CaptureConfig ────────────────────────────────────────────────────────────


class TestCaptureConfig:
    def test_defaults(self):
        c = CaptureConfig()
        assert c.width == 1920
        assert c.height == 1080
        assert c.jpeg_quality == 85

    def test_quality_range(self):
        with pytest.raises(ValidationError):
            CaptureConfig(jpeg_quality=0)
        with pytest.raises(ValidationError):
            CaptureConfig(jpeg_quality=101)


# ─── RtmpConfig ───────────────────────────────────────────────────────────────


class TestRtmpConfig:
    def test_valid_url(self):
        r = RtmpConfig(rtmp_url="rtmp://a.rtmp.youtube.com/live2")
        assert r.rtmp_url.startswith("rtmp://")

    def test_invalid_url(self):
        with pytest.raises(ValidationError):
            RtmpConfig(rtmp_url="http://example.com/stream")

    def test_valid_bitrate(self):
        r = RtmpConfig(video_bitrate="2M")
        assert r.video_bitrate == "2M"

    def test_invalid_bitrate(self):
        with pytest.raises(ValidationError):
            RtmpConfig(video_bitrate="high")


# ─── ProfileConfig ────────────────────────────────────────────────────────────


class TestProfileConfig:
    def test_defaults(self):
        p = ProfileConfig()
        assert "timelapse" in p.photos_dir

    def test_resolve_dirs(self):
        p = ProfileConfig(
            photos_dir="~/timelapse/{profile}/photos",
            videos_dir="~/timelapse/{profile}/videos",
            archive_dir="~/timelapse/{profile}/archive",
        )
        resolved = p.resolve_dirs("garden")
        home = Path.home()
        assert str(resolved.photos_path) == str(home / "timelapse" / "garden" / "photos")
        assert "{profile}" not in str(resolved.photos_path)

    def test_schedule_discriminator_interval(self):
        data = {
            "schedule": {"mode": "interval", "interval": "10m"},
        }
        p = ProfileConfig.model_validate(data)
        assert isinstance(p.schedule, IntervalSchedule)

    def test_schedule_discriminator_solar(self):
        data = {
            "schedule": {
                "mode": "solar",
                "latitude": 51.5,
                "longitude": -0.12,
                "events": [{"type": "sunrise", "offset_minutes": 0}],
            }
        }
        p = ProfileConfig.model_validate(data)
        assert isinstance(p.schedule, SolarSchedule)


# ─── GlobalConfig ─────────────────────────────────────────────────────────────


class TestGlobalConfig:
    def test_defaults(self):
        g = GlobalConfig()
        assert g.log_level == "INFO"
        assert g.timezone is None

    def test_log_level_valid(self):
        g = GlobalConfig(log_level="DEBUG")
        assert g.log_level == "DEBUG"

    def test_log_level_invalid(self):
        with pytest.raises(ValidationError):
            GlobalConfig(log_level="VERBOSE")


# ─── Loader round-trip ───────────────────────────────────────────────────────


class TestConfigLoaderRoundTrip:
    """Test that save → load round-trips cleanly."""

    def test_global_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "timelapse.config.loader.CONFIG_DIR", tmp_path
        )
        monkeypatch.setattr(
            "timelapse.config.loader.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml"
        )
        monkeypatch.setattr(
            "timelapse.config.loader.PROFILES_DIR", tmp_path / "profiles"
        )

        from timelapse.config.loader import load_global_config, save_global_config

        original = GlobalConfig(timezone="America/New_York", log_level="DEBUG")
        save_global_config(original)

        loaded = load_global_config()
        assert loaded.timezone == "America/New_York"
        assert loaded.log_level == "DEBUG"

    def test_profile_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("timelapse.config.loader.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("timelapse.config.loader.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        profiles_dir = tmp_path / "profiles"
        monkeypatch.setattr("timelapse.config.loader.PROFILES_DIR", profiles_dir)

        from timelapse.config.loader import load_profile_config, save_profile_config

        original = ProfileConfig(
            schedule=IntervalSchedule(interval="30s"),
        )
        save_profile_config("test", original)

        loaded = load_profile_config("test")
        assert isinstance(loaded.schedule, IntervalSchedule)
        assert loaded.schedule.interval == "30s"
