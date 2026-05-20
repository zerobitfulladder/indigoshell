"""Shared system readouts with background sampling.

A single 1 Hz GLib timer polls psutil once per second, caches the latest
CPU/RAM percent, and keeps a rolling 60-sample (1 min) history deque
for each. StatMeter and HardwarePanel both read from this — no more
multiple callers racing `psutil.cpu_percent(interval=None)`'s global
"since last call" semantics. The sampler starts lazily on first read
and runs for the life of the process.
"""

from collections import deque

import gi
import psutil

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

_SAMPLE_INTERVAL_MS = 1000
_HISTORY_SAMPLES    = 60   # 1 minute at 1 Hz

_cpu_history: deque[float] = deque(maxlen=_HISTORY_SAMPLES)
_ram_history: deque[float] = deque(maxlen=_HISTORY_SAMPLES)
_cpu_latest: float = 0.0
_ram_latest: float = 0.0
_timer_id: int | None = None


def _sample() -> bool:
    global _cpu_latest, _ram_latest
    _cpu_latest = psutil.cpu_percent(interval=None)
    _ram_latest = psutil.virtual_memory().percent
    _cpu_history.append(_cpu_latest)
    _ram_history.append(_ram_latest)
    return True


def _ensure_started() -> None:
    global _timer_id
    if _timer_id is not None:
        return
    # Prime once so first read isn't 0%.
    _sample()
    _timer_id = GLib.timeout_add(_SAMPLE_INTERVAL_MS, _sample)


def cpu_percent() -> float:
    _ensure_started()
    return _cpu_latest


def memory_percent() -> float:
    _ensure_started()
    return _ram_latest


def cpu_history() -> list[float]:
    _ensure_started()
    return list(_cpu_history)


def memory_history() -> list[float]:
    _ensure_started()
    return list(_ram_history)


_temp_path: str | None = None
_temp_path_resolved: bool = False


def _resolve_temp_path() -> str | None:
    """Locate the sysfs file for the CPU package die temperature once.
    psutil.sensors_temperatures() reads every hwmon sensor (~18 files
    on a typical laptop) on each call; we only need one. Caching the
    path lets the hot poll path do a single open()+read()."""
    import os
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return None
    candidates = []
    for entry in os.listdir(base):
        dev = os.path.join(base, entry)
        try:
            with open(os.path.join(dev, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        candidates.append((name, dev))
    # Prefer Intel coretemp's "Package id 0", then AMD k10temp, then
    # anything else with a Package-labelled input, then any temp input.
    name_priority = ("coretemp", "k10temp", "zenpower")
    candidates.sort(key=lambda c: name_priority.index(c[0]) if c[0] in name_priority else len(name_priority))
    for _name, dev in candidates:
        for label_file in sorted(f for f in os.listdir(dev) if f.endswith("_label")):
            try:
                with open(os.path.join(dev, label_file)) as f:
                    label = f.read().strip()
            except OSError:
                continue
            if label.startswith("Package") or label.startswith("Tctl") or label.startswith("Tdie"):
                return os.path.join(dev, label_file.replace("_label", "_input"))
    # Last resort: any temp1_input from the first hwmon device.
    for _name, dev in candidates:
        path = os.path.join(dev, "temp1_input")
        if os.path.exists(path):
            return path
    return None


def temperature_package() -> float:
    """CPU package die temperature in °C. Reads a single sysfs file
    after a one-time scan to find the right one."""
    global _temp_path, _temp_path_resolved
    if not _temp_path_resolved:
        _temp_path = _resolve_temp_path()
        _temp_path_resolved = True
    if _temp_path is None:
        return 0.0
    try:
        with open(_temp_path) as f:
            return int(f.read()) / 1000.0
    except OSError:
        return 0.0
