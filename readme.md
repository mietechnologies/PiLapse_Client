# PiLapse

A production-grade, fully standalone Raspberry Pi timelapse system with live preview and optional RTMP restreaming. No external server required.

Designed, built, and tested by [Brett Chapin](https://github.com/BAChapin) / [MieTech LLC](https://github.com/mietechnologies).

---

## Features

- **Fully standalone** — captures, stores, and generates videos on-device (no server required)
- **Scheduled capture** — interval, fixed times, or solar (sunrise/solar noon/sunset)
- **Live preview** — MJPEG (Pi Zero W) or HLS (Pi Zero 2 W+) via built-in web server
- **RTMP restreaming** — stream live to YouTube / Twitch / any RTMP server (Pi Zero 2 W+)
- **Automatic video generation** — ffmpeg stitches frames when your threshold is hit
- **Disk space guard** — configurable free-space floor with automatic oldest-first cleanup
- **Multi-profile** — run multiple independent timelapse setups simultaneously
- **Systemd service** — runs on boot, restarts on failure, survives reboots
- **Secure by default** — preview binds to localhost; built-in Basic Auth option

---

## Hardware support & performance tiers

| Feature | Pi Zero W | Pi Zero 2 W | Pi 3/4/5 |
|---|---|---|---|
| Still capture (1920×1080) | ✓ | ✓ | ✓ |
| MJPEG preview (640×360 @ 1 fps) | ✓ | ✓ | ✓ |
| HLS preview (720p @ 15 fps) | ✗ too slow | ✓ (hw enc) | ✓ |
| RTMP restream | ✗ | ⚠ 720p max | ✓ |
| Video generation (ffmpeg batch) | ✓ slow | ✓ | ✓ |

**Pi Zero W**: single-core ARMv6 @ 1 GHz, 512 MB RAM.
MJPEG preview at 640×360 / 1 fps uses ~15–25% CPU.
Run ffmpeg video generation with `nice -n 19` to avoid camera stalls.

**Pi Zero 2 W**: quad-core Cortex-A53 @ 1 GHz, 512 MB RAM.
HLS at 1280×720 / 15 fps is feasible using the V4L2 hardware H.264 encoder.
Avoid running HLS + RTMP simultaneously.

---

## Prerequisites

### OS packages (Raspberry Pi OS Bookworm, 64-bit recommended)

```bash
# libcamera + picamera2 (camera access)
sudo apt install -y python3-picamera2 libcamera-apps

# ffmpeg (video generation + HLS + RTMP)
sudo apt install -y ffmpeg

# Python build tools
sudo apt install -y python3-pip python3-venv git
```

### Python dependencies

```bash
# From local clone:
pip install -e ".[dev]"
```

Minimum required packages (installed automatically):

| Package | Purpose |
|---|---|
| `click` | CLI |
| `pydantic` | Config validation |
| `pyyaml` | YAML config files |
| `requests` | OpenWeatherMap API |
| `astral` | Offline solar time calculation |
| `python-dateutil` | Date/time parsing |
| `tzlocal` | System timezone detection |

> **Note:** `picamera2` is installed via `apt`, not `pip`.

---

## Quick start

### 1. First-run setup wizard

```bash
timelapse setup
# or for a named profile:
timelapse --profile garden setup
```

### 2. Validate config

```bash
timelapse config validate
```

### 3. Run (foreground)

```bash
timelapse run
```

### 4. Run as systemd service (auto-start on boot)

```bash
sudo cp systemd/timelapse@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timelapse@default
journalctl -u timelapse@default -f   # live logs
```

---

## CLI reference

```
timelapse [--profile NAME] COMMAND

Commands:
  setup               Interactive setup wizard
  run                 Start capturing (foreground)
  status              Show capture statistics and disk status
  snapshot            Take one frame immediately (outside normal schedule)
  gen-video           Generate video from current photo batch now
  config validate     Check config for errors
  config show         Print resolved config as YAML
```

---

## Configuration

### File locations

```
~/.config/timelapse/
  config.yaml              ← global settings (timezone, log level)
  profiles/
    default.yaml           ← default profile
    garden.yaml            ← custom profile
~/.local/share/timelapse/
  default/state.json       ← schedule state (auto-managed)
```

See `profiles/default.yaml` and `config.example.yaml` for fully commented examples.

### Schedule modes

**Interval** — every N seconds/minutes/hours:
```yaml
schedule:
  mode: interval
  interval: "5m"   # 30s, 5m, 2h, 1d
```

**Fixed times** — one or more HH:MM times per day:
```yaml
schedule:
  mode: times
  times: ["08:00", "12:00", "17:00"]
```

**Solar** — sunrise / solar noon / sunset with optional offsets:
```yaml
schedule:
  mode: solar
  latitude: 51.5074
  longitude: -0.1278
  owm_api_key_env: "TIMELAPSE_OWM_KEY"
  events:
    - type: sunrise
      offset_minutes: 10
    - type: sunset
      offset_minutes: -15
```

---

## Secure internet access for live preview

The preview server binds to `127.0.0.1` by default.

**Option A — Tailscale (recommended):**
```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
# Access preview at http://<tailscale-ip>:8080/
```

**Option B — Cloudflare Tunnel (public HTTPS URL):**
```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

**Option C — nginx + Let's Encrypt** (see README for full config)

---

## Secrets management

Stream keys and passwords are read from **environment variables only** — never stored in config files.

```bash
# /etc/timelapse/default.env (chmod 0600, loaded by systemd)
TIMELAPSE_RTMP_KEY_DEFAULT=your-stream-key
TIMELAPSE_WEB_PASSWORD=your-preview-password
TIMELAPSE_OWM_KEY=your-owm-api-key
```

---

## Extension points

- **Auto-upload after video**: hook in `timelapse/video/generator.py`
- **Webhook notifications**: hook in `timelapse/capture/pipeline.py`
- **Additional RTMP destinations**: extend `timelapse/streaming/rtmp.py`
- **ML trigger**: intercept in `timelapse/capture/pipeline.py` before `record_capture()`

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

---

## License

MIT — _This program is brought to you by [MieTech LLC](https://github.com/mietechnologies)._