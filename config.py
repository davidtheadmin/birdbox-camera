"""Runtime configuration: defaults + persistence to config.json.

Values editable from the settings UI are stored here. The file is created
on first save; missing keys fall back to defaults so old config files
survive upgrades.
"""
import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    # --- LED / sensor loop (see birdcam-ui-briefing.md) ---
    "level_min": 800,      # this bright or brighter -> LEDs off
    "level_max": 4000,     # this dark or darker -> LEDs at max_bright
    "max_bright": 80,      # duty-cycle cap, %
    "smooth": 0.3,         # exponential smoothing factor, 0-1
    "sensor_interval": 2.0,  # seconds between sensor reads (read itself takes ~0.3s+)

    # --- camera / stream ---
    "stream_width": 1280,
    "stream_height": 720,
    "stream_fps": 15,
    "stream_quality": 80,  # MJPEG quality 1-100

    # --- server ---
    "port": 8080,
    "captures_dir": "captures",
}

# Keys the settings API may change, with (type, min, max) validation.
EDITABLE = {
    "level_min": (int, 0, 100000),
    "level_max": (int, 0, 100000),
    "max_bright": (int, 0, 100),
    "smooth": (float, 0.01, 1.0),
    "sensor_interval": (float, 0.5, 60.0),
    "stream_width": (int, 160, 4608),
    "stream_height": (int, 120, 2592),
    "stream_fps": (int, 1, 30),
    "stream_quality": (int, 10, 100),
}

_lock = threading.Lock()


class Config:
    """Thread-safe dict-like config with JSON persistence."""

    def __init__(self, path=CONFIG_PATH):
        self.path = Path(path)
        self._values = dict(DEFAULTS)
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text())
                for k in DEFAULTS:
                    if k in stored:
                        self._values[k] = stored[k]
            except (json.JSONDecodeError, OSError) as e:
                print(f"[config] could not read {self.path}: {e}; using defaults")

    def __getitem__(self, key):
        with _lock:
            return self._values[key]

    def as_dict(self):
        with _lock:
            return dict(self._values)

    def update(self, changes):
        """Validate and apply changes to editable keys. Returns (applied, errors)."""
        applied, errors = {}, {}
        for key, raw in changes.items():
            if key not in EDITABLE:
                errors[key] = "not an editable setting"
                continue
            typ, lo, hi = EDITABLE[key]
            try:
                val = typ(raw)
            except (TypeError, ValueError):
                errors[key] = f"expected {typ.__name__}"
                continue
            if not (lo <= val <= hi):
                errors[key] = f"must be between {lo} and {hi}"
                continue
            applied[key] = val
        if applied:
            with _lock:
                self._values.update(applied)
                self._save_locked()
        return applied, errors

    def _save_locked(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._values, indent=2))
        tmp.replace(self.path)
