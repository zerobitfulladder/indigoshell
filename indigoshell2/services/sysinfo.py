"""Shared system readouts.

One 1 Hz sampler polls psutil and caches CPU/RAM, keeping a rolling
60-sample (1 minute) history. Every reader shares it — `cpu_percent()`
has "since the last call" semantics in psutil, so two independent callers
would each get a different, wrong answer.

Ported from v1 unchanged apart from the timer: a GLib timeout becomes an
asyncio task on the shell's one loop.
"""

import asyncio
import logging
import os
from collections import deque

import psutil

log = logging.getLogger(__name__)

_SAMPLE_INTERVAL = 1.0
_HISTORY_SAMPLES = 60

_cpu_history: deque[float] = deque(maxlen=_HISTORY_SAMPLES)
_ram_history: deque[float] = deque(maxlen=_HISTORY_SAMPLES)
_cpu_latest = 0.0
_ram_latest = 0.0
_task: asyncio.Task | None = None


_subscribers: list = []


def subscribe(callback) -> None:
    """Called after every sample.

    Consumers that draw the history must redraw on this rather than on a
    timer of their own. Two 1Hz clocks are not one clock: a widget
    polling at its own 1Hz drifts against the sampler and ends up
    appending the same latest value twice, so its graph scrolls without
    the number changing.
    """
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe(callback) -> None:
    if callback in _subscribers:
        _subscribers.remove(callback)


def _sample() -> None:
    global _cpu_latest, _ram_latest
    _cpu_latest = psutil.cpu_percent(interval=None)
    _ram_latest = psutil.virtual_memory().percent
    _cpu_history.append(_cpu_latest)
    _ram_history.append(_ram_latest)


def _notify() -> None:
    # Deliberately not called from `_sample()`. The priming sample runs
    # inside `_ensure_started()` *before* `_task` is set, and a subscriber
    # that reads `cpu_percent()` would re-enter `_ensure_started()`, find
    # `_task` still None, and sample again — recursing until the stack
    # blew. Only the loop notifies.
    for callback in list(_subscribers):
        try:
            callback()
        except Exception:
            log.exception("sysinfo subscriber failed")


async def _loop() -> None:
    while True:
        await asyncio.sleep(_SAMPLE_INTERVAL)
        try:
            _sample()
            _notify()
        except Exception:
            log.exception("sysinfo sample failed")


def _ensure_started() -> None:
    global _task
    if _task is not None:
        return
    _sample()          # prime, so the first read isn't 0%
    try:
        _task = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        pass           # no loop yet (e.g. imported by a config check)


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


# ── CPU package temperature ─────────────────────────────────────────────
_temp_path: str | None = None
_temp_resolved = False


def _resolve_temp_path() -> str | None:
    """Locate the CPU package die temperature sysfs file, once.

    psutil.sensors_temperatures() reads every hwmon sensor (~18 files) on
    each call; we need one. Caching the path makes the hot path a single
    open()+read().
    """
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return None
    candidates = []
    for entry in os.listdir(base):
        dev = os.path.join(base, entry)
        try:
            with open(os.path.join(dev, "name")) as f:
                candidates.append((f.read().strip(), dev))
        except OSError:
            continue
    priority = ("coretemp", "k10temp", "zenpower")
    candidates.sort(key=lambda c: priority.index(c[0])
                    if c[0] in priority else len(priority))
    for _name, dev in candidates:
        try:
            labels = sorted(f for f in os.listdir(dev) if f.endswith("_label"))
        except OSError:
            continue
        for label_file in labels:
            try:
                with open(os.path.join(dev, label_file)) as f:
                    label = f.read().strip()
            except OSError:
                continue
            if label.startswith(("Package", "Tctl", "Tdie")):
                return os.path.join(dev, label_file.replace("_label", "_input"))
    for _name, dev in candidates:
        path = os.path.join(dev, "temp1_input")
        if os.path.exists(path):
            return path
    return None


def temperature_package() -> float:
    global _temp_path, _temp_resolved
    if not _temp_resolved:
        _temp_path = _resolve_temp_path()
        _temp_resolved = True
        log.debug("cpu temperature source: %s", _temp_path)
    if _temp_path is None:
        return 0.0
    try:
        with open(_temp_path) as f:
            return int(f.read()) / 1000.0
    except OSError:
        return 0.0
