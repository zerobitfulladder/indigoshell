"""The agent on the top bar, and while a hung-up call works or rings.

`AgentChip`, the top bar's right end (config_default's `top_bar`),
always there once an agent is configured: the old corner status made
short enough for the bar, a box saying what the agent is doing beside
the call's small square CALL chip, both in the call's look. While it
works (the call open or not) the box names the step it is on and the
chip's arcs turn; a question for you turns both yellow and the chip
blinks ASK; a call back turns both red, blinking, until the call is
answered or the ring is seen elsewhere; the rest of the time both rest
dimmed, with the voice that worked last. The box never shows what the
agent thinks or says: only the step. With no agent configured it is
not there and takes no room. A click answers (opens the call); a
right-click silences a ring.

`CallRouter`, the service that watches the agent and decides what shows:
the chip's state, always; `call-status` only while an agent of this
desktop works with its call closed, when `show_working` asks for it.

`CallStatus` (`call-status`): that corner plate, with its own small
chip. A long command's progress takes the plate's second line while it
runs, so the plate keeps its size (a resize costs the shell a slow
frame). It used to ring too; the bar's chip does that now, so with
`show_working=False` (an agent on another machine) it never shows.
"""

import logging
import math
import time
from typing import Callable

import skia

from ... import markdown, shapes, text, theme
from ...widgets.base import Size, Widget
from ..session import Agent
from .bars import Bars
from .call import CALL_WINDOW
from .chip import Chip
from .frame import TRACE_BLEED, Trace
from ...widgets.holo import RASTER_ALPHA, Projection, paint_raster
from .style import (ACT_SIZE, AGENT_ACCENT, BLINK_FPS, BRACKET_ALPHA,
                    BRACKET_W, CHIP_INSET, CHIP_INSET_ALPHA, CHIP_SM, CORNERS, FRAME_W,
                    GAP, PAD_X, PAD_Y, RING_FOR_S, SPIN, STATUS_W,
                    STATUS_WORDS, TAG_SIZE, TOOL_FG, UNBOUNDED, accent_of,
                    blink_on, clock_text, corner_marks, fill_of, frame_inset,
                    inner_path, line_h, own_accent, paint_heading,
                    paint_plate, plate_fill, plate_path)


STATUS_WINDOW = "call-status"

log = logging.getLogger(__name__)


# ── the corner plate shown while a closed call works ────────────────────
# (The router opened it for a ring too; the top bar's AgentChip has the
# ring now, and the plate's ring lines only show as it closes for one.)
class CallStatus(Widget):
    """`call-status`: a one-line plate and a small chip. Click answers
    (opens the `call` window); right-click silences a ring without
    answering. The chip sits against the screen edge: `side` is the
    screen edge the window is anchored to, "right" or "left"."""

    def __init__(self, agent: Agent, *, answer_hint: str = "mod+a to answer",
                 width: int = STATUS_W, call: str = CALL_WINDOW,
                 side: str = "right") -> None:
        super().__init__()
        self.agent = agent
        self.answer_hint = answer_hint
        self.width = width
        self.call = call
        self.side = side
        self.chip = Chip(CHIP_SM, self._look, lambda: agent.ring_started,
                         tint=lambda: own_accent(agent))
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
        # Chip against the screen edge — on the right unlike the open
        # call, where the agent's chip sits left of its plate.
        self.rect = rect
        h = rect.height()
        x = rect.left() if self.side == "left" else rect.right() - h
        self.chip.arrange(skia.Rect.MakeXYWH(x, rect.top(), h, h))
        self.projection.arrange(rect)

    def _plate_rect(self) -> skia.Rect:
        r = self.rect
        if self.side == "left":
            return skia.Rect.MakeLTRB(r.left() + r.height() + GAP, r.top(),
                                      r.right(), r.bottom())
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
            # A remote agent's own words for the ring, if it wrote some.
            first = getattr(a, "ring_line", None) or (
                (a.error if a.phase == "error" else a.message) or "")
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
        paint_plate(canvas, pr, accent, fill_of(a))
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
            self.bars.paint_compact(canvas, line, job, own_accent(a))
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
            get_daemon().open(self.call)
            return True
        if button == 3 and self.agent.ringing is not None \
                and self.agent.approval is None:
            self.agent.ringing = None
            self.agent._emit()
            return True
        return False


