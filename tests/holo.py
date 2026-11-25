"""A `holo` panel — the call's hologram on any window — on its first
open and on its second.

    .venv/bin/python tests/holo.py

The window draws the raster over its content and owns the scan
(`WindowSpec.holo`, widgets/holo.py), outside the content's tree. So
the checks are the ones a widget would get for free from being in the
tree: it is ticked at its rate while a pass is on, its pass draws
something, and nothing of the pass is left on screen once it is over
(`_live.stale`). And the raster lands only on pixels the content
painted: none in the bleed margin or in a cut corner.
"""

import asyncio
import sys
import time

import _live

from indigoshell import theme
from indigoshell.config_default import PANEL_SPAWN
from indigoshell.widgets.base import Insets
from indigoshell.widgets.holo import (RASTER_PITCH, SCAN_EVERY_S,
                                      SCAN_HEAD_PX, SCAN_SPEED, SCAN_WAKE_PX)
from indigoshell.widgets.label import Label
from indigoshell.widgets.layout import Box, Column
from indigoshell.window import Layer, WindowSpec

W, H = 420, 240


def panel() -> Box:
    return Box(
        Column([Label(f"line {i}: the quick brown fox", color=theme.HUD_FG)
                for i in range(6)], spacing=8),
        padding=Insets.all(20),
        background=theme.POPUP_BG,
        border=theme.POPUP_BORDER,
        border_width=2.0,
        bevel=theme.POPUP_BEVEL,
        bevel_corners=theme.POPUP_BEVEL_CORNERS,
    )


def raster_checks(win) -> tuple[bool, str]:
    """Lines on the plate's blank right margin, every RASTER_PITCH rows;
    nothing in the bleed margin or the cut top-right corner."""
    px = _live.pixels(win)
    r = win.content_rect
    x = int(r.right()) - 12                  # past the text, inside the frame
    rows = [px[y, x].astype(int) for y in range(int(r.top()) + 40,
                                                int(r.top()) + 40 + 3 * RASTER_PITCH)]
    on = [y % RASTER_PITCH == 0 for y in range(int(r.top()) + 40,
                                               int(r.top()) + 40 + 3 * RASTER_PITCH)]
    lit = [sum(p[:3]) for p, o in zip(rows, on) if o]
    dark = [sum(p[:3]) for p, o in zip(rows, on) if not o]
    lines = min(lit) > max(dark)
    bleed = px[1, 1][3] == 0 if win.bleed else True
    corner = px[int(r.top()) + 2, int(r.right()) - 2][3] == 0
    return (lines and bleed and corner,
            f"lines {lines} (lit {min(lit)} vs {max(dark)}), "
            f"bleed clear {bleed}, cut corner clear {corner}")


async def main() -> int:
    canvas = _live.canvas()
    spec = WindowSpec(name="holo", layer=Layer.OVERLAY, size=(W, H),
                      background="#00000000", effects=(PANEL_SPAWN,),
                      holo=True, content=panel())
    win = None
    bad = 0
    for label in ("first open", "opened again"):
        if win is None:
            win = _live.open_window(canvas, spec)
            _live.count_frames(win.holo)
        else:
            await _live.reopen(win)
        await asyncio.sleep(0.5)                # the spawn is over

        ok_raster, raster_note = raster_checks(win)
        settled = _live.pixels(win)

        # A pass, now: as if SCAN_EVERY_S had just come round.
        win.holo.epoch = time.monotonic() - SCAN_EVERY_S + 0.05
        win.holo._arm()
        win.holo.frames = 0
        await asyncio.sleep(0.05 + 0.8)
        mid = _live.pixels(win)
        drawn = int((abs(mid.astype(int) - settled.astype(int)).max(axis=2)
                     > 10).sum())
        span = (win.content_rect.height() + SCAN_WAKE_PX
                + 2 * 2.5 * SCAN_HEAD_PX) / SCAN_SPEED
        await asyncio.sleep(span - 0.8 + 0.6)   # its wake is off the bottom
        fps = win.holo.frames / span
        left, box = _live.stale(win, f"holo-{label.replace(' ', '-')}")

        good = ok_raster and drawn > 500 and fps > 24 and left == 0
        bad += not good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: {raster_note}; "
              f"pass drew {drawn} px at {fps:.0f} fps; "
              f"left after it {left}" + (f" in {box}" if box else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
