# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Added fully standalone on-device timelapse system eliminating the external server dependency.
- Added `timelapse` CLI with subcommands: `setup`, `run`, `status`, `snapshot`, `gen-video`, `config validate`, and `config show`.
- Added interactive first-run setup wizard (`timelapse/config/wizard.py`) covering schedule, directories, video, streaming, and RTMP in a single terminal session.
- Added Pydantic v2 config models (`timelapse/config/models.py`) supporting interval, fixed-times, and solar schedule modes with full field validation.
- Added YAML config loader with atomic writes and multi-profile support (`timelapse/config/loader.py`); profiles live at `~/.config/timelapse/profiles/<name>.yaml`.
- Added reboot-safe scheduler (`timelapse/scheduler/scheduler.py`) with interruptible sleep, 60-second anti-double-capture cooldown, and support for all three schedule modes.
- Added solar time resolution (`timelapse/scheduler/solar.py`) using OpenWeatherMap API with automatic fallback to the `astral` library for offline operation.
- Added persistent JSON state management (`timelapse/scheduler/state.py`) tracking photo count, batch count, video count, last capture time, and per-day solar time cache.
- Added picamera2 camera abstraction (`timelapse/capture/camera.py`) with multi-stream configuration (full-res main + low-res lores), a `MockCamera` for development, and actionable error messages on missing camera or library.
- Added capture pipeline (`timelapse/capture/pipeline.py`) coordinating disk guard, camera, state updates, and video generation trigger.
- Added MJPEG preview server (`timelapse/streaming/mjpeg.py`) using Python stdlib only, serving `/`, `/stream`, and `/snapshot` routes with optional HTTP Basic Auth.
- Added HLS preview server (`timelapse/streaming/hls.py`) using picamera2 H264Encoder piped through ffmpeg, serving an HLS.js player page with optional Basic Auth; requires Pi Zero 2 W or better.
- Added RTMP restreamer (`timelapse/streaming/rtmp.py`) via picamera2 H264Encoder and FfmpegOutput; blocked with a logged warning on Pi Zero W.
- Added streaming controller (`timelapse/streaming/webserver.py`) that auto-selects MJPEG vs HLS based on Pi hardware tier and falls back to MJPEG if HLS fails.
- Added disk space guard (`timelapse/disk/guard.py`) enforcing a configurable minimum free-space floor with `oldest_first` and `oldest_videos_first` retention policies and a path-safety check before any deletion.
- Added ffmpeg timelapse video generator (`timelapse/video/generator.py`) with deterministic timestamp-based output filenames, async execution, and configurable post-generation photo handling (`move_to_archive`, `delete`, `keep`).
- Added Raspberry Pi hardware tier detection (`timelapse/utils/pi_detect.py`) from `/proc/device-tree/model` with human-readable performance notes per tier.
- Added secrets resolution (`timelapse/utils/secrets.py`) reading from environment variables (primary) or a `0600` secrets file (alternative), with explicit rejection of world-readable files.
- Added rotating file logging (`timelapse/utils/log.py`) writing per-profile log files (5 MB × 5 rotations) alongside stderr output.
- Added `pyproject.toml` packaging configuration with `timelapse` entry point script and optional `[dev]` extras.
- Added parameterized systemd unit (`systemd/timelapse@.service`) supporting multiple simultaneous profiles via `timelapse@<profile>.service` instances.
- Added fully commented example configs (`config.example.yaml`, `profiles/default.yaml`).
- Added unit test suite (66 tests) covering config model validation, config loader round-trips, all three scheduler next-time algorithms, state persistence and concurrency guards, solar cache behavior, and disk guard path-safety.

### Changed
- readme.md: Rewrote to document the standalone architecture, CLI reference, hardware performance tiers, schedule modes, secrets management, and secure internet exposure options.
