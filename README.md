# Cat Monitor

A Raspberry Pi 5 web application for monitoring a cat: a day/night-switching
camera stream (MediaMTX), an automatic IR illuminator, a NeoPixel LED ring
that doubles as a status indicator and an analog clock, and a fixed daily
alarm schedule (LED effect + radio + recording).

## Features

- **Live camera streaming** via MediaMTX (WebRTC/WHEP)
- **Automatic day/night camera mode** — MediaMTX is restarted with a
  daylight (`mediamtx_RGB.yml`) or IR (`mediamtx_IR.yml`) config as the sun
  rises/sets, based on `astral` dawn/sunrise/sunset/dusk times for the
  configured location
- **Automatic IR illuminator control** — while someone is watching the
  stream (or a recording is in progress) and the camera is in IR mode, the
  app periodically samples brightness with `rpicam-still` and toggles the
  IR LED on GPIO D17 using hysteresis thresholds to avoid flicker at the
  boundary
- **NeoPixel analog clock** — the 12-LED ring shows the current hour/minute
  as dim markers when idle
- **Fixed daily alarm schedule** (`ALARM_TIMES` in `app.py`) — at each
  scheduled time the LED ring turns blue, internet radio plays through
  PulseAudio, and a 10-minute RTSP recording of the camera is saved
- **Alarm recordings** — only the latest completed alarm recording is kept
  and served through the web UI
- **Minimal web dashboard** — live stream, fullscreen toggle, and last-alarm
  recording playback

## Hardware

- Raspberry Pi 5
- Camera on `--camera 1`, driven by `rpicam-vid` / `rpicam-still` (libcamera)
- 12x WS2812B NeoPixel LEDs on GPIO D12
- IR illuminator LED switched via GPIO D17 (`digitalio`)
- PulseAudio (`ffmpeg ... -f pulse`) for the alarm radio playback

## Project Layout

```
app.py                      Flask app: day/night camera switching, IR illuminator control,
                             alarm system, analog clock LED effect, minimal web UI
app_BU.py                   Older backup version of the app (web-controlled LEDs,
                             alarm_settings.json) — kept for reference, not run in production
test.py                     Offline script that analyzes Images/bright and Images/dark samples
                             to help tune the IR_BRIGHTNESS_THRESHOLD_* constants in app.py
requirements.txt            Python dependencies for the venv
Images/bright, Images/dark  Sample snapshots used by test.py (gitignored)
mediamtx                    MediaMTX binary (streaming server)
mediamtx_RGB.yml            MediaMTX config used during the day (daytime camera pipeline)
mediamtx_IR.yml             MediaMTX config used at night (IR camera pipeline)
cat-monitor.service         systemd unit for autostart-on-boot
start_app.sh / stop_app.sh  Used by systemd: run/stop app.py in a tmux session
                             (app.py itself launches/restarts MediaMTX)
last_alarm_recording.mp4    Most recent completed alarm recording (created at runtime, gitignored)
logfile.log / logfile_last.log  App logs for the current/previous run (gitignored)
```

Radio stream used for the alarm: `https://knkx-live-a.edge.audiocdn.com/6285_128k`

## One-Time Setup

### 1. Create the virtual environment

The venv must be self-contained — install packages directly into it. Do **not**
use `--system-site-packages` and do **not** rename/move a venv folder after
creating it (the `activate` script hardcodes its own absolute path).

