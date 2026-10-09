"""
PiLapse CLI — entry point for all subcommands.

Usage:
  timelapse [--profile NAME] COMMAND [ARGS]

Commands:
  setup          Run interactive setup wizard
  run            Start the timelapse daemon (foreground)
  status         Show current capture status
  snapshot       Take a single frame immediately
  gen-video      Generate a video from accumulated photos now
  server         Connect to / check / disconnect from a PiLapse server
  config validate Check config for errors
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from timelapse import __version__

logger = logging.getLogger(__name__)


# ─── CLI group ───────────────────────────────────────────────────────────────


@click.group()
@click.version_option(__version__, prog_name="timelapse")
@click.option(
    "--profile", "-p",
    default="default",
    show_default=True,
    envvar="TIMELAPSE_PROFILE",
    help="Profile name (maps to ~/.config/timelapse/profiles/<name>.yaml).",
)
@click.pass_context
def cli(ctx: click.Context, profile: str) -> None:
    """PiLapse — Raspberry Pi timelapse system."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile


# ─── setup ───────────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def setup(ctx: click.Context) -> None:
    """Run the interactive setup wizard for a profile."""
    profile = ctx.obj["profile"]
    from timelapse.config.wizard import run_wizard

    run_wizard(profile)


# ─── run ─────────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--all", "all_profiles", is_flag=True,
    help="Run every profile in one process, sharing the camera (recommended).",
)
@click.option("--no-preview", is_flag=True, help="Disable preview server even if configured.")
@click.option("--no-rtmp", is_flag=True, help="Disable RTMP stream even if configured.")
@click.pass_context
def run(ctx: click.Context, all_profiles: bool, no_preview: bool, no_rtmp: bool) -> None:
    """Start capturing (foreground; use systemd for background service)."""
    profile = ctx.obj["profile"]

    # Suppress noisy libcamera INFO/DEBUG lines before any picamera2 import.
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARNING")

    from timelapse.config.loader import ConfigError, list_profiles, load_global_config, load_profile_config
    from timelapse.scheduler.rules import validate_schedule

    if all_profiles:
        names = list_profiles()
        if not names:
            click.echo("No profiles configured. Run: timelapse setup")
            sys.exit(1)
    else:
        _check_setup_or_exit(profile)
        names = [profile]

    glob_cfg = load_global_config()
    try:
        configs = {name: load_profile_config(name) for name in names}
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    problems = [
        f"{name}: {p}"
        for name, cfg in configs.items()
        for p in validate_schedule(cfg.schedule, cfg.location or glob_cfg.location)
    ]
    if problems:
        for p in problems:
            click.echo(f"✗ {p}", err=True)
        sys.exit(1)

    # ── logging ────────────────────────────────────────────────────────────
    from timelapse.utils.log import setup_logging

    setup_logging(glob_cfg.log_path, glob_cfg.log_level, "all" if all_profiles else profile)
    logger.info("PiLapse v%s starting — profile(s): %s", __version__, ", ".join(names))

    # ── timezone ───────────────────────────────────────────────────────────
    local_tz = _resolve_tz(glob_cfg.timezone)

    # ── Pi model info ─────────────────────────────────────────────────────
    from timelapse.utils.pi_detect import PERFORMANCE_NOTES, get_tier

    tier = get_tier()
    click.echo(PERFORMANCE_NOTES.get(tier, ""))

    # ── state: completed profiles ─────────────────────────────────────────
    from timelapse.scheduler.state import ProfileState

    for name in list(configs):
        state = ProfileState(name)
        if state.get_status() != "completed":
            continue
        if all_profiles:
            # Don't block an unattended service on a prompt.
            logger.info("Profile '%s' is completed; skipping. Reset with `timelapse -p %s run`.", name, name)
            del configs[name]
        elif click.confirm(
            f"Profile '{name}' is marked completed. Reset and start fresh?", default=False
        ):
            state.reset()
        else:
            click.echo("Aborted. Use `timelapse status` to inspect.")
            sys.exit(0)
    if not configs:
        click.echo("Every profile is completed; nothing to run.")
        sys.exit(0)

    # ── camera ────────────────────────────────────────────────────────────
    from timelapse.capture.camera import check_camera_available

    avail, msg = check_camera_available()
    if not avail:
        click.echo(f"[WARNING] {msg}", err=True)
        if not click.confirm("Continue with MockCamera (for testing)?", default=False):
            sys.exit(1)

    # ── server uploads ────────────────────────────────────────────────────
    from timelapse.server.uploader import uploader_from_config

    uploader = uploader_from_config(glob_cfg.server)

    # ── start everything ──────────────────────────────────────────────────
    from timelapse.runner import DeviceRunner

    device = DeviceRunner(
        configs,
        local_tz,
        location=glob_cfg.location,
        weather_cfg=glob_cfg.weather,
        uploader=uploader,
        preview=not no_preview,
        rtmp=not no_rtmp,
    )
    if no_preview:
        click.echo("Preview disabled via --no-preview.")
    device.start()

    # ── signal handling / graceful shutdown ───────────────────────────────
    def _shutdown(sig, frame):
        click.echo(f"\nShutting down (signal {sig})...")
        device.stop()
        logger.info("Clean shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo(f"PiLapse running — profile(s): {', '.join(configs)}.  Press Ctrl-C to stop.")
    # Brief pause so the scheduler threads can complete their first capture
    # before the status snapshot is printed (avoids mid-capture race).
    import time as _time; _time.sleep(0.5)
    for r in device.runners:
        _print_status(r.name, r.state, r.config, r.disk_guard, glob_cfg, local_tz)

    # Block main thread indefinitely
    signal.pause()


# ─── status ──────────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show the current capture status for a profile."""
    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    from timelapse.config.loader import load_global_config, load_profile_config
    from timelapse.disk.guard import DiskGuard
    from timelapse.scheduler.state import ProfileState

    glob_cfg = load_global_config()
    prof_cfg = load_profile_config(profile)
    state = ProfileState(profile)
    disk_guard = DiskGuard(
        min_free_gb=prof_cfg.disk.min_free_gb,
        photos_dir=prof_cfg.photos_path,
        videos_dir=prof_cfg.videos_path,
        archive_dir=prof_cfg.archive_path,
        policy=prof_cfg.disk.retention.policy,
    )
    _print_status(
        profile, state, prof_cfg, disk_guard, glob_cfg, _resolve_tz(glob_cfg.timezone)
    )


def _print_status(profile: str, state, prof_cfg, disk_guard, glob_cfg, local_tz) -> None:
    data = state.as_dict()
    ds = disk_guard.status()
    rules = prof_cfg.schedule.describe()
    location = prof_cfg.location or glob_cfg.location

    click.echo(f"\n{'─'*50}")
    click.echo(f"  Profile:       {profile}")
    click.echo(f"  Status:        {state.get_status()}")
    click.echo(f"  Schedule:      {rules[0]}")
    for r in rules[1:]:
        click.echo(f"                 {r}")
    click.echo(f"  Total photos:  {data.get('photo_count', 0)}")
    click.echo(f"  Current batch: {data.get('current_batch', 0)} / {prof_cfg.video.photos_per_video}")
    click.echo(f"  Videos made:   {data.get('video_count', 0)}")
    last = data.get("last_capture_at", "never")
    click.echo(f"  Last capture:  {last}")
    skipped = data.get("skipped_count", 0)
    if skipped:
        click.echo(f"  Skipped slots: {skipped}  (camera busy or clock jumped)")
    upcoming = _upcoming_lines(prof_cfg.schedule, location, local_tz, limit=5)
    click.echo(f"  Next captures: {upcoming[0] if upcoming else 'none scheduled'}")
    for line in upcoming[1:]:
        click.echo(f"                 {line}")
    click.echo(f"  Disk free:     {ds['free_gb']:.2f} GB  (min {ds['min_free_gb']:.1f} GB)  {'✓' if ds['ok'] else '⚠ LOW'}")
    click.echo(f"  Photos dir:    {prof_cfg.photos_dir}")
    click.echo(f"  Videos dir:    {prof_cfg.videos_dir}")
    click.echo(f"{'─'*50}\n")


def _upcoming_lines(schedule, location, local_tz, *, limit: int, until=None) -> list[str]:
    from timelapse.scheduler.rules import ScheduleError, upcoming

    now = datetime.now(timezone.utc)
    try:
        return [
            f"{t.astimezone(local_tz).strftime('%a %Y-%m-%d %H:%M:%S %Z')}  "
            f"({'; '.join(r.describe() for r in rules)})"
            for t, rules in upcoming(schedule, now, local_tz, location, until=until, limit=limit)
        ]
    except ScheduleError as exc:
        return [f"⚠ {exc}"]


# ─── schedule ────────────────────────────────────────────────────────────────


@cli.group("schedule")
def schedule_group() -> None:
    """Inspect the capture schedule."""


@schedule_group.command("preview")
@click.option("--days", default=1, show_default=True, type=click.IntRange(1, 366),
              help="How many days ahead to show.")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 10000),
              help="Maximum number of captures to list.")
