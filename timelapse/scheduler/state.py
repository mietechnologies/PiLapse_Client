"""
Persistent state for a timelapse profile.

State file: ~/.local/share/timelapse/<profile>/state.json

Tracked:
  - last_capture_at      ISO-8601 timestamp of the most recent successful capture
  - photo_count          total photos taken in the current run
  - video_count          total videos generated
  - current_batch        photos since last video generation
  - status               running | completed | paused | error
  - solar_cache          cached sunrise/noon/sunset for today (avoids repeat API calls)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_STATE_DIR = Path.home() / ".local" / "share" / "timelapse"

Status = str  # "running" | "completed" | "paused" | "error"


def _state_path(profile: str) -> Path:
    return _STATE_DIR / profile / "state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class ProfileState:
    """
    Thread-safe JSON state for one profile.

    Reads lazily on first access, writes atomically on every mutation.
    """

    def __init__(self, profile: str) -> None:
        self._profile = profile
        self._path = _state_path(profile)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._loaded = False

    # ── private helpers ────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with self._path.open() as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        self._loaded = True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(self._data, f, indent=2)
        # Atomic replace
        os.replace(tmp, self._path)

    def _get(self, key: str, default: Any = None) -> Any:
        self._ensure_loaded()
        return self._data.get(key, default)

    def _set(self, key: str, value: Any) -> None:
        self._ensure_loaded()
        self._data[key] = value
        self._save()

    # ── public API ─────────────────────────────────────────────────────────

    def get_status(self) -> Status:
        with self._lock:
            return self._get("status", "running")

    def set_status(self, status: Status) -> None:
        with self._lock:
            self._set("status", status)

    def get_last_capture_time(self) -> Optional[datetime]:
        with self._lock:
            return _parse_dt(self._get("last_capture_at"))

    def record_capture(self, captured_at: Optional[datetime] = None) -> None:
        """Record a successful capture; increment counters."""
        with self._lock:
            self._ensure_loaded()
            ts = (captured_at or datetime.now(timezone.utc)).isoformat()
            self._data["last_capture_at"] = ts
            self._data["photo_count"] = self._data.get("photo_count", 0) + 1
            self._data["current_batch"] = self._data.get("current_batch", 0) + 1
            self._save()

    def get_photo_count(self) -> int:
        with self._lock:
            return self._get("photo_count", 0)

    def get_current_batch(self) -> int:
        """Photos accumulated since the last video generation."""
        with self._lock:
            return self._get("current_batch", 0)

    def reset_batch(self) -> None:
        with self._lock:
            self._set("current_batch", 0)

    def get_video_count(self) -> int:
        with self._lock:
            return self._get("video_count", 0)

    def record_video(self) -> None:
        with self._lock:
            self._ensure_loaded()
            self._data["video_count"] = self._data.get("video_count", 0) + 1
            self._data["current_batch"] = 0
            self._save()

    # ── solar cache ────────────────────────────────────────────────────────

    def get_solar_cache(self, date_str: str) -> Optional[dict[str, str]]:
        """
        Return cached solar times for `date_str` (YYYY-MM-DD), or None if stale/missing.
        """
        with self._lock:
            cache = self._get("solar_cache")
            if cache and cache.get("date") == date_str:
                return cache
            return None

    def set_solar_cache(self, date_str: str, times: dict[str, str]) -> None:
        """Cache solar times dict {sunrise, solar_noon, sunset} for a date."""
        with self._lock:
            self._ensure_loaded()
            self._data["solar_cache"] = {"date": date_str, **times}
            self._save()

    # ── anti-double-capture helpers ────────────────────────────────────────

    def was_recently_captured(self, cooldown_seconds: int = 60) -> bool:
        """
        Return True if a capture happened within `cooldown_seconds` ago.
        Prevents double-triggering on scheduler restart or clock skew.
        """
        last = self.get_last_capture_time()
        if last is None:
            return False
        now = datetime.now(timezone.utc)
        # Make last aware if it somehow stored naive
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        return elapsed < cooldown_seconds

    # ── diagnostics ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            return dict(self._data)

    def reset(self) -> None:
        """Wipe all state (use carefully)."""
        with self._lock:
            self._data = {"status": "running"}
            self._save()
