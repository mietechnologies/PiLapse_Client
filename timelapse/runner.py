"""
Device runner — one process owns the camera and runs every profile.

libcamera lets only one process hold a camera, so profiles (intents on the
same camera, e.g. a 5 s and a 30 s lapse of a 3D printer) can't each run as a
separate process. Instead `timelapse run --all` opens the camera once, gives
every profile its own state, disk guard, video generator and capture
pipeline, and drives them all from one Scheduler. When several intents are
due at the same moment, one photo is taken and filed for each of them.

Device-wide settings are derived from the profiles:
  * Camera still size / JPEG quality: the largest any profile asks for.
  * Preview and RTMP: one profile's streaming settings (see stream_profile).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from timelapse.config.models import ProfileConfig

logger = logging.getLogger(__name__)


def stream_profile(profiles: dict[str, ProfileConfig]) -> Optional[str]:
    """
    The profile whose streaming settings the device uses: 'default' if it
    enables preview or RTMP, otherwise the first such profile by name.
    """
    def streams(cfg: ProfileConfig) -> bool:
        return cfg.streaming.preview.enabled or cfg.streaming.rtmp.enabled

    names = sorted(profiles, key=lambda n: (n != "default", n))
    for name in names:
        if streams(profiles[name]):
            return name
    return None


def camera_settings(profiles: dict[str, ProfileConfig]) -> dict:
    """create_camera() kwargs that satisfy every profile."""
    cfgs = list(profiles.values())
    preview_src = profiles.get(stream_profile(profiles) or "", cfgs[0])
    preview = preview_src.streaming.preview
    return {
        "still_width": max(c.capture.width for c in cfgs),
        "still_height": max(c.capture.height for c in cfgs),
        "jpeg_quality": max(c.capture.jpeg_quality for c in cfgs),
        "preview_width": preview.mjpeg_width,
        "preview_height": preview.mjpeg_height,
        "preview_fps": preview.mjpeg_fps,
    }


@dataclass
class ProfileRunner:
    name: str
    config: ProfileConfig
    state: object
    disk_guard: object
    pipeline: object
    disk_timer: Optional[threading.Timer] = field(default=None, repr=False)
    _stopped: threading.Event = field(default_factory=threading.Event, repr=False)

    def start(self) -> None:
        self._check_disk()

    def stop(self) -> None:
        self._stopped.set()
        if self.disk_timer:
            self.disk_timer.cancel()

    def _check_disk(self) -> None:
        if self._stopped.is_set():
            return
        freed = self.disk_guard.enforce()
        if freed:
            logger.info("[%s] Periodic disk guard freed %d file(s).", self.name, freed)
        ds = self.disk_guard.status()
        if not ds["ok"]:
            logger.warning(
                "[%s] Disk free space low: %.2f GB (min %.2f GB)",
                self.name,
                ds["free_gb"],
                ds["min_free_gb"],
            )
        self.disk_timer = threading.Timer(self.config.disk.check_interval_s, self._check_disk)
        self.disk_timer.daemon = True
        self.disk_timer.start()


class DeviceRunner:
    """
    Owns the camera, scheduler, streaming, weather recorder, uploader and one
    ProfileRunner per profile.
    """

    def __init__(
        self,
        profiles: dict[str, ProfileConfig],
        local_tz,
        *,
        location=None,
        weather_cfg=None,
        uploader=None,
        preview: bool = True,
        rtmp: bool = True,
        clock_sync_timeout_s: Optional[float] = None,
    ) -> None:
        if not profiles:
            raise ValueError("No profiles to run.")
        self.profiles = profiles
        self._tz = local_tz
        self.uploader = uploader
        self._location = location
        self._weather_cfg = weather_cfg
        self._preview = preview
        self._rtmp = rtmp
        self._clock_sync_timeout_s = clock_sync_timeout_s
        self.camera = None
        self.stream_ctrl = None
        self.weather = None
        self.scheduler = None
        self.runners: list[ProfileRunner] = []

    def start(self) -> None:
        from timelapse.capture.camera import create_camera

        self.camera = create_camera(**camera_settings(self.profiles))
        self.camera.start()

        self._start_streaming()

        if self.uploader:
            self.uploader.start()

        if self._weather_cfg is not None:
            from timelapse.weather import WeatherRecorder

            self.weather = WeatherRecorder(self._weather_cfg, self._location)
            self.weather.start()

        for name, cfg in sorted(self.profiles.items()):
            runner = self._build_profile(name, cfg)
            runner.start()
            self.runners.append(runner)

        self.scheduler = self._build_scheduler()
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        for runner in self.runners:
            runner.stop()
        if self.weather:
            self.weather.stop()
        if self.stream_ctrl:
            self.stream_ctrl.stop()
        if self.uploader:
            self.uploader.stop()
        if self.camera:
            self.camera.stop()

    def on_due(self, intents, slot) -> None:
        """
        Scheduler callback: take one photo and file it for every due intent.
        The first intent that can capture (e.g. has disk space) takes it.
        """
        by_name = {r.name: r for r in self.runners}
        due = [by_name[i.name] for i in intents if i.name in by_name]
        taken = None
        for runner in due:
            taken = runner.pipeline.capture_photo()
            if taken:
                break
        if not taken:
            return
        path, captured_at = taken
        for other in due:
            if other is not runner:
                other.pipeline.adopt(path, captured_at)

    # ── internals ──────────────────────────────────────────────────────────

    def _start_streaming(self) -> None:
        from timelapse.streaming.webserver import StreamingController

        name = stream_profile(self.profiles)
        if name is None:
            logger.info("No profile enables preview or RTMP; streaming off.")
            return
        if not self._preview and not self._rtmp:
            return

        cfg = self.profiles[name].model_copy(deep=True)
        cfg.streaming.preview.enabled = cfg.streaming.preview.enabled and self._preview
        cfg.streaming.rtmp.enabled = cfg.streaming.rtmp.enabled and self._rtmp
        if len(self.profiles) > 1:
            logger.info("Using streaming settings from profile '%s'.", name)

        self.stream_ctrl = StreamingController(cfg, self.camera, name)
        try:
            self.stream_ctrl.start()
        except Exception as exc:
            logger.warning("Streaming failed to start: %s — capture continues.", exc)

    def _build_scheduler(self):
        from timelapse.scheduler.scheduler import Intent, Scheduler

        intents = [
            Intent(r.name, r.config.schedule, r.state, r.config.location or self._location)
            for r in self.runners
        ]
        kwargs = {}
        if self._clock_sync_timeout_s is not None:
            kwargs["clock_sync_timeout_s"] = self._clock_sync_timeout_s
        return Scheduler(intents=intents, on_due=self.on_due, local_tz=self._tz, **kwargs)

    def _build_profile(self, name: str, cfg: ProfileConfig) -> ProfileRunner:
        from timelapse.capture.pipeline import CapturePipeline
        from timelapse.disk.guard import DiskGuard
        from timelapse.scheduler.state import ProfileState
        from timelapse.video.generator import VideoGenerator

        state = ProfileState(name)
        disk_guard = DiskGuard(
            min_free_gb=cfg.disk.min_free_gb,
            photos_dir=cfg.photos_path,
            videos_dir=cfg.videos_path,
            archive_dir=cfg.archive_path,
            policy=cfg.disk.retention.policy,
        )
        video_gen = VideoGenerator(cfg, state)
        pipeline = CapturePipeline(
            config=cfg,
            state=state,
            camera=self.camera,
            disk_guard=disk_guard,
            video_trigger_fn=video_gen.generate_async,
            weather=self.weather,
            on_captured_fn=self._upload_hook(name, cfg),
        )
        return ProfileRunner(name, cfg, state, disk_guard, pipeline)

    def _upload_hook(self, name: str, cfg: ProfileConfig):
        if not self.uploader or not cfg.upload_to_server:
            return None
        from timelapse.server.client import intent_for_profile

        intent = intent_for_profile(name)
        uploader = self.uploader

        def hook(path, captured_at):
            uploader.enqueue(
                path, intent=intent, taken_at=captured_at, archive_dir=cfg.archive_path
            )

        return hook
