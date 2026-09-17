"""Notification toasts — v1's look, one window per toast.

Layout (body wraps under the heading row):

    ┌────────────────────────────────────────────┐
    │ // SUMMARY app_name                        │
    │                                            │
    │ body line 1                                │
    │ body line 2                                │
    │ [Action 1]  [Action 2]                     │
    └────────────────────────────────────────────┘

A thin beveled frame in the urgency color wraps the whole thing, the
timer trace overdraws it clockwise as time runs out, and a segmented
meter appears when the `value` hint is set — all verbatim from v1's
cairo recipe, in Skia.

v1 stacked every toast inside one window; here each toast IS a window,
so the spawn/despawn effects run per-notification. `NotificationManager`
owns the D-Bus server, opens a window per toast bottom-right above the
bar (newest closest to the bar, like dunst), and re-anchors the stack
as toasts come and go. Hovering any toast pauses every timer.
"""

import asyncio
import html
import logging
import math
import re
import time

import skia

from .. import shapes, text, theme
from ..services.notifications import (
    REASON_CLOSED,
    REASON_DISMISSED,
    REASON_EXPIRED,
    URGENCY_CRITICAL,
    URGENCY_LOW,
    Notification,
    NotificationServer,
)
from ..window import Anchor, Layer, WindowSpec
from .base import Insets, Size, Widget

log = logging.getLogger(__name__)

_FRAME_BY_URGENCY = {
    URGENCY_LOW:      theme.NOTIF_FRAME_LOW,
    URGENCY_CRITICAL: theme.NOTIF_FRAME_CRITICAL,
}
_BODY_FG_BY_URGENCY = {
    URGENCY_CRITICAL: theme.NOTIF_BODY_FG_CRITICAL,
}

_HEAD_SIZE = 15.0
_BODY_SIZE = 15.0
_ACTION_SIZE = 14.0
_HEAD_BODY_GAP = 4
_ACTIONS_GAP = 6        # between body and the action row, and between buttons
_ACTION_PAD_X = 10
_ACTION_PAD_Y = 4

_TAG_RE = re.compile(r"<[^>]*>")


def _frame_color(urgency: int) -> str:
    return _FRAME_BY_URGENCY.get(urgency, theme.NOTIF_FRAME_NORMAL)


def _body_fg(urgency: int) -> str:
    return _BODY_FG_BY_URGENCY.get(urgency, theme.NOTIF_BODY_FG_NORMAL)


def _plain_body(body: str) -> str:
    """Spec allows a small markup subset in the body; Pango rendered it
    in v1, here the tags are stripped and entities resolved."""
    return html.unescape(_TAG_RE.sub("", body))


def _wrap(line: str, size: float, max_w: float) -> list[str]:
    """Greedy word wrap; overlong words break mid-word (v1's WORD_CHAR)."""
    out: list[str] = []
    for raw in line.splitlines() or [""]:
        words = raw.split(" ")
        cur = ""
        for word in words:
            candidate = f"{cur} {word}" if cur else word
            if text.measure(candidate, size) <= max_w:
                cur = candidate
                continue
            if cur:
                out.append(cur)
                cur = ""
            while text.measure(word, size) > max_w and len(word) > 1:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if text.measure(word[:mid], size) <= max_w:
                        lo = mid
                    else:
                        hi = mid - 1
                out.append(word[:lo])
                word = word[lo:]
            cur = word
        out.append(cur)
    return out


def _ellipsize(value: str, size: float, max_w: float, bold: bool = False) -> str:
    if text.measure(value, size, bold) <= max_w:
        return value
    while value and text.measure(value + "…", size, bold) > max_w:
        value = value[:-1]
    return value + "…"


class _ActionButton(Widget):
    """One action chip. Hover inverts, click invokes."""

    def __init__(self, toast: "NotificationToast", key: str, label: str) -> None:
        super().__init__()
        self.toast = toast
        self.action_key = key
        self.label = label

    @property
    def interactive(self) -> bool:
        return True

    def measure(self, avail: Size) -> Size:
        return Size(text.measure(self.label, _ACTION_SIZE) + _ACTION_PAD_X * 2,
                    text.line_height(_ACTION_SIZE) + _ACTION_PAD_Y * 2)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        fill = skia.Paint(AntiAlias=False)
        fill.setColor(theme.color(
            _frame_color(self.toast.notif.urgency) if self.hovered
            else theme.NOTIF_ACTION_BG))
        canvas.drawRect(r, fill)
        fg = theme.BASE_BLACK if self.hovered else theme.NOTIF_ACTION_FG
        text.draw(canvas, self.label, r.left() + _ACTION_PAD_X,
                  text.baseline_in(r, _ACTION_SIZE), _ACTION_SIZE,
                  theme.color(fg))

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1:
            return False
        self.toast.manager.invoke_action(self.toast.notif.id, self.action_key)
        return True


