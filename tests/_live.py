"""Shared by the live tests: the real `Window` and the real widgets, on
an X window placed off the screen.

These are scripts, not a test suite to collect: they need the X display
and the GPU the shell itself runs on, and are run by hand —

    .venv/bin/python tests/clock.py           the frame clock alone
    .venv/bin/python tests/call.py            the call overlay, fuzzed
    .venv/bin/python tests/call_status.py     the hung-up status chip
    .venv/bin/python tests/call_progress.py   progress bars on the call
    .venv/bin/python tests/spawn.py           spawn timing (shows a panel)

Each prints what it checked and exits non-zero if something failed.
`tests/progress.py` needs no display: it runs yt-dlp, ffmpeg, curl and
rsync through the progress shims.
Nothing appears on screen, `spawn.py` aside: the windows are
override-redirect, past the right edge of the screen, and take no grabs.
The shell can be running meanwhile. The tests never touch its socket,
its windows or its saved state — `XDG_STATE_HOME` points at a temporary
directory before anything of the shell's is imported.

**Why they run in one long-lived process.** The bug that froze the
call's effects only existed on a window opened for the second time: the
widgets outlive an open, and what they remembered from the first made
them idle through the next. Every check made on a fresh process passed.
So the tests that matter here open, close, park and open again
(`reopen`).

**The oracle most of them share** is `stale()`. Once everything timed
has ended, what is in the window's surface must be exactly what a forced
full repaint draws. Any pixel that differs was left over from a frame
that should have been painted over — a reply half decoded, a blink
caught dim, a glow nobody cleared.
"""

import asyncio
import dataclasses
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="indigoshell-test-state-")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import numpy as np                                      # noqa: E402
import skia                                             # noqa: E402

from indigoshell.agent.session import Agent             # noqa: E402
from indigoshell.backend.x11 import Display             # noqa: E402
from indigoshell.widgets.base import Insets             # noqa: E402
from indigoshell.window import Window, WindowSpec       # noqa: E402

# Far enough right that the widest window here is wholly off the screen.
OFFSCREEN = -1400

_shots: str | None = None       # where `stale()` saves what it found


def connect() -> Display:
    """The X display, pumped by the running loop as the daemon does."""
    loop = asyncio.get_running_loop()
    display = Display()
    display.attach_loop(loop)

    def pump() -> None:
        while display.pump():
            pass

    loop.add_reader(display.fd, pump)
    return display


def offscreen(spec: WindowSpec, **changes) -> WindowSpec:
    """`spec` as a test window: off the screen, no grabs."""
    margin = Insets(top=spec.margin.top, right=OFFSCREEN)
    return dataclasses.replace(
        spec, name=f"test-{spec.name}", margin=margin,
        grab_keyboard=False, dismiss_on_outside_click=False, **changes)


def open_window(display: Display, spec: WindowSpec) -> Window:
    win = Window(display, spec, asyncio.get_running_loop())
    win.create()
    win.show()
    display.pump()
    return win


async def reopen(win: Window, closed_for: float = 0.2) -> None:
    """What the daemon does with a keep-alive window: despawn it, park
    it, then map the same window and the same widgets again."""
    done = asyncio.Event()
    win.dismiss(done.set)
    await done.wait()
    win.park()
    await asyncio.sleep(closed_for)
    win.unpark()
    win.show()
    win.display.pump()


def pixels(win: Window) -> np.ndarray:
    """The window's surface as it stands, RGBA rows."""
    if win.gl is not None:
        win.gl.make_current(win.gl_surface)
    image = win.surface.makeImageSnapshot().makeRasterImage()
    return np.array(image.toarray(), copy=True)


def stale(win: Window, tag: str = "") -> tuple[int, tuple | None]:
    """How many pixels of the surface differ from a forced full repaint,
    and the box they are in. With a `tag`, both versions of that box are
    saved as PNGs for a look."""
    global _shots
    before = pixels(win)
    win._damage_all = True
    win.redraw()
    after = pixels(win)
    differs = np.abs(before.astype(int) - after.astype(int)).max(axis=2) > 10
    count = int(differs.sum())
    if not count:
        return 0, None
    ys, xs = np.nonzero(differs)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    if tag:
        if _shots is None:
            _shots = tempfile.mkdtemp(prefix="indigoshell-test-shots-")
            print(f"    (what was left on screen, and its repaint: {_shots})")
        pad = 12
        x0, y0 = max(0, box[0] - pad), max(0, box[1] - pad)
        x1 = min(before.shape[1], box[2] + pad)
        y1 = min(before.shape[0], box[3] + pad)
        for name, shot in (("left", before), ("repaint", after)):
            crop = np.ascontiguousarray(shot[y0:y1, x0:x1])
            skia.Image.fromarray(crop).save(
                os.path.join(_shots, f"{tag}-{name}.png"), skia.kPNG)
    return count, box


def count_frames(widget) -> None:
    """Count `widget.animate()` calls in `widget.frames`."""
    widget.frames = 0
    animate = widget.animate

    def counted(t: float) -> None:
        widget.frames += 1
        animate(t)

    widget.animate = counted


class ScriptedAgent(Agent):
    """The agent's real turn bookkeeping with no CLI behind it: the
    stream is whatever the test feeds in."""

    def dial(self) -> None:
        self._client = object()
        if self.phase in ("offline", "dialing"):
            self.phase = "idle"

    async def _send(self, line: str) -> None:
        return

    def _schedule_idle(self) -> None:
        return

    def think_start(self) -> None:
        self._on_stream({"type": "content_block_start",
                         "content_block": {"type": "thinking"}})

    def think(self, text: str) -> None:
        self._on_stream({"type": "content_block_delta",
                         "delta": {"type": "thinking_delta",
                                   "thinking": text}})

    def text_start(self) -> None:
        self._on_stream({"type": "content_block_start",
                         "content_block": {"type": "text"}})

    def text(self, text: str) -> None:
        self._on_stream({"type": "content_block_delta",
                         "delta": {"type": "text_delta", "text": text}})

    def tool(self, tool_id: str, command: str) -> None:
        self._on_tool(types.SimpleNamespace(
            name="Bash", input={"command": command}, id=tool_id), sub=False)

    def result(self, tool_id: str, output: str, failed: bool = False) -> None:
        self._on_result(types.SimpleNamespace(
            tool_use_id=tool_id, content=output, is_error=failed))

    def end(self) -> None:
        self._end_turn(types.SimpleNamespace(is_error=False, result="",
                                             session_id=None))
