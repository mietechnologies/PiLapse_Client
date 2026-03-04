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
from typing import Annotated, Literal, Optional, Union

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


class IntervalSchedule(BaseModel):
    mode: Literal["interval"] = "interval"
    interval: str = "5m"

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        parse_interval_seconds(v)  # raises if invalid
        return v

    @property
    def interval_seconds(self) -> int:
        return parse_interval_seconds(self.interval)


class TimesSchedule(BaseModel):
    mode: Literal["times"] = "times"
    times: list[str] = Field(min_length=1)

    @field_validator("times")
    @classmethod
    def validate_times(cls, v: list[str]) -> list[str]:
        for t in v:
            parse_hhmm(t)
        return sorted(v)  # keep sorted for deterministic next-time calculation


class SolarEvent(BaseModel):
    type: Literal["sunrise", "solar_noon", "sunset"]
    offset_minutes: int = 0


class SolarSchedule(BaseModel):
    mode: Literal["solar"] = "solar"
    events: list[SolarEvent] = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    owm_api_key_env: str = "TIMELAPSE_OWM_KEY"

    @field_validator("events")
    @classmethod
    def no_duplicate_event_types(cls, v: list[SolarEvent]) -> list[SolarEvent]:
        seen: set[str] = set()
        for e in v:
            key = f"{e.type}_{e.offset_minutes}"
            if key in seen:
                raise ValueError(f"Duplicate solar event: {e.type} @{e.offset_minutes}m")
            seen.add(key)
        return v


Schedule = Annotated[
    Union[IntervalSchedule, TimesSchedule, SolarSchedule],
    Field(discriminator="mode"),
]


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
    schedule: Schedule = Field(default_factory=IntervalSchedule)
    disk: DiskConfig = Field(default_factory=DiskConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)

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


# ─── global config ──────────────────────────────────────────────────────────


class GlobalConfig(BaseModel):
    # None → detect system timezone at runtime via tzlocal
    timezone: Optional[str] = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "~/.local/share/timelapse/logs"

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser()
