"""
Unit tests for ProfileState (scheduler/state.py).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from timelapse.scheduler.state import ProfileState


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Return a ProfileState backed by a temp directory."""
    monkeypatch.setattr(
        "timelapse.scheduler.state._STATE_DIR", tmp_path
    )
    return ProfileState("test")


class TestProfileStateBasics:
    def test_initial_status(self, state):
        assert state.get_status() == "running"

    def test_set_status(self, state):
        state.set_status("completed")
        assert state.get_status() == "completed"

    def test_initial_counts(self, state):
        assert state.get_photo_count() == 0
        assert state.get_current_batch() == 0
        assert state.get_video_count() == 0

    def test_record_capture_increments(self, state):
        state.record_capture()
        assert state.get_photo_count() == 1
        assert state.get_current_batch() == 1

    def test_record_capture_multiple(self, state):
        for _ in range(5):
            state.record_capture()
        assert state.get_photo_count() == 5
        assert state.get_current_batch() == 5

    def test_record_video_resets_batch(self, state):
        state.record_capture()
        state.record_capture()
        state.record_video()
        assert state.get_current_batch() == 0
        assert state.get_video_count() == 1
        assert state.get_photo_count() == 2  # total unchanged

    def test_last_capture_time_none_initially(self, state):
        assert state.get_last_capture_time() is None

    def test_last_capture_time_stored(self, state):
        dt = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        state.record_capture(dt)
        stored = state.get_last_capture_time()
        assert stored is not None
        assert stored.year == 2024
        assert stored.month == 1

    def test_reset(self, state):
        state.record_capture()
        state.set_status("completed")
        state.reset()
        assert state.get_status() == "running"
        assert state.get_photo_count() == 0


class TestProfileStatePersistence:
    def test_state_persists_across_instances(self, tmp_path, monkeypatch):
        monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path)

        s1 = ProfileState("persist")
        s1.record_capture()
        s1.record_capture()
        s1.set_status("paused")

        # New instance, same profile
        s2 = ProfileState("persist")
        assert s2.get_photo_count() == 2
        assert s2.get_status() == "paused"

    def test_state_file_is_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path)
        s = ProfileState("json_test")
        s.record_capture()
        path = tmp_path / "json_test" / "state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "last_capture_at" in data
        assert data["photo_count"] == 1


class TestAntiDoubleCaptureGuard:
    def test_not_recently_captured_if_no_history(self, state):
        assert state.was_recently_captured(60) is False

    def test_recently_captured_returns_true_within_cooldown(self, state):
        state.record_capture(datetime.now(timezone.utc))
        assert state.was_recently_captured(cooldown_seconds=300) is True

    def test_not_recently_captured_after_cooldown(self, state):
        old = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        state.record_capture(old)
        # cooldown of 1 second, but last capture was 4+ years ago
        assert state.was_recently_captured(cooldown_seconds=1) is False


class TestSolarCache:
    def test_cache_miss(self, state):
        assert state.get_solar_cache("2024-01-15") is None

    def test_cache_hit(self, state):
        times = {
            "sunrise": "2024-01-15T07:30:00+00:00",
            "solar_noon": "2024-01-15T12:00:00+00:00",
            "sunset": "2024-01-15T17:30:00+00:00",
            "source": "astral",
        }
        state.set_solar_cache("2024-01-15", times)
        result = state.get_solar_cache("2024-01-15")
        assert result is not None
        assert result["sunrise"] == times["sunrise"]

    def test_cache_wrong_date(self, state):
        times = {
            "sunrise": "2024-01-15T07:30:00+00:00",
            "solar_noon": "2024-01-15T12:00:00+00:00",
            "sunset": "2024-01-15T17:30:00+00:00",
        }
        state.set_solar_cache("2024-01-15", times)
        assert state.get_solar_cache("2024-01-16") is None

    def test_cache_overwrites_old_date(self, state):
        state.set_solar_cache("2024-01-14", {"sunrise": "X", "solar_noon": "Y", "sunset": "Z"})
        state.set_solar_cache("2024-01-15", {"sunrise": "A", "solar_noon": "B", "sunset": "C"})
        assert state.get_solar_cache("2024-01-14") is None
        assert state.get_solar_cache("2024-01-15") is not None
