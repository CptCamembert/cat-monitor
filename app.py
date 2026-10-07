import json
import math
import os
import io
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from typing import Any
import logging

from PIL import Image, ImageStat
import numpy as np

from flask import Flask, Response, jsonify, request, send_file

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import dawn, dusk, sunrise, sunset

import digitalio

# setup flask
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# setup neopixel ring
try:
  import board
  import neopixel
except Exception as exc:  # pragma: no cover
  raise RuntimeError(f"NeoPixel hardware not available: {exc}")

PIXEL_COUNT = 12
PIXEL_PIN = board.D12

IDLE_COLOR = (2, 0, 0)

pixels = neopixel.NeoPixel(
    PIXEL_PIN,
    PIXEL_COUNT,
    brightness=1.0,
    auto_write=False,
)


# setup ir emitter
ir_pin = digitalio.DigitalInOut(board.D17)
ir_pin.direction = digitalio.Direction.OUTPUT


# setup alarms
ALARM_TIMES = ["06:58","11:58","16:58","21:58"]

ALARM_EFFECT_DURATION = 300
ALARM_EFFECT_COLOR = (0, 0, 10)

ALARM_RADIO_DURATION = 300
ALARM_RADIO_VOLUME = 0.5
RADIO_URL = "https://knkx-live-a.edge.audiocdn.com/6285_128k"

APP_DIR = os.path.dirname(__file__)

ALARM_RECORDING_DURATION = 600
ALARM_RECORDING_PATH = os.path.join(APP_DIR, "last_alarm_recording.mp4")
ALARM_RECORDING_TEMP_PATH = os.path.join(APP_DIR, "last_alarm_recording.tmp.mp4")
CAMERA_RTSP_URL = "rtsp://127.0.0.1:8554/cam1"

MEDIAMTX_BIN = os.path.join(APP_DIR, "mediamtx")
MEDIAMTX_PATH_RGB = os.path.join(APP_DIR, "mediamtx_RGB.yml")
MEDIAMTX_PATH_IR = os.path.join(APP_DIR, "mediamtx_IR.yml")

VIEWER_TIMEOUT = 10  # seconds
last_viewer_ping = 0.0


# setup logging
LOG_FILE = os.path.join(APP_DIR, "logfile.log")
LOG_BACKUP_FILE = os.path.join(APP_DIR, "logfile_last.log")

if os.path.isfile(LOG_FILE):
  os.replace(LOG_FILE, LOG_BACKUP_FILE)

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
app.logger.addHandler(file_handler)
app.logger.propagate = False


# setup up sunrise/sunset clock
location = LocationInfo(timezone="Europe/Berlin", latitude=48.4028, longitude=11.7412)
tz = ZoneInfo(location.timezone)
today = date.today()

dawn_time = dawn(location.observer, date = today, tzinfo=tz)
sunrise_time = sunrise(location.observer, date=today, tzinfo=tz)
sunset_time = sunset(location.observer, date=today, tzinfo=tz)
dusk_time = dusk(location.observer, date = today, tzinfo=tz)
IR_OFF_TIME = sunrise_time + (sunrise_time - dawn_time)
IR_ON_TIME = sunset_time - (dusk_time - sunset_time)
now = datetime.now(tz)

app.logger.info(f"Today's dawn:    {dawn_time.strftime('%H:%M')}")
app.logger.info(f"Today's sunrise: {sunrise_time.strftime('%H:%M')}")
app.logger.info(f"Today's sunset:  {sunset_time.strftime('%H:%M')}")
app.logger.info(f"Today's dusk:    {dusk_time.strftime('%H:%M')}")
app.logger.info(f"IR will toggle off at {IR_OFF_TIME.strftime('%H:%M')}")
app.logger.info(f"IR will toggle on at  {IR_ON_TIME.strftime('%H:%M')}")

current_IR_mode = None
desired_IR_mode = not(sunrise_time < now < sunset_time)
IR_BRIGHTNESS_THRESHOLD_PERCENTILE = 70
IR_BRIGHTNESS_THRESHOLD_UPPER = 32
IR_BRIGHTNESS_THRESHOLD_LOWER = 28

camera_lock = threading.Lock()


# --------- threads ----------

effect_thread = threading.Thread()
record_thread = threading.Thread()

