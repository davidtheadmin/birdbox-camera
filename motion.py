"""Motion detection via cheap frame differencing.

Samples the live pipeline a few times a second, decodes each JPEG down to a
tiny greyscale image, and compares it with the previous one. When enough of
the frame changes it starts a recording; after a cooldown with no further
motion it stops that recording again. Deliberately lightweight so it runs on
a Pi Zero 2 W alongside video encoding — frames are shrunk during JPEG decode
(Pillow's draft mode) and only sampled at ``motion_interval`` seconds.

It only stops recordings that *it* started; a recording you began by hand is
left alone.
"""
import io
import threading
import time

try:
    from PIL import Image, ImageChops
except ImportError:                       # Pillow missing -> motion disabled
    Image = None

SMALL = (128, 72)     # resolution frames are compared at
PIXEL_DELTA = 25      # per-pixel greyscale change that counts as "changed"


class MotionDetector:
    def __init__(self, camera, config):
        self.camera = camera
        self.config = config
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="motion", daemon=True)
        self._prev = None
        self._owned = False           # did motion start the current recording?
        self._last_motion = 0.0
        self._activity = 0.0          # latest changed-area fraction, for the UI meter
        self._last_trigger = None

    # --- lifecycle ---

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    # --- state for the UI ---

    def state(self):
        return {
            "enabled": bool(self.config["motion_enabled"]),
            "available": Image is not None,
            "activity": round(self._activity, 4),
            "threshold": round(self._area_threshold(), 4),
            "sensitivity": self.config["motion_sensitivity"],
            "recording": self._owned,
            "last_trigger": self._last_trigger,
        }

    def _area_threshold(self):
        # More sensitive -> a smaller changed area is enough to trigger.
        sens = self.config["motion_sensitivity"]     # 1..100
        return max(0.004, 0.20 * (1 - sens / 100.0))

    # --- frame maths ---

    def _downscale(self, jpeg):
        im = Image.open(io.BytesIO(jpeg))
        im.draft("L", SMALL)                 # let libjpeg downscale during decode
        return im.convert("L").resize(SMALL)

    def _diff_fraction(self, a, b):
        diff = ImageChops.difference(a, b)
        mask = diff.point(lambda p: 255 if p > PIXEL_DELTA else 0)
        changed = mask.histogram()[-1]       # count of pixels at value 255
        return changed / float(SMALL[0] * SMALL[1])

    # --- loop ---

    def _run(self):
        if Image is None:
            print("[motion] Pillow not installed; motion detection disabled")
            return
        seq = 0
        while not self._stop.is_set():
            interval = self.config["motion_interval"]
            if not self.config["motion_enabled"]:
                if self._owned:              # was armed mid-recording, now disarmed
                    self._stop_owned()
                self._prev = None
                self._activity = 0.0
                self._stop.wait(min(interval, 1.0))
                continue

            frame, seq = self.camera.wait_frame(seq, timeout=2.0)
            if frame is None:
                continue
            try:
                small = self._downscale(frame)
            except Exception:                # a corrupt frame shouldn't kill the loop
                continue

            if self._prev is not None:
                frac = self._diff_fraction(self._prev, small)
                self._activity = frac
                now = time.time()
                if frac > self._area_threshold():
                    self._last_motion = now
                    if not self.camera.recording:
                        try:
                            name = self.camera.start_recording()
                            self._owned = True
                            self._last_trigger = now
                            print(f"[motion] triggered -> recording {name}")
                        except RuntimeError:
                            pass             # a manual recording is already running
                elif self._owned and now - self._last_motion > self.config["motion_cooldown"]:
                    self._stop_owned()
            self._prev = small
            self._stop.wait(interval)

    def _stop_owned(self):
        try:
            self.camera.stop_recording()
            print("[motion] cooldown -> stopped recording")
        except RuntimeError:
            pass
        self._owned = False
