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
    AtRule,
    EveryRule,
    LocationConfig,
    PreviewConfig,
    ProfileConfig,
    RetentionConfig,
    Rule,
    RtmpConfig,
    ScheduleConfig,
    StreamingConfig,
    SunRule,
    VideoConfig,
    WeatherConfig,
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

    click.echo("\n  Location (used for sunrise/sunset and weather):")
    location = _location_step(glob.location, tz)

    weather = _weather_step(glob.weather, location)

    glob = GlobalConfig(
        timezone=tz if tz else None,
        log_level=glob.log_level,
        log_dir=glob.log_dir,
        location=location,
        weather=weather,
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
    schedule = _schedule_step(has_location=location is not None)

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

    # ── web preview ────────────────────────────────────────────────────────
    click.echo("\n─── Live preview (web server) ───────────────────────────")
    preview_enabled = _confirm("Enable live preview web server?", default=True)
    auth_enabled = False
    auth = AuthConfig()
    preview_host = "0.0.0.0"
    preview_port = 8080
    preview_mode = "auto"

    if preview_enabled:
        preview_host = _prompt(
            "Bind host (0.0.0.0 to expose on LAN; 127.0.0.1 for local only)",
            "0.0.0.0",
        )
        preview_port_s = _prompt("Port", "8080")
        preview_port = int(preview_port_s)
        preview_mode = _choose(
            "Preview mode",
            ["auto", "hls", "mjpeg"],
            "auto",
        )
        auth_enabled = _confirm("Enable HTTP Basic Auth for preview?", default=False)
        if auth_enabled:
            username = _prompt("Username", "timelapse")
            pw_env = _prompt("Env var for password", "TIMELAPSE_WEB_PASSWORD")
            auth = AuthConfig(enabled=True, username=username, password_env=pw_env)
            click.echo(
                f"\n  ⚠  Set ${pw_env} in your environment or "
                "systemd EnvironmentFile.\n"
            )

    preview = PreviewConfig(
        enabled=preview_enabled,
        mode=preview_mode,  # type: ignore[arg-type]
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
        key_env = _prompt(f"Env var for stream key", key_env_default)
        bitrate = _prompt("Video bitrate (e.g. 1000k, 2M)", "1000k")
        rtmp = RtmpConfig(
            enabled=True,
            rtmp_url=rtmp_url,
            stream_key_env=key_env,
            video_bitrate=bitrate,
        )
        click.echo(
            f"\n  ⚠  Set ${key_env} in your environment or systemd EnvironmentFile.\n"
            "  The stream key is NEVER written to disk by this wizard.\n"
        )

    # ── assemble & save ────────────────────────────────────────────────────
    streaming = StreamingConfig(preview=preview, rtmp=rtmp)
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


# ─── location ────────────────────────────────────────────────────────────────


def _location_step(current: "LocationConfig | None", tz_name: str) -> "LocationConfig | None":
    from timelapse.utils import location as loc

    if current is not None:
        label = current.name or f"{current.latitude:.4f}, {current.longitude:.4f}"
        if _confirm(f"Keep current location ({label})?", default=True):
            return current

    choice = _choose(
        "How should we find it", ["detect", "search", "manual", "skip"], "detect"
    )
    result: LocationConfig | None = None
    if choice == "detect":
        click.echo("  Looking up your location from your internet connection...")
        place = loc.detect_from_ip()
        if place and _confirm(
            f"Found {place.name} ({place.latitude:.2f}, {place.longitude:.2f}). Use this?"
            , default=True
        ):
            result = LocationConfig(
                latitude=place.latitude, longitude=place.longitude, name=place.name, source="ip"
            )
        elif place is None:
            click.echo("  ✗ Couldn't detect a location (offline?).")
        if result is None:
            choice = "search"
    if result is None and choice == "search":
        while result is None:
            query = _prompt("City or place name (blank to enter coordinates)")
            if not query:
                choice = "manual"
                break
            places = loc.search(query)
            if not places:
                click.echo("  ✗ Nothing found. Try another name.")
                continue
            for i, p in enumerate(places, 1):
                click.echo(f"    {i}. {p.name} ({p.latitude:.2f}, {p.longitude:.2f})")
            pick = _prompt("Pick a number (blank to search again)")
            if pick.isdigit() and 1 <= int(pick) <= len(places):
                p = places[int(pick) - 1]
                result = LocationConfig(
                    latitude=p.latitude, longitude=p.longitude, name=p.name, source="search"
                )
    if result is None and choice == "manual":
        lat = _prompt_float("Latitude (decimal degrees, e.g. 41.08)", -90, 90)
        lon = _prompt_float("Longitude (decimal degrees, e.g. -85.14)", -180, 180)
        result = LocationConfig(latitude=lat, longitude=lon, source="manual")

    if result is not None:
        _warn_tz_mismatch(result, tz_name)
    return result


def _prompt_float(text: str, lo: float, hi: float) -> float:
    while True:
        raw = _prompt(text, required=True)
        try:
            v = float(raw)
        except ValueError:
            click.echo("  ✗ Enter a number.")
            continue
        if lo <= v <= hi:
            return v
        click.echo(f"  ✗ Must be between {lo} and {hi}.")


def _warn_tz_mismatch(location: LocationConfig, tz_name: str) -> None:
    try:
        import zoneinfo

        from timelapse.scheduler.solar import tz_location_mismatch_hours

        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return
    if tz_location_mismatch_hours(location.longitude, tz) > 2.5:
        click.echo(
            f"  ⚠  That location doesn't look like it's in the {tz_name} timezone.\n"
            "     Check the location and timezone, or sunrise/sunset captures will\n"
            "     land at odd hours."
        )


# ─── weather ─────────────────────────────────────────────────────────────────


def _weather_step(current: WeatherConfig, location: "LocationConfig | None") -> WeatherConfig:
    if location is None:
        return current
    enabled = _confirm(
        "Record the weather with each photo (needs a free OpenWeatherMap API key)?",
        default=current.enabled,
    )
    if not enabled:
        return current.model_copy(update={"enabled": False})
    key_env = _prompt("Env var for the OpenWeatherMap API key", current.api_key_env)
    units = _choose("Units", ["metric", "imperial"], current.units)
    click.echo(
        f"\n  ⚠  Set ${key_env} in your environment or systemd EnvironmentFile.\n"
        "  Without it, photos are still taken, just without weather.\n"
    )
    return current.model_copy(update={"enabled": True, "api_key_env": key_env, "units": units})


# ─── schedule ────────────────────────────────────────────────────────────────


def _schedule_step(has_location: bool) -> ScheduleConfig:
    click.echo("  Add one or more rules. The camera fires whenever any rule is due.")
    click.echo("    at     — fixed times each day (e.g. 07:30 or 08:00,12:00,17:00)")
    click.echo("    every  — an interval (e.g. 30s, 5m, 1h), optionally only in a window")
    click.echo("    sun    — sunrise / solar_noon / sunset, with an optional offset")
    rules: list[Rule] = []
    while True:
        options = ["at", "every", "sun", "done"] if rules else ["at", "every", "sun"]
        kind = _choose("Add a rule", options, "done" if rules else "every")
        if kind == "done":
            break
        if kind == "sun" and not has_location:
            click.echo("  ✗ Sun rules need a location. Re-run setup and set one first.")
            continue
        try:
            rule = _rule_prompt(kind, has_location)
        except ValueError as exc:
            click.echo(f"  ✗ {exc}")
            continue
        rules.append(rule)
        click.echo(f"  ✓ Added: {rule.describe()}")
    return ScheduleConfig(rules=rules)


def _rule_prompt(kind: str, has_location: bool) -> Rule:
    from pydantic import ValidationError

    data: dict = {}
    if kind == "at":
        raw = _prompt("Times as comma-separated HH:MM", "08:00,12:00,17:00")
        data["at"] = [t.strip() for t in raw.split(",") if t.strip()]
    elif kind == "every":
        data["every"] = _prompt("Interval (e.g. 30s, 5m, 1h)", "5m")
        hint = "e.g. 08:00,18:00" + (" or sunrise-30m,sunset+30m" if has_location else "")
        window = _prompt(f"Only between (start,end; {hint}; blank = all day)")
        if window:
            data["between"] = [w.strip() for w in window.split(",")]
    else:
        data["sun"] = _choose("Event", ["sunrise", "solar_noon", "sunset"], "sunrise")
        data["offset"] = _prompt("Offset (e.g. -15m, +10m, 0)", "0")
    days = _prompt("Days (e.g. mon,tue,fri; blank = every day)")
    if days:
        data["days"] = [d.strip() for d in days.split(",")]
    model = {"at": AtRule, "every": EveryRule, "sun": SunRule}[kind]
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc


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
