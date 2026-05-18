"""HUD-style clock: date on the left, time on the right, with an
optional `extra_widget` (e.g. a BatteryMeter) packed underneath."""

import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from .base import Widget, make_label


class Clock(Widget):
    interval_ms = 1000

    def __init__(
        self,
        date_format: str = "%a %d %b",
        time_format: str = "%H:%M",
        time_size_pt: int = 18,
        date_size_pt: int = 11,
        width: int = 170,
        time_color: str | None = None,
        date_color: str | None = None,
        extra_widget: Widget | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.date_format = date_format
        self.time_format = time_format
        self.time_size_pt = time_size_pt
        self.date_size_pt = date_size_pt
        self.width = width
        self.time_color = time_color or theme.CYAN_BRIGHT
        self.date_color = date_color or theme.BASE_MUTED
        self.extra_widget = extra_widget
        self._date_label: Gtk.Label | None = None
        self._time_label: Gtk.Label | None = None

    def build_widget(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        vbox.set_valign(Gtk.Align.CENTER)

        # Date (left) + time (right)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_size_request(self.width, -1)

        self._date_label = make_label("", "date")
        self._date_label.set_xalign(0.0)
        self._date_label.set_valign(Gtk.Align.BASELINE)
        row.pack_start(self._date_label, False, False, 0)

        self._time_label = make_label("", "time")
        self._time_label.set_xalign(1.0)
        self._time_label.set_valign(Gtk.Align.BASELINE)
        row.pack_end(self._time_label, False, False, 0)
        vbox.pack_start(row, False, False, 0)

        if self.extra_widget is not None:
            child = self.extra_widget.build()
            vbox.pack_start(child, False, False, 0)

        return vbox

    def start(self) -> None:
        super().start()
        if self.extra_widget is not None:
            self.extra_widget.start()

    def stop(self) -> None:
        if self.extra_widget is not None:
            self.extra_widget.stop()
        super().stop()

    def walk(self):
        yield self
        if self.extra_widget is not None:
            yield from self.extra_widget.walk()

    def tick(self) -> bool:
        now = datetime.datetime.now()
        if self._date_label is not None:
            self._date_label.set_markup(
                f"<span size='{self.date_size_pt * 1000}' "
                f"foreground='{self.date_color}'>{now.strftime(self.date_format)}</span>"
            )
        if self._time_label is not None:
            self._time_label.set_markup(
                f"<span weight='bold' size='{self.time_size_pt * 1000}' "
                f"foreground='{self.time_color}'>{now.strftime(self.time_format)}</span>"
            )
        return True
