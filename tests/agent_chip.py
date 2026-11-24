"""The top bar's agent chip (`AgentChip`, agent/ui/status.py) and what
`CallRouter` tells it.

    .venv/bin/python tests/agent_chip.py              all of it, ~15 s
    .venv/bin/python tests/agent_chip.py --off-screen 1-3 only: no window

1. The state, from a stand-in agent through a real `CallRouter` on a
   stub daemon (no X): idle (dimmed, the voice of the turn on show, no
   frames) before any turn; working (name, the voice's colour, the
   light's blink) while a turn runs, the call open or not; idle again
   when the turn ends without a ring, keeping the voice that worked; the
   whole chip blinking on a ring or a question, with no `call-status`
   opened for either; the ring ended by the hub's `seen`, by answering
   (the call opened) or by a right-click; a turn in the other voice
   changing the name and colour without restarting the blink;
   `show_working=True` keeping its corner plate while working (the chip
   working under it) but closing it for a ring; a calling router before
   a working one, a working one before an idle one; the router stopped;
   a left click opening the call; no router at all: nothing.
2. The blinks as numbers: the light at 1 Hz on a 2 fps grid, the ring
   pattern on a 20 fps grid, read the same by a frame a little early or
   late.
3. Layout: with no router the chip takes no width and no gap in the
   bar's row (`collapsed`); with one, idle or not, it is at the row's
   right end with the bar's gap before it; whole pixels; painted off
   screen (raster): the colour, the light's blink, the ring's flash and
   dark, idle dimmed with its light out, the call chip's stripes;
   the user's config.py (by its path, scratch XDG, no bytecode) has the
   chip at its top bar's right end and finds its router in its SERVICES.
4. The real `Window` and frame clock, on an X window off the screen
   (override-redirect, no grab, never mapped where it shows), opened
   twice: no frame at all over an idle second while idle; the light's
   2 frames a second while working, and nothing else of the chip; the
   ring's 20 while calling; none again once idle; and nothing left on
   screen by any of it (`_live.stale`).

Prints ok/FAIL lines and exits 1 if anything failed. Never touches the
running shell: no socket, lock, state or window of its.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import types

import _live                                    # sets XDG_STATE_HOME first

import skia

from indigoshell import theme
from indigoshell.agent.ui import status
from indigoshell.agent.ui.status import (CALL_BLINK, CHIP_H, WORK_BLINK,
                                         AgentChip, CallRouter, _blink_fps,
                                         _blink_lit)
from indigoshell.core import daemon as daemon_mod
from indigoshell.widgets.base import Size
from indigoshell.widgets.label import Label
from indigoshell.widgets.layout import Align, Row

REPO = _live.REPO
FAILED: list[str] = []
LILAC, INDIGO = "#c8a2c8", "#b967ff"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── stand-ins ───────────────────────────────────────────────────────────

class StubDaemon:
    """What the router and the chip use of the daemon: its open windows
    by name, opening and closing them (recorded), its config."""

    def __init__(self, services=()) -> None:
        self.instances: dict = {}
        self.config = {"services": list(services)}
        self.opened: list[str] = []
        self.closed: list[str] = []

    def open(self, name, params=None, **_kw):
        self.opened.append(name)
        self.instances[name] = object()
        return name

    def close(self, name):
        self.closed.append(name)
        return self.instances.pop(name, None) is not None


class StandIn:
    """What the router and the chip read of an agent (`RemoteAgent`'s
    attributes): its turn, its ring, its question, its voice."""

    def __init__(self, name="INDIGO", accent=INDIGO) -> None:
        self.name, self.accent = name, accent
        self.busy = self.call_open = False
        self.ringing = self.approval = None
        self.jobs = types.SimpleNamespace(shown=lambda: [])
        self._listeners = []

    def subscribe(self, fn):
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, event="change"):
        for fn in list(self._listeners):
            fn(event)

    def set(self, **values):
        for k, v in values.items():
            setattr(self, k, v)
        self._emit()


class FakeWindow:
    """A window that only records damage."""

    def __init__(self) -> None:
        self.damaged: list = []

    def damage(self, widget=None, *, layout: bool = False) -> None:
        self.damaged.append((widget, layout))


def rig(*, show_working=False, call="call", stat="call-status"):
    d = StubDaemon()
    daemon_mod._DAEMON = d
    agent = StandIn()
    router = CallRouter(agent, call=call, status=stat, show_working=show_working)
    d.config["services"].append(router)
    router.start()
    chip = AgentChip()                  # finds the router in the daemon's SERVICES
    win = FakeWindow()
    chip.attach(win)
    return d, agent, router, chip, win


def look(chip) -> tuple:
    return (chip.state, chip.shown[2] if chip.shown else None,
            chip.shown[3] if chip.shown else None)


# ── 1. state ────────────────────────────────────────────────────────────

def test_state() -> None:
    section("1. what the chip shows, from the router")
    d, agent, router, chip, win = rig()
    check("found its router among the daemon's SERVICES", chip.routers == [router])
    check("before any turn: idle, dimmed, the voice the agent speaks with, no frames",
          look(chip) == ("idle", "INDIGO", INDIGO) and not chip.collapsed
          and chip.level() == status.IDLE_DIM and chip.measure(Size(500, 42)).width > 0
          and chip.animation_fps == 0 and chip.light.animation_fps == 0, f"{look(chip)}")

    agent.set(busy=True)
    check("a turn runs: working, the voice's name and colour, full strength",
          look(chip) == ("working", "INDIGO", INDIGO) and chip.level() == 1.0
          and win.damaged and win.damaged[-1] == (chip, False), f"{look(chip)}")
    check("... the light asks 2 frames a second, the chip none",
          chip.light.animation_fps == 2 and chip.animation_fps == 0,
          f"{chip.light.animation_fps}/{chip.animation_fps}")
    since = chip.since
    n = len(win.damaged)
    agent.set()                         # the agent's stream: nothing on show changes
    check("an agent event that changes nothing on show: no repaint", len(win.damaged) == n)

    agent.set(name="LILAC", accent=LILAC)
    check("a turn in the other voice: its name and colour, the blink carrying on",
          look(chip) == ("working", "LILAC", LILAC) and chip.since == since)

    agent.set(call_open=True)
    check("the call opens mid-turn: still working (always on the bar)",
          chip.state == "working" and chip.light.animation_fps == 2)
    d.instances["call"] = object()
    agent.set(call_open=False)
    check("the call window open: still working", chip.state == "working")
    d.instances.pop("call")
    agent.set()

    agent.set(busy=False)
    check("the turn ends without a ring: idle, dimmed, the voice that worked, no frames",
          look(chip) == ("idle", "LILAC", LILAC) and chip.animation_fps == 0
          and chip.light.animation_fps == 0)

    agent.set(ringing="done", name="INDIGO", accent=INDIGO)
    check("a ring: calling, the whole chip blinking at 20 fps, the light steady",
          look(chip) == ("calling", "INDIGO", INDIGO) and chip.animation_fps == 20
          and chip.light.animation_fps == 0)
    check("... and no status window opened for it", d.opened == [] and "call-status"
          not in d.instances, f"{d.opened}")
    agent.set(ringing=None)
    check("the hub's `seen` (the ring stops): idle", chip.state == "idle")

    agent.set(ringing="done")
    agent.set(call_open=True, ringing=None)
    check("answered with MOD+A (the call opens): idle", chip.state == "idle")
    agent.set(call_open=False)

    agent.set(busy=True)
    agent.set(approval=object(), ringing="ask")
    check("a question mid-turn, the call closed: calling, over working",
          chip.state == "calling")
    agent.set(approval=None, ringing=None)
    check("... answered elsewhere, the turn still running: working again",
          chip.state == "working")
    agent.set(call_open=True, approval=object())
    check("a question with the call open: working (the call asks it)",
          chip.state == "working")
    agent.set(call_open=False, approval=None, busy=False)

    agent.set(ringing="error")
    check("a ring for a turn that failed: calling too", chip.state == "calling")
    clicked = chip.click(3)
    check("a right-click silences it, as the corner plate's did: idle",
          clicked and agent.ringing is None and chip.state == "idle")

    check("a left click, idle, opens the call",
          chip.click(1) and d.opened == ["call"], f"{d.opened}")
    d.instances.pop("call")
    agent.set()
    check("no status window opened by anything in all that (show_working=False)",
          "call-status" not in d.opened, f"{d.opened}")

    agent.set(busy=True)
    router.stop()
    check("the router stopped: idle", chip.state == "idle")
    chip.detach()
    check("detached: following nothing", router._followers == [] and chip.routers == [])

    # show_working=True: the desktop's own agent, its corner plate.
    d, agent, router, chip, win = rig(show_working=True)
    agent.set(busy=True)
    check("show_working=True, working: the corner plate opens, the chip shows the work "
          "under it", d.opened == ["call-status"] and chip.state == "working", f"{d.opened}")
    agent.set(approval=object(), ringing="ask")
    check("... a question: the plate closes and the chip blinks",
          d.closed == ["call-status"] and chip.state == "calling", f"{d.closed}")
    agent.set(approval=None, ringing=None)
    check("... answered: the plate back, the chip working",
          d.opened == ["call-status"] * 2 and chip.state == "working")
    agent.set(busy=False, ringing="done")
    check("... the turn ends ringing: the plate closes, the chip blinks",
          "call-status" not in d.instances and chip.state == "calling")
    chip.detach()
    router.stop()

    # Several routers: calling, then working, then the first.
    d = StubDaemon()
    daemon_mod._DAEMON = d
    a1, a2 = StandIn(), StandIn("LILAC", LILAC)
    r1 = CallRouter(a1, call="call", status="s1", show_working=False)
    r2 = CallRouter(a2, call="call2", status="s2", show_working=False)
    for r in (r1, r2):
        d.config["services"].append(r)
        r.start()
    chip = AgentChip()
    chip.attach(FakeWindow())
    a1.set(busy=True)
    a2.set(ringing="done")
    check("two calls: the one calling back shows over the one working",
          look(chip) == ("calling", "LILAC", LILAC))
    a2.set(ringing=None)
    check("... then the working one", look(chip) == ("working", "INDIGO", INDIGO))
    a1.set(busy=False)
    a2.set(busy=True)
    check("... whichever works", look(chip) == ("working", "LILAC", LILAC))
    a2.set(busy=False)
    check("... both idle: the first", look(chip) == ("idle", "INDIGO", INDIGO))
    chip.detach()

    daemon_mod._DAEMON = None
    lone = AgentChip()
    lone.attach(FakeWindow())
    check("no daemon (or no router in it): nothing, collapsed, following nothing",
          lone.routers == [] and lone.shown is None and lone.collapsed
          and lone.measure(Size(500, 42)) == Size(0, 0))
    lone.detach()


# ── 2. blinks ───────────────────────────────────────────────────────────

def test_blinks() -> None:
    section("2. the blinks")
    check("frames a second: the light 2 (500ms steps), the ring 20 (50ms steps)",
          _blink_fps(WORK_BLINK) == 2 and _blink_fps(CALL_BLINK) == 20,
          f"{_blink_fps(WORK_BLINK)}, {_blink_fps(CALL_BLINK)}")
    light = [_blink_lit(WORK_BLINK, i * 0.5) for i in range(6)]
    check("the light: lit, dim, lit, ... a step every 0.5 s", light == [True, False] * 3,
          f"{light}")
    jitter = [_blink_lit(WORK_BLINK, i * 0.5 + d) for i in range(6) for d in (-0.2, 0.2)]
    check("... a frame up to 200ms early or late reads its own step",
          jitter == [v for v in [True, False] * 3 for _ in (0, 1)])
    ring = "".join("#" if _blink_lit(CALL_BLINK, i / 20) else "." for i in range(44))
    want = ("###" "..") + ("###") + ("." * 14)
    check("the ring: 150 lit, 100 dark, 150 lit, 700 dark, looped (the bar's urgent "
          "workspace pattern)", ring == want * 2, ring)
    check("... a frame 20ms off reads its own step",
          all(_blink_lit(CALL_BLINK, i / 20 + d) == _blink_lit(CALL_BLINK, i / 20)
              for i in range(44) for d in (-0.02, 0.02)))


# ── 3. layout and paint ─────────────────────────────────────────────────

USER_CONFIG_CHILD = r"""
import importlib.util, json, sys, types
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("user_config_under_test", sys.argv[2])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from indigoshell import config_default as cd
from indigoshell.agent.ui.status import AgentChip, CallRouter
from indigoshell.core import daemon
top = next(w for w in module.WINDOWS if w.name == cd.TOP_BAR)
row = list(top.content.children())
chip = row[-1]
daemon._DAEMON = types.SimpleNamespace(config={"services": module.SERVICES}, instances={})
class W:
    def damage(self, *a, **k): pass
