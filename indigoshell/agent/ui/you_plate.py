"""Your plate: what you said this turn, anything queued, and the line
you are typing."""

import math
import skia

from ... import text, theme
from ...widgets.base import Size, Widget
from ...widgets.text_input import TextInput
from ..session import Agent
from .frame import Comet
from .style import ACT_SIZE, BODY_SIZE, MAX_HEARD_LINES, NAME_SIZE, PAD_X, PAD_Y, PLATE_W, TAG_SIZE, YOU_ACCENT, YOU_FILL, line_h, paint_heading, paint_plate


# ── your plate ──────────────────────────────────────────────────────────
class YouPlate(Widget):
    def __init__(self, agent: Agent, width: int = PLATE_W) -> None:
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
        self._input_h = self.input.measure(Size(self.inner_w, 100)).height
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

    def paint(self, canvas: skia.Canvas) -> None:
        a = self.agent
        paint_plate(canvas, self.rect, YOU_ACCENT, YOU_FILL)
        self.comet.paint(canvas, self.rect)
        x = self.rect.left() + PAD_X
        right = self.rect.right() - PAD_X
        y = self.rect.top() + PAD_Y

        hh = line_h(NAME_SIZE, True) + 6
        box = skia.Rect.MakeXYWH(x, y, right - x, hh)
        paint_heading(canvas, box, NAME_SIZE, (("YOU", YOU_ACCENT, True),))
        hint = "⏎ send   ^H history   ^N new   esc hang up"
        if a.approval is not None and not self.input.text:
            hint = "Y / A / N to answer"
        elif a.busy:
            hint = "⏎ queue   ^C stop   esc hang up"
        w = text.measure(hint, TAG_SIZE)
        text.draw(canvas, hint, right - w, text.baseline_in(box, TAG_SIZE),
                  TAG_SIZE, theme.color(theme.BASE_MUTED))
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

        r = self.input.rect
        text.draw(canvas, "›", x, text.baseline_in(r, BODY_SIZE, True),
                  BODY_SIZE, theme.color(YOU_ACCENT), bold=True)
        self.input.placeholder = ("follow up" if a.heard else "say something")
        self.input.paint(canvas)
