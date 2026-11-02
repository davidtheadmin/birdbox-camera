"""Tests for the pre-roll buffer (picam.RecordBuffer) and the matching audio
pre-roll in AudioManager. Plain unittest, runs without Picamera2:

    python3 -m unittest test_picam

Frames are fake byte strings; what matters is which ones end up in the file
and in what order. The full path with real H.264 (buffer -> file -> mp4 mux)
was checked by hand with an x264 stream; see CLAUDE.md.
"""
import tempfile
import unittest
import wave
from pathlib import Path

from picam import RecordBuffer

US = 1_000_000


def feed(rb, start, count, fps=30, gop=30):
    """Frames start..start+count-1; a keyframe every `gop` frames."""
    for n in range(start, start + count):
        rb.outputframe(b"F%05d;" % n, n % gop == 0, int(n * US / fps))


def frames_in(path):
    return [int(x[1:]) for x in Path(path).read_bytes().decode().split(";") if x]


class RecordBufferTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "rec.h264"

    def test_preroll_starts_on_keyframe_at_least_preroll_back(self):
        rb = RecordBuffer(3.0)
        feed(rb, 0, 180)                      # 6 s, keyframes at 0,1,2,... s
        pre = rb.start_file(self.path)
        feed(rb, 180, 60)                     # 2 s more
        stats = rb.stop_file()
        got = frames_in(self.path)
        self.assertEqual(got[0] % 30, 0, "file must start on a keyframe")
        self.assertGreaterEqual(pre, 3.0)
        self.assertLess(pre, 4.1, "no more than one keyframe interval extra")
        self.assertEqual(got, list(range(got[0], 240)), "no gaps or duplicates")
        self.assertAlmostEqual(stats["fps"], 30, places=1)
        self.assertEqual(stats["frames"], len(got))

    def test_zero_preroll_still_starts_on_last_keyframe(self):
        rb = RecordBuffer(0.0)
        feed(rb, 0, 75)                       # last keyframe at frame 60
        rb.start_file(self.path)
        feed(rb, 75, 10)
        rb.stop_file()
        self.assertEqual(frames_in(self.path)[0], 60)

    def test_start_before_first_keyframe_waits_for_one(self):
        rb = RecordBuffer(3.0)
        rb.outputframe(b"F00001;", False, 1)  # stream joined mid-GOP
        rb.start_file(self.path)
        rb.outputframe(b"F00002;", False, 2)
        rb.outputframe(b"F00030;", True, 3)
        rb.outputframe(b"F00031;", False, 4)
        stats = rb.stop_file()
        self.assertEqual(frames_in(self.path), [30, 31])
        self.assertEqual(stats["frames"], 2)

    def test_clear_after_fps_change_drops_old_rate_frames(self):
        rb = RecordBuffer(3.0)
        feed(rb, 0, 90, fps=30)
        rb.clear()
        feed(rb, 0, 45, fps=15, gop=15)       # night: 15 fps
        pre = rb.start_file(self.path)
        feed(rb, 45, 30, fps=15, gop=15)
        stats = rb.stop_file()
        self.assertAlmostEqual(stats["fps"], 15, places=1)
        self.assertGreaterEqual(pre, 2.9)

    def test_ring_memory_is_bounded(self):
        rb = RecordBuffer(3.0)
        feed(rb, 0, 30 * 60)                  # a minute of frames
        self.assertLessEqual(len(rb._ring), 30 * 4 + 1)

    def test_stop_without_frames(self):
        rb = RecordBuffer(3.0)
        rb.start_file(self.path)
        self.assertEqual(rb.stop_file()["frames"], 0)


class AudioPrerollTest(unittest.TestCase):
    def test_wav_starts_preroll_seconds_back(self):
        from audio import AudioManager, CHUNK_FRAMES, RATE
        cfg = {"audio_enabled": 1, "audio_record": 1, "audio_gain_db": 0.0,
               "audio_filter": 0, "audio_denoise": 0, "sound_trigger": 0,
               "preroll_seconds": 3.0, "audio_device": "auto"}
        am = AudioManager(cfg)
        chunk = b"\x01\x00" * CHUNK_FRAMES
        for _ in range(60):                       # 6 s of audio
            am._handle_chunk(chunk)
        path = Path(tempfile.mkdtemp()) / "rec.wav"
        self.assertTrue(am.start_recording(path, preroll=2.5))
        for _ in range(10):                       # 1 s while recording
            am._handle_chunk(chunk)
        am.stop_recording()
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / RATE
        self.assertAlmostEqual(seconds, 3.5, places=2)


if __name__ == "__main__":
    unittest.main()
