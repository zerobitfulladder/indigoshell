"""Minimal cyberpunk line graph.

A ring buffer of samples drawn as a stroked polyline, an optional
translucent fill beneath it, a faint ceiling rule, and an accent dot on
the newest sample. Owns no data source — feed it with `push(value)`.

Ported from v1 unchanged; cairo's path building becomes a skia.Path, and
the round join/cap that cairo set explicitly become Paint properties.
"""

from collections import deque

import skia

from .. import theme
from .base import Size, Widget


class LineGraph(Widget):
    def __init__(self, *, color: str = theme.CYAN_BRIGHT,
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
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.color = color
        self.fill_alpha = fill_alpha
        self.max_samples = max_samples
        self.height = height
        self.min_width = min_width
        self.vmin = vmin
        self.vmax = vmax
        self.autoscale = autoscale
        self.autoscale_top = autoscale_top
        self.line_thick = line_thick
        self.show_grid = show_grid
        self.show_dot = show_dot
        self._samples: deque[float] = deque(maxlen=max_samples)

    # ── data ────────────────────────────────────────────────────────────
    def push(self, value: float) -> None:
        self._samples.append(float(value))
        self.invalidate()

    def clear(self) -> None:
        self._samples.clear()
        self.invalidate()

    def set_samples(self, values) -> None:
        self._samples = deque(values, maxlen=self.max_samples)
        self.invalidate()

    def latest(self) -> float | None:
        return self._samples[-1] if self._samples else None

    # ── scale ───────────────────────────────────────────────────────────
    def _value_range(self) -> tuple[float, float]:
        if not self._samples:
            return self.vmin, self.vmax
        if self.autoscale:
            lo, hi = min(self._samples), max(self._samples)
            if hi - lo < 1e-6:
                hi = lo + 1.0
            pad = (hi - lo) * 0.1
            return lo - pad, hi + pad
        if self.autoscale_top:
            # Keep the zero baseline but let the top float, so low values
            # still fill a meaningful part of the plot. Clamp the floor so
            # the first few samples don't produce an absurd scale.
            hi = max(max(self._samples) * 1.15, self.vmin + 5.0)
            return self.vmin, hi
        return self.vmin, self.vmax

    def _y_for(self, value: float, top: float, height: float,
               vmin: float, vmax: float) -> float:
        if vmax - vmin < 1e-6:
            return top + height / 2
        ratio = max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
        # 2px inset top and bottom so the line and dot never sit flush
        # against the edge.
        return top + (height - 4) - ratio * (height - 4) + 2

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        return Size(max(self.min_width, avail.width), self.height)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        width, height = r.width(), r.height()
        vmin, vmax = self._value_range()

        if self.show_grid:
            # Ceiling rule at the top of the plot area, so it reads as a
            # header divider rather than a mid-axis tick.
            grid = skia.Paint(AntiAlias=False, StrokeWidth=1.0)
            grid.setColor(theme.color(theme.BASE_MUTED, 0.35))
            grid.setStyle(skia.Paint.kStroke_Style)
            canvas.drawLine(r.left(), r.top() + 0.5,
                            r.right(), r.top() + 0.5, grid)

        n = len(self._samples)
        if n < 2:
            if n == 1 and self.show_dot:
                dot = skia.Paint(AntiAlias=True)
                dot.setColor(theme.color(self.color))
                canvas.drawCircle(
                    r.right() - 2,
                    self._y_for(self._samples[0], r.top(), height, vmin, vmax),
                    2.0, dot)
            return

        # Newest sample anchored to the right edge, so the plot scrolls
        # right-to-left as samples accrue.
        step = width / max(1, self.max_samples - 1)
        x0 = r.right() - (n - 1) * step
        points = [(x0 + i * step, self._y_for(v, r.top(), height, vmin, vmax))
                  for i, v in enumerate(self._samples)]

        if self.fill_alpha > 0:
            area = skia.Path()
            area.moveTo(x0, r.bottom())
            for x, y in points:
                area.lineTo(x, y)
            area.lineTo(r.right(), r.bottom())
            area.close()
            fill = skia.Paint(AntiAlias=True)
            fill.setColor(theme.color(self.color, self.fill_alpha))
            canvas.drawPath(area, fill)

        line = skia.Path()
        line.moveTo(*points[0])
        for x, y in points[1:]:
            line.lineTo(x, y)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setColor(theme.color(self.color, 0.95))
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(self.line_thick)
        stroke.setStrokeJoin(skia.Paint.kRound_Join)
        stroke.setStrokeCap(skia.Paint.kRound_Cap)
        canvas.drawPath(line, stroke)

        if self.show_dot:
            dot = skia.Paint(AntiAlias=True)
            dot.setColor(theme.color(self.color))
            canvas.drawCircle(r.right() - 2, points[-1][1], 2.2, dot)
