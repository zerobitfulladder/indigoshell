"""Unified subprocess utilities.

Three shapes:

- `run(cmd)` — capture stdout, return string. Swallows missing-binary
  and timeout errors and returns "".
- `fire(cmd)` — fire-and-forget. `detach=True` runs in a new session so
  the child outlives the parent (for spawning desktop apps).
- `subscribe(cmd, on_line)` — spawn a long-running command and call
  `on_line(line)` for every stdout line. Returns the `Popen` handle so
  the caller can `terminate()` it during shutdown. `on_line` runs on a
  worker thread — use `GLib.idle_add` to touch UI.
"""

import subprocess
import threading
from typing import Callable, Sequence


def run(cmd: Sequence[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
        return r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def fire(cmd: Sequence[str], *, detach: bool = False) -> None:
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=detach,
        )
    except FileNotFoundError:
        pass


def popen(
    cmd: Sequence[str],
    *,
    text: bool = False,
    bufsize: int = -1,
) -> subprocess.Popen | None:
    """Bare spawn with stdout=PIPE, stderr=DEVNULL. Returns the handle
    so the caller can read stdout / fd directly. Use this when you need
    raw byte access or a custom reader loop (cava bands, parec PCM,
    GLib.io_add_watch). For line-based text, use `subscribe` instead.

    `bufsize=-1` (Python default) gives a BufferedReader — `read(n)`
    blocks until exactly n bytes arrive. Pass `bufsize=0` for raw
    unbuffered IO (only useful if you're reading the fd directly,
    bypassing Python's buffer)."""
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=text,
            bufsize=bufsize,
        )
    except FileNotFoundError:
        return None


def subscribe(
    cmd: Sequence[str],
    on_line: Callable[[str], None],
    *,
    on_missing: Callable[[], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> subprocess.Popen | None:
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        if on_missing:
            on_missing()
        return None

    def _read():
        assert p.stdout
        for line in p.stdout:
            on_line(line)
        if on_exit:
            on_exit(p.wait())

    threading.Thread(target=_read, daemon=True).start()
    return p
