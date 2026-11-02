"""Camera pipeline. Three backends, chosen at start-up:

- picamera2 (pHAT units, default when available; see picam.py): the sensor
  delivers two streams at once. Recordings come from the main stream as
  hardware H.264 at rec_size/rec_fps (1080p30) with a few seconds of
  pre-roll; the live view, motion and snapshots use the small stream.
  Recordings are written as raw .h264 and muxed (no re-encode) to .mp4 with
  their .wav right after they stop.
- rpicam (fallback if Picamera2 is missing or fails to start): one
  rpicam-vid MJPEG process feeds everything, and recordings are the live
  stream saved as raw concatenated JPEGs (.mjpeg), converted to mp4 after.
- mock: synthetic frames for the dev laptop.

In all cases the latest live JPEG is published to viewers, snapshots and
motion detection from one place, and only this process touches the camera.

Mock mode (no rpicam-vid / BIRDCAM_MOCK=1) synthesizes frames with
Pillow so the whole UI works on the dev laptop.
"""
import io
import json
import queue
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import picam

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def is_mock():
    return bool(os.environ.get("BIRDCAM_MOCK")) or shutil.which("rpicam-vid") is None


def rotate_jpeg(jpeg, degrees):
    """Rotate JPEG bytes clockwise by 90/180/270; unchanged if Pillow is missing."""
    if not degrees:
        return jpeg
    try:
        from PIL import Image
    except ImportError:
        return jpeg
    op = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}[degrees]
    out = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).transpose(op).save(out, "JPEG", quality=90)
    return out.getvalue()


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


_ffmpeg_display_rotation = None


