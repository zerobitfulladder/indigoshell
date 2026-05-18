"""Cyberpunk-style identity tag: bold word framed by four small corner
brackets, with the word color slowly pulsing between two shades."""

import math

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from .base import Widget, make_label, paint
from .stdout_text import _lerp_hex


class SystagBlock(Widget):
    interval_ms: int | None = None

    def __init__(
        self,
        text: str = "INDIGO",
        size_pt: int = 13,
        padding: int = 4,
        corner_arm: int = 4,
        corner_thick: int = 1,
        bracket_color: str | None = None,
        pulse_colors: tuple[str, str] | None = None,
        pulse_period_ms: int = 33,  # ~30 fps
        cycle_s: float = 2.6,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.text = text
        self.size_pt = size_pt
        self.padding = padding
        self.corner_arm = corner_arm
        self.corner_thick = corner_thick
        self.bracket_color = bracket_color or theme.MAGENTA_DIM
        self.pulse_colors = pulse_colors or (theme.MAGENTA_MID, theme.MAGENTA_BLOOM)
        self.pulse_period_ms = pulse_period_ms
        self.cycle_s = cycle_s
        self._label: Gtk.Label | None = None
        self._frame: Gtk.Box | None = None
        self._phase: float = 0.0
        self._timer: int | None = None

    def build_widget(self):
        # Frame: a horizontal Box that contains the label, padded; we
        # paint the corner brackets in a connect_after draw on this box.
        frame = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        frame.set_margin_top(0)
        frame.set_margin_bottom(0)

        self._label = make_label("", "tag")
        self._label.set_margin_top(self.padding)
        self._label.set_margin_bottom(self.padding)
        self._label.set_margin_start(self.padding)
        self._label.set_margin_end(self.padding)
        frame.pack_start(self._label, False, False, 0)

        frame.connect_after("draw", self._draw_corners)
        self._frame = frame
        self._render_label()
        return frame

    def start(self) -> None:
        super().start()
        if self._timer is None:
            self._timer = GLib.timeout_add(self.pulse_period_ms, self._tick_pulse)

    def stop(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        super().stop()

    # ── pulse ────────────────────────────────────────────────────────
    def _tick_pulse(self) -> bool:
        step = (self.pulse_period_ms / 1000.0) / self.cycle_s * 2 * math.pi
        self._phase = (self._phase + step) % (2 * math.pi)
        self._render_label()
        return True

    def _render_label(self) -> None:
        if self._label is None:
            return
        t = (math.sin(self._phase) + 1) / 2
        color = _lerp_hex(self.pulse_colors[0], self.pulse_colors[1], t)
        self._label.set_markup(
            f"<span weight='bold' size='{self.size_pt * 1000}' "
            f"foreground='{color}'>{self.text}</span>"
        )

    # ── corner brackets ──────────────────────────────────────────────
    def _draw_corners(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        arm = self.corner_arm
        t = self.corner_thick
        paint(cr, self.bracket_color)
        # top-left
        cr.rectangle(0, 0, arm, t)
        cr.rectangle(0, 0, t, arm)
        # top-right
        cr.rectangle(width - arm, 0, arm, t)
        cr.rectangle(width - t, 0, t, arm)
        # bottom-left
        cr.rectangle(0, height - t, arm, t)
        cr.rectangle(0, height - arm, t, arm)
        # bottom-right
        cr.rectangle(width - arm, height - t, arm, t)
        cr.rectangle(width - t, height - arm, t, arm)
        cr.fill()
        return False
