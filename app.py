"""Birdcam web UI — single entrypoint.

    python3 app.py            # on the Pi: real GPIO + rpicam-vid
    BIRDCAM_MOCK=1 python3 app.py   # dev laptop: simulated sensor + camera

This process is the single owner of GPIO 17/18 and the camera; the sensor/
LED loop and the camera pipeline run as background threads inside it.
"""
import atexit
import json
import time
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, abort)

from camera import CameraManager
from config import Config
from hardware import create_hardware
from ledcontrol import LedController
import status as system_status

app = Flask(__name__)

config = Config()
CAPTURES_DIR = (Path(__file__).parent / config["captures_dir"]).resolve()
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

hardware = create_hardware()
led = LedController(hardware, config)
camera = CameraManager(config, CAPTURES_DIR)


def _shutdown():
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


# ---------- gallery ----------

MEDIA_EXTS = {".jpg", ".mjpeg"}


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
    items = []
    for path in CAPTURES_DIR.iterdir():
        if path.suffix not in MEDIA_EXTS or not path.is_file():
            continue
        item = {
            "name": path.name,
            "type": "video" if path.suffix == ".mjpeg" else "image",
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
        }
        if item["type"] == "video":
            sidecar = path.with_suffix(".json")
            if sidecar.exists():
                try:
                    item.update(json.loads(sidecar.read_text()))
                except (ValueError, OSError):
                    pass
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
    if path.suffix == ".mjpeg" and sidecar.exists():
        sidecar.unlink()
    return jsonify({"deleted": name})


# ---------- status & settings ----------

@app.route("/api/status")
def api_status():
    s = system_status.get_status(CAPTURES_DIR)
    s.update({
        "led": led.get_state(),
        "recording": camera.recording_state(),
        "mock": {"gpio": hardware.mock, "camera": camera.mock},
        "server_time": time.time(),
    })
    return jsonify(s)


@app.route("/api/config")
def config_get():
    return jsonify(config.as_dict())


@app.route("/api/config", methods=["POST"])
def config_set():
    data = request.get_json(silent=True) or {}
    applied, errors = config.update(data)
    if any(k.startswith("stream_") for k in applied):
        camera.restart_pipeline()
    status_code = 200 if not errors else 400
    return jsonify({"applied": applied, "errors": errors,
                    "config": config.as_dict()}), status_code


if __name__ == "__main__":
    led.start()
    camera.start()
    atexit.register(_shutdown)
    mock_note = " (MOCK mode)" if hardware.mock or camera.mock else ""
    print(f"birdcam ui on http://0.0.0.0:{config['port']}{mock_note}")
    # threaded=True: each MJPEG viewer holds a connection open
    app.run(host="0.0.0.0", port=config["port"], threaded=True,
            debug=False, use_reloader=False)
