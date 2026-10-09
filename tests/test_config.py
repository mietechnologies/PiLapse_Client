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
    AtRule,
    EveryRule,
    LocationConfig,
    ProfileConfig,
    RtmpConfig,
    ScheduleConfig,
    SunRule,
    TimeSpec,
    WeatherConfig,
    parse_hhmm,
    parse_interval_seconds,
    parse_offset_seconds,
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


# ─── schedule rules ───────────────────────────────────────────────────────────


class TestParseOffset:
    @pytest.mark.parametrize(
        "value,expected",
        [("-15m", -900), ("+1h", 3600), ("30s", 30), ("0", 0), ("10", 600), (-15, -900)],
    )
    def test_valid(self, value, expected):
        assert parse_offset_seconds(value) == expected

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_offset_seconds("soon")


class TestTimeSpec:
    def test_clock(self):
        t = TimeSpec.parse("08:30")
        assert (t.hour, t.minute, t.sun) == (8, 30, None)

    def test_sun_with_offset(self):
        t = TimeSpec.parse("sunset+30m")
        assert t.sun == "sunset" and t.offset_s == 1800

    def test_invalid(self):
        with pytest.raises(ValueError):
            TimeSpec.parse("lunchtime")


class TestRules:
    def test_at_sorted_and_single_string(self):
        assert AtRule(at=["17:00", "08:00"]).at == ["08:00", "17:00"]
        assert AtRule.model_validate({"at": "07:30"}).at == ["07:30"]

    def test_unquoted_yaml_times(self):
        data = yaml.safe_load("at: [12:00, 08:30]")
        assert data["at"][0] == 720  # what YAML 1.1 actually gives us
        assert AtRule.model_validate(data).at == ["08:30", "12:00"]
        between = yaml.safe_load("between: [13:00, sunset]")["between"]
        assert EveryRule(every="5m", between=between).between == ["13:00", "sunset"]

    def test_at_invalid_time(self):
        with pytest.raises(ValidationError):
            AtRule(at=["25:00"])

    def test_at_empty(self):
        with pytest.raises(ValidationError):
            AtRule(at=[])

    def test_every(self):
        r = EveryRule(every="10m", between=["sunrise-30m", "18:00"])
        assert r.interval_seconds == 600
        assert r.uses_sun

    def test_every_invalid(self):
        with pytest.raises(ValidationError):
            EveryRule(every="10x")
        with pytest.raises(ValidationError):
            EveryRule(every="5m", between=["08:00"])

    def test_sun(self):
        r = SunRule(sun="sunset", offset="-15m")
        assert r.offset_seconds == -900
        assert r.describe() == "sunset -15m"

    def test_days(self):
        r = AtRule(at=["08:00"], days=["Monday", "fri"])
        assert r.days == ["mon", "fri"]
        assert r.weekdays == {0, 4}
        with pytest.raises(ValidationError):
            AtRule(at=["08:00"], days=["someday"])

    def test_schedule_picks_rule_type_by_key(self):
        s = ScheduleConfig.model_validate(
            {"rules": [{"at": ["07:30"]}, {"every": "5m"}, {"sun": "sunrise"}]}
        )
        assert [type(r) for r in s.rules] == [AtRule, EveryRule, SunRule]
        assert s.uses_sun

    def test_unknown_rule_rejected(self):
        with pytest.raises(ValidationError):
            ScheduleConfig.model_validate({"rules": [{"hourly": True}]})

    def test_default_schedule(self):
        assert ScheduleConfig().describe() == ["every 5m"]


class TestWeatherConfig:
    def test_defaults(self):
        w = WeatherConfig()
        assert not w.enabled and w.refresh_seconds == 600

    def test_refresh_floor(self):
        with pytest.raises(ValidationError):
            WeatherConfig(refresh="30s")


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

    def test_legacy_interval_schedule(self):
        p = ProfileConfig.model_validate({"schedule": {"mode": "interval", "interval": "10m"}})
        assert p.schedule.describe() == ["every 10m"]

    def test_legacy_times_schedule(self):
        p = ProfileConfig.model_validate(
            {"schedule": {"mode": "times", "times": ["17:00", "08:00"]}}
        )
        assert p.schedule.describe() == ["at 08:00, 17:00"]

    def test_legacy_solar_schedule_moves_location(self):
        data = {
            "schedule": {
                "mode": "solar",
                "latitude": 51.5,
                "longitude": -0.12,
                "owm_api_key_env": "TIMELAPSE_OWM_KEY",
                "events": [
                    {"type": "sunrise", "offset_minutes": 10},
                    {"type": "sunset", "offset_minutes": -15},
                ],
            }
        }
        p = ProfileConfig.model_validate(data)
        assert p.schedule.describe() == ["sunrise +10m", "sunset -15m"]
        assert p.location == LocationConfig(latitude=51.5, longitude=-0.12, source="legacy")

    def test_round_trip(self):
        p = ProfileConfig.model_validate(
            {"schedule": {"rules": [{"every": "5m", "between": ["sunrise", "sunset"]}]}}
        )
        again = ProfileConfig.model_validate(p.model_dump(exclude_none=True))
        assert again.schedule == p.schedule


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
            schedule=ScheduleConfig(
                rules=[EveryRule(every="30s", days=["mon"]), SunRule(sun="sunset", offset="-15m")]
            ),
        )
        save_profile_config("test", original)

        loaded = load_profile_config("test")
        assert loaded.schedule == original.schedule
        # Saved YAML uses the rule keys, not empty placeholders
        text = (profiles_dir / "test.yaml").read_text()
        assert "every: 30s" in text and "sun: sunset" in text
