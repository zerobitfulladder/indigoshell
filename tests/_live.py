"""Shared by the live tests: the real `Window` and the real widgets, on
a canvas of their own.

These are scripts, not a test suite to collect, run by hand —

    .venv/bin/python tests/clock.py           the frame clock alone
    .venv/bin/python tests/call.py            the call overlay, fuzzed
    .venv/bin/python tests/call_status.py     the hung-up status chip
    .venv/bin/python tests/call_progress.py   progress bars on the call
    .venv/bin/python tests/spawn.py           how much of a spawn is seen

Each prints what it checked and exits non-zero if something failed.
`tests/progress.py` needs no window: it runs yt-dlp, ffmpeg, curl and
rsync through the progress shims.

**The canvas** is the shell's own `canvas.Scene` with no compositor
under it (`Canvas`): the windows are placed on it, keep their frame
clocks and paint as they do on screen, in CPU raster, and what they
damage is composed onto a surface of the test's own the way the
Wayland backend presents it. Nothing is shown anywhere and nothing
needs a display, so the shell can be running meanwhile. The tests never
touch its socket, its windows or its saved state — `XDG_STATE_HOME`
points at a temporary directory before anything of the shell's is
imported.

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
caught dim, a glow nobody cleared. Exactly, short of one thing: on the
CPU, Skia anti-aliases a curve's edge a little differently under a
damage clip than whole, so a pixel on an edge may differ by as much as
the edge's own contrast (`AA_LEVELS` at most) and still count as clean.
"""

import asyncio
import dataclasses
import os
import sys
import tempfile
import time
from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="indigoshell-test-state-")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import numpy as np                                      # noqa: E402
import skia                                             # noqa: E402

from indigoshell.agent.activity import (Activity,       # noqa: E402
                                        describe_tool, tool_detail)
from indigoshell.agent.progress import Job, Jobs, runs_command  # noqa: E402
from indigoshell.agent.remote import (CLIENT, PROTOCOL,  # noqa: E402
                                      RemoteAgent)
from indigoshell.canvas import Scene                    # noqa: E402
from indigoshell.widgets.base import Insets             # noqa: E402
from indigoshell.window import Anchor, Layer, Window, WindowSpec  # noqa: E402

# The canvas's size: a 1440p screen, room for the widest window here.
SIZE = (2560, 1440)

_shots: str | None = None       # where `stale()` saves what it found


class Canvas:
    """A `Scene` presented as the Wayland backend presents it, onto a
    raster surface of its own: its damage composed once the loop comes
    round. `on_present(composed)` hears each present."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.scene = Scene(loop)
        self.scene.keyboard.available = True    # a keymap, as live
        self.scene.resize(*SIZE)
        self.scene.take_damage()
        self.surface = skia.Surface(*SIZE)
        self.on_present = None
        self._queued = False
        self.scene.on_damage = self._present_soon

    def _present_soon(self) -> None:
        if not self._queued:
            self._queued = True
            self.loop.call_soon(self._present)

    def _present(self) -> None:
        self._queued = False
        damage = self.scene.take_damage()
        if damage.isEmpty():
            return
        self.scene.compose(self.surface.getCanvas(), damage)
        if self.on_present is not None:
            self.on_present(self)

    def pixels(self, box: skia.IRect | None = None) -> np.ndarray:
        """The composed canvas as it stands, RGBA rows; `box` of it."""
        image = self.surface.makeImageSnapshot()
        if box is not None:
            image = image.makeSubset(box)
        return np.array(image.toarray(), copy=True)


def canvas() -> Canvas:
    return Canvas(asyncio.get_running_loop())


def open_window(canvas: Canvas, spec: WindowSpec, **changes) -> Window:
    """`spec` (with `changes`) created and shown on `canvas`."""
    if changes:
        spec = dataclasses.replace(spec, **changes)
    win = Window(canvas.scene, spec, canvas.loop)
    win.create()
    win.show()
    return win


async def reopen(win: Window, closed_for: float = 0.2) -> None:
    """What the daemon does with a keep-alive window: despawn it, park
    it, then show the same window and the same widgets again."""
    done = asyncio.Event()
    win.dismiss(done.set)
    await done.wait()
    win.park()
    await asyncio.sleep(closed_for)
    win.unpark()
    win.show()


def pixels(win: Window) -> np.ndarray:
    """The window's surface as it stands, RGBA rows."""
    return np.array(win.surface.makeImageSnapshot().toarray(), copy=True)


