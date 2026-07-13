"""Camera pipeline: one rpicam-vid MJPEG process feeds everything.

A reader thread splits the JPEG stream into frames and publishes the
latest one; live viewers, snapshots and recordings all consume from that
single pipeline, so the live view never pauses and only one process ever
touches the camera.

Recordings are stored as raw concatenated JPEG frames (.mjpeg) plus a
small .json sidecar with fps/duration; the app replays them as an MJPEG
stream so they play in the browser without ffmpeg. VLC also plays the
raw files directly.

Mock mode (no rpicam-vid / BIRDCAM_MOCK=1) synthesizes frames with
Pillow so the whole UI works on the dev laptop.
"""
import io
import json
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def is_mock():
    return bool(os.environ.get("BIRDCAM_MOCK")) or shutil.which("rpicam-vid") is None


class CameraManager:
    """Owns the camera process, the latest frame, and recording state."""

    def __init__(self, config, captures_dir):
        self.config = config
        self.captures_dir = Path(captures_dir)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.mock = is_mock()

        self._cond = threading.Condition()
        self._frame = None          # latest complete JPEG
        self._frame_seq = 0
        self._stop = threading.Event()
        self._proc = None

        self._rec_lock = threading.Lock()
        self._rec_file = None       # open file handle while recording
        self._rec_path = None
        self._rec_started = None
        self._rec_frames = 0

        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    # --- lifecycle ---

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kill_proc()
        if self.recording:
            self.stop_recording()
        self._thread.join(timeout=5)

    def restart_pipeline(self):
        """Apply new stream settings: kill rpicam-vid; the camera thread
        restarts it with the current config. No-op state-wise in mock mode
        (the mock loop reads config every frame)."""
        self._kill_proc()

    def _kill_proc(self):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    # --- frame plumbing ---

    def _publish(self, jpeg):
        with self._cond:
            self._frame = jpeg
            self._frame_seq += 1
            self._cond.notify_all()
        with self._rec_lock:
            if self._rec_file:
                self._rec_file.write(jpeg)
                self._rec_frames += 1

    def wait_frame(self, last_seq, timeout=5.0):
        """Block until a frame newer than last_seq exists. Returns (jpeg, seq)
        or (None, last_seq) on timeout."""
        with self._cond:
            if self._frame_seq <= last_seq:
                self._cond.wait(timeout)
            if self._frame_seq > last_seq and self._frame is not None:
                return self._frame, self._frame_seq
            return None, last_seq

    def latest_frame(self):
        with self._cond:
            return self._frame

    def mjpeg_stream(self):
        """Generator yielding multipart MJPEG parts for live viewing."""
        seq = 0
        while not self._stop.is_set():
            frame, seq = self.wait_frame(seq)
            if frame is None:
                continue
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                   + frame + b"\r\n")

    # --- snapshot / recording ---

    def snapshot(self):
        """Save the latest frame as a still. Returns the filename."""
        frame = self.latest_frame()
        if frame is None:
            raise RuntimeError("no frame available yet")
        name = time.strftime("snap_%Y%m%d_%H%M%S") + ".jpg"
        (self.captures_dir / name).write_bytes(frame)
        return name

    @property
    def recording(self):
        return self._rec_file is not None

    def recording_state(self):
        with self._rec_lock:
            if not self._rec_file:
                return {"recording": False}
            return {
                "recording": True,
                "duration": round(time.time() - self._rec_started, 1),
                "file": self._rec_path.name,
            }

    def start_recording(self):
        with self._rec_lock:
            if self._rec_file:
                raise RuntimeError("already recording")
            name = time.strftime("rec_%Y%m%d_%H%M%S") + ".mjpeg"
            self._rec_path = self.captures_dir / name
            self._rec_file = open(self._rec_path, "wb")
            self._rec_started = time.time()
            self._rec_frames = 0
            return name

    def stop_recording(self):
        with self._rec_lock:
            if not self._rec_file:
                raise RuntimeError("not recording")
            self._rec_file.close()
            duration = time.time() - self._rec_started
            meta = {
                "frames": self._rec_frames,
                "duration": round(duration, 2),
                "fps": round(self._rec_frames / duration, 2) if duration > 0 else 0,
                "width": self.config["stream_width"],
                "height": self.config["stream_height"],
            }
            self._rec_path.with_suffix(".json").write_text(json.dumps(meta))
            name = self._rec_path.name
            self._rec_file = None
            self._rec_path = None
            return name, meta

    # --- replay of saved .mjpeg recordings ---

    @staticmethod
    def iter_recorded_frames(path):
        """Yield JPEG frames from a raw .mjpeg file."""
        data = Path(path).read_bytes()
        pos = 0
        while True:
            start = data.find(JPEG_SOI, pos)
            if start < 0:
                return
            end = data.find(JPEG_EOI, start + 2)
            if end < 0:
                return
            yield data[start:end + 2]
            pos = end + 2

    @staticmethod
    def first_frame(path, chunk_size=65536):
        """Return the first JPEG frame of a .mjpeg file without reading it all
        (used for gallery thumbnails)."""
        buf = bytearray()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    return None
                buf += chunk
                start = buf.find(JPEG_SOI)
                if start >= 0:
                    end = buf.find(JPEG_EOI, start + 2)
                    if end >= 0:
                        return bytes(buf[start:end + 2])

    def replay_stream(self, path, fps):
        interval = 1.0 / max(fps, 1)
        for frame in self.iter_recorded_frames(path):
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                   + frame + b"\r\n")
            time.sleep(interval)

    # --- the camera process / reader thread ---

    def _run(self):
        if self.mock:
            self._run_mock()
        else:
            self._run_real()

    def _rpicam_cmd(self):
        return [
            "rpicam-vid",
            "-t", "0",
            "--codec", "mjpeg",
            "--width", str(self.config["stream_width"]),
            "--height", str(self.config["stream_height"]),
            "--framerate", str(self.config["stream_fps"]),
            "--quality", str(self.config["stream_quality"]),
            "--nopreview",
            "--flush",
            "-o", "-",
        ]

    def _run_real(self):
        while not self._stop.is_set():
            cmd = self._rpicam_cmd()
            print(f"[camera] starting: {' '.join(cmd)}")
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0)
            except OSError as e:
                print(f"[camera] failed to start rpicam-vid: {e}")
                self._stop.wait(10)
                continue
            self._read_frames(self._proc.stdout)
            rc = self._proc.wait()
            if not self._stop.is_set():
                print(f"[camera] rpicam-vid exited (rc={rc}), restarting in 3s")
                self._stop.wait(3)

    def _read_frames(self, pipe):
        """Split the raw MJPEG byte stream into JPEG frames."""
        buf = bytearray()
        while not self._stop.is_set():
            chunk = pipe.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(JPEG_SOI)
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(JPEG_EOI, start + 2)
                if end < 0:
                    if start > 0:
                        del buf[:start]
                    break
                self._publish(bytes(buf[start:end + 2]))
                del buf[:end + 2]

    # --- mock camera ---

    def _run_mock(self):
        print("[camera] mock mode: synthesizing frames")
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            Image = None
        t0 = time.monotonic()
        placeholder = self._placeholder_jpeg()
        while not self._stop.is_set():
            w, h = self.config["stream_width"], self.config["stream_height"]
            fps = self.config["stream_fps"]
            if Image:
                t = time.monotonic() - t0
                img = Image.new("RGB", (w, h), (18, 24, 18))
                d = ImageDraw.Draw(img)
                # nest box hole + a "bird" bobbing around
                d.ellipse([w * 0.4, h * 0.15, w * 0.6, h * 0.5], fill=(5, 5, 5))
                bx = w * (0.5 + 0.25 * math.sin(t * 0.9))
                by = h * (0.65 + 0.08 * math.sin(t * 2.3))
                d.ellipse([bx - 30, by - 20, bx + 30, by + 20], fill=(120, 90, 60))
                d.ellipse([bx + 14, by - 34, bx + 40, by - 8], fill=(130, 100, 70))
                d.text((10, 10), time.strftime("%Y-%m-%d %H:%M:%S")
                       + "  [MOCK CAMERA]", fill=(200, 200, 200))
                out = io.BytesIO()
                img.save(out, "JPEG", quality=self.config["stream_quality"])
                self._publish(out.getvalue())
            else:
                self._publish(placeholder)
            self._stop.wait(1.0 / fps)

    @staticmethod
    def _placeholder_jpeg():
        # minimal valid 1x1 grey JPEG for mock mode without Pillow
        import base64
        return base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
            "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
            "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