@click.pass_context
def schedule_preview(ctx: click.Context, days: int, limit: int) -> None:
    """List upcoming capture times without running anything."""
    from datetime import timedelta

    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)
    from timelapse.config.loader import load_global_config, load_profile_config

    glob_cfg = load_global_config()
    prof_cfg = load_profile_config(profile)
    local_tz = _resolve_tz(glob_cfg.timezone)
    until = datetime.now(timezone.utc) + timedelta(days=days)
    lines = _upcoming_lines(
        prof_cfg.schedule, prof_cfg.location or glob_cfg.location, local_tz,
        limit=limit, until=until,
    )
    click.echo(f"Upcoming captures for '{profile}' (next {days} day(s)):")
    for line in lines or ["  none"]:
        click.echo(f"  {line}")
    if len(lines) == limit:
        click.echo(f"  … (showing the first {limit}; use --limit to see more)")


# ─── snapshot ────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--output", "-o", default=None, help="Output file path (default: ~/timelapse/snapshot_<ts>.jpg)")
@click.pass_context
def snapshot(ctx: click.Context, output: Optional[str]) -> None:
    """Capture a single frame immediately (does NOT affect schedule state)."""
    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    from timelapse.capture.camera import create_camera, check_camera_available
    from timelapse.config.loader import load_profile_config

    prof_cfg = load_profile_config(profile)
    avail, msg = check_camera_available()
    if not avail:
        click.echo(f"Camera not available:\n{msg}", err=True)
        sys.exit(1)

    if output:
        out_path = Path(output).expanduser()
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = Path.home() / "timelapse" / f"snapshot_{ts}.jpg"

    cam = create_camera(
        still_width=prof_cfg.capture.width,
        still_height=prof_cfg.capture.height,
    )
    cam.start()
    try:
        from timelapse.capture.pipeline import CapturePipeline

        # Use pipeline.snapshot for proper path handling
        ok = cam.capture_still(out_path)
        if ok:
            click.echo(f"Snapshot saved: {out_path}")
        else:
            click.echo("Snapshot failed — check logs.", err=True)
            sys.exit(1)
    finally:
        cam.stop()


