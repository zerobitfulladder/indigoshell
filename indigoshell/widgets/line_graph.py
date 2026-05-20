"""Reusable minimal cyberpunk line graph.

A fixed-width ring buffer of samples rendered as:

  • a thin stroked polyline in `color`
  • an optional translucent fill below the line
  • two faint horizontal gridlines (50% and 100% of the value range)
  • a small accent dot on the most recent sample

The widget owns no data source — feed it via `push(value)`. Callers
size it via `height` and `min_width`. It does not know about percent
vs. absolute units; configure `vmin`/`vmax` (or pass `autoscale=True`
to fit the visible window).
"""

from collections import deque

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from .base import paint


__all__ = ["LineGraph"]


class LineGraph(Gtk.DrawingArea):

    def __init__(
        self,
        *,
        color: str = theme.CYAN_BRIGHT,
        fill_alpha: float = 0.14,
        max_samples: int = 60,
        height: int = 60,
        min_width: int = 80,
        vmin: float = 0.0,
        vmax: float = 100.0,
        autoscale: bool = False,
        autoscale_top: bool = False,
        line_thick: float = 1.4,
        show_grid: bool = True,
        show_dot: bool = False,
    ) -> None:
        super().__init__()
        self.set_size_request(min_width, height)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)

        self._color = color
        self._fill_alpha = fill_alpha
        self._max = max_samples
        self._vmin = vmin
        self._vmax = vmax
        self._autoscale = autoscale
        self._autoscale_top = autoscale_top
        self._line_thick = line_thick
        self._show_grid = show_grid
        self._show_dot = show_dot
        self._samples: deque[float] = deque(maxlen=max_samples)

        self.connect("draw", self._on_draw)

    # ── data ────────────────────────────────────────────────────────────
    def push(self, value: float) -> None:
        self._samples.append(float(value))
        self.queue_draw()

    def clear(self) -> None:
        self._samples.clear()
        self.queue_draw()

    def set_color(self, hex_color: str) -> None:
        self._color = hex_color
        self.queue_draw()

    def latest(self) -> float | None:
        return self._samples[-1] if self._samples else None

    # ── drawing ─────────────────────────────────────────────────────────
    def _value_range(self) -> tuple[float, float]:
        if not self._samples:
            return self._vmin, self._vmax
        if self._autoscale:
            lo, hi = min(self._samples), max(self._samples)
            if hi - lo < 1e-6:
                hi = lo + 1.0
            pad = (hi - lo) * 0.1
            return lo - pad, hi + pad
        if self._autoscale_top:
            # Keep zero baseline; let the upper bound float so low
            # values still fill a meaningful portion of the graph.
            hi = max(self._samples)
            # Pad ~15% headroom and clamp to a sensible floor so the
            # very first samples don't get an absurdly tall scale.
            hi = max(hi * 1.15, self._vmin + 5.0)
            return self._vmin, hi
        return self._vmin, self._vmax

    def _y_for(self, value: float, height: int, vmin: float, vmax: float) -> float:
        if vmax - vmin < 1e-6:
            return height / 2
        ratio = (value - vmin) / (vmax - vmin)
        ratio = max(0.0, min(1.0, ratio))
        # 2px top/bottom inset so the dot/line don't sit flush with edges.
        return (height - 4) - ratio * (height - 4) + 2

    def _on_draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        vmin, vmax = self._value_range()

        if self._show_grid:
            # Ceiling line — drawn at the top of the plot area so it
            # reads as a header divider rather than a mid-axis tick.
            paint(cr, theme.BASE_MUTED, 0.35)
            cr.set_line_width(1.0)
            cr.move_to(0, 0.5); cr.line_to(width, 0.5)
            cr.stroke()

        n = len(self._samples)
        if n < 2:
            # Single sample (or none) — just draw the dot if we have one.
            if n == 1 and self._show_dot:
                y = self._y_for(self._samples[0], height, vmin, vmax)
                paint(cr, self._color, 1.0)
                cr.arc(width - 2, y, 2.0, 0, 6.2832)
                cr.fill()
            return False

        # Map each sample to an x: newest sample anchored at the right edge
        # so the graph "scrolls" from right to left as samples accrue.
        cap = self._max
        step = width / max(1, cap - 1)
        x0 = width - (n - 1) * step

        # Fill below the line.
        if self._fill_alpha > 0:
            cr.move_to(x0, height)
            for i, v in enumerate(self._samples):
                x = x0 + i * step
                y = self._y_for(v, height, vmin, vmax)
                cr.line_to(x, y)
            cr.line_to(width, height)
            cr.close_path()
            paint(cr, self._color, self._fill_alpha)
            cr.fill()

        # Line.
        for i, v in enumerate(self._samples):
            x = x0 + i * step
            y = self._y_for(v, height, vmin, vmax)
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        paint(cr, self._color, 0.95)
        cr.set_line_width(self._line_thick)
        cr.set_line_join(1)  # CAIRO_LINE_JOIN_ROUND
        cr.set_line_cap(1)   # CAIRO_LINE_CAP_ROUND
        cr.stroke()

        # Head dot at the latest sample.
        if self._show_dot:
            y = self._y_for(self._samples[-1], height, vmin, vmax)
            paint(cr, self._color, 1.0)
            cr.arc(width - 2, y, 2.2, 0, 6.2832)
            cr.fill()
        return False
