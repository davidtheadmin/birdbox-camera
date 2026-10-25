"""Background sensor/LED loop, absorbed from birdcam_lights.py.

Runs as a single daemon thread owned by the web app. In auto mode the
VEML7700 reading (lux) is mapped to LED brightness on a log scale, with
exponential smoothing;
in manual mode a user-set brightness is applied and the sensor is still
read so the UI keeps showing live light levels.
"""
import math
import threading
import time
from collections import deque
from pathlib import Path

# Raw readings: ~6 h at a 2 s interval.
HISTORY_MAXLEN = 10800
# Per-minute averages: 14 days, also appended to a CSV so they survive reboots.
MINUTES_MAXLEN = 14 * 24 * 60
HISTORY_FILE = Path(__file__).parent / "history.csv"


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
        self._minutes = deque(maxlen=MINUTES_MAXLEN)  # per-minute averages, same shape
        self._bucket = None                           # (minute_start, [levels], [brightness])
        self._load_minutes()
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

    def get_history(self, start=None, end=None, max_points=400):
        """Return [(ts, level, brightness)] between start and end (unix time).

        Uses raw readings where they exist and per-minute averages for older
        times, then averages into at most `max_points` evenly spaced buckets.
        """
        end = end if end is not None else time.time()
        start = start if start is not None else end - 3600
        with self._lock:
            raw = list(self._history)
            minutes = list(self._minutes)
        raw_from = raw[0][0] if raw else float("inf")
        pts = [p for p in minutes if start <= p[0] < min(end, raw_from)]
        pts += [p for p in raw if start <= p[0] <= end]
        if len(pts) <= max_points:
            return pts
        width = (end - start) / max_points
        buckets = {}
        for t, lv, b in pts:
            buckets.setdefault(int((t - start) // width), []).append((t, lv, b))
        out = []
        for _, group in sorted(buckets.items()):
            levels = [g[1] for g in group if g[1] is not None]
            out.append((
                round(sum(g[0] for g in group) / len(group), 1),
                round(sum(levels) / len(levels), 2) if levels else None,
                round(sum(g[2] for g in group) / len(group), 1),
            ))
        return out

    # --- per-minute history, persisted ---

    def _load_minutes(self):
        cutoff = time.time() - MINUTES_MAXLEN * 60
        rows = []
        try:
            with open(HISTORY_FILE) as f:
                for line in f:
                    try:
                        t, lv, b = line.strip().split(",")
                        t = float(t)
                        if t >= cutoff:
                            rows.append((t, float(lv) if lv else None, float(b)))
                    except ValueError:
                        continue
        except OSError:
            return
        self._minutes.extend(rows[-MINUTES_MAXLEN:])
        # rewrite the file so it never grows past 14 days
        try:
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                for t, lv, b in self._minutes:
                    f.write(f"{t:.0f},{'' if lv is None else lv},{b}\n")
            tmp.replace(HISTORY_FILE)
        except OSError as e:
            print(f"[history] could not rewrite {HISTORY_FILE}: {e}")

    def _add_to_minute(self, now, level, brightness):
        minute = int(now // 60) * 60
        if self._bucket and self._bucket[0] != minute:
            m, levels, brights = self._bucket
            lv = round(sum(levels) / len(levels), 2) if levels else None
            b = round(sum(brights) / len(brights), 1)
            self._minutes.append((m + 30, lv, b))
            try:
                with open(HISTORY_FILE, "a") as f:
                    f.write(f"{m + 30},{'' if lv is None else lv},{b}\n")
            except OSError:
                pass
            self._bucket = None
        if self._bucket is None:
            self._bucket = (minute, [], [])
        if level is not None:
            self._bucket[1].append(level)
        self._bucket[2].append(brightness)

    # --- the loop ---

    def _level_to_brightness(self, lux):
        """Brighter than lux_off -> LEDs off; darker than lux_full -> max_bright.
        In between, interpolate on log10(lux), which matches how dusk falls."""
        off = max(self.config["lux_off"], 0.01)
        full = max(self.config["lux_full"], 0.001)
        cap = self.config["max_bright"]
        if off <= full:  # degenerate config; treat as a threshold at lux_off
            return cap if lux < off else 0
        if lux >= off:
            return 0
        if lux <= full:
            return cap
        return cap * (math.log10(off) - math.log10(lux)) / (math.log10(off) - math.log10(full))

    def _run(self):
        while not self._stop.is_set():
            level = self.hw.read_light_level()  # blocking, ~0.1-0.4 s
            now = time.time()
            with self._lock:
                self._level = level
                if self._mode == "auto":
                    target = self._level_to_brightness(level)
                    self._current += (target - self._current) * self.config["smooth"]
                    self.hw.set_led_brightness(self._current)
                self._history.append((now, level, round(self._current, 1)))
                self._add_to_minute(now, level, round(self._current, 1))
            self._stop.wait(self.config["sensor_interval"])
