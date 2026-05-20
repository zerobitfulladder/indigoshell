"""Reusable cyberpunk segmented progress bar.

Renders N discrete cells with a small gap between them — the "ticks
fill from left" look used by the notification toast meter, the disk
indicator in the hardware panel, etc.

The widget is data-only — call `set_value(pct)` with a 0–100 number.
For non-percent metrics (temperatures, byte counts, …) the caller is
responsible for converting to 0–100 first.

Pass `color` as either a hex string (fixed color) or a callable
`(pct: float) -> str` so the color can shift with the value
(e.g. cyan → yellow → magenta as utilization rises).
"""

from typing import Callable, Union

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from .base import paint


__all__ = ["BarMeter"]


ColorSpec = Union[str, Callable[[float], str]]


class BarMeter(Gtk.DrawingArea):

    def __init__(
        self,
        *,
        color: ColorSpec = theme.CYAN_BRIGHT,
        dim_color: str = theme.NOTIF_METER_DIM,
        segments: int = 32,
        gap: int = 2,
        thick: int = 8,
        min_width: int = 220,
    ) -> None:
        super().__init__()
        self._color = color
        self._dim = dim_color
        self._n = segments
        self._gap = gap
        self._h = thick
        self._pct = 0.0
        self.set_size_request(min_width, thick + 4)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)
        self.connect("draw", self._on_draw)

    def set_value(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self.queue_draw()

    def _resolve_color(self) -> str:
        return self._color(self._pct) if callable(self._color) else self._color

    def _on_draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n   = self._n
        gap = self._gap
        h   = self._h
        tick_w = max(1.0, (width - gap * (n - 1)) / n)
        ratio = self._pct / 100.0
        fill_end_x = ratio * width
        y = (height - h) / 2
        color = self._resolve_color()
        for i in range(n):
            x = i * (tick_w + gap)
            mid = x + tick_w / 2
            if mid <= fill_end_x:
                paint(cr, color)
            else:
                paint(cr, self._dim, 0.6)
            cr.rectangle(x, y, tick_w, h)
            cr.fill()
        return False
