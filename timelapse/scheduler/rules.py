"""
Rule engine: given a rule and a moment, when does it fire next?

Everything here is pure (no threads, no camera, no clock reads), so it can be
tested with plain datetimes and reused by `status` / `schedule preview`.

All results are timezone-aware.  Local wall-clock rules (`at`, `every`
grids, `between` windows) are evaluated in `tz` with zoneinfo, so DST
changes are handled by the timezone database.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, Optional

from timelapse.config.models import (
    AtRule,
    EveryRule,
    LocationConfig,
    Rule,
    ScheduleConfig,
    SunRule,
    TimeSpec,
    parse_hhmm,
)
from timelapse.scheduler.solar import get_solar_times

logger = logging.getLogger(__name__)

# How far ahead to search for the next slot.  Long enough to get through a
# polar night with sun-based rules.
MAX_SEARCH_DAYS = 370

# Fixed anchor for `every` rules without a window.  Interval slots are laid
# out on a wall-clock grid from this midnight, so 5s / 30s / 5m rules line up
# with each other and with round clock times.
_GRID_ANCHOR = datetime(2000, 1, 3)  # a Monday, local wall time


class ScheduleError(Exception):
    """Raised when a schedule can't be evaluated (e.g. sun rule, no location)."""


# ─── public API ──────────────────────────────────────────────────────────────


def next_after(
    rule: Rule, after: datetime, tz, location: Optional[LocationConfig]
) -> Optional[datetime]:
    """First time strictly after `after` at which `rule` fires, or None."""
    # Compare in UTC: aware datetimes sharing a tzinfo compare by wall time
    # and ignore `fold`, which is wrong across DST changes.
    after = after.astimezone(timezone.utc)
    result = _next_after_utc(rule, after, tz, location)
    return result.astimezone(tz) if result is not None else None


def _next_after_utc(
    rule: Rule, after: datetime, tz, location: Optional[LocationConfig]
) -> Optional[datetime]:
    if rule.uses_sun and location is None:
        raise ScheduleError(
            f"Rule '{rule.describe()}' uses sunrise/sunset but no location is set. "
            "Run `timelapse setup` to set the device location."
        )
    if isinstance(rule, EveryRule):
        if rule.between is None:
            return _next_grid(rule.interval_seconds, after, tz, rule.weekdays)
        return _next_windowed(rule, after, tz, location)
    if isinstance(rule, (AtRule, SunRule)):
        return _next_daily(rule, after, tz, location)
    raise ScheduleError(f"Unknown rule type: {type(rule).__name__}")


def next_fire(
    schedule: ScheduleConfig, after: datetime, tz, location: Optional[LocationConfig]
) -> Optional[datetime]:
    """Earliest next firing across all rules of a schedule, or None."""
    times = [
        t.astimezone(timezone.utc)
        for r in schedule.rules
        if (t := next_after(r, after, tz, location))
    ]
    return min(times).astimezone(tz) if times else None


def upcoming(
    schedule: ScheduleConfig,
    start: datetime,
    tz,
    location: Optional[LocationConfig],
    *,
    until: Optional[datetime] = None,
    limit: int = 20,
) -> Iterator[tuple[datetime, list[Rule]]]:
    """Yield (time, rules firing then) from `start`, up to `limit` entries."""
    t = start
    for _ in range(limit):
        per_rule = [(next_after(r, t, tz, location), r) for r in schedule.rules]
        # Compare in UTC (see next_after)
        per_rule = [(dt.astimezone(timezone.utc), r) for dt, r in per_rule if dt is not None]
        if not per_rule:
            return
        first = min(dt for dt, _ in per_rule)
        if until is not None and first > until:
            return
        yield first.astimezone(tz), [r for dt, r in per_rule if dt == first]
        t = first


def validate_schedule(schedule: ScheduleConfig, location: Optional[LocationConfig]) -> list[str]:
    """Human-readable problems that would stop the schedule from running."""
    issues = []
    if schedule.uses_sun and location is None:
        issues.append(
            "Schedule uses sunrise/sunset but no location is set. "
            "Run `timelapse setup` (or add `location:` to config.yaml)."
        )
    return issues


# ─── helpers ─────────────────────────────────────────────────────────────────


def _day_allowed(rule: Rule, d: date) -> bool:
    days = rule.weekdays
    return days is None or d.weekday() in days


def _local(d: date, t: time, tz) -> datetime:
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


def _local_date(dt: datetime, tz) -> date:
    return dt.astimezone(tz).date()


def _sun_time(
    event: str, offset_s: int, d: date, tz, location: LocationConfig
) -> tuple[Optional[datetime], Optional[str]]:
    sol = get_solar_times(location.latitude, location.longitude, d, tz)
    base = sol.get(event)
    if base is None:
        return None, sol.polar
    base = base.replace(microsecond=0).astimezone(timezone.utc)
    return base + timedelta(seconds=offset_s), sol.polar


