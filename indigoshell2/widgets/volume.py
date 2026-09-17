"""Volume — a vertical stack of cells lit bottom-up.

Event-driven: `pactl subscribe` reports changes and only then do we
re-query the sink, so there is no polling. Click toggles mute, scroll
adjusts. Bars flip red when muted.
"""

import logging
import re

import skia

from .. import theme
from ..services import proc
from .base import Size, Widget

log = logging.getLogger(__name__)

SINK = "@DEFAULT_SINK@"


class Volume(Widget):
    def __init__(self, *, step: int = 5, cells: int = 8, cell_thick: int = 3,
                 gap: int = 1, width: int = 18, cap: bool = False,
                 cap_color: str = theme.CYAN_BRIGHT, cap_overhang: int = 2,
                 **kwargs) -> None:
        kwargs.setdefault("on_left_click", self._toggle_mute)
        kwargs.setdefault("on_scroll_up", self._scroll_up)
        kwargs.setdefault("on_scroll_down", self._scroll_down)
        super().__init__(**kwargs)
        self.step = step
        self.cells = max(1, cells)
        self.cell_thick = max(1, cell_thick)
        self.gap = max(0, gap)
        self.w = max(4, width)
        # A 1px line one row above the topmost lit cell, reaching
        # `cap_overhang` past the cells on both sides — the cells are
        # inset by that much so the widget's width is unchanged.
        self.cap = cap
        self.cap_color = cap_color
        self.cap_overhang = max(0, cap_overhang)
        self._percent = 0.0
        self._muted = False
        self._sub: proc.Subscription | None = None

    def attach(self, window) -> None:
        super().attach(window)
        if self._sub is None:
            self._sub = proc.subscribe(
                ["pactl", "subscribe"], self._on_line)
            self._schedule_refresh()

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    def _on_line(self, line: str) -> None:
        if "sink" in line or "server" in line:
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        import asyncio
        try:
            asyncio.get_running_loop().create_task(self._refresh())
        except RuntimeError:
            pass

    async def _refresh(self) -> None:
        muted = "yes" in (await proc.run(
            ["pactl", "get-sink-mute", SINK])).lower()
        m = re.search(r"(\d+)%",
                      await proc.run(["pactl", "get-sink-volume", SINK]))
        vol = float(m.group(1)) if m else 0.0
        if (vol, muted) != (self._percent, self._muted):
            self._percent, self._muted = vol, muted
            self.invalidate()

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        h = self.cells * self.cell_thick + (self.cells - 1) * self.gap
        return Size(self.w, h)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        n = self.cells
        # Integral cell height, same reason as the horizontal meters: a
        # fractional height lands as alternating 3px/4px cells.
        stride = self.cell_thick + self.gap
        bottom = round(r.bottom())
        lit = theme.ERROR if self._muted else theme.VIOLET_BRIGHT
        dim = theme.MAGENTA_DIM if self._muted else theme.VIOLET_DIM
        # Muted lights every cell red rather than showing the level.
        lit_count = n if self._muted else int(self._percent / 100.0 * n)
        inset = self.cap_overhang if self.cap else 0
        cx, cw = round(r.left()) + inset, round(r.width()) - 2 * inset
        paint = skia.Paint(AntiAlias=False)
        for i in range(n):
            # i=0 is the bottom-most cell; the stack fills upward.
            y = bottom - (i + 1) * stride + self.gap
            paint.setColor(theme.color(lit if i < lit_count else dim))
            canvas.drawRect(
                skia.Rect.MakeXYWH(cx, y, cw, self.cell_thick), paint)
        if self.cap and not self._muted:
            y = max(round(r.top()), bottom - (lit_count + 1) * stride + self.gap)
            paint.setColor(theme.color(self.cap_color))
            canvas.drawRect(
                skia.Rect.MakeXYWH(round(r.left()), y, round(r.width()), 1),
                paint)

    # ── input ───────────────────────────────────────────────────────────
    def _toggle_mute(self, _w=None) -> None:
        proc.fire(["pactl", "set-sink-mute", SINK, "toggle"])

    def _scroll_up(self, _w=None) -> None:
        if self._muted:
            proc.fire(["pactl", "set-sink-mute", SINK, "0"])
            return
        proc.fire(["pactl", "set-sink-volume", SINK,
                   f"{min(100, int(self._percent) + self.step)}%"])

    def _scroll_down(self, _w=None) -> None:
        if self._muted:
            proc.fire(["pactl", "set-sink-mute", SINK, "0"])
            return
        proc.fire(["pactl", "set-sink-volume", SINK,
                   f"{max(0, int(self._percent) - self.step)}%"])
