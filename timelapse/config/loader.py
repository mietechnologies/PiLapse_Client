"""
Config loader — reads global config and per-profile YAML files.

Paths:
  ~/.config/timelapse/config.yaml          ← GlobalConfig
  ~/.config/timelapse/profiles/<name>.yaml ← ProfileConfig
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from timelapse.config.models import GlobalConfig, ProfileConfig

# ─── paths ──────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "timelapse"
GLOBAL_CONFIG_PATH = CONFIG_DIR / "config.yaml"
PROFILES_DIR = CONFIG_DIR / "profiles"
DEFAULT_PROFILE = "default"


def config_dir() -> Path:
    return CONFIG_DIR


def profiles_dir() -> Path:
    return PROFILES_DIR


def global_config_path() -> Path:
    return GLOBAL_CONFIG_PATH


def profile_config_path(profile: str) -> Path:
    return PROFILES_DIR / f"{profile}.yaml"


# ─── ensure dirs ─────────────────────────────────────────────────────────────


def ensure_config_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


# ─── loaders ────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    """Load a YAML file; return empty dict if the file does not exist."""
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f)
    return data or {}


def load_global_config() -> GlobalConfig:
    """Load and validate the global config file."""
    data = _load_yaml(GLOBAL_CONFIG_PATH)
    try:
        return GlobalConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"Global config validation failed ({GLOBAL_CONFIG_PATH}):\n{exc}"
        ) from exc


def load_profile_config(profile: str = DEFAULT_PROFILE) -> ProfileConfig:
    """
    Load and validate a profile config, then resolve {profile} placeholders.
    Raises ConfigError on missing file or validation failure.
    """
    path = profile_config_path(profile)
    if not path.exists():
        raise ConfigError(
            f"Profile '{profile}' not found at {path}.\n"
            f"Run `timelapse setup --profile {profile}` to create it."
        )
    data = _load_yaml(path)
    try:
        cfg = ProfileConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"Profile '{profile}' config validation failed ({path}):\n{exc}"
        ) from exc
    return cfg.resolve_dirs(profile)


def load_profile_config_or_none(profile: str = DEFAULT_PROFILE) -> Optional[ProfileConfig]:
    """Same as load_profile_config but returns None instead of raising."""
    try:
        return load_profile_config(profile)
    except ConfigError:
        return None


def list_profiles() -> list[str]:
    """Return sorted list of profile names (without .yaml extension)."""
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


# ─── writers ────────────────────────────────────────────────────────────────


def save_global_config(cfg: GlobalConfig) -> None:
    ensure_config_dirs()
    data = cfg.model_dump(exclude_none=True)
    _write_yaml(GLOBAL_CONFIG_PATH, data)


def save_profile_config(profile: str, cfg: ProfileConfig) -> None:
    ensure_config_dirs()
    data = cfg.model_dump(exclude_none=True)
    _write_yaml(profile_config_path(profile), data)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    shutil.move(str(tmp), str(path))


# ─── validation helper ───────────────────────────────────────────────────────


def validate_config(profile: str = DEFAULT_PROFILE) -> list[str]:
    """
    Full validation of global + profile config.
    Returns list of human-readable error strings (empty = all good).
    """
    errors: list[str] = []

    try:
        load_global_config()
    except ConfigError as e:
        errors.append(str(e))

    try:
        cfg = load_profile_config(profile)
        # Extra semantic checks
        errors.extend(_semantic_checks(cfg, profile))
    except ConfigError as e:
        errors.append(str(e))

    return errors


def _semantic_checks(cfg: ProfileConfig, profile: str) -> list[str]:
    """Business-logic checks beyond pydantic field validation."""
    issues: list[str] = []

    # Check that HLS/RTMP won't conflict with preview mode
    if cfg.streaming.rtmp.enabled and cfg.streaming.preview.enabled:
        if cfg.streaming.preview.mode == "hls":
            issues.append(
                "Warning: running both HLS preview and RTMP simultaneously is very "
                "CPU-intensive on Pi Zero W. Consider MJPEG preview or disabling one."
            )

    # Solar schedule needs lat/lon
    from timelapse.config.models import SolarSchedule

    if isinstance(cfg.schedule, SolarSchedule):
        if cfg.schedule.latitude == 0 and cfg.schedule.longitude == 0:
            issues.append(
                "Solar schedule: latitude/longitude are both 0 — likely a placeholder. "
                "Update with your real coordinates."
            )

    # photos_per_video sanity
    if cfg.video.photos_per_video < 10:
        issues.append(
            f"video.photos_per_video={cfg.video.photos_per_video} is very small; "
            "videos will be very short. Consider >= 30."
        )

    return issues


# ─── error type ─────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Raised when config is missing, unreadable, or invalid."""