def _resolve_spec(
    spec: TimeSpec, d: date, tz, location: Optional[LocationConfig]
) -> tuple[Optional[datetime], Optional[str]]:
    if spec.sun is None:
        return _local(d, time(spec.hour, spec.minute), tz), None
    assert location is not None
    return _sun_time(spec.sun, spec.offset_s, d, tz, location)


# ─── at / sun ────────────────────────────────────────────────────────────────


def _daily_candidates(
    rule: "AtRule | SunRule", d: date, tz, location: Optional[LocationConfig]
) -> list[datetime]:
    if isinstance(rule, AtRule):
        return [_local(d, time(*parse_hhmm(t)), tz) for t in rule.at]
    assert location is not None
    dt, _ = _sun_time(rule.sun, rule.offset_seconds, d, tz, location)
    return [dt] if dt else []


def _next_daily(
    rule: "AtRule | SunRule", after: datetime, tz, location: Optional[LocationConfig]
) -> Optional[datetime]:
    # Start a day early: a negative offset or the DST gap can push a slot
    # belonging to yesterday's date past `after`.
    d0 = _local_date(after, tz) - timedelta(days=1)
    for i in range(MAX_SEARCH_DAYS + 1):
        d = d0 + timedelta(days=i)
        if not _day_allowed(rule, d):
            continue
        for cand in sorted(_daily_candidates(rule, d, tz, location)):
            if cand > after:
                return cand
    return None


# ─── every (no window): wall-clock grid ─────────────────────────────────────


def _next_grid(
    step_s: int, after: datetime, tz, weekdays: Optional[set[int]] = None
) -> Optional[datetime]:
    step = timedelta(seconds=step_s)
    after_naive = after.astimezone(tz).replace(tzinfo=None)
    limit = after_naive + timedelta(days=MAX_SEARCH_DAYS)

    # Around a DST change wall-clock slots don't map to UTC in order (a
    # repeated hour, or a skipped one), so scan a couple of hours either
    # side and keep the earliest real instant.  Otherwise the first slot
    # after `after` is the answer.
    near_dst = (after - timedelta(hours=3)).astimezone(tz).utcoffset() != (
        after + timedelta(hours=3)
    ).astimezone(tz).utcoffset()
    back = math.ceil(7200 / step_s) + 1 if near_dst else 0
    horizon = after_naive + timedelta(hours=2)

    k = math.floor((after_naive - _GRID_ANCHOR) / step) + 1 - back
    best: Optional[datetime] = None
    while True:
        naive = _GRID_ANCHOR + k * step
        if naive > limit:
            return best
        if best is not None and (not near_dst or naive > horizon):
            return best
        if weekdays is not None and naive.date().weekday() not in weekdays:
            # Jump to the first slot of the next day instead of walking it.
            next_midnight = datetime.combine(naive.date() + timedelta(days=1), time())
            k = max(k + 1, math.ceil((next_midnight - _GRID_ANCHOR) / step))
            continue
        for fold in (0, 1):
            cand = naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
            if cand > after and (best is None or cand < best):
                best = cand
        k += 1


# ─── every between start and end ────────────────────────────────────────────


def _window(
    rule: EveryRule, d: date, tz, location: Optional[LocationConfig]
) -> Optional[tuple[datetime, datetime]]:
    start_spec, end_spec = rule.window  # type: ignore[misc]
    ws, polar_s = _resolve_spec(start_spec, d, tz, location)
    we, polar_e = _resolve_spec(end_spec, d, tz, location)
    if ws is None or we is None:
        polar = polar_s or polar_e
        if polar != "day":
            return None  # polar night: the sun-based window never opens
        # Polar day: the missing sun edge becomes the edge of the day
        if ws is None:
            ws = _local(d, time(), tz)
        if we is None:
            we = _local(d + timedelta(days=1), time(), tz)
    if we <= ws:
        # Window crosses midnight (e.g. between 22:00 and 02:00)
        if end_spec.sun is None:
            we = _local(d + timedelta(days=1), time(end_spec.hour, end_spec.minute), tz)
        else:
            we, _ = _resolve_spec(end_spec, d + timedelta(days=1), tz, location)
            if we is None or we <= ws:
                return None
    return ws, we


def _next_windowed(
    rule: EveryRule, after: datetime, tz, location: Optional[LocationConfig]
) -> Optional[datetime]:
    # Slots inside the window sit on the same wall-clock grid as un-windowed
    # rules, so "every 5m between sunrise and sunset" fires at :00, :05, ...
    # and lines up with other intents.
    step_s = rule.interval_seconds
    d0 = _local_date(after, tz) - timedelta(days=1)
    for i in range(MAX_SEARCH_DAYS + 1):
        d = d0 + timedelta(days=i)
        if not _day_allowed(rule, d):
            continue
        win = _window(rule, d, tz, location)
        if win is None:
            continue
        ws, we = win
        if we <= after:
            continue
        cand = _next_grid(step_s, max(after, ws - timedelta(microseconds=1)), tz)
        if cand is not None and cand <= we:
            return cand
    return None
