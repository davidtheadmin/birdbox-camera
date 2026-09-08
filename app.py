"""Birdcam web UI — single entrypoint.

    python3 app.py            # on the Pi: real GPIO + rpicam-vid
    BIRDCAM_MOCK=1 python3 app.py   # dev laptop: simulated sensor + camera

This process is the single owner of GPIO 17/18 and the camera; the sensor/
LED loop and the camera pipeline run as background threads inside it.
"""
import atexit
import json
import subprocess
import threading
import time
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, abort)

from camera import CameraManager
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
motion = MotionDetector(camera, config)


def _shutdown():
    motion.stop()
    camera.stop()
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
    seconds = min(int(request.args.get("seconds", 3600)), 24 * 3600)
    pts = led.get_history(seconds=seconds)
    return jsonify([{"t": round(t, 1), "level": lv, "brightness": b}
                    for t, lv, b in pts])


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


# ---------- gallery ----------

MEDIA_EXTS = {".jpg", ".mjpeg", ".mp4"}


def _safe_media_path(name):
    """Resolve a gallery filename, refusing traversal or unknown types."""
    if "/" in name or "\\" in name or name.startswith("."):
        abort(400)
    path = CAPTURES_DIR / name
    if path.suffix not in MEDIA_EXTS or not path.is_file():
        abort(404)
    return path


@app.route("/api/gallery")
def gallery():
    kinds = {".mp4": "mp4", ".mjpeg": "mjpeg"}
    transcoding = camera.transcoding_names()
    items = []
    for path in CAPTURES_DIR.iterdir():
        if path.suffix not in MEDIA_EXTS or not path.is_file():
            continue
        kind = kinds.get(path.suffix, "image")
        item = {
            "name": path.name,
            "type": kind,
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
        }
        if kind != "image":
            sidecar = path.with_suffix(".json")
            if sidecar.exists():
                try:
                    item.update(json.loads(sidecar.read_text()))
                except (ValueError, OSError):
                    pass
        if path.name in transcoding:
            item["processing"] = True
        items.append(item)
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return jsonify({"items": items,
                    "disk": system_status.disk_usage(CAPTURES_DIR)})


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
    if path.suffix == ".mp4":
        frame = camera.mp4_thumbnail(path)
    else:
        frame = CameraManager.first_frame(path)
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


@app.route("/api/media/<name>", methods=["DELETE"])
def media_delete(name):
    path = _safe_media_path(name)
    rec_state = camera.recording_state()
    if rec_state.get("file") == name:
        return jsonify({"error": "recording in progress"}), 409
    path.unlink()
    sidecar = path.with_suffix(".json")
    if path.suffix in (".mjpeg", ".mp4") and sidecar.exists():
        sidecar.unlink()
    return jsonify({"deleted": name})


# ---------- status & settings ----------

@app.route("/api/status")
def api_status():
    s = system_status.get_status(CAPTURES_DIR)
    s.update({
        "led": led.get_state(),
        "recording": camera.recording_state(),
        "motion": motion.state(),
        "mock": {"gpio": hardware.mock, "camera": camera.mock},
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
    camera_keys = {"flip_h", "flip_v", "af_mode", "af_range", "lens_position"}
    if any(k.startswith("stream_") or k in camera_keys for k in applied):
        camera.restart_pipeline()
    status_code = 200 if not errors else 400
    return jsonify({"applied": applied, "errors": errors,
                    "config": config.as_dict()}), status_code


if __name__ == "__main__":
    led.start()
    camera.start()
    motion.start()
    atexit.register(_shutdown)
    mock_note = " (MOCK mode)" if hardware.mock or camera.mock else ""
    print(f"birdcam ui on http://0.0.0.0:{config['port']}{mock_note}")
    # threaded=True: each MJPEG viewer holds a connection open
    app.run(host="0.0.0.0", port=config["port"], threaded=True,
            debug=False, use_reloader=False)
