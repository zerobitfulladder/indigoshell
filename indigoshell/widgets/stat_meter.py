"""Big-value stat widget with underline meter.

Layout:

    <PERCENT_BIG_BOLD>  <label>
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Top row: bold value + small uppercase label (e.g. "CPU"). Underneath,
a thin horizontal underline in `dim_color` is overlaid left-to-right
by `bright_color` proportional to the value.
"""

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from .base import Widget, paint


class StatMeter(Widget):
    interval_ms = 500

    def __init__(
        self,
        label: str,
        source: Callable[[], float],
        value_format: str = "{:.0f}%",
        value_size_pt: int = 14,
        label_size_pt: int = 9,
        line_width: int = 70,
        line_thickness: int = 3,
        line_segments: int = 20,
        line_gap: int = 2,
        bright_color: str | None = None,
        dim_color: str | None = None,
        label_color: str | None = None,
        value_color: str | None = None,
        gradient: tuple[tuple[float, str], ...] | None = None,
        to_pct: Callable[[float], float] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.label_text = label
        self.source = source
        self.value_format = value_format
        self.value_size_pt = value_size_pt
        self.label_size_pt = label_size_pt
        self.line_width = line_width
        self.line_thickness = line_thickness
        self.bright_color = bright_color or theme.CYAN_BRIGHT
        self.dim_color = dim_color or theme.CYAN_DIM
        self.label_color = label_color or theme.CYAN_DIM
        self.value_color = value_color or theme.CYAN_BRIGHT
        self.gradient = gradient
        self.to_pct = to_pct or (lambda v: v)
        self.line_segments = max(1, line_segments)
        self.line_gap = max(0, line_gap)
        self._value: float = 0.0
        self._value_label: Gtk.Label | None = None
        self._line: Gtk.EventBox | None = None
        self._percent: float = 0.0

    def build_widget(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        vbox.set_valign(Gtk.Align.CENTER)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_halign(Gtk.Align.START)

        self._value_label = Gtk.Label()
        self._value_label.set_xalign(0.0)
        self._value_label.set_valign(Gtk.Align.BASELINE)
        row.pack_start(self._value_label, False, False, 0)

        text = Gtk.Label()
        text.set_markup(
            f"<span size='{self.label_size_pt * 1000}' "
            f"foreground='{self.label_color}'>{self.label_text}</span>"
        )
        text.set_valign(Gtk.Align.BASELINE)
        row.pack_start(text, False, False, 0)
        vbox.pack_start(row, False, False, 0)

        filler = Gtk.Box()
        filler.set_size_request(self.line_width, self.line_thickness)
        line = Gtk.EventBox()
        line.add(filler)
        line.set_visible_window(False)
        line.set_halign(Gtk.Align.START)
        line.connect_after("draw", self._draw_line)
        self._line = line
        vbox.pack_start(line, False, False, 0)

        self._render_value()
        return vbox

    def tick(self) -> bool:
        self._value = float(self.source())
        self._percent = max(0.0, min(100.0, float(self.to_pct(self._value))))
        self._render_value()
        if self._line is not None:
            self._line.queue_draw()
        return True

    def _current_bright(self) -> str:
        """Color used for the bright value text + fill ticks. Picks
        from the gradient by current meter ratio, if gradient is set."""
        if not self.gradient:
            return self.bright_color
        ratio = self._percent / 100.0
        chosen = self.gradient[0][1]
        for threshold, c in self.gradient:
            if ratio >= threshold:
                chosen = c
        return chosen

    def _render_value(self) -> None:
        if self._value_label is None:
            return
        text = self.value_format.format(self._value)
        color = self._current_bright() if self.gradient else self.value_color
        self._value_label.set_markup(
            f"<span weight='bold' size='{self.value_size_pt * 1000}' "
            f"foreground='{color}'>{text}</span>"
        )

    def _draw_line(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n = self.line_segments
        gap = self.line_gap
        tick_w = max(1.0, (width - gap * (n - 1)) / n)
        ratio = max(0.0, min(1.0, self._percent / 100.0))
        fill_end_x = ratio * width  # right edge of the bright portion
        bright = self._current_bright()
        for i in range(n):
            x = i * (tick_w + gap)
            # Tick is bright if its center is within the filled region.
            mid = x + tick_w / 2
            paint(cr, bright if mid <= fill_end_x else self.dim_color)
            cr.rectangle(x, 0, tick_w, height)
            cr.fill()
        return False