class NotificationToast(Widget):
    """A single toast. Click anywhere dismisses (or fires the `default`
    action, matching dunst's `mouse_left_click = close_current`)."""

    def __init__(self, manager: "NotificationManager", notif: Notification,
                 timeout_ms: int) -> None:
        super().__init__()
        self.manager = manager
        self.notif = notif
        self._lines: list[str] = []
        self._buttons: list[_ActionButton] = []
        self._timeout_ms = 0
        self._elapsed_ms = 0.0
        self._last_tick: float | None = None
        self._expired = False
        self._apply(notif, timeout_ms)

    def _apply(self, notif: Notification, timeout_ms: int) -> None:
        self.notif = notif
        inner_w = theme.NOTIF_WIDTH - 2 * theme.NOTIF_PADDING_X
        body = _plain_body(notif.body or "")
        self._lines = _wrap(body, _BODY_SIZE, inner_w) if body.strip() else []
        # Action row — skip "default" (handled by body click).
        self._buttons = [_ActionButton(self, k, label)
                         for k, label in notif.actions if k != "default"]
        if self.window is not None:
            for btn in self._buttons:
                btn.attach(self.window)
        self._timeout_ms = max(0, int(timeout_ms))
        self._elapsed_ms = 0.0
        self._last_tick = None
        self._expired = False
        # The frame clock drives the timer and its border trace; a
        # persistent toast needs neither and costs nothing while idle.
        self.animation_fps = 30 if self._timeout_ms > 0 else 0

    def update(self, notif: Notification, timeout_ms: int) -> None:
        """Mutate in place when an app reuses the id."""
        self._apply(notif, timeout_ms)
        self.invalidate(layout=True)

    # ── tree ─────────────────────────────────────────────────────────
    def children(self):
        return tuple(self._buttons)

    @property
    def interactive(self) -> bool:
        return True

    # ── layout ───────────────────────────────────────────────────────
    def _meter_extra(self) -> int:
        return (theme.NOTIF_METER_THICK + theme.NOTIF_METER_INSET_Y
                if self.notif.value is not None else 0)

    def measure(self, avail: Size) -> Size:
        h = theme.NOTIF_PADDING_Y * 2 + text.line_height(_HEAD_SIZE, bold=True)
        if self._lines:
            h += _HEAD_BODY_GAP + len(self._lines) * text.line_height(_BODY_SIZE)
        if self._buttons:
            h += _ACTIONS_GAP + (text.line_height(_ACTION_SIZE)
                                 + _ACTION_PAD_Y * 2)
        h += self._meter_extra()
        return Size(theme.NOTIF_WIDTH, math.ceil(h))

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if not self._buttons:
            return
        btn_h = text.line_height(_ACTION_SIZE) + _ACTION_PAD_Y * 2
        y = (rect.bottom() - theme.NOTIF_PADDING_Y - self._meter_extra()
             - btn_h)
        x = rect.left() + theme.NOTIF_PADDING_X
        for btn in self._buttons:
            size = btn.measure(Size(rect.width(), btn_h))
            btn.arrange(skia.Rect.MakeXYWH(x, y, size.width, size.height))
            x += size.width + _ACTIONS_GAP

    # ── paint ────────────────────────────────────────────────────────
    def _bevel_path(self, inset: float = 0.0) -> skia.Path:
        r = self.rect
        return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                              bevel=theme.NOTIF_BEVEL,
                              corners=theme.NOTIF_BEVEL_CORNERS, inset=inset)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        accent = _frame_color(self.notif.urgency)

        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(theme.NOTIF_BG))
        canvas.drawPath(self._bevel_path(), fill)

        # ── beveled border (matches Menu rows) ──
        line_w = theme.NOTIF_BORDER_THICK
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(line_w)
        stroke.setColor(theme.color(accent))
        border = self._bevel_path(inset=line_w / 2)
        canvas.drawPath(border, stroke)

        # ── timer trace overdraws the urgency border clockwise from
        # top-left as time elapses; full perimeter → expire. ──
        if self._timeout_ms > 0 and self._elapsed_ms > 0:
            progress = min(1.0, self._elapsed_ms / self._timeout_ms)
            measure = skia.PathMeasure(border, False)
            partial = skia.Path()
            if measure.getSegment(0.0, measure.getLength() * progress,
                                  partial, True):
                stroke.setColor(theme.color(theme.NOTIF_TIMER_BORDER_FG))
                canvas.drawPath(partial, stroke)

        self._paint_text(canvas)
        if self.notif.value is not None:
            self._paint_meter(canvas, accent)
        for btn in self._buttons:
            btn.paint(canvas)

    def _paint_text(self, canvas: skia.Canvas) -> None:
        r = self.rect
        x0 = r.left() + theme.NOTIF_PADDING_X
        inner_w = r.width() - 2 * theme.NOTIF_PADDING_X
        head_h = text.line_height(_HEAD_SIZE, bold=True)
        m = text.font(_HEAD_SIZE, True).getMetrics()
        baseline = r.top() + theme.NOTIF_PADDING_Y - m.fAscent

        # `// SUMMARY app_name` — matches dunst's `format = ...` recipe.
        x = x0
        for value, color, bold in (
            ("//", theme.NOTIF_SEPARATOR_FG, True),
            (self.notif.summary or "", theme.NOTIF_SUMMARY_FG, True),
            (self.notif.app_name or "", theme.NOTIF_APPNAME_FG, False),
        ):
            if not value:
                continue
            room = inner_w - (x - x0)
            if room <= 0:
                break
            value = _ellipsize(value, _HEAD_SIZE, room, bold)
            text.draw(canvas, value, x, baseline, _HEAD_SIZE,
                      theme.color(color), bold)
            x += text.measure(value, _HEAD_SIZE, bold) + \
                text.measure(" ", _HEAD_SIZE, bold)

        y = r.top() + theme.NOTIF_PADDING_Y + head_h + _HEAD_BODY_GAP
        body_m = text.font(_BODY_SIZE).getMetrics()
        body_h = text.line_height(_BODY_SIZE)
        fg = theme.color(_body_fg(self.notif.urgency))
        for line in self._lines:
            text.draw(canvas, line, x0, y - body_m.fAscent, _BODY_SIZE, fg)
            y += body_h

    def _paint_meter(self, canvas: skia.Canvas, accent: str) -> None:
        """CPU-style segmented progress meter along the bottom edge."""
        r = self.rect
        n   = theme.NOTIF_METER_SEGMENTS
        gap = theme.NOTIF_METER_GAP
        h   = theme.NOTIF_METER_THICK
        side = theme.NOTIF_PADDING_X
        usable_w = max(1.0, r.width() - 2 * side)
        tick_w = max(1.0, (usable_w - gap * (n - 1)) / n)
        ratio = max(0.0, min(1.0, (self.notif.value or 0) / 100.0))
        fill_end_x = ratio * usable_w
        y = (r.bottom() - int(theme.NOTIF_BORDER_THICK) - h
             - theme.NOTIF_METER_INSET_Y)
        paint = skia.Paint(AntiAlias=False)
        for i in range(n):
            x = i * (tick_w + gap)
            mid = x + tick_w / 2
            paint.setColor(theme.color(
                accent if mid <= fill_end_x else theme.NOTIF_METER_DIM))
            canvas.drawRect(
                skia.Rect.MakeXYWH(r.left() + side + x, y, tick_w, h), paint)

    # ── timer ────────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        if self._timeout_ms <= 0 or self._expired:
            return
        now = time.monotonic()
        if self._last_tick is not None and not self.manager.any_hovered():
            # Hovering any toast pauses the countdown on ALL of them —
            # skipping the accumulation while the baseline keeps moving
            # is what makes the paused interval free.
            self._elapsed_ms += (now - self._last_tick) * 1000.0
        self._last_tick = now
        if self._elapsed_ms >= self._timeout_ms:
            self._expired = True
            self.manager.dismiss(self.notif.id, REASON_EXPIRED)
            return
        self.invalidate()

    # ── input ────────────────────────────────────────────────────────
    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button not in (1, 2, 3):
            return False
        for key, _label in self.notif.actions:
            if key == "default":
                self.manager.invoke_action(self.notif.id, key)
                return True
        self.manager.dismiss(self.notif.id, REASON_DISMISSED)
        return True


