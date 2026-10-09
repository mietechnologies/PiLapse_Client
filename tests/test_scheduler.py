"""
Unit tests for the rule engine and scheduler.

Everything runs offline with explicit datetimes — no camera, no network,
no real clock, no Raspberry Pi.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from timelapse.config.models import LocationConfig, ScheduleConfig
from timelapse.scheduler.rules import ScheduleError, next_fire, upcoming
from timelapse.scheduler.scheduler import Intent, Scheduler
from timelapse.scheduler.solar import get_solar_times
from timelapse.scheduler.state import ProfileState

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
FORT_WAYNE = LocationConfig(latitude=41.08, longitude=-85.14)


def _sched(*rules, **kw) -> ScheduleConfig:
    return ScheduleConfig.model_validate({"rules": list(rules), **kw})


def _times(schedule, start, n, tz=NY, location=FORT_WAYNE):
    return [t for t, _ in upcoming(schedule, start, tz, location, limit=n)]


# ─── rules ─────────────────────────────────────────────────────────────────


class TestAtRule:
    def test_picks_next_slot_today(self):
        s = _sched({"at": ["08:00", "12:00", "17:00"]})
        nxt = next_fire(s, datetime(2026, 1, 15, 10, 0, tzinfo=NY), NY, None)
        assert nxt == datetime(2026, 1, 15, 12, 0, tzinfo=NY)

    def test_rolls_to_tomorrow(self):
        s = _sched({"at": ["08:00", "17:00"]})
        nxt = next_fire(s, datetime(2026, 1, 15, 18, 0, tzinfo=NY), NY, None)
        assert nxt == datetime(2026, 1, 16, 8, 0, tzinfo=NY)

    def test_strictly_after(self):
        s = _sched({"at": ["08:00"]})
        nxt = next_fire(s, datetime(2026, 1, 15, 8, 0, tzinfo=NY), NY, None)
        assert nxt == datetime(2026, 1, 16, 8, 0, tzinfo=NY)

    def test_days_filter(self):
        s = _sched({"at": ["08:00"], "days": ["mon"]})
        # 2026-01-15 is a Thursday → next Monday is the 19th
        nxt = next_fire(s, datetime(2026, 1, 15, 9, 0, tzinfo=NY), NY, None)
        assert nxt == datetime(2026, 1, 19, 8, 0, tzinfo=NY)

    def test_fall_back_fires_once(self):
        s = _sched({"at": ["01:30"]})
        times = _times(s, datetime(2026, 11, 1, 0, 0, tzinfo=NY), 2)
        assert [t.isoformat() for t in times] == [
            "2026-11-01T01:30:00-04:00",
            "2026-11-02T01:30:00-05:00",
        ]


class TestEveryRule:
    def test_aligned_to_clock(self):
        s = _sched({"every": "5m"})
        nxt = next_fire(s, datetime(2026, 1, 15, 10, 2, 13, tzinfo=NY), NY, None)
        assert nxt == datetime(2026, 1, 15, 10, 5, tzinfo=NY)

    def test_intents_line_up(self):
        """5 s and 30 s intents share every 30 s slot."""
        start = datetime(2026, 1, 15, 12, 0, 1, tzinfo=NY)
        fast = set(_times(_sched({"every": "5s"}), start, 12))
        slow = _times(_sched({"every": "30s"}), start, 2)
        assert all(t in fast for t in slow)

    def test_multi_day_interval(self):
        s = _sched({"every": "2d"})
        times = _times(s, datetime(2026, 1, 15, 12, tzinfo=NY), 3)
        gaps = {(b - a) for a, b in zip(times, times[1:])}
        assert gaps == {timedelta(days=2)}

    def test_fall_back_keeps_both_hours(self):
        s = _sched({"every": "30m"})
        # Subtract in UTC: same-tzinfo subtraction uses wall time
        times = [t.astimezone(UTC) for t in _times(s, datetime(2026, 11, 1, 0, 10, tzinfo=NY), 6)]
        gaps = {(b - a) for a, b in zip(times, times[1:])}
        assert gaps == {timedelta(minutes=30)}

    def test_spring_forward_no_duplicates(self):
        s = _sched({"every": "30m"})
        times = _times(s, datetime(2026, 3, 8, 1, 10, tzinfo=NY), 4)
        assert times == sorted(set(times))
        assert times[1] == datetime(2026, 3, 8, 3, 0, tzinfo=NY)

    def test_clock_window(self):
        s = _sched({"every": "1h", "between": ["08:00", "10:00"]})
        times = _times(s, datetime(2026, 1, 15, 0, 0, tzinfo=NY), 4)
        assert [t.hour for t in times] == [8, 9, 10, 8]

    def test_window_across_midnight(self):
        s = _sched({"every": "2h", "between": ["22:00", "02:00"]})
        times = _times(s, datetime(2026, 1, 15, 12, 0, tzinfo=NY), 4)
        assert [t.hour for t in times] == [22, 0, 2, 22]

    def test_sun_window(self):
        s = _sched({"every": "1h", "between": ["sunrise", "sunset"]})
        start = datetime(2026, 1, 15, 0, 0, tzinfo=NY)
        times = _times(s, start, 30)
        sol = get_solar_times(FORT_WAYNE.latitude, FORT_WAYNE.longitude, date(2026, 1, 15), NY)
        today = [t for t in times if t.date() == date(2026, 1, 15)]
        assert today and all(sol.sunrise <= t <= sol.sunset for t in today)
        assert all(t.minute == 0 and t.second == 0 for t in times)


class TestSunRule:
    def test_offset_applied(self):
        s = _sched({"sun": "sunrise", "offset": "+10m"})
        sol = get_solar_times(FORT_WAYNE.latitude, FORT_WAYNE.longitude, date(2026, 1, 15), NY)
        nxt = next_fire(s, datetime(2026, 1, 15, 0, 0, tzinfo=NY), NY, FORT_WAYNE)
        assert nxt == sol.sunrise.replace(microsecond=0) + timedelta(minutes=10)

    def test_needs_location(self):
        with pytest.raises(ScheduleError):
            next_fire(_sched({"sun": "sunset"}), datetime(2026, 1, 15, tzinfo=NY), NY, None)

    def test_high_latitude_summer(self):
        """Tampere at midsummer: twilight never ends, but sunrise still exists."""
        hel = ZoneInfo("Europe/Helsinki")
        tampere = LocationConfig(latitude=61.5, longitude=23.8)
        s = _sched({"sun": "sunrise"})
        nxt = next_fire(s, datetime(2026, 6, 21, 0, 0, tzinfo=hel), hel, tampere)
        assert nxt is not None and nxt.date() == date(2026, 6, 21)

    def test_polar_day_skips_to_first_sunrise(self):
        osl = ZoneInfo("Europe/Oslo")
        svalbard = LocationConfig(latitude=78.2, longitude=15.6)
        nxt = next_fire(_sched({"sun": "sunrise"}), datetime(2026, 6, 21, tzinfo=osl), osl, svalbard)
        assert nxt is not None and nxt.month == 8

    def test_polar_day_window_is_whole_day(self):
        osl = ZoneInfo("Europe/Oslo")
        svalbard = LocationConfig(latitude=78.2, longitude=15.6)
        s = _sched({"every": "6h", "between": ["sunrise", "sunset"]})
        times = _times(s, datetime(2026, 6, 21, 1, tzinfo=osl), 3, osl, svalbard)
        assert [t.hour for t in times] == [6, 12, 18]


# ─── scheduler ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _state(tmp_path, monkeypatch, name="s"):
    monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path)
    return ProfileState(name)


def _scheduler(intents, clock, on_due=None):
    return Scheduler(intents, on_due or MagicMock(), UTC, now_fn=clock)


class TestScheduler:
    def test_sleeps_until_next_slot(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_last_slot(clock.now)
        on_due = MagicMock()
        sched = _scheduler([Intent("a", _sched({"every": "5m"}), st)], clock, on_due)
        assert sched.tick() == 30.0  # capped chunk
        clock.now += timedelta(minutes=4, seconds=50)
        assert sched.tick() == pytest.approx(10.0)
        on_due.assert_not_called()

    def test_failed_capture_does_not_spin(self, tmp_path, monkeypatch):
        """A failing capture marks the slot handled; it is not retried at once."""
        clock = FakeClock(datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_last_slot(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        on_due = MagicMock(side_effect=RuntimeError("disk full"))
        sched = _scheduler([Intent("a", _sched({"every": "5m"}), st)], clock, on_due)
        assert sched.tick() == 0.0
        assert on_due.call_count == 1
        assert sched.tick() > 0  # sleeps until 10:10 instead of retrying
        assert on_due.call_count == 1

    def test_catch_up_once_after_downtime(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 7, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_last_slot(datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC))
        on_due = MagicMock()
        sched = _scheduler([Intent("a", _sched({"every": "1m"}), st)], clock, on_due)
        assert sched.tick() == 0.0  # the 09:58 slot is within catch_up (10m)
        assert on_due.call_count == 1
        assert sched.tick() > 0  # but only once, not once per missed minute
        assert on_due.call_count == 1

    def test_no_catch_up_for_old_misses(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 12, 0, 30, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_last_slot(datetime(2026, 1, 14, 12, 0, 0, tzinfo=UTC))
        on_due = MagicMock()
        sched = _scheduler([Intent("a", _sched({"at": ["08:00"]}), st)], clock, on_due)
        assert sched.tick() > 0
        on_due.assert_not_called()

    def test_coalesces_intents(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 0, 30, tzinfo=UTC))
        fast = _state(tmp_path, monkeypatch, "fast")
        slow = _state(tmp_path, monkeypatch, "slow")
        fast.set_last_slot(datetime(2026, 1, 15, 10, 0, 25, tzinfo=UTC))
        slow.set_last_slot(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        calls = []
        sched = _scheduler(
            [Intent("fast", _sched({"every": "5s"}), fast), Intent("slow", _sched({"every": "30s"}), slow)],
            clock,
            lambda intents, slot: calls.append(sorted(i.name for i in intents)),
        )
        sched.tick()
        assert calls == [["fast", "slow"]]
        clock.now += timedelta(seconds=5)
        sched.tick()
        assert calls[-1] == ["fast"]

    def test_busy_camera_skips_instead_of_queueing(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_last_slot(datetime(2026, 1, 15, 9, 59, 55, tzinfo=UTC))

        def slow_capture(intents, slot):
            clock.now += timedelta(seconds=12)  # capture longer than the interval

        sched = _scheduler([Intent("a", _sched({"every": "5s"}), st)], clock, slow_capture)
        assert sched.tick() == 0.0  # fires 10:00:00, returns at 10:00:12
        delay = sched.tick()
        assert delay == pytest.approx(3.0)  # next is 10:00:15, the missed ones are dropped
        assert st.get_skipped_count() == 1

    def test_paused_intent_is_ignored(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        st.set_status("paused")
        on_due = MagicMock()
        sched = _scheduler([Intent("a", _sched({"every": "5s"}), st)], clock, on_due)
        assert sched.tick() == 300.0
        on_due.assert_not_called()

    def test_missing_location_logs_and_idles(self, tmp_path, monkeypatch):
        clock = FakeClock(datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))
        st = _state(tmp_path, monkeypatch)
        sched = _scheduler([Intent("a", _sched({"sun": "sunset"}), st)], clock)
        assert sched.tick() == 300.0


class TestClock:
    def test_unknown_status_does_not_block(self, monkeypatch):
        import threading

        from timelapse.scheduler import clock

        monkeypatch.setattr(clock, "is_clock_synced", lambda: None)
        assert clock.wait_for_clock_sync(threading.Event(), 300) is True

    def test_waits_until_synced(self, monkeypatch):
        import threading

        from timelapse.scheduler import clock

        answers = iter([False, False, True])
        monkeypatch.setattr(clock, "is_clock_synced", lambda: next(answers))
        monkeypatch.setattr(clock, "POLL_S", 0.01)
        assert clock.wait_for_clock_sync(threading.Event(), 5) is True


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
