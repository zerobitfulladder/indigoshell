"""Segmented meters — the shared cyberpunk idiom.

All three draw N discrete cells with a gap, lit left-to-right (or
bottom-up) in proportion to a value. Ported cell-for-cell from v1's
bar_meter.py, stat_meter.py and battery_meter.py; only the drawing calls
change (cairo -> Skia) and the timers fold into the frame clock.
"""

import math
import os
from typing import Callable, Union

import psutil
import skia

from .. import text, theme
from .base import Size, Widget
from .label import Label
from .layout import Align, Column, Row, Spacer

ColorSpec = Union[str, Callable[[float], str]]


def tick_count(width: float, tick: int, gap: int) -> int:
    """How many whole ticks fit across `width`."""
    return max(1, int((width + gap) // (tick + gap)))


def _ticks(canvas: skia.Canvas, x: float, y: float, w: float, h: float,
           *, tick: int, gap: int, pct: float, lit: str, dim: str,
           dim_alpha: float | None = None) -> None:
    """Fixed-width ticks across `w`, lit where a tick's midpoint falls
    inside the filled fraction.

    The geometry is integral on purpose. v1 divided the span by a segment
    *count*, which gives a fractional tick width (70px / 20 ticks with
    2px gaps is 1.6) — on a pixel grid that lands as an uneven 1,2,2,1,2
    pattern, and antialiasing only blurs the edges without evening out
    the solid cores. Fixing the tick width and deriving the count instead
    makes every tick identical and crisp, and fills the span exactly.
    """
    stride = tick + gap
    n = tick_count(w, tick, gap)
    span = n * stride - gap
    fill_end = max(0.0, min(1.0, pct / 100.0)) * span
    # Two paths, two draws — not one drawRect per tick. A bar of 18 ticks
    # was 18 Python->Skia calls plus 18 colour lookups and 18 Rect
    # allocations every frame; across the whole bar that was 121 draws a
    # frame, and `_ticks` alone was the largest single item in the
    # profile. The geometry is identical, it is just submitted at once.
    on, off = skia.Path(), skia.Path()
    ox, oy, oh = round(x), round(y), round(h)
    for i in range(n):
        cx = i * stride
        target = on if cx + tick / 2 <= fill_end else off
        target.addRect(skia.Rect.MakeXYWH(ox + cx, oy, tick, oh))
    paint = skia.Paint(AntiAlias=False)
    if not off.isEmpty():
        paint.setColor(theme.color(dim, dim_alpha))
        canvas.drawPath(off, paint)
    if not on.isEmpty():
        paint.setColor(theme.color(lit))
        canvas.drawPath(on, paint)


class BarMeter(Widget):
    """Standalone segmented progress bar. Data-only — call set_value()."""

    def __init__(self, *, color: ColorSpec = theme.CYAN_BRIGHT,
                 dim_color: str = theme.BASE_GUTTER,
                 tick: int = 4, gap: int = 2, thick: int = 8,
                 min_width: int = 220, **kwargs) -> None:
        super().__init__(**kwargs)
        self._color = color
        self._dim = dim_color
        self._tick = tick
        self._gap = gap
        self._h = thick
        self._min_width = min_width
        self._pct = 0.0

    def set_value(self, pct: float) -> None:
        pct = max(0.0, min(100.0, float(pct)))
        if pct != self._pct:
            self._pct = pct
            self.invalidate()

    def measure(self, avail: Size) -> Size:
        # The requested width, not the available one. v1's GTK version set
        # hexpand and grew to fill; inside a panel row that overflows the
        # panel. Set flex=1 on the instance to opt back into filling.
        return Size(self._min_width, self._h + 4)

    def paint(self, canvas: skia.Canvas) -> None:
        color = self._color(self._pct) if callable(self._color) else self._color
        r = self.rect
        y = r.top() + (r.height() - self._h) / 2
        _ticks(canvas, r.left(), y, r.width(), self._h,
               tick=self._tick, gap=self._gap, pct=self._pct,
               lit=color, dim=self._dim, dim_alpha=0.6)


class StatMeter(Widget):
    """Big value + small label, over a segmented underline.

        42%  CPU
        ▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯
    """

    animation_fps = 2      # v1 polled at 500ms

    def __init__(self, label: str, source: Callable[[], float], *,
                 value_format: str = "{:.0f}%",
                 value_size: float = 14, label_size: float = 9,   # points
                 line_width: int = 70, line_thickness: int = 3,
                 line_tick: int = 2, line_gap: int = 2,
                 bright_color: str | None = None,
                 dim_color: str | None = None,
                 label_color: str | None = None,
                 value_color: str | None = None,
                 gradient: tuple[tuple[float, str], ...] | None = None,
                 to_pct: Callable[[float], float] | None = None,
                 style: str = "underline",
                 peak_hold: bool = True, peak_decay: float = 1.5,
                 peak_color: str = theme.YELLOW_BRIGHT,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = source
        # "underline": value + label over a segmented line, as drawn in
        # the docstring. "bracket": the label as a small tracked key at
        # the left and the value at the right, over a corner-bracketed
        # bar with a peak marker — the battery meter's frame idiom.
        self.style = style
        self.label_text = label
        self.label_size = label_size
        self.label_color = label_color or theme.CYAN_DIM
        # Peak hold: the highest reading seen recently, falling by
        # `peak_decay` percent per sample (samples are 500ms apart).
        self.peak_hold = peak_hold
        self.peak_decay = peak_decay
        self.peak_color = peak_color
        self._peak_pct = 0.0
        self.value_format = value_format
        self.value_size = value_size
        self.line_width = line_width
        self.line_thickness = line_thickness
        self.line_tick = max(1, line_tick)
        self.line_gap = max(0, line_gap)
        self.bright_color = bright_color or theme.CYAN_BRIGHT
        self.dim_color = dim_color or theme.CYAN_DIM
        self.value_color = value_color or theme.CYAN_BRIGHT
        self.gradient = gradient
        self.to_pct = to_pct or (lambda v: v)
        self._value = 0.0
        self._percent = 0.0

        self._value_label = Label("", size=text.pt(value_size), bold=True,
                                  color=self._current_bright)
        self._row = Row(
            [self._value_label,
             Label(label, size=text.pt(label_size),
                   color=label_color or theme.CYAN_DIM)],
            spacing=4, align=Align.BASELINE)
        # Prime before the first measure: sizing against an empty string
        # would allocate a slot too narrow for any real value.
        self._sample()

    def children(self):
        return (self._row,)

    def _current_bright(self) -> str:
        """Colour of the value text and lit ticks. With a gradient set, it
        steps by the meter ratio; otherwise it's fixed."""
        if not self.gradient:
            return self.value_color
        ratio = self._percent / 100.0
        chosen = self.gradient[0][1]
        for threshold, c in self.gradient:
            if ratio >= threshold:
                chosen = c
        return chosen

    def _sample(self) -> None:
        self._value = float(self.source())
        self._percent = max(0.0, min(100.0, float(self.to_pct(self._value))))
        self._peak_pct = max(self._percent, self._peak_pct - self.peak_decay)
        self._value_label.set_value(self.value_format.format(self._value))

    def animate(self, t: float) -> None:
        # Called at `animation_fps` — the window schedules each widget on
        # its own rate, so this does not have to gate itself.
        self._sample()

    def _bracket_row_h(self) -> float:
        return text.line_height(text.pt(self.value_size), bold=True)

    def measure(self, avail: Size) -> Size:
        if self.style == "bracket":
            return Size(self.line_width,
                        self._bracket_row_h() + 2 + self.line_thickness + 6)
        row = self._row.measure(avail)
        return Size(max(row.width, self.line_width),
                    row.height + 1 + self.line_thickness)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.style == "bracket":
            return
        row = self._row.measure(Size(rect.width(), rect.height()))
        self._row.arrange(skia.Rect.MakeXYWH(
            rect.left(), rect.top(), rect.width(), row.height))

    def paint(self, canvas: skia.Canvas) -> None:
        if self.style == "bracket":
            self._paint_bracket(canvas)
            return
        self._row.paint(canvas)
        y = self.rect.bottom() - self.line_thickness
        _ticks(canvas, self.rect.left(), y, self.line_width,
               self.line_thickness, tick=self.line_tick, gap=self.line_gap,
               pct=self._percent, lit=self._current_bright(),
               dim=self.dim_color)

    def _paint_bracket(self, canvas: skia.Canvas) -> None:
        r = self.rect
        x, y, w = r.left(), r.top(), r.width()
        lsize, vsize = text.pt(self.label_size), text.pt(self.value_size)
        bright = self._current_bright()
        value = self.value_format.format(self._value)
        text.draw(canvas, self.label_text, x + 1,
                  y - text.font(lsize).getMetrics().fAscent, lsize,
                  theme.color(self.label_color), False, 1.5)
        text.draw(canvas, value, x + w - text.measure(value, vsize, True),
                  y - text.font(vsize, True).getMetrics().fAscent, vsize,
                  theme.color(bright), True)
        bh = self.line_thickness
        by = y + self._bracket_row_h() + 2
        fh = bh + 6
        # Corner brackets, one path.
        arms = skia.Path()
        a, t = 4, 1
        for rx, ry, rw, rh in (
            (0, 0, a, t), (0, 0, t, a),
            (w - a, 0, a, t), (w - t, 0, t, a),
            (0, fh - t, a, t), (0, fh - a, t, a),
            (w - a, fh - t, a, t), (w - t, fh - a, t, a),
        ):
            arms.addRect(skia.Rect.MakeXYWH(x + rx, by + ry, rw, rh))
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(self.dim_color))
        canvas.drawPath(arms, paint)
        _ticks(canvas, x + 4, by + 3, w - 8, bh, tick=self.line_tick,
               gap=self.line_gap, pct=self._percent, lit=bright,
               dim=self.dim_color, dim_alpha=0.6)
        if self.peak_hold:
            px = x + 4 + (w - 8) * self._peak_pct / 100.0
            paint.setColor(theme.color(self.peak_color))
            canvas.drawRect(skia.Rect.MakeXYWH(round(px), by + 1, 1, bh + 4),
                            paint)


def _ac_online_from_sysfs() -> bool | None:
    """Fallback for psutil reporting power_plugged=None on some laptops."""
    root = "/sys/class/power_supply"
    try:
        names = os.listdir(root)
    except OSError:
        return None
    found = False
    for name in names:
        try:
            with open(f"{root}/{name}/type") as f:
                if f.read().strip() != "Mains":
                    continue
            with open(f"{root}/{name}/online") as f:
                online = f.read().strip()
        except OSError:
            continue
        found = True
        if online == "1":
            return True
    return False if found else None


class BatteryMeter(Widget):
    """`[||||  ]` silhouette: corner brackets plus vertical cells.

    The state table and every animation mode are v1's verbatim. What
    changed is the clock: v1 ran a GLib timer per mode at 33-150ms and
    re-created it whenever the mode changed. Here the widget runs on the
    window's 30fps frame clock and advances its own accumulator by the
    mode's interval — same motion, one timer for the whole window.
    """

    @property
    def animation_fps(self) -> int:
        """Follow the current mode's step interval.

        The state table already says how often this changes: 110ms while
        discharging, 70 while charging, 33 for the full-charge shimmer,
        and never when no battery is present. Declaring a flat 30 asked
        the window for three times the frames the sweep could use, and
        every one of them repainted the whole bar.
        """
        ms = self._state_config()["ms"]
        if ms <= 0:
            return 0
        return min(30, math.ceil(1000.0 / ms))

    def __init__(self, cells: int = 16, cell_thick: int = 2, gap: int = 2,
                 height: int = 22, corner_arm: int = 4, corner_thick: int = 1,
                 brackets: bool = True, fill: bool = False,
                 pad_x: int = 5, pad_y: int = 3,
                 fake_state: tuple[float, bool] | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.cells = max(1, cells)
        self.cell_thick = max(1, cell_thick)
        self.gap = max(0, gap)
        self.h = max(4, height)
        self.corner_arm = max(2, corner_arm)
        self.corner_thick = max(1, corner_thick)
        self.brackets = brackets
        # fill=True: claim no width of its own and derive the cell count
        # from whatever the container hands it, so the meter spans its
        # sibling instead of stopping at a fixed cell count.
        self.fill = fill
        self.pad_x = max(2, pad_x)
        self.pad_y = max(0, pad_y)
        self.fake_state = fake_state

        self._percent = 0.0
        self._charging = False
        self._present = True
        self._sweep_pos = 0
        self._blink_on = True
        self._phase = 0.0
        self._acc = 0.0
        self._poll_acc = 0.0
        self._mode = "static"
        self._dir = 0
        self._poll()

    # ── state → behaviour table (verbatim from v1) ──────────────────────
    def _state_config(self) -> dict:
        if not self._present:
            return dict(mode="static", sweep=theme.FG_MUTED, lit=theme.FG_MUTED,
                        empty=theme.FG_MUTED, bracket=theme.FG_MUTED,
                        dir=0, ms=0)
        if self._charging and self._percent >= 99:
            return dict(mode="shimmer", sweep=theme.LIME_BRIGHT,
                        lit=theme.LIME_DIM, empty=theme.LIME_DIM,
                        bracket=theme.LIME_DIM, dir=0, ms=33)
        if self._charging:
            return dict(mode="sweep", sweep=theme.CYAN_BRIGHT,
                        lit=theme.CYAN_DIM, empty=theme.MAGENTA_DIM,
                        bracket=theme.CYAN_DIM, dir=1, ms=70)
        if self._percent < 20:
            return dict(mode="blink", sweep=theme.MAGENTA_BRIGHT,
                        lit=theme.MAGENTA_BRIGHT, empty=theme.MAGENTA_DIM,
                        bracket=theme.MAGENTA_DIM, dir=0, ms=150)
        if self._percent < 50:
            return dict(mode="sweep", sweep=theme.YELLOW_BRIGHT,
                        lit=theme.YELLOW_DIM, empty=theme.MAGENTA_DIM,
                        bracket=theme.YELLOW_DIM, dir=-1, ms=90)
        return dict(mode="sweep", sweep=theme.CYAN_BRIGHT, lit=theme.CYAN_DIM,
                    empty=theme.MAGENTA_DIM, bracket=theme.CYAN_DIM,
                    dir=-1, ms=110)

    def _poll(self) -> None:
        if self.fake_state is not None:
            self._present = True
            self._percent, self._charging = self.fake_state
            return
        b = psutil.sensors_battery()
        if b is None:
            self._present = False
            return
        self._present = True
        self._percent = float(b.percent)
        plugged = b.power_plugged
        if plugged is None:
            plugged = _ac_online_from_sysfs()
        self._charging = bool(plugged)

    @property
    def _lit_count(self) -> int:
        return int(self._percent / 100.0 * self._cells_drawn)

    @property
    def _cells_drawn(self) -> int:
        if self.fill and self.rect.width() > 0:
            inset = self.pad_x if self.brackets else 0
            return tick_count(self.rect.width() - 2 * inset,
                              self.cell_thick, self.gap)
        return self.cells

    def animate(self, t: float) -> None:
        dt_ms = self.tick_dt(t) * 1000.0

        self._poll_acc += dt_ms
        if self._poll_acc >= 5000:      # v1 polled battery every 5s
            self._poll_acc = 0.0
            self._poll()

        cfg = self._state_config()
        mode, direction = cfg["mode"], cfg["dir"]
        if mode == "sweep" and (self._mode != "sweep" or self._dir != direction):
            # Entering a new sweep direction: start from the end that
            # makes the motion read as filling (charging) or emptying.
            self._sweep_pos = 0 if direction > 0 else max(1, self._lit_count)
        self._mode, self._dir = mode, direction
        if cfg["ms"] <= 0:
            return

        self._acc += dt_ms
        while self._acc >= cfg["ms"]:
            self._acc -= cfg["ms"]
            if mode == "sweep":
                nxt = self._sweep_pos + direction
                lit = max(1, self._lit_count)
                if nxt < 0:
                    nxt = lit
                elif nxt > lit:
                    nxt = 0
                self._sweep_pos = nxt
            elif mode == "blink":
                self._blink_on = not self._blink_on
            elif mode == "shimmer":
                self._phase = (self._phase + 0.08) % (2 * math.pi)

    def _cell_color(self, i: int, cfg: dict) -> str:
        if i >= self._lit_count:
            return cfg["empty"]
        if self._mode == "sweep":
            return cfg["sweep"] if i < self._sweep_pos else cfg["lit"]
        if self._mode == "blink":
            return cfg["sweep"] if self._blink_on else cfg["empty"]
        if self._mode == "shimmer":
            t = (math.sin(self._phase + i * 0.45) + 1) / 2
            return theme.lerp(cfg["lit"], cfg["sweep"], t)
        return cfg["lit"]

    def measure(self, avail: Size) -> Size:
        if self.fill:
            return Size(0.0, self.h)
        cells_w = (self.cells * self.cell_thick
                   + (self.cells - 1) * self.gap)
        pad = 2 * self.pad_x if self.brackets else 0
        return Size(cells_w + pad, self.h)

    def paint(self, canvas: skia.Canvas) -> None:
        cfg = self._state_config()
        r = self.rect
        ox, oy = r.left(), r.top()
        width, height = r.width(), r.height()
        paint = skia.Paint(AntiAlias=False)

        if self.brackets:
            # Four corner L-brackets, as one path — eight arms, one draw.
            arms = skia.Path()
            a, t = self.corner_arm, self.corner_thick
            for rx, ry, rw, rh in (
                (0, 0, a, t), (0, 0, t, a),
                (width - a, 0, a, t), (width - t, 0, t, a),
                (0, height - t, a, t), (0, height - a, t, a),
                (width - a, height - t, a, t), (width - t, height - a, t, a),
            ):
                arms.addRect(skia.Rect.MakeXYWH(ox + rx, oy + ry, rw, rh))
            paint.setColor(theme.color(cfg["bracket"]))
            canvas.drawPath(arms, paint)

        inner_top = oy + self.pad_y
        inner_h = max(1.0, height - 2 * self.pad_y)
        inset = self.pad_x if self.brackets else 0
        x = ox + inset
        cells = self.cells
        if self.fill:
            cells = tick_count(width - 2 * inset, self.cell_thick, self.gap)
        # Grouped by colour so each distinct colour is one draw. Sweep
        # and blink use two or three across all 44 cells; only shimmer,
        # which lerps a continuous ramp, ends up with one group per cell —
        # and that mode only runs while charging above 99%.
        groups: dict[str, skia.Path] = {}
        for i in range(cells):
            color = self._cell_color(i, cfg)
            path = groups.get(color)
            if path is None:
                path = groups[color] = skia.Path()
            path.addRect(
                skia.Rect.MakeXYWH(x, inner_top, self.cell_thick, inner_h))
            x += self.cell_thick + self.gap
        for color, path in groups.items():
            paint.setColor(theme.color(color))
            canvas.drawPath(path, paint)