def ffmpeg_has_display_rotation():
    """True if ffmpeg can tag a stream as rotated without re-encoding via
    -display_rotation (ffmpeg >= 6.1). Older versions (Bookworm's 5.1) use
    the 'rotate' metadata tag instead, which 6.x dropped."""
    global _ffmpeg_display_rotation
    if _ffmpeg_display_rotation is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"],
                                 capture_output=True, text=True, timeout=20).stdout
            _ffmpeg_display_rotation = "-display_rotation" in out
        except (OSError, subprocess.TimeoutExpired):
            _ffmpeg_display_rotation = False
    return _ffmpeg_display_rotation


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

        self.backend = "mock" if self.mock else None   # decided when the thread starts
        self._pipe = None           # running picam.PicamPipeline
        self._restart = threading.Event()
        self.is_dark = lambda: False  # set by app.py: are the IR LEDs on?

        self._rec_lock = threading.Lock()
        self._rec_file = None       # .mjpeg file handle (rpicam/mock backends)
        self._rec_out = None        # picam.RecordBuffer writing the .h264 (picamera2)
        self._rec_preroll = 0.0
        self._rec_path = None
        self._rec_started = None
        self._rec_frames = 0
        self._rec_audio = False
        self._rec_rotate = 0

        self.audio = None           # AudioManager, if a mic is attached

        self._transcoding = set()   # .mjpeg names queued for or being converted to .mp4
        self._transcode_lock = threading.Lock()
        # One conversion at a time: the Pi 3A+ has 512 MB and a single hardware
        # encoder, and the live stream must keep its CPU.
        self._transcode_q = queue.Queue()
        self._transcode_thread = threading.Thread(
            target=self._transcode_loop, name="transcode", daemon=True)

        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    # --- lifecycle ---

    def start(self):
        self._thread.start()
        self._transcode_thread.start()
        self._queue_backlog(include_mjpeg=bool(self.config["auto_mp4"]))

    def stop(self):
        self._stop.set()
        self._restart.set()
        self._kill_proc()
        if self.recording:
            self.stop_recording()
        self._thread.join(timeout=5)

    def restart_pipeline(self):
        """Apply new camera settings: the camera thread tears the pipeline
        down and starts it again with the current config. A running
        recording is finished first. No-op state-wise in mock mode (the mock
        loop reads config every frame)."""
        if self.backend == "picamera2":
            self._restart.set()
        else:
            self._kill_proc()

    def state(self):
        pipe = self._pipe
        out = {"backend": self.backend}
        if self.backend == "picamera2" and pipe is not None:
            out.update({"rec_size": "x".join(map(str, pipe.rec_size)), "fps": pipe.fps,
                        "preroll": self.config["preroll_seconds"]})
        return out

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

    # --- orientation ---
    # The camera can only flip, so 180° is done there; the remaining 90° for
    # portrait (rotation 90/270) is applied downstream: CSS in the browser,
    # Pillow for snapshots/thumbnails, ffmpeg transpose for mp4.

    def _flips(self):
        rot180 = self.config["rotation"] in (180, 270)
        return bool(self.config["flip_h"]) ^ rot180, bool(self.config["flip_v"]) ^ rot180

    def soft_rotation(self):
        return 90 if self.config["rotation"] in (90, 270) else 0

    def snapshot(self):
        """Save the latest frame as a still. Returns the filename."""
        frame = self.latest_frame()
        if frame is None:
            raise RuntimeError("no frame available yet")
        name = time.strftime("snap_%Y%m%d_%H%M%S") + ".jpg"
        (self.captures_dir / name).write_bytes(rotate_jpeg(frame, self.soft_rotation()))
        return name

    @property
    def recording(self):
        return self._rec_path is not None

    def recording_state(self):
        with self._rec_lock:
            if self._rec_path is None:
                return {"recording": False}
            return {
                "recording": True,
                "duration": round(time.time() - self._rec_started + self._rec_preroll, 1),
                "file": self._rec_path.name,
                "audio": self._rec_audio,
            }

    def start_recording(self):
        with self._rec_lock:
            if self._rec_path is not None:
                raise RuntimeError("already recording")
            stem = time.strftime("rec_%Y%m%d_%H%M%S")
            pipe = self._pipe
            if self.backend == "picamera2" and pipe is not None:
                path = self.captures_dir / (stem + ".h264")
                self._rec_preroll = pipe.rec.start_file(path)   # writes the pre-roll
                self._rec_out = pipe.rec
            elif self.backend == "picamera2":
                raise RuntimeError("camera is restarting, try again in a moment")
            else:
                path = self.captures_dir / (stem + ".mjpeg")
                self._rec_file = open(path, "wb")
                self._rec_preroll = 0.0
            self._rec_path = path
            self._rec_started = time.time()
            self._rec_frames = 0
            self._rec_rotate = self.soft_rotation()   # frames are stored unrotated
            # Audio starts in the same lock so both files begin together, with
            # the same pre-roll as the video. (AudioManager never takes this
            # lock, so no deadlock.)
            self._rec_audio = bool(
                self.audio and self.audio.start_recording(
                    path.with_suffix(".wav"), preroll=self._rec_preroll))
            return path.name

    def stop_recording(self):
        with self._rec_lock:
            if self._rec_path is None:
                raise RuntimeError("not recording")
            if self._rec_out is not None:                       # picamera2: .h264
                stats = self._rec_out.stop_file()
                if self.audio:
                    self.audio.stop_recording()
                w, h = self.config["rec_size"].split("x")
                meta = {
                    "frames": stats["frames"],
                    "duration": stats["duration"],
                    "fps": stats["fps"] or self.config["rec_fps"],
                    "width": int(w), "height": int(h),
                    "codec": "h264",
                    "preroll": round(self._rec_preroll, 2),
                }
            else:                                               # rpicam/mock: .mjpeg
                self._rec_file.close()
                if self.audio:
                    self.audio.stop_recording()
                duration = time.time() - self._rec_started
                meta = {
                    "frames": self._rec_frames,
                    "duration": round(duration, 2),
                    "fps": round(self._rec_frames / duration, 2) if duration > 0 else 0,
                    "width": self.config["stream_width"],
                    "height": self.config["stream_height"],
                }
            meta.update({"audio": self._rec_audio, "rotate": self._rec_rotate})
            rec_path = self._rec_path
            name = rec_path.name
            self._rec_file = None
            self._rec_out = None
            self._rec_path = None
            self._rec_preroll = 0.0
            if meta["frames"] == 0:
                # No video arrived (camera stalled or restarted at once): an
                # empty clip only clutters the gallery.
                for f in (rec_path, rec_path.with_suffix(".wav")):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                print(f"[camera] {name}: no video frames, discarded")
                return name, dict(meta, discarded=True)
            rec_path.with_suffix(".json").write_text(json.dumps(meta))
        # .h264 is always muxed to mp4 (a raw stream won't play anywhere; it's
        # a copy, so it takes seconds). With auto_mp4 on, .mjpeg is converted
        # too; otherwise it waits for "to mp4" in the gallery.
        if (rec_path.suffix == ".h264" or self.config["auto_mp4"]) and not self._stop.is_set():
            try:
                self.convert_to_mp4(name)
            except (RuntimeError, FileNotFoundError) as e:
                print(f"[camera] not converting {name}: {e}")
        return name, meta

    # --- mp4 transcode (background queue, one at a time) ---

    def transcoding_names(self):
        with self._transcode_lock:
            return set(self._transcoding)

    def _queue_backlog(self, include_mjpeg=True):
        """Queue recordings not yet in mp4 (left over from a crash or a restart
        mid-conversion, or from before auto_mp4), oldest first, skipping ones
        that failed before."""
        paths = list(self.captures_dir.glob("rec_*.h264"))
        if include_mjpeg:
            paths += self.captures_dir.glob("rec_*.mjpeg")
        for path in sorted(paths, key=lambda p: p.name):
            meta = self._read_meta(path)
            if meta.get("mp4_failed"):
                continue
            try:
                self.convert_to_mp4(path.name)
            except (RuntimeError, FileNotFoundError):
                pass

    @staticmethod
    def _read_meta(path):
        sidecar = Path(path).with_suffix(".json")
        try:
            return json.loads(sidecar.read_text())
        except (ValueError, OSError):
            return {}

    def convert_to_mp4(self, name):
        """Queue a background mp4 conversion of an .mjpeg or .h264 recording."""
        path = self.captures_dir / name
        if path.suffix not in (".mjpeg", ".h264") or not path.is_file():
            raise FileNotFoundError(name)
        if not has_ffmpeg():
            raise RuntimeError("ffmpeg not installed")
        with self._transcode_lock:
            if path.name in self._transcoding:
                raise RuntimeError("already converting")
            self._transcoding.add(path.name)
        self._transcode_q.put(path)

    def _transcode_loop(self):
        while not self._stop.is_set():
            try:
                path = self._transcode_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if path.is_file() and path.suffix == ".h264":
                    self._mux_h264(path, self._read_meta(path))
                elif path.is_file():
                    self._transcode_worker(path, self._read_meta(path))
            except Exception as e:          # never let one bad file stop the queue
                print(f"[camera] transcode of {path.name} crashed: {e}")
            finally:
                with self._transcode_lock:
                    self._transcoding.discard(path.name)

    def _transcode_worker(self, mjpeg_path, meta):
        """mjpeg (+ wav) -> H.264/AAC mp4. Tries the Pi hardware encoder, falls
        back to software libx264, and as a last resort drops the audio. On
        success the .mjpeg and .wav are replaced by the .mp4."""
        fps = meta.get("fps") or self.config["stream_fps"] or 15
        mp4_path = mjpeg_path.with_suffix(".mp4")
        tmp = mjpeg_path.with_suffix(".mp4.tmp")
        wav_path = mjpeg_path.with_suffix(".wav")
        has_wav = wav_path.is_file() and wav_path.stat().st_size > 44
        video_in = ["-f", "mjpeg", "-r", str(fps), "-i", str(mjpeg_path)]
        audio_in = ["-i", str(wav_path), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "96k"]
        rotate = meta.get("rotate", 0)
        vf = ["-vf", "transpose=1"] if rotate == 90 else []   # 90° clockwise
        tail = vf + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        encoders = [
            ["-c:v", "h264_v4l2m2m", "-b:v", "4M"],                 # Pi hardware encoder
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"],  # software fallback
        ]
        attempts = []
        if has_wav:
            attempts += [(enc, True) for enc in encoders]
        attempts += [(enc, False) for enc in encoders]
        ok = muxed_audio = False
        for enc, with_audio in attempts:
            nice = ["nice", "-n", "19"] if shutil.which("nice") else []  # not on Windows
            cmd = (nice + ["ffmpeg", "-y", "-loglevel", "error"] + video_in
                   + (audio_in if with_audio else []) + enc + tail)
            try:
                r = subprocess.run(cmd,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                break
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                ok, muxed_audio = True, with_audio
                break
            if tmp.exists():
                tmp.unlink()
        if ok:
            tmp.replace(mp4_path)
            meta = dict(meta, container="mp4", audio=muxed_audio)
            mp4_path.with_suffix(".json").write_text(json.dumps(meta))
            # sidecar has the shared stem, so it stays. The .wav goes only if
            # it made it into the mp4; otherwise it stays as the audio copy.
            for old in [mjpeg_path] + ([wav_path] if muxed_audio else []):
                try:
                    old.unlink()
                except OSError:
                    pass
            print(f"[camera] transcoded {mjpeg_path.name} -> {mp4_path.name}")
        else:
            if tmp.exists():
                tmp.unlink()
            print(f"[camera] mp4 transcode failed for {mjpeg_path.name}; kept mjpeg")
            try:   # don't retry it on every start; "to mp4" can still force it
                mjpeg_path.with_suffix(".json").write_text(json.dumps(dict(meta, mp4_failed=True)))
            except OSError:
                pass

    def _mux_h264(self, h264_path, meta):
        """.h264 (+ .wav) -> .mp4 without re-encoding the video: seconds, not
        minutes. Portrait clips get a rotation tag instead of a transpose, which
        players, browsers and ffmpeg's own thumbnails honour."""
        fps = meta.get("fps") or self.config["rec_fps"] or 30
        mp4_path = h264_path.with_suffix(".mp4")
        tmp = h264_path.with_suffix(".mp4.tmp")
        wav_path = h264_path.with_suffix(".wav")
        has_wav = wav_path.is_file() and wav_path.stat().st_size > 44
        rotate = meta.get("rotate", 0)
        rot_in, rot_out = [], []
        if rotate == 90:
            if ffmpeg_has_display_rotation():
                rot_in = ["-display_rotation", "-90"]      # counter-clockwise degrees
            else:
                rot_out = ["-metadata:s:v:0", "rotate=90"]  # clockwise degrees
        video_in = rot_in + ["-fflags", "+genpts", "-framerate", f"{fps:.3f}",
                             "-f", "h264", "-i", str(h264_path)]
        audio_in = ["-i", str(wav_path), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "96k"]
        tail = ["-c:v", "copy"] + rot_out + ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
        ok = muxed_audio = False
        for with_audio in ([True] if has_wav else []) + [False]:
            cmd = (["ffmpeg", "-y", "-loglevel", "error"] + video_in
                   + (audio_in if with_audio else []) + tail)
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError:
                break
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                ok, muxed_audio = True, with_audio
                break
            print(f"[camera] mux {h264_path.name} failed: {r.stderr.decode(errors='replace')[-300:]}")
            if tmp.exists():
                tmp.unlink()
        if ok:
            tmp.replace(mp4_path)
            mp4_path.with_suffix(".json").write_text(
                json.dumps(dict(meta, container="mp4", audio=muxed_audio)))
            for old in [h264_path] + ([wav_path] if muxed_audio else []):
                try:
                    old.unlink()
                except OSError:
                    pass
            print(f"[camera] muxed {h264_path.name} -> {mp4_path.name}")
        else:
            try:
                h264_path.with_suffix(".json").write_text(json.dumps(dict(meta, mp4_failed=True)))
            except OSError:
                pass

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

    @staticmethod
    def mp4_thumbnail(path):
        """Extract the first frame of an mp4 as JPEG bytes via ffmpeg."""
        if shutil.which("ffmpeg") is None:
            return None
        try:
            r = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", str(path),
                 "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        return r.stdout or None

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
            return
        want = self.config["camera_backend"]
        if want != "rpicam" and picam.available():
            if self._run_picam():
                return                      # stopped normally
            print("[camera] Picamera2 could not start; falling back to rpicam-vid")
        elif want == "picamera2":
            print("[camera] Picamera2 not installed (sudo apt install python3-picamera2); "
                  "using rpicam-vid")
        self.backend = "rpicam"
        self._run_real()

    def _target_fps(self):
        return self.config["night_fps"] if self.is_dark() else self.config["rec_fps"]

    def _run_picam(self):
        """Run the Picamera2 pipeline until stop. Restarts it on settings
        changes and if frames stop arriving; switches the frame rate between
        day and night between recordings. Returns False if it never started
        (the caller falls back to rpicam-vid)."""
        self.backend = "picamera2"
        started_once = False
        while not self._stop.is_set():
            self._restart.clear()
            pipe = picam.PicamPipeline(self.config, self._flips, self._publish)
            try:
                pipe.start(self._target_fps())
            except Exception as e:
                print(f"[camera] Picamera2 start failed: {e!r}")
                if not started_once:
                    self.backend = None
                    return False
                self._stop.wait(10)
                continue
            started_once = True
            self._pipe = pipe
            since = time.monotonic()
            while not self._stop.is_set() and not self._restart.is_set():
                self._restart.wait(2.0)
                want = self._target_fps()
                if want != pipe.fps and not self.recording:
                    try:
                        pipe.set_fps(want)
                    except Exception as e:
                        print(f"[camera] frame rate change failed: {e!r}")
                if time.monotonic() - since > 15 and not pipe.healthy():
                    print("[camera] no frames from Picamera2 for 10 s, restarting it")
                    break
            if self.recording:
                try:
                    self.stop_recording()
                except RuntimeError:
                    pass
            self._pipe = None
            pipe.stop()
            if not self._stop.is_set():
                self._stop.wait(1.0)
        return True

    def _rpicam_cmd(self):
        cmd = [
            "rpicam-vid",
            "-t", "0",
            "--codec", "mjpeg",
            "--width", str(self.config["stream_width"]),
            "--height", str(self.config["stream_height"]),
            "--framerate", str(self.config["stream_fps"]),
            "--quality", str(self.config["stream_quality"]),
            "--nopreview",
            "--flush",
        ]
        # Field of view. Without --mode, libcamera picks the IMX708's cropped
        # 1536x864 mode for a 720p stream; name the mode so "wide" really is
        # the whole sensor. Both modes are 2x2 binned, so quality is similar.
        if self.config["fov"] == "zoom":
            cmd += ["--mode", "1536:864:10:P"]
        else:
            cmd += ["--mode", "2304:1296:10:P"]
        # Orientation: both flags together == 180° rotation (upside-down mount).
        hflip, vflip = self._flips()
        if hflip:
            cmd.append("--hflip")
        if vflip:
            cmd.append("--vflip")
        # Focus (Camera Module 3 has a motorised lens; ignored by fixed-focus modules).
        if self.config["af_mode"] == "manual":
            cmd += ["--autofocus-mode", "manual",
                    "--lens-position", str(self.config["lens_position"])]
        else:
            cmd += ["--autofocus-mode", "continuous",
                    "--autofocus-range", str(self.config["af_range"])]
        cmd += ["-o", "-"]
        return cmd

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
                if self.config["fov"] == "zoom":   # same 2/3 centre crop as the real mode
                    img = img.crop((w / 6, h / 6, w * 5 / 6, h * 5 / 6)).resize((w, h))
                hflip, vflip = self._flips()
                if hflip:
                    img = img.transpose(Image.FLIP_LEFT_RIGHT)
                if vflip:
                    img = img.transpose(Image.FLIP_TOP_BOTTOM)
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
