"""The top bar's agent (`AgentChip`, agent/ui/status.py): the box saying
what it is doing and the call's small square chip, and what `CallRouter`
tells them.

    .venv/bin/python tests/agent_chip.py              all of it, ~20 s
    .venv/bin/python tests/agent_chip.py --no-window  1-3 only

1. The state, from a stand-in agent through a real `CallRouter` on a
   stub daemon (no window): idle (dimmed, her name and colour, no frames)
   before any turn; working (the step on the box, the square's arcs
   turning) while a turn runs, the call open or not, and while the hub
   compacts the chat (the box saying so, no step); idle again when the
   turn ends without a ring; a
   question with the call closed: both pieces yellow, the square
   blinking, the box steady; a ring: both red and blinking in the bar's
   ring pattern, then still and dim ("missed call") after RING_FOR_S;
   no `call-status` opened for either; the ring ended by the hub's
   `seen`, by answering (the call opened) or by a right-click;
   `show_working=True` keeping its corner plate while working (the
   pieces working under it) but closing it for a ring; a calling router
   before a working one, a working one before an idle one; the router
   stopped; a left click opening the call; no router at all: nothing.
2. What the box says: only what the agent is doing (the phase as a
   word, the newest tool step, the question, the ring and its
   notification), never its thinking, its reply, or its notes and info
   lines, checked in the words and in every string the box draws.
3. Layout: with no router the chip takes no width and no gap in the
   bar's row (`collapsed`); with one, idle or not, one fixed width
   whatever the state and the text; whole pixels, centred in the bar;
   hit on either piece, not in the gap or a cut corner; painted
   (raster): her colour working, yellow asking, red
   ringing (lit and between flashes), dimmed idle, the call's stripes;
   the user's config.py (by its path, scratch XDG, no bytecode) has the
   chip at its top bar's right end and finds its router in SERVICES.
4. The real `Window` and frame clock, on a canvas of the test's own
   (`_live.Canvas`), opened twice: no frame over an idle second; the
   square's 30 frames a second while its arcs turn, the box none; both
   at the ring's 20 while it blinks; none once it goes missed or idle;
   and nothing left on screen by any of it (`_live.stale`).

Prints ok/FAIL lines and exits 1 if anything failed. Never touches the
running shell: no socket, lock, state or window of its.
"""

import argparse
import asyncio
import collections
import json
import os
import subprocess
import sys
import tempfile
import time
import types

import _live                                    # sets XDG_STATE_HOME first

import skia

from indigoshell import text, theme
from indigoshell.agent.activity import Activity, Approval
from indigoshell.agent.ui import status
from indigoshell.agent.ui.status import (BAR_H, BOX_GAP, BOX_W, IDLE_DIM,
                                         AgentChip, CallRouter, _doing)
from indigoshell.agent.ui.style import (ASK_ACCENT, BLINK_FPS, CHIP_FPS,
                                        RING_ACCENT)
from indigoshell.core import daemon as daemon_mod
from indigoshell.widgets.base import Size
from indigoshell.widgets.label import Label
from indigoshell.widgets.layout import Align, Row

REPO = _live.REPO
FAILED: list[str] = []
LILAC = "#c8a2c8"
OTHER = "#05d9e8"           # a second agent's, for the router's order
SECRET = "SECRET"           # in every thought, reply and note: never on the box


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
    attributes): its turn and steps, its ring, its question, a
    compaction, her name and colour; and a thought and a reply that must
    never show."""

    def __init__(self, name="LILAC", accent=LILAC) -> None:
        self.name, self.accent = name, accent
        self.busy = self.call_open = self.compacting = False
        self.ringing = self.approval = None
        self.ring_started, self.ring_line, self.error = 0.0, None, None
        self.phase = "idle"
        self.activity: collections.deque = collections.deque(maxlen=40)
        self.thought = f"{SECRET} thought: maybe the user wants X"
        self.message = f"{SECRET} reply: here is what I found"
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
        if values.get("ringing"):
            self.ring_started = time.monotonic()
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


def step(title: str) -> Activity:
    return Activity("tool", title, detail=title, tool="Bash")


def ask() -> Approval:
    return Approval(tool="laptop", title="run as root", detail="sudo pacman -Syu",
                    future=None)


def look(chip) -> tuple:
    return (chip.state, chip.name(), chip.accent())


def mode(chip) -> str:
    return chip.chip_look()[2]


def settle(chip) -> None:
    """Let the square's arcs ease back to rest, as frames would."""
    t = 0.0
    for _ in range(200):
        t += 1 / 30
        chip.chip.animate(t)


