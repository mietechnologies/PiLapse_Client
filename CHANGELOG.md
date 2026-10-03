# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — live streaming rebuilt (breaking config changes)
- Live video is now encoded ONCE by the Pi's hardware H.264 encoder and shared by every output (live view, RTMP). Previously each output started its own encoder, and the configured resolution, fps and bitrate were ignored (the stream was always 640×360 at 1 Mbps).
- New `streaming.video` section (`width`, `height`, `fps`, `bitrate`, `keyframe_interval_s`) controls that shared encode. Must not exceed the still size or 1920×1080.
- Live view now publishes to [MediaMTX](https://github.com/bluenviron/mediamtx) (`preview.mode: mediamtx`, the new default), which serves viewers over WebRTC (sub-second latency), HLS and RTSP and handles their logins. `deploy/mediamtx.yml` is a locked-down config: only the Pi may publish, viewers must log in. Legacy `auto`/`hls` modes map to `mediamtx`.
- The hand-written HLS server and RTMP module are removed (`timelapse/streaming/hls.py`, `rtmp.py`); `webserver.py` is now `controller.py`.
- MJPEG mode uses the hardware MJPEG encoder, which runs only while someone is watching. The old preview thread converted a full 1080p RGB frame to JPEG in Python continuously — even in HLS mode with no viewers.
- RTMP adds a silent AAC track by default (`rtmp.silent_audio`); YouTube rejects video-only streams. `rtmp.width/height/fps/video_bitrate/audio_enabled` are replaced by `streaming.video` and `silent_audio`.
- ffmpeg outputs restart automatically with back-off when ffmpeg exits (network drop, MediaMTX restart), never block the camera's encoder thread (frames are dropped and resume at a keyframe), and receive the camera's real frame timestamps (IVF framing) instead of arrival-time stamps, which produced non-monotonic timestamps on RTMP.
- Camera streams are YUV420 instead of RGB888, halving reserved camera memory; buffers reduced from 6 to 4. Stills are JPEG-encoded straight from the YUV planes and written atomically.
- `capture.jpeg_quality` and `capture.capture_timeout_s` are now honored (both were ignored). A timed-out capture no longer leaks a camera buffer.
- Stopping one encoder no longer stops every encoder (picamera2's `stop_encoder()` with no argument stops all of them).
- Unknown config keys (typos, removed settings) are now reported as warnings on load and by `timelapse config validate`.
- Setup wizard asks for live view mode, defaults MJPEG to requiring a login, and asks for live video quality.

### Changed — deployment
- systemd/timelapse@.service: removed `CPUQuota=90%` — it is measured against one core, so it held the service to under a quarter of a quad-core Pi Zero 2 W. `ExecStart` now points at the `/opt/pilapse/venv` virtualenv from the new install steps; adds the `video` group, ordering after MediaMTX, and basic hardening. Install instructions set `User=` to your account (Bookworm has no default `pi` user).
- deploy/mediamtx.service: new unit running MediaMTX as an unprivileged dynamic user.
- deploy/mediamtx.yml: disables MediaMTX's MoQ server (unused; it opened two extra ports and wrote TLS files to its working directory). Requires MediaMTX v1.21+.
- readme.md: new install section (virtualenv with `--system-site-packages`, since Bookworm blocks system pip and picamera2 comes from apt), live view and RTMP sections, Pi Zero 2 W Wi-Fi/thermal tips; removed the incorrect "binds to localhost by default" claim.

### Fixed
- timelapse/cli.py: `timelapse run` no longer prompts when there is no terminal (systemd). A completed profile exits cleanly instead of crash-looping; a missing camera exits with an error so systemd retries.
- pyproject.toml: `readme` pointed at `README.md` but the file is `readme.md`, so built packages silently had no description.
- timelapse/cli.py: `--no-rtmp` was accepted but ignored, and `--no-preview` also disabled RTMP.
- timelapse/cli.py: `timelapse snapshot` while the service is running now explains that the camera is in use instead of failing with a libcamera error.

### Security
- timelapse/streaming/hls.py: Fixed path traversal in the HLS file server — `GET /hls/../../<path>` returned any file readable by the service user. Only bare `.m3u8`/`.ts` filenames inside the segment directory are now served.
- timelapse/streaming/webserver.py: Preview server now refuses to start when auth is enabled but the password variable is unset (previously it silently served unauthenticated), and warns when serving on a non-loopback address without auth.
- timelapse/streaming/auth.py: Shared Basic Auth check for MJPEG and HLS using constant-time comparison (`hmac.compare_digest`).

### Fixed
- timelapse/scheduler/scheduler.py: A failed capture (camera missing, disk full) no longer retries in a tight loop (~10k attempts/s on interval schedules); retries now back off 10 s → 300 s and reset on success.
- timelapse/disk/guard.py: Disk guard now finds photos archived in `archive_dir/<video>/` subfolders. Previously they were never reclaimed and the guard deleted videos and unprocessed photos instead. Emptied archive subfolders are removed.
- timelapse/streaming/hls.py: HLS server releases its port when the encoder fails, so the MJPEG fallback can bind (was `Address already in use`). HLS server is now threaded.
- timelapse/video/generator.py: A failed video generation no longer re-runs a full encode on every subsequent capture; automatic retries wait 30 minutes.
- timelapse/video/generator.py: Photos captured while a video is encoding now count toward the next batch instead of being dropped from the counter.
- timelapse/video/generator.py: `h264_v4l2m2m` is rate-controlled with the new `video.bitrate` setting (default `8M`); it ignores `crf`, which produced low-quality output.

## [0.1.0] - 2026.03.04

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
- timelapse/scheduler/scheduler.py#_tick(): Fixed critical bug where the 60-second anti-double-capture cooldown was applied after a scheduled sleep, causing captures at intervals shorter than 60 seconds to fire at ~90-second intervals instead of the configured interval; cooldown guard now only applies on startup/catchup (delay ≤ 0).
- timelapse/scheduler/scheduler.py#start(): Moved `logger.info("Scheduler started")` before `thread.start()` to eliminate a log ordering race where "Triggering capture" appeared before "Scheduler started".
- timelapse/scheduler/state.py#_load(): Added `setdefault("status", "running")` so the status key is always present after load, preventing `as_dict()` from returning `"unknown"` on first run.
- timelapse/cli.py#run(): Set `LIBCAMERA_LOG_LEVELS=*:WARNING` before any picamera2 import to suppress verbose libcamera INFO/DEBUG output; wired `preview_fps` from profile config through to `create_camera()`; added 0.5 s sleep before initial status print to avoid a mid-capture race condition.
- timelapse/cli.py#_print_status(): Changed `data.get("status", "unknown")` to `state.get_status()` so the displayed status always reflects the authoritative value.
- timelapse/capture/camera.py#PiCamera.__init__(): Added `preview_fps` parameter; stored as `_preview_interval = max(0.1, 1.0 / preview_fps)` and used in `_preview_loop` instead of a hardcoded 0.5 s delay.
- timelapse/capture/camera.py#create_camera(): Added `preview_fps` parameter and forwarded it to `PiCamera`.
- timelapse/streaming/mjpeg.py#MJPEGServer: Switched from single-threaded `HTTPServer` to `ThreadingMixIn`-based server; the `/stream` handler's infinite loop was blocking all other browser connections (root page, favicon, second client), causing browsers to hang on connect.
- timelapse/streaming/mjpeg.py#_stream_mjpeg(): Replaced hardcoded `time.sleep(0.5)` with configurable `frame_interval` derived from `mjpeg_fps`.
- timelapse/streaming/webserver.py#_start_mjpeg(): Passed `mjpeg_fps` from profile config through to `MJPEGServer`.
