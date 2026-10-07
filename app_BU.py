import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any

from flask import Flask, Response, jsonify, request, send_file

try:
  import board
  import neopixel
except Exception as exc:  # pragma: no cover
  raise RuntimeError(f"NeoPixel hardware not available: {exc}")

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

PIXEL_COUNT = 12
PIXEL_PIN = board.D12

DEFAULT_LED_ENABLED = True
DEFAULT_LED_EFFECT = "solid"
DEFAULT_LED_BRIGHTNESS = 0.05
DEFAULT_LED_COLOR = [255, 255, 255]
IDLE_OFF_BRIGHTNESS = 0.01
IDLE_OFF_COLOR = (255, 0, 0)
ALARM_COLOR = (0, 0, 255)
ALARM_MIN_BRIGHTNESS = 0.05
ALARM_MAX_BRIGHTNESS = 0.25
ALARM_BREATH_PERIOD = 3.0

pixels = neopixel.NeoPixel(
    PIXEL_PIN,
    PIXEL_COUNT,
    brightness=DEFAULT_LED_BRIGHTNESS,
    auto_write=False,
)

ALARM_STORAGE_PATH = os.path.join(os.path.dirname(__file__),
                                  "alarm_settings.json")
RADIO_URL = "https://knkx-live-a.edge.audiocdn.com/6285_128k"
ALARM_RECORDING_SECONDS = 10 * 60
LAST_ALARM_RECORDING_PATH = os.path.join(os.path.dirname(__file__),
                                         "last_alarm_recording.mp4")
LAST_ALARM_RECORDING_TEMP_PATH = os.path.join(
  os.path.dirname(__file__), "last_alarm_recording.tmp.mp4")
CAMERA_RTSP_URL = "rtsp://127.0.0.1:8554/cam1"

state = {
    "enabled": DEFAULT_LED_ENABLED,
    "effect": DEFAULT_LED_EFFECT,
    "brightness": DEFAULT_LED_BRIGHTNESS,
    "color": list(DEFAULT_LED_COLOR),
    "watchers": 0,
    "auto_idle_off": True,
    "idle_off_sent": False,
    "alarm_active": False,
    "alarm_until": 0.0,
    "alarm_duration": 5.0,
    "alarm_volume": 0.5,
    "radio_volume": 0.5,
    "scheduled_alarms": [],
}


def _load_scheduled_alarms() -> None:
  try:
    with open(ALARM_STORAGE_PATH, "r", encoding="utf-8") as handle:
      payload = json.load(handle)
    alarms = payload.get("scheduled_alarms", []) if isinstance(payload,
                                                               dict) else []
    if isinstance(alarms, list):
      state["scheduled_alarms"] = alarms
  except FileNotFoundError:
    state["scheduled_alarms"] = []
  except Exception:
    state["scheduled_alarms"] = []


def _save_scheduled_alarms() -> None:
  try:
    with open(ALARM_STORAGE_PATH, "w", encoding="utf-8") as handle:
      json.dump({"scheduled_alarms": state.get("scheduled_alarms", [])},
                handle,
                indent=2)
  except Exception:
    pass


chase_stop = threading.Event()
chase_thread: threading.Thread | None = None
chase_lock = threading.Lock()
alarm_stop = threading.Event()
alarm_thread: threading.Thread | None = None
alarm_lock = threading.Lock()
watcher_stop = threading.Event()
watcher_thread: threading.Thread | None = None
camera_lock = threading.Lock()
last_frame = bytearray()
radio_process: subprocess.Popen[bytes] | None = None
radio_lock = threading.Lock()
recording_thread: threading.Thread | None = None
recording_lock = threading.Lock()


def _get_mediamtx_reader_count() -> int:
  urls = [
      "http://127.0.0.1:9997/v3/paths/get/cam1",
      "http://127.0.0.1:9997/v3/paths/get?name=cam1",
  ]

  for url in urls:
    try:
      req = urllib.request.Request(url, headers={"Accept": "application/json"})
      with urllib.request.urlopen(req, timeout=1.0) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
      readers = payload.get("readers")
      if isinstance(readers, list):
        return len(readers)
      if isinstance(readers, dict):
        items = readers.get("items")
        if isinstance(items, list):
          return len(items)
        return int(readers.get("count", 0) or 0)
      if isinstance(readers, (int, float)):
        return int(readers)
      return 0
    except Exception:
      continue

  return 0