# ── 1. state ────────────────────────────────────────────────────────────

def test_state() -> None:
    section("1. what the pieces show, from the router")
    d, agent, router, chip, win = rig()
    check("found its router among the daemon's SERVICES", chip.routers == [router])
    check("before any turn: idle, both dimmed, her name and colour, no frames",
          look(chip) == ("idle", "LILAC", LILAC) and not chip.collapsed
          and chip.strength() == IDLE_DIM and chip.chip.animation_fps == 0
          and chip.box.animation_fps == 0 and chip.words() == ("", ""), f"{look(chip)}")

    agent.activity.append(step("read ~/notes.md"))
    agent.set(busy=True, phase="working")
    check("a turn runs: working, the step on the box, full strength",
          look(chip) == ("working", "LILAC", LILAC) and chip.strength() == 1.0
          and chip.words() == ("working", "read ~/notes.md")
          and win.damaged and win.damaged[-1] == (chip, False), f"{chip.words()}")
    check("... the square's arcs turn at 30 frames a second, the box asks none",
          mode(chip) == "working" and chip.chip.animation_fps == CHIP_FPS == 30
          and chip.box.animation_fps == 0)
    n = len(win.damaged)
    agent.set()                         # the agent's stream: nothing on show changes
    check("an agent event that changes nothing on show: no repaint", len(win.damaged) == n)
    agent.activity.append(step("browser click \"Sign in\""))
    agent.set()
    check("a new step: the box says it, repainted",
          chip.words()[1] == "browser click \"Sign in\"" and len(win.damaged) == n + 1)
    agent.set(phase="thinking")
    check("thinking: its arcs at the thinking speed", mode(chip) == "thinking")

    agent.set(call_open=True)
    check("the call opens mid-turn: still working (always on the bar)",
          chip.state == "working")
    agent.set(call_open=False)

    agent.set(busy=False, phase="idle")
    settle(chip)
    check("the turn ends without a ring: idle, dimmed, no frames once the arcs are back "
          "at rest", look(chip) == ("idle", "LILAC", LILAC)
          and chip.chip.animation_fps == 0 and chip.box.animation_fps == 0)

    agent.set(compacting=True, phase="compacting")
    check("the hub compacts the chat (no turn): working, the box says so and names no "
          "step, the arcs at the compacting speed", chip.state == "working"
          and chip.words() == ("compacting", "") and mode(chip) == "compacting"
          and chip.chip.animation_fps == CHIP_FPS, f"{chip.words()} {mode(chip)}")
    agent.set(compacting=False, phase="idle")
    settle(chip)
    check("... done: idle again", chip.state == "idle")

    agent.set(busy=True, phase="asking", approval=ask(), ringing="ask")
    settle(chip)
    check("a question, the call closed: both yellow (the old ask colour), the square "
          "blinking at 20 fps, the box steady", chip.state == "calling"
          and chip.accent() == ASK_ACCENT and mode(chip) == "blink"
          and chip.chip.animation_fps == BLINK_FPS and chip.box.animation_fps == 0
          and chip.strength() == 1.0 and chip.words() == ("needs your ok", "sudo pacman -Syu"),
          f"{chip.accent()} {mode(chip)} {chip.words()}")
    agent.set(approval=None, ringing=None, phase="working")
    check("... answered: working again", chip.state == "working")
    agent.set(call_open=True, approval=ask())
    check("a question with the call open: working (the call asks it)",
          chip.state == "working")
    agent.set(call_open=False, approval=None, busy=False, phase="idle")

    agent.set(ringing="done", ring_line="Kernel updated")
    settle(chip)
    check("a ring: both red (the old ring colour), the square and the box blinking at the "
          "ring's 20 fps", chip.state == "calling" and chip.accent() == RING_ACCENT
          and mode(chip) == "blink" and chip.chip.animation_fps == BLINK_FPS
          and chip.box.animation_fps == BLINK_FPS
          and chip.words() == ("calling back", "Kernel updated"), f"{chip.words()}")
    check("... and no status window opened for it", d.opened == [] and "call-status"
          not in d.instances, f"{d.opened}")
    lit, dark = [], []
    for el in (0.0, 0.05, 0.2, 0.3, 0.5):
        agent.ring_started = time.monotonic() - el
        (lit if status.blink_on(el) else dark).append(chip.strength())
    check("... the box's frame blinks with the square: full, then 0.3 between flashes",
          set(lit) == {1.0} and set(dark) == {0.3}, f"{lit} / {dark}")
    agent.ring_started = time.monotonic() - status.RING_FOR_S - 1
    check("... after RING_FOR_S unanswered: still and dim, \"missed call\", no frames",
          mode(chip) == "dim" and chip.chip.animation_fps == 0
          and chip.box.animation_fps == 0 and chip.strength() == 0.45
          and chip.words()[0] == "missed call")
    agent.set(ringing=None)
    check("the hub's `seen` (the ring stops): idle", chip.state == "idle")

    agent.set(ringing="done")
    agent.set(call_open=True, ringing=None)
    check("answered with MOD+A (the call opens): idle", chip.state == "idle")
    agent.set(call_open=False)

    agent.set(ringing="error", phase="error", error="Can't reach the hub")
    check("a ring for a turn that failed: calling, \"line dropped\" and the error",
          chip.state == "calling" and chip.words() == ("line dropped", "Can't reach the hub"))
    clicked = chip.click(3)
    check("a right-click silences it, as the corner plate's did: idle",
          clicked and agent.ringing is None and chip.state == "idle")
    agent.set(phase="idle", error=None)

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
    agent.set(busy=True, phase="working")
    check("show_working=True, working: the corner plate opens, the pieces show the work "
          "under it", d.opened == ["call-status"] and chip.state == "working", f"{d.opened}")
    agent.set(approval=ask(), ringing="ask")
    check("... a question: the plate closes, the pieces go yellow",
          d.closed == ["call-status"] and chip.state == "calling", f"{d.closed}")
    agent.set(approval=None, ringing=None)
    check("... answered: the plate back, the pieces working",
          d.opened == ["call-status"] * 2 and chip.state == "working")
    agent.set(busy=False, ringing="done")
    check("... the turn ends ringing: the plate closes, the pieces go red",
          "call-status" not in d.instances and chip.state == "calling")
    chip.detach()
    router.stop()

    # Several routers: calling, then working, then the first.
    d = StubDaemon()
    daemon_mod._DAEMON = d
    a1, a2 = StandIn(), StandIn("SECOND", OTHER)
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
          chip.state == "calling" and chip.name() == "SECOND")
    a2.set(ringing=None)
    check("... then the working one", chip.state == "working" and chip.name() == "LILAC")
    a1.set(busy=False)
    check("... both idle: the first", chip.state == "idle" and chip.name() == "LILAC")
    chip.detach()

    daemon_mod._DAEMON = None
    lone = AgentChip()
    lone.attach(FakeWindow())
    check("no daemon (or no router in it): nothing, collapsed, following nothing",
          lone.routers == [] and lone.shown is None and lone.collapsed
          and lone.measure(Size(500, 42)) == Size(0, 0))
    lone.detach()


