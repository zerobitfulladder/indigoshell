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
from indigoshell.backend.x11 import Display             # noqa: E402
from indigoshell.widgets.base import Insets             # noqa: E402
from indigoshell.window import Anchor, Layer, Window, WindowSpec  # noqa: E402

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
        margin=Insets(top=16, right=theme.BAR_MARGIN + 12),
        override_redirect=True, focusable=False, keep_alive=True,
        background="#00000000", blur=True, effects=(PANEL_SPAWN,),
        effect_scale=0.35, content=content)


class ScriptedAgent(RemoteAgent):
    """The call's real bookkeeping of the hub's events (`RemoteAgent`)
    with no hub behind it: what the hub says is whatever the test feeds
    in, in the hub's own event shapes (lilac's PROTOCOL.md), and what the
    call sends the hub lands in `sent`. Nothing connects: its line is
    never started, and its endpoints and certificates are stand-ins.

    It plays the hub's side of a turn as the hub does: a line said in
    the call comes back as `line` and, with no turn running, starts one
    (`turn`, then `state`); the scripted pieces carry the phase the hub
    reports with them (`state`: thinking, speaking, working); and `end()`
    is `done`, then the `ring` for the device the line came from, then
    `state` (idle). A `seen` the call sends comes back as the hub's
    `seen`. Replies to what the call sends arrive once the loop comes
    round, or before the next scripted event, whichever is first.

    `track(jobs)` gives it real progress bars (progress.py), matched to
    its steps as the desktop's own agent matched them; the hub reports
    none, so `RemoteAgent` keeps an empty stand-in."""

    DEVICE = "laptop"               # this device, as the hub names it
    VOICES = [{"id": "indigo", "name": "Indigo", "color": "#b967ff"},
              {"id": "lilac", "name": "Lilac", "color": "#c8a2c8"}]
    MODEL = "opus"                  # the hub's, until the call sets one

    def __init__(self, *, voice: str) -> None:
        super().__init__(voice=voice, endpoints=["wss://hub.invalid:443"],
                         tls={"ca": "hub-ca.pem", "cert": "device.pem",
                              "key": "device.key"},
                         state_key=None)
        self.sent: list[dict] = []
        self._to_hub: deque[dict] = deque()
        # The hub's side of the conversation.
        self._turns = 0
        self._hub_turn: int | None = None       # the turn running there
        self._hub_phase = "idle"
        self._hub_voice = voice
        self._hub_device: str | None = None     # where its line came from
        self._blocks: list[str] = []
        self._open_block: int | None = None

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
            "voices": self.VOICES, "state": self._hub_state(),
            "view": {"turn": None, "line": "", "voice": self.voice,
                     "text": "", "blocks": [], "steps": [], "shown": []},
            "ask": None, "unseen": None,
            "devices": [{"name": self.DEVICE, "online": True,
                         "last_seen": time.time(), "clients": [CLIENT],
                         "hands": [], "wakes": False}]})

    def _send(self, message: dict) -> bool:
        self.sent.append(message)
        if message.get("type") in ("say", "seen"):
            self._to_hub.append(message)
            try:
                asyncio.get_running_loop().call_soon(self._hear)
            except RuntimeError:
                pass                    # no loop: the next event hears it
        return True

    def _feed(self, kind: str, **fields) -> None:
        self._on_event({"type": kind, **fields})

    def _hub_state(self) -> dict:
        return {"phase": self._hub_phase, "busy": self._hub_turn is not None,
                "model": self.model or self.MODEL, "model_label": None,
                "effort": self.effort or "medium",
                "session": self.session_id, "error": None, "hint": None}

    def _phase(self, phase: str) -> None:
        if phase != self._hub_phase:
            self._hub_phase = phase
            self._feed("state", **self._hub_state())

    def _hear(self) -> None:
        """The hub's answers to what the call sent, in order."""
        while self._to_hub:
            message = self._to_hub.popleft()
            if message["type"] == "seen":
                self._feed("seen", turn=message.get("turn"))
                continue
            line = message["text"]
            voice = message.get("voice") or self.voice
            self._feed("line", text=line, voice=voice, device=self.DEVICE,
                       client=CLIENT,
                       attached=len(message.get("attach") or ()))
            if self._hub_turn is None:  # else folded into the running one
                self._turns += 1
                self._hub_turn = self._turns
                self._hub_voice, self._hub_device = voice, self.DEVICE
                self._blocks, self._open_block = [], None
                self._feed("turn", id=self._hub_turn, voice=voice,
                           device=self.DEVICE, client=CLIENT, line=line)
                self._hub_phase = "thinking"
                self._feed("state", **self._hub_state())

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
        self._feed("done", turn=turn, voice=self._hub_voice, ok=True,
                   text=said, error=None, hint=None, notify=notify,
                   seconds=0.0)
        self._feed("ring", what="turn", id=turn,
                   to=[self._hub_device] if self._hub_device else ["*"],
                   voice=self._hub_voice, ok=True, notify=notify)
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
