"""Your plate: what you said this turn, anything queued, and the line
you are typing, with the Look chip over it while the next line is to
carry what you are looking at (Ctrl+L, call.py)."""

import math

import skia

from ... import shapes, text, theme
from ...widgets.base import Size, Widget
from ...widgets.text_input import TextInput
from ..remote import RemoteAgent
from .frame import Comet
from .style import (ACT_SIZE, BODY_SIZE, MAX_HEARD_LINES, NAME_SIZE, PAD_X,
                    PAD_Y, PLATE_W, TAG_SIZE, YOU_ACCENT, YOU_FILL, line_h,
                    paint_heading, paint_plate, when)


_CHIP_H = line_h(ACT_SIZE) + 12     # the Look chip's row
_CHIP_PAD = 8


# ── your plate ──────────────────────────────────────────────────────────
class YouPlate(Widget):
    def __init__(self, agent: RemoteAgent, width: int = PLATE_W) -> None:
        super().__init__()
        self.agent = agent
        self.width = width
        self.input = TextInput(placeholder="say something",
                               size=BODY_SIZE, caret_color=YOU_ACCENT,
                               color=theme.HUD_FG_BRIGHT,
                               # A caret that never stops blinking: the
                               # line is open for as long as the call is.
                               blink_for=math.inf)
        self._input_h = 0.0
        self.comet = Comet()        # one lap round the plate on a send
        # The Look chip: (words, colour), or None. Set by the call.
        self.look_chip: tuple[str, str] | None = None

    @property
    def animation_fps(self) -> int:
        return self.comet.fps()

    @property
    def inner_w(self) -> float:
        return self.width - 2 * PAD_X

    def children(self):
        return [self.input]

    def _heard(self) -> list[str]:
        if not self.agent.heard:
            return []
        lines = text.wrap(self.agent.heard, BODY_SIZE, self.inner_w)
        if len(lines) > MAX_HEARD_LINES:
            lines = lines[:MAX_HEARD_LINES]
            lines[-1] = text.ellipsize(lines[-1] + " …", BODY_SIZE,
                                       self.inner_w)
        return lines

    def _content_h(self) -> float:
        h = line_h(NAME_SIZE, True) + 6
        h += len(self._heard()) * (line_h(BODY_SIZE) + 3)
        h += len(self.agent.queued) * (line_h(ACT_SIZE) + 3)
        if self.look_chip is not None:
            h += _CHIP_H
        # Whole pixels, like every other row: the plates and chips below
        # are placed from this height, and a frame edge on a fractional
        # row is soft — and on the GPU its antialiasing reaches a pixel
        # past the clip of a partial repaint, where it is never cleared
        # and builds up, repaint by repaint, into a bright dot.
        self._input_h = math.ceil(
            self.input.measure(Size(self.inner_w, 100)).height)
        return h + self._input_h

    def measure(self, avail: Size) -> Size:
        return Size(self.width, self._content_h() + 2 * PAD_Y)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        h = self._content_h()
        top = rect.top() + PAD_Y + h - self._input_h
        prompt = text.measure("› ", BODY_SIZE, bold=True)
        self.input.arrange(skia.Rect.MakeXYWH(
            rect.left() + PAD_X + prompt, top,
            self.inner_w - prompt, self._input_h))

    def _paint_keys(self, canvas, box, left: float, right: float) -> None:
        """The keys, right-aligned in the heading; the least needed go
        when the heading is short of room."""
        a = self.agent
        look = "^L look" if getattr(a, "attachments", False) else ""
        if a.busy:
            keys = ["⏎ send", look, "^C stop", "^K earlier", "^D devices",
                    "esc hang up"]
        else:
            keys = ["⏎ send", look, "^K earlier", "^D devices", "esc hang up"]
        keys = [k for k in keys if k]
        gap = "   "
        for drop in ("^D devices", "^K earlier", "^L look"):
            if left + 12 + text.measure(gap.join(keys), TAG_SIZE) <= right:
                break
            if drop in keys:
                keys.remove(drop)
        hint = gap.join(keys)
        text.draw(canvas, hint, right - text.measure(hint, TAG_SIZE),
                  text.baseline_in(box, TAG_SIZE), TAG_SIZE,
                  theme.color(theme.BASE_MUTED))

    def _paint_chip(self, canvas, x: float, y: float, right: float) -> None:
        """The Look chip: what the next line will carry, in a small
        beveled tag."""
        words, color = self.look_chip
        words = text.ellipsize(words, ACT_SIZE, right - x - 2 * _CHIP_PAD)
        w = text.measure(words, ACT_SIZE) + 2 * _CHIP_PAD
        h = _CHIP_H - 6
        tag = shapes.beveled(x, y + 2, w, h, bevel=5,
                             corners=("top-right", "bottom-left"))
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(color, 0.12))
        canvas.drawPath(tag, fill)
        edge = skia.Paint(AntiAlias=True)
        edge.setStyle(skia.Paint.kStroke_Style)
        edge.setStrokeWidth(1)
        edge.setColor(theme.color(color, 0.8))
        canvas.drawPath(tag, edge)
        text.draw(canvas, words, x + _CHIP_PAD,
                  text.baseline_in(skia.Rect.MakeXYWH(x, y + 2, w, h),
                                   ACT_SIZE),
                  ACT_SIZE, theme.color(color))

    def paint(self, canvas: skia.Canvas) -> None:
        a = self.agent
        paint_plate(canvas, self.rect, YOU_ACCENT, YOU_FILL)
        self.comet.paint(canvas, self.rect)
        x = self.rect.left() + PAD_X
        right = self.rect.right() - PAD_X
        y = self.rect.top() + PAD_Y

        hh = line_h(NAME_SIZE, True) + 6
        box = skia.Rect.MakeXYWH(x, y, right - x, hh)
        # The hub's chat is shared: a line asked on another of your
        # devices says which. And when it was said, as an earlier
        # exchange says it.
        on = getattr(a, "heard_on", None)
        looked = getattr(a, "heard_attached", 0)
        head_end = paint_heading(
            canvas, box, NAME_SIZE,
            (("YOU", YOU_ACCENT, True),
             (when(a.at) if a.heard else "", theme.BASE_MUTED, False),
             (f"on {on}" if on else "", theme.BASE_MUTED, False),
             ("+ look" if looked and a.heard else "", theme.CYAN_MID,
              False)))
        self._paint_keys(canvas, box, head_end, right)
        y += hh

        for line in self._heard():
            lh = line_h(BODY_SIZE) + 3
            text.draw(canvas, line, x,
                      text.baseline_in(skia.Rect.MakeXYWH(x, y, 1, lh),
                                       BODY_SIZE),
                      BODY_SIZE, theme.color(theme.HUD_FG))
            y += lh
        for line in a.queued:
            lh = line_h(ACT_SIZE) + 3
            row = skia.Rect.MakeXYWH(x, y, right - x, lh)
            text.draw(canvas, "queued", x, text.baseline_in(row, ACT_SIZE),
                      ACT_SIZE, theme.color(theme.YELLOW_DIM))
            qx = x + text.measure("queued ", ACT_SIZE)
            text.draw(canvas, text.ellipsize(line, ACT_SIZE, right - qx), qx,
                      text.baseline_in(row, ACT_SIZE), ACT_SIZE,
                      theme.color(theme.HUD_FG))
            y += lh
        if self.look_chip is not None:
            self._paint_chip(canvas, x, y, right)
            y += _CHIP_H

        r = self.input.rect
        text.draw(canvas, "›", x, text.baseline_in(r, BODY_SIZE, True),
                  BODY_SIZE, theme.color(YOU_ACCENT), bold=True)
        self.input.placeholder = ("follow up" if a.heard else "say something")
        self.input.paint(canvas)
