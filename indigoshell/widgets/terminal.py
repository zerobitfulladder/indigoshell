import os
import signal

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Pango, Vte

from .. import theme
from ..style import Style
from .base import Widget


def _rgba(hex_str: str, alpha: float = 1.0) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.parse(hex_str)
    rgba.alpha = alpha
    return rgba


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
        env: dict[str, str] | None = None,
        style: Style | None = None,
        **kwargs,
    ):
        super().__init__(style, **kwargs)
        self.command = command
        self.cols = cols
        self.rows = rows
        self.transparent = transparent
        self.respawn = respawn
        # Extra environment variables merged on top of the daemon's env
        # before spawning the child. Use for tools whose themes are
        # controlled by env (NEWT_COLORS, LESS, GREP_COLORS, …).
        self.env = env or {}
        self._term: Vte.Terminal | None = None
        self._pid: int | None = None
        self._stopping = False

    def build_widget(self):
        term = Vte.Terminal()
        term.set_size(self.cols, self.rows)
        if self.transparent:
            term.set_clear_background(False)
            bg = _rgba(theme.TERMINAL_BG, alpha=0.0)
        else:
            bg = _rgba(theme.TERMINAL_BG)
        fg = _rgba(theme.TERMINAL_FG)
        palette = [_rgba(c) for c in theme.TERMINAL_PALETTE]
        term.set_colors(fg, bg, palette)
        term.set_color_cursor(_rgba(theme.TERMINAL_CURSOR))
        term.set_font(Pango.FontDescription(theme.TERMINAL_FONT))
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
        # VTE's envv replaces the child env wholesale when non-None, so
        # merge with the daemon's env first; otherwise the child loses
        # PATH/HOME/SHELL/etc.
        envv: list[str] | None
        if self.env:
            merged = {**os.environ, **self.env}
            envv = [f"{k}={v}" for k, v in merged.items()]
        else:
            envv = None
        self._term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            self.command,
            envv,
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
