import os
import signal

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Vte, GLib

from ..style import Style
from .base import Widget


class Terminal(Widget):
    """VTE terminal widget. Spawns `command` on start(), kills it on stop().

    Suitable inside a `Popup` — the process is launched when the popup shows
    and terminated when it dismisses.
    """

    def __init__(
        self,
        command: list[str],
        cols: int = 80,
        rows: int = 20,
        transparent: bool = False,
        respawn: bool = False,
        style: Style | None = None,
        **kwargs,
    ):
        super().__init__(style, **kwargs)
        self.command = command
        self.cols = cols
        self.rows = rows
        self.transparent = transparent
        self.respawn = respawn
        self._term: Vte.Terminal | None = None
        self._pid: int | None = None
        self._stopping = False

    def build_widget(self):
        term = Vte.Terminal()
        term.set_size(self.cols, self.rows)
        if self.transparent:
            term.set_clear_background(False)
        term.connect("child-exited", self._on_child_exited)
        self._term = term
        return term

    def _on_child_exited(self, _term, _status):
        self._pid = None
        if self.respawn and not self._stopping:
            self.start()

    def start(self):
        if self._term is None or self._pid is not None:
            return
        self._term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            self.command,
            None,
            GLib.SpawnFlags.SEARCH_PATH,
            None,
            None,
            -1,
            None,
            self._on_spawn,
        )

    def _on_spawn(self, _term, pid, error):
        if error is not None or pid == -1:
            return
        self._pid = pid

    def stop(self):
        self._stopping = True
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._pid = None
