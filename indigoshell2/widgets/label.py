"""Text widget."""

from typing import Callable

import skia

from .. import text, theme
from .base import Size, Widget


class Label(Widget):
    def __init__(self, value: str | Callable[[], str], *,
                 size: float = theme.FONT_SIZE,
                 color: str | Callable[[], str] = theme.FG,
                 bold: bool = False,
                 align: str = "left",
                 tracking: float = 0.0,
                 min_width: float = 0.0,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._value = value
        self.size = size
        self.color = color
        self.bold = bold
        self.align = align
        # Letter spacing, in px — Pango's `letter_spacing`.
        self.tracking = tracking
        # Floor on the measured width, so a column of labels with
        # different text still reserves one cell width and whatever
        # follows them lines up. GTK's `set_width_chars`.
        self.min_width = min_width

    @property
    def value(self) -> str:
        return self._value() if callable(self._value) else self._value

    def set_value(self, value: str) -> None:
        if self._value != value:
            self._value = value
            self.invalidate(layout=True)

    def measure(self, avail: Size) -> Size:
        width = text.measure(self.value, self.size, self.bold, self.tracking)
        return Size(max(width, self.min_width),
                    text.line_height(self.size, self.bold))

    def baseline(self, size: Size) -> float:
        return -text.font(self.size, self.bold).getMetrics().fAscent

    def paint(self, canvas: skia.Canvas) -> None:
        value = self.value
        color = self.color() if callable(self.color) else self.color
        width = text.measure(value, self.size, self.bold, self.tracking)
        if self.align == "right":
            x = self.rect.right() - width
        elif self.align == "center":
            x = self.rect.centerX() - width / 2
        else:
            x = self.rect.left()
        text.draw(canvas, value, x,
                  text.baseline_in(self.rect, self.size, self.bold),
                  self.size, theme.color(color), self.bold, self.tracking)
