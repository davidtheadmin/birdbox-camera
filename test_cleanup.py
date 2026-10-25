"""Tests for cleanup.py's deletion policy.

Plain unittest (stdlib, no pytest dependency) — run with:
    python3 -m unittest test_cleanup

Free disk space is faked (derived from the sizes of files actually left in
the scratch captures dir) rather than relying on the real filesystem, so
thresholds are deterministic regardless of how much space the test machine
actually has.
"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import cleanup as cleanup_mod
from cleanup import CleanupManager

MEDIA_SIZE_EXTS = {".mjpeg", ".mp4", ".jpg"}
BASE = 1800000000  # arbitrary fixed epoch so filenames/marker values are stable


class FakeCamera:
    def __init__(self, recording_file=None, transcoding=None):
        self._recording_file = recording_file
        self._transcoding = set(transcoding or ())

    def recording_state(self):
        if self._recording_file:
            return {"recording": True, "file": self._recording_file}
        return {"recording": False}

    def transcoding_names(self):
        return set(self._transcoding)


def make_file(directory, name, size, mtime):
    """A generic file (used for snapshots, which aren't capture-time-parsed)."""
    path = Path(directory) / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def make_recording(directory, capture_epoch, size, ext=".mjpeg", mtime=None, sidecar_size=50):
    """A recording named the way camera.py actually names them, so
    cleanup.capture_time() can parse it. `mtime` defaults to capture_epoch
    but can be set independently to simulate a later mp4 conversion
    rewriting the file's mtime without changing its capture time."""
    name = "rec_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime(capture_epoch)) + ext
    path = Path(directory) / name
    path.write_bytes(b"x" * size)
    t = capture_epoch if mtime is None else mtime
    os.utime(path, (t, t))
    sidecar = path.with_suffix(".json")
    sidecar.write_bytes(b"x" * sidecar_size)
    os.utime(sidecar, (t, t))
    return path


def fake_disk_usage(captures_dir, capacity):
    """A disk_usage() stand-in whose 'free' reflects whatever cleanup has
    actually deleted so far, without touching the real filesystem."""
    def fn(path):
        used = sum(p.stat().st_size for p in Path(captures_dir).iterdir()
                   if p.is_file() and p.suffix in MEDIA_SIZE_EXTS)
        free = capacity - used
        return {"total": capacity, "free": free,
                "used_percent": round(100 * used / capacity, 1) if capacity else 0}
    return fn


class CleanupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # GB=1 lets test thresholds be plain byte counts instead of
        # fractions of a gigabyte.
        self._real_gb = cleanup_mod.GB
        cleanup_mod.GB = 1
        self.addCleanup(setattr, cleanup_mod, "GB", self._real_gb)

    def patch_capacity(self, capacity):
        real_disk_usage = cleanup_mod.system_status.disk_usage
        cleanup_mod.system_status.disk_usage = fake_disk_usage(self.tmp, capacity)
        self.addCleanup(setattr, cleanup_mod.system_status, "disk_usage", real_disk_usage)

    def make_manager(self, config, camera=None):
        return CleanupManager(config, self.tmp, camera or FakeCamera())

    def write_marker(self, capture_epoch):
        (Path(self.tmp) / ".archived_through").write_text(str(capture_epoch))

    def names(self):
        return {p.name for p in Path(self.tmp).iterdir()}

    # --- scenarios ---

    def test_oldest_archived_deleted_first_and_sidecars_follow(self):
        make_file(self.tmp, "snap.jpg", 500, BASE)
        rec_a = make_recording(self.tmp, BASE + 2000, 2000)
        rec_b = make_recording(self.tmp, BASE + 3000, 2000)
        rec_c = make_recording(self.tmp, BASE + 4000, 2000)
        self.write_marker(BASE + 3000)  # rec_a, rec_b archived; rec_c is not

        self.patch_capacity(8150)  # initial free = 8150 - 6650 = 1500
        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 1000}
        mgr = self.make_manager(config)

        mgr._cleanup_once()

        names = self.names()
        self.assertNotIn(rec_a.name, names)
        self.assertNotIn(rec_a.with_suffix(".json").name, names, "sidecar must go with its recording")
        self.assertNotIn(rec_b.name, names)
        self.assertNotIn(rec_b.with_suffix(".json").name, names)
        self.assertIn(rec_c.name, names, "unarchived recording must survive")
        self.assertIn(rec_c.with_suffix(".json").name, names)
        self.assertIn("snap.jpg", names, "snapshots are never deleted")
        self.assertEqual(mgr._last_deleted, 2)
        self.assertFalse(mgr._emergency)

    def test_stops_at_unarchived_boundary_even_if_below_target(self):
        # Only one archived file exists; deleting it isn't enough to reach
        # min_free_gb, but the newer, unarchived file must still survive
        # since we're not below emergency_free_gb.
        rec_old = make_recording(self.tmp, BASE + 1000, 1000)
        rec_new = make_recording(self.tmp, BASE + 2000, 1000)
        self.write_marker(BASE + 1000)  # only rec_old is archived
        self.patch_capacity(2600)  # initial free = 2600-2100*2 = 400

        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 100}
        mgr = self.make_manager(config)
        mgr._cleanup_once()

        names = self.names()
        self.assertNotIn(rec_old.name, names)
        self.assertIn(rec_new.name, names)
        self.assertEqual(mgr._last_deleted, 1)

    def test_missing_marker_treated_as_nothing_archived(self):
        # No .archived_through at all: normal deletion must not touch
        # anything; only the emergency path may.
        rec_old = make_recording(self.tmp, BASE + 1000, 1000)
        self.patch_capacity(2500)  # free = 1450, comfortably above emergency

        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 100}
        mgr = self.make_manager(config)
        mgr._cleanup_once()

        self.assertTrue(rec_old.exists(), "no marker + not in emergency => nothing may be deleted")
        self.assertEqual(mgr._last_deleted, 0)

    def test_emergency_path_deletes_unarchived_oldest_first(self):
        rec_a = make_recording(self.tmp, BASE + 2000, 2000)
        rec_b = make_recording(self.tmp, BASE + 3000, 2000)
        rec_c = make_recording(self.tmp, BASE + 4000, 2000)
        # no marker at all -> everything is "unarchived"
        self.patch_capacity(6000)  # initial free well under emergency_free_gb

        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 3000}
        mgr = self.make_manager(config)
        mgr._cleanup_once()

        names = self.names()
        self.assertNotIn(rec_a.name, names)
        self.assertNotIn(rec_b.name, names)
        self.assertIn(rec_c.name, names,
                       "stops deleting once free space clears the emergency floor")
        self.assertEqual(mgr._last_deleted, 2)

    def test_snapshot_never_deleted_even_stuck_in_emergency(self):
        make_file(self.tmp, "snap.jpg", 500, BASE)
        rec = make_recording(self.tmp, BASE + 2000, 2000)
        # Capacity so tight that even after deleting every recording, the
        # lone snapshot keeps free space below the emergency floor.
        self.patch_capacity(1400)

        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 1000}
        mgr = self.make_manager(config)
        mgr._cleanup_once()

        names = self.names()
        self.assertNotIn(rec.name, names)
        self.assertIn("snap.jpg", names)
        self.assertTrue(mgr._emergency, "still below emergency_free_gb with nothing left to delete")

    def test_in_progress_recording_is_never_a_candidate(self):
        rec_a = make_recording(self.tmp, BASE + 2000, 2000)  # oldest, but being written
        rec_b = make_recording(self.tmp, BASE + 3000, 2000)
        self.write_marker(BASE + 3000)  # both archived
        self.patch_capacity(4600)  # initial free = 400, needs one deletion

        camera = FakeCamera(recording_file=rec_a.name)
        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 100}
        mgr = self.make_manager(config, camera)
        mgr._cleanup_once()

        names = self.names()
        self.assertIn(rec_a.name, names, "the file currently being recorded must survive")
        self.assertNotIn(rec_b.name, names, "cleanup should fall through to the next oldest")

    def test_transcoding_file_is_never_a_candidate(self):
        rec_a = make_recording(self.tmp, BASE + 2000, 2000)  # oldest, but mid-transcode
        rec_b = make_recording(self.tmp, BASE + 3000, 2000)
        self.write_marker(BASE + 3000)
        self.patch_capacity(4600)

        camera = FakeCamera(transcoding={rec_a.name})
        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 100}
        mgr = self.make_manager(config, camera)
        mgr._cleanup_once()

        names = self.names()
        self.assertIn(rec_a.name, names, "a file mid-transcode must survive")
        self.assertNotIn(rec_b.name, names)

    def test_ordering_uses_capture_time_not_mtime(self):
        # rec_a is the OLDEST recording (by filename) and archived, but its
        # mtime is artificially recent -- as a manual mp4 conversion would
        # leave it. rec_b is a NEWER, unarchived recording with an ordinary
        # (older-looking) mtime. If cleanup ever regresses to sorting or
        # comparing by mtime instead of capture_time(), it would pick rec_b
        # first (wrong: unarchived) and/or decide rec_a isn't archived
        # (wrong: its capture time is well before the marker).
        rec_a = make_recording(self.tmp, BASE + 1000, 2000, mtime=BASE + 9000)
        rec_b = make_recording(self.tmp, BASE + 2000, 2000, mtime=BASE + 2000)
        self.write_marker(BASE + 1500)  # archives rec_a's capture time, not rec_b's

        self.patch_capacity(4600)  # initial free = 400, needs exactly one deletion
        config = {"cleanup_enabled": 1, "min_free_gb": 5000, "emergency_free_gb": 100}
        mgr = self.make_manager(config)
        mgr._cleanup_once()

        names = self.names()
        self.assertNotIn(rec_a.name, names, "archived by capture time despite a recent mtime")
        self.assertIn(rec_b.name, names, "unarchived by capture time despite an older mtime")
        self.assertEqual(mgr._last_deleted, 1)

    def test_disabled_master_switch_is_respected_by_run_loop(self):
        # _cleanup_once() itself has no enabled check (that's the loop's
        # job) — verify the loop honours cleanup_enabled=0 by never calling
        # into deletion logic when it's off.
        rec = make_recording(self.tmp, BASE + 1000, 2000)
        self.patch_capacity(500)  # deeply below any threshold
        config = {"cleanup_enabled": 0, "min_free_gb": 5000, "emergency_free_gb": 100,
                  "cleanup_interval": 3600}
        mgr = self.make_manager(config)
        mgr._stop.set()  # make _run() exit after (not) doing one pass
        mgr._run()
        self.assertTrue(rec.exists())


if __name__ == "__main__":
    unittest.main()
