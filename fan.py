"""Box fan on the pHAT's 12 V AUX output (GPIO13), switched by CPU temperature.

A daemon thread like cleanup.py and motion.py. In auto mode it uses two
thresholds so the fan doesn't flick on and off around one temperature: on
at fan_on_c, off again at or below fan_off_c. While a recording runs the
fan is kept off so the I2S mic on the same board doesn't record its hum
(fan_quiet_recording), unless the CPU reaches fan_max_c: overheating is
worse than a noisy clip. Modes "on" and "off" override everything.

If the CPU temperature can't be read (the dev laptop), auto keeps the fan
off.
"""
import threading
import time

import status as system_status

INTERVAL = 5.0   # seconds between checks


class FanController:
    def __init__(self, hardware, config, camera, read_temp=system_status.cpu_temp):
        self.hw = hardware
        self.config = config
        self.camera = camera
        self._read_temp = read_temp
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._on = False
        self._reason = "starting"
        self._temp = None
        self._changed = None          # unix ts of the last switch
        self._thread = threading.Thread(target=self._run, name="fan", daemon=True)

    # --- lifecycle ---

    def start(self):
        self._apply(False, "starting")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self._apply(False, "stopped")

    # --- logic ---

    def decide(self, temp, recording, currently_on):
        """Pure decision: (on, reason). Kept separate so it can be tested."""
        cfg = self.config
        mode = cfg["fan_mode"]
        if mode == "on":
            return True, "manual on"
        if mode == "off":
            return False, "manual off"
        if temp is None:
            return False, "no temperature reading"
        if temp >= cfg["fan_max_c"]:
            return True, f"over {cfg['fan_max_c']:g} °C"
        if recording and cfg["fan_quiet_recording"]:
            return False, "quiet while recording"
        if temp >= cfg["fan_on_c"]:
            return True, f"over {cfg['fan_on_c']:g} °C"
        if temp <= cfg["fan_off_c"]:
            return False, f"under {cfg['fan_off_c']:g} °C"
        # between the thresholds: keep doing what it was doing
        return currently_on, "cooling down" if currently_on else "below switch-on"

    def _tick(self):
        temp = self._read_temp()
        recording = bool(self.camera.recording_state().get("recording")) if self.camera else False
        on, reason = self.decide(temp, recording, self._on)
        with self._lock:
            self._temp = temp
        self._apply(on, reason)

    def _apply(self, on, reason):
        with self._lock:
            changed = on != self._on or self._changed is None
            self._on, self._reason = on, reason
            if changed:
                self._changed = time.time()
        if changed:
            try:
                self.hw.set_aux(on)
            except OSError as e:
                print(f"[fan] could not switch AUX: {e}")
            print(f"[fan] {'on' if on else 'off'} ({reason})")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:      # never let the fan thread die
                print(f"[fan] error: {e}")
            self._stop.wait(INTERVAL)

    def state(self):
        with self._lock:
            return {
                "available": bool(getattr(self.hw, "aux_available", False)),
                "on": self._on,
                "mode": self.config["fan_mode"],
                "reason": self._reason,
                "temp": self._temp,
                "since": self._changed,
            }
