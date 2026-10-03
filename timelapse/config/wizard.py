"""
Interactive first-run setup wizard.

Collects the minimum required settings and writes config files.
Designed for terminal use (stdin/stdout).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from timelapse.config.loader import (
    ensure_config_dirs,
    load_global_config,
    save_global_config,
    save_profile_config,
)
from timelapse.config.models import (
    AuthConfig,
    CaptureConfig,
    DiskConfig,
    GlobalConfig,
    IntervalSchedule,
    LiveVideoConfig,
    PreviewConfig,
    ProfileConfig,
    RetentionConfig,
    RtmpConfig,
    SolarEvent,
    SolarSchedule,
    StreamingConfig,
    TimesSchedule,
    VideoConfig,
)


def _prompt(text: str, default: str = "", required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"  {text}{suffix}: ").strip()
        if not value:
            value = default
        if required and not value:
            print("  ✗ This field is required.")
            continue
        return value


def _confirm(text: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        answer = input(f"  {text}{suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  ✗ Please enter y or n.")


def _choose(text: str, options: list[str], default: str) -> str:
    opts = "/".join(
        f"[{o}]" if o == default else o for o in options
    )
    while True:
        answer = input(f"  {text} ({opts}): ").strip().lower()
        if not answer:
            return default
        if answer in options:
            return answer
        print(f"  ✗ Choose one of: {', '.join(options)}")


def run_wizard(profile: str = "default") -> None:
    """
    Interactive setup wizard.  Writes global config + profile config.
    Safe to re-run — existing values are shown as defaults.
    """
    click.echo("")
    click.echo("=" * 60)
    click.echo("  PiLapse Setup Wizard")
    click.echo(f"  Profile: {profile}")
    click.echo("=" * 60)
    click.echo("")
    click.echo("  Press Enter to accept defaults shown in [brackets].")
    click.echo("  You can re-run `timelapse setup` at any time.\n")

    ensure_config_dirs()

    # ── global config ──────────────────────────────────────────────────────
    click.echo("─── Global settings ─────────────────────────────────────")
    try:
        glob = load_global_config()
    except Exception:
        glob = GlobalConfig()

    tz_default = glob.timezone or _detect_system_tz()
    tz = _prompt("Timezone (e.g. Europe/London, America/New_York)", tz_default)
    glob = GlobalConfig(
        timezone=tz if tz else None,
        log_level=glob.log_level,
        log_dir=glob.log_dir,
    )
    save_global_config(glob)
    click.echo("  ✓ Global config saved.\n")

    # ── profile: directories ───────────────────────────────────────────────
    click.echo("─── Directories ─────────────────────────────────────────")
    home = Path.home()
    default_photos = str(home / "timelapse" / profile / "photos")
    default_videos = str(home / "timelapse" / profile / "videos")
    default_archive = str(home / "timelapse" / profile / "archive")

    photos_dir = _prompt("Photos directory", default_photos)
    videos_dir = _prompt("Videos directory", default_videos)
    archive_dir = _prompt("Archive directory (processed photos)", default_archive)

    # ── capture settings ───────────────────────────────────────────────────
    click.echo("\n─── Capture settings ────────────────────────────────────")
    width_s = _prompt("Still image width", "1920")
    height_s = _prompt("Still image height", "1080")
    quality_s = _prompt("JPEG quality (1-100)", "85")
    capture = CaptureConfig(
        width=int(width_s),
        height=int(height_s),
        jpeg_quality=int(quality_s),
    )

    # ── schedule ───────────────────────────────────────────────────────────
    click.echo("\n─── Capture schedule ────────────────────────────────────")
    click.echo("  Modes:")
    click.echo("    interval  — every N seconds/minutes/hours (e.g. 5m, 1h)")
    click.echo("    times     — specific times each day (e.g. 08:00,12:00,17:00)")
    click.echo("    solar     — sunrise/solar_noon/sunset (uses lat/lon)")
    mode = _choose("Schedule mode", ["interval", "times", "solar"], "interval")

    schedule: IntervalSchedule | TimesSchedule | SolarSchedule
    if mode == "interval":
        interval_str = _prompt("Interval (e.g. 30s, 5m, 1h)", "5m")
        schedule = IntervalSchedule(interval=interval_str)
    elif mode == "times":
        times_raw = _prompt(
            "Times as comma-separated HH:MM (e.g. 08:00,12:00,17:00)", "08:00,12:00,17:00"
        )
        times = [t.strip() for t in times_raw.split(",") if t.strip()]
        schedule = TimesSchedule(times=times)
    else:
        lat_s = _prompt("Latitude (decimal degrees, e.g. 51.5)", required=True)
        lon_s = _prompt("Longitude (decimal degrees, e.g. -0.12)", required=True)
        click.echo("  Solar events (sunrise / solar_noon / sunset).")
        events: list[SolarEvent] = []
        for event_type in ("sunrise", "solar_noon", "sunset"):
            if _confirm(f"  Capture at {event_type}?", default=event_type != "solar_noon"):
                offset_s = _prompt(f"  Offset minutes for {event_type} (e.g. +10, -15)", "0")
                events.append(SolarEvent(type=event_type, offset_minutes=int(offset_s)))  # type: ignore[arg-type]
        if not events:
            click.echo("  No solar events selected — defaulting to sunrise.")
            events = [SolarEvent(type="sunrise", offset_minutes=0)]
        owm_env = _prompt(
            "Env var name for OpenWeatherMap API key", "TIMELAPSE_OWM_KEY"
        )
        schedule = SolarSchedule(
            latitude=float(lat_s),
            longitude=float(lon_s),
            events=events,
            owm_api_key_env=owm_env,
        )
        click.echo(
            f"\n  ⚠  Remember to set ${owm_env} in your environment or systemd "
            "EnvironmentFile before starting the service.\n"
            "  If the API key is missing, solar events will be skipped until "
            "the key is available.\n"
        )

    # ── video generation ────────────────────────────────────────────────────
    click.echo("\n─── Video generation ────────────────────────────────────")
    n_photos = _prompt("Photos per video (e.g. 100)", "100")
    fps = _prompt("Output video FPS (e.g. 10, 24, 30)", "10")
    repeat = _confirm("Keep capturing after each video (repeat=true)?", default=True)
    after_opts = ["move_to_archive", "delete", "keep"]
    after = _choose(
        "After generating video, photos should be",
        after_opts,
        "move_to_archive",
    )
    video = VideoConfig(
        photos_per_video=int(n_photos),
        fps=int(fps),
        repeat=repeat,
        after_video=after,  # type: ignore[arg-type]
    )

    # ── disk guard ─────────────────────────────────────────────────────────
    click.echo("\n─── Disk guard ──────────────────────────────────────────")
    min_free = _prompt("Minimum free disk space to keep (GB)", "3.0")
    disk = DiskConfig(min_free_gb=float(min_free))

    # ── live view ──────────────────────────────────────────────────────────
    click.echo("\n─── Live view ───────────────────────────────────────────")
    preview_enabled = _confirm("Enable live view?", default=True)
    preview = PreviewConfig(enabled=preview_enabled)

    if preview_enabled:
        click.echo(
            "  mediamtx: low-latency WebRTC/HLS in any browser, logins handled by\n"
            "            MediaMTX (install it separately — see README).\n"
            "  mjpeg:    built-in web server, no extra software, more bandwidth."
        )
        preview_mode = _choose("Live view mode", ["mediamtx", "mjpeg"], "mediamtx")
        if preview_mode == "mediamtx":
            preview = PreviewConfig(enabled=True, mode="mediamtx")
        else:
            preview_host = _prompt(
                "Bind host (0.0.0.0 to expose on LAN; 127.0.0.1 for local only)",
                "0.0.0.0",
            )
            preview_port = int(_prompt("Port", "8080"))
            auth = AuthConfig()
            if _confirm("Require a login to view? (strongly recommended)", default=True):
                username = _prompt("Username", "timelapse")
                pw_env = _prompt("Env var for password", "TIMELAPSE_WEB_PASSWORD")
                auth = AuthConfig(enabled=True, username=username, password_env=pw_env)
                click.echo(
                    f"\n  ⚠  Set ${pw_env} in your environment or systemd EnvironmentFile.\n"
                    "     The live view will not start without it.\n"
                )
            preview = PreviewConfig(
                enabled=True,
                mode="mjpeg",
                host=preview_host,
                port=preview_port,
                auth=auth,
            )

    # ── RTMP ──────────────────────────────────────────────────────────────
    click.echo("\n─── RTMP restream (YouTube/Twitch/etc.) ─────────────────")
    rtmp_enabled = _confirm("Enable RTMP restreaming?", default=False)
    rtmp = RtmpConfig()
    if rtmp_enabled:
        rtmp_url = _prompt("RTMP URL", "rtmp://a.rtmp.youtube.com/live2")
        key_env_default = f"TIMELAPSE_RTMP_KEY_{profile.upper()}"
        key_env = _prompt("Env var for stream key", key_env_default)
        rtmp = RtmpConfig(
            enabled=True,
            rtmp_url=rtmp_url,
            stream_key_env=key_env,
        )
        click.echo(
            f"\n  ⚠  Set ${key_env} in your environment or systemd EnvironmentFile.\n"
            "  The stream key is NEVER written to disk by this wizard.\n"
        )

    # ── live video quality (shared by live view and RTMP) ─────────────────
    live_video = LiveVideoConfig()
    if preview_enabled or rtmp_enabled:
        click.echo("\n─── Live video quality ──────────────────────────────────")
        res = _choose("Resolution", ["1280x720", "1920x1080", "854x480"], "1280x720")
        w, h = (int(x) for x in res.split("x"))
        if w > capture.width or h > capture.height:
            # The live stream can't be larger than the still stream (libcamera)
            w, h = min(w, capture.width), min(h, capture.height)
            click.echo(f"  Live video capped to the still size: {w}x{h}")
        fps = int(_prompt("Frame rate (lower = less power/bandwidth)", "15"))
        bitrate = _prompt("Bitrate (e.g. 1500k, 3M)", "1500k")
        live_video = LiveVideoConfig(width=w, height=h, fps=fps, bitrate=bitrate)

    # ── assemble & save ────────────────────────────────────────────────────
    streaming = StreamingConfig(video=live_video, preview=preview, rtmp=rtmp)
    profile_cfg = ProfileConfig(
        photos_dir=photos_dir,
        videos_dir=videos_dir,
        archive_dir=archive_dir,
        capture=capture,
        schedule=schedule,
        disk=disk,
        video=video,
        streaming=streaming,
    )
    save_profile_config(profile, profile_cfg)

    click.echo("\n" + "=" * 60)
    click.echo(f"  ✓ Profile '{profile}' saved.")
    click.echo(f"  Config: ~/.config/timelapse/profiles/{profile}.yaml")
    click.echo("")
    click.echo("  Next steps:")
    click.echo("    timelapse config validate  — check config for issues")
    click.echo("    timelapse run              — start capturing")
    click.echo("    timelapse status           — check running status")
    click.echo("=" * 60 + "\n")


def _detect_system_tz() -> str:
    try:
        import tzlocal

        tz = tzlocal.get_localzone()
        return str(tz)
    except Exception:
        return "UTC"


def needs_setup(profile: str = "default") -> bool:
    """Return True if the profile has not been configured yet."""
    from timelapse.config.loader import profile_config_path

    return not profile_config_path(profile).exists()