# Skia's CPU raster anti-aliases a curve's edge a little differently when
# it is drawn under a clip (a damaged region) than whole: up to ~40 levels
# on an edge pixel, never more than the edge's own contrast. A pixel that
# close to its repaint, on an edge, is that, not something left over.
AA_LEVELS = 48


def _contrast(image: np.ndarray) -> np.ndarray:
    """The most any channel of `image` varies over each pixel's 3x3
    neighbourhood."""
    padded = np.pad(image[..., :3].astype(int), ((1, 1), (1, 1), (0, 0)), mode="edge")
    h, w = image.shape[:2]
    near = [padded[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)]
    return (np.max(near, axis=0) - np.min(near, axis=0)).max(axis=2)


def stale(win: Window, tag: str = "") -> tuple[int, tuple | None]:
    """How many pixels of the surface differ from a forced full repaint,
    and the box they are in. With a `tag`, both versions of that box are
    saved as PNGs for a look."""
    global _shots
    before = pixels(win)
    win._damage_all = True
    win.redraw()
    after = pixels(win)
    diff = np.abs(before.astype(int) - after.astype(int)).max(axis=2)
    differs = (diff > 10) & ~((diff <= AA_LEVELS) & (diff <= _contrast(after)))
    count = int(differs.sum())
    if not count:
        return 0, None
    ys, xs = np.nonzero(differs)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    if tag:
        if _shots is None:
            _shots = tempfile.mkdtemp(prefix="indigoshell-test-shots-")
            print(f"    (what was left over, and its repaint: {_shots})")
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


def status_spec(content) -> WindowSpec:
    """The hung-up call's corner plate (`CallStatus`, agent/ui/status.py)
    as a config places it; the default config has none any more, since
    the top bar's chip took its place."""
    from indigoshell import theme
    from indigoshell.agent.ui.status import STATUS_WINDOW
    from indigoshell.config_default import PANEL_SPAWN
    return WindowSpec(
        name=STATUS_WINDOW, layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.RIGHT, size=(1, 1),
        margin=Insets(top=theme.TOP_BAR_HEIGHT + 16, right=theme.BAR_MARGIN + 12),
        keep_alive=True,
        background="#00000000", blur=True, effects=(PANEL_SPAWN,),
        effect_scale=0.35, content=content)


