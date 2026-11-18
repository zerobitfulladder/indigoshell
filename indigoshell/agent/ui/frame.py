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

Both only say whether they are moving right now (`fps`). The window
repaints a widget once more when it stops asking for frames, so neither
has to see its own last frame off the screen.
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
        # The turn that just ended: (when, its lap, how far round it).
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
            self._finish = None
        return None

    def fps(self, agent) -> int:
        if agent.busy:
            return TRACE_FPS
        return FINISH_FPS if self._progress(agent) is not None else 0

    def paint(self, canvas: skia.Canvas, r: skia.Rect, agent,
              accent: str) -> None:
        got = self._progress(agent)
        if got is None:
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

    def fire(self) -> None:
        self._at = time.monotonic()

    def active(self) -> bool:
        return (self._at is not None
                and time.monotonic() - self._at < COMET_S)

    def fps(self) -> int:
        return COMET_FPS if self.active() else 0

    def paint(self, canvas: skia.Canvas, r: skia.Rect) -> None:
        if not self.active():
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
