"""Birdcam web UI — single entrypoint.

    python3 app.py            # on the Pi: real GPIO + rpicam-vid
    BIRDCAM_MOCK=1 python3 app.py   # dev laptop: simulated sensor + camera

This process is the single owner of the PWM/I2C hardware, the camera and the
microphone; the sensor/LED loop, the camera pipeline and the audio capture run
as background threads inside it.
"""
import atexit
import json
import subprocess
import threading
import time
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, abort)

from audio import AudioManager

from camera import CameraManager, rotate_jpeg
from cleanup import CleanupManager
from config import Config
from hardware import create_hardware
from ledcontrol import LedController
from motion import MotionDetector
import status as system_status

app = Flask(__name__)

config = Config()
CAPTURES_DIR = (Path(__file__).parent / config["captures_dir"]).resolve()
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

hardware = create_hardware()
led = LedController(hardware, config)
camera = CameraManager(config, CAPTURES_DIR)
audio = AudioManager(config)
audio.attach_camera(camera)
camera.audio = audio
motion = MotionDetector(camera, config)
cleanup = CleanupManager(config, CAPTURES_DIR, camera)


def _shutdown():
    cleanup.stop()
    motion.stop()
    camera.stop()
    audio.stop()
    led.stop()
    hardware.cleanup()


# ---------- pages ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- live video ----------

@app.route("/stream")
def stream():
    return Response(camera.mjpeg_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/snapshot", methods=["POST"])
def snapshot():
    try:
        name = camera.snapshot()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"saved": name})


@app.route("/api/record/start", methods=["POST"])
def record_start():
    try:
        name = camera.start_recording()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"recording": name})


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    try:
        name, meta = camera.stop_recording()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"saved": name, **meta})


# ---------- LED / sensor ----------

@app.route("/api/led")
def led_get():
    return jsonify(led.get_state())

@app.route("/api/led", methods=["POST"])
def led_set():
    data = request.get_json(silent=True) or {}
    try:
        if "mode" in data:
            led.set_mode(data["mode"])
        if "brightness" in data:
            led.set_manual_brightness(data["brightness"])
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(led.get_state())


@app.route("/api/history")
def history():
    now = time.time()
    try:
        end = float(request.args.get("end", now))
        if "start" in request.args:
            start = float(request.args["start"])
        else:
            start = end - min(float(request.args.get("seconds", 3600)), 14 * 86400)
        points = max(10, min(int(request.args.get("points", 400)), 2000))
    except ValueError:
        return jsonify({"error": "bad start/end/points"}), 400
    pts = led.get_history(start=start, end=end, max_points=points)
    return jsonify([{"t": t, "level": lv, "brightness": b} for t, lv, b in pts])


# ---------- motion detection ----------

@app.route("/api/motion")
def motion_get():
    return jsonify(motion.state())


@app.route("/api/motion", methods=["POST"])
def motion_set():
    data = request.get_json(silent=True) or {}
    changes = {}
    if "enabled" in data:
        changes["motion_enabled"] = 1 if data["enabled"] else 0
    if "sensitivity" in data:
        changes["motion_sensitivity"] = data["sensitivity"]
    _, errors = config.update(changes)
    if errors:
        return jsonify({"error": errors}), 400
    return jsonify(motion.state())


# ---------- audio ----------

@app.route("/audio/live")
def audio_live():
    """Endless WAV stream of the mic for an <audio> element."""
    return Response(audio.live_stream(), mimetype="audio/wav",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/audio")
def audio_get():
    return jsonify(audio.state())


@app.route("/api/audio", methods=["POST"])
def audio_set():
    data = request.get_json(silent=True) or {}
    keys = {"enabled": "audio_enabled", "trigger": "sound_trigger"}
    changes = {}
    for k, cfg_key in keys.items():
        if k in data:
            changes[cfg_key] = 1 if data[k] else 0
    if "threshold_db" in data:
        changes["sound_threshold_db"] = data["threshold_db"]
    if "gain_db" in data:
        changes["audio_gain_db"] = data["gain_db"]
    _, errors = config.update(changes)
    if errors:
        return jsonify({"error": errors}), 400
    return jsonify(audio.state())


# ---------- gallery ----------

MEDIA_EXTS = {".jpg", ".mjpeg", ".mp4", ".wav"}  # .wav: served, not listed


def _safe_media_path(name):
    """Resolve a gallery filename, refusing traversal or unknown types."""
    if "/" in name or "\\" in name or name.startswith("."):
        abort(400)
    path = CAPTURES_DIR / name
    if path.suffix not in MEDIA_EXTS or not path.is_file():
        abort(404)
    return path


def _taken_at(path):
    """When a capture was taken, from its rec_/snap_YYYYmmdd_HHMMSS name (the
    Pi's local time). Converting to mp4 rewrites mtime, so mtime would put old
    clips at the top with today's date."""
    try:
        stamp = path.stem.split("_", 1)[1]
        return time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S"))
    except (IndexError, ValueError):
        return path.stat().st_mtime


@app.route("/api/gallery")
def gallery():
    kinds = {".mp4": "mp4", ".mjpeg": "mjpeg"}
    transcoding = camera.transcoding_names()
    items = []
    for path in CAPTURES_DIR.iterdir():
        if path.suffix not in MEDIA_EXTS or path.suffix == ".wav" or not path.is_file():
            continue
        kind = kinds.get(path.suffix, "image")
        item = {
            "name": path.name,
            "type": kind,
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "taken": _taken_at(path),
        }
        if kind != "image":
            sidecar = path.with_suffix(".json")
            if sidecar.exists():
                try:
                    item.update(json.loads(sidecar.read_text()))
                except (ValueError, OSError):
                    pass
        if kind == "mjpeg" and path.with_suffix(".wav").is_file():
            item["audio_file"] = path.with_suffix(".wav").name
        if path.name in transcoding:
            item["processing"] = True
        items.append(item)
    items.sort(key=lambda i: i["taken"], reverse=True)
    return jsonify({"items": items,
                    "disk": system_status.disk_usage(CAPTURES_DIR),
                    "cleanup": cleanup.state()})


@app.route("/media/<name>")
def media(name):
    _safe_media_path(name)
    return send_from_directory(
        CAPTURES_DIR, name,
        as_attachment=request.args.get("download") == "1")


@app.route("/thumb/<name>")
def thumb(name):
    path = _safe_media_path(name)
    if path.suffix == ".jpg":
        return send_from_directory(CAPTURES_DIR, name)
    if path.suffix == ".wav":
        abort(404)
    if path.suffix == ".mp4":
        frame = camera.mp4_thumbnail(path)
    else:
        frame = CameraManager.first_frame(path)
        rotate = CameraManager._read_meta(path).get("rotate", 0)
        if frame is not None and rotate:
            frame = rotate_jpeg(frame, rotate)
    if frame is None:
        abort(404)
    return Response(frame, mimetype="image/jpeg")


@app.route("/replay/<name>")
def replay(name):
    path = _safe_media_path(name)
    if path.suffix != ".mjpeg":
        abort(400)
    fps = config["stream_fps"]
    sidecar = path.with_suffix(".json")
    if sidecar.exists():
        try:
            fps = json.loads(sidecar.read_text()).get("fps") or fps
        except (ValueError, OSError):
            pass
    return Response(camera.replay_stream(path, fps),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/media/<name>/convert", methods=["POST"])
def media_convert(name):
    path = _safe_media_path(name)
    if path.suffix != ".mjpeg":
        return jsonify({"error": "not an mjpeg recording"}), 400
    try:
        camera.convert_to_mp4(name)
    except FileNotFoundError:
        abort(404)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"converting": name})


