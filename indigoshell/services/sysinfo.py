"""Cached system readouts shared by multiple meters.

`psutil.cpu_percent(interval=None)` returns the percent since the *last
call*, so multiple subscribers in the same tick burst would each see
garbage. We snapshot once per ~400ms and serve the cached value.
"""

import time

import psutil

_CPU_CACHE_TTL = 0.5  # matches StatMeter.interval_ms
_cpu_cache = {"value": 0.0, "last": 0.0}


def cpu_percent() -> float:
    now = time.monotonic()
    if now - _cpu_cache["last"] >= _CPU_CACHE_TTL:
        _cpu_cache["value"] = psutil.cpu_percent(interval=None)
        _cpu_cache["last"] = now
    return _cpu_cache["value"]


def memory_percent() -> float:
    return psutil.virtual_memory().percent


def temperature_package() -> float:
    """Intel coretemp package die temp, or first sensor as fallback."""
    temps = psutil.sensors_temperatures() or {}
    for entry in temps.get("coretemp", []):
        if entry.label.startswith("Package"):
            return float(entry.current)
    entries = next(iter(temps.values()), [])
    return float(entries[0].current) if entries else 0.0
