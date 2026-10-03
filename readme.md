# PiLapse

A fully standalone Raspberry Pi timelapse camera with a secure live view and optional RTMP streaming (YouTube / Twitch / Rumble). Built for the Pi Zero 2 W. No external server required.

Designed, built, and tested by [Brett Chapin](https://github.com/BAChapin) / [MieTech LLC](https://github.com/mietechnologies).

---

## Features

- **Fully standalone** — captures, stores, and generates videos on-device (no server required)
- **Scheduled capture** — interval, fixed times, or solar (sunrise/solar noon/sunset)
- **Live view** — WebRTC (sub-second latency) and HLS in any browser, plus RTSP for NVRs, via [MediaMTX](https://github.com/bluenviron/mediamtx); or a built-in MJPEG server with no extra software
- **RTMP streaming** — stream live to YouTube / Twitch / Rumble / any RTMP server
- **Hardware encoding** — live video is encoded once by the Pi's H.264 encoder and shared by every output, so the CPU stays free
- **Automatic video generation** — ffmpeg stitches frames when your threshold is hit
- **Disk space guard** — configurable free-space floor with automatic oldest-first cleanup
- **Multi-profile** — keep several independent timelapse setups (one camera runs one profile at a time)
- **Systemd service** — runs on boot, restarts on failure, survives reboots
- **Secure by default** — live view requires a login; only the Pi itself can publish to MediaMTX

---

## Hardware support & performance

| Feature | Pi Zero 2 W | Pi 3 / 4 | Pi 5 |
|---|---|---|---|
| Still capture | ✓ | ✓ | ✓ |
| Live view / RTMP (hardware H.264) | ✓ 720p @ 15 fps recommended | ✓ up to 1080p @ 30 fps | ⚠ no H.264 hardware — software encode |
| Video generation (ffmpeg batch) | ✓ | ✓ | ✓ |

**Pi Zero 2 W** (quad-core Cortex-A53 @ 1 GHz, 512 MB RAM): the camera, ISP and H.264 encoder are dedicated hardware, so the CPU mostly just moves compressed data. In practice **Wi-Fi is the bottleneck**, not the CPU:

- Disable Wi-Fi power saving if the stream stalls. To persist it, find your connection with `nmcli connection show`, then: `sudo nmcli connection modify "<connection>" wifi.powersave 2` (2 = disabled) and reconnect.
- Use a heatsink, and check for throttling with `vcgencmd get_throttled` (`0x0` = never throttled).
- To stream to several platforms, push once to your own server and fan out there.

The original **Pi Zero W** is untested.

---

## Install

On Raspberry Pi OS Bookworm (64-bit recommended):

```bash
# 1. System packages: camera stack, ffmpeg, Python tooling
sudo apt update
sudo apt install -y python3-picamera2 ffmpeg python3-venv git

# 2. A virtualenv that can still see the apt-installed picamera2
#    (Bookworm blocks system-wide pip installs, and picamera2 isn't on PyPI)
sudo mkdir -p /opt/pilapse && sudo chown "$USER": /opt/pilapse
python3 -m venv --system-site-packages /opt/pilapse/venv
git clone https://github.com/mietechnologies/PiLapse_Client.git ~/PiLapse_Client
/opt/pilapse/venv/bin/pip install ~/PiLapse_Client

# 3. Configure (writes ~/.config/timelapse/)
/opt/pilapse/venv/bin/timelapse setup
/opt/pilapse/venv/bin/timelapse config validate

# 4. Try it in the foreground (Ctrl-C to stop)
/opt/pilapse/venv/bin/timelapse run

# 5. Run on boot as your user
sudo cp ~/PiLapse_Client/systemd/timelapse@.service /etc/systemd/system/
sudo sed -i "s/^User=.*/User=$USER/; s/^Group=.*/Group=$USER/" \
    /etc/systemd/system/timelapse@.service
sudo systemctl daemon-reload
sudo systemctl enable --now timelapse@default
journalctl -u timelapse@default -f   # live logs
```

To update later: `git -C ~/PiLapse_Client pull && /opt/pilapse/venv/bin/pip install ~/PiLapse_Client && sudo systemctl restart timelapse@default`.

For the live view, also install MediaMTX — see [Live view](#live-view).

### Development (any machine)

```bash
pip install -e ".[dev]"
pytest
```

Without a camera, `timelapse run` offers a `MockCamera` that writes placeholder frames.

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

## Live view

### With MediaMTX (recommended)

PiLapse publishes the hardware-encoded stream to a local [MediaMTX](https://github.com/bluenviron/mediamtx) server, which serves it to viewers and handles logins:

| How | URL |
|---|---|
| Browser, WebRTC (sub-second latency) | `http://<pi>:8889/<profile>/` |
| Browser, HLS (a few seconds behind) | `http://<pi>:8888/<profile>/` |
| VLC / Frigate / Home Assistant (RTSP) | `rtsp://<pi>:8554/<profile>` |

Install it (requires **v1.21 or later**):

```bash
# From https://github.com/bluenviron/mediamtx/releases download the
# linux arm64 archive (64-bit Raspberry Pi OS) or armv7 (32-bit), then:
tar xzf mediamtx_*_linux_*.tar.gz mediamtx
sudo install -m 755 mediamtx /usr/local/bin/

# Locked-down config: only this Pi may publish, viewers must log in
sudo install -m 644 ~/PiLapse_Client/deploy/mediamtx.yml /usr/local/etc/mediamtx.yml
# Set the viewer password: put this hash in place of REPLACE_WITH_HASH
echo -n 'your-password' | openssl dgst -binary -sha256 | openssl base64
sudo nano /usr/local/etc/mediamtx.yml

sudo cp ~/PiLapse_Client/deploy/mediamtx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

Log in as `viewer` with that password. If MediaMTX restarts, PiLapse reconnects on its own.

### Built-in MJPEG (no extra software)

Set `streaming.preview.mode: mjpeg`. PiLapse then serves `http://<pi>:8080/` itself, with `/stream` and `/snapshot`. It uses far more bandwidth per viewer than WebRTC, and the hardware MJPEG encoder only runs while someone is watching. Login is on by default and the server **refuses to start** if `$TIMELAPSE_WEB_PASSWORD` is unset.

### Watching from outside your home network

Don't port-forward the camera. Instead:

- **Tailscale (recommended)** — `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`, then use the Pi's Tailscale address in the URLs above.
- **Cloudflare Tunnel** — `cloudflared tunnel --url http://127.0.0.1:8888` gives a public HTTPS URL for HLS (WebRTC needs UDP and won't pass through it).

---

## RTMP streaming (YouTube / Twitch / Rumble)

```yaml
streaming:
  rtmp:
    enabled: true
    rtmp_url: "rtmp://a.rtmp.youtube.com/live2"   # your platform's ingest URL
    stream_key_env: "TIMELAPSE_RTMP_KEY_DEFAULT"   # env var holding the key
```

The stream key comes from the environment (see [Secrets](#secrets-management)) and is redacted from logs. A silent audio track is added by default because YouTube rejects video-only streams. If the connection drops, PiLapse reconnects with back-off.

Quality is set once for all live outputs in `streaming.video` (see `profiles/default.yaml`).

---

## Secrets management

Stream keys and passwords are read from **environment variables only** — never stored in config files.

```bash
# /etc/timelapse/default.env (chmod 0600, loaded by the systemd unit)
TIMELAPSE_RTMP_KEY_DEFAULT=your-stream-key
TIMELAPSE_WEB_PASSWORD=your-preview-password
TIMELAPSE_OWM_KEY=your-owm-api-key
```

---

## Extension points

- **Auto-upload after video**: hook in `timelapse/video/generator.py`
- **Webhook notifications**: hook in `timelapse/capture/pipeline.py`
- **Additional live outputs**: add an `FfmpegPipeOutput` in `timelapse/streaming/controller.py` — it shares the existing hardware encode
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