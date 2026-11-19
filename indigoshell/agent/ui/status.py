"""While a hung-up call works or rings: `call-status`, a small plate
and chip in the corner, and `CallRouter`, the service that shows it.
A long command's progress takes the plate's second line while it runs,
so the plate keeps its size (a resize costs the shell a slow frame)."""

import math
import time

import skia

from ... import markdown, text, theme
from ...widgets.base import Size, Widget
from ..session import Agent
from .bars import Bars
from .call import CALL_WINDOW
from .chip import Chip
from .frame import TRACE_BLEED, Trace
from .holo import Projection
from .style import (ACT_SIZE, AGENT_ACCENT, AGENT_FILL, CHIP_SM, GAP, PAD_X,
                    PAD_Y, RING_FOR_S, SPIN, STATUS_W, STATUS_WORDS, TAG_SIZE,
                    TOOL_FG, UNBOUNDED, accent_of, clock_text, line_h,
                    paint_heading, paint_plate, plate_path)


STATUS_WINDOW = "call-status"


# ── the chip shown while a closed call works or rings ───────────────────
class CallStatus(Widget):
    """`call-status`: a one-line plate and a small chip. Click answers;
    right-click silences a ring without answering."""

    def __init__(self, agent: Agent, *, answer_hint: str = "mod+a to answer",
                 width: int = STATUS_W) -> None:
        super().__init__()
        self.agent = agent
        self.answer_hint = answer_hint
        self.width = width
        self.chip = Chip(CHIP_SM, self._look, lambda: agent.ring_started)
        self.trace = Trace()        # the turn clock, as on the call's frame
        self.projection = Projection()   # as on the call
        self.bars = Bars()
        self._was_busy = False
        self._unsub = None
        self._size: tuple[int, int] | None = None

    def children(self):
        return [self.chip, self.projection]

    def _fresh(self) -> bool:
        a = self.agent
        return (a.ringing is not None
                and time.monotonic() - a.ring_started < RING_FOR_S)

    def _look(self):
        a = self.agent
        accent = accent_of(a)
        if a.approval is not None:
            # A question never goes "missed": the task waits on it.
            return accent, "ASK", "blink"
        if a.ringing is not None:
            label = "DROP" if a.ringing == "error" else "CALL"
            return accent, label, "blink" if self._fresh() else "dim"
        if a.busy:
            return accent, "CALL", a.phase if a.phase in SPIN else "working"
        return accent, "CALL", "still"

    @property
    def animation_fps(self) -> int:
        # The trace round the frame, and with it the turn clock, while a
        # turn runs. A ring only has to be seen ending: asking for frames
        # until it does gets the plate the window's last repaint at that
        # moment, with "missed call" on it as the chip goes dim.
        job = self._job()
        return max(self.trace.fps(self.agent) or (1 if self._fresh() else 0),
                   self.bars.fps([job] if job is not None else []))

    def _job(self):
        """The job whose bar the plate shows: the newest still running,
        or one just finished. A ring or a question takes the line."""
        a = self.agent
        if a.ringing is not None or a.approval is not None:
            return None
        jobs = a.jobs.shown()
        live = [j for j in jobs if j.live]
        return max(live or jobs, key=lambda j: j.id) if jobs else None

    def attach(self, window) -> None:
        super().attach(window)
        self._was_busy = self.agent.busy
        self._unsub = self.agent.subscribe(self._on_agent)
        size = self.measure(UNBOUNDED)
        self._size = (math.ceil(size.width), math.ceil(size.height))
        window.resize_content(*self._size)

    def _on_agent(self, _event: str) -> None:
        if self._was_busy and not self.agent.busy:
            self.trace.turn_ended(self.agent)
        self._was_busy = self.agent.busy
        self.invalidate()

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        super().detach()

    def _height(self) -> float:
        return 2 * PAD_Y - 4 + line_h(TAG_SIZE + 2, True) + 4 + line_h(ACT_SIZE) + 2

    def measure(self, avail: Size) -> Size:
        # The chip is a square as tall as the plate, so the two sit flush.
        h = math.ceil(self._height())
        self.chip.size = h
        return Size(self.width, h)

    def arrange(self, rect: skia.Rect) -> None:
        # Chip on the right, against the screen edge — unlike the open
        # call, where the agent's chip sits left of its plate.
        self.rect = rect
        h = rect.height()
        self.chip.arrange(skia.Rect.MakeXYWH(rect.right() - h, rect.top(),
                                             h, h))
        self.projection.arrange(rect)

    def _plate_rect(self) -> skia.Rect:
        r = self.rect
        right = r.right() - r.height() - GAP
        return skia.Rect.MakeLTRB(r.left(), r.top(), right, r.bottom())

    def blur_shape(self) -> skia.Path:
        path = skia.Path()
        path.addPath(plate_path(self._plate_rect()))
        path.addPath(self.chip.path())
        return path

    def paint_bounds(self) -> skia.Rect:
        # The trace's head glows past the plate, as on the call.
        return self.rect.makeOutset(TRACE_BLEED, TRACE_BLEED)

    def _lines(self) -> tuple[str, str]:
        a = self.agent
        if a.approval is not None:
            return "needs your ok", a.approval.detail
        if a.ringing is not None:
            word = "calling back" if self._fresh() else "missed call"
            if a.ringing == "error":
                word = "line dropped"
            first = (a.error if a.phase == "error" else a.message) or ""
            blocks = markdown.parse(first.strip()) if first.strip() else []
            preview = next((b.text for b in blocks if b.text.strip()), "")
            return word, preview or self.answer_hint
        if a.busy:
            last = a.activity[-1].text if a.activity else (
                "thinking" if a.phase == "thinking" else a.message)
            return STATUS_WORDS.get(a.phase, a.phase), last or ""
        if self._job() is not None:
            return "running", ""
        return "on the line", ""

    def paint(self, canvas: skia.Canvas) -> None:
        a = self.agent
        accent = accent_of(a)
        pr = self._plate_rect()
        paint_plate(canvas, pr, accent, AGENT_FILL)
        self.trace.paint(canvas, pr, a, accent)
        x = pr.left() + PAD_X - 4
        right = pr.right() - PAD_X
        word, detail = self._lines()
        top = pr.top() + PAD_Y - 4
        head = skia.Rect.MakeXYWH(x, top, right - x, line_h(TAG_SIZE + 2, True) + 4)
        paint_heading(canvas, head, TAG_SIZE + 2,
                      ((a.name, accent, True), (word, theme.BASE_MUTED, False)))
        if a.busy:
            clock = clock_text(time.monotonic() - a.turn_started)
            w = text.measure(clock, TAG_SIZE)
            text.draw(canvas, clock, right - w,
                      text.baseline_in(head, TAG_SIZE), TAG_SIZE,
                      theme.color(theme.YELLOW_DIM))
        line = skia.Rect.MakeXYWH(x, head.bottom(), right - x,
                                  line_h(ACT_SIZE) + 2)
        job = self._job()
        if job is not None:
            self.bars.paint_compact(canvas, line, job, AGENT_ACCENT)
        else:
            color = TOOL_FG if a.busy and not a.ringing else theme.HUD_FG
            text.draw(canvas, text.ellipsize(detail, ACT_SIZE, right - x), x,
                      text.baseline_in(line, ACT_SIZE), ACT_SIZE,
                      theme.color(color))
        self.chip.paint(canvas)
        self.projection.paint(canvas)

    @property
    def interactive(self) -> bool:
        return True

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        from ...core.daemon import get_daemon
        if button == 1:
            get_daemon().open(CALL_WINDOW)
            return True
        if button == 3 and self.agent.ringing is not None \
                and self.agent.approval is None:
            self.agent.ringing = None
            self.agent._emit()
            return True
        return False


# ── the router ──────────────────────────────────────────────────────────
class CallRouter:
    """Shows `call-status` while a closed call is busy or ringing.

    A SERVICES entry: neither window is alive to decide this while it is
    closed, so something outside them has to watch the agent.
    """

    def __init__(self, agent: Agent, *, call: str = CALL_WINDOW,
                 status: str = STATUS_WINDOW) -> None:
        self.agent = agent
        self.call = call
        self.status = status
        self._unsub = None

    def start(self) -> None:
        self._unsub = self.agent.subscribe(self._on_agent)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on_agent(self, _event: str) -> None:
        from ...core.daemon import get_daemon
        d = get_daemon()
        a = self.agent
        want = (not a.call_open and self.call not in d.instances
                and (a.busy or a.ringing is not None
                     or a.approval is not None
                     or bool(a.jobs.shown())))   # a command left running
        have = self.status in d.instances
        if want and not have:
            d.open(self.status)
        elif have and not want:
            d.close(self.status)
