"""Picamera2 camera pipeline for the pHAT units: one sensor, two streams.

The ISP delivers every frame in two sizes, so recording at a higher
resolution never needs a camera restart:

- main  (default 1920x1080) -> hardware H.264 encoder, running all the time
  into RecordBuffer, a short ring buffer. Starting a recording writes the
  buffer from the last keyframe that is at least `preroll_seconds` old, then
  keeps appending live frames, so motion clips include the seconds *before*
  the trigger.
- lores (the live-view size, default 1280x720) -> hardware MJPEG encoder ->
  LiveOutput, throttled to stream_fps, for the browser, motion detection and
  snapshots.

The sensor runs at rec_fps (30) by day and night_fps (15) when the IR LEDs
are on, because at 30 fps each frame can only be exposed for 1/30 s.

Picamera2 is imported lazily, so this module also imports (and RecordBuffer
can be tested) on a machine without it.
"""
import collections
import threading
import time

try:
    from picamera2.outputs import Output as _OutputBase
except ImportError:                                   # dev laptop / tests
    class _OutputBase:
        def __init__(self, pts=None):
            self.recording = False
            self.needs_pacing = False
            self.needs_add_stream = False

        def start(self):
            self.recording = True

        def stop(self):
            self.recording = False

        def _add_stream(self, *args, **kwargs):
            pass


def available():
    try:
        import picamera2  # noqa: F401
        return True
    except Exception:          # ImportError, or libcamera binding errors
        return False


class RecordBuffer(_OutputBase):
    """Receives every H.264 frame. Keeps the last few seconds (starting at a
    keyframe) in memory and, while a file is open, writes frames to it."""

    def __init__(self, preroll_seconds):
        super().__init__()
        self.preroll_us = int(max(0.0, preroll_seconds) * 1e6)
        self._lock = threading.Lock()
        self._ring = collections.deque()      # (timestamp_us, keyframe, bytes)
        self._file = None
        self._stats = None
        self.last_frame_at = 0.0               # time.monotonic() of the last frame

    # --- encoder side (runs on the encoder's thread) ---

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if audio:
            return
        ts = int(timestamp) if timestamp is not None else int(time.monotonic() * 1e6)
        data = bytes(frame)
        with self._lock:
            self.last_frame_at = time.monotonic()
            if self._file is not None:
                self._write(ts, keyframe, data)
            self._ring.append((ts, keyframe, data))
            self._trim(ts)

    def _trim(self, now_ts):
        """Drop everything before the newest keyframe that is still at least
        preroll old, so the buffer always starts on a keyframe and covers at
        least the pre-roll (up to one keyframe interval more)."""
        cutoff = now_ts - self.preroll_us
        keep_from = None
        for i, (t, k, _) in enumerate(self._ring):
            if t > cutoff:
                break
            if k:
                keep_from = i
        for _ in range(keep_from or 0):
            self._ring.popleft()

    def _write(self, ts, keyframe, data):
        s = self._stats
        if s["frames"] == 0 and not keyframe:
            return                             # a decodable file starts on a keyframe
        try:
            self._file.write(data)
        except OSError as e:
            print(f"[picam] write failed, stopping recording: {e}")
            self._file.close()
            self._file = None
            return
        s["frames"] += 1
        if s["first_ts"] is None:
            s["first_ts"] = ts
        s["last_ts"] = ts

    def clear(self):
        """Forget the buffer (after a frame-rate change: mixed rates in one
        clip would play at the wrong speed)."""
        with self._lock:
            self._ring.clear()

    # --- recording control (runs on the app's threads) ---

    def start_file(self, path):
        """Open `path` and write the buffered pre-roll into it. Returns the
        pre-roll length in seconds actually written."""
        f = open(path, "wb")
        with self._lock:
            self._file = f
            self._stats = {"frames": 0, "first_ts": None, "last_ts": None}
            now_ts = self._ring[-1][0] if self._ring else None
            for t, k, d in self._ring:
                self._write(t, k, d)
            first = self._stats["first_ts"]
            preroll = (now_ts - first) / 1e6 if first is not None else 0.0
            self._stats["preroll"] = round(preroll, 3)
            return preroll

    def stop_file(self):
        """Close the file. Returns frames, measured fps, duration, pre-roll."""
        with self._lock:
            f, s = self._file, self._stats
            self._file = None
        if f is not None:
            f.close()
        if not s or s["frames"] == 0:
            return {"frames": 0, "fps": 0, "duration": 0, "preroll": 0}
        span = (s["last_ts"] - s["first_ts"]) / 1e6
        fps = (s["frames"] - 1) / span if span > 0 else 0
        duration = s["frames"] / fps if fps else 0
        return {"frames": s["frames"], "fps": round(fps, 3),
                "duration": round(duration, 2), "preroll": s.get("preroll", 0)}


