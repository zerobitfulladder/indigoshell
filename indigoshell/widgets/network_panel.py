"""Multi-interface network panel for popups.

Lists every UP interface with a (non-loopback) IPv4 address and its
up/down rates. Used as the content of the wifi click popup.
"""

import socket
import time

import psutil

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from ..style import Style
from .base import Widget, make_label


def _fmt(bps: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.0f}{unit}" if unit == "B" else f"{bps:.1f}{unit}"
        bps /= 1024
    return f"{bps:.1f}TB"


class NetworkPanel(Widget):
    interval_ms = 1000

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._grid: Gtk.Grid | None = None
        # Per-iface (bytes_sent, bytes_recv, monotonic_t) for rate calc.
        self._counters: dict[str, tuple[int, int, float]] = {}

    def build_widget(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        grid = Gtk.Grid()
        grid.set_column_spacing(18)
        grid.set_row_spacing(4)
        self._grid = grid
        outer.pack_start(grid, False, False, 0)
        return outer

    def default_css(self) -> str:
        return (
            f"#{self.name} label.iface  {{ color: {theme.CYAN_BRIGHT}; font-weight: bold; }}\n"
            f"#{self.name} label.ip     {{ color: {theme.FG};        font-family: monospace; }}\n"
            f"#{self.name} label.up     {{ color: {theme.YELLOW_BRIGHT}; font-family: monospace; }}\n"
            f"#{self.name} label.dn     {{ color: {theme.MAGENTA_BRIGHT}; font-family: monospace; }}\n"
            f"#{self.name} label.empty  {{ color: {theme.BASE_MUTED}; font-style: italic; }}\n"
        )

    def tick(self) -> bool:
        if self._grid is None:
            return True
        rows = self._gather()
        self._render(rows)
        return True

    def _gather(self) -> list[tuple[str, str, float, float]]:
        now = time.monotonic()
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)
        out: list[tuple[str, str, float, float]] = []
        for iface, addr_list in addrs.items():
            if iface == "lo":
                continue
            st = stats.get(iface)
            if not st or not st.isup:
                continue
            ip = None
            for a in addr_list:
                if a.family == socket.AF_INET:
                    ip = a.address
                    break
            if not ip:
                continue
            c = counters.get(iface)
            up = dn = 0.0
            if c is not None:
                prev = self._counters.get(iface)
                if prev is not None:
                    ls, lr, lt = prev
                    dt = now - lt
                    if dt > 0:
                        up = max(0.0, (c.bytes_sent - ls) / dt)
                        dn = max(0.0, (c.bytes_recv - lr) / dt)
                self._counters[iface] = (c.bytes_sent, c.bytes_recv, now)
            out.append((iface, ip, up, dn))
        return out

    def _render(self, rows) -> None:
        assert self._grid is not None
        for child in self._grid.get_children():
            self._grid.remove(child)
        if not rows:
            empty = make_label("no active interface", "empty")
            self._grid.attach(empty, 0, 0, 6, 1)
            self._grid.show_all()
            return
        rate_chars = 9  # e.g. "999.9kB/s"
        for row, (iface, ip, up, dn) in enumerate(rows):
            self._grid.attach(make_label(iface, "iface"), 0, row, 1, 1)
            self._grid.attach(make_label(ip, "ip"), 1, row, 1, 1)

            up_arrow = make_label("↑", "up")
            up_arrow.set_xalign(0.5)
            self._grid.attach(up_arrow, 2, row, 1, 1)

            up_val = make_label(f"{_fmt(up)}/s", "up")
            up_val.set_xalign(1.0)
            up_val.set_width_chars(rate_chars)
            self._grid.attach(up_val, 3, row, 1, 1)

            dn_arrow = make_label("↓", "dn")
            dn_arrow.set_xalign(0.5)
            self._grid.attach(dn_arrow, 4, row, 1, 1)

            dn_val = make_label(f"{_fmt(dn)}/s", "dn")
            dn_val.set_xalign(1.0)
            dn_val.set_width_chars(rate_chars)
            self._grid.attach(dn_val, 5, row, 1, 1)
        self._grid.show_all()