def _apply_led_color(color: list[int],
                     brightness: float,
                     effect: str = "solid") -> None:
  pixels.brightness = brightness
  if effect == "chase":
    return
  pixels.fill(tuple(color))
  pixels.show()


def _set_idle_off_state() -> None:
  """Stop active effects and apply the single canonical idle-off LED state."""
  chase_stop.set()
  state.update({
      "enabled": False,
      "effect": "solid",
      "brightness": IDLE_OFF_BRIGHTNESS,
      "color": list(IDLE_OFF_COLOR),
      "idle_off_sent": True,
  })
  _apply_led_color(state["color"], state["brightness"])


def _sync_idle_state() -> None:
  if state.get("alarm_active") and time.monotonic() < state.get(
      "alarm_until", 0):
    return

  count = _get_mediamtx_reader_count()
  previous = state.get("watchers", 0)
  state["watchers"] = count

  if count > 0:
    state["idle_off_sent"] = False
    if state.get("enabled") and state.get("effect") == "solid":
      _apply_led_color(state["color"], state["brightness"], effect="solid")
    return

  if previous > 0 and state.get(
      "auto_idle_off") and not state.get("idle_off_sent"):
    _set_idle_off_state()


def force_idle_off_if_no_viewers() -> bool:
  if state.get("alarm_active") and time.monotonic() < state.get(
      "alarm_until", 0):
    return False

  count = _get_mediamtx_reader_count()
  state["watchers"] = count

  if count > 0:
    state["idle_off_sent"] = False
    return False

  if state.get("auto_idle_off") and not state.get("idle_off_sent"):
    _set_idle_off_state()
    return True

  return False


def _current_day_key() -> str:
  now = datetime.now()
  return now.strftime("%Y-%m-%d")


def start_radio(volume: float) -> None:
  """Launch the alarm radio at a fixed volume; ffmpeg's pulse output is the
  only pipeline confirmed to reach the speaker on this device."""
  global radio_process
  if not shutil.which("ffmpeg"):
    return
  volume = max(0.0, min(1.0, float(volume)))
  audio_env = os.environ.copy()
  if not audio_env.get("XDG_RUNTIME_DIR"):
    runtime_dir = f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime_dir):
      audio_env["XDG_RUNTIME_DIR"] = runtime_dir
  with radio_lock:
    stop_radio()
    try:
      radio_process = subprocess.Popen(
          [
              "ffmpeg",
              "-hide_banner",
              "-loglevel",
              "warning",
              "-i",
              RADIO_URL,
              "-af",
              f"volume={volume}",
              "-f",
              "pulse",
              "cat-monitor-radio",
          ],
          env=audio_env,
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
      )
    except Exception:
      radio_process = None


def stop_radio() -> None:
  global radio_process
  if radio_process is not None and radio_process.poll() is None:
    radio_process.terminate()
    try:
      radio_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
      radio_process.kill()
  radio_process = None


def _record_last_alarm_video() -> None:
  if not shutil.which("ffmpeg"):
    return

  try:
    os.remove(LAST_ALARM_RECORDING_TEMP_PATH)
  except FileNotFoundError:
    pass

  try:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            CAMERA_RTSP_URL,
            "-t",
            str(ALARM_RECORDING_SECONDS),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            LAST_ALARM_RECORDING_TEMP_PATH,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0 and os.path.getsize(
        LAST_ALARM_RECORDING_TEMP_PATH) > 0:
      os.replace(LAST_ALARM_RECORDING_TEMP_PATH, LAST_ALARM_RECORDING_PATH)
  except (OSError, subprocess.SubprocessError):
    pass
  finally:
    try:
      os.remove(LAST_ALARM_RECORDING_TEMP_PATH)
    except FileNotFoundError:
      pass


def start_alarm_recording() -> None:
  global recording_thread
  with recording_lock:
    if recording_thread is not None and recording_thread.is_alive():
      return
    recording_thread = threading.Thread(target=_record_last_alarm_video,
                                        daemon=True)
    recording_thread.start()


def _trigger_alarm(duration: float) -> None:
  with alarm_lock:
    state["alarm_duration"] = duration
    state["alarm_prev_state"] = {
        "enabled": bool(state.get("enabled", DEFAULT_LED_ENABLED)),
        "effect": str(state.get("effect", DEFAULT_LED_EFFECT)),
        "brightness": float(state.get("brightness", DEFAULT_LED_BRIGHTNESS)),
        "color": list(state.get("color", DEFAULT_LED_COLOR)),
    }
    state["alarm_active"] = True
    state["alarm_until"] = time.monotonic() + duration
    chase_stop.set()

  global alarm_thread
  if alarm_thread is not None and alarm_thread.is_alive():
    alarm_stop.set()
    alarm_thread.join(timeout=0.2)

  alarm_stop.clear()
  alarm_thread = threading.Thread(target=run_alarm,
                                  args=(duration, ),
                                  daemon=True)
  alarm_thread.start()
  start_radio(state.get("alarm_volume", 0.5))


