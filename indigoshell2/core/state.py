"""Persistent state — the things the shell should remember across runs.

A flat JSON file under `$XDG_STATE_HOME/indigoshell2/state.json`, keys
namespaced by whoever owns them (`display.output`). Read once, written
atomically on every `set`, so a crash mid-write leaves the previous
file rather than half of a new one. Small by design: this is for
choices a menu made (which display, which sink), not for caches.
"""

import json
import logging
import os
import tempfile
from typing import Any

from . import paths

log = logging.getLogger(__name__)

_cache: dict | None = None


def path() -> str:
    return os.path.join(paths.state_dir(), "state.json")


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(path()) as f:
                loaded = json.load(f)
            _cache = loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            _cache = {}
        except (OSError, ValueError):
            log.exception("state file unreadable; starting empty")
            _cache = {}
    return _cache


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set(key: str, value: Any) -> None:      # noqa: A001 - mirrors dict.get/set
    data = _load()
    if data.get(key) == value and key in data:
        return
    data[key] = value
    _write(data)


def delete(key: str) -> None:
    data = _load()
    if key in data:
        del data[key]
        _write(data)


def _write(data: dict) -> None:
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
