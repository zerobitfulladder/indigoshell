"""How much of a panel's spawn effect is left to see when the panel first
shows on screen?

    .venv/bin/python tests/spawn.py [--held-ms 110]

**This one is visible**: a striped test panel opens in the top-left
corner six times, for under a second each. Keep that corner still while
it runs — the test finds the panel by watching the screen change there.

A second X connection reads one column of the root window about a
thousand times a second while the window logs each frame it presents
and how far through the spawn it was. The moment the column
first changes is when the panel became visible; the spawn's progress at
that moment is what was lost before anyone could see it.

Three opens are plain. Three pretend the keyboard grab is refused for
`--held-ms`, which is what a window opened from a keybinding gets: the
window manager's own grab on the key lasts until the key comes up. No
grab is really taken — the request's reply is faked. That case is the
one that used to fail: the retries slept through the held key with the
spawn already counting, and the panel was first seen four-fifths played.

Two things are checked. The spawn is at most `SEEN_BY` along when the
panel is first seen. And a refused grab does not hold up `show()`: it
returns about as fast as in the plain opens, not once the key is up.
"""

import argparse
import asyncio
import dataclasses
import sys
import threading
import time

import _live
import numpy as np
import skia
import xcffib
import xcffib.xproto as xp

from indigoshell import config_default as config
from indigoshell.widgets.base import Insets, Size, Widget
from indigoshell.window import Anchor, Layer, Window, WindowSpec

W, H, LEFT, TOP = 420, 300, 60, 80
SEEN_BY = 0.30          # the spawn may be this far along when first seen
GRAB_REFUSED, GRAB_OK = 1, 0


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


class Logged(Window):
    """A window that notes each frame it presents: (when, appear)."""

    presented: list | None = None

    def redraw(self) -> None:
        super().redraw()
        if self.presented is not None:
            self.presented.append((time.monotonic(), self._appear))


def watch_column(stop: threading.Event, out: list, x: int) -> None:
    """Read a 1px column of the screen, over and over, on a connection
    of its own."""
    conn = xcffib.connect()
    root = conn.get_setup().roots[0].root
    while not stop.is_set():
        reply = conn.core.GetImage(xp.ImageFormat.ZPixmap, root, x, TOP, 1, H,
                                   0xFFFFFFFF).reply()
        column = np.frombuffer(bytes(reply.data), np.uint8)
        out.append((time.monotonic(),
                    column.reshape(H, 4)[:, :3].astype(int)))
        time.sleep(0.0005)      # leave the interpreter to the window
    conn.disconnect()


def refuse_grab_for(display, seconds: float):
    """Make keyboard grabs report "already grabbed" until `seconds` from
    now, without sending anything. Returns the undo."""
    until = time.monotonic() + seconds
    core = display.conn.core
    real = core.GrabKeyboard

    class Reply:
        def reply(self):
            held = time.monotonic() < until
            return type("GrabReply", (), {
                "status": GRAB_REFUSED if held else GRAB_OK})()

    core.GrabKeyboard = lambda *args, **kwargs: Reply()

    def undo() -> None:
        core.GrabKeyboard = real

    return undo


async def one_open(display, win: Logged, *, parked: bool, held_ms: int,
                   plain_ms: float = 0.0) -> tuple[bool, float]:
    """Open the panel once. `plain_ms` is how long `show()` took without
    a refused grab, for the opens that have one. Returns (passed, how
    long `show()` took)."""
    samples: list = []
    stop = threading.Event()
    watcher = threading.Thread(target=watch_column,
                               args=(stop, samples, LEFT + W // 2 + 3))
    watcher.start()
    await asyncio.sleep(0.15)               # the screen before the panel
    still = len(samples)
    undo = refuse_grab_for(display, held_ms / 1000) if held_ms else None
    win.spec = dataclasses.replace(win.spec, grab_keyboard=bool(held_ms))
    win.presented = []
    if parked:
        win.unpark()
    else:
        win.create()
    t0 = time.monotonic()
    win.show()
    show_ms = (time.monotonic() - t0) * 1000
    display.pump()
    await asyncio.sleep(0.7)
    stop.set()
    watcher.join()
    if undo is not None:
        undo()
    win._kb_grabbed = False                 # it never held a real one

    before = samples[0][1]
    moving = any((np.abs(s - before).max(axis=1) > 12).mean() > 0.05
                 for _t, s in samples[:still])
    seen = next((t for t, s in samples[still:]
                 if (np.abs(s - before).max(axis=1) > 12).mean() > 0.05), None)
    progress = None
    if seen is not None:
        progress = 0.0
        for when, appear in win.presented:
            if when > seen:
                break
            progress = appear
    frames = " ".join(f"{(t - t0) * 1000:.0f}:{a:.2f}"
                      for t, a in win.presented if a < 1.0)[:150]
    # Half the hold on top of a plain open: the old retries slept the
    # whole of it, and then some.
    blocked = bool(held_ms) and show_ms > plain_ms + held_ms / 2
    ok = (progress is not None and not moving and not blocked
          and (not parked or progress <= SEEN_BY))
    kind = ("parked" if parked else "new")\
        + (f", key held {held_ms}ms" if held_ms else "")
    if moving:
        verdict = "something else is moving in that corner — not measured"
    elif seen is None:
        verdict = "the panel never showed in the screen's pixels"
    else:
        verdict = (f"first seen {(seen - t0) * 1000:.0f}ms after show(), "
                   f"the spawn {progress:.0%} played")
    print(f"  {'ok  ' if ok else 'FAIL'} {kind}: show() took {show_ms:.0f}ms"
          f"{' — held up by the grab' if blocked else ''}; {verdict}\n"
          f"       frames (ms:progress) {frames}", flush=True)

    done = asyncio.Event()
    win.dismiss(done.set)
    await done.wait()
    win.park()
    await asyncio.sleep(0.25)
    return ok, show_ms


async def main(args) -> int:
    display = _live.connect()
    spec = WindowSpec(
        name="test-spawn", layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.LEFT, size=(W, H),
        margin=Insets(top=TOP, left=LEFT), override_redirect=True,
        focusable=False, keep_alive=True, effects=(config.PANEL_SPAWN,),
        content=Stripes())
    win = Logged(display, spec, asyncio.get_running_loop())
    bad = 0
    plain = []
    for i in range(3):
        ok, show_ms = await one_open(display, win, parked=i > 0, held_ms=0)
        bad += not ok
        if i:
            plain.append(show_ms)
    for _ in range(3):
        ok, _ms = await one_open(display, win, parked=True,
                                 held_ms=args.held_ms, plain_ms=max(plain))
        bad += not ok
    win.destroy()
    display.pump()
    print("FAILED" if bad else "all passed")
    return 1 if bad else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--held-ms", type=int, default=110,
                        help="how long the pretend key stays down")
    sys.exit(asyncio.run(main(parser.parse_args())))