class NotificationManager:
    """Owns the D-Bus server and one window per live toast.

    Listed in the config's SERVICES; the daemon calls `start()` once its
    loop is up. Toast windows are registered into `daemon.kinds` under
    `notif-<id>` — the same open-time-spec pattern as the tray panel —
    and re-anchored whenever the stack changes shape.
    """

    def __init__(self, *, effects: tuple = (), effect_scale: float = 0.35) -> None:
        self.effects = tuple(effects)
        self.effect_scale = effect_scale
        self._server = NotificationServer(
            on_notify=self._on_notify,
            on_close_request=lambda nid: self._remove(nid, REASON_CLOSED),
        )
        self._toasts: dict[int, NotificationToast] = {}
        self._order: list[int] = []          # oldest first, newest last
        self._task = None

    # ── daemon service hooks ─────────────────────────────────────────
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            self._server.start())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._toasts.clear()
        self._order.clear()

    # ── server callbacks ─────────────────────────────────────────────
    def _resolve_timeout(self, notif: Notification) -> int:
        """Sender's expire_timeout (ms), falling back to urgency defaults
        when negative. Returns 0 to mean "never auto-dismiss"."""
        timeout = notif.expire_timeout
        if timeout < 0:
            if notif.urgency == URGENCY_CRITICAL:
                return theme.NOTIF_TIMEOUT_CRITICAL
            if notif.urgency == URGENCY_LOW:
                return theme.NOTIF_TIMEOUT_LOW_MS
            return theme.NOTIF_TIMEOUT_NORMAL_MS
        return timeout

    def _on_notify(self, notif: Notification) -> None:
        from ..core.daemon import get_daemon
        d = get_daemon()
        timeout_ms = self._resolve_timeout(notif)

        existing = self._toasts.get(notif.id)
        if existing is not None:
            existing.update(notif, timeout_ms)
            win = d.instances.get(_win_name(notif.id))
            if win is not None:
                size = existing.measure(Size(theme.NOTIF_WIDTH, 10_000.0))
                win.resize_content(round(size.width), round(size.height))
                # A persistent toast has no clock; an update that adds a
                # timeout needs one started. Stop first — _start_clock
                # doesn't check for a live handle.
                win._stop_clock()
                win._start_clock()
            self._reposition()
            return

        toast = NotificationToast(self, notif, timeout_ms)
        size = toast.measure(Size(theme.NOTIF_WIDTH, 10_000.0))
        name = _win_name(notif.id)
        d.kinds[name] = WindowSpec(
            name=name,
            layer=Layer.OVERLAY,
            anchor=Anchor.BOTTOM | Anchor.RIGHT,
            size=(round(size.width), round(size.height)),
            # Newest sits at the bottom (closest to the bar, like dunst);
            # _reposition pushes the older ones up.
            margin=Insets(right=theme.NOTIF_OFFSET_X,
                          bottom=theme.NOTIF_OFFSET_Y),
            override_redirect=True,
            focusable=False,
            background="#00000000",
            effects=self.effects,
            effect_scale=self.effect_scale,
            content=toast,
        )
        self._toasts[notif.id] = toast
        self._order.append(notif.id)
        try:
            d.open(name)
        except Exception:
            log.exception("failed to open toast window")
            self._toasts.pop(notif.id, None)
            self._order.remove(notif.id)
            d.kinds.pop(name, None)
            return
        self._reposition()

    # ── toast callbacks ──────────────────────────────────────────────
    def dismiss(self, nid: int, reason: int) -> None:
        self._remove(nid, reason)

    def invoke_action(self, nid: int, key: str) -> None:
        self._server.emit_action(nid, key)
        self._remove(nid, REASON_CLOSED)

    def any_hovered(self) -> bool:
        return any(w.hovered
                   for toast in self._toasts.values()
                   for w in toast.walk())

    # ── stack bookkeeping ────────────────────────────────────────────
    def _remove(self, nid: int, reason: int) -> None:
        from ..core.daemon import get_daemon
        toast = self._toasts.pop(nid, None)
        if toast is None:
            return
        self._order.remove(nid)
        d = get_daemon()
        name = _win_name(nid)
        d.close(name)
        d.kinds.pop(name, None)
        self._server.emit_closed(nid, reason)
        self._reposition()

    def _reposition(self) -> None:
        """Re-anchor every mapped toast: newest at NOTIF_OFFSET_Y, each
        older one stacked NOTIF_GAP above the one below it."""
        from ..core.daemon import get_daemon
        import dataclasses
        d = get_daemon()
        bottom = theme.NOTIF_OFFSET_Y
        for nid in reversed(self._order):
            name = _win_name(nid)
            win = d.instances.get(name)
            if win is None:
                continue
            _cx, _cy, cw, ch = win.content_root
            spec = dataclasses.replace(
                win.spec, margin=Insets(right=theme.NOTIF_OFFSET_X,
                                        bottom=bottom))
            win.spec = spec
            d.kinds[name] = spec
            win.resize_content(cw, ch)
            bottom += ch + theme.NOTIF_GAP


def _win_name(nid: int) -> str:
    return f"notif-{nid}"
