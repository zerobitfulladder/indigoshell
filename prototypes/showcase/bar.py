"""Where to click on the bar, computed from the bar itself.

The bar's right half is laid out from the right edge, and the media
widget is a ♫ glyph while idle but 286px once a title plays — so every
target left of it moves when the music starts. Rather than hard-coding
coordinates that are only right in one state, this lays out the shipped
bar's real widget classes at the screen's width, with the network
widget's real label and address and the media widget in the state asked
for, and reports each widget's centre.

Checked against a live frame of the idle bar: every widget lands where
the running one draws it.
"""

import asyncio
import logging

import skia

from indigoshell import theme
from indigoshell.config_default import WINDOWS
from indigoshell.widgets.base import Size

logging.disable(logging.WARNING)


def targets(screen_w: int, screen_h: int, *, playing: bool) -> dict:
    spec = next(w for w in WINDOWS if w.name == "bar")
    row = spec.content
    kids = {type(k).__name__: k for k in row.children()}

    asyncio.run(kids["Network"]._refresh())      # the real label and IP
    media = kids["Media"]
    media._title = "♫ title long enough to take the full width" if playing else ""

    pad = spec.padding
    rect = skia.Rect.MakeXYWH(pad.left, 0, screen_w - pad.horizontal,
                              theme.BAR_HEIGHT)
    row.measure(Size(rect.width(), rect.height()))
    row.arrange(rect)

    y = screen_h - theme.BAR_HEIGHT // 2

    def centre(widget) -> tuple[int, int]:
        return round(widget.rect.centerX()), y

    clock_row = kids["Row"]                       # [Clock, Systray]
    tray = list(clock_row.children())[-1]
    return {
        "indigo": centre(kids["Systag"]),
        "meters": centre(kids["Brackets"]),
        "network": centre(kids["Network"]),
        "media": centre(media),
        "volume": centre(kids["Volume"]),
        "tray": centre(tray),
    }


if __name__ == "__main__":
    for state in (False, True):
        print("playing" if state else "idle", targets(2560, 1440, playing=state))