# ─── gen-video ───────────────────────────────────────────────────────────────


@cli.command("gen-video")
@click.option("--force", is_flag=True, help="Generate even if photo count < threshold.")
@click.pass_context
def gen_video(ctx: click.Context, force: bool) -> None:
    """Generate a timelapse video from the current photo batch."""
    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    from timelapse.config.loader import load_profile_config
    from timelapse.scheduler.state import ProfileState
    from timelapse.video.generator import VideoGenerator

    prof_cfg = load_profile_config(profile)
    state = ProfileState(profile)
    batch = state.get_current_batch()

    if not force and batch < prof_cfg.video.photos_per_video:
        click.echo(
            f"Only {batch} / {prof_cfg.video.photos_per_video} photos accumulated. "
            f"Use --force to generate anyway."
        )
        sys.exit(1)

    click.echo(f"Generating video from {batch} photos...")
    gen = VideoGenerator(prof_cfg, state)
    out = gen.generate()
    if out:
        click.echo(f"Video saved: {out}")
    else:
        click.echo("Video generation failed — check logs.", err=True)
        sys.exit(1)


# ─── server subgroup ─────────────────────────────────────────────────────────


@cli.group("server")
def server_group() -> None:
    """Connect this Pi to a PiLapse server (optional)."""


@server_group.command("connect")
@click.option("--url", default=None, help="Server URL, e.g. http://pilapse.local:3000")
@click.option("--name", default=None, help="Camera name on the server (default: hostname).")
def server_connect(url: Optional[str], name: Optional[str]) -> None:
    """Log in to a server and register this Pi as a camera."""
    from timelapse.server.connect import connect

    connect(url=url, name=name)


@server_group.command("status")
def server_status() -> None:
    """Show the server connection and upload queue."""
    from timelapse.server.connect import status

    status()


@server_group.command("disconnect")
def server_disconnect() -> None:
    """Stop uploading and forget this Pi's device token."""
    from timelapse.server.connect import disconnect

    disconnect()


# ─── config subgroup ─────────────────────────────────────────────────────────


@cli.group("config")
def config_group() -> None:
    """Config management commands."""


@config_group.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """Validate the global config and a profile config."""
    profile = ctx.obj["profile"]
    from timelapse.config.loader import validate_config

    errors = validate_config(profile)
    if not errors:
        click.echo(f"✓ Config for profile '{profile}' is valid.")
    else:
        click.echo(f"Issues found in config for profile '{profile}':")
        for e in errors:
            click.echo(f"  • {e}")
        sys.exit(1)


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the resolved config for a profile as YAML."""
    import yaml

    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    from timelapse.config.loader import load_global_config, load_profile_config

    glob_cfg = load_global_config()
    prof_cfg = load_profile_config(profile)

    click.echo("# Global config")
    click.echo(yaml.dump(glob_cfg.model_dump(), default_flow_style=False))
    click.echo(f"# Profile: {profile}")
    click.echo(yaml.dump(prof_cfg.model_dump(), default_flow_style=False))


# ─── helpers ─────────────────────────────────────────────────────────────────


def _check_setup_or_exit(profile: str) -> None:
    from timelapse.config.wizard import needs_setup

    if needs_setup(profile):
        click.echo(
            f"Profile '{profile}' has not been configured.\n"
            f"Run: timelapse setup --profile {profile}"
        )
        sys.exit(1)


def _resolve_tz(tz_name: Optional[str]):
    """Return a tzinfo object for the given timezone name or system default."""
    try:
        import zoneinfo

        if tz_name:
            return zoneinfo.ZoneInfo(tz_name)
        import tzlocal

        return tzlocal.get_localzone()
    except Exception as exc:
        logger.warning("Timezone resolution failed (%s) — using UTC.", exc)
        return timezone.utc
