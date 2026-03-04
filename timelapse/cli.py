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
@click.option("--no-preview", is_flag=True, help="Disable preview server even if configured.")
@click.option("--no-rtmp", is_flag=True, help="Disable RTMP stream even if configured.")
@click.pass_context
def run(ctx: click.Context, no_preview: bool, no_rtmp: bool) -> None:
    """Start capturing (foreground; use systemd for background service)."""
    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    # Suppress noisy libcamera INFO/DEBUG lines before any picamera2 import.
    import os as _os
    _os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARNING")

    from timelapse.config.loader import load_global_config, load_profile_config
    from timelapse.config.models import SolarSchedule

    glob_cfg = load_global_config()
    prof_cfg = load_profile_config(profile)

    # ── logging ────────────────────────────────────────────────────────────
    from timelapse.utils.log import setup_logging

    setup_logging(glob_cfg.log_path, glob_cfg.log_level, profile)
    logger.info("PiLapse v%s starting — profile '%s'", __version__, profile)

    # ── timezone ───────────────────────────────────────────────────────────
    local_tz = _resolve_tz(glob_cfg.timezone)

    # ── Pi model info ─────────────────────────────────────────────────────
    from timelapse.utils.pi_detect import PERFORMANCE_NOTES, get_tier

    tier = get_tier()
    click.echo(PERFORMANCE_NOTES.get(tier, ""))

    # ── state ─────────────────────────────────────────────────────────────
    from timelapse.scheduler.state import ProfileState

    state = ProfileState(profile)
    if state.get_status() == "completed" and not click.confirm(
        f"Profile '{profile}' is marked completed. Reset and start fresh?", default=False
    ):
        click.echo("Aborted. Use `timelapse status` to inspect.")
        sys.exit(0)
    if state.get_status() == "completed":
        state.reset()

    # ── camera ────────────────────────────────────────────────────────────
    from timelapse.capture.camera import check_camera_available, create_camera

    avail, msg = check_camera_available()
    if not avail:
        click.echo(f"[WARNING] {msg}", err=True)
        if not click.confirm("Continue with MockCamera (for testing)?", default=False):
            sys.exit(1)

    cam = create_camera(
        still_width=prof_cfg.capture.width,
        still_height=prof_cfg.capture.height,
        preview_width=prof_cfg.streaming.preview.mjpeg_width,
        preview_height=prof_cfg.streaming.preview.mjpeg_height,
        jpeg_quality=prof_cfg.capture.jpeg_quality,
        preview_fps=prof_cfg.streaming.preview.mjpeg_fps,
    )
    cam.start()

    # ── disk guard ────────────────────────────────────────────────────────
    from timelapse.disk.guard import DiskGuard

    disk_guard = DiskGuard(
        min_free_gb=prof_cfg.disk.min_free_gb,
        photos_dir=prof_cfg.photos_path,
        videos_dir=prof_cfg.videos_path,
        archive_dir=prof_cfg.archive_path,
        policy=prof_cfg.disk.retention.policy,
    )

    # ── video generator ───────────────────────────────────────────────────
    from timelapse.video.generator import VideoGenerator

    video_gen = VideoGenerator(prof_cfg, state)

    # ── capture pipeline ──────────────────────────────────────────────────
    from timelapse.capture.pipeline import CapturePipeline

    pipeline = CapturePipeline(
        config=prof_cfg,
        state=state,
        camera=cam,
        disk_guard=disk_guard,
        video_trigger_fn=video_gen.generate_async,
    )

    # ── streaming ─────────────────────────────────────────────────────────
    from timelapse.streaming.webserver import StreamingController

    stream_ctrl = StreamingController(prof_cfg, cam, profile)
    if no_preview:
        click.echo("Preview disabled via --no-preview.")
    else:
        try:
            stream_ctrl.start()
        except Exception as exc:
            logger.warning("Streaming failed to start: %s — capture continues.", exc)

    # ── scheduler ─────────────────────────────────────────────────────────
    from timelapse.scheduler.scheduler import Scheduler

    scheduler = Scheduler(
        profile=profile,
        config=prof_cfg,
        state=state,
        capture_fn=pipeline,
        local_tz=local_tz,
    )
    scheduler.start()

    # ── periodic disk check ────────────────────────────────────────────────
    disk_timer: Optional[threading.Timer] = None

    def _check_disk():
        nonlocal disk_timer
        freed = disk_guard.enforce()
        if freed:
            logger.info("Periodic disk guard freed %d file(s).", freed)
        ds = disk_guard.status()
        if not ds["ok"]:
            logger.warning("Disk free space low: %.2f GB (min %.2f GB)", ds["free_gb"], ds["min_free_gb"])
        disk_timer = threading.Timer(prof_cfg.disk.check_interval_s, _check_disk)
        disk_timer.daemon = True
        disk_timer.start()

    _check_disk()

    # ── signal handling / graceful shutdown ───────────────────────────────
    def _shutdown(sig, frame):
        click.echo(f"\nShutting down (signal {sig})...")
        if disk_timer:
            disk_timer.cancel()
        scheduler.stop()
        stream_ctrl.stop()
        cam.stop()
        logger.info("Clean shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo(f"PiLapse running — profile '{profile}'.  Press Ctrl-C to stop.")
    # Brief pause so the scheduler thread can complete its first capture
    # before the status snapshot is printed (avoids mid-capture race).
    import time as _time; _time.sleep(0.5)
    _print_status(profile, state, prof_cfg, disk_guard)

    # Block main thread indefinitely
    signal.pause()


# ─── status ──────────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show the current capture status for a profile."""
    profile = ctx.obj["profile"]
    _check_setup_or_exit(profile)

    from timelapse.config.loader import load_profile_config
    from timelapse.disk.guard import DiskGuard
    from timelapse.scheduler.state import ProfileState

    prof_cfg = load_profile_config(profile)
    state = ProfileState(profile)
    disk_guard = DiskGuard(
        min_free_gb=prof_cfg.disk.min_free_gb,
        photos_dir=prof_cfg.photos_path,
        videos_dir=prof_cfg.videos_path,
        archive_dir=prof_cfg.archive_path,
        policy=prof_cfg.disk.retention.policy,
    )
    _print_status(profile, state, prof_cfg, disk_guard)


def _print_status(profile: str, state, prof_cfg, disk_guard) -> None:
    from timelapse.config.models import IntervalSchedule, SolarSchedule, TimesSchedule

    data = state.as_dict()
    ds = disk_guard.status()

    schedule = prof_cfg.schedule
    sched_str = ""
    if isinstance(schedule, IntervalSchedule):
        sched_str = f"interval ({schedule.interval})"
    elif isinstance(schedule, TimesSchedule):
        sched_str = f"times ({', '.join(schedule.times)})"
    elif isinstance(schedule, SolarSchedule):
        events = ", ".join(f"{e.type}{e.offset_minutes:+d}m" for e in schedule.events)
        sched_str = f"solar ({events})"

    click.echo(f"\n{'─'*50}")
    click.echo(f"  Profile:       {profile}")
    click.echo(f"  Status:        {state.get_status()}")
    click.echo(f"  Schedule:      {sched_str}")
    click.echo(f"  Total photos:  {data.get('photo_count', 0)}")
    click.echo(f"  Current batch: {data.get('current_batch', 0)} / {prof_cfg.video.photos_per_video}")
    click.echo(f"  Videos made:   {data.get('video_count', 0)}")
    last = data.get("last_capture_at", "never")
    click.echo(f"  Last capture:  {last}")
    click.echo(f"  Disk free:     {ds['free_gb']:.2f} GB  (min {ds['min_free_gb']:.1f} GB)  {'✓' if ds['ok'] else '⚠ LOW'}")
    click.echo(f"  Photos dir:    {prof_cfg.photos_dir}")
    click.echo(f"  Videos dir:    {prof_cfg.videos_dir}")
    click.echo(f"{'─'*50}\n")


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