```bash
cd /home/pi5-max/cat_monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

### 2. System dependencies

`rpicam-vid` / `rpicam-still` (libcamera / `rpicam-apps`), `ffmpeg`, and a
running PulseAudio/PipeWire-Pulse session (for the alarm radio and
recording pipelines).

MediaMTX itself is not a systemd service — `app.py` launches and restarts it
directly as a subprocess, picking `mediamtx_RGB.yml` or `mediamtx_IR.yml`
depending on the time of day.

## Running

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

**Attach to the running tmux session** (systemd and `start_app.sh` both use a
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

- Fullscreen toggle for the live stream
- Last Alarm Recording — plays the most recently completed alarm clip, and
  auto-refreshes every 30 seconds
- While the page is open, it pings `/api/viewer-alive` every 5 seconds; this
  heartbeat is what tells the IR-illuminator logic "someone is watching"

There is currently no web UI or API for LED color/brightness or alarm
scheduling — `ALARM_TIMES` and the LED/IR behavior are configured directly in
`app.py` (that richer control panel exists only in the older `app_BU.py`).

## API Reference

| Method & Path | Purpose |
|---|---|
| `GET /` | Web dashboard (HTML/JS) |
| `GET /api/last-alarm-recording` | Status and URL for the latest completed alarm recording |
| `GET /last-alarm-recording.mp4` | Latest completed alarm recording |
| `POST /api/viewer-alive` | Heartbeat from the dashboard; resets the "someone is viewing" timeout used by the IR illuminator logic |

## Day/Night Switching & IR Illuminator

- At startup, `app.py` computes today's dawn/sunrise/
  sunset/dusk via `astral` for the configured location (edit the
  `LocationInfo` call in `app.py` if you move the Pi) and derives
  `IR_OFF_TIME`/`IR_ON_TIME`.
- When the desired mode changes, MediaMTX is stopped and restarted with
  `mediamtx_RGB.yml` (day) or `mediamtx_IR.yml` (night).
- While in IR mode, if a viewer has pinged recently or a recording is in
  progress, the app grabs a still with `rpicam-still`, computes the 70th
  percentile brightness, and turns the IR LED (GPIO D17) on/off using
  hysteresis thresholds (`IR_BRIGHTNESS_THRESHOLD_UPPER`/`_LOWER`) so it
  doesn't flicker near the boundary.
- `test.py` plus the sample images in `Images/bright` and `Images/dark` are
  an offline helper for re-deriving those threshold constants if the camera
  or environment changes.

## Alarm System

- `ALARM_TIMES` is a fixed list of `"HH:MM"` strings in `app.py` — there's no
  persistence or API; edit the list and restart the app to change the
  schedule.
- Each trigger starts three threads:
  - `run_effect` — turns the LED ring blue for `ALARM_EFFECT_DURATION`
    seconds, then back to idle
  - `run_radio` — streams `RADIO_URL` through `ffmpeg` to a PulseAudio sink
    for `ALARM_RADIO_DURATION` seconds
  - `run_record` — records the camera's RTSP stream with `ffmpeg` for
    `ALARM_RECORDING_DURATION` seconds and atomically replaces
    `last_alarm_recording.mp4` on success

## Analog Clock

When no alarm effect is running, the LED ring shows the current hour and
minute as two dim markers, refreshed once a minute (`run_analog_clock`).

## Troubleshooting

**Service won't start**
```bash
sudo systemctl status cat-monitor.service
sudo journalctl -u cat-monitor.service -n 50
```

**Camera not working**
```bash
rpicam-vid --camera 1 --nopreview -t 1000
```

**LEDs not working**
```bash
.venv/bin/python -c "import board, neopixel; print(board.D12)"
```

**No alarm sound**
```bash
pactl info
ffmpeg -hide_banner -i https://knkx-live-a.edge.audiocdn.com/6285_128k -f pulse cat-monitor-radio
```

**Video loads on one device but not another** — see the note under
[Web Interface](#web-interface) about accessing via the Pi's real address.

## `auto.crt` / `auto.key`

These are a TLS certificate/key pair that MediaMTX's MoQ (Media over QUIC)
server auto-generates and saves in this directory the first time it starts,
per the `moqServerKey: auto.key` / `moqServerCert: auto.crt` settings in the
`mediamtx_*.yml` configs. They regenerate automatically if missing, so they
don't need to exist in the repo (already gitignored) and are safe to delete.
You don't need them unless you intend to use MediaMTX's MoQ endpoint —
the dashboard here streams over WebRTC/WHEP instead.

## License

No license file is currently included in this repository.


