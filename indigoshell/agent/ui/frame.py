"""What a frame does besides framing: the turn trace and the send comet.

**The trace** is the toasts' timer border, used as the agent's turn
clock. While a turn runs, the agent's frame dims and is redrawn
clockwise from the top-left corner with a small yellow head, one lap a
minute; the next lap takes it back out, so the frame never snaps from
full to empty. When the answer lands, the rest of the lap sweeps closed
and the frame is whole again — at rest it is simply the frame. The
hung-up status plate carries the same clock.

**The comet** runs once round your plate when you send a line, in step
with your chip's half-turn.

**The pulse** is the timing for a short effect that repeats — the
hologram's scan (holo.py) — which animates only while it plays and
schedules its own wake-up for the next pass, so an idle call keeps its
frame clock off in between.
"""

import time

import skia

from ... import theme
from .style import AGENT_FILL, FRAME_W, frame_inset, plate_path

TRACE_LAP_S = 60.0          # one lap of the frame per minute of the turn
TRACE_FINISH_S = 0.45       # the closing sweep when the answer lands
TRACE_HEAD_PX = 22.0
# How far the head's glow reaches past the plate. A plate with a trace
# repaints this much around itself (`paint_bounds`), or the glow it
# leaves outside its own rect is never cleared.
TRACE_BLEED = 10.0
# 30, not lower: at 12 the head visibly hopped along the frame. Only the
# agent's plate (or the status plate) repaints at this rate, and only
# while a turn runs.
TRACE_FPS = 30
FINISH_FPS = 30
TRACE_DIM = 0.8             # how far the untraced frame fades into the plate
COMET_S = 0.45
COMET_TAIL_PX = 160.0
COMET_FPS = 30
PULSE_FPS = 30
# Every timed effect asks for frames this long past its end. The clock
# stops the moment a widget asks for none, so the last frame painted is
# whichever ran *before* the end — a comet two-thirds round, a scanline
# near the bottom — and it stayed on screen until something else
# repainted. A few more frames guarantee the rest state gets painted.
SETTLE_GRACE_S = 0.15


def _stroke(color: str, alpha: float | None = None, width: float = FRAME_W,
            blur: float = 0.0) -> skia.Paint:
    p = skia.Paint(AntiAlias=True)
    p.setStyle(skia.Paint.kStroke_Style)
    p.setStrokeWidth(width)
    p.setColor(theme.color(color, alpha))
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle,
                                                 blur))
    return p


def _ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


