"""The agent on the top bar, and while a hung-up call works or rings.

`AgentChip`, the top bar's right end (config_default's `top_bar`),
always there once an agent is configured: a small beveled chip with the
name of a voice, in that voice's colour. While the agent works (the call
open or not) it is the voice on the task, and the whole chip breathes,
slowly; when it calls back (a turn ended, or a question for you, with
the call closed) the whole chip blinks fast, until the call is answered
or the ring is seen elsewhere; the rest of the time it rests dimmed,
with the voice that worked last. With no agent configured it is not
there and takes no room. A click answers (opens the call); a right-click
silences a ring.

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
from .style import (ACT_SIZE, BEVEL, CHIP_INSET, CHIP_INSET_ALPHA, CHIP_SM,
                    CORNERS, FRAME_W, GAP, PAD_X, PAD_Y, RING_FOR_S, SPIN, STATUS_W,
                    STATUS_WORDS, TAG_SIZE, TOOL_FG, UNBOUNDED, accent_of,
                    clock_text, fill_of, frame_inset, inner_path, line_h,
                    own_accent, paint_heading, paint_plate, plate_fill,
                    plate_path)


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
# The call's small square chip (chip.py), a bar's height tall and as long
# as a voice's name: the same plate in the voice's colour
# (style.plate_fill: the plates' violet-black leaning to it, then a tint
# of it on top), the hologram's raster across it (holo: a faint line
# every 3px, the call plates' stripes, on the window's own pixel grid),
# its 2px frame and the faint hairline inside it (style.inner_path at
# CHIP_INSET). The corners are cut at the top-right and bottom-left, the
# call's bevel scaled to the height. The name is set like the INDIGO tag
# at the bar's other end, in bold capitals, a step smaller to sit in the
# chip, centred. Every state is the whole chip at one strength: resting
# at IDLE_DIM, breathing while it works, blinking while it calls back.
CHIP_H = 28                 # in the 42px bar: 7px clear above and below
CHIP_PAD_X = 11             # the name clear of the hairline
CHIP_BEVEL = round(BEVEL * CHIP_H / CHIP_SM)    # the call chip's cut: 8
# One width for every voice: room for the longest of them, so the bar
# doesn't shift when the voice changes, plus CHIP_ROOM, where a light
# once sat beside the name (its 7px and the 8px before it), so the name
# has air either side.
CHIP_FIT = "INDIGO"
CHIP_ROOM = 15
# The voice's colour over the plate, as chip.Chip paints it, and the
# extra it gets in a ring's flash.
CHIP_TINT = 0.16
CHIP_FLASH_TINT = 0.14
CHIP_NAME_SIZE = text.pt(11)    # 14.7px; the INDIGO tag is 13pt
CHIP_TRACKING = 1.0
CHIP_NAME_MAX_W = 160       # a longer voice name ends in an ellipsis
IDLE_DIM = 0.4              # the whole chip while the agent is idle
# Working: a slow breath, a cosine from full down to PULSE_LOW and back
# every PULSE_S, starting full. 24 frames a second read as smooth at 1x
# (each frame moves the strength under 3.5%), and no more are asked.
PULSE_S = 2.0
PULSE_LOW = 0.5
PULSE_FPS = 24
# Calling back: a fast, even blink, as (ms, lit) steps like
# theme.WORKSPACE_URGENT_RING, looped from the ring's start: 4 a second,
# the whole chip flashing to full and dropping to CALL_DIM. Frames come
# only on its steps (`_blink_fps`): 8 a second.
CALL_BLINK = ((125, True), (125, False))
CALL_DIM = 0.2


def _blink_fps(pattern) -> int:
    """Frames a second that land on every step of `pattern`: 1000 over
    the greatest common divisor of 1000 and its steps (125ms steps: 8;
    50ms: 20). 0 with none."""
    steps = [int(ms) for ms, _lit in pattern or ()]
    return 1000 // math.gcd(1000, *steps) if steps else 0


def _blink_lit(pattern, elapsed: float) -> bool:
    """Whether `pattern`, looped, is lit `elapsed` seconds in. Read on
    its own frame grid (rounded to the nearest of `_blink_fps`), so a
    frame a few ms early or late shows the step it was due for."""
    fps = _blink_fps(pattern)
    if not fps:
        return True
    period = sum(ms for ms, _lit in pattern)
    t = (round(max(0.0, elapsed) * fps) * (1000.0 / fps)) % period
    for ms, lit in pattern:
        if t < ms:
            return lit
        t -= ms
    return True


def _pulse(elapsed: float) -> float:
    """The working breath `elapsed` seconds in: full at 0, PULSE_LOW at
    half a period, eased all the way (a cosine)."""
    wave = 0.5 + 0.5 * math.cos(2 * math.pi * elapsed / PULSE_S)
    return PULSE_LOW + (1.0 - PULSE_LOW) * wave


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
    """The top bar's agent chip: a voice's name on a beveled plate.
    Working: the voice on the task, the whole chip breathing. Calling
    back: the whole chip blinking fast. Idle: the chip dimmed, with the
    voice of the turn on show (the one that worked last; before any
    turn, the voice the agent speaks with now).

    It follows `routers` (`CallRouter`s), which decide what it shows.
    None, the default, finds them among the running daemon's SERVICES
    when the bar opens, so a config wires nothing for it. With several,
    a calling one goes first, then the first working, then the first.
    With none (no agent configured) it is not there: `collapsed`, so it
    takes no width and no gap in the bar's row, and costs nothing.

    It asks for frames only while it moves: PULSE_FPS while it works,
    the blink's 8 a second while it calls back, none while idle.
    """

    def __init__(self, routers=None) -> None:
        super().__init__()
        self._given = list(routers) if routers is not None else None
        self.routers: list[CallRouter] = []
        self._unfollow: list[Callable[[], None]] = []
        # On show: (router, "idle" | "working" | "calling", name,
        # colour); None with no router.
        self.shown: tuple | None = None
        self.since = 0.0        # when the state on show began: its motion's start
        self.on = True          # lit, or between two of the blink's flashes
        self.breath = 1.0       # the working breath's strength now

    @property
    def state(self) -> str | None:
        return self.shown[1] if self.shown is not None else None

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
        self.shown = None
        super().detach()

    def _on_router(self) -> None:
        shown = None
        if self.routers:
            pick = (next((r for r in self.routers if r.state == "calling"),
                         None)
                    or next((r for r in self.routers if r.state == "working"),
                            None)
                    or self.routers[0])
            a = pick.agent
            # The voice of the turn on show (a RemoteAgent's `name`): the
            # one on the task, or the one that worked last.
            name = str(getattr(a, "name", "") or "lilac").upper()
            shown = (pick, pick.state or "idle", name, own_accent(a))
        old = self.shown
        if shown == old:
            return              # the agent's stream, nothing on show changed
        if old is None or shown is None or old[:2] != shown[:2]:
            # A new state starts its motion at full strength.
            self.since = time.monotonic()
            self.on = True
            self.breath = 1.0
        self.shown = shown
        # Raising a rate wakes the window's clock through this damage.
        self.invalidate(layout=old is None or shown is None
                        or old[2] != shown[2])

    # ── motion ──────────────────────────────────────────────────────────
    @property
    def animation_fps(self) -> int:
        if self.state == "working":
            return PULSE_FPS
        return _blink_fps(CALL_BLINK) if self.state == "calling" else 0

    def animate(self, t: float) -> None:
        elapsed = time.monotonic() - self.since
        self.on = (self.state != "calling"
                   or _blink_lit(CALL_BLINK, elapsed))
        self.breath = _pulse(elapsed) if self.state == "working" else 1.0

    def level(self) -> float:
        """The whole chip's strength: dimmed while idle, breathing while
        it works, full or CALL_DIM while it blinks."""
        if self.state == "idle":
            return IDLE_DIM
        if self.state == "working":
            return self.breath
        return CALL_DIM if self.state == "calling" and not self.on else 1.0

    # ── layout / paint ──────────────────────────────────────────────────
    def _name(self) -> str:
        if self.shown is None:
            return ""
        return text.ellipsize(self.shown[2], CHIP_NAME_SIZE,
                              CHIP_NAME_MAX_W, True)

    def measure(self, avail: Size) -> Size:
        if self.shown is None:
            return Size(0.0, 0.0)
        name_w = max(text.measure(n, CHIP_NAME_SIZE, True, CHIP_TRACKING)
                     for n in (self._name(), CHIP_FIT))
        # Whole pixels: the chip repaints on its own every frame it
        # moves, and an antialiased shape on a fractional row leaks past
        # its clip on the GPU.
        return Size(math.ceil(2 * CHIP_PAD_X + name_w + CHIP_ROOM), CHIP_H)

    def path(self) -> skia.Path:
        r = self.rect
        return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                              bevel=CHIP_BEVEL, corners=CORNERS)

    def paint(self, canvas: skia.Canvas) -> None:
        if self.shown is None or self.rect.isEmpty():
            return
        colour = self.shown[3]
        flash = self.state == "calling" and self.on
        k = self.level()
        r = self.rect
        path = self.path()
        # As chip.Chip paints its plate: the fill, the tint, the raster,
        # the frame, the hairline inside it.
        base, base_a = plate_fill(colour)
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(base, base_a * k))
        canvas.drawPath(path, fill)
        tint = skia.Paint(AntiAlias=True)
        tint.setColor(theme.color(
            colour, (CHIP_TINT + (CHIP_FLASH_TINT if flash else 0.0)) * k))
        canvas.drawPath(path, tint)
        paint_raster(canvas, path, colour, RASTER_ALPHA * k)
        frame = skia.Paint(AntiAlias=True)
        frame.setStyle(skia.Paint.kStroke_Style)
        frame.setStrokeWidth(FRAME_W)
        frame.setColor(theme.color(colour, k))
        canvas.drawPath(shapes.beveled(
            r.left(), r.top(), r.width(), r.height(), bevel=CHIP_BEVEL,
            corners=CORNERS, inset=frame_inset()), frame)
        hairline = skia.Paint(AntiAlias=True)
        hairline.setStyle(skia.Paint.kStroke_Style)
        hairline.setStrokeWidth(1.0)
        hairline.setColor(theme.color(colour, CHIP_INSET_ALPHA * k))
        canvas.drawPath(inner_path(r, CHIP_INSET, CHIP_BEVEL), hairline)
        # Centred, the capitals' middle on the chip's middle.
        name = self._name()
        w = text.measure(name, CHIP_NAME_SIZE, True, CHIP_TRACKING)
        cap = text.font(CHIP_NAME_SIZE, True).getMetrics().fCapHeight
        text.draw(canvas, name, round(r.centerX() - w / 2),
                  round(r.centerY() + cap / 2), CHIP_NAME_SIZE,
                  theme.color(colour, k), bold=True, tracking=CHIP_TRACKING)

    # ── input ───────────────────────────────────────────────────────────
    @property
    def interactive(self) -> bool:
        return self.shown is not None

    def hit(self, x: float, y: float) -> Widget | None:
        if self.shown is None or not self.path().contains(x, y):
            return None
        return self

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
