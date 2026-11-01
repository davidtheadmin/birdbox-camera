"""Audio: one arecord process feeds everything, like the camera pipeline.

Hardware: an I2S MEMS mic (INMP441 or ICS-43434) on the pHAT's I2S header
(BCK = GPIO18, LR = GPIO19, MIC = GPIO20). Needs, in /boot/firmware/config.txt:

    dtparam=i2s=on
    dtoverlay=googlevoicehat-soundcard

That overlay runs the I2S port at a fixed 48 kHz and also drives an I2S amp
(MAX98357A) on GPIO21, so the same line covers a speaker added later.

A reader thread pulls 100 ms chunks of 16-bit mono PCM from arecord, applies
a digital gain (these mics are quiet), and hands each chunk to:

- the level meter (RMS and peak in dBFS, for the UI),
- the recording writer: while the camera records, a .wav with the same stem
  as the .mjpeg is written next to it; the mp4 transcode muxes it in,
- live listeners: /audio/live streams an endless WAV to the browser,
- the sound trigger: optionally starts a recording when the level crosses a
  threshold, like motion.py does for frame changes.

Only one process can open the capture device, same rule as the camera.

Mock mode (BIRDCAM_MOCK=1, or no arecord) synthesises background noise
with occasional chirps so the whole UI works on the laptop.
"""
import array
import math
import os
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave

try:
    import numpy as np
except ImportError:          # works without, but gain is skipped and the
    np = None                # meter samples every 8th value

RATE = 48000                 # fixed by the googlevoicehat overlay
CHUNK_SECONDS = 0.1
CHUNK_FRAMES = int(RATE * CHUNK_SECONDS)
CHUNK_BYTES = CHUNK_FRAMES * 2           # S16_LE mono
LIVE_RATE = 24000            # live stream is downsampled 2:1 to halve wifi load
FLOOR_DB = -96.0
CARD_HINTS = ("googlevoi", "voicehat", "i2s", "mems", "mic")


def is_mock():
    return bool(os.environ.get("BIRDCAM_MOCK")) or shutil.which("arecord") is None


def find_capture_device():
    """Return an ALSA device string for the I2S mic, or None.

    Prefers a card whose name looks like the voicehat overlay; otherwise the
    first capture card that isn't USB/HDMI."""
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    cards = []
    for line in out.splitlines():
        m = re.match(r"card (\d+): (\S+) \[([^\]]*)\], device (\d+)", line)
        if m:
            num, cid, desc, dev = m.groups()
            cards.append((cid, desc.lower(), dev))
    for cid, desc, dev in cards:
        if any(h in cid.lower() or h in desc for h in CARD_HINTS):
            return f"plughw:CARD={cid},DEV={dev}"
    for cid, desc, dev in cards:
        if "usb" not in desc and "hdmi" not in desc:
            return f"plughw:CARD={cid},DEV={dev}"
    return None


def wav_stream_header(rate, channels=1, bits=16):
    """WAV header with 'unknown' (max) sizes, for an endless live stream."""
    byte_rate = rate * channels * bits // 8
    block = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                    byte_rate, block, bits)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def _to_db(x):
    """Linear level (full scale = 1.0) -> dBFS, floored."""
    return FLOOR_DB if x <= 0 else max(FLOOR_DB, 20 * math.log10(x))


