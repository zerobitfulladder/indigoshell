"""How the reply arrives: the lyrics' decode, per chunk of streamed text.

The reply streams in a few characters at a time, so a reveal cannot play
once for the whole message. Each character's arrival time is recorded
the first time a row shows it; a run of characters that arrived together
is one chunk, and each chunk plays the lyrics' `GlitchWipe` by its own
age — cells, tears and colour fringes, settled in `HOLD`. A pause in the
stream therefore never leaves text undecoded, and nothing readable is
held back for more than a fraction of a second.

The wipe's own cursor bar is off — one per chunk would stutter — and a
single violet bar with a trailing glow leads the stream instead. Settled
text is painted exactly as before, on the same grid columns, so
selection and copy are untouched.

Text that is already there when the plate first sees it (an old reply
when the call opens, a resumed conversation) is settled from the start.
"""

import bisect
import dataclasses
import time

import skia

from ... import text, theme
from ...effects import GlitchWipe, paint_through
from .rows import Row, paint_line

HOLD = 0.42                 # a chunk's decode, start to settled
REVEAL_FPS = 30
BAR_GLOW_PX = 22.0


class Reveal:
    def __init__(self) -> None:
        violet, cyan = (_rgb(theme.VIOLET_BRIGHT), _rgb(theme.CYAN_BRIGHT))
        self.wipe = GlitchWipe(duration=HOLD, scan=0.12, settle=0.88,
                               shift=3.0, split=2.0, after=0.0,
                               spark=cyan, cursor=violet, bar=False)
        self.arrivals: dict[str, list[float]] = {}
        self._turn = None
        self._latest = 0.0          # when the newest character arrived

    # ── bookkeeping ─────────────────────────────────────────────────────
    def track(self, agent, rows: list[Row]) -> None:
        """Note when each character of the reply's rows first appeared.
        Called whenever the rows are built; cheap when nothing changed."""
        if agent.turn_started != self._turn:
            self._turn = agent.turn_started
            self.arrivals.clear()
        now = time.monotonic()
        stamp = now if agent.busy else 0.0      # streaming, or settled
        for row in rows:
            if not row.key.startswith("m"):
                continue
            arr = self.arrivals.setdefault(row.key, [])
            n = len(row.src)
            if n > len(arr):
                arr.extend([stamp] * (n - len(arr)))
                if stamp:
                    self._latest = now
            elif n < len(arr):          # the Markdown re-read shortened it
                del arr[n:]

    def fps(self) -> int:
        """Frames while the newest chunk is still decoding."""
        return REVEAL_FPS if time.monotonic() - self._latest < HOLD else 0

    def _arrival(self, row: Row, i: int) -> float:
        arr = self.arrivals.get(row.key, ())
        return arr[i] if i < len(arr) else 0.0

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas, box: skia.Rect, row: Row, sel,
              accent: str) -> None:
        """A reply row: the settled part as usual, each fresh chunk
        through the wipe, and the leading bar while it streams."""
        now = time.monotonic()
        arr = self.arrivals.get(row.key)
        # Arrival times only grow along a row, so the first fresh index is
        # a bisection away — and a row whose last character has settled
        # is drawn exactly as without a reveal. Scanning every character
        # of every row each frame was most of what a streaming reply cost.
        if not arr or row.end == row.start \
                or now - self._arrival(row, row.end - 1) >= HOLD:
            paint_line(canvas, box, row, sel, accent)
            if row.cursor:
                _bar(canvas, box, row, accent)
            return
        k = bisect.bisect_left(arr, now - HOLD, row.start,
                               min(row.end, len(arr)))
        paint_line(canvas, box, _piece(row, row.start, k), sel, accent)
        cell = text.advance(row.size)
        x0 = box.left() + row.x
        i = k
        while i < row.end:
            t0 = self._arrival(row, i)
            j = i
            while j < row.end and self._arrival(row, j) == t0:
                j += 1
            sub = _piece(row, i, j)
            left = box.left() if i == row.start else x0 + (i - row.start) * cell
            rect = skia.Rect.MakeLTRB(left, box.top(),
                                      x0 + (j - row.start) * cell, box.bottom())
            paint_through(
                canvas, rect,
                lambda cv, s=sub: paint_line(cv, box, s, None, accent),
                [self.wipe], t=now, appear=max(0.002, (now - t0) / HOLD),
                seed=(i * 7.31) % 13.0)
            i = j
        if row.cursor:
            _bar(canvas, box, row, accent)


def _rgb(spec: str) -> tuple[float, float, float]:
    c = theme.color4f(spec)
    return c.fR, c.fG, c.fB


def _piece(row: Row, a: int, b: int) -> Row:
    """The part of `row` from `a` to `b`, placed where it sits on the
    grid, without the row's cursor (and its bullet and bar after the
    first piece)."""
    extra = {} if a == row.start else {"bullet": "", "bar": None}
    return dataclasses.replace(
        row, start=a, end=b, cursor=False,
        x=row.x + (a - row.start) * text.advance(row.size), **extra)


def _bar(canvas: skia.Canvas, box: skia.Rect, row: Row, accent: str) -> None:
    """The stream's lead: a 2px bar with a glow trailing behind it, as
    the lyrics' wipe draws its cursor."""
    x = (box.left() + row.x
         + (row.end - row.start) * text.advance(row.size) + 1)
    top, bottom = box.top() + 2, box.bottom() - 2
    glow = skia.GradientShader.MakeLinear(
        [skia.Point(x - BAR_GLOW_PX, 0), skia.Point(x, 0)],
        [theme.color(accent, 0.0), theme.color(accent, 0.42)])
    canvas.drawRect(skia.Rect.MakeLTRB(x - BAR_GLOW_PX, top, x, bottom),
                    skia.Paint(Shader=glow))
    solid = skia.Paint(AntiAlias=False)
    solid.setColor(theme.color(accent))
    canvas.drawRect(skia.Rect.MakeXYWH(x, top - 1, 2, bottom - top + 2), solid)
    halo = skia.Paint(AntiAlias=True)
    halo.setColor(theme.color(accent, 0.5))
    halo.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 2.0))
    canvas.drawRect(skia.Rect.MakeXYWH(x - 1, top - 1, 4, bottom - top + 2),
                    halo)