@app.route("/api/media/<name>", methods=["DELETE"])
def media_delete(name):
    path = _safe_media_path(name)
    rec_state = camera.recording_state()
    if rec_state.get("file") == name:
        return jsonify({"error": "recording in progress"}), 409
    if path.suffix == ".wav":
        return jsonify({"error": "delete the recording, not its audio"}), 400
    path.unlink()
    if path.suffix in (".mjpeg", ".mp4"):
        for extra in (path.with_suffix(".json"), path.with_suffix(".wav")):
            if extra.exists():
                extra.unlink()
    return jsonify({"deleted": name})


# ---------- status & settings ----------

@app.route("/api/status")
def api_status():
    s = system_status.get_status(CAPTURES_DIR)
    s.update({
        "led": led.get_state(),
        "recording": camera.recording_state(),
        "motion": motion.state(),
        "audio": audio.state(),
        "cleanup": cleanup.state(),
        "mock": {"gpio": hardware.mock, "camera": camera.mock, "audio": audio.mock},
        "server_time": time.time(),
    })
    return jsonify(s)


# ---------- system power ----------

def _deferred_system_cmd(cmd):
    """Run a power command after a short delay so the HTTP response can flush
    to the browser before the Pi goes down."""
    def run():
        time.sleep(1.0)
        try:
            subprocess.run(cmd, check=False)
        except OSError as e:
            print(f"[system] '{' '.join(cmd)}' failed: {e}")
    threading.Thread(target=run, daemon=True).start()


@app.route("/api/system/reboot", methods=["POST"])
def system_reboot():
    # Never reboot the dev laptop: in mock mode this is a no-op.
    if hardware.mock:
        return jsonify({"ok": True, "mock": True, "action": "reboot"})
    _deferred_system_cmd(["sudo", "shutdown", "-r", "now"])
    return jsonify({"ok": True, "action": "reboot"})


@app.route("/api/system/shutdown", methods=["POST"])
def system_shutdown():
    # Never power off the dev laptop: in mock mode this is a no-op.
    if hardware.mock:
        return jsonify({"ok": True, "mock": True, "action": "shutdown"})
    _deferred_system_cmd(["sudo", "shutdown", "-h", "now"])
    return jsonify({"ok": True, "action": "shutdown"})


@app.route("/api/config")
def config_get():
    return jsonify(config.as_dict())


@app.route("/api/config", methods=["POST"])
def config_set():
    data = request.get_json(silent=True) or {}
    applied, errors = config.update(data)
    camera_keys = {"flip_h", "flip_v", "rotation", "fov", "af_mode", "af_range", "lens_position"}
    if any(k.startswith("stream_") or k in camera_keys for k in applied):
        camera.restart_pipeline()
    if applied.get("auto_mp4"):
        camera._queue_backlog()      # convert clips recorded while it was off
    status_code = 200 if not errors else 400
    return jsonify({"applied": applied, "errors": errors,
                    "config": config.as_dict()}), status_code


if __name__ == "__main__":
    led.start()
    camera.start()
    audio.start()
    motion.start()
    cleanup.start()
    atexit.register(_shutdown)
    mock_note = " (MOCK mode)" if hardware.mock or camera.mock or audio.mock else ""
    print(f"birdcam ui on http://0.0.0.0:{config['port']}{mock_note}")
    # threaded=True: each MJPEG viewer holds a connection open
    app.run(host="0.0.0.0", port=config["port"], threaded=True,
            debug=False, use_reloader=False)