class Trace:
    """The turn clock on one frame. Call `turn_ended()` when the agent
    goes from busy to idle; read `fps(agent)` for the frame rate it
    needs; `paint()` over the plate's own frame."""

    def __init__(self) -> None:
        self._finish: tuple[float, int, float] | None = None

    def turn_ended(self, agent) -> None:
        if not agent.turn_started:
            return
        e = time.monotonic() - agent.turn_started
        self._finish = (time.monotonic(), int(e // TRACE_LAP_S),
                        (e % TRACE_LAP_S) / TRACE_LAP_S)

    def _progress(self, agent) -> tuple[int, float, float] | None:
        """(lap, fraction of it, closing sweep 0..1), or None at rest."""
        if agent.busy:
            self._finish = None
            e = time.monotonic() - agent.turn_started
            return int(e // TRACE_LAP_S), (e % TRACE_LAP_S) / TRACE_LAP_S, 0.0
        if self._finish is not None:
            t0, lap, f = self._finish
            k = (time.monotonic() - t0) / TRACE_FINISH_S
            if k < 1.0:
                return lap, f, _ease_out(k)
        return None

    def fps(self, agent) -> int:
        if agent.busy:
            return TRACE_FPS
        return FINISH_FPS if self._finish is not None else 0

    def paint(self, canvas: skia.Canvas, r: skia.Rect, agent,
              accent: str) -> None:
        got = self._progress(agent)
        if got is None:
            # The sweep is over only once a paint covering the whole frame
            # has drawn it without the trace. Ended by any paint, a partial
            # one (the caret blinking, a scan passing) stopped the frames
            # and left the rest of the frame half swept.
            if self._finish is not None and not agent.busy \
                    and canvas.getLocalClipBounds().contains(r):
                self._finish = None
            return
        lap, f, done = got
        border = plate_path(r, inset=frame_inset())
        measure = skia.PathMeasure(border, False)
        length = measure.getLength()
        dim = theme.lerp(accent, AGENT_FILL[0], TRACE_DIM)
        canvas.drawPath(border, _stroke(dim))
        if lap % 2 == 0:            # even laps draw the frame in...
            lo, hi = 0.0, f * length + (length - f * length) * done
            head = hi
        else:                       # ...odd laps take it back out
            lo, hi = f * length * (1 - done), length
            head = lo
        seg = skia.Path()
        if measure.getSegment(lo, hi, seg, True):
            canvas.drawPath(seg, _stroke(accent))
        if done < 1.0:
            a0, a1 = ((head - TRACE_HEAD_PX, head) if lap % 2 == 0
                      else (head, head + TRACE_HEAD_PX))
            tip = skia.Path()
            if measure.getSegment(max(0.0, a0), min(length, a1), tip, True):
                canvas.drawPath(tip, _stroke(theme.YELLOW_BRIGHT, 0.7, 4.0,
                                             blur=3.0))
                canvas.drawPath(tip, _stroke(theme.YELLOW_BRIGHT,
                                             width=FRAME_W + 0.5))


class Comet:
    """One lap of light round a plate, fired when a line is sent."""

    def __init__(self, color: str = theme.MAGENTA_BLOOM) -> None:
        self.color = color
        self._at: float | None = None
        self._painted_lit = False

    def fire(self) -> None:
        self._at = time.monotonic()

    def active(self) -> bool:
        return (self._at is not None
                and time.monotonic() - self._at < COMET_S)

    def fps(self) -> int:
        # Frames until one has been painted without the comet: a grace
        # window alone was not enough when a stall swallowed it.
        if self.active() or self._painted_lit:
            return COMET_FPS
        return 0

    def paint(self, canvas: skia.Canvas, r: skia.Rect) -> None:
        self._painted_lit = self.active()
        if not self._painted_lit:
            return
        k = (time.monotonic() - self._at) / COMET_S
        border = plate_path(r, inset=frame_inset())
        measure = skia.PathMeasure(border, False)
        length = measure.getLength()
        head = _ease_out(k) * length
        steps = 8
        for i in range(steps):
            a = head - COMET_TAIL_PX * (1 - i / steps)
            b = head - COMET_TAIL_PX * (1 - (i + 1) / steps)
            seg = skia.Path()
            if measure.getSegment(max(0.0, a), max(0.0, b), seg, True):
                canvas.drawPath(seg, _stroke(
                    self.color, (i + 1) / steps * (1 - k * 0.3),
                    FRAME_W + 0.5))


class Pulse:
    """A short effect, `duration` seconds out of every `period`, shifted
    by `offset`. `fps(widget)` asks for frames only while it plays; when
    it is between passes it schedules one `invalidate()` for the start of
    the next, which is what wakes a stopped frame clock."""

    def __init__(self, period: float, duration: float,
                 offset: float = 0.0) -> None:
        self.period, self.duration, self.offset = period, duration, offset
        self._t0 = time.monotonic()
        self._handle = None

    def _k(self) -> float:
        return (time.monotonic() - self._t0 + self.offset) % self.period

    def elapsed(self) -> float:
        """Seconds since the current pass started (it plays for the first
        `duration` of them)."""
        return self._k()

    def phase(self) -> float | None:
        """0..1 through the current pass, or None between passes."""
        k = self._k()
        return k / self.duration if k < self.duration else None

    def fps(self, widget) -> int:
        if self._k() < self.duration + SETTLE_GRACE_S:
            return PULSE_FPS
        self._wake(widget)
        return 0

    def _wake(self, widget) -> None:
        win = getattr(widget, "window", None)
        if self._handle is not None or win is None or win.loop is None:
            return
        delay = self.period - self._k()
        self._handle = win.loop.call_later(delay, self._fire, widget)

    def _fire(self, widget) -> None:
        self._handle = None
        widget.invalidate()

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

