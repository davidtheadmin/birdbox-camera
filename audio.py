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
import collections
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


class BandPass:
    """Streaming FIR band-pass (windowed sinc, overlap-save via FFT), numpy only.

    Removes rumble/hum below `low` Hz and the high-frequency crackle the I2S
    mics pick up from the board above `high` Hz. Birdsong sits mostly between
    1 and 10 kHz. 2049 Blackman taps give a ~130 Hz transition, -74 dB stop
    band and 21 ms latency; filtering 100 ms of audio costs ~1 ms on a 3A+.
    """
    TAPS = 2049

    def __init__(self, rate, low, high):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        def lowpass(fc):
            return 2 * fc / rate * np.sinc(2 * fc / rate * n)
        h = (lowpass(high) - lowpass(low)) * np.blackman(self.TAPS)
        self.key = (low, high)
        self._h = h
        self._fft_n = None
        self._tail = np.zeros(self.TAPS - 1, dtype=np.float64)

    def __call__(self, x):
        block = np.concatenate([self._tail, x.astype(np.float64)])
        self._tail = block[-(self.TAPS - 1):]
        n = 1 << (len(block) - 1).bit_length()
        if n != self._fft_n:
            self._fft_n, self._H = n, np.fft.rfft(self._h, n)
        y = np.fft.irfft(np.fft.rfft(block, n) * self._H, n)
        return y[self.TAPS - 1:len(block)].astype(np.float32)


class SpectralGate:
    """Streaming stationary-noise reduction (spectral gating), numpy only.

    Learns the steady background per frequency (tones and hiss that are
    always there, e.g. board/WiFi pickup that sounds like crickets) and turns
    it down; anything clearly above it, like a bird call, passes. STFT with
    1024-sample sqrt-Hann frames, 50 % overlap, so ~21 ms extra latency.

    The noise estimate falls quickly when a bin gets quieter and rises only
    slowly (~1 dB/s), so a song lasting a few seconds isn't learned as noise.
    """
    N = 1024
    HOP = N // 2
    WARMUP = 48                # frames (~0.5 s)
    NOISE_FLOOR = 1.0          # per-bin power floor (~1 LSB), so digital
                               # silence can't pin the estimate at zero

    def __init__(self, max_reduction_db=20.0, over=8.0):
        self.floor = 10 ** (-max_reduction_db / 20.0)
        self.over = over
        self.key = max_reduction_db
        self._win = np.sqrt(np.hanning(self.N + 1)[:-1])   # periodic, OLA-exact
        self._in = np.zeros(self.N - self.HOP)
        self._ola = np.zeros(self.N - self.HOP)
        self._noise = None
        self._gain = None
        self._frames = 0
        rise_db_per_frame = 1.0 * self.HOP / RATE          # ~1 dB/s
        self._rise = 10 ** (rise_db_per_frame / 10)

    def __call__(self, x):
        buf = np.concatenate([self._in, x.astype(np.float64)])
        out = []
        pos = 0
        while pos + self.N <= len(buf):
            spec = np.fft.rfft(buf[pos:pos + self.N] * self._win)
            power = spec.real ** 2 + spec.imag ** 2 + 1e-12
            if self._noise is None:
                self._smooth = power.copy()
                self._noise = power.copy()
                self._gain = np.ones_like(power)
            # Track the minimum of the *smoothed* power: raw bin power
            # fluctuates ~10 dB frame to frame, and its minimum would put the
            # estimate far below the real noise level.
            self._smooth = 0.85 * self._smooth + 0.15 * power
            self._frames += 1
            if self._frames < self.WARMUP:
                # first ~0.5 s (also after a restart or silence): follow the
                # level directly instead of creeping up at 1 dB/s
                self._noise = self._smooth.copy()
            else:
                down = self._smooth < self._noise
                self._noise[down] = self._smooth[down]
                self._noise[~down] *= self._rise
            np.maximum(self._noise, self.NOISE_FLOOR, out=self._noise)
            g = np.maximum(self.floor, 1.0 - self.over * self._noise / power)
            g = np.convolve(g, (0.25, 0.5, 0.25), mode="same")  # less "musical" noise
            self._gain = np.maximum(g, 0.5 * self._gain)          # fast attack, short release
            frame = np.fft.irfft(spec * self._gain, self.N) * self._win
            frame[: len(self._ola)] += self._ola
            out.append(frame[: self.HOP])
            self._ola = frame[self.HOP:]
            pos += self.HOP
        self._in = buf[pos:]
        return (np.concatenate(out) if out else np.zeros(0)).astype(np.float32)


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
        # Recent processed audio, so a recording can start with the same
        # pre-roll as the video (picamera2 backend). Chunks of ~100 ms.
        self._recent = collections.deque()
        self._recent_bytes = 0

        self._filter = None
        self._gate = None
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

    def start_recording(self, wav_path, preroll=0.0):
        """Start writing a .wav next to a new video recording, beginning
        `preroll` seconds in the past to match the video. Returns True if
        audio is being recorded."""
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
            if preroll > 0 and self._recent:
                want = int(preroll * RATE) * 2
                past = b"".join(self._recent)[-want:]
                if len(past) < want:   # mic started after the video buffer: pad
                    past = b"\x00" * (want - len(past)) + past
                w.writeframesraw(past)
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

    def _bandpass(self, x):
        if not self.config["audio_filter"]:
            self._filter = None
            return x
        key = (self.config["audio_low_hz"], self.config["audio_high_hz"])
        if self._filter is None or self._filter.key != key:
            self._filter = BandPass(RATE, *key)
        return self._filter(x)

    def _denoise(self, x):
        if not self.config["audio_denoise"]:
            self._gate = None
            return x
        db = self.config["audio_denoise_db"]
        if self._gate is None or self._gate.key != db:
            self._gate = SpectralGate(db)
        return self._gate(x)

    def _process(self, raw):
        gain_db = self.config["audio_gain_db"]
        if np is not None:
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            x = self._bandpass(x)
            x = self._denoise(x)
            if len(x) == 0:          # the gate is still filling its first frame
                x = np.zeros(2, dtype=np.float32)
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
            self._recent.append(data)
            self._recent_bytes += len(data)
            keep = int((self.config["preroll_seconds"] + 1.5) * RATE) * 2
            while self._recent and self._recent_bytes - len(self._recent[0]) >= keep:
                self._recent_bytes -= len(self._recent.popleft())
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
