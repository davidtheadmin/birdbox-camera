"""Background sensor/LED loop, absorbed from birdcam_lights.py.

Runs as a single daemon thread owned by the web app. In auto mode the LDR
reading is mapped linearly to PWM brightness with exponential smoothing;
in manual mode a user-set brightness is applied and the sensor is still
read so the UI keeps showing live light levels.
"""
import threading
import time
from collections import deque

# ~6h of history at a 2s interval; the API downsamples for the graph.
HISTORY_MAXLEN = 10800


class LedController:
    def __init__(self, hardware, config):
        self.hw = hardware
        self.config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._mode = "auto"            # "auto" | "manual"
        self._manual_brightness = 0.0  # slider value, 0-100
        self._current = 0.0            # smoothed applied brightness
        self._level = None             # last sensor reading
        self._history = deque(maxlen=HISTORY_MAXLEN)  # (ts, level, brightness)
        self._hyst_on = False          # hysteresis mode: current on/off state
        self._last_switch = 0.0        # hysteresis mode: monotonic time of last switch
        self._thread = threading.Thread(target=self._run, name="led-loop", daemon=True)

    # --- lifecycle ---

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.hw.set_led_brightness(0)

    # --- control API (called from Flask request threads) ---

    def get_state(self):
        with self._lock:
            return {
                "mode": self._mode,
                "brightness": round(self._current, 1),
                "manual_brightness": round(self._manual_brightness, 1),
                "level": self._level,
            }

    def set_mode(self, mode):
        if mode not in ("auto", "manual"):
            raise ValueError("mode must be 'auto' or 'manual'")
        with self._lock:
            self._mode = mode
            if mode == "manual":
                self._current = self._manual_brightness
                self.hw.set_led_brightness(self._current)

    def set_manual_brightness(self, percent):
        percent = max(0.0, min(100.0, float(percent)))
        with self._lock:
            self._manual_brightness = percent
            if self._mode == "manual":
                self._current = percent
                self.hw.set_led_brightness(percent)

    def get_history(self, seconds=3600, max_points=360):
        """Return [(ts, level, brightness)] covering the last `seconds`,
        downsampled to at most `max_points` entries."""
        cutoff = time.time() - seconds
        with self._lock:
            pts = [p for p in self._history if p[0] >= cutoff]
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points)]
        return pts

    # --- the loop ---

    def _level_to_brightness_linear(self, level):
        lo = self.config["level_min"]
        hi = self.config["level_max"]
        cap = self.config["max_bright"]
        if hi <= lo:  # degenerate config; treat as a threshold at lo
            return cap if level >= lo else 0
        if level <= lo:
            return 0
        if level >= hi:
            return cap
        return cap * (level - lo) / (hi - lo)

    def _level_to_brightness_hysteresis(self, level):
        """Two-threshold on/off with a minimum dwell between switches, to
        break the LED->LDR feedback loop: level_min is the turn-OFF point,
        level_max the turn-ON point, so small oscillations inside that band
        don't flip the state, and auto_dwell rate-limits flips outside it."""
        lo = self.config["level_min"]
        hi = self.config["level_max"]
        dwell = self.config["auto_dwell"]
        if hi <= lo:  # degenerate config; treat as a plain threshold at lo
            lo_ok, hi_ok = lo, lo
        else:
            lo_ok, hi_ok = lo, hi
        now = time.monotonic()
        if now - self._last_switch >= dwell:
            if not self._hyst_on and level >= hi_ok:
                self._hyst_on = True
                self._last_switch = now
            elif self._hyst_on and level <= lo_ok:
                self._hyst_on = False
                self._last_switch = now
        return self.config["max_bright"] if self._hyst_on else 0

    def _run(self):
        while not self._stop.is_set():
            if self.config["sample_dark"]:
                # Blank the LEDs for the read so the LDR sees ambient light
                # only, not the LEDs' own IR feeding back into the sensor.
                with self._lock:
                    restore = self._current
                self.hw.set_led_brightness(0)
                level = self.hw.read_light_level()  # blocking, ~0.3s+
                self.hw.set_led_brightness(restore)
            else:
                level = self.hw.read_light_level()  # blocking, ~0.3s+
            now = time.time()
            with self._lock:
                self._level = level
                if self._mode == "auto":
                    if self.config["auto_mode"] == "hysteresis":
                        target = self._level_to_brightness_hysteresis(level)
                    else:
                        target = self._level_to_brightness_linear(level)
                    self._current += (target - self._current) * self.config["smooth"]
                    self.hw.set_led_brightness(self._current)
                self._history.append((now, level, round(self._current, 1)))
            self._stop.wait(self.config["sensor_interval"])
