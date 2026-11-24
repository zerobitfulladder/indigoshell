"""While a hung-up call works or rings.

`AgentChip`, the top bar's right end (config_default's `top_bar`): while
the agent works with its call hung up, a small beveled chip with the
name of the voice on the task and, right of it, a light in that voice's
colour, blinking; when the agent calls back (a turn ended, or a question
for you), the whole chip blinks, until the call is answered or the ring
is seen elsewhere. Nothing at all the rest of the time, and no room
taken. A click answers (opens the call); a right-click silences a ring.

`CallRouter`, the service that watches the agent while its call is
closed and decides what shows: the chip, always; `call-status` only
while an agent of this desktop works, when `show_working` asks for it.

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
    """Watches a call's agent while the call is closed, and says what
    shows for it: the top bar's chip (`AgentChip`), and `call-status`.

    A SERVICES entry: neither window is alive to decide this while it is
    closed, so something outside them has to watch the agent.

    `background` is what the chip shows: "calling" while the agent rings
    or asks with its call closed, "working" while a turn runs (or a
    command it left running) with the call closed, None otherwise. A chip
    `follow`s it. The call open, nothing: the call shows its own state.

    `show_working=True` puts `call-status` in the corner instead while
    the agent works with its call closed (the turn's steps and clock, the
    bars of its long commands), for the desktop's own agent; the chip
    stays off meanwhile, as the plate would cover it. A ring or a
    question never opens it: the chip blinks for those. `False` keeps the
    corner empty, for an agent on its own machine (a `RemoteAgent`).
    """

    def __init__(self, agent: Agent, *, call: str = CALL_WINDOW,
                 status: str = STATUS_WINDOW,
                 show_working: bool = True) -> None:
        self.agent = agent
        self.call = call
        self.status = status
        self.show_working = show_working
        self.background: str | None = None
        self._followers: list[Callable[[], None]] = []
        self._unsub = None

    def start(self) -> None:
        self._unsub = self.agent.subscribe(self._on_agent)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self.background = None
        self._tell()

    def follow(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Have `fn()` called after each look at the agent (it reads
        `background` and the agent itself); returns the undo."""
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
        working = closed and (a.busy
                              or bool(a.jobs.shown()))   # a command left running
        want = working and self.show_working and not calling
        have = self.status in d.instances
        if want and not have:
            d.open(self.status)
        elif have and not want:
            d.close(self.status)
        self.background = ("calling" if calling
                           else "working" if working and not want else None)
        self._tell()


# ── the top bar's chip ──────────────────────────────────────────────────
# The call's small square chip (chip.py), a bar's height tall and as long
# as the name: the same plate in the voice's colour (style.plate_fill:
# the plates' violet-black leaning to it, then a tint of it on top), the
# hologram's raster across it (holo: a faint line every 3px, the call
# plates' stripes, on the window's own pixel grid), its 2px frame and the
# faint hairline inside it (style.inner_path at CHIP_INSET). The corners
# are cut at the top-right and bottom-left, the call's bevel scaled to
# the height. The name is set like the INDIGO tag at the bar's other end,
# in bold capitals, a step smaller to sit in the chip. The light is the
# network widget's blinking dot made round: the full colour and its
# small glow while lit, a quarter of it between.
CHIP_H = 28                 # in the 42px bar: 7px clear above and below
CHIP_PAD_X = 11             # the name and the light clear of the hairline
CHIP_BEVEL = round(BEVEL * CHIP_H / CHIP_SM)    # the call chip's cut: 8
# The voice's colour over the plate, as chip.Chip paints it, and the
# extra it gets in a ring's flash.
CHIP_TINT = 0.16
CHIP_FLASH_TINT = 0.14
CHIP_NAME_SIZE = text.pt(11)    # 14.7px; the INDIGO tag is 13pt
CHIP_TRACKING = 1.0
CHIP_NAME_MAX_W = 160       # a longer voice name ends in an ellipsis
LIGHT_D = 7
LIGHT_GAP = 8               # name ↔ light
LIGHT_DIM = 0.25            # the network dots' dim step...
LIGHT_GLOW = 2.0            # ...and their glow (blur sigma) while lit
LIGHT_GLOW_ALPHA = 0.55
# Blinks, as (ms, lit) steps like theme.WORKSPACE_URGENT_RING, looped
# from the moment the state began. Frames come only on their steps
# (`_blink_fps`): 2 a second for the light, 20 for the ring.
WORK_BLINK = ((500, True), (500, False))    # the light while it works: 1 Hz
CALL_BLINK = theme.WORKSPACE_URGENT_RING    # the whole chip calling back:
#                                             the bar's ring, 150/100/150/700
CALL_DIM = 0.2              # the whole chip between the ring's flashes


