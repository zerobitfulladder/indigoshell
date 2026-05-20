"""Shared music-status broker.

Tracks player status (Playing / Paused / Stopped / ...) using
`playerctl --follow status`, read via GLib.io_add_watch — no extra
thread. Exposes add_listener/remove_listener for widgets and
`set_playing()` for external drivers, custom configs, or tests.
"""

import os
import subprocess
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from . import proc


class MusicStatus:
    def __init__(self, player: str | None = None) -> None:
        self._player = player
        self._listeners: list[Callable[[bool], None]] = []
        self._proc: subprocess.Popen | None = None
        self._watch_id: int | None = None
        self._buf = b""
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)
        if self._proc is None:
            self._start()
        # Replay the current state so the listener doesn't wait for the
        # next status change to render correctly.
        try:
            fn(self._playing)
        except Exception:
            pass

    def remove_listener(self, fn: Callable[[bool], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass
        if not self._listeners:
            self._stop()

    def set_playing(self, playing: bool) -> None:
        """External hook — drive the broker manually (testing, custom
        triggers). Also called internally on every playerctl line."""
        if playing == self._playing:
            return
        self._playing = playing
        for fn in list(self._listeners):
            try:
                fn(playing)
            except Exception:
                pass

    def _start(self) -> None:
        cmd = ["playerctl", "--follow", "status"]
        if self._player:
            cmd[1:1] = ["--player", self._player]
        self._proc = proc.popen(cmd, bufsize=0)
        if self._proc is None or self._proc.stdout is None:
            return
        fd = self._proc.stdout.fileno()
        os.set_blocking(fd, False)
        self._watch_id = GLib.io_add_watch(fd, GLib.IO_IN, self._on_io)

    def _stop(self) -> None:
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                pass
            self._proc = None
        self._buf = b""

    def _on_io(self, fd, _condition) -> bool:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return True
        if not chunk:
            self._stop()
            return False
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode(errors="ignore").strip()
            if text:
                self.set_playing(text == "Playing")
        return True


_statuses: dict[str | None, MusicStatus] = {}


def get_status(player: str | None = None) -> MusicStatus:
    """Per-player singleton. `player=None` watches every MPRIS source
    (legacy callers); pass a specific name to filter to one player."""
    if player not in _statuses:
        _statuses[player] = MusicStatus(player)
    return _statuses[player]
