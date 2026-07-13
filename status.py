"""System status: uptime, CPU temperature, wifi signal, free disk space.

Reads Linux /proc and /sys directly (no psutil dependency); each value
degrades to None on the dev laptop where the file doesn't exist.
"""
import shutil
import time

_app_started = time.time()


def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def uptime_seconds():
    raw = _read_file("/proc/uptime")
    if raw:
        return float(raw.split()[0])
    return time.time() - _app_started  # dev fallback: app uptime


def cpu_temp():
    raw = _read_file("/sys/class/thermal/thermal_zone0/temp")
    if raw:
        return round(int(raw.strip()) / 1000, 1)
    return None


def wifi_signal():
    """Signal level in dBm from /proc/net/wireless, or None."""
    raw = _read_file("/proc/net/wireless")
    if not raw:
        return None
    for line in raw.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                return round(float(parts[3].rstrip(".")))
            except ValueError:
                continue
    return None


def disk_usage(path):
    usage = shutil.disk_usage(path)
    return {
        "total": usage.total,
        "free": usage.free,
        "used_percent": round(100 * (usage.total - usage.free) / usage.total, 1),
    }


def get_status(captures_dir):
    return {
        "uptime": round(uptime_seconds()),
        "cpu_temp": cpu_temp(),
        "wifi_dbm": wifi_signal(),
        "disk": disk_usage(captures_dir),
    }