def _apply_led_color(color: list[int]) -> None:
  pixels.fill(tuple(color))
  pixels.show()

def _apply_ir() -> None:
  # are we in IR-mode?
  if current_IR_mode:

    # is someone watching or recording?
    if record_thread.is_alive() or time.monotonic() - last_viewer_ping < VIEWER_TIMEOUT:
  
      # take picture and calculate avg brightness
      with camera_lock:
        result = subprocess.run(
          [
            "rpicam-still",
            "--camera", "1",
            "-n",
            "-t", "100",
            "--width", "320",
            "--height", "240",
            "-o", "-",
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.DEVNULL,
          check=True
        )

      image = Image.open(io.BytesIO(result.stdout)).convert("L")
      image_pixels = np.array(image)
      brightness = np.percentile(image_pixels, IR_BRIGHTNESS_THRESHOLD_PERCENTILE)

      # make hysteresis threshold
      threshold = IR_BRIGHTNESS_THRESHOLD_UPPER if ir_pin.value else IR_BRIGHTNESS_THRESHOLD_LOWER

      # is the image too dark?
      if brightness < threshold:
        if not ir_pin.value:
          app.logger.info("Turning on IR emitter")
          ir_pin.value = True

        return

  # none of the above apply, so turn off IR emitter
  if ir_pin.value:
    app.logger.info("Turning off IR emitter")
    ir_pin.value = False
    return


# ---------- alarm things ----------

def run_timer():
  while True:

    time.sleep(1)

    # turn on IR if needed
    _apply_ir()
        
    # skip if currently recording
    if not record_thread.is_alive():

      # manage alarm
      now = datetime.now(tz)
      current_time = now.strftime("%H:%M")
      if current_time in ALARM_TIMES:
        app.logger.info("Triggering alarm...")
        trigger_alarm()
        continue

      # manage camera mode
      global desired_IR_mode
      desired_IR_mode = not(IR_OFF_TIME < now < IR_ON_TIME)
      if current_IR_mode != desired_IR_mode:
        if desired_IR_mode:
          app.logger.info("Restarting cameras, switching to IR-mode")
        else:
          app.logger.info("Restarting cameras, switching to RGB-mode")
        start_mediamtx()
        continue

def trigger_alarm():
  # start effect thread
  global effect_thread
  effect_thread = threading.Thread(target=run_effect, daemon=True)
  effect_thread.start()
  # start radio thread
  threading.Thread(target=run_radio, daemon=True).start()
  # start recording thread
  global record_thread
  record_thread = threading.Thread(target=run_record, daemon=True)
  record_thread.start()

def run_effect():
  # make led ring blue
  app.logger.info("Effect thread started")
  _apply_led_color(ALARM_EFFECT_COLOR)
  time.sleep(ALARM_EFFECT_DURATION)
  _apply_led_color(IDLE_COLOR)
  app.logger.info("Effect thread stopped")

def run_radio():
  # configure audio environment
  app.logger.info("Radio thread started")
  audio_env = os.environ.copy()
  if not audio_env.get("XDG_RUNTIME_DIR"):
    runtime_dir = f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime_dir):
      audio_env["XDG_RUNTIME_DIR"] = runtime_dir

  # play radio
  subprocess.run(
    [
      "ffmpeg",
      "-hide_banner",
      "-loglevel",
      "warning",
      "-i",
      RADIO_URL,
      "-t",
      str(ALARM_RADIO_DURATION),
      "-af",
      f"volume={ALARM_RADIO_VOLUME}",
      "-f",
      "pulse",
      "cat-monitor-radio",
    ],
    env=audio_env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )
  app.logger.info("Radio thread stopped")

def run_record():
  # delete old temp recording
  app.logger.info("Recording thread started")
  try:
    os.remove(ALARM_RECORDING_TEMP_PATH)
  except FileNotFoundError:
    pass

  # record
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
        str(ALARM_RECORDING_DURATION),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        ALARM_RECORDING_TEMP_PATH,
      ],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      # stderr=subprocess.DEVNULL,
      check=False,
    )

    # replace old recording with new recording
    if result.returncode == 0 and os.path.getsize(ALARM_RECORDING_TEMP_PATH) > 0:
      os.replace(ALARM_RECORDING_TEMP_PATH, ALARM_RECORDING_PATH)
  except (OSError, subprocess.SubprocessError):
    pass
  finally:
    # delete old temp recording
    try:
      os.remove(ALARM_RECORDING_TEMP_PATH)
    except FileNotFoundError:
      pass

    app.logger.info("Recording thread stopped")