chip.attach(W())
routers = [s for s in module.SERVICES if isinstance(s, CallRouter)]
print(json.dumps({"last": type(chip).__name__, "routers": len(chip.routers),
                  "same": chip.routers == routers,
                  "show_working": [r.show_working for r in chip.routers],
                  "agent": type(chip.routers[0].agent).__name__ if chip.routers else None}))
chip.detach()
"""


def test_layout(scratch: str) -> None:
    section("3. layout, paint, the user's config")
    W, H = 600, theme.TOP_BAR_HEIGHT
    daemon_mod._DAEMON = None
    none = AgentChip()
    none.attach(FakeWindow())
    other = Label("MINE", color=theme.CYAN_BRIGHT)
    row = Row([other, none], spacing=theme.BAR_SPACING, align=Align.CENTER)
    alone = Row([Label("MINE")], spacing=theme.BAR_SPACING)
    check("no agent configured: the row measures as if the chip were not there (no gap)",
          row.measure(Size(W, H)).width == alone.measure(Size(W, H)).width)
    row.arrange(skia.Rect.MakeWH(W, H))
    check("... and arranges so: the chip has no width",
          none.rect.width() == 0 and none.rect.left() == other.rect.right())
    none.detach()

    d, agent, router, chip, _w = rig()
    other = Label("MINE", color=theme.CYAN_BRIGHT)
    row = Row([other, chip], spacing=theme.BAR_SPACING, align=Align.CENTER)
    m = row.measure(Size(W, H))
    row.arrange(skia.Rect.MakeXYWH(0, 0, m.width, H))
    check("an agent, idle: the chip there all the same, at the row's end",
          chip.state == "idle" and chip.rect.width() > 0
          and abs(chip.rect.left() - other.rect.right() - theme.BAR_SPACING) < 1e-6)
    agent.set(busy=True)
    m = row.measure(Size(W, H))
    row.arrange(skia.Rect.MakeXYWH(0, 0, m.width, H))
    r = chip.rect
    check("shown: after the bar's gap, whole pixels, CHIP_H tall and centred in the bar",
          abs(r.left() - other.rect.right() - theme.BAR_SPACING) < 1e-6
          and r.width() == int(r.width()) and r.height() == CHIP_H
          and r.top() == (H - CHIP_H) / 2 == int(r.top()), f"{r}")
    check("... the light inside its right end, its glow within the chip",
          chip.light.rect.right() < r.right() and r.contains(chip.light.paint_bounds()))
    check("hit by its beveled shape: the middle, not the cut top-right corner",
          chip.hit(r.centerX(), r.centerY()) is chip
          and chip.hit(r.right() - 1, r.top() + 1) is None)

    def painted():
        surface = skia.Surface(int(m.width), H)
        canvas = surface.getCanvas()
        canvas.clear(theme.color(theme.BAR_BG))
        row.paint(canvas)
        return surface.makeImageSnapshot().toarray()

    def colour_px(img, hex_, tol=24):
        rgb = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
        box = img[:, int(r.left()):int(r.right()), :3].astype(int)
        # toarray is BGRA here, RGBA elsewhere: compare both ways
        a = (abs(box - rgb).max(-1) <= tol).sum()
        b = (abs(box[..., ::-1] - rgb).max(-1) <= tol).sum()
        return int(max(a, b))

    agent.set(ringing=None)
    chip.since = time.monotonic()
    chip.light.animate(0)
    lit = painted()
    chip.since = time.monotonic() - 0.5
    chip.light.animate(0)
    dim = painted()
    agent.set(ringing="done")
    chip.since = time.monotonic()
    chip.animate(0)
    flash = painted()
    chip.since = time.monotonic() - 0.2
    chip.animate(0)
    dark = painted()
    on, off = colour_px(lit, INDIGO), colour_px(dim, INDIGO)
    check("painted (raster): the voice's colour on the chip (frame, name, light), less of "
          "it with the light dim, and almost none between the ring's flashes",
          on > 100 and off < on and colour_px(flash, INDIGO) > 100
          and colour_px(dark, INDIGO) < 10,
          f"{on} / {off} px; flash {colour_px(flash, INDIGO)} / {colour_px(dark, INDIGO)}")
    # Between the hairline and the name, above the bottom-left cut.
    plate = lit[int(r.top()) + 7:int(r.bottom()) - 11, int(r.left()) + 7:int(r.left()) + 10, :3]
    rows = plate.astype(int).sum(axis=(1, 2))
    check("... the call chip's raster across its plate: rows alternate in brightness, a "
          "line every 3 px", len(rows) >= 6 and len(set(int(v) for v in rows)) > 1
          and all(rows[i] == rows[i + 3] for i in range(len(rows) - 3)), f"{list(rows)}")
    agent.set(ringing=None)
    agent.set(busy=False)
    idle = painted()

    def ink(img):
        """How far the chip's box is from the bar's black, summed."""
        bg = [int(theme.BAR_BG[i:i + 2], 16) for i in (1, 3, 5)]
        box = img[:, int(r.left()):int(r.right()), :3].astype(int)
        return int(min(abs(box - bg).sum(), abs(box[..., ::-1] - bg).sum()))
    check("idle: the same chip there, dimmed (well under half the ink of working)",
          chip.state == "idle" and 0 < ink(idle) < 0.5 * ink(dim),
          f"ink {ink(idle)} idle, {ink(dim)} working")
    chip.detach()

    from indigoshell.core.paths import config_dir
    path = os.path.join(config_dir(), "config.py")
    if not os.path.isfile(path):
        print("  (no user config.py on this machine; its check skipped)", flush=True)
        return
    child = os.path.join(scratch, "user_config_child.py")
    with open(child, "w") as f:
        f.write(USER_CONFIG_CHILD)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               **{k: os.path.join(scratch, k.lower()) for k in
                  ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")})
    p = subprocess.run([sys.executable, "-B", child, REPO, path], capture_output=True,
                       text=True, env=env, timeout=120)
    lines = p.stdout.strip().splitlines()
    out = json.loads(lines[-1]) if p.returncode == 0 and lines else {}
    check("the user's config.py: AgentChip at its top bar's right end, following the "
          "one CallRouter in its SERVICES",
          out.get("last") == "AgentChip" and out.get("routers") == 1 and out.get("same"),
          f"{out or p.stderr.strip()[-300:]}")


# ── 4. the frame clock ──────────────────────────────────────────────────

async def test_clock() -> None:
    from indigoshell.window import Anchor, Layer, WindowSpec

    section("4. frames, on the real Window and clock (off screen, no grab)")
    d = StubDaemon()
    daemon_mod._DAEMON = d
    agent = StandIn()
    router = CallRouter(agent, show_working=False)
    d.config["services"].append(router)
    router.start()
    chip = AgentChip()
    _live.count_frames(chip)
    _live.count_frames(chip.light)
    spec = WindowSpec(
        name="agent-chip", layer=Layer.OVERLAY, anchor=Anchor.TOP | Anchor.RIGHT,
        size=(560, theme.TOP_BAR_HEIGHT), override_redirect=True, focusable=False,
        background=theme.BAR_BG, keep_alive=True,
        content=Row([Label("github.com"), chip], spacing=theme.BAR_SPACING,
                    align=Align.CENTER))
    display = _live.connect()
    win = _live.open_window(display, _live.offscreen(spec))
    frames = {"n": 0}
    run = win._run_frame

    def counted():
        frames["n"] += 1
        return run()

    win._run_frame = counted

    async def measure(seconds: float) -> tuple[int, int, int]:
        chip.frames = chip.light.frames = frames["n"] = 0
        await asyncio.sleep(seconds)
        return frames["n"], chip.frames, chip.light.frames

    for label in ("first open", "opened again"):
        await asyncio.sleep(0.4)
        idle = await measure(1.0)
        check(f"{label}, idle: no frame at all over an idle second", idle == (0, 0, 0),
              f"window/chip/light {idle}")
        agent.set(busy=True)
        await asyncio.sleep(0.1)
        work = await measure(2.0)
        check(f"{label}, working: the light's 2 frames a second, the chip none of its own",
              3 <= work[2] <= 5 and work[1] == 0 and work[0] <= 6,
              f"window/chip/light {work} in 2 s")
        n, box = _live.stale(win, f"chip-working-{label.replace(' ', '-')}")
        check(f"{label}: nothing left on screen by the light's blink", n == 0,
              f"{n}px at {box}" if n else "")
        agent.set(busy=False, ringing="done")
        await asyncio.sleep(0.1)
        ring = await measure(1.1)
        check(f"{label}, calling: the ring's 20 frames a second",
              18 <= ring[1] <= 24 and ring[2] == 0, f"window/chip/light {ring} in 1.1 s")
        n, box = _live.stale(win, f"chip-ringing-{label.replace(' ', '-')}")
        check(f"{label}: nothing left on screen by the ring's blink", n == 0,
              f"{n}px at {box}" if n else "")
        agent.set(ringing=None)
        await asyncio.sleep(0.4)
        n, box = _live.stale(win, f"chip-ring-{label.replace(' ', '-')}")
        check(f"{label}: the ring answered, nothing left on screen", n == 0,
              f"{n}px at {box}" if n else "")
        after = await measure(1.0)
        check(f"{label}, idle again: no frames", after == (0, 0, 0),
              f"window/chip/light {after}")
        check(f"{label}: no status window opened", d.opened == [], f"{d.opened}")
        if label == "first open":
            await _live.reopen(win)
    win.destroy()
    router.stop()
    daemon_mod._DAEMON = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--off-screen", action="store_true", help="1-3 only: no window")
    args = parser.parse_args()
    scratch = tempfile.mkdtemp(prefix="indigoshell-agent-chip-test-")
    t0 = time.monotonic()
    try:
        test_state()
        test_blinks()
        test_layout(scratch)
        if not args.off_screen:
            asyncio.run(test_clock())
    except Exception:
        import traceback
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    print(f"\n{time.monotonic() - t0:.0f} s")
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
