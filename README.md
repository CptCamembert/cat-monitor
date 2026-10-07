# Cat Monitor

A Raspberry Pi web application for monitoring a cat: live camera streaming, NeoPixel LED status/effects, and a scheduled or manual radio alarm (LED breathing effect + radio audio on demand).

## Features

- **Live camera streaming** via MediaMTX (WebRTC/WHEP), 1280x720 @ 30fps
- **NeoPixel LED control** — 12 RGB LEDs, solid color or chase effect
- **Auto idle-off** — LEDs dim to a red indicator when nobody is viewing the stream
- **Manual alarm** — trigger a green breathing LED effect + radio audio on demand
- **Scheduled alarms** — recurring daily alarms at specific times, persisted to disk
- **Alarm recordings** — each scheduled alarm saves a 10-minute clip; only the latest completed clip is retained
- **Web dashboard** — dark-themed single-page UI for all controls

## Hardware

- Raspberry Pi 5
- Camera supported by `rpicam-vid` / `rpicam-jpeg` (libcamera)
- 12x WS2812B NeoPixel LEDs on GPIO pin D12 (BCM 12)
- PipeWire audio (`ffplay`) for the continuously running radio alarm

## Project Layout

```
app.py                 Flask app: LED control, alarm system, camera streaming, web UI
test_app.py            Unit tests (unittest, hardware mocked out)
alarm_settings.json    Persisted scheduled alarms (auto-created/updated by app.py)
mediamtx               MediaMTX binary (streaming server)
mediamtx.yml           MediaMTX configuration
Radio stream: `https://knkx-live-a.edge.audiocdn.com/6285_128k`
auto.crt / auto.key    Auto-generated TLS cert/key for MediaMTX's MOQ server (regenerate automatically, safe to delete)
cat-monitor.service    systemd unit for autostart-on-boot
start_app.sh / stop_app.sh         Used by systemd: run/stop MediaMTX + app.py in a tmux session
start_streams.sh / stop_streams.sh Manual foreground alternative (no tmux/systemd) for quick local testing
CODE_EXPLANATION.md    Line-by-line explanation of app.py for maintainers
```

## One-Time Setup

### 1. Create the virtual environment

The venv must be self-contained — install packages directly into it. Do **not**
use `--system-site-packages` and do **not** rename/move a venv folder after
creating it (the `activate` script hardcodes its own absolute path).

```bash
cd /home/pi5-max/cat_monitor
python3 -m venv .venv
.venv/bin/pip install flask adafruit-blinka adafruit-circuitpython-neopixel
```

**If `pip install` fails to build `lgpio`** with an error like
`/usr/bin/ld: cannot find -llgpio`, the apt-installed `liblgpio1` package only
ships the versioned runtime library, not the unversioned symlink the linker
needs. Fix once, system-wide:

```bash
sudo apt-get install swig libgpiod-dev
sudo ln -sf /usr/lib/aarch64-linux-gnu/liblgpio.so.1 /usr/lib/aarch64-linux-gnu/liblgpio.so
```

Then re-run the `pip install` above.

### 2. MediaMTX

MediaMTX is not a systemd service — it's launched as a plain background
process by `start_app.sh` (for autostart) or `start_streams.sh` (for manual
runs), always alongside `app.py`, using the shared `mediamtx.yml` config.

## Running

**Manual (foreground, for quick testing):**
```bash
./start_streams.sh
# Ctrl+C stops both mediamtx and app.py
```

**Via systemd (recommended for normal use):**
```bash
chmod +x start_app.sh stop_app.sh
sudo cp cat-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cat-monitor.service
```

**Check status / logs:**
```bash
sudo systemctl status cat-monitor.service
sudo journalctl -u cat-monitor.service -f
```

**Attach to the running tmux session** (systemd or `start_app.sh` both use a
tmux session named `cat-monitor`):
```bash
tmux attach -t cat-monitor
# detach without stopping: Ctrl+B, then D
```

**Stop:**
```bash
sudo systemctl stop cat-monitor.service
# or, if started manually:
./stop_app.sh
```

## Web Interface

Open `http://<pi-address>:3000` in a browser.

Access it via the Pi's real network address (LAN IP, Tailscale IP/hostname,
etc.) — not via an SSH/VS Code port-forwarded `localhost`. The page's
JavaScript builds the video stream URL from the browser's own hostname
(`window.location.hostname`), and WebRTC media additionally needs UDP
connectivity to the Pi (port 8189) which most port-forwarding setups don't
tunnel. Tailscale works because it provides real point-to-point connectivity
for all ports/protocols; a single forwarded TCP port does not.

### Controls

- Color picker / RGB sliders — LED color (0-255 per channel)
- Brightness slider — 0.0-1.0
- Power toggle — on/off. When off, the LED itself dims to a red indicator
  color, but the controls default back to white @ 25% brightness so the next
  "on" starts from a sensible color rather than the idle-off values.
- Alarm volume slider
- "Trigger alarm" button — manual alarm, 1-60 seconds
- Scheduled alarms — add/remove recurring daily alarms (HH:MM, 1-60 minutes)
- Last Alarm Recording — plays the most recent completed scheduled-alarm clip

## API Reference

| Method & Path | Purpose |
|---|---|
| `GET /` | Web dashboard (HTML/JS) |
| `GET /stream.mjpg` | MJPEG video stream (fallback path; the dashboard normally uses MediaMTX WebRTC instead) |
| `GET /snapshot.jpg` | Single JPEG snapshot |
| `GET /api/last-alarm-recording` | Status and URL for the latest completed alarm recording |
| `GET /last-alarm-recording.mp4` | Latest completed alarm recording |
| `GET /api/led` | Current LED state |
| `POST /api/led` | Set LED state — body: `{enabled, effect, brightness, color}` |
| `GET /api/alarms` | List scheduled alarms |
| `POST /api/alarms` | Add scheduled alarm — body: `{time: "HH:MM", duration}` (minutes) |
| `DELETE /api/alarms` | Remove scheduled alarm — body: `{index}` |
| `POST /api/alarm` | Trigger a manual alarm — body: `{duration, volume}` (duration in seconds) |
| `POST /api/alarm-volume` | Set default alarm volume — body: `{volume}` |
| `POST /api/idle-off` | Force idle-off immediately if there are no viewers |

## Autostart Details

- **systemd** (`cat-monitor.service`) starts on boot, runs as user `pi5-max`,
  and restarts automatically on failure.
- It calls `start_app.sh`, which creates a tmux session named `cat-monitor`
  and runs MediaMTX + `python app.py` inside it.
- Logs are visible both via `journalctl -u cat-monitor.service` and by
  attaching to the tmux session directly.
- `stop_app.sh` kills the tmux session and any stray MediaMTX/app/rpicam
  processes.

## Testing

```bash
.venv/bin/python -m unittest test_app -v
```

Tests mock out `board`/`neopixel` (no real hardware needed) and cover LED
state transitions, idle-off behavior, alarm triggering, and alarm persistence.

## Troubleshooting

**Service won't start**
```bash
sudo systemctl status cat-monitor.service
sudo journalctl -u cat-monitor.service -n 50
```

**Camera not working**
```bash
rpicam-vid --camera 0 --nopreview -t 1000
```

**LEDs not working**
```bash
.venv/bin/python -c "import board, neopixel; print(board.D12)"
```

**No alarm sound**
```bash
pactl info
ffplay -nodisp -vn https://knkx-live-a.edge.audiocdn.com/6285_128k
```

**Video loads on one device but not another** — see the note under
[Web Interface](#web-interface) about accessing via the Pi's real address.

## License

See [LICENSE](LICENSE).

