"""
Config models — validated with Pydantic v2.

Layout:
  ~/.config/timelapse/config.yaml          ← GlobalConfig
  ~/.config/timelapse/profiles/<name>.yaml ← ProfileConfig

Everything that is "per-camera-setup" lives in ProfileConfig.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

# ─── helpers ────────────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h|d)$", re.IGNORECASE)


def parse_interval_seconds(value: str) -> int:
    """Parse a human interval string ('30s', '5m', '2h', '1d') → seconds."""
    m = _INTERVAL_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"Invalid interval '{value}'. Use '<N>s', '<N>m', '<N>h', or '<N>d'."
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute).  Raises ValueError on bad input."""
    try:
        h, mm = value.strip().split(":")
        hour, minute = int(h), int(mm)
    except (ValueError, AttributeError):
        raise ValueError(f"Time '{value}' must be in HH:MM format (24 h).")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time '{value}' out of range.")
    return hour, minute


# ─── schedule models ────────────────────────────────────────────────────────
#
# A schedule is a list of rules; the camera fires at the earliest next time
# across all of them.  Each rule is identified by its key:
#
#   - at: ["07:30", "12:00"]                  fixed local times each day
#   - every: 5m                               fixed interval (aligned to the clock)
#     between: ["sunrise-30m", "sunset+30m"]  optional daily window
#   - sun: sunset                             sunrise | solar_noon | sunset
#     offset: -15m                            optional signed offset
#
# Every rule accepts an optional `days: [mon, tue, ...]` filter.

_OFFSET_RE = re.compile(r"^([+-])?\s*(\d+)(s|m|h)?$", re.IGNORECASE)
_SUN_EVENTS = ("sunrise", "solar_noon", "sunset")
_TIMESPEC_RE = re.compile(
    r"^(?:(?P<hhmm>\d{1,2}:\d{2})|(?P<sun>sunrise|solar_noon|sunset)\s*(?P<off>[+-]\s*\d+[smh]?)?)$",
    re.IGNORECASE,
)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SunEvent = Literal["sunrise", "solar_noon", "sunset"]


def parse_offset_seconds(value: "str | int") -> int:
    """Parse a signed offset ('-15m', '+1h', '30s', '0') → seconds.

    A bare number is minutes, so `offset: -15` in YAML means 15 minutes before.
    """
    if isinstance(value, bool):
        raise ValueError(f"Invalid offset '{value}'.")
    if isinstance(value, int):
        return value * 60
    m = _OFFSET_RE.match(str(value).strip())
    if not m:
        raise ValueError(
            f"Invalid offset '{value}'. Use e.g. '-15m', '+1h', '30s' or '0'."
        )
    sign = -1 if m.group(1) == "-" else 1
    n = int(m.group(2))
    unit = (m.group(3) or "m").lower()
    return sign * n * {"s": 1, "m": 60, "h": 3600}[unit]


class TimeSpec(BaseModel):
    """A time of day: either a clock time or a sun event with an offset."""

    hour: Optional[int] = None
    minute: Optional[int] = None
    sun: Optional[SunEvent] = None
    offset_s: int = 0

    @classmethod
    def parse(cls, value: str) -> "TimeSpec":
        m = _TIMESPEC_RE.match(str(value).strip())
        if not m:
            raise ValueError(
                f"Invalid time '{value}'. Use HH:MM or a sun event like "
                "'sunrise', 'sunset+30m', 'solar_noon-1h'."
            )
        if m.group("hhmm"):
            h, mm = parse_hhmm(m.group("hhmm"))
            return cls(hour=h, minute=mm)
        off = m.group("off")
        return cls(
            sun=m.group("sun").lower(),  # type: ignore[arg-type]
            offset_s=parse_offset_seconds(off.replace(" ", "")) if off else 0,
        )


def _yaml_time(v):
    """YAML 1.1 reads an unquoted 12:00 as the base-60 integer 720; undo that."""
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 24 * 60:
        return f"{v // 60:02d}:{v % 60:02d}"
    return v


def _validate_days(v: Optional[list[str]]) -> Optional[list[str]]:
    if v is None:
        return None
    out = []
    for d in v:
        key = str(d).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ValueError(f"Unknown day '{d}'. Use mon, tue, wed, thu, fri, sat, sun.")
        out.append(key)
    if not out:
        raise ValueError("days must list at least one day (or be omitted).")
    return out