class ScriptedAgent(RemoteAgent):
    """The call's real bookkeeping of the hub's events (`RemoteAgent`)
    with no hub behind it: what the hub says is whatever the test feeds
    in, in the hub's own event shapes (lilac's PROTOCOL.md, protocol 2),
    and what the call sends the hub lands in `sent`. Nothing connects:
    its line is never started, and its endpoints and certificates are
    stand-ins.

    It plays the hub's side of a turn as the hub does: a line said in
    the call comes back as `line` and, with no turn running, starts one
    (`turn`, then `state`); the scripted pieces carry the phase the hub
    reports with them (`state`: thinking, speaking, working); and `end()`
    is `done` (kept as an exchange of the chat), then the `ring` for the
    device the line came from, then `state` (idle). A `seen` the call
    sends comes back as the hub's `seen`, and a `scrollback` as a page of
    the exchanges kept (`history` seeds earlier ones). `compact()` plays
    a compaction. Replies to what the call sends arrive once the loop
    comes round, or before the next scripted event, whichever is first.

    `track(jobs)` gives it real progress bars (progress.py), matched to
    its steps as the desktop's own agent matched them; the hub reports
    none, so `RemoteAgent` keeps an empty stand-in."""

    DEVICE = "laptop"               # this device, as the hub names it
    AGENT = {"name": "Lilac", "color": "#c8a2c8"}
    MODEL = "opus"                  # the hub's, until the call sets one
    CONTEXT = {"used": 61_000, "compact_at": 200_000, "cap": 500_000,
               "window": 1_000_000, "from": None}

    def __init__(self) -> None:
        super().__init__(endpoints=["wss://hub.invalid:443"],
                         tls={"ca": "hub-ca.pem", "cert": "device.pem",
                              "key": "device.key"})
        self.sent: list[dict] = []
        self._to_hub: deque[dict] = deque()
        # The hub's side of the chat.
        self._turns = 0
        self._hub_turn: int | None = None       # the turn running there
        self._hub_phase = "idle"
        self._hub_device: str | None = None     # where its line came from
        self._hub_lines: list[str] = []
        self._hub_at = 0.0
        self._blocks: list[str] = []
        self._open_block: int | None = None
        self.kept: list[dict] = []              # the exchanges it keeps
        self.hub_context = dict(self.CONTEXT)
        self._edge = "start"            # past the oldest kept

    def history(self, items: list[dict], edge: str = "start") -> None:
        """Earlier exchanges the hub already keeps (oldest first), and
        what lies past the oldest when the call asks."""
        self.kept = [*items, *self.kept]
        self._edge = edge

    # ── the line, stood in for ──────────────────────────────────────────
    def dial(self) -> None:
        """On the line, idle, as the old agent's dial left it: the first
        time the hub's `welcome`, then a `warm` like the real one."""
        if self.connected:
            super().dial()
            return
        self.via = "hub.invalid"
        self._on_welcome({
            "type": "welcome", "protocol": PROTOCOL, "device": self.DEVICE,
            "agent": self.AGENT, "state": self._hub_state(),
            "view": {"turn": None, "at": None, "line": "", "text": "",
                     "blocks": [], "steps": [], "shown": [],
                     "exchange": None},
            "ask": None, "unseen": None,
            "devices": [{"name": self.DEVICE, "online": True,
                         "last_seen": time.time(), "clients": [CLIENT],
                         "hands": [], "wakes": False}]})

    def _send(self, message: dict) -> bool:
        self.sent.append(message)
        if message.get("type") in ("say", "seen", "scrollback"):
            self._to_hub.append(message)
            try:
                asyncio.get_running_loop().call_soon(self._hear)
            except RuntimeError:
                pass                    # no loop: the next event hears it
        return True

    def _feed(self, kind: str, **fields) -> None:
        self._on_event({"type": kind, **fields})

    def _hub_state(self) -> dict:
        return {"phase": self._hub_phase,
                "busy": self._hub_turn is not None
                or self._hub_phase == "compacting",
                "model": self.model or self.MODEL, "model_label": None,
                "effort": self.effort or "medium",
                "context": dict(self.hub_context), "error": None,
                "hint": None}

    def _phase(self, phase: str) -> None:
        if phase != self._hub_phase:
            self._hub_phase = phase
            self._feed("state", **self._hub_state())

    def _page(self, before, limit: int) -> None:
        start = self.hub_context.get("from")
        held = [x for x in self.kept if start is None or x["id"] >= start]
        older = [x for x in held if before is None or x["id"] < before]
        page = older[-limit:]
        more = len(older) > len(page)
        reply = {"before": before, "more": more, "items": page}
        if not more:
            reply["edge"] = "compacted" if start is not None and any(
                x["id"] < start for x in self.kept) else self._edge
        self._feed("scrollback", **reply)

    def _hear(self) -> None:
        """The hub's answers to what the call sent, in order."""
        while self._to_hub:
            message = self._to_hub.popleft()
            if message["type"] == "seen":
                self._feed("seen", turn=message.get("turn"))
                continue
            if message["type"] == "scrollback":
                self._page(message.get("before"),
                           int(message.get("limit") or 30))
                continue
            line = message["text"]
            self._feed("line", text=line, device=self.DEVICE,
                       client=CLIENT,
                       attached=len(message.get("attach") or ()))
            if self._hub_turn is None:  # else folded into the running one
                self._turns += 1
                self._hub_turn = self._turns
                self._hub_device = self.DEVICE
                self._hub_lines, self._hub_at = [line], time.time()
                self._blocks, self._open_block = [], None
                self._feed("turn", id=self._hub_turn,
                           device=self.DEVICE, client=CLIENT, line=line)
                self._hub_phase = "thinking"
                self._feed("state", **self._hub_state())
            else:
                self._hub_lines.append(line)

    def compact(self, before: int = 240_000, after: int | None = 31_000,
                keep: int = 2) -> None:
        """A compaction on the hub: `compacting` (busy, no turn), then
        `compacted`, keeping the last `keep` exchanges word for word."""
        self._hear()
        self._phase("compacting")
        start = self.kept[-keep]["id"] if len(self.kept) >= keep else None
        self.hub_context.update(used=None, **({"from": start} if start else {}))
        self._feed("compacted", before=before, after=after,
                   **{"from": start})
        self.hub_context["used"] = after
        self._phase("idle")

    # ── the turn, scripted ──────────────────────────────────────────────
    def think_start(self) -> None:
        self._hear()
        self._phase("thinking")

    def think(self, text: str) -> None:
        self._hear()
        self._feed("thinking", turn=self._hub_turn, text=text)

    def text_start(self) -> None:
        """A new text block: its first `text` event carries its number."""
        self._hear()
        self._blocks.append("")
        self._open_block = len(self._blocks) - 1
        self._phase("speaking")

    def text(self, text: str) -> None:
        self._hear()
        if self._open_block is None:
            self._blocks.append("")
            self._open_block = len(self._blocks) - 1
        self._blocks[self._open_block] += text
        self._feed("text", turn=self._hub_turn, block=self._open_block,
                   text=text)
        self._phase("speaking")

    def tool(self, tool_id: str, command: str) -> None:
        self._hear()
        args = {"command": command}
        self._open_block = None
        self._feed("step", turn=self._hub_turn, id=tool_id,
                   title=describe_tool("Bash", args),
                   detail=tool_detail("Bash", args), tool="Bash",
                   device=None, sub=False)
        self._phase("working")

    def result(self, tool_id: str, output: str, failed: bool = False) -> None:
        self._hear()
        self._feed("step_done", turn=self._hub_turn, id=tool_id,
                   result=output.expandtabs(8).strip(), failed=failed)

    def end(self) -> None:
        """The turn ends well: `done`, the ring for where it was asked
        (every device for one nobody asked), then `state`."""
        self._hear()
        turn, self._hub_turn = self._hub_turn, None
        self._hub_phase = "idle"
        said = "\n\n".join(b.strip() for b in self._blocks if b.strip())
        first = next((ln.strip(" #*>-`") for ln in said.splitlines()
                      if ln.strip(" #*>-`")), "")
        notify = {"title": self.name.capitalize(), "text": first[:200]}
        xid = (self.kept[-1]["id"] if self.kept else 800) + 1
        self.kept.append({"id": xid, "at": self._hub_at, "source": "user",
                          "device": self._hub_device, "client": CLIENT,
                          "lines": list(self._hub_lines), "reply": said,
                          "attached": 0, "shown": []})
        self.hub_context["used"] = (self.hub_context["used"] or 0) \
            + 400 * (len(said) // 40 + 1)
        self._feed("done", turn=turn, ok=True, text=said, error=None,
                   hint=None, notify=notify, seconds=0.0, exchange=xid)
        self._feed("ring", what="turn", id=turn,
                   to=[self._hub_device] if self._hub_device else ["*"],
                   ok=True, notify=notify)
        self._feed("state", **self._hub_state())

    # ── progress bars ───────────────────────────────────────────────────
    def track(self, jobs: Jobs) -> None:
        self.jobs = jobs
        jobs.claim = self._claim_job
        jobs.subscribe(self._on_job)

    def _claim_job(self, job: Job) -> str:
        """The Bash step whose command a job that just started reporting
        runs, or the one Bash step still running."""
        if job.under != "cli":
            return ""
        steps = [a for a in self.activity if a.tool == "Bash" and a.tool_id]
        for act in reversed(steps):
            if runs_command(job.cmdline, act.command):
                return act.tool_id
        running = [a for a in steps if a.result is None]
        return running[0].tool_id if len(running) == 1 else ""

    def _on_job(self, event: str, job: Job) -> None:
        if event == "end" and job.step and job.visible():
            for act in self.activity:   # what it came to stays on its step
                if act.tool_id == job.step:
                    act.summary = job.summary()
        self._emit("progress")

    def job_of(self, act: Activity) -> Job | None:
        job = self.jobs.by_step(act.tool_id)
        return job if job is not None and job.live and job.visible() \
            else None

    def loose_jobs(self) -> list[Job]:
        steps = {a.tool_id for a in self.activity if a.tool_id}
        return [j for j in self.jobs.shown() if j.step not in steps]