def _check_scheduled_alarms() -> None:
  if state.get("alarm_active") and time.monotonic() < state.get(
      "alarm_until", 0):
    return

  now = datetime.now()
  current_time = now.strftime("%H:%M")
  today_key = _current_day_key()

  for alarm in state.get("scheduled_alarms", []):
    if not alarm.get("enabled", True):
      continue
    if alarm.get("time") == current_time and alarm.get(
        "triggered_on") != today_key:
      duration_minutes = float(alarm.get("duration", 5.0))
      duration_seconds = max(1.0, duration_minutes * 60.0)
      _trigger_alarm(duration_seconds)
      start_alarm_recording()
      alarm["triggered_on"] = today_key
      return

  for alarm in state.get("scheduled_alarms", []):
    if alarm.get("triggered_on") == today_key:
      continue
    if alarm.get("triggered_on") and alarm.get("triggered_on") != today_key:
      alarm["triggered_on"] = None


def watcher_monitor() -> None:
  while not watcher_stop.is_set():
    _sync_idle_state()
    _check_scheduled_alarms()
    time.sleep(2.0)


def _open_camera_stream() -> subprocess.Popen[bytes]:
  return subprocess.Popen(
      [
          "rpicam-vid",
          "--camera",
          "0",
          "--nopreview",
          "--width",
          "1280",
          "--height",
          "720",
          "--framerate",
          "30",
          "--codec",
          "mjpeg",
          "--output",
          "-",
      ],
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      bufsize=0,
  )


def set_led_state(enabled: bool, color: list[int], effect: str,
                  brightness: float) -> dict[str, Any]:
  global chase_thread

  if state.get("alarm_active") and time.monotonic() < state.get(
      "alarm_until", 0):
    return state

  with chase_lock:
    state["enabled"] = enabled
    state["effect"] = effect
    state["brightness"] = brightness
    state["color"] = [max(0, min(255, int(v))) for v in color]
    pixels.brightness = brightness

    if not enabled:
      _set_idle_off_state()
      return state

    if effect == "chase":
      chase_stop.clear()
      if chase_thread is None or not chase_thread.is_alive():
        chase_thread = threading.Thread(target=run_chase, daemon=True)
        chase_thread.start()
      state["idle_off_sent"] = False
      return state

    chase_stop.set()
    state["idle_off_sent"] = False
    _apply_led_color(state["color"], state["brightness"])
    return state


def run_chase() -> None:
  while not chase_stop.is_set():
    for idx in range(PIXEL_COUNT):
      if chase_stop.is_set():
        return
      pixels.fill((0, 0, 0))
      pixels[idx] = tuple(state["color"])
      pixels.show()
      time.sleep(0.08)
    if chase_stop.is_set():
      return


def run_alarm(duration: float) -> None:
  end = time.monotonic() + duration
  started = time.monotonic()
  while time.monotonic() < end:
    if not state.get("alarm_active") or alarm_stop.is_set():
      return

    phase = (time.monotonic() - started) % ALARM_BREATH_PERIOD
    progress = (math.sin((2 * math.pi * phase / ALARM_BREATH_PERIOD) -
                         (math.pi / 2)) + 1) / 2
    pixels.brightness = ALARM_MIN_BRIGHTNESS + (
        (ALARM_MAX_BRIGHTNESS - ALARM_MIN_BRIGHTNESS) * progress)
    pixels.fill(ALARM_COLOR)
    pixels.show()
    time.sleep(0.02)

  if state.get("alarm_active"):
    state["alarm_active"] = False
    state["alarm_until"] = 0.0
    stop_radio()
    previous = state.get("alarm_prev_state", {})
    state["alarm_prev_state"] = {}

    if previous:
      set_led_state(
          enabled=bool(previous.get("enabled", True)),
          color=list(
              previous.get("color", state.get("color", DEFAULT_LED_COLOR))),
          effect=str(previous.get("effect", DEFAULT_LED_EFFECT)),
          brightness=float(
              previous.get("brightness",
                           state.get("brightness", DEFAULT_LED_BRIGHTNESS))),
      )


