"""Tab strip in the braindance HUD idiom.

The active tab is a filled plate with a magenta bar along its bottom
edge; inactive tabs are bare muted text. A single muted rule runs under
the whole strip, which is what separates the strip from the rows without
boxing every tab.

Ported from `render.py set_panel()` in the VitureBD braindance overlay —
the tab loop drawing `(36,24,64)` plates with a `P["magenta"]` underline,
then `d.line(...)` across the panel.
"""

import skia

from .. import text, theme
from .base import Size, Widget

TAB_HEIGHT = 38
TAB_PAD_X = 16
TAB_GAP = 6
UNDER_THICK = 2
RULE_GAP = 10
RULE_THICK = 1

PLATE = theme.HUD_PLATE    # braindance (36, 24, 64)


class TabBar(Widget):
    def __init__(self, titles, on_switch, *, size: float = theme.FONT_SIZE - 1,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.titles = list(titles)
        self.on_switch = on_switch
        self.size = size
        self.active = 0
        self._spans: list[tuple[float, float]] = []

    def set_active(self, index: int) -> None:
        index = max(0, min(len(self.titles) - 1, index))
        if index != self.active:
            self.active = index
            self.invalidate()

    # ── layout ──────────────────────────────────────────────────────────
    def _width_of(self, title: str) -> float:
        return text.measure(title, self.size, True, 1.0) + TAB_PAD_X * 2

    def measure(self, avail: Size) -> Size:
        return Size(avail.width, TAB_HEIGHT + RULE_GAP + RULE_THICK)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self._spans = []
        x = rect.left()
        for title in self.titles:
            w = self._width_of(title)
            self._spans.append((x, w))
            x += w + TAB_GAP

    # ── input ───────────────────────────────────────────────────────────
    def _index_at(self, x: float, y: float) -> int:
        if y > self.rect.top() + TAB_HEIGHT:
            return -1
        for i, (left, w) in enumerate(self._spans):
            if left <= x < left + w:
                return i
        return -1

    # Which tab was hit needs the pointer position, so this overrides
    # click()/hit() rather than setting on_left_click — see Widget.click.
    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.rect.contains(x, y) else None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1:
            return False
        index = self._index_at(x, y)
        if index >= 0 and index != self.active:
            self.set_active(index)
            self.on_switch(index)
        return index >= 0

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        top = r.top()

        for i, (left, w) in enumerate(self._spans):
            active = i == self.active
            if active:
                plate = skia.Paint(AntiAlias=False)
                plate.setColor(theme.color(PLATE))
                canvas.drawRect(
                    skia.Rect.MakeXYWH(left, top, w, TAB_HEIGHT), plate)
                bar = skia.Paint(AntiAlias=False)
                bar.setColor(theme.color(theme.MAGENTA_BRIGHT))
                canvas.drawRect(
                    skia.Rect.MakeXYWH(left, top + TAB_HEIGHT - UNDER_THICK,
                                       w, UNDER_THICK), bar)
            title = self.titles[i]
            tw = text.measure(title, self.size, True, 1.0)
            text.draw(canvas, title, left + (w - tw) / 2,
                      text.baseline_in(
                          skia.Rect.MakeXYWH(left, top, w, TAB_HEIGHT),
                          self.size, True),
                      self.size,
                      theme.color(theme.HUD_FG_BRIGHT if active
                                  else theme.BASE_MUTED),
                      True, 1.0)

        rule = skia.Paint(AntiAlias=False)
        rule.setColor(theme.color(theme.BASE_MUTED, 0.7))
        canvas.drawRect(
            skia.Rect.MakeXYWH(r.left(), top + TAB_HEIGHT + RULE_GAP,
                               r.width(), RULE_THICK), rule)
