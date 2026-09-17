"""Workspace indicators driven by EWMH root-window properties.

Each workspace is a stack of four bars; the bottom N light up for N
windows on that desktop (capped at four). The current desktop uses a
different pair of colours, and a workspace holding a window with
_NET_WM_STATE_DEMANDS_ATTENTION blinks in a ring pattern.

Event-driven, no polling: PropertyNotify on the root covers
_NET_CURRENT_DESKTOP / _NET_NUMBER_OF_DESKTOPS / _NET_CLIENT_LIST, and
each client is watched for _NET_WM_DESKTOP and _NET_WM_STATE.

This shares the daemon's one xcffib connection rather than opening its
own, so there is no second event queue that could buffer events during a
reply round-trip, and no polling tick to drain it.
"""

import logging

import skia

from .. import text as textmod, theme
from .base import Size, Widget

log = logging.getLogger(__name__)

BARS = 4
GAP_RATIO = 0.35
# The hex style: `01 02 [03] 04`, a rail underneath with a cursor segment
# under the current desktop, and a pip per window above an occupied one.
# Everything is laid out from `hex_size`: pips at the top, digits on a
# baseline one px below their size, the rail 5px under that.


class Workspaces(Widget):
    @property
    def animation_fps(self) -> int:
        """Only while a workspace is urgent, for the ring blink —
        everything else here is event-driven. This is what the flat 30
        was always documented as meaning; now it is what it does.

        The transition into urgency goes through `_apply`, whose
        `invalidate` restarts the window's clock.
        """
        return 30 if self._any_urgent else 0

    def __init__(self, *, size: int = 22, spacing: int = 1,
                 style: str = "bars", hex_size: float = 12,
                 hex_gap: int = 10, **kwargs) -> None:
        kwargs.setdefault("on_scroll_up", lambda _w: self._step(-1))
        kwargs.setdefault("on_scroll_down", lambda _w: self._step(1))
        super().__init__(**kwargs)
        self.size = size
        self.spacing = spacing
        # "bars": the four-bar stack per desktop. "hex": a numbered
        # readout — see HEX_H.
        self.style = style
        self.hex_size = hex_size
        self.hex_gap = hex_gap

        self._count = 0
        self._current = 0
        self._per_desktop: list[int] = []
        self._urgent: list[bool] = []
        self._pending: int | None = None

        self._clients_dirty = True
        self._desktop_cache: dict[int, int] = {}
        self._urgent_cache: dict[int, bool] = {}
        self._watched: set[int] = set()

        self._ring_idx = 0
        self._ring_visible = True
        self._ring_acc = 0.0
        self._any_urgent = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        display = window.display
        display.add_root_listener(self._on_root_property)
        self._refresh()

    def _on_root_property(self, ev) -> None:
        display = self.window.display
        name = None
        try:
            name = display.conn.core.GetAtomName(ev.atom).reply().name.to_string()
        except Exception:
            return
        if name in ("_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS"):
            self._refresh()
        elif name == "_NET_CLIENT_LIST":
            self._clients_dirty = True
            self._refresh()

    def _on_client_property(self, ev) -> None:
        """A watched client changed desktop or urgency."""
        self._desktop_cache.pop(ev.window, None)
        self._urgent_cache.pop(ev.window, None)
        self._clients_dirty = True
        self._refresh()

    # ── state ───────────────────────────────────────────────────────────
    def _refresh(self) -> None:
        if self.window is None:
            return
        d = self.window.display
        root = d.root
        count = d.get_int(root, "_NET_NUMBER_OF_DESKTOPS") or 0
        current = d.get_int(root, "_NET_CURRENT_DESKTOP") or 0
        clients = d.get_ints(root, "_NET_CLIENT_LIST", 512)

        if current == self._pending:
            self._pending = None

        if self._clients_dirty:
            desktops: dict[int, int] = {}
            urgents: dict[int, bool] = {}
            demands = d.atom("_NET_WM_STATE_DEMANDS_ATTENTION")
            for xid in clients:
                if xid not in self._watched:
                    d.watch_window_properties(xid)
                    d.on(xid, self._on_client_property)
                    self._watched.add(xid)
                desk = self._desktop_cache.get(xid)
                if desk is None:
                    desk = d.get_int(xid, "_NET_WM_DESKTOP")
                if desk is not None:
                    desktops[xid] = desk
                urgent = self._urgent_cache.get(xid)
                if urgent is None:
                    urgent = demands in d.get_ints(xid, "_NET_WM_STATE", 32)
                urgents[xid] = urgent
            self._desktop_cache, self._urgent_cache = desktops, urgents
            for stale in self._watched - set(clients):
                d.off(stale)
            self._watched &= set(clients)
            self._clients_dirty = False

        per = [0] * count
        urgent = [False] * count
        for xid, desk in self._desktop_cache.items():
            if 0 <= desk < count:
                per[desk] += 1
                if self._urgent_cache.get(xid):
                    urgent[desk] = True

        changed = (count, current, per, urgent) != (
            self._count, self._current, self._per_desktop, self._urgent)
        self._count, self._current = count, current
        self._per_desktop, self._urgent = per, urgent

        any_urgent = any(urgent)
        if any_urgent and not self._any_urgent:
            self._ring_idx, self._ring_acc = 0, 0.0
            pattern = theme.WORKSPACE_URGENT_RING
            self._ring_visible = pattern[0][1] if pattern else True
        elif not any_urgent and self._any_urgent:
            self._ring_visible = True
        self._any_urgent = any_urgent

        if changed:
            self.invalidate(layout=True)

    # ── urgent ring blink ───────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt_ms = self.tick_dt(t) * 1000.0
        pattern = theme.WORKSPACE_URGENT_RING
        if not self._any_urgent or not pattern:
            return
        self._ring_acc += dt_ms
        ms, _visible = pattern[self._ring_idx]
        while self._ring_acc >= ms:
            self._ring_acc -= ms
            self._ring_idx = (self._ring_idx + 1) % len(pattern)
            ms, self._ring_visible = pattern[self._ring_idx]

    # ── layout / paint ──────────────────────────────────────────────────
    def _hex_cell(self) -> tuple[float, float]:
        """(digits width, bracket width) at the hex style's size."""
        return (textmod.measure("00", self.hex_size, True),
                textmod.measure("[", self.hex_size))

    def _stride(self) -> float:
        """Advance from one desktop's cell to the next, in px."""
        if self.style == "hex":
            cellw, bw = self._hex_cell()
            return cellw + 2 * bw + self.hex_gap
        return self.size + self.spacing

    def measure(self, avail: Size) -> Size:
        n = max(0, self._count)
        if n == 0:
            return Size(0, self.size)
        if self.style == "hex":
            cellw, bw = self._hex_cell()
            return Size(n * (cellw + 2 * bw) + (n - 1) * self.hex_gap,
                        self.hex_size + 9)
        return Size(n * self.size + (n - 1) * self.spacing, self.size)

    def _colors(self, index: int) -> tuple[str, str]:
        if self._urgent[index] and self._ring_visible:
            return theme.WORKSPACE_URGENT_FG, theme.WORKSPACE_URGENT_FG
        if index == self._current:
            return theme.CYAN_MID, theme.MAGENTA_MID
        return theme.WORKSPACE_OCCUPIED_FG, theme.WORKSPACE_EMPTY_FG

    def paint(self, canvas: skia.Canvas) -> None:
        if self.style == "hex":
            self._paint_hex(canvas)
        else:
            self._paint_bars(canvas)

    def _paint_hex(self, canvas: skia.Canvas) -> None:
        r = self.rect
        size = self.hex_size
        cellw, bw = self._hex_cell()
        x0, y = r.left(), r.top()
        base = y + size + 1
        rail = y + size + 6
        stride = self._stride()
        paint = skia.Paint(AntiAlias=False)
        cyan = theme.color(theme.CYAN_BRIGHT)
        for i in range(self._count):
            cx = x0 + i * stride
            cur = i == self._current
            occ = self._per_desktop[i] > 0
            if self._urgent[i] and self._ring_visible:
                number, bold = theme.WORKSPACE_URGENT_FG, True
            elif cur:
                number, bold = theme.WORKSPACE_CURRENT_FG, True
            elif occ:
                number, bold = theme.WORKSPACE_OCCUPIED_FG, True
            else:
                number, bold = theme.WORKSPACE_EMPTY_FG, False
            if cur:
                textmod.draw(canvas, "[", cx, base, size, cyan)
                textmod.draw(canvas, "]", cx + bw + cellw, base, size, cyan)
            textmod.draw(canvas, f"{i + 1:02d}", cx + bw, base, size,
                         theme.color(number), bold)
            if occ and not cur:
                paint.setColor(theme.color(theme.WORKSPACE_OCCUPIED_FG))
                for k in range(min(self._per_desktop[i], BARS)):
                    canvas.drawRect(
                        skia.Rect.MakeXYWH(cx + bw + k * 4, y, 2, 2), paint)
        total = self._count * (cellw + 2 * bw) + (self._count - 1) * self.hex_gap
        paint.setColor(theme.color(theme.WORKSPACE_EMPTY_FG))
        canvas.drawRect(skia.Rect.MakeXYWH(x0, rail, total, 1), paint)
        if 0 <= self._current < self._count:
            paint.setColor(cyan)
            canvas.drawRect(skia.Rect.MakeXYWH(
                x0 + self._current * stride, rail - 1, cellw + 2 * bw, 3), paint)

    def _paint_bars(self, canvas: skia.Canvas) -> None:
        r = self.rect
        h = r.height()
        # Four bars plus three gaps, sized as a fraction of the cell so the
        # stack scales with the bar height.
        bar_h = max(2.0, h / (BARS + (BARS - 1) * GAP_RATIO))
        gap = max(1.0, bar_h * GAP_RATIO)
        total = bar_h * BARS + gap * (BARS - 1)
        bar_w = self.size * 0.75
        paint = skia.Paint(AntiAlias=False)

        for i in range(self._count):
            cell_x = r.left() + i * (self.size + self.spacing)
            x = cell_x + (self.size - bar_w) / 2
            y0 = r.top() + (h - total) / 2
            occupied, empty = self._colors(i)
            lit = min(self._per_desktop[i], BARS)
            for b in range(BARS):
                # b=0 is the bottom bar; the stack fills upward.
                y = y0 + (BARS - 1 - b) * (bar_h + gap)
                paint.setColor(theme.color(occupied if b < lit else empty))
                canvas.drawRect(skia.Rect.MakeXYWH(x, y, bar_w, bar_h), paint)

    # ── input ───────────────────────────────────────────────────────────
    def hit(self, x: float, y: float) -> "Widget | None":
        return self if self.rect.contains(x, y) else None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button in (4, 5):
            return super().click(button, x, y)
        if button == 1:
            index = int((x - self.rect.left()) // self._stride())
            if 0 <= index < self._count:
                self._switch(index)
                return True
        return False

    def _step(self, delta: int) -> None:
        if self._count <= 0:
            return
        base = self._pending if self._pending is not None else self._current
        target = (base + delta) % self._count
        self._pending = target
        self._switch(target)

    def _switch(self, index: int) -> None:
        if self.window is None:
            return
        self.window.display.send_client_message(
            self.window.display.root, "_NET_CURRENT_DESKTOP", [index])
