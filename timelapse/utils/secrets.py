"""
Secret resolution for stream keys and passwords.

Priority order:
  1. Environment variable (recommended — works with systemd EnvironmentFile)
  2. Secrets file  (~/.config/timelapse/secrets/<profile>.env, must be 0600)

Tradeoffs
---------
Env var (default):
  + Never written to disk by this app
  + Standard pattern for systemd services (EnvironmentFile=)
  + Easy to rotate: change env, restart service
  - Visible in /proc/<pid>/environ to root / same user

Secrets file:
  + Self-contained — no need to set env before starting
  + 0600 permissions mean only the owning user can read it
  - One more file to manage and back up
  - Still readable by root

We deliberately do NOT support plaintext in config.yaml.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SECRETS_DIR = Path.home() / ".config" / "timelapse" / "secrets"


def get_secret(env_var: str, secrets_file: Optional[str] = None) -> Optional[str]:
    """
    Resolve a secret value.

    1. Check `env_var` in os.environ.
    2. If not found and `secrets_file` is given, read KEY=VALUE pairs from it.
    Returns None if the secret cannot be found.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        return value

    if secrets_file:
        path = Path(secrets_file).expanduser()
        value = _read_from_secrets_file(path, env_var)
        if value:
            return value

    logger.warning(
        "Secret not found: env var $%s is unset and no secrets file resolved it.", env_var
    )
    return None


def _read_from_secrets_file(path: Path, key: str) -> Optional[str]:
    """Parse a KEY=VALUE file (like .env) and return the value for `key`."""
    if not path.exists():
        logger.debug("Secrets file not found: %s", path)
        return None

    # Enforce strict permissions
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        logger.error(
            "Secrets file %s has insecure permissions (%s). "
            "Fix with: chmod 0600 %s",
            path,
            oct(mode),
            path,
        )
        return None

    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError as exc:
        logger.error("Could not read secrets file %s: %s", path, exc)

    return None


def ensure_secrets_dir() -> Path:
    _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    _SECRETS_DIR.chmod(0o700)
    return _SECRETS_DIR


def secrets_file_path(profile: str) -> Path:
    return _SECRETS_DIR / f"{profile}.env"


def create_secrets_file_template(profile: str) -> Path:
    """Write a template secrets file with 0600 permissions."""
    ensure_secrets_dir()
    path = secrets_file_path(profile)
    if not path.exists():
        path.write_text(
            f"# Secrets for timelapse profile: {profile}\n"
            f"# Fill in values and chmod 0600 this file.\n\n"
            f"TIMELAPSE_RTMP_KEY_{profile.upper()}=\n"
            f"TIMELAPSE_WEB_PASSWORD=\n"
            f"TIMELAPSE_OWM_KEY=\n"
        )
        path.chmod(0o600)
    return path
