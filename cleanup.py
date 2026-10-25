"""Background cleanup: deletes oldest recordings when captures/ runs low on
space, in the same style as motion.py — a daemon thread started from app.py.

The PC-side archive script (see pc-archive/) pulls recordings off the Pi and
writes captures/.archived_through back here: the capture time (unix seconds,
parsed from the rec_YYYYMMDD_HHMMSS filename — see capture_time() below) of
the newest recording it has safely archived. This module only deletes
recordings at or before that point, so the Pi never deletes something the
archive hasn't captured yet.

Retention is ordered by capture time, not mtime: converting a .mjpeg to
.mp4 (see camera.py's convert_to_mp4) rewrites the file's mtime to the
conversion time, which could otherwise make an old, already-archived
recording look newer than it is and confuse both this ordering and the
marker comparison.

Exception: if free space drops below emergency_free_gb, unarchived
recordings are deleted too (oldest first), because a full SD card is worse
than losing footage the PC never got around to archiving. Every such
deletion is logged as a warning.

Never touched: .jpg snapshots, the recording currently being written
(camera.recording_state()), and any file currently mid-transcode
(camera.transcoding_names()).
"""
import calendar
import re
import threading
import time
from pathlib import Path

import status as system_status

GB = 1024 ** 3
RECORDING_EXTS = {".mjpeg", ".mp4"}
MARKER_NAME = ".archived_through"
NAME_RE = re.compile(r"^rec_(\d{8})_(\d{6})\.")


def capture_time(path):
    """The recording's start time as encoded in its filename
    (rec_YYYYMMDD_HHMMSS), parsed as UTC so the Pi and the PC agree on it
    regardless of either machine's local timezone. This is what retention
    ordering and the archive marker are keyed on — NOT mtime, which a
    manual mp4 conversion rewrites to the conversion time and would
    otherwise make an old recording look newer than it is. Falls back to
    mtime for anything that doesn't match the naming convention."""
    m = NAME_RE.match(path.name)
    if m:
        try:
            return float(calendar.timegm(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")))
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class CleanupManager:
    def __init__(self, config, captures_dir, camera):
        self.config = config
        self.captures_dir = Path(captures_dir)
        self.camera = camera
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_run = None       # unix ts of the last completed check
        self._last_deleted = 0      # files deleted on that run
        self._emergency = False     # free space currently below emergency_free_gb
        self._thread = threading.Thread(target=self._run, name="cleanup", daemon=True)

    # --- lifecycle ---

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    # --- state for the UI ---

    def state(self):
        with self._lock:
            return {
                "enabled": bool(self.config["cleanup_enabled"]),
                "last_run": self._last_run,
                "last_deleted": self._last_deleted,
                "emergency": self._emergency,
                "min_free_gb": self.config["min_free_gb"],
            }

    # --- the marker handshake ---

    def _archived_through(self):
        """Newest capture time the PC has confirmed archiving, or None if
        the marker is missing (never archived — only the emergency path may
        delete)."""
        marker = self.captures_dir / MARKER_NAME
        try:
            return float(marker.read_text().strip())
        except (OSError, ValueError):
            return None

    # --- candidate listing (stat only, never reads file contents) ---

    def _candidates(self, exclude_names):
        items = []
        for path in self.captures_dir.iterdir():
            if path.suffix not in RECORDING_EXTS or path.name in exclude_names:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append((capture_time(path), path, size))
        items.sort(key=lambda t: t[0])  # oldest first
        return items

    def _delete_recording(self, path, size, archived):
        sidecar = path.with_suffix(".json")
        try:
            path.unlink()
        except OSError as e:
            print(f"[cleanup] failed to delete {path.name}: {e}")
            return False
        try:
            sidecar.unlink()
        except OSError:
            pass  # no sidecar, or already gone — fine
        if archived:
            print(f"[cleanup] deleted {path.name} ({size} bytes) — archived")
        else:
            print(f"[cleanup] WARNING: deleted {path.name} ({size} bytes) — "
                  f"UNARCHIVED, emergency free space path")
        return True

    # --- the loop ---

    def _run(self):
        while not self._stop.is_set():
            interval = self.config["cleanup_interval"]
            if self.config["cleanup_enabled"]:
                try:
                    self._cleanup_once()
                except OSError as e:
                    print(f"[cleanup] error: {e}")
            self._stop.wait(interval)

    def _cleanup_once(self):
        min_free = self.config["min_free_gb"] * GB
        emergency_free = self.config["emergency_free_gb"] * GB
        archived_through = self._archived_through()

        rec_state = self.camera.recording_state()
        exclude = set(self.camera.transcoding_names())
        active_name = rec_state.get("file")
        if active_name:
            exclude.add(active_name)

        free = system_status.disk_usage(self.captures_dir)["free"]
        deleted = 0

        if free < min_free:
            for ctime, path, size in self._candidates(exclude):
                free = system_status.disk_usage(self.captures_dir)["free"]
                if free >= min_free:
                    break
                archived = archived_through is not None and ctime <= archived_through
                if not archived and free >= emergency_free:
                    # Oldest remaining file isn't archived and we're not
                    # desperate enough to touch it yet; every candidate
                    # after this one is newer (and so also unarchived), so
                    # there's nothing more this run can safely do.
                    break
                if self._delete_recording(path, size, archived):
                    deleted += 1

        free = system_status.disk_usage(self.captures_dir)["free"]
        with self._lock:
            self._last_run = time.time()
            self._last_deleted = deleted
            self._emergency = free < emergency_free