def capture_snapshot() -> bytes:
  with camera_lock:
    proc = subprocess.run(
        [
            "rpicam-jpeg",
            "--camera",
            "0",
            "--width",
            "640",
            "--height",
            "360",
            "--nopreview",
            "-t",
            "80",
            "-o",
            "-",
        ],
        capture_output=True,
        check=False,
    )

    if proc.returncode != 0 or not proc.stdout:
      if last_frame:
        return bytes(last_frame)
      return b""

    data = bytes(proc.stdout)
    last_frame.clear()
    last_frame.extend(data)
    return data


def start_mjpeg_stream() -> subprocess.Popen[bytes]:
  return _open_camera_stream()


def mjpeg_generator(proc: subprocess.Popen[bytes]):
  if proc.stdout is None:
    return

  buffer = bytearray()
  while proc.poll() is None:
    chunk = proc.stdout.read(65536)
    if not chunk:
      break
    buffer.extend(chunk)

    while True:
      start = buffer.find(b"\xff\xd8")
      if start == -1:
        if len(buffer) > 65536:
          del buffer[:-65536]
        break
      end = buffer.find(b"\xff\xd9", start)
      if end == -1:
        break
      frame = bytes(buffer[start:end + 2])
      del buffer[:end + 2]
      yield (b"--FRAME\r\n"
             b"Content-Type: image/jpeg\r\n"
             b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
             frame + b"\r\n")