# ---------- mediamtx things ----------

mediamtx_process = None

def start_mediamtx():
  global mediamtx_process, current_IR_mode

  # stop if already running
  if mediamtx_process is not None:
    if mediamtx_process.poll() is None:
      mediamtx_process.terminate()
      try:
        mediamtx_process.wait(timeout=5)
      except subprocess.TimeoutExpired:
        mediamtx_process.kill()
        mediamtx_process.wait()
    mediamtx_process = None

  # start new process
  mediamtx_path = MEDIAMTX_PATH_IR if desired_IR_mode else MEDIAMTX_PATH_RGB
  mediamtx_process = subprocess.Popen(
    [MEDIAMTX_BIN, mediamtx_path],
    cwd=APP_DIR,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )
  current_IR_mode = desired_IR_mode


# ---------- web things ----------

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
        .recording-content {
          display: grid;
          gap: 16px;
          padding: 0 16px 16px;
        }
        .recording-video {
          width: 100%;
          background: #000;
          aspect-ratio: 16 / 9;
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
        #fullscreenToggle {
          width: 100%;
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
          <section class="panel recording-panel">
            <div id="recordingControls" class="recording-content">
              <div id="recordingEmpty" style="color: var(--muted);">No completed alarm recording yet.</div>
              <video id="lastAlarmRecording" class="recording-video" controls preload="metadata" hidden></video>
            </div>
          </section>
        </div>
      </div>

      <script>
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

        const lastAlarmRecording = document.getElementById('lastAlarmRecording');
        const recordingEmpty = document.getElementById('recordingEmpty');
        let recordingUrl = null;

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

        async function pingViewerAlive() {
          try {
            await fetch('/api/viewer-alive', {
              method: 'POST'
            });
          } catch (_) {
          }
        }

        function init() {
          loadLastAlarmRecording();
          window.setInterval(loadLastAlarmRecording, 30000);

          pingViewerAlive();
          window.setInterval(pingViewerAlive, 5000);
        }

        init();
      </script>
    </body>
    </html>
    """

@app.get("/api/last-alarm-recording")
def last_alarm_recording_api():
  if not os.path.isfile(ALARM_RECORDING_PATH):
    return jsonify({"available": False})
  modified = int(os.path.getmtime(ALARM_RECORDING_PATH))
  return jsonify({"available": True,"url": f"/last-alarm-recording.mp4?v={modified}"})

@app.get("/last-alarm-recording.mp4")
def last_alarm_recording() -> Response:
  if not os.path.isfile(ALARM_RECORDING_PATH):
    return Response(status=404)
  return send_file(ALARM_RECORDING_PATH, mimetype="video/mp4", conditional=True)

@app.post("/api/viewer-alive")
def viewer_alive():
    global last_viewer_ping
    last_viewer_ping = time.monotonic()
    return Response(status=204)


# ---------- analog clock ----------

LED_OFFSET = 3

def run_analog_clock() -> None:
  # make the LED ring into an anlog clock
  while True:
    if not effect_thread.is_alive():
      now = datetime.now()

      hour = now.hour
      hour_index = hour % 12
      hour_pixel = (hour_index + LED_OFFSET) % PIXEL_COUNT
      
      minute = now.minute
      minute_index = minute // 5
      minute_pixel = (minute_index + LED_OFFSET) % PIXEL_COUNT

      pixels.fill(IDLE_COLOR)
      pixels[hour_pixel] = (0, 0, 0)
      pixels[minute_pixel] = tuple(c * 5 for c in IDLE_COLOR)
      pixels.show()

    time.sleep(60)

# ---------- entry point ----------

if __name__ == "__main__":
  _apply_led_color(IDLE_COLOR)
  # timer thread
  timer_thread = threading.Thread(target=run_timer, daemon=True)
  timer_thread.start()
  analog_clock_thread = threading.Thread(target=run_analog_clock, daemon=True)
  analog_clock_thread.start()
  app.run(host="0.0.0.0", port=3000, debug=False, use_reloader=False)