# ── 2. what the box says ────────────────────────────────────────────────

def test_words() -> None:
    section("2. the box says only what the agent is doing")
    a = StandIn()
    a.busy, a.phase = True, "thinking"
    check("thinking, no step yet: the word alone, not the thought",
          _doing(a, False) == ("thinking", ""), f"{_doing(a, False)}")
    from indigoshell.agent.ui.agent_plate import think_line
    a.turn_started = 0.0
    lines = {think_line(a, 0.25 * k) for k in range(8)}
    check("the call's thinking row: that it thinks, never the thought",
          lines == {"thinking", "thinking.", "thinking..", "thinking..."}
          and not any(SECRET in line for line in lines), f"{sorted(lines)}")
    a.phase = "speaking"
    check("speaking: the word alone, not the reply", _doing(a, False) == ("speaking", ""))
    a.activity.append(step("read ~/notes/plan.md"))
    a.activity.append(Activity("note", f"{SECRET} the hub's own note"))
    a.activity.append(Activity("info", f"{SECRET} an info line"))
    a.phase = "working"
    check("working: the newest tool step, past the notes and info after it",
          _doing(a, False) == ("working", "read ~/notes/plan.md"))

    d, agent, router, chip, _w = rig()
    other = Label("MINE")
    row = Row([other, chip], spacing=theme.BAR_SPACING, align=Align.CENTER)
    drawn: list[str] = []
    real = text.draw

    def record(canvas, value, *args, **kwargs):
        drawn.append(str(value))
        return real(canvas, value, *args, **kwargs)

    text.draw = record
    try:
        scenes = [
            dict(busy=True, phase="thinking"),
            dict(phase="speaking"),
            dict(phase="working"),
            dict(approval=ask(), ringing="ask"),
            dict(approval=None, ringing=None, busy=False, phase="idle"),
            dict(ringing="done", ring_line=None),
            dict(ringing=None),
        ]
        agent.activity.extend([Activity("note", f"{SECRET} note"),
                               Activity("info", f"{SECRET} info")])
        for scene in scenes:
            agent.set(**scene)
            m = row.measure(Size(800, 42))
            row.arrange(skia.Rect.MakeXYWH(0, 0, m.width, 42))
            surface = skia.Surface(int(m.width), 42)
            row.paint(surface.getCanvas())
    finally:
        text.draw = real
    check("a thought, a reply, notes and info on the agent never reach the box, in any "
          "state (every string it drew)", drawn and not any(SECRET in s for s in drawn),
          f"{len(drawn)} strings drawn")
    chip.detach()


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
                  "agent": type(chip.routers[0].agent).__name__ if chip.routers else None}))
