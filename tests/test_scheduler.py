"""
Unit tests for scheduler logic.

Uses freezegun to control 'now' without touching the clock.
All tests run offline — no camera, no network, no real Pi needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

from timelapse.config.models import (
    IntervalSchedule,
    ProfileConfig,
    SolarEvent,
    SolarSchedule,
    TimesSchedule,
)
from timelapse.scheduler.scheduler import CAPTURE_COOLDOWN_S, Scheduler
from timelapse.scheduler.state import ProfileState

UTC = timezone.utc


def _make_state(tmp_path, monkeypatch, profile="sched_test"):
    monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path)
    return ProfileState(profile)


def _make_scheduler(config, state, capture_fn=None):
    if capture_fn is None:
        capture_fn = MagicMock(return_value=True)
    return Scheduler(
        profile="test",
        config=config,
        state=state,
        capture_fn=capture_fn,
        local_tz=UTC,
    )


# ─── interval schedule ─────────────────────────────────────────────────────


class TestIntervalScheduleNextTime:
    def test_first_capture_is_immediate(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch, "i1")
        config = ProfileConfig(schedule=IntervalSchedule(interval="5m"))
        sched = _make_scheduler(config, state)

        now = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        assert next_dt is not None
        assert abs((next_dt - now).total_seconds()) < 5  # essentially now

    def test_next_time_respects_interval(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch, "i2")
        last = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        state.record_capture(last)

        config = ProfileConfig(schedule=IntervalSchedule(interval="5m"))
        sched = _make_scheduler(config, state)

        now = datetime(2024, 1, 15, 8, 1, 0, tzinfo=UTC)  # 1 min after last
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        expected = last + timedelta(minutes=5)
        assert next_dt is not None
        assert abs((next_dt - expected).total_seconds()) < 2

    def test_missed_slot_gives_now(self, tmp_path, monkeypatch):
        """If we've missed more than one interval (reboot etc.), capture now."""
        state = _make_state(tmp_path, monkeypatch, "i3")
        last = datetime(2024, 1, 15, 7, 0, 0, tzinfo=UTC)
        state.record_capture(last)

        config = ProfileConfig(schedule=IntervalSchedule(interval="5m"))
        sched = _make_scheduler(config, state)

        now = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)  # 60 min later
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        assert next_dt is not None
        assert abs((next_dt - now).total_seconds()) < 5


# ─── fixed times schedule ──────────────────────────────────────────────────


class TestTimesScheduleNextTime:
    def test_picks_next_future_slot(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch, "t1")
        config = ProfileConfig(schedule=TimesSchedule(times=["08:00", "12:00", "17:00"]))
        sched = _make_scheduler(config, state)

        # It's 10:00 — next slot should be 12:00
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        assert next_dt is not None
        assert next_dt.hour == 12
        assert next_dt.minute == 0

    def test_after_last_slot_gives_tomorrow(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch, "t2")
        config = ProfileConfig(schedule=TimesSchedule(times=["08:00", "12:00", "17:00"]))
        sched = _make_scheduler(config, state)

        # It's 18:00 — all today's slots passed → tomorrow 08:00
        now = datetime(2024, 1, 15, 18, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        assert next_dt is not None
        assert next_dt.day == 16
        assert next_dt.hour == 8

    def test_already_captured_slot_is_skipped(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch, "t3")
        # Simulated just captured at 08:00
        last = datetime(2024, 1, 15, 8, 0, 30, tzinfo=UTC)
        state.record_capture(last)

        config = ProfileConfig(schedule=TimesSchedule(times=["08:00", "12:00"]))
        sched = _make_scheduler(config, state)

        # It's 08:01 — 08:00 slot was just taken → next is 12:00
        now = datetime(2024, 1, 15, 8, 1, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))
        assert next_dt is not None
        assert next_dt.hour == 12


# ─── solar schedule ────────────────────────────────────────────────────────


