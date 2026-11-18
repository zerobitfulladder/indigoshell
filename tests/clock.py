"""The frame clock by itself, with plain widgets: each at its own rate,
starting and stopping, speeding up, the settle frame, surviving a widget
that raises, and a window opened for the second time.

    .venv/bin/python tests/clock.py
"""

import asyncio
import logging
import sys
import time

import _live
import skia

from indigoshell.widgets.base import Insets, Size, Widget
from indigoshell.window import Anchor, Layer, WindowSpec

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}", flush=True)
    if not ok:
        FAILED.append(name)


class Box(Widget):
    """A coloured box that records its frames. `burst(s)` animates it at
    30fps for `s` seconds, during which it also draws a bar across its
    top — something that is only right to see mid-effect."""

    def __init__(self, fps: int = 0, color: int = 0xFF8030C0,
                 raises: str | None = None) -> None:
        super().__init__()
        self.base_fps = fps
        self.color = color
        self.until = 0.0
        self.frames: list[float] = []
        self.raises = raises        # "fps", "animate" or "paint"
        self.poke: Box | None = None

    @property
    def animation_fps(self) -> int:
        if self.raises == "fps":
            raise RuntimeError("animation_fps raised")
        return 30 if time.monotonic() < self.until else self.base_fps

    def burst(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds
        self.invalidate()

    def animate(self, t: float) -> None:
        self.frames.append(time.monotonic())
        if self.raises == "animate":
            raise RuntimeError("animate raised")

    def measure(self, avail: Size) -> Size:
        return Size(60, 40)

    def paint(self, canvas: skia.Canvas) -> None:
        if self.raises == "paint":
            canvas.save()
            canvas.clipRect(skia.Rect.MakeWH(1, 1))     # left on the stack
            raise RuntimeError("paint raised")
        canvas.drawRect(self.rect, skia.Paint(Color=self.color))
        if time.monotonic() < self.until:
            canvas.drawRect(skia.Rect.MakeXYWH(
                self.rect.left(), self.rect.top(), self.rect.width(), 8),
                skia.Paint(Color=0xFFFFFF00))
        elif self.poke is not None:
            # Damage raised from inside the last frame's paint.
            other, self.poke = self.poke, None
            other.color = 0xFF00FF00
            other.invalidate()

    def rate(self, since: float, span: float) -> float:
        return sum(1 for t in self.frames if since <= t < since + span) / span


class Strip(Widget):
    def __init__(self, boxes: list[Box]) -> None:
        super().__init__()
        self.boxes = boxes

    def children(self):
        return self.boxes

    def measure(self, avail: Size) -> Size:
        return Size(70 * len(self.boxes), 40)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        for i, box in enumerate(self.boxes):
            box.arrange(skia.Rect.MakeXYWH(rect.left() + 70 * i, rect.top(),
                                           60, 40))

    def paint(self, canvas: skia.Canvas) -> None:
        clip = canvas.getLocalClipBounds()
        for box in self.boxes:
            if box.rect.intersects(clip):
                box.paint(canvas)


def window(display, boxes: list[Box], keep_alive: bool = False):
    return _live.open_window(display, WindowSpec(
        name="test-clock", layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.RIGHT, size=(70 * len(boxes), 40),
        margin=Insets(right=_live.OFFSCREEN), override_redirect=True,
        focusable=False, keep_alive=keep_alive, content=Strip(boxes)))


async def main() -> int:
    display = _live.connect()

    print("each widget at its own rate")
    slow, mid, fx = Box(2), Box(12, 0xFF3060C0), Box(0, 0xFF206020)
    win = window(display, [slow, mid, fx])
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    fx.burst(1.0)
    await asyncio.sleep(2.0)
    check("a 2fps widget", 1.5 <= slow.rate(t0, 2.0) <= 2.5,
          f"{slow.rate(t0, 2.0):.1f}/s")
    check("a 12fps widget", 10.5 <= mid.rate(t0, 2.0) <= 13,
          f"{mid.rate(t0, 2.0):.1f}/s")
    check("a 30fps burst", 26 <= fx.rate(t0, 1.0) <= 32,
          f"{fx.rate(t0, 1.0):.1f}/s")
    after = [t - t0 for t in fx.frames if t > t0 + 1.0]
    check("one settle frame after it", len(after) == 1 and after[0] < 1.1,
          f"at {[round(x, 3) for x in after]}s")
    check("the clock falls back to 12fps", win._clock_fps == 12,
          f"{win._clock_fps}fps")
    n, box = _live.stale(win)
    check("nothing left over", n == 0, f"{n}px {box or ''}")
    win.destroy()

    print("an effect starts at once, whatever the clock was doing")
    fx = Box(0)
    win = window(display, [fx])
    await asyncio.sleep(0.3)
    check("an idle window's clock is stopped",
          win._clock_fps == 0 and win._frame_handle is None)
    t0 = time.monotonic()
    fx.burst(0.5)
    await asyncio.sleep(0.25)
    first = fx.frames[0] - t0 if fx.frames else 9.0
    check("first frame from a stopped clock", first < 0.05,
          f"{first * 1000:.0f}ms")
    await asyncio.sleep(0.6)
    check("...which stops again", win._clock_fps == 0)
    n, box = _live.stale(win)
    check("nothing left over", n == 0, f"{n}px {box or ''}")
    win.destroy()

    slow, fx = Box(1), Box(0, 0xFF206020)
    win = window(display, [slow, fx])
    await asyncio.sleep(0.35)               # between two 1fps ticks
    t0 = time.monotonic()
    fx.burst(0.4)
    await asyncio.sleep(0.3)
    first = fx.frames[0] - t0 if fx.frames else 9.0
    check("first frame under a 1fps clock", first < 0.05,
          f"{first * 1000:.0f}ms")
    check("...which speeds up", win._clock_fps == 30, f"{win._clock_fps}fps")
    win.destroy()

    fx = Box(1)
    win = window(display, [fx])
    await asyncio.sleep(0.4)
    t0 = time.monotonic()
    fx.burst(0.4)
    await asyncio.sleep(0.5)
    got = sum(1 for t in fx.frames if t0 <= t < t0 + 0.4)
    check("a 1fps widget that speeds up does not wait out its slot",
          got >= 10, f"{got} frames in 0.4s")
    win.destroy()

    print("a stall that swallows the end of an effect")
    fx = Box(0)
    win = window(display, [fx])
    await asyncio.sleep(0.3)
    fx.burst(0.3)
    await asyncio.sleep(0.1)                # painted mid-effect...
    time.sleep(0.6)                         # ...and the loop blocks past its end
    await asyncio.sleep(0.15)
    n, box = _live.stale(win)
    check("the widget at rest is what is on screen", n == 0,
          f"{n}px {box or ''}")
    check("and the clock has stopped", win._clock_fps == 0)
    win.destroy()

    print("damage raised while the last frame paints")
    a, b = Box(0), Box(0, 0xFF303030)
    win = window(display, [a, b])
    await asyncio.sleep(0.3)
    a.poke = b
    a.burst(0.2)
    await asyncio.sleep(0.6)
    n, box = _live.stale(win)
    check("is painted, though the clock was stopping", n == 0,
          f"{n}px {box or ''}")
    win.destroy()

    print("a widget that raises")
    logging.getLogger("indigoshell").setLevel(logging.CRITICAL)
    for where in ("fps", "animate", "paint"):
        good = Box(12)
        bad = Box(0 if where == "fps" else 12, raises=where)
        win = window(display, [good, bad])
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        await asyncio.sleep(1.0)
        rate = good.rate(t0, 1.0)
        check(f"in {where}: its neighbour keeps its frames",
              10 <= rate <= 13 and win._frame_handle is not None,
              f"{rate:.1f}/s")
        win.destroy()
    logging.getLogger("indigoshell").setLevel(logging.INFO)

    print("a window opened for the second time")
    fx = Box(12)
    win = window(display, [fx], keep_alive=True)
    await asyncio.sleep(2.5)                # the first open, a while
    await _live.reopen(win)
    t0 = time.monotonic()
    await asyncio.sleep(1.0)
    check("animates from its first second", 10.5 <= fx.rate(t0, 1.0) <= 13,
          f"{fx.rate(t0, 1.0):.1f}/s")
    win.destroy()

    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