chip.detach()
"""


def colour_px(img, r, hex_, tol=30) -> int:
    rgb = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
    box = img[:, int(r.left()):int(r.right()), :3].astype(int)
    a = (abs(box - rgb).max(-1) <= tol).sum()
    b = (abs(box[..., ::-1] - rgb).max(-1) <= tol).sum()
    return int(max(a, b))


def ink(img, r) -> int:
    bg = [int(theme.BAR_BG[i:i + 2], 16) for i in (1, 3, 5)]
    box = img[:, int(r.left()):int(r.right()), :3].astype(int)
    return int(min(abs(box - bg).sum(), abs(box[..., ::-1] - bg).sum()))


def test_layout(scratch: str) -> None:
    section("3. layout, paint, the user's config")
    W, H = 900, theme.TOP_BAR_HEIGHT
    daemon_mod._DAEMON = None
    none = AgentChip()
    none.attach(FakeWindow())
    other = Label("MINE", color=theme.CYAN_BRIGHT)
    row = Row([other, none], spacing=theme.BAR_SPACING, align=Align.CENTER)
    alone = Row([Label("MINE")], spacing=theme.BAR_SPACING)
    check("no agent configured: the row measures as if the chip were not there (no gap)",
          row.measure(Size(W, H)).width == alone.measure(Size(W, H)).width)
    none.detach()

    d, agent, router, chip, _w = rig()
    other = Label("MINE", color=theme.CYAN_BRIGHT)
    row = Row([other, chip], spacing=theme.BAR_SPACING, align=Align.CENTER)
    widths = set()
    for scene in (dict(), dict(busy=True, phase="working"),
                  dict(approval=ask(), ringing="ask"), dict(approval=None, ringing="done")):
        agent.activity.append(step("$ " + "a very long command " * 8))
        agent.set(**scene)
        widths.add(chip.measure(Size(W, H)).width)
    check("one fixed width whatever the state or the step's length: BOX_W, the gap and "
          "the square", widths == {BOX_W + BOX_GAP + BAR_H}, f"{widths}")
    agent.set(ringing=None, busy=True, phase="working")
    m = row.measure(Size(W, H))
    row.arrange(skia.Rect.MakeXYWH(0, 0, m.width, H))
    r, box, sq = chip.rect, chip.box.rect, chip.chip.rect
    check("whole pixels, both pieces BAR_H tall and centred in the bar, the square at the "
          "right end", box.left() == int(box.left()) and box.top() == (H - BAR_H) / 2
          and box.height() == sq.height() == sq.width() == BAR_H
          and sq.right() == r.right() and sq.left() - box.right() == BOX_GAP,
          f"box {box}, square {sq}")
    check("hit on either piece; not in the gap between them, nor a cut corner",
          chip.hit(box.centerX(), box.centerY()) is chip
          and chip.hit(sq.centerX(), sq.centerY()) is chip
          and chip.hit(box.right() + BOX_GAP / 2, box.centerY()) is None
          and chip.hit(sq.right() - 1, sq.top() + 1) is None)

    def painted():
        surface = skia.Surface(int(m.width), H)
        canvas = surface.getCanvas()
        canvas.clear(theme.color(theme.BAR_BG))
        row.paint(canvas)
        return surface.makeImageSnapshot().toarray()

    working = painted()
    agent.set(approval=ask(), ringing="ask")
    asking = painted()
    agent.set(approval=None, ringing="done", busy=False)
    agent.ring_started = time.monotonic()
    ring_lit = painted()
    agent.ring_started = time.monotonic() - 0.2
    ring_dark = painted()
    agent.set(ringing=None)
    idle = painted()
    check("working: the frames in her colour",
          colour_px(working, box, LILAC) > 200 and colour_px(working, sq, LILAC) > 40,
          f"{colour_px(working, box, LILAC)} / {colour_px(working, sq, LILAC)} px")
    check("a question: both pieces yellow",
          colour_px(asking, box, ASK_ACCENT) > 200 and colour_px(asking, sq, ASK_ACCENT) > 40)
    check("a ring: both red at a flash, much less between flashes",
          colour_px(ring_lit, box, RING_ACCENT) > 200 and colour_px(ring_lit, sq, RING_ACCENT) > 40
          and colour_px(ring_dark, r, RING_ACCENT) < colour_px(ring_lit, r, RING_ACCENT) / 4,
          f"{colour_px(ring_lit, r, RING_ACCENT)} / {colour_px(ring_dark, r, RING_ACCENT)} px")
    check("idle: both pieces there, dimmed (well under the ink of working)",
          0 < ink(idle, r) < 0.7 * ink(working, r), f"ink {ink(idle, r)} / {ink(working, r)}")
    # Between the frame and the text's top, away from the corner marks.
    plate = working[int(box.top()) + 3:int(box.top()) + 9,
                    int(box.left()) + 160:int(box.left()) + 163, :3]
    rows = plate.astype(int).sum(axis=(1, 2))
    stripes = [int(v) for v in rows]
    check("the call's raster across the box: rows repeat every 3 px",
          len(set(stripes)) > 1 and all(stripes[i] == stripes[i + 3]
                                        for i in range(len(stripes) - 3)), f"{stripes}")
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

    section("4. frames, on the real Window and clock")
    status.RING_FOR_S = 2.0             # a ring that goes "missed" within the test
    d = StubDaemon()
    daemon_mod._DAEMON = d
    agent = StandIn()
    router = CallRouter(agent, show_working=False)
    d.config["services"].append(router)
    router.start()
    chip = AgentChip()
    _live.count_frames(chip.chip)
    _live.count_frames(chip.box)
    spec = WindowSpec(
        name="agent-chip", layer=Layer.OVERLAY, anchor=Anchor.TOP | Anchor.RIGHT,
        size=(560, theme.TOP_BAR_HEIGHT), background=theme.BAR_BG, keep_alive=True,
        content=Row([Label("flux"), chip], spacing=theme.BAR_SPACING, align=Align.CENTER))
    win = _live.open_window(_live.canvas(), spec)
    frames = {"n": 0}
    run = win._run_frame

    def counted():
        frames["n"] += 1
        return run()

    win._run_frame = counted

    async def measure(seconds: float) -> tuple[int, int, int]:
        chip.chip.frames = chip.box.frames = frames["n"] = 0
        await asyncio.sleep(seconds)
        return frames["n"], chip.chip.frames, chip.box.frames

    for label in ("first open", "opened again"):
        await asyncio.sleep(0.6)
        idle = await measure(1.0)
        check(f"{label}, idle: no frame at all over an idle second", idle == (0, 0, 0),
              f"window/square/box {idle}")
        agent.activity.append(step("read ~/notes/plan.md"))
        agent.set(busy=True, phase="working")
        await asyncio.sleep(0.1)
        work = await measure(1.0)
        check(f"{label}, working: the square's arcs at 30 frames a second, the box none",
              26 <= work[1] <= 32 and work[2] == 0 and work[0] <= work[1] + 2,
              f"window/square/box {work} in 1 s")
        n, box = _live.stale(win, f"chip-working-{label.replace(' ', '-')}")
        check(f"{label}: nothing left on screen by the turning arcs", n == 0,
              f"{n}px at {box}" if n else "")
        agent.set(busy=False, phase="idle", ringing="done", ring_line="done")
        await asyncio.sleep(0.6)            # the arcs ease back to rest first
        ring = await measure(1.0)
        check(f"{label}, calling: both pieces at the ring's 20 frames a second",
              17 <= ring[1] <= 22 and 17 <= ring[2] <= 22, f"window/square/box {ring} in 1 s")
        n, box = _live.stale(win, f"chip-ringing-{label.replace(' ', '-')}")
        check(f"{label}: nothing left on screen by the blink", n == 0,
              f"{n}px at {box}" if n else "")
        await asyncio.sleep(0.8)            # past RING_FOR_S: missed
        missed = await measure(1.0)
        n, box = _live.stale(win, f"chip-missed-{label.replace(' ', '-')}")
        check(f"{label}, missed: still and dim, no frames, nothing left on screen",
              missed == (0, 0, 0) and n == 0 and mode(chip) == "dim",
              f"window/square/box {missed}" + (f"; {n}px at {box}" if n else ""))
        agent.set(ringing=None)
        await asyncio.sleep(0.4)
        after = await measure(1.0)
        n, box = _live.stale(win, f"chip-idle-{label.replace(' ', '-')}")
        check(f"{label}, idle again: no frames, nothing left on screen",
              after == (0, 0, 0) and n == 0, f"window/square/box {after}")
        check(f"{label}: no status window opened", d.opened == [], f"{d.opened}")
        if label == "first open":
            await _live.reopen(win)
    win.destroy()
    router.stop()
    daemon_mod._DAEMON = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-window", action="store_true", help="1-3 only")
    args = parser.parse_args()
    scratch = tempfile.mkdtemp(prefix="indigoshell-agent-chip-test-")
    t0 = time.monotonic()
    try:
        test_state()
        test_words()
        test_layout(scratch)
        if not args.no_window:
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