# ── the router ──────────────────────────────────────────────────────────
class CallRouter:
    """Watches a call's agent, and says what shows for it: the top
    bar's chip (`AgentChip`), and `call-status` while the call is closed.

    A SERVICES entry: neither window is alive to decide this while it is
    closed, so something outside them has to watch the agent.

    `state` is what the chip shows: "calling" while the agent rings or
    asks with its call closed, "working" while a turn runs (or a command
    it left running), the call open or not, None when idle. A chip
    `follow`s it.

    `show_working=True` also puts `call-status` in the corner while the
    agent works with its call closed (the turn's steps and clock, the
    bars of its long commands), for the desktop's own agent; the chip
    shows the work under it all the same, as it does under an open call.
    A ring or a question never opens it: the chip blinks for those.
    `False` keeps the corner empty, for an agent on its own machine (a
    `RemoteAgent`).
    """

    def __init__(self, agent: Agent, *, call: str = CALL_WINDOW,
                 status: str = STATUS_WINDOW,
                 show_working: bool = True) -> None:
        self.agent = agent
        self.call = call
        self.status = status
        self.show_working = show_working
        self.state: str | None = None
        self._followers: list[Callable[[], None]] = []
        self._unsub = None

    def start(self) -> None:
        self._unsub = self.agent.subscribe(self._on_agent)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self.state = None
        self._tell()

    def follow(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Have `fn()` called after each look at the agent (it reads
        `state` and the agent itself); returns the undo."""
        self._followers.append(fn)
        return lambda: self._followers.remove(fn) \
            if fn in self._followers else None

    def _tell(self) -> None:
        for fn in list(self._followers):
            try:
                fn()
            except Exception:
                log.exception("a follower of the call's router failed")

    def _on_agent(self, _event: str) -> None:
        from ...core.daemon import get_daemon
        d = get_daemon()
        a = self.agent
        closed = not a.call_open and self.call not in d.instances
        calling = closed and (a.ringing is not None
                              or a.approval is not None)
        working = a.busy or bool(a.jobs.shown())    # a command left running
        want = closed and working and self.show_working and not calling
        have = self.status in d.instances
        if want and not have:
            d.open(self.status)
        elif have and not want:
            d.close(self.status)
        self.state = ("calling" if calling
                      else "working" if working else None)
        self._tell()


# ── the top bar's chip ──────────────────────────────────────────────────
# The old corner status (CallStatus and its chip) at the bar's size: two
# pieces BAR_H tall in the 42px bar, 5px clear above and below. The box
# is drawn as `paint_plate` draws a plate (the speaker's fill, the
# hologram's raster in the frame's colour, the 2px frame, corner marks
# inside it) and the square as chip.Chip draws its chip (fill, tint,
# raster, frame, the faint hairline inside at the call's CHIP_INSET, two
# arcs), with no label: the name and the step are the box's, and the
# arcs take the middle, where at this size they read as the moving
# thing rather than as a thicker frame. The geometry is scaled from the
# call's: the bevel, the marks, the arcs. At the call's own numbers a
# 32px piece is all corner.
BAR_H = 32
BAR_BEVEL = 8               # the call's 12 on a 44px chip, scaled
BOX_W = 360                 # the box, fixed: the bar never moves with its text
BOX_PAD_X = 10
BOX_GAP = 6                 # box ↔ chip (the call's GAP, scaled)
BOX_MARK_D = 4              # the plate's corner marks (6 and 12 at full size)
BOX_MARK_ARM = 8
BOX_NAME_SIZE = TAG_SIZE + 2    # the plate's heading: the name and the word
BOX_DETAIL_SIZE = ACT_SIZE      # and its line: the step
CHIP_ARC_R = 7.0            # the arcs, inside the hairline
CHIP_ARC_W = 2.0
# chip.Chip's strengths: lit and between flashes of a blink, a missed
# ring; its tint over the plate and the extra in a flash.
LIT, UNLIT, MISSED = 1.0, 0.3, 0.45
CHIP_TINT, CHIP_FLASH_TINT = 0.16, 0.14
IDLE_DIM = 0.4              # both pieces while the agent is idle


def _doing(agent, fresh: bool) -> tuple[str, str]:
    """What the box says: a word and its detail. Only what the agent is
    doing: the phase as a word and the step it is on (the newest tool
    step's own line, `Activity.text` of kind "tool"), the question it
    asks, the ring and the agent's notification for it. Never its
    thinking (`thought`), its reply (`message`), or notes and info."""
    a = agent
    if a.approval is not None:
        return "needs your ok", a.approval.detail or a.approval.title
    if a.ringing is not None:
        if a.ringing == "error":
            return "line dropped", (a.error if a.phase == "error" else "") or ""
        return ("calling back" if fresh else "missed call",
                getattr(a, "ring_line", None) or "mod+a to answer")
    if a.busy or a.jobs.shown():
        step = next((act.text for act in reversed(a.activity)
                     if act.kind == "tool"), "")
        word = STATUS_WORDS.get(a.phase, a.phase) if a.busy else "running"
        return word, step
    return "", ""


def _configured_routers() -> list[CallRouter]:
    """The running daemon's `CallRouter`s, from its SERVICES: built
    before any window opens (started only after the bars are up, which
    a follower doesn't mind)."""
    from ...core.daemon import get_daemon
    try:
        services = get_daemon().config.get("services", ())
    except RuntimeError:                # no daemon: off screen, a test
        return []
    return [s for s in services if isinstance(s, CallRouter)]


class AgentChip(Widget):
    """The top bar's agent: a box saying what it is doing and the call's
    small square chip, as the old corner status was, shorter.

    Working: the box names the step (`_doing`), the chip's arcs turn at
    the phase's speed. A question (with the call closed): both yellow,
    the chip blinking. A call back: both red, blinking in the bar's
    ring pattern (style.blink_on) for RING_FOR_S, then still and dim,
    "missed call", until answered. Idle: both dimmed, the box with the
    voice of the turn on show (the one that worked last).

    It follows `routers` (`CallRouter`s), which decide its state. None,
    the default, finds them among the running daemon's SERVICES when the
    bar opens, so a config wires nothing for it. With several, a calling
    one goes first, then the first working, then the first. With none (no
    agent configured) it is not there: `collapsed`, no width and no gap
    in the bar's row, and costs nothing.

    Its two pieces are widgets of their own and ask for frames apart:
    the chip while its arcs turn or settle and while it blinks, the box
    only while it blinks. Idle, neither asks for any.
    """

    def __init__(self, routers=None) -> None:
        super().__init__()
        self._given = list(routers) if routers is not None else None
        self.routers: list[CallRouter] = []
        self._unfollow: list[Callable[[], None]] = []
        # On show: (router, "idle" | "working" | "calling"); None with no
        # router. `_key` is what was painted, to repaint only on change.
        self.shown: tuple | None = None
        self._key: tuple | None = None
        self.box = _Box(self)
        self.chip = _BarChip(self)

    def children(self):
        return (self.box, self.chip)

    @property
    def state(self) -> str | None:
        return self.shown[1] if self.shown is not None else None

    @property
    def agent(self):
        return self.shown[0].agent if self.shown is not None else None

    @property
    def collapsed(self) -> bool:
        return self.shown is None

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        self.routers = (self._given if self._given is not None
                        else _configured_routers())
        self._unfollow = [r.follow(self._on_router) for r in self.routers]
        self._on_router()

    def detach(self) -> None:
        for undo in self._unfollow:
            undo()
        self._unfollow = []
        self.routers = []
        self.shown = self._key = None
        super().detach()

    def _on_router(self) -> None:
        shown = None
        if self.routers:
            pick = (next((r for r in self.routers if r.state == "calling"),
                         None)
                    or next((r for r in self.routers if r.state == "working"),
                            None)
                    or self.routers[0])
            shown = (pick, pick.state or "idle")
        old, self.shown = self.shown, shown
        key = None if shown is None else (
            shown, self.name(), self.accent(), self.tint(), self.words(),
            self.chip_look())
        if key == self._key and (old is None) == (shown is None):
            return              # the agent's stream, nothing on show changed
        self._key = key
        # Raising a rate wakes the window's clock through this damage.
        self.invalidate(layout=(old is None) != (shown is None))

    # ── what shows ──────────────────────────────────────────────────────
    def fresh(self) -> bool:
        a = self.agent
        return (a is not None and a.ringing is not None
                and time.monotonic() - a.ring_started < RING_FOR_S)

    def name(self) -> str:
        a = self.agent
        # The voice of the turn on show (a RemoteAgent's `name`): the one
        # on the task, or the one that worked last.
        return str(getattr(a, "name", "") or "lilac").upper() if a else ""

    def tint(self) -> str:
        """Whose pieces: the voice's colour, in their fill whatever the
        frame is doing."""
        a = self.agent
        return own_accent(a) if a is not None else AGENT_ACCENT

    def accent(self) -> str:
        """The frames' colour: yellow asking, red ringing (style's
        accent_of, as the old plate), else the voice's."""
        return accent_of(self.agent) if self.state == "calling" \
            else self.tint()

    def words(self) -> tuple[str, str]:
        if self.state in (None, "idle"):
            return "", ""
        return _doing(self.agent, self.fresh())

    def chip_look(self):
        """chip.Chip's (accent, label, mode), as the old corner chip
        chose them (CallStatus._look), for the state the router gives.
        The label is left empty: this square has none."""
        a, st = self.agent, self.state
        if a is None or st == "idle":
            return self.tint(), "", "still"
        if st == "calling":
            if a.approval is not None:
                return self.accent(), "", "blink"
            return self.accent(), "", "blink" if self.fresh() else "dim"
        return self.tint(), "", a.phase if a.phase in SPIN else "working"

    def ring_since(self) -> float:
        a = self.agent
        return a.ring_started if a is not None else 0.0

    def ringing(self) -> bool:
        """A ring on show (not a question): both pieces blink for it."""
        a = self.agent
        return self.state == "calling" and a is not None \
            and a.approval is None and a.ringing is not None

    def strength(self) -> float:
        """Both pieces' strength: the frames' blink while ringing, as
        chip.Chip blinks (lit, unlit, missed), times IDLE_DIM while
        idle."""
        if self.state == "idle":
            return IDLE_DIM
        if self.ringing():
            if not self.fresh():
                return MISSED
            return LIT if blink_on(time.monotonic() - self.ring_since()) \
                else UNLIT
        return LIT

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        if self.shown is None:
            return Size(0.0, 0.0)
        return Size(BOX_W + BOX_GAP + BAR_H, BAR_H)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        x, y = round(rect.left()), round(rect.top())
        self.box.arrange(skia.Rect.MakeXYWH(x, y, BOX_W, BAR_H))
        self.chip.arrange(skia.Rect.MakeXYWH(x + BOX_W + BOX_GAP, y,
                                             BAR_H, BAR_H))

    def paint(self, canvas: skia.Canvas) -> None:
        if self.shown is None:
            return
        clip = canvas.getLocalClipBounds()
        for piece in (self.box, self.chip):
            if piece.paint_bounds().intersects(clip):
                piece.paint(canvas)

    # ── input ───────────────────────────────────────────────────────────
    @property
    def interactive(self) -> bool:
        return self.shown is not None

    def hit(self, x: float, y: float) -> Widget | None:
        if self.shown is None:
            return None
        if self.box.path().contains(x, y) or self.chip.path().contains(x, y):
            return self
        return None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        """As the corner plate did: a click answers, opening the call; a
        right-click silences a ring (not a question) without answering."""
        if self.shown is None:
            return False
        router = self.shown[0]
        agent = router.agent
        if button == 1:
            from ...core.daemon import get_daemon
            get_daemon().open(router.call)
            return True
        if button == 3 and agent.ringing is not None \
                and agent.approval is None:
            agent.ringing = None
            agent._emit()
            return True
        return False


def _beveled(r: skia.Rect, inset: float = 0.0) -> skia.Path:
    return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                          bevel=BAR_BEVEL, corners=CORNERS, inset=inset)


class _Box(Widget):
    """The box: a plate as `paint_plate` draws one, scaled, with one line
    on it: the voice's name in the frame's colour, the word (muted), the
    detail (cyan for a step, as the old plate's line). Fixed width, the
    line cut with an ellipsis."""

    def __init__(self, owner: AgentChip) -> None:
        super().__init__()
        self.owner = owner

    @property
    def animation_fps(self) -> int:
        o = self.owner
        return BLINK_FPS if o.ringing() and o.fresh() else 0

    def measure(self, avail: Size) -> Size:
        return Size(BOX_W, BAR_H)

    def path(self) -> skia.Path:
        return _beveled(self.rect)

    def paint(self, canvas: skia.Canvas) -> None:
        o = self.owner
        if o.shown is None:
            return
        r = self.rect
        k = IDLE_DIM if o.state == "idle" else 1.0
        frame_a = o.strength()
        accent, tint = o.accent(), o.tint()
        base, base_a = fill_of(o.agent)
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(base, base_a * k))
        canvas.drawPath(self.path(), fill)
        paint_raster(canvas, self.path(), accent, RASTER_ALPHA * k)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(FRAME_W)
        stroke.setColor(theme.color(accent, frame_a))
        canvas.drawPath(_beveled(r, frame_inset()), stroke)
        marks = skia.Paint(AntiAlias=True)
        marks.setStyle(skia.Paint.kStroke_Style)
        marks.setStrokeWidth(BRACKET_W)
        marks.setColor(theme.color(accent, BRACKET_ALPHA * frame_a))
        canvas.drawPath(corner_marks(r, BOX_MARK_D, BOX_MARK_ARM, BAR_BEVEL),
                        marks)
        # One line, steady through a blink so it stays readable.
        word, detail = o.words()
        detail = " ".join(str(detail).split())      # one line, always
        x = r.left() + BOX_PAD_X
        right = r.right() - BOX_PAD_X - BAR_BEVEL / 2
        base_y = text.baseline_in(r, BOX_NAME_SIZE, True)
        space = text.measure(" ", BOX_NAME_SIZE)
        for value, size, bold, colour in (
                (o.name(), BOX_NAME_SIZE, True, accent),
                (word, BOX_NAME_SIZE, False, theme.BASE_MUTED),
                (detail, BOX_DETAIL_SIZE, False,
                 TOOL_FG if o.state == "working" else theme.HUD_FG)):
            if not value or x >= right:
                continue
            value = text.ellipsize(value, size, right - x, bold)
            if not value:
                continue
            text.draw(canvas, value, x, base_y, size,
                      theme.color(colour, k), bold)
            x += text.measure(value, size, bold) + space


class _BarChip(Chip):
    """The call's CALL chip (chip.py) at the bar's size, without its
    label: its motion as it is (arcs turning at the phase's speed,
    easing back to rest, the ring's blink, measured from the ring), its
    drawing in chip.Chip's order with the geometry scaled, dimmed while
    idle."""

    def __init__(self, owner: AgentChip) -> None:
        super().__init__(BAR_H, owner.chip_look, owner.ring_since,
                         tint=owner.tint)
        self.owner = owner

    def path(self) -> skia.Path:
        return _beveled(self.rect)

    def paint(self, canvas: skia.Canvas) -> None:
        if self.owner.shown is None:
            return
        accent, _label, mode = self.look()
        r = self.rect
        lit = mode != "blink" or blink_on(time.monotonic() - self.ring_since())
        alpha = (MISSED if mode == "dim" else LIT if lit else UNLIT) \
            * (IDLE_DIM if self.owner.state == "idle" else 1.0)
        k = IDLE_DIM if self.owner.state == "idle" else 1.0
        fill = skia.Paint(AntiAlias=True)
        base, base_a = plate_fill(self.tint)
        fill.setColor(theme.color(base, base_a * k))
        canvas.drawPath(self.path(), fill)
        tint = skia.Paint(AntiAlias=True)
        tint.setColor(theme.color(accent, CHIP_TINT * alpha + (
            CHIP_FLASH_TINT if mode == "blink" and lit else 0.0)))
        canvas.drawPath(self.path(), tint)
        paint_raster(canvas, self.path(), self.tint, RASTER_ALPHA * k)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(FRAME_W)
        stroke.setColor(theme.color(accent, alpha))
        canvas.drawPath(_beveled(r, frame_inset()), stroke)
        hair = skia.Paint(AntiAlias=True)
        hair.setStyle(skia.Paint.kStroke_Style)
        hair.setStrokeWidth(1.0)
        hair.setColor(theme.color(accent, CHIP_INSET_ALPHA * alpha))
        canvas.drawPath(inner_path(r, CHIP_INSET, BAR_BEVEL), hair)
        arc = skia.Paint(AntiAlias=True)
        arc.setStyle(skia.Paint.kStroke_Style)
        arc.setStrokeWidth(CHIP_ARC_W)
        arc.setColor(theme.color(accent, alpha))
        cx, cy = r.centerX(), r.centerY()
        oval = skia.Rect.MakeLTRB(cx - CHIP_ARC_R, cy - CHIP_ARC_R,
                                  cx + CHIP_ARC_R, cy + CHIP_ARC_R)
        sweep = 64.0
        for base_angle in (self.angle, self.angle + 180.0):
            canvas.drawArc(oval, base_angle - sweep / 2, sweep, False, arc)