class _RuleBase(BaseModel):
    model_config = {"extra": "forbid"}

    days: Optional[list[str]] = None

    @field_validator("days")
    @classmethod
    def validate_days(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return _validate_days(v)

    @property
    def weekdays(self) -> Optional[set[int]]:
        """Allowed weekdays as date.weekday() numbers, or None for every day."""
        if self.days is None:
            return None
        return {WEEKDAYS.index(d) for d in self.days}

    @property
    def uses_sun(self) -> bool:
        return False


class AtRule(_RuleBase):
    """Fire at one or more fixed local times every day."""

    at: list[str] = Field(min_length=1)

    @field_validator("at", mode="before")
    @classmethod
    def coerce_single(cls, v):
        v = [v] if isinstance(v, (str, int)) else v
        return [_yaml_time(t) for t in v] if isinstance(v, list) else v

    @field_validator("at")
    @classmethod
    def validate_times(cls, v: list[str]) -> list[str]:
        for t in v:
            parse_hhmm(t)
        return sorted(set(v))

    def describe(self) -> str:
        return "at " + ", ".join(self.at)


class EveryRule(_RuleBase):
    """Fire on a fixed interval, optionally only inside a daily window."""

    every: str
    between: Optional[list[str]] = None

    @field_validator("every")
    @classmethod
    def validate_every(cls, v: str) -> str:
        parse_interval_seconds(v)
        return v

    @field_validator("between", mode="before")
    @classmethod
    def coerce_yaml_times(cls, v):
        return [_yaml_time(t) for t in v] if isinstance(v, list) else v

    @field_validator("between")
    @classmethod
    def validate_between(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return None
        if len(v) != 2:
            raise ValueError("between must be [start, end], e.g. ['sunrise-30m', 'sunset+30m'].")
        for t in v:
            TimeSpec.parse(t)
        return v

    @property
    def interval_seconds(self) -> int:
        return parse_interval_seconds(self.every)

    @property
    def window(self) -> Optional[tuple[TimeSpec, TimeSpec]]:
        if self.between is None:
            return None
        return TimeSpec.parse(self.between[0]), TimeSpec.parse(self.between[1])

    @property
    def uses_sun(self) -> bool:
        w = self.window
        return bool(w and (w[0].sun or w[1].sun))

    def describe(self) -> str:
        s = f"every {self.every}"
        if self.between:
            s += f" between {self.between[0]} and {self.between[1]}"
        return s


class SunRule(_RuleBase):
    """Fire at a sun event (sunrise, solar noon, sunset) plus an offset."""

    sun: SunEvent
    offset: str = "0"

    @field_validator("offset", mode="before")
    @classmethod
    def validate_offset(cls, v) -> str:
        parse_offset_seconds(v)
        return str(v)

    @property
    def offset_seconds(self) -> int:
        return parse_offset_seconds(self.offset)

    @property
    def uses_sun(self) -> bool:
        return True

    def describe(self) -> str:
        off = self.offset_seconds
        if off == 0:
            return self.sun
        sign = "+" if off > 0 else "-"
        return f"{self.sun} {sign}{_fmt_seconds(abs(off))}"


def _fmt_seconds(s: int) -> str:
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


Rule = Union[AtRule, EveryRule, SunRule]


def _describe_days(rule: _RuleBase) -> str:
    return f" ({', '.join(rule.days)})" if rule.days else ""


class ScheduleConfig(BaseModel):
    rules: list[Rule] = Field(min_length=1, default_factory=lambda: [EveryRule(every="5m")])
    # After downtime (reboot, restart), a slot missed by at most this much is
    # still taken once; older misses are skipped.
    catch_up: str = "10m"

    @field_validator("catch_up")
    @classmethod
    def validate_catch_up(cls, v: str) -> str:
        if str(v).strip() not in ("0", "0s", "0m"):
            parse_interval_seconds(v)
        return v

    @property
    def catch_up_seconds(self) -> int:
        v = str(self.catch_up).strip()
        return 0 if v in ("0", "0s", "0m") else parse_interval_seconds(v)

    @property
    def uses_sun(self) -> bool:
        return any(r.uses_sun for r in self.rules)

    def describe(self) -> list[str]:
        return [r.describe() + _describe_days(r) for r in self.rules]

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy(cls, data):
        """Accept the 0.1.0 single-mode format (`mode: interval|times|solar`)."""
        if not isinstance(data, dict) or "mode" not in data:
            return data
        return {"rules": legacy_schedule_rules(data)}


def legacy_schedule_rules(data: dict) -> list[dict]:
    """Convert a 0.1.0 `schedule:` block to a list of rule dicts."""
    mode = data.get("mode")
    if mode == "interval":
        return [{"every": data.get("interval", "5m")}]
    if mode == "times":
        return [{"at": data.get("times", [])}]
    if mode == "solar":
        return [
            {"sun": e["type"], "offset": f"{int(e.get('offset_minutes', 0))}m"}
            for e in data.get("events", [])
        ]
    raise ValueError(f"Unknown legacy schedule mode '{mode}'.")


# ─── location & weather ─────────────────────────────────────────────────────


class LocationConfig(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: Optional[str] = None
    # How the location was set: ip | search | manual | legacy (informational)
    source: str = "manual"


class WeatherConfig(BaseModel):
    """Record current weather (OpenWeatherMap) alongside each photo."""

    enabled: bool = False
    provider: Literal["openweathermap"] = "openweathermap"
    # Name of the env var holding the API key — never the key itself
    api_key_env: str = "TIMELAPSE_OWM_KEY"
    refresh: str = "10m"
    units: Literal["metric", "imperial"] = "metric"

    @field_validator("refresh")
    @classmethod
    def validate_refresh(cls, v: str) -> str:
        if parse_interval_seconds(v) < 60:
            raise ValueError("weather.refresh must be at least 1m.")
        return v

    @property
    def refresh_seconds(self) -> int:
        return parse_interval_seconds(self.refresh)


# ─── capture ────────────────────────────────────────────────────────────────


class CaptureConfig(BaseModel):
    width: int = Field(1920, ge=320, le=4056)
    height: int = Field(1080, ge=240, le=3040)
    jpeg_quality: int = Field(85, ge=1, le=100)
    # Timeout for a single still capture (seconds)
    capture_timeout_s: int = Field(10, ge=1, le=60)


# ─── disk / retention ───────────────────────────────────────────────────────


class RetentionConfig(BaseModel):
    # oldest_first: delete the oldest files regardless of type
    # oldest_videos_first: prefer deleting old videos before photos
    policy: Literal["oldest_first", "oldest_videos_first"] = "oldest_first"


class DiskConfig(BaseModel):
    min_free_gb: float = Field(3.0, ge=0.1)
    check_interval_s: int = Field(300, ge=30)  # how often to check free space
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


# ─── video generation ───────────────────────────────────────────────────────


class VideoConfig(BaseModel):
    photos_per_video: int = Field(100, ge=2)
    fps: int = Field(10, ge=1, le=120)
    repeat: bool = True
    # What to do with source photos after a video is generated:
    #   move_to_archive  → move to archive_dir (safest)
    #   delete           → permanently delete
    #   keep             → leave in place (photos_dir fills up)
    after_video: Literal["move_to_archive", "delete", "keep"] = "move_to_archive"
    # ffmpeg video codec for output (libx264 is universally compatible)
    video_codec: str = "libx264"
    crf: int = Field(23, ge=0, le=51)  # quality; lower = better, larger file


# ─── streaming: web preview ─────────────────────────────────────────────────


class AuthConfig(BaseModel):
    enabled: bool = False
    username: str = "timelapse"
    # Password is read from this env var at runtime (never stored in config)
    password_env: str = "TIMELAPSE_WEB_PASSWORD"


class PreviewConfig(BaseModel):
    enabled: bool = True
    # auto: use HLS on Pi Zero 2 W or better; fall back to MJPEG on Zero W
    mode: Literal["auto", "hls", "mjpeg"] = "auto"
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1024, le=65535)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # HLS-specific (Pi Zero 2 W / better)
    hls_width: int = Field(1280, ge=320)
    hls_height: int = Field(720, ge=240)
    hls_fps: int = Field(15, ge=1, le=30)
    hls_segment_seconds: int = Field(2, ge=1, le=10)
    hls_list_size: int = Field(5, ge=2, le=20)
    # MJPEG-specific (Pi Zero W fallback)
    mjpeg_width: int = Field(640, ge=160)
    mjpeg_height: int = Field(360, ge=120)
    mjpeg_fps: float = Field(1.0, ge=0.1, le=10.0)


# ─── streaming: RTMP ────────────────────────────────────────────────────────


class RtmpConfig(BaseModel):
    enabled: bool = False
    rtmp_url: str = "rtmp://a.rtmp.youtube.com/live2"
    # Name of the env var holding the stream key — NEVER store the key itself
    stream_key_env: str = "TIMELAPSE_RTMP_KEY_DEFAULT"
    # Optional secrets file path (alternative to env var; must be chmod 0600)
    secrets_file: Optional[str] = None
    video_bitrate: str = "1000k"
    audio_enabled: bool = False
    width: int = Field(1280, ge=320)
    height: int = Field(720, ge=240)
    fps: int = Field(30, ge=1, le=60)

    @field_validator("rtmp_url")
    @classmethod
    def must_be_rtmp(cls, v: str) -> str:
        if not (v.startswith("rtmp://") or v.startswith("rtmps://")):
            raise ValueError("rtmp_url must start with rtmp:// or rtmps://")
        return v

    @field_validator("video_bitrate")
    @classmethod
    def valid_bitrate(cls, v: str) -> str:
        if not re.match(r"^\d+[kKmM]?$", v):
            raise ValueError("video_bitrate must be e.g. '1000k', '2M'")
        return v


class StreamingConfig(BaseModel):
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    rtmp: RtmpConfig = Field(default_factory=RtmpConfig)


# ─── top-level profile config ────────────────────────────────────────────────


class ProfileConfig(BaseModel):
    # Will be expanded with str.format(profile=<name>) at load time
    photos_dir: str = "~/timelapse/{profile}/photos"
    videos_dir: str = "~/timelapse/{profile}/videos"
    archive_dir: str = "~/timelapse/{profile}/archive"

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    # Optional per-profile override of the device location (global config).
    # Set automatically when loading a 0.1.0 solar schedule.
    location: Optional[LocationConfig] = None
    disk: DiskConfig = Field(default_factory=DiskConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)

    # Only used when this device is connected to a PiLapse server.
    upload_to_server: bool = True

    @model_validator(mode="before")
    @classmethod
    def lift_legacy_solar_location(cls, data):
        """0.1.0 kept lat/lon inside a solar schedule; move it to `location`."""
        if not isinstance(data, dict):
            return data
        sched = data.get("schedule")
        if isinstance(sched, dict) and sched.get("mode") == "solar" and "latitude" in sched:
            data = dict(data)
            data.setdefault(
                "location",
                {
                    "latitude": sched["latitude"],
                    "longitude": sched["longitude"],
                    "source": "legacy",
                },
            )
        return data

    @model_validator(mode="after")
    def validate_rtmp_key_env_name(self) -> "ProfileConfig":
        """Warn if RTMP env var name still uses the placeholder profile name."""
        return self

    def resolve_dirs(self, profile_name: str) -> "ProfileConfig":
        """Return a copy with {profile} placeholders expanded and ~ resolved."""
        kwargs = self.model_dump()
        for key in ("photos_dir", "videos_dir", "archive_dir"):
            raw = kwargs[key]
            expanded = Path(raw.format(profile=profile_name)).expanduser()
            kwargs[key] = str(expanded)
        return ProfileConfig.model_validate(kwargs)

    @property
    def photos_path(self) -> Path:
        return Path(self.photos_dir)

    @property
    def videos_path(self) -> Path:
        return Path(self.videos_dir)

    @property
    def archive_path(self) -> Path:
        return Path(self.archive_dir)


# ─── server connection ──────────────────────────────────────────────────────


class ServerConfig(BaseModel):
    """Written by `timelapse server connect`; one per device."""

    url: str
    pi_id: str
    pi_name: str = ""
    # The device token itself is never stored here.
    token_env: str = "TIMELAPSE_SERVER_TOKEN"
    secrets_file: Optional[str] = "~/.config/timelapse/secrets/server.env"
    timeout_s: int = Field(30, ge=1, le=300)

    @field_validator("url")
    @classmethod
    def http_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("Server URL must start with http:// or https://")
        return v


# ─── global config ──────────────────────────────────────────────────────────


class GlobalConfig(BaseModel):
    # None → detect system timezone at runtime via tzlocal
    timezone: Optional[str] = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "~/.local/share/timelapse/logs"
    server: Optional[ServerConfig] = None

    # Device location — used by sun-based rules and weather
    location: Optional[LocationConfig] = None
    weather: WeatherConfig = Field(default_factory=WeatherConfig)

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser()
