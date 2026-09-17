"""HUD-style clock: date on the left, time on the right, with an optional
extra widget (e.g. a BatteryMeter) stacked underneath.

The two labels align on their text baselines, not their boxes — they are
different sizes, so box-aligning them would leave the date floating.
"""

import datetime

import skia

from .. import text, theme
from .base import Size, Widget
from .label import Label
from .layout import Align, Box, Column, Row, Spacer


class Clock(Widget):
    animation_fps = 1

    def __init__(self, *, date_format: str = "%a %d %b",
                 time_format: str = "%H:%M",
                 time_size: float = 18, date_size: float = 11,   # points
                 gap: int = 10,
                 time_color: str | None = None,
                 date_color: str | None = None,
                 extra: Widget | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.date_format = date_format
        self.time_format = time_format
        self.extra = extra

        self._date = Label("", size=text.pt(date_size),
                           color=date_color or theme.BASE_MUTED)
        self._time = Label("", size=text.pt(time_size), bold=True,
                           align="right",
                           color=time_color or theme.CYAN_BRIGHT)
        # A fixed gap rather than a Spacer inside a fixed width: the font
        # is monospace and both formats are constant-width, so the cluster
        # never jitters, and it hugs its content instead of leaving a
        # dead stretch between date and time.
        row = Row([self._date, Spacer(size=gap), self._time],
                  align=Align.BASELINE)
        kids = [row] + ([extra] if extra is not None else [])
        # STRETCH so a fill=True extra widget spans the date/time row.
        self._column = Column(kids, spacing=1, align=Align.STRETCH)
        self._refresh()

    def children(self):
        return (self._column,)

    def _refresh(self) -> None:
        now = datetime.datetime.now()
        self._date.set_value(now.strftime(self.date_format))
        self._time.set_value(now.strftime(self.time_format))

    def animate(self, t: float) -> None:
        self._refresh()

    def measure(self, avail: Size) -> Size:
        return self._column.measure(avail)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self._column.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        self._column.paint(canvas)
