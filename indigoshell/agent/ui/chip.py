"""The CALL chip: a beveled square with two arcs around a label —
turning while the agent works, blinking when it wants you."""

import time
from typing import Callable

import skia

from ... import text, theme
from ...widgets.base import Size, Widget
from .style import (AGENT_ACCENT, BLINK_FPS, CHIP_FPS, CHIP_INSET,
                    CHIP_INSET_ALPHA, FRAME_W, KICK_S, SETTLE_RATE, SPIN,
                    blink_on, frame_inset, inner_path, paint_raster,
                    plate_fill, plate_path)


# ── chip ────────────────────────────────────────────────────────────────
class Chip(Widget):
    """A square beveled chip with a label and two arcs around it.

    `look()` is asked every frame for (accent, label, mode): a key of
    SPIN (arcs turn at that speed), "blink" (the ring pattern, measured
    from `ring_since()`), "still" or "dim". Stopped arcs ease back to
    rest, above and below the label.
    """

    def __init__(self, size: int, look, ring_since=None, *,
                 tint: str | Callable[[], str] = AGENT_ACCENT) -> None:
        super().__init__()
        self.size = size
        self.look = look
        # Whose chip: its fill keeps that speaker's tint whatever the
        # accent is doing (yellow while asking, red ringing or down). A
        # function for a speaker whose colour changes: the hub's agent
        # speaks in two voices.
        self._tint = tint
        self.ring_since = ring_since or (lambda: 0.0)
        self.angle = -90.0
        self._kick_left = 0.0

    @property
    def tint(self) -> str:
        return self._tint() if callable(self._tint) else self._tint

    def _offset(self) -> float:
        """Degrees from the nearest rest angle (the arcs are symmetric,
        so -90 and 90 are the same pose)."""
        d = (self.angle + 90.0) % 180.0
        return d - 180.0 if d > 90.0 else d

    @property
    def animation_fps(self) -> int:
        # A blink can end by the clock alone (a nudge is two blinks);
        # the window repaints the chip once more when this falls to 0,
        # so it is never left in its last blink, lit or dim.
        _a, _l, mode = self.look()
        if mode in SPIN or self._kick_left > 0 or abs(self._offset()) > 0.5:
            return CHIP_FPS
        return BLINK_FPS if mode == "blink" else 0

    def kick(self) -> None:
        """A half-turn, eased — a line leaving."""
        self._kick_left = KICK_S
        self.invalidate()

    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)
        _a, _l, mode = self.look()
        if mode in SPIN:
            self.angle = (self.angle + SPIN[mode] * dt) % 360.0
        elif self._kick_left > 0:
            before = 1.0 - (self._kick_left / KICK_S)
            self._kick_left = max(0.0, self._kick_left - dt)
            after = 1.0 - (self._kick_left / KICK_S)
            ease = lambda x: 1 - (1 - x) ** 3
            self.angle = (self.angle + 180.0 * (ease(after) - ease(before))) % 360.0
        else:
            off = self._offset()
            step = off if abs(off) < 0.5 else off * min(1.0, dt * SETTLE_RATE)
            self.angle = (self.angle - step) % 360.0

    def measure(self, avail: Size) -> Size:
        return Size(self.size, self.size)

    def path(self) -> skia.Path:
        return plate_path(self.rect)

    def paint(self, canvas: skia.Canvas) -> None:
        accent, label, mode = self.look()
        r = self.rect
        lit = mode != "blink" or blink_on(time.monotonic() - self.ring_since())
        alpha = 1.0 if lit else 0.3
        if mode == "dim":
            alpha = 0.45

        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(*plate_fill(self.tint)))
        canvas.drawPath(self.path(), fill)
        tint = skia.Paint(AntiAlias=True)
        tint.setColor(theme.color(accent, 0.16 * alpha
                                  + (0.14 if mode == "blink" and lit else 0)))
        canvas.drawPath(self.path(), tint)
        paint_raster(canvas, self.path(), self.tint)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(FRAME_W)
        stroke.setColor(theme.color(accent, alpha))
        canvas.drawPath(plate_path(r, inset=frame_inset()), stroke)
        inset = skia.Paint(AntiAlias=True)
        inset.setStyle(skia.Paint.kStroke_Style)
        inset.setStrokeWidth(1.0)
        inset.setColor(theme.color(accent, CHIP_INSET_ALPHA * alpha))
        canvas.drawPath(inner_path(r, CHIP_INSET), inset)

        # Clear of the label on every side, inside the stroke: the arcs
        # turn through the label's corners, never across its text.
        radius = self.size * (0.38 if self.size >= 56 else 0.41)
        arc = skia.Paint(AntiAlias=True)
        arc.setStyle(skia.Paint.kStroke_Style)
        arc.setStrokeWidth(max(2.0, self.size * 0.05))
        arc.setColor(theme.color(accent, alpha))
        oval = skia.Rect.MakeLTRB(r.centerX() - radius, r.centerY() - radius,
                                  r.centerX() + radius, r.centerY() + radius)
        sweep = 64.0
        for base in (self.angle, self.angle + 180.0):
            canvas.drawArc(oval, base - sweep / 2, sweep, False, arc)

        size = max(8.0, round(self.size * 0.17))
        track = 0.6 if self.size >= 56 else 0.0
        w = text.measure(label, size, bold=True, tracking=track)
        text.draw(canvas, label, r.centerX() - w / 2,
                  text.baseline_in(r, size, bold=True), size,
                  theme.color(accent, alpha), bold=True, tracking=track)