class LiveOutput(_OutputBase):
    """Receives hardware-encoded JPEGs of the lores stream and passes them
    on at no more than max_fps()."""

    def __init__(self, publish, max_fps):
        super().__init__()
        self._publish = publish
        self._max_fps = max_fps
        self._last = 0.0
        self.last_frame_at = 0.0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if audio:
            return
        now = time.monotonic()
        self.last_frame_at = now
        if now - self._last < 0.9 / max(1, self._max_fps()):
            return
        self._last = now
        self._publish(bytes(frame))


class PicamPipeline:
    """Owns one running Picamera2 instance with its two encoders."""

    SENSOR_MODES = {"wide": (2304, 1296), "zoom": (1536, 864)}  # IMX708, 2x2 binned

    def __init__(self, config, flips, publish):
        self.config = config
        self.flips = flips                    # () -> (hflip, vflip)
        self.rec = RecordBuffer(config["preroll_seconds"])
        self.live = LiveOutput(publish, lambda: config["stream_fps"])
        self.fps = None
        self.rec_size = tuple(int(v) for v in config["rec_size"].split("x"))
        self._cam = None

    def _focus_controls(self, controls):
        cfg = self.config
        if cfg["af_mode"] == "manual":
            return {"AfMode": controls.AfModeEnum.Manual,
                    "LensPosition": float(cfg["lens_position"])}
        ranges = {"normal": controls.AfRangeEnum.Normal,
                  "macro": controls.AfRangeEnum.Macro,
                  "full": controls.AfRangeEnum.Full}
        return {"AfMode": controls.AfModeEnum.Continuous,
                "AfRange": ranges.get(cfg["af_range"], controls.AfRangeEnum.Normal)}

    @staticmethod
    def _frame_duration(fps):
        d = int(1e6 / fps)
        return (d, d)

    def start(self, fps):
        from libcamera import Transform, controls
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder, MJPEGEncoder, Quality

        cfg = self.config
        hflip, vflip = self.flips()
        ctrls = {"FrameDurationLimits": self._frame_duration(fps)}
        try:
            ctrls.update(self._focus_controls(controls))
        except AttributeError:
            pass                               # module without autofocus

        cam = Picamera2()
        try:
            video = cam.create_video_configuration(
                main={"size": self.rec_size, "format": "YUV420"},
                lores={"size": (cfg["stream_width"], cfg["stream_height"]), "format": "YUV420"},
                sensor={"output_size": self.SENSOR_MODES.get(cfg["fov"], self.SENSOR_MODES["wide"]),
                        "bit_depth": 10},
                transform=Transform(hflip=int(hflip), vflip=int(vflip)),
                controls=ctrls,
                buffer_count=4,               # 512 MB board: keep CMA use modest
            )
            cam.align_configuration(video)
            cam.configure(video)

            q = cfg["stream_quality"]
            quality = (Quality.LOW if q < 50 else Quality.MEDIUM if q < 70
                       else Quality.HIGH if q < 85 else Quality.VERY_HIGH)
            h264 = H264Encoder(bitrate=int(cfg["rec_bitrate"] * 1e6), repeat=True,
                               iperiod=int(cfg["rec_fps"]))   # a keyframe every ~1 s
            mjpeg = MJPEGEncoder()
            cam.start_encoder(encoder=h264, output=self.rec, name="main")
            cam.start_encoder(encoder=mjpeg, output=self.live, name="lores", quality=quality)
            cam.start()
        except Exception:
            try:
                cam.stop_encoder()
            except Exception:
                pass
            cam.close()
            raise
        self._cam = cam
        self.fps = fps
        main = cam.camera_configuration()["main"]["size"]
        lores = cam.camera_configuration()["lores"]["size"]
        print(f"[picam] running: record {main[0]}x{main[1]} H.264 @ {fps} fps, "
              f"live {lores[0]}x{lores[1]} MJPEG @ {cfg['stream_fps']} fps, "
              f"pre-roll {cfg['preroll_seconds']} s")

    def set_fps(self, fps):
        self._cam.set_controls({"FrameDurationLimits": self._frame_duration(fps)})
        self.rec.clear()
        self.fps = fps
        print(f"[picam] frame rate -> {fps} fps")

    def healthy(self, timeout=10.0):
        now = time.monotonic()
        return (now - self.rec.last_frame_at < timeout
                and now - self.live.last_frame_at < timeout)

    def stop(self):
        cam, self._cam = self._cam, None
        if cam is None:
            return
        for step in (cam.stop_encoder, cam.stop, cam.close):
            try:
                step()
            except Exception as e:
                print(f"[picam] {step.__name__} failed: {e}")
