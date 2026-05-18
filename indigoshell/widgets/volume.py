"""Volume widget — bars + pactl plumbing flattened into one class.

Subscribes to `pactl subscribe` and only re-queries sink state when
something changes. Renders as a vertical stack of cells that light up
proportional to the level; bars flip red when muted.
"""

import re
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from ..services import proc
from ..style import Style
from .base import Widget, paint

SINK = "@DEFAULT_SINK@"


class Volume(Widget):
    """Volume rendered as a stack of N cells lit proportional to the
    level. Click to toggle mute, scroll to change. Event-driven via
    `pactl subscribe`."""

    def __init__(
        self,
        step: int = 5,
        cells: int = 8,
        cell_thick: int = 3,
        gap: int = 1,
        width: int = 18,
        style: Style | None = None,
        **kwargs,
    ):
        kwargs.setdefault("on_left_click", self._toggle_mute)
        kwargs.setdefault("on_scroll_up", self._scroll_up)
        kwargs.setdefault("on_scroll_down", self._scroll_down)
        super().__init__(style, **kwargs)
        self.step = step
        self.cells = max(1, cells)
        self.cell_thick = max(1, cell_thick)
        self.gap = max(0, gap)
        self.w = max(4, width)
        self._sub_proc: subprocess.Popen | None = None
        self._last_volume: int = 0
        self._last_muted: bool = False
        self._ev: Gtk.EventBox | None = None
        self._percent: float = 0.0
        self._muted: bool = False

    def build_widget(self):
        cells_h = self.cells * self.cell_thick + (self.cells - 1) * self.gap
        filler = Gtk.Box()
        filler.set_size_request(self.w, cells_h)
        return filler

    def build(self):
        w = super().build()
        w.connect_after("draw", self._draw)
        self._ev = w
        return w

    def start(self):
        threading.Thread(target=self._refresh, daemon=True).start()
        self._sub_proc = proc.subscribe(["pactl", "subscribe"], self._on_line)

    def stop(self):
        if self._sub_proc and self._sub_proc.poll() is None:
            self._sub_proc.terminate()
        self._sub_proc = None

    def _on_line(self, line: str) -> None:
        if "sink" in line or "server" in line:
            self._refresh()

    def _refresh(self):
        muted = "yes" in proc.run(["pactl", "get-sink-mute", SINK]).lower()
        m = re.search(r"(\d+)%", proc.run(["pactl", "get-sink-volume", SINK]))
        vol = int(m.group(1)) if m else 0
        self._last_muted = muted
        self._last_volume = vol
        GLib.idle_add(self._apply_state, vol, muted)

    def _apply_state(self, vol: int, muted: bool) -> bool:
        self._muted = muted
        self._percent = float(vol)
        if self._ev is not None:
            self._ev.queue_draw()
        return False

    def _draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n = self.cells
        cell_h = max(1.0, (height - self.gap * (n - 1)) / n)
        lit_color = theme.ERROR if self._muted else theme.VIOLET_BRIGHT
        dim_color = theme.MAGENTA_DIM if self._muted else theme.VIOLET_DIM
        lit_count = n if self._muted else int(self._percent / 100.0 * n)
        for i in range(n):
            # i=0 is the bottom-most cell; lit fills bottom-up.
            y = height - (i + 1) * cell_h - i * self.gap
            color = lit_color if i < lit_count else dim_color
            paint(cr, color)
            cr.rectangle(0, y, width, cell_h)
            cr.fill()
        return False

    def _toggle_mute(self, _w):
        proc.fire(["pactl", "set-sink-mute", SINK, "toggle"])

    def _scroll_up(self, _w):
        if self._last_muted:
            proc.fire(["pactl", "set-sink-mute", SINK, "0"])
            return
        new = min(100, self._last_volume + self.step)
        proc.fire(["pactl", "set-sink-volume", SINK, f"{new}%"])

    def _scroll_down(self, _w):
        if self._last_muted:
            proc.fire(["pactl", "set-sink-mute", SINK, "0"])
            return
        new = max(0, self._last_volume - self.step)
        proc.fire(["pactl", "set-sink-volume", SINK, f"{new}%"])
