# PiLapse

A production-grade, fully standalone Raspberry Pi timelapse system with live preview and optional RTMP restreaming. No external server required, but you can optionally connect to a [PiLapse Server](https://github.com/mietechnologies/PiLapse_Server) to collect photos from several cameras.

Designed, built, and tested by [Brett Chapin](https://github.com/BAChapin) / [MieTech LLC](https://github.com/mietechnologies).

---

## Features

- **Fully standalone** — captures, stores, and generates videos on-device (no server required)
- **Scheduled capture** — combine fixed times, intervals (optionally only in a daylight window), and sunrise/solar noon/sunset with offsets
- **Weather recording** — optional OpenWeatherMap conditions saved with every photo
- **Live preview** — MJPEG (Pi Zero W) or HLS (Pi Zero 2 W+) via built-in web server
- **RTMP restreaming** — stream live to YouTube / Twitch / any RTMP server (Pi Zero 2 W+)
- **Automatic video generation** — ffmpeg stitches frames when your threshold is hit
- **Disk space guard** — configurable free-space floor with automatic oldest-first cleanup
- **Multi-profile** — several capture intents on one camera (e.g. a detailed 5 s lapse and a short 30 s lapse), all run by one process
- **Optional server uploads** — every photo is queued and uploaded to your PiLapse Server, surviving outages and restarts
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
| `requests` | Weather (OpenWeatherMap) and location lookup during setup |
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
timelapse run --all        # every profile, sharing the camera
timelapse -p garden run    # or just one profile
```

Only one process can use the camera at a time, so run all your profiles together with `--all` rather than one process per profile. When several profiles are due at the same moment (a 5 s and a 30 s schedule meet every 30 s), one photo is taken and filed for each; the camera captures at the largest resolution any profile asks for.

### 4. Run as systemd service (auto-start on boot)

```bash
sudo cp systemd/timelapse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timelapse
journalctl -u timelapse -f   # live logs
```

Upgrading from 0.1.x per-profile units: `sudo systemctl disable --now 'timelapse@*'` first.

### 5. (Optional) Connect to a PiLapse server

```bash
timelapse server connect
sudo systemctl restart timelapse
```

See [Connecting to a PiLapse server](#connecting-to-a-pilapse-server).

---

## CLI reference

```
timelapse [--profile NAME] COMMAND

Commands:
  setup               Interactive setup wizard
  run [--all]         Start capturing (foreground); --all runs every profile
  status              Show capture statistics and disk status
  snapshot            Take one frame immediately (outside normal schedule)
  gen-video           Generate video from current photo batch now
  schedule preview    List upcoming capture times
  config validate     Check config for errors
  config show         Print resolved config as YAML
  server connect      Log in to a PiLapse server and register this Pi
  server status       Show connection, token and upload queue
  server disconnect   Stop uploading and forget the device token
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

### Schedule

A schedule is a list of rules. The camera fires whenever **any** rule is due.

```yaml
schedule:
  rules:
    - at: ["07:30"]                          # every day at 07:30
    - at: ["08:00", "12:00", "17:00"]        # several times a day
    - every: "5m"                            # every 5 minutes (on :00, :05, ...)
    - every: "1m"                            # only in daylight, on weekdays
      between: ["sunrise-30m", "sunset+30m"]
      days: [mon, tue, wed, thu, fri]
    - sun: sunrise                           # sunrise / solar_noon / sunset
      offset: "+10m"
    - sun: sunset
      offset: "-15m"
```

- `every` slots are aligned to the clock, so a 5 s and a 30 s schedule share every 30 s frame.
- `between` ends are `HH:MM` or a sun event with an optional offset; a window may cross midnight.
- After a reboot, a slot missed by up to `catch_up` (default `10m`) is still taken once; older ones are skipped.
- If a capture is still running when the next slot comes up, that slot is skipped (and counted in `timelapse status`), never queued.
- Old `mode: interval | times | solar` configs from 0.1.0 still load.

Check what a schedule will do without running it:

```bash
timelapse schedule preview --days 2
```

### Location, sunrise/sunset and weather

`timelapse setup` asks for the device location once: detected from your internet connection (you confirm the city), searched by city name, or typed as coordinates. It's stored in `config.yaml`, and sun times are then computed offline with `astral`; nothing touches the network at run time for scheduling.

Optionally, `weather.enabled: true` records the current conditions from OpenWeatherMap (temperature, feels-like, humidity, pressure, clouds, wind, condition) with each photo, as one JSON line per photo in `<photos_dir>/captures.jsonl`. It needs a free API key in `$TIMELAPSE_OWM_KEY`; without one, photos are taken normally with no weather.

---

## Connecting to a PiLapse server

A [PiLapse Server](https://github.com/mietechnologies/PiLapse_Server) is optional. When connected, the Pi keeps capturing exactly as before and also uploads every photo, tagged with the profile (intent) that took it.

Run this on the Pi as the same user the service runs as (not with `sudo`):

```
$ timelapse server connect
Server URL (e.g. http://pilapse.local:3000): pilapse.local:3000
  ✓ Connected to PiLapse Server 1.0.0
Email: you@example.com
Password:
  ✓ Logged in
Name this camera [printer-pi]: Printer
  ✓ Registered "Printer". Photos from every profile will upload to the server.
```

If the server has no accounts yet, this creates the owner account instead of logging in. The setup wizard also offers this step.

- The Pi is registered once as a camera; each profile shows up on the server as an intent of that camera.
- The server returns a **device token** for this Pi, stored in `~/.config/timelapse/secrets/server.env` (0600). Your password is not stored.
- Uploads run in the background from a queue in `~/.local/share/timelapse/upload-queue/`. If the server is unreachable, photos wait there and upload when it's back. Photos deleted locally before they upload (`after_video: delete`, or the disk guard) are skipped.
- To keep a profile local-only, set `upload_to_server: false` in it.
- `timelapse server status` checks the connection; `timelapse server disconnect` stops uploading.

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
# /etc/timelapse/pilapse.env (chmod 0600, loaded by systemd)
TIMELAPSE_RTMP_KEY_DEFAULT=your-stream-key
TIMELAPSE_WEB_PASSWORD=your-preview-password
TIMELAPSE_OWM_KEY=your-owm-api-key        # weather recording (optional)
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