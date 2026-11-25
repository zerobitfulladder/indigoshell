"""How much of a panel's spawn effect is left to see when the panel first
shows?

    .venv/bin/python tests/spawn.py [--stall-ms 150]

A striped test panel with the panels' spawn effect opens six times on a
canvas of the test's own (`_live.Canvas`), which composes what was
damaged once the loop comes round, as the Wayland backend presents it.
Each present reads one column of the composed canvas through the panel
and notes how far through the spawn the window was. The first present
in which the column changed is when the panel became visible; the
spawn's progress then is what was lost before anyone could see it.

The first open is a new window, the rest the same window parked and
shown again. Three opens are plain; three hold the loop for
`--stall-ms` right after `show()` — whatever stands between showing a
window and its first tick (another window's heavy frame, a busy
compositor). The spawn's time is counted in its own frames
(`Window._advance_phase`), so the stall must cost it nothing: read off
the wall clock, a spawn whose loop was blocked for 150 ms came back
four-fifths played, and the panel was seen to pop in with the last
flicker of its effect.

Checked: the spawn is at most `SEEN_BY` along when the panel is first
seen, every time.
"""

import argparse
import asyncio
import sys
import time

import _live
import numpy as np
import skia

from indigoshell import config_default as config
from indigoshell.widgets.base import Insets, Size, Widget
from indigoshell.window import Anchor, Layer, Window, WindowSpec

W, H, LEFT, TOP = 420, 300, 60, 80
SEEN_BY = 0.30          # the spawn may be this far along when first seen


class Stripes(Widget):
    """Vertical stripes: a row torn sideways shows a different colour."""

    def measure(self, avail: Size) -> Size:
        return Size(W, H)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        for i in range(int(r.width() // 8) + 1):
            color = skia.ColorSetARGB(255, 40 + (i % 2) * 180, 20,
                                      120 + (i * 37) % 120)
            canvas.drawRect(skia.Rect.MakeXYWH(r.left() + i * 8, r.top(), 8,
                                               r.height()),
                            skia.Paint(Color=color))


async def one_open(canvas: _live.Canvas, win: Window, *, parked: bool,
                   stall_ms: int) -> bool:
    """Open the panel once and see when it was first seen."""
    column = skia.IRect.MakeXYWH(LEFT + W // 2 + 3, TOP, 1, H)
    before = canvas.pixels(column)[:, 0].astype(int)
    presents: list = []             # (when, the spawn's progress, column changed)

    def on_present(c: _live.Canvas) -> None:
        now = c.pixels(column)[:, 0].astype(int)
        changed = (np.abs(now - before).max(axis=1) > 12).mean() > 0.05
        presents.append((time.monotonic(), win._appear, bool(changed)))

    canvas.on_present = on_present
    if parked:
        win.unpark()
    else:
        win.create()
    t0 = time.monotonic()
    win.show()
    show_ms = (time.monotonic() - t0) * 1000
    if stall_ms:
        time.sleep(stall_ms / 1000)         # the loop held, the spawn shown
    await asyncio.sleep(0.7)
    canvas.on_present = None

    seen = next(((t, a) for t, a, changed in presents if changed), None)
    ok = seen is not None and seen[1] <= SEEN_BY
    kind = ("parked" if parked else "new") \
        + (f", the loop held {stall_ms} ms" if stall_ms else "")
    if seen is None:
        verdict = "the panel never showed on the canvas"
    else:
        verdict = (f"first seen {(seen[0] - t0) * 1000:.0f} ms after show(), "
                   f"the spawn {seen[1]:.0%} played")
    frames = " ".join(f"{(t - t0) * 1000:.0f}:{a:.2f}"
                      for t, a, _c in presents if a < 1.0)[:150]
    print(f"  {'ok  ' if ok else 'FAIL'} {kind}: show() took {show_ms:.0f} ms; {verdict}\n"
          f"       presents (ms:progress) {frames}", flush=True)

    done = asyncio.Event()
    win.dismiss(done.set)
    await done.wait()
    win.park()
    await asyncio.sleep(0.25)
    return ok


async def main(args) -> int:
    canvas = _live.canvas()
    spec = WindowSpec(
        name="test-spawn", layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.LEFT, size=(W, H),
        margin=Insets(top=TOP, left=LEFT), keep_alive=True,
        effects=(config.PANEL_SPAWN,), content=Stripes())
    win = Window(canvas.scene, spec, canvas.loop)
    bad = 0
    for i in range(3):
        bad += not await one_open(canvas, win, parked=i > 0, stall_ms=0)
    for _ in range(3):
        bad += not await one_open(canvas, win, parked=True, stall_ms=args.stall_ms)
    win.destroy()
    print("FAILED" if bad else "all passed")
    return 1 if bad else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stall-ms", type=int, default=150,
                        help="how long the loop is held after show()")
    sys.exit(asyncio.run(main(parser.parse_args())))