@app.get("/")
def index() -> str:
  return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Cat Monitor</title>
      <style>
        :root {
          --bg: #0f172a;
          --panel: #111827;
          --panel-2: #1f2937;
          --text: #e2e8f0;
          --muted: #94a3b8;
          --accent: #38bdf8;
          --border: #334155;
        }
        * { box-sizing: border-box; }
        body {
          margin: 0;
          font-family: Arial, sans-serif;
          background: var(--bg);
          color: var(--text);
          min-height: 100vh;
          display: grid;
          place-items: center;
        }
        .wrap {
          width: min(100vw, 1000px);
          background: var(--panel);
          border: 0;
          border-radius: 0;
          padding: 0;
          box-shadow: none;
        }
        .layout {
          display: grid;
          grid-template-columns: 1fr;
          gap: 10px;
        }
        .panel {
          background: var(--panel-2);
          border: 0;
          border-radius: 0;
          padding: 0;
        }
        .panel.controls {
          padding: 12px 16px 16px;
        }
        .feed {
          width: min(100%, 1280px);
          aspect-ratio: 16 / 9;
          display: block;
          background: #000;
          border: 1px solid var(--border);
          overflow: hidden;
        }
        .feed:fullscreen {
          width: 100%;
          height: 100%;
          border: 0;
        }
        #cameraVideo {
          display: block;
          width: 100%;
          height: 100%;
          background: #000;
          object-fit: contain;
          aspect-ratio: 16 / 9;
        }
        .controls {
          display: grid;
          gap: 16px;
        }
        .row {
          display: flex;
          gap: 12px;
          align-items: center;
          flex-wrap: wrap;
        }
        .button-stack {
          display: flex;
          flex-direction: column;
          gap: 8px;
          width: 100%;
        }
        .button-stack button {
          width: 100%;
        }
        .control-panel {
          padding: 0;
        }
        .control-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          min-height: 48px;
          padding: 0 12px 0 16px;
        }
        .control-header h2 {
          margin: 0;
          font-size: 1rem;
        }
        .panel-toggle {
          width: 36px;
          height: 36px;
          padding: 0;
          background: transparent;
          color: var(--text);
          font-size: 1.2rem;
          line-height: 1;
        }
        .panel-toggle span {
          display: block;
        }
        .control-content {
          display: grid;
          gap: 16px;
          padding: 0 16px 16px;
        }
        .control-content[hidden] {
          display: none;
        }
        .recording-video {
          width: 100%;
          background: #000;
          aspect-ratio: 16 / 9;
        }
        label {
          color: var(--muted);
          font-size: 0.92rem;
        }
        input[type="color"] {
          width: 80px;
          height: 42px;
          border: none;
          background: transparent;
          padding: 0;
        }
        input[type="range"] {
          flex: 1;
          min-width: 120px;
        }
        button {
          padding: 10px 14px;
          border: 0;
          border-radius: 10px;
          background: var(--accent);
          color: #062033;
          font-weight: 700;
          cursor: pointer;
        }
        button.secondary {
          background: #cbd5e1;
          color: #0f172a;
        }
        .swatch {
          width: 36px;
          height: 36px;
          border-radius: 8px;
          border: 2px solid rgba(255,255,255,0.25);
          background: #000;
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="layout">
          <div class="panel">
            <div id="cameraFeed" class="feed">
              <video id="cameraVideo" autoplay playsinline muted></video>
            </div>
            <div class="panel controls" style="padding: 10px 16px;">
              <button id="fullscreenToggle" type="button">Fullscreen</button>
            </div>
          </div>
          <section class="panel control-panel">
            <div class="control-header">
              <h2>LED</h2>
              <button class="panel-toggle" type="button" aria-controls="ledControls" aria-expanded="false" title="Expand LED controls"><span aria-hidden="true">&#x2304;</span></button>
            </div>
            <div id="ledControls" class="control-content" hidden>
              <div class="row">
                <label for="colorPicker">Color</label>
                <input id="colorPicker" type="color" value="#ffffff" />
                <div id="swatch" class="swatch"></div>
              </div>
              <div class="row">
                <label>R</label>
                <input id="rSlider" type="range" min="0" max="255" value="255" />
                <span id="rVal">255</span>
              </div>
              <div class="row">
                <label>G</label>
                <input id="gSlider" type="range" min="0" max="255" value="255" />
                <span id="gVal">255</span>
              </div>
              <div class="row">
                <label>B</label>
                <input id="bSlider" type="range" min="0" max="255" value="255" />
                <span id="bVal">255</span>
              </div>
              <div class="row">
                <label for="brightness">Brightness</label>
                <input id="brightness" type="range" min="0" max="1" step="0.01" value="0.05" />
                <span id="brightnessVal">0.05</span>
              </div>
              <div class="button-stack">
                <button id="powerToggle">On</button>
              </div>
            </div>
          </section>
          <section class="panel control-panel">
            <div class="control-header">
              <h2>Alarm</h2>
              <button class="panel-toggle" type="button" aria-controls="alarmControls" aria-expanded="false" title="Expand alarm controls"><span aria-hidden="true">&#x2304;</span></button>
            </div>
            <div id="alarmControls" class="control-content" hidden>
              <div class="row">
                <label for="alarmVolume">Alarm volume</label>
                <input id="alarmVolume" type="range" min="0" max="1" step="0.01" value="0.5" />
                <span id="alarmVolumeVal">0.50</span>
              </div>
              <div class="button-stack">
                <button id="alarmButton" class="secondary">Trigger alarm</button>
              </div>
              <div class="row" style="margin-top: 18px;">
                <label for="alarmSeconds">Alarm</label>
                <input id="alarmSeconds" type="number" min="1" max="60" step="1" value="5" style="width: 80px;" />
                <span>sec</span>
              </div>
              <div class="row" style="margin-top: 10px;">
                <label for="scheduledAlarmTime">Time</label>
                <input id="scheduledAlarmTime" type="time" value="11:58" />
                <label for="scheduledAlarmDuration">For</label>
                <input id="scheduledAlarmDuration" type="number" min="1" max="60" step="1" value="5" style="width: 80px;" />
                <span>min</span>
                <button id="addScheduledAlarm" class="secondary" type="button">Add alarm</button>
              </div>
              <div id="scheduledAlarmList" style="display: grid; gap: 8px; margin-top: 8px;"></div>
            </div>
          </section>
          <section class="panel control-panel">
            <div class="control-header">
              <h2>Last Alarm Recording</h2>
              <button class="panel-toggle" type="button" aria-controls="recordingControls" aria-expanded="false" title="Expand last alarm recording"><span aria-hidden="true">&#x2304;</span></button>
            </div>
            <div id="recordingControls" class="control-content" hidden>
              <div id="recordingEmpty" style="color: var(--muted);">No completed alarm recording yet.</div>
              <video id="lastAlarmRecording" class="recording-video" controls preload="metadata" hidden></video>
            </div>
          </section>
        </div>
      </div>

      <script>
        const state = {
          enabled: true,
          effect: 'solid',
          brightness: 0.05,
          color: [255, 255, 255]
        };

        const cameraVideo = document.getElementById('cameraVideo');
        const cameraFeed = document.getElementById('cameraFeed');
        const fullscreenToggle = document.getElementById('fullscreenToggle');
        const cameraUrl = 'http://' + window.location.hostname + ':8889/cam1/whep?video=1&audio=0';

        function syncFullscreenLabel() {
          fullscreenToggle.textContent = document.fullscreenElement === cameraFeed
            ? 'Exit fullscreen'
            : 'Fullscreen';

          if (!document.fullscreenElement && screen.orientation?.unlock) {
            screen.orientation.unlock();
          }
        }

        fullscreenToggle.addEventListener('click', async () => {
          if (document.fullscreenElement === cameraFeed) {
            await document.exitFullscreen();
            return;
          }
          await cameraFeed.requestFullscreen();
          if (screen.orientation?.lock) {
            await screen.orientation.lock('landscape').catch(() => {});
          }
        });
        document.addEventListener('fullscreenchange', syncFullscreenLabel);

        document.querySelectorAll('.panel-toggle').forEach((toggle) => {
          toggle.addEventListener('click', () => {
            const content = document.getElementById(toggle.getAttribute('aria-controls'));
            const collapsed = content.toggleAttribute('hidden');
            toggle.setAttribute('aria-expanded', String(!collapsed));
            toggle.title = collapsed ? 'Expand controls' : 'Collapse controls';
            toggle.querySelector('span').textContent = String.fromCodePoint(
              collapsed ? 0x2304 : 0x2303);
          });
        });

        const script = document.createElement('script');
        script.src = 'http://' + window.location.hostname + ':8889/reader.js';
        script.async = true;

        let webrtcReader = null;

        script.onload = () => {
          webrtcReader = new MediaMTXWebRTCReader({
            url: cameraUrl,
            onTrack: (evt) => {
              cameraVideo.srcObject = evt.streams[0];
              cameraVideo.play().catch(() => {});
            },
            onError: (err) => {
              console.error('WebRTC stream error:', err);
            }
          });
        };

        script.onerror = () => {
          console.error('Failed to load MediaMTX reader.js');
        };

        document.head.appendChild(script);
        window.addEventListener('beforeunload', () => {
          if (webrtcReader) {
            webrtcReader.close();
          }
        });

        const colorPicker = document.getElementById('colorPicker');
        const rSlider = document.getElementById('rSlider');
        const gSlider = document.getElementById('gSlider');
        const bSlider = document.getElementById('bSlider');
        const brightnessSlider = document.getElementById('brightness');
        const alarmVolumeSlider = document.getElementById('alarmVolume');
        const powerToggle = document.getElementById('powerToggle');
        const alarmButton = document.getElementById('alarmButton');
        const alarmSeconds = document.getElementById('alarmSeconds');
        const scheduledAlarmTime = document.getElementById('scheduledAlarmTime');
        const scheduledAlarmDuration = document.getElementById('scheduledAlarmDuration');
        const addScheduledAlarm = document.getElementById('addScheduledAlarm');
        const scheduledAlarmList = document.getElementById('scheduledAlarmList');
        const swatch = document.getElementById('swatch');
        const lastAlarmRecording = document.getElementById('lastAlarmRecording');
        const recordingEmpty = document.getElementById('recordingEmpty');
        let recordingUrl = null;

        function rgbToHex(r, g, b) {
          return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('');
        }

        function updateColorFromRgb() {
          const r = Number(rSlider.value);
          const g = Number(gSlider.value);
          const b = Number(bSlider.value);
          state.color = [r, g, b];
          colorPicker.value = rgbToHex(r, g, b);
          swatch.style.background = rgbToHex(r, g, b);
          sendLedState();
        }

        function updateFromColorInput() {
          const hex = colorPicker.value;
          const r = parseInt(hex.slice(1, 3), 16);
          const g = parseInt(hex.slice(3, 5), 16);
          const b = parseInt(hex.slice(5, 7), 16);
          rSlider.value = r;
          gSlider.value = g;
          bSlider.value = b;
          state.color = [r, g, b];
          swatch.style.background = hex;
          sendLedState();
        }

        function syncLabels() {
          document.getElementById('rVal').textContent = rSlider.value;
          document.getElementById('gVal').textContent = gSlider.value;
          document.getElementById('bVal').textContent = bSlider.value;
          document.getElementById('brightnessVal').textContent = Number(brightnessSlider.value).toFixed(2);
          document.getElementById('alarmVolumeVal').textContent = Number(alarmVolumeSlider.value).toFixed(2);
        }

        function applyLedState(ledState) {
          state.enabled = Boolean(ledState.enabled);
          state.effect = ledState.effect || 'solid';
          if (state.enabled) {
            state.brightness = Number(ledState.brightness);
            state.color = ledState.color || [255, 255, 255];
          } else {
            // LED is off/idle (dim red indicator). Show a sensible default
            // (white, 25%) in the controls instead, so turning the power
            // back on doesn't start from the idle-off color/brightness.
            state.brightness = 0.25;
            state.color = [255, 255, 255];
          }
          colorPicker.value = rgbToHex(...state.color);
          rSlider.value = state.color[0];
          gSlider.value = state.color[1];
          bSlider.value = state.color[2];
          brightnessSlider.value = state.brightness;
          powerToggle.textContent = state.enabled ? 'On' : 'Off';
          swatch.style.background = colorPicker.value;
          syncLabels();
        }

        async function sendLedState() {
          state.brightness = Number(brightnessSlider.value);
          const payload = {
            enabled: state.enabled,
            effect: state.effect,
            brightness: state.brightness,
            color: state.color
          };
          await fetch('/api/led', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
        }

        rSlider.addEventListener('input', () => { syncLabels(); updateColorFromRgb(); });
        gSlider.addEventListener('input', () => { syncLabels(); updateColorFromRgb(); });
        bSlider.addEventListener('input', () => { syncLabels(); updateColorFromRgb(); });
        brightnessSlider.addEventListener('input', () => { syncLabels(); sendLedState(); });
        alarmVolumeSlider.addEventListener('input', () => {
          syncLabels();
          fetch('/api/alarm-volume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ volume: Number(alarmVolumeSlider.value) })
          });
        });
        colorPicker.addEventListener('input', updateFromColorInput);

        powerToggle.addEventListener('click', async () => {
          state.enabled = !state.enabled;
          powerToggle.textContent = state.enabled ? 'On' : 'Off';
          await sendLedState();
        });

        alarmButton.addEventListener('click', async () => {
          const duration = Number(alarmSeconds.value) || 5;
          await fetch('/api/alarm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration, volume: Number(alarmVolumeSlider.value) })
          });
        });

        async function renderScheduledAlarms() {
          const resp = await fetch('/api/alarms');
          const payload = await resp.json();
          const alarms = payload.alarms || [];

          if (!alarms.length) {
            scheduledAlarmList.innerHTML = '<div style="color: var(--muted); font-size: 0.9rem;">No scheduled alarms.</div>';
            return;
          }

          scheduledAlarmList.innerHTML = alarms.map((alarm, idx) => `
            <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; padding:8px 10px; background: rgba(255,255,255,0.03); border-radius:8px;">
              <span>${alarm.time} for ${Number(alarm.duration).toFixed(0)} min</span>
              <button type="button" class="secondary" data-remove-index="${idx}">Remove</button>
            </div>
          `).join('');

          scheduledAlarmList.querySelectorAll('[data-remove-index]').forEach((button) => {
            button.addEventListener('click', async () => {
              const idx = Number(button.dataset.removeIndex);
              await fetch('/api/alarms', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ index: idx })
              });
              renderScheduledAlarms();
            });
          });
        }

        async function loadLastAlarmRecording() {
          try {
            const response = await fetch('/api/last-alarm-recording');
            const recording = await response.json();
            if (!recording.available || recording.url === recordingUrl) {
              return;
            }
            recordingUrl = recording.url;
            lastAlarmRecording.src = recording.url;
            lastAlarmRecording.hidden = false;
            recordingEmpty.hidden = true;
          } catch (_) {
            // The panel remains empty until a completed clip exists.
          }
        }

        addScheduledAlarm.addEventListener('click', async () => {
          const time = scheduledAlarmTime.value;
          const duration = Number(scheduledAlarmDuration.value) || 5;
          if (!time) {
            return;
          }
          await fetch('/api/alarms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ time, duration })
          });
          renderScheduledAlarms();
        });

        function init() {
          fetch('/api/led')
            .then(response => response.json())
            .then(payload => applyLedState(payload.state))
            .catch(() => syncLabels());
          renderScheduledAlarms();
          loadLastAlarmRecording();
          window.setInterval(loadLastAlarmRecording, 30000);
        }

        init();
      </script>
    </body>
    </html>
    """


@app.get("/stream.mjpg")
def stream_mjpg() -> Response:
  proc = start_mjpeg_stream()

  def stream():
    try:
      yield from mjpeg_generator(proc)
    finally:
      if proc.poll() is None:
        proc.terminate()
        try:
          proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
          proc.kill()

  return Response(stream(),
                  mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/snapshot.jpg")
def snapshot() -> Response:
  try:
    image_bytes = capture_snapshot()
    if not image_bytes:
      return Response(b"", status=503, mimetype="image/jpeg")
    return Response(image_bytes, mimetype="image/jpeg")
  except Exception:
    if last_frame:
      return Response(bytes(last_frame), mimetype="image/jpeg")
    return Response(b"", status=503, mimetype="image/jpeg")


@app.get("/api/last-alarm-recording")
def last_alarm_recording_api():
  if not os.path.isfile(LAST_ALARM_RECORDING_PATH):
    return jsonify({"available": False})
  modified = int(os.path.getmtime(LAST_ALARM_RECORDING_PATH))
  return jsonify({
      "available": True,
      "url": f"/last-alarm-recording.mp4?v={modified}",
  })


@app.get("/last-alarm-recording.mp4")
def last_alarm_recording() -> Response:
  if not os.path.isfile(LAST_ALARM_RECORDING_PATH):
    return Response(status=404)
  return send_file(LAST_ALARM_RECORDING_PATH, mimetype="video/mp4",
                   conditional=True)


@app.route("/api/led", methods=["GET", "POST"])
def led_api():
  if request.method == "GET":
    return jsonify({"ok": True, "state": state})

  payload = request.get_json(silent=True) or {}
  enabled = bool(payload.get("enabled", True))
  effect = payload.get("effect", "solid")
  brightness = float(payload.get("brightness", state["brightness"]))
  color = payload.get("color", state["color"])
  if not isinstance(color, list) or len(color) != 3:
    color = state["color"]
  state.update(
      set_led_state(enabled=enabled,
                    color=color,
                    effect=effect,
                    brightness=brightness))
  return jsonify({"ok": True, "state": state})


@app.get("/api/alarms")
def list_scheduled_alarms():
  return jsonify({"alarms": state.get("scheduled_alarms", [])})


@app.post("/api/alarms")
def add_scheduled_alarm():
  payload = request.get_json(silent=True) or {}
  alarm_time = str(payload.get("time", "")).strip()
  duration = float(payload.get("duration", 5.0))
  if not re.fullmatch(r"\d{2}:\d{2}", alarm_time):
    return jsonify({"ok": False, "error": "invalid time format"}), 400
  duration = max(1.0, min(60.0, duration))

  state.setdefault("scheduled_alarms", []).append({
      "time": alarm_time,
      "duration": duration,
      "enabled": True,
      "triggered_on": None,
  })
  _save_scheduled_alarms()
  return jsonify({"ok": True, "alarms": state["scheduled_alarms"]})


@app.delete("/api/alarms")
def remove_scheduled_alarm():
  payload = request.get_json(silent=True) or {}
  index = int(payload.get("index", -1))
  alarms = state.get("scheduled_alarms", [])
  if 0 <= index < len(alarms):
    del alarms[index]
  _save_scheduled_alarms()
  return jsonify({"ok": True, "alarms": alarms})


@app.post("/api/alarm")
def trigger_alarm():
  payload = request.get_json(silent=True) or {}
  duration = float(payload.get("duration", state.get("alarm_duration", 5.0)))
  duration = max(1.0, min(60.0, duration))
  volume = float(payload.get("volume", state.get("alarm_volume", 0.5)))
  state["alarm_volume"] = max(0.0, min(1.0, volume))
  _trigger_alarm(duration)
  return jsonify({
      "ok": True,
      "active": True,
      "duration": duration,
      "state": state
  })


@app.post("/api/alarm-volume")
def set_alarm_volume():
  payload = request.get_json(silent=True) or {}
  volume = float(payload.get("volume", state.get("alarm_volume", 0.5)))
  state["alarm_volume"] = max(0.0, min(1.0, volume))
  return jsonify({"ok": True, "volume": state["alarm_volume"]})


@app.post("/api/idle-off")
def idle_off_if_no_viewers():
  off = force_idle_off_if_no_viewers()
  return jsonify({
      "ok": True,
      "off": off,
      "watchers": state.get("watchers", 0),
      "state": state
  })


_load_scheduled_alarms()

if __name__ == "__main__":
  set_led_state(enabled=True,
                color=state["color"],
                effect="solid",
                brightness=state["brightness"])
  if watcher_thread is None or not watcher_thread.is_alive():
    watcher_thread = threading.Thread(target=watcher_monitor, daemon=True)
    watcher_thread.start()
  app.run(host="0.0.0.0", port=3000, debug=False, use_reloader=False)
