"""One browser process: who is it, and handing URLs to it.

Pure Python, so that `python -m indigoshell.browser.qt <url>` with a
browser already running hands its URL over and exits without loading
Qt or Chromium.

The running browser holds an flock on `protocol.instance_lock_path()`
for its whole life and listens on `protocol.instance_socket_path()`
(control.ControlServer, which also takes the agent's requests), each
its profile's (browser/profiles.py: one browser per profile). Between
taking the lock and listening it spends a second or so starting
QtWebEngine; a hand-off made meanwhile keeps trying until it is
listening.
"""

import fcntl
import os
import socket
import time

from indigoshell.browser import protocol

HANDOFF_TIMEOUT = 15.0          # s: a cold QtWebEngine start, with room


class HandoffFailed(RuntimeError):
    pass


def try_lock(profile: str | None = None) -> int | None:
    """The instance lock's fd if we are now `profile`'s browser, None if
    another process is. The fd stays open for the process's life (it is
    not inherited: Python opens files close-on-exec)."""
    path = protocol.instance_lock_path(profile)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _send(urls: list[str], timeout: float, profile: str | None = None) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(protocol.instance_socket_path(profile))
        s.sendall(protocol.encode(protocol.open_urls(urls)))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return protocol.decode(data.split(b"\n", 1)[0]) or {}


def claim_or_hand_off(urls: list[str], timeout: float = HANDOFF_TIMEOUT, *,
                      profile: str | None = None) -> int | None:
    """Become `profile`'s browser (returns the lock fd), or give `urls` to
    the one running (returns None). Raises HandoffFailed if neither works
    out within `timeout`: a browser holds the lock but never answers."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        fd = try_lock(profile)
        if fd is not None:
            return fd
        try:
            reply = _send(urls, timeout=max(0.5, deadline - time.monotonic()), profile=profile)
            if reply.get(protocol.TYPE) == protocol.OK:
                return None
            last = f"answered {reply!r}"
        except OSError as e:          # not listening yet, or going away
            last = str(e)
        if time.monotonic() >= deadline:
            raise HandoffFailed(f"a browser is running but did not take the URLs ({last})")
        time.sleep(0.1)