class TestSolarScheduleNextTime:
    @patch("timelapse.scheduler.scheduler.get_solar_times")
    def test_uses_cached_solar_times(self, mock_get, tmp_path, monkeypatch):
        from timelapse.scheduler.solar import SolarTimes

        state = _make_state(tmp_path, monkeypatch, "s1")
        sunrise = datetime(2024, 1, 15, 7, 30, 0, tzinfo=UTC)
        solar_noon = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        sunset = datetime(2024, 1, 15, 17, 30, 0, tzinfo=UTC)
        st = SolarTimes(sunrise, solar_noon, sunset, source="mock")

        # Pre-cache today's solar times
        state.set_solar_cache("2024-01-15", st.as_dict())

        config = ProfileConfig(
            schedule=SolarSchedule(
                latitude=51.5,
                longitude=-0.12,
                events=[SolarEvent(type="sunrise", offset_minutes=0)],
            )
        )
        sched = _make_scheduler(config, state)

        # It's 06:00 — sunrise hasn't happened yet
        now = datetime(2024, 1, 15, 6, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))

        # get_solar_times should NOT be called (cache hit)
        mock_get.assert_not_called()
        assert next_dt is not None
        assert next_dt.hour == 7
        assert next_dt.minute == 30

    @patch("timelapse.scheduler.scheduler.get_solar_times")
    def test_solar_fetch_failure_returns_none(self, mock_get, tmp_path, monkeypatch):
        mock_get.return_value = None  # simulate network failure

        state = _make_state(tmp_path, monkeypatch, "s2")
        config = ProfileConfig(
            schedule=SolarSchedule(
                latitude=51.5,
                longitude=-0.12,
                events=[SolarEvent(type="sunrise")],
            )
        )
        sched = _make_scheduler(config, state)

        now = datetime(2024, 1, 15, 6, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))

        assert next_dt is None

    @patch("timelapse.scheduler.scheduler.get_solar_times")
    def test_solar_offset_applied(self, mock_get, tmp_path, monkeypatch):
        from timelapse.scheduler.solar import SolarTimes

        state = _make_state(tmp_path, monkeypatch, "s3")
        sunrise = datetime(2024, 1, 15, 7, 30, 0, tzinfo=UTC)
        solar_noon = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        sunset = datetime(2024, 1, 15, 17, 30, 0, tzinfo=UTC)
        mock_get.return_value = SolarTimes(sunrise, solar_noon, sunset, source="mock")

        config = ProfileConfig(
            schedule=SolarSchedule(
                latitude=51.5,
                longitude=-0.12,
                events=[SolarEvent(type="sunrise", offset_minutes=10)],
            )
        )
        sched = _make_scheduler(config, state)

        now = datetime(2024, 1, 15, 6, 0, 0, tzinfo=UTC)
        with freeze_time(now):
            next_dt = sched._next_capture_time(datetime.now(UTC))

        assert next_dt is not None
        # Should be sunrise + 10 min = 07:40
        assert next_dt.hour == 7
        assert next_dt.minute == 40


# ─── disk guard integration ────────────────────────────────────────────────


class TestDiskGuard:
    def test_safe_delete_guard(self, tmp_path):
        from timelapse.disk.guard import DiskGuard, _safe_to_delete

        photos = tmp_path / "photos"
        videos = tmp_path / "videos"
        archive = tmp_path / "archive"
        for d in (photos, videos, archive):
            d.mkdir()

        guard = DiskGuard(3.0, photos, videos, archive)

        # File inside photos_dir → safe
        f = photos / "frame_001.jpg"
        f.write_bytes(b"x")
        assert _safe_to_delete(f, [photos, videos, archive]) is True

        # File outside → not safe
        outsider = tmp_path.parent / "important.jpg"
        assert _safe_to_delete(outsider, [photos, videos, archive]) is False

    def test_archived_photos_are_deletion_candidates(self, tmp_path):
        """Archived photos live in archive/<video-stem>/ and must be reclaimable."""
        import os

        from timelapse.disk.guard import DiskGuard

        photos, videos, archive = tmp_path / "p", tmp_path / "v", tmp_path / "a"
        for d in (photos, videos, archive):
            d.mkdir()
        old = archive / "timelapse_X" / "frame_old.jpg"
        old.parent.mkdir()
        old.write_bytes(b"x")
        new = photos / "frame_new.jpg"
        new.write_bytes(b"x")
        os.utime(old, (1_000, 1_000))

        guard = DiskGuard(3.0, photos, videos, archive)
        assert guard._deletion_candidates() == [old, new]

    def test_enforce_removes_emptied_archive_subdir(self, tmp_path):
        from timelapse.disk.guard import DiskGuard

        photos, videos, archive = tmp_path / "p", tmp_path / "v", tmp_path / "a"
        for d in (photos, videos, archive):
            d.mkdir()
        sub = archive / "timelapse_X"
        sub.mkdir()
        (sub / "frame_1.jpg").write_bytes(b"x")

        guard = DiskGuard(3.0, photos, videos, archive)
        # Report "full" until the one file is gone
        with patch.object(DiskGuard, "has_free_space", side_effect=[False, False, True, True]):
            assert guard.enforce() == 1
        assert not sub.exists()
        assert archive.exists()


# ─── failure back-off ──────────────────────────────────────────────────────


class TestFailureBackoff:
    def test_failed_capture_backs_off_instead_of_spinning(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path, monkeypatch)
        capture = MagicMock(return_value=False)
        sched = _make_scheduler(ProfileConfig(), state, capture)
        sleeps = []
        monkeypatch.setattr(sched, "_interruptible_sleep", sleeps.append)

        for _ in range(3):
            sched._tick()

        assert capture.call_count == 3
        assert sleeps == [10, 20, 40]

    def test_backoff_caps_and_resets_on_success(self, tmp_path, monkeypatch):
        from timelapse.scheduler.scheduler import FAILURE_BACKOFF_MAX_S

        state = _make_state(tmp_path, monkeypatch)
        sched = _make_scheduler(ProfileConfig(), state)
        sched._consecutive_failures = 50
        assert sched._failure_backoff_s() == FAILURE_BACKOFF_MAX_S

        sched._consecutive_failures = 3
        monkeypatch.setattr(sched, "_interruptible_sleep", lambda s: None)
        sched._tick()  # default capture_fn succeeds
        assert sched._consecutive_failures == 0
