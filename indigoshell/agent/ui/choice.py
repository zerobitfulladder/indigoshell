"""The answers under a question: GREENLIGHT and SCRUB.

Two small slabs cut like the plates, the call's take on the chord
menu's chips. Each is a widget of its own so the window's hover and
clicks reach it; the agent plate places them in its keys row and paints
them with its other rows. A and D answer from the keyboard too
(CallHost.key); the buttons carry no key letters, the user knows them."""

from typing import Callable

import skia

from ... import shapes, text, theme
from ...widgets.base import Size, Widget
from .style import ACT_SIZE, CORNERS

CHOICE_H = 30
CHOICE_GAP = 12         # between the two buttons
PAD_X = 20
BEVEL = 7
LINE_W = 1.5
SIZE = ACT_SIZE
TRACKING = 1.5


class Choice(Widget):
    """One answer, outlined in its colour; hover and press fill it in.
    Both answers carry the same weight: neither is nudged, since what
    asks is root (the user's pick over a solid GREENLIGHT)."""

    def __init__(self, label: str, verdict: str, accent: str,
                 pick: Callable[[str], None]) -> None:
        super().__init__(on_left_click=lambda _w: pick(verdict))
        self.label = label
        self.verdict = verdict
        self.accent = accent

    def measure(self, avail: Size) -> Size:
        w = text.measure(self.label, SIZE, bold=True, tracking=TRACKING)
        return Size(round(w + 2 * PAD_X), CHOICE_H)

    def place(self, x: float, y: float) -> float:
        """Sit at (x, y) on whole pixels; returns the right edge."""
        size = self.measure(Size(0, 0))
        self.arrange(skia.Rect.MakeXYWH(round(x), round(y),
                                        size.width, size.height))
        return self.rect.right()

    def hide(self) -> None:
        self.arrange(skia.Rect.MakeEmpty())
        self.hovered = self.pressed = False

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        if r.isEmpty():
            return
        slab = shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                              bevel=BEVEL, corners=CORNERS, inset=LINE_W / 2)
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(
            self.accent,
            0.38 if self.pressed else 0.22 if self.hovered else 0.08))
        canvas.drawPath(slab, fill)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(LINE_W)
        stroke.setColor(theme.color(self.accent,
                                    1.0 if self.hovered or self.pressed
                                    else 0.8))
        canvas.drawPath(slab, stroke)
        w = text.measure(self.label, SIZE, bold=True, tracking=TRACKING)
        text.draw(canvas, self.label, r.left() + (r.width() - w) / 2,
                  text.baseline_in(r, SIZE), SIZE,
                  theme.color(self.accent), bold=True, tracking=TRACKING)