class AudioManager:
    def __init__(self, config):
        self.config = config
        self.mock = is_mock()
        self.camera = None              # set by attach_camera()

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
        self._proc = None
        self.device = None
        self._error = None

        self._lock = threading.Lock()   # guards recording writer + listeners
        self._wav = None
        self._wav_path = None
        self._listeners = set()

        self._rms_db = FLOOR_DB
        self._peak_db = FLOOR_DB
        self._peak_hold = FLOOR_DB
        self._peak_hold_t = 0.0
        self._last_chunk = 0.0

        self._owned = False             # did the sound trigger start the recording?
        self._last_loud = 0.0
        self._last_trigger = None

    # --- lifecycle ---

    def attach_camera(self, camera):
        self.camera = camera

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kill_proc()
        self.stop_recording()
        self._thread.join(timeout=3)

    def restart(self):
        """Re-open the device (e.g. after audio_enabled toggles back on)."""
        self._kill_proc()

    def _kill_proc(self):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    # --- state for the UI ---

    def state(self):
        now = time.time()
        live = now - self._last_chunk < 1.0
        with self._lock:
            listeners = len(self._listeners)
            recording = self._wav is not None
        return {
            "enabled": bool(self.config["audio_enabled"]),
            "available": live,
            "mock": self.mock,
            "device": self.device,
            "error": None if live else self._error,
            "rms_db": round(self._rms_db, 1) if live else None,
            "peak_db": round(self._peak_hold, 1) if live else None,
            "gain_db": self.config["audio_gain_db"],
            "listeners": listeners,
            "recording": recording,
            "trigger": {
                "enabled": bool(self.config["sound_trigger"]),
                "threshold_db": self.config["sound_threshold_db"],
                "recording": self._owned,
                "last_trigger": self._last_trigger,
            },
        }

    # --- recording (called by CameraManager) ---

    def start_recording(self, wav_path):
        """Start writing a .wav next to a new video recording. Returns True
        if audio is being recorded."""
        if not (self.config["audio_enabled"] and self.config["audio_record"]):
            return False
        if time.time() - self._last_chunk > 1.0:
            return False                 # no live audio: don't leave an empty file
        try:
            w = wave.open(str(wav_path), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
        except OSError as e:
            print(f"[audio] cannot write {wav_path}: {e}")
            return False
        with self._lock:
            if self._wav:
                self._wav.close()
            self._wav, self._wav_path = w, wav_path
        return True

    def stop_recording(self):
        with self._lock:
            w, path = self._wav, self._wav_path
            self._wav = self._wav_path = None
        if w:
            try:
                w.close()             # writes the final sizes into the header
            except OSError as e:
                print(f"[audio] closing {path} failed: {e}")
        return path

    # --- live listening ---

    def live_stream(self):
        """Generator: endless WAV (S16 mono, LIVE_RATE) for an <audio> tag."""
        q = queue.Queue(maxsize=40)      # ~4 s; a slow client drops chunks
        with self._lock:
            self._listeners.add(q)
        try:
            yield wav_stream_header(LIVE_RATE)
            while not self._stop.is_set():
                try:
                    yield q.get(timeout=2.0)
                except queue.Empty:
                    # keep the connection alive with silence while the mic is down
                    yield b"\x00\x00" * int(LIVE_RATE * 0.2)
        finally:
            with self._lock:
                self._listeners.discard(q)

    # --- per-chunk processing ---

    def _process(self, raw):
        gain_db = self.config["audio_gain_db"]
        if np is not None:
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            if gain_db:
                x *= 10 ** (gain_db / 20.0)
                np.clip(x, -32768, 32767, out=x)
            pcm = x.astype("<i2")
            rms = float(np.sqrt(np.mean(x * x))) / 32768.0
            peak = float(np.max(np.abs(x))) / 32768.0
            data = pcm.tobytes()
            # 2:1 for the live stream: average pairs (crude low-pass, fine
            # for birdsong; content above 12 kHz is dropped either way)
            n = len(pcm) // 2 * 2
            live = ((x[:n:2] + x[1:n:2]) * 0.5).astype("<i2").tobytes()
        else:
            a = array.array("h")
            a.frombytes(raw)
            if sys.byteorder != "little":
                a.byteswap()
            s = a[::8]
            rms = math.sqrt(sum(v * v for v in s) / max(len(s), 1)) / 32768.0
            peak = max((abs(v) for v in s), default=0) / 32768.0
            data = raw
            live = a[::2].tobytes() if sys.byteorder == "little" else raw[: len(raw) // 2]
        return data, live, rms, peak

    def _handle_chunk(self, raw):
        data, live, rms, peak = self._process(raw)
        now = time.time()
        self._last_chunk = now
        self._rms_db = _to_db(rms)
        self._peak_db = _to_db(peak)
        if self._peak_db >= self._peak_hold or now - self._peak_hold_t > 1.5:
            self._peak_hold, self._peak_hold_t = self._peak_db, now

        with self._lock:
            if self._wav:
                try:
                    self._wav.writeframesraw(data)
                except OSError as e:
                    print(f"[audio] write failed, stopping audio recording: {e}")
                    self._wav = None
            for q in self._listeners:
                try:
                    q.put_nowait(live)
                except queue.Full:
                    pass

        self._sound_trigger(now)          # outside the lock: may call camera

    def _sound_trigger(self, now):
        cam = self.camera
        if cam is None:
            return
        if not self.config["sound_trigger"]:
            if self._owned:
                self._stop_owned()
            return
        if self._rms_db > self.config["sound_threshold_db"]:
            self._last_loud = now
            if not cam.recording:
                try:
                    name = cam.start_recording()
                    self._owned = True
                    self._last_trigger = now
                    print(f"[audio] sound trigger -> recording {name}")
                except RuntimeError:
                    pass                  # someone else is recording
        elif self._owned and now - self._last_loud > self.config["motion_cooldown"]:
            self._stop_owned()

    def _stop_owned(self):
        self._owned = False
        try:
            self.camera.stop_recording()
            print("[audio] quiet -> stopped recording")
        except RuntimeError:
            pass                          # already stopped by hand

    # --- capture loop ---

    def _run(self):
        if self.mock:
            self._run_mock()
        else:
            self._run_real()

    def _arecord_cmd(self):
        return ["arecord", "-q", "-D", self.device, "-c", "1", "-r", str(RATE),
                "-f", "S16_LE", "-t", "raw", "--buffer-time=500000"]

    def _run_real(self):
        while not self._stop.is_set():
            if not self.config["audio_enabled"]:
                self._error = "disabled"
                self._stop.wait(1.0)
                continue
            dev = self.config["audio_device"]
            self.device = dev if dev and dev != "auto" else find_capture_device()
            if not self.device:
                self._error = ("no capture device — check dtoverlay="
                               "googlevoicehat-soundcard and `arecord -l`")
                self._stop.wait(30)
                continue
            cmd = self._arecord_cmd()
            print(f"[audio] starting: {' '.join(cmd)}")
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, bufsize=0)
            except OSError as e:
                self._error = f"arecord failed: {e}"
                self._stop.wait(10)
                continue
            self._error = None
            self._read(self._proc.stdout)
            rc = self._proc.wait()
            err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
            if not self._stop.is_set():
                self._error = err.splitlines()[-1] if err else f"arecord exited (rc={rc})"
                print(f"[audio] arecord exited (rc={rc}): {err[-200:]}; restarting in 5s")
                self._stop.wait(5)

    def _read(self, pipe):
        buf = bytearray()
        while not self._stop.is_set():
            if not self.config["audio_enabled"]:
                self._kill_proc()
                return
            chunk = pipe.read(CHUNK_BYTES - len(buf))
            if not chunk:
                return
            buf += chunk
            if len(buf) >= CHUNK_BYTES:
                self._handle_chunk(bytes(buf))
                buf.clear()

    # --- mock audio ---

    def _run_mock(self):
        print("[audio] mock mode: synthesising birdsong")
        self.device = "mock"
        phase = 0.0
        chirp_until = 0.0
        next_chirp = time.time() + 3
        t_next = time.monotonic()
        while not self._stop.is_set():
            if not self.config["audio_enabled"]:
                self._error = "disabled"
                self._stop.wait(0.5)
                continue
            self._error = None
            now = time.time()
            if now >= next_chirp:
                chirp_until = now + random.uniform(0.3, 1.2)
                next_chirp = now + random.uniform(2, 9)
            loud = now < chirp_until
            samples = array.array("h")
            for i in range(CHUNK_FRAMES):
                v = random.gauss(0, 4)                       # mic self-noise
                if loud:
                    f = 3500 + 1500 * math.sin(2 * math.pi * 6 * i / CHUNK_FRAMES)
                    phase += 2 * math.pi * f / RATE
                    v += 2500 * math.sin(phase)
                samples.append(int(max(-32768, min(32767, v))))
            if sys.byteorder != "little":
                samples.byteswap()
            self._handle_chunk(samples.tobytes())
            t_next += CHUNK_SECONDS
            self._stop.wait(max(0.0, t_next - time.monotonic()))
