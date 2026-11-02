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
    "lux_off": 20.0,       # this bright or brighter (lux) -> LEDs off
    "lux_full": 1.0,       # this dark or darker (lux) -> LEDs at max_bright
    "max_bright": 50,      # LED current cap, % of 94 mA (50 % = ~47 mA)
    "smooth": 0.3,         # exponential smoothing factor, 0-1
    "sensor_interval": 2.0,  # seconds between sensor reads

    # --- camera / stream ---
    "stream_width": 1280,
    "stream_height": 720,
    "stream_fps": 15,
    "stream_quality": 80,  # MJPEG quality 1-100
    "flip_h": 0,           # mirror horizontally (--hflip)
    "flip_v": 0,           # mirror vertically (--vflip); both = 180° for an upside-down mount
    "rotation": 0,         # 0/90/180/270, clockwise. 180 is done by the camera,
                           # 90/270 in software (browser, snapshots, mp4)
    "fov": "wide",         # wide = whole sensor (2304x1296 mode, full 120°);
                           # zoom = centre crop (1536x864 mode, ~1.5x narrower)

    # --- focus (Camera Module 3 has a motorised lens) ---
    "af_mode": "continuous",  # continuous | manual
    "af_range": "macro",      # normal | macro | full  (macro helps a close nest focus)
    "lens_position": 4.0,     # manual focus in dioptres (1/metres); 0 = infinity, 4 ≈ 0.25 m

    # --- recordings ---
    "auto_mp4": 1,            # convert every finished recording to mp4 (H.264 + AAC)
    # picamera2 backend: recordings come from a second, bigger stream with
    # the hardware H.264 encoder, independent of the live view above
    "camera_backend": "auto",  # auto (picamera2 if installed) | picamera2 | rpicam
    "rec_size": "1920x1080",
    "rec_fps": 30,
    "rec_bitrate": 8.0,       # Mbit/s
    "preroll_seconds": 3.0,   # recordings start this long before the trigger

    # --- motion detection ---
    "motion_enabled": 0,      # arm auto-record on motion
    "motion_sensitivity": 50,  # 1 (least) .. 100 (most) sensitive
    "motion_interval": 1.0,   # seconds between motion samples
    "motion_cooldown": 6.0,   # keep recording this long after the last motion

    # --- audio (I2S mic on the pHAT; see audio.py) ---
    "audio_enabled": 1,        # run the mic at all
    "audio_record": 1,         # write a .wav alongside every video recording
    "audio_gain_db": 20.0,     # digital gain; MEMS mics are quiet at bird distances
    "audio_filter": 1,         # band-pass: drop rumble and high-frequency crackle
    "audio_low_hz": 200.0,     # below this is cut (hum, wind, handling)
    "audio_high_hz": 12000.0,  # above this is cut (board/WiFi pickup)
    "audio_denoise": 1,        # learn the steady background (tones, hiss) and turn it down
    "audio_denoise_db": 20.0,  # max reduction of that background
    "audio_device": "auto",    # ALSA device, or "auto" to find the I2S card
    "sound_trigger": 0,        # start a recording when it gets loud
    "sound_threshold_db": -40.0,  # RMS level (dBFS, after gain) that counts as loud

    # --- cleanup (Pi-side card space management; see cleanup.py) ---
    "cleanup_enabled": 1,        # master switch
    "min_free_gb": 8.0,          # target free space on the captures filesystem
    "emergency_free_gb": 2.0,    # below this, delete unarchived recordings too
    "cleanup_interval": 300,     # seconds between checks

    # --- server ---
    "port": 8080,
    "captures_dir": "captures",
}

# Keys the settings API may change. Numeric keys use (type, min, max);
# choice keys use (type, [allowed values]).
EDITABLE = {
    "lux_off": (float, 0.01, 100000),
    "lux_full": (float, 0.001, 100000),
    "max_bright": (int, 0, 100),
    "smooth": (float, 0.01, 1.0),
    "sensor_interval": (float, 0.5, 60.0),
    "stream_width": (int, 160, 4608),
    "stream_height": (int, 120, 2592),
    "stream_fps": (int, 1, 30),
    "stream_quality": (int, 10, 100),
    "flip_h": (int, 0, 1),
    "flip_v": (int, 0, 1),
    "fov": (str, ["wide", "zoom"]),
    "rotation": (int, [0, 90, 180, 270]),
    "af_mode": (str, ["continuous", "manual"]),
    "af_range": (str, ["normal", "macro", "full"]),
    "lens_position": (float, 0.0, 10.0),
    "auto_mp4": (int, 0, 1),
    "rec_size": (str, ["1920x1080", "1280x720"]),
    "rec_fps": (int, 5, 30),
    "rec_bitrate": (float, 1.0, 20.0),
    "preroll_seconds": (float, 0.0, 10.0),
    "motion_enabled": (int, 0, 1),
    "motion_sensitivity": (int, 1, 100),
    "motion_interval": (float, 0.2, 10.0),
    "motion_cooldown": (float, 1.0, 120.0),
    "audio_enabled": (int, 0, 1),
    "audio_record": (int, 0, 1),
    "audio_gain_db": (float, 0.0, 40.0),
    "audio_filter": (int, 0, 1),
    "audio_low_hz": (float, 20.0, 2000.0),
    "audio_high_hz": (float, 2000.0, 23000.0),
    "audio_denoise": (int, 0, 1),
    "audio_denoise_db": (float, 3.0, 40.0),
    "sound_trigger": (int, 0, 1),
    "sound_threshold_db": (float, -90.0, 0.0),
    "cleanup_enabled": (int, 0, 1),
    "min_free_gb": (float, 0.5, 500.0),
    "emergency_free_gb": (float, 0.1, 500.0),
    "cleanup_interval": (int, 10, 3600),
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
            spec = EDITABLE.get(key)
            if spec is None:
                errors[key] = "not an editable setting"
                continue
            typ = spec[0]
            try:
                val = typ(raw)
            except (TypeError, ValueError):
                errors[key] = f"expected {typ.__name__}"
                continue
            if isinstance(spec[1], (list, tuple)):        # choice list
                if val not in spec[1]:
                    errors[key] = "must be one of " + ", ".join(map(str, spec[1]))
                    continue
            else:                                          # numeric range
                lo, hi = spec[1], spec[2]
                if not (lo <= val <= hi):
                    errors[key] = f"must be between {lo} and {hi}"
                    continue
            applied[key] = val
        if "min_free_gb" in applied or "emergency_free_gb" in applied:
            with _lock:
                trial_min = applied.get("min_free_gb", self._values["min_free_gb"])
                trial_emergency = applied.get("emergency_free_gb", self._values["emergency_free_gb"])
            if trial_emergency >= trial_min:
                msg = "emergency_free_gb must be less than min_free_gb"
                for key in ("min_free_gb", "emergency_free_gb"):
                    if key in applied:
                        errors[key] = msg
                        del applied[key]
        if applied:
            with _lock:
                self._values.update(applied)
                self._save_locked()
        return applied, errors

    def _save_locked(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._values, indent=2))
        tmp.replace(self.path)
