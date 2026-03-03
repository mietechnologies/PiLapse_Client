"""Rotating file + stderr logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(log_dir: Path, level: str = "INFO", profile: str = "default") -> None:
    """
    Configure root logger:
      - RotatingFileHandler → <log_dir>/<profile>.log  (5 MB × 5 rotations)
      - StreamHandler       → stderr (INFO and above)
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{profile}.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Stderr handler (always INFO+)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    root.addHandler(fh)
    root.addHandler(sh)