def _blink_fps(pattern) -> int:
    """Frames a second that land on every step of `pattern`: 1000 over
    the steps' greatest common divisor (50ms steps: 20). 0 with none."""
    steps = [int(ms) for ms, _lit in pattern or ()]
    gcd = math.gcd(*steps) if steps else 0
    return max(1, round(1000 / gcd)) if gcd else 0


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
    """The top bar's agent chip: the voice's name and a light, on a
    beveled plate, while the agent works in the background; the whole
    chip blinking while it calls back; nothing otherwise.

    It follows `routers` (`CallRouter`s), which decide what it shows.
    None, the default, finds them among the running daemon's SERVICES
    when the bar opens, so a config wires nothing for it; with no agent
    configured it stays empty and costs nothing. With several, a calling
    one goes first, else the first working.

    While hidden it is `collapsed`: no width, and no gap in the bar's
    row. Its light is a widget of its own, so the working blink repaints
    the dot and not the chip; the chip itself asks for frames only while
    it calls back.
    """

    def __init__(self, routers=None) -> None:
        super().__init__()
        self._given = list(routers) if routers is not None else None
        self.routers: list[CallRouter] = []
        self._unfollow: list[Callable[[], None]] = []
        self.light = _Light(self)
        # On show: (router, "working" | "calling", name, colour), or None.
        self.shown: tuple | None = None
        self.since = 0.0        # when the state on show began: its blink's start
        self.on = True          # lit, or between two of the ring's flashes

    def children(self):
        return (self.light,)

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
        pick = next((r for r in self.routers if r.background == "calling"),
                    None) or next((r for r in self.routers
                                   if r.background == "working"), None)
        shown = None
        if pick is not None:
            a = pick.agent
            name = str(getattr(a, "name", "") or "agent").upper()
            shown = (pick, pick.background, name, own_accent(a))
        old = self.shown
        if shown == old:
            return              # the agent's stream, nothing on show changed
        if old is None or shown is None or old[:2] != shown[:2]:
            # A new state starts its blink lit.
            self.since = time.monotonic()
            self.on = self.light.lit = True
        self.shown = shown
        # Raising a rate wakes the window's clock through this damage.
        self.invalidate(layout=old is None or shown is None
                        or old[2] != shown[2])

    # ── motion ──────────────────────────────────────────────────────────
    @property
    def animation_fps(self) -> int:
        return _blink_fps(CALL_BLINK) if self.state == "calling" else 0

    def animate(self, t: float) -> None:
        self.on = (self.state != "calling"
                   or _blink_lit(CALL_BLINK, time.monotonic() - self.since))

    def level(self) -> float:
        """The whole chip's opacity: dim between the ring's flashes."""
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
        name_w = text.measure(self._name(), CHIP_NAME_SIZE, True,
                              CHIP_TRACKING)
        # Whole pixels: the light repaints on its own, and an
        # antialiased shape on a fractional row leaks past its clip.
        return Size(math.ceil(CHIP_PAD_X + name_w + LIGHT_GAP + LIGHT_D
                              + CHIP_PAD_X), CHIP_H)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        x = rect.right() - CHIP_PAD_X - LIGHT_D
        self.light.arrange(skia.Rect.MakeXYWH(
            x, rect.centerY() - LIGHT_D / 2, LIGHT_D, LIGHT_D))

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
        # The capitals' middle on the chip's middle, level with the light.
        cap = text.font(CHIP_NAME_SIZE, True).getMetrics().fCapHeight
        text.draw(canvas, self._name(), r.left() + CHIP_PAD_X,
                  round(r.centerY() + cap / 2), CHIP_NAME_SIZE,
                  theme.color(colour, k), bold=True, tracking=CHIP_TRACKING)
        self.light.paint(canvas)

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


class _Light(Widget):
    """The chip's light, a widget of its own so that each blink while
    the agent works damages only the dot and its glow."""

    def __init__(self, chip: AgentChip) -> None:
        super().__init__()
        self.chip = chip
        self.lit = True

    @property
    def animation_fps(self) -> int:
        return _blink_fps(WORK_BLINK) if self.chip.state == "working" else 0

    def animate(self, t: float) -> None:
        chip = self.chip
        self.lit = (chip.state != "working"
                    or _blink_lit(WORK_BLINK, time.monotonic() - chip.since))

    def measure(self, avail: Size) -> Size:
        return Size(LIGHT_D, LIGHT_D)

    def paint_bounds(self) -> skia.Rect:
        # The glow, and room past it: a shape whose antialiased fringe
        # ends at the clip leaks a pixel past it on the GPU.
        pad = 3 * LIGHT_GLOW + 2
        return self.rect.makeOutset(pad, pad)

    def paint(self, canvas: skia.Canvas) -> None:
        chip = self.chip
        if chip.shown is None:
            return
        colour = chip.shown[3]
        lit = self.lit or chip.state != "working"
        k = chip.level()
        r = self.rect
        cx, cy, radius = r.centerX(), r.centerY(), LIGHT_D / 2
        if lit and k >= 1.0:
            glow = skia.Paint(AntiAlias=True)
            glow.setColor(theme.color(colour, LIGHT_GLOW_ALPHA))
            glow.setMaskFilter(skia.MaskFilter.MakeBlur(
                skia.kNormal_BlurStyle, LIGHT_GLOW))
            canvas.drawCircle(cx, cy, radius, glow)
        dot = skia.Paint(AntiAlias=True)
        dot.setColor(theme.color(colour, (1.0 if lit else LIGHT_DIM) * k))
        canvas.drawCircle(cx, cy, radius, dot)
