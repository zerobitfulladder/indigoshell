"""Progress on the call: a job's bar under its step, the line of its
stages, and the status plate's one-line version. The numbers come from
agent/progress.py; this only draws them.

The bar is the desktop's own meter: the gauges', the toasts' and the
battery's segmented cells (widgets/meters.py), whole pixels each, lit
left to right, in the agent's violet as the RAM gauge is. While a job
runs, the battery's charging sweep runs through the lit cells, a cell
at a time. A job with no end in sight (busy, no total) sends a short
run of lit cells across instead. The fill eases toward each new value,
so cells light one after another rather than in jumps.
"""

import math
import time

import skia

from ... import text, theme
from ...widgets.meters import tick_count
from ..progress import Job, clock
from .style import ACT_SIZE

# The cells: the battery's 2px-gap rhythm, a little wider and as thick
# as the toasts' meter, so a cell reads at a glance on the plate.
CELL_W = 3
CELL_GAP = 2
CELL_H = 6
LIT = theme.VIOLET_BRIGHT
# The gauges' unlit cells are VIOLET_DIM on the bar; on the agent's plate,
# which is tinted almost that colour, they need lifting to be seen.
UNLIT = theme.lerp(theme.VIOLET_DIM, theme.VIOLET, 0.5)
DONE = theme.VIOLET                     # a finished job's cells, at rest
SWEEP = theme.lerp(theme.VIOLET_BRIGHT, "#ffffff", 0.5)
SWEEP_STEP_S = 0.07                     # the battery's charging step
SWEEP_CELLS = 3                         # how many cells the sweep lights
BUSY_CELLS = 5                          # a busy bar's run
CELL_FPS = 15                           # one frame per sweep step
EASE_PER_S = 9.0            # the fill closes ~90% of a gap in 0.25s
GAP = 10.0
LABEL_COLS = 22             # a job's name takes at most this much
STATUS_LABEL_COLS = 16
BAR_MIN = 72.0


class Bars:
    """Draws jobs, remembering where each bar's fill was drawn last so
    the next frame eases on from there. One per plate."""

    def __init__(self) -> None:
        self._shown: dict[int, tuple[float, float]] = {}  # id -> (fill, t)

    def fill(self, job: Job) -> float | None:
        target = job.fraction()
        if target is None:
            self._shown.pop(job.id, None)
            return None
        now = time.monotonic()
        got = self._shown.get(job.id)
        if got is None or not job.live or target < got[0] - 0.02:
            value = target            # first sight, the end, a new file
        else:
            was, t = got
            value = was + (target - was) * (1 - math.exp(-(now - t)
                                                         * EASE_PER_S))
        self._shown[job.id] = (value, now)
        return value

    def fps(self, jobs: list[Job]) -> int:
        """Frames a second the bars on show need: one per sweep step
        while any of them runs, none once they are all done."""
        ids = {j.id for j in jobs}
        for gone in [i for i in self._shown if i not in ids]:
            del self._shown[gone]
        return CELL_FPS if any(j.live for j in jobs) else 0

    # ── the bar ─────────────────────────────────────────────────────────
    def paint_bar(self, canvas: skia.Canvas, r: skia.Rect, job: Job) -> None:
        stride = CELL_W + CELL_GAP
        n = tick_count(r.width(), CELL_W, CELL_GAP)
        span = n * stride - CELL_GAP
        x0 = round(r.left())
        y0 = round(r.centerY() - CELL_H / 2)
        step = int(time.monotonic() / SWEEP_STEP_S)
        fill = self.fill(job)
        if fill is None:
            # Busy: a run of lit cells stepping across, round and round.
            head = step % (n + BUSY_CELLS)
            lit = set(range(max(0, head - BUSY_CELLS), min(n, head)))
            colors = [(SWEEP if i == head - 1 else LIT) if i in lit
                      else UNLIT for i in range(n)] if job.live \
                else [UNLIT] * n
        else:
            # Lit where a cell's middle is inside the fill, as the meters do.
            end = fill * span
            lit_n = sum(1 for i in range(n) if i * stride + CELL_W / 2 <= end)
            if not job.live:
                colors = [DONE if i < lit_n else UNLIT for i in range(n)]
            else:
                # The charging sweep: a few brighter cells running from
                # the first lit cell to the edge, then again.
                at = step % (lit_n + SWEEP_CELLS) if lit_n else -1
                colors = [(SWEEP if at - SWEEP_CELLS < i <= at else LIT)
                          if i < lit_n else UNLIT for i in range(n)]
        groups: dict[str, skia.Path] = {}
        for i, color in enumerate(colors):
            path = groups.get(color)
            if path is None:
                path = groups[color] = skia.Path()
            path.addRect(skia.Rect.MakeXYWH(x0 + i * stride, y0, CELL_W,
                                            CELL_H))
        paint = skia.Paint(AntiAlias=False)
        for color, path in groups.items():
            paint.setColor(theme.color(color))
            canvas.drawPath(path, paint)

    # ── under a step on the call ────────────────────────────────────────
    def paint_row(self, canvas: skia.Canvas, box: skia.Rect, job: Job,
                  x: float, accent: str) -> None:
        """`name  ━━━━━━━──────  43%  12.4 MB / 51.0 MB · 3.1 MB/s · 0:12 left`"""
        size = ACT_SIZE
        base = text.baseline_in(box, size)
        cell = text.advance(size)
        label = text.ellipsize(job.title(), size,
                               min(LABEL_COLS * cell, box.width() * 0.4))
        text.draw(canvas, label, x, base, size,
                  theme.color(theme.HUD_FG if job.live else theme.BASE_MUTED))
        x += text.measure(label, size) + GAP

        if job.live:
            eta = job.eta()
            tail = f"{clock(eta)} left" if eta is not None \
                else clock(job.elapsed())
            stats = [s for s in (job.amount(), job.speed(), tail) if s]
            pct = job.percent()
        else:
            stats, pct = [job.summary()], ""
        room = box.right() - x - BAR_MIN - GAP
        pct_w = text.measure(pct, size, bold=True) + GAP if pct else 0.0
        right = " · ".join(stats)
        # Too long for the row: the speed goes first, then the amount.
        while len(stats) > 1 and text.measure(right, size) + pct_w > room:
            stats.pop(1 if len(stats) > 2 else 0)
            right = " · ".join(stats)
        right = text.ellipsize(right, size, max(0.0, room - pct_w))
        rw = text.measure(right, size)
        text.draw(canvas, right, box.right() - rw, base, size,
                  theme.color(theme.HUD_FG if job.live else theme.BASE_MUTED))
        if pct:
            text.draw(canvas, pct, box.right() - rw - pct_w, base, size,
                      theme.color(accent), bold=True)
        bar_right = box.right() - rw - pct_w - (GAP if rw else 0)
        if bar_right - x >= BAR_MIN / 2:
            self.paint_bar(canvas, skia.Rect.MakeLTRB(x, box.top(), bar_right,
                                                      box.bottom()), job)

    def paint_info(self, canvas: skia.Canvas, box: skia.Rect, job: Job,
                   x: float, accent: str) -> None:
        """`download › audio › transcribe › write    Turkish, 9:48 long`:
        steps done in the text colour, the running one in the agent's,
        those to come muted."""
        size = ACT_SIZE
        base = text.baseline_in(box, size)
        runs = _stage_runs(job, accent)
        width = sum(text.measure(t, size, bold=b) for t, _c, b in runs)
        if width > box.right() - x:
            # Too many to list: where it is, and of how many.
            here = job.stages.index(job.stage) + 1 \
                if job.stage in job.stages else 0
            runs = [(f"{here}/{len(job.stages)} " if here else "", theme.HUD_FG,
                     False), (job.stage, accent, True)]
        for value, color, bold in runs:
            if not value:
                continue
            value = text.ellipsize(value, size, box.right() - x, bold=bold)
            text.draw(canvas, value, x, base, size, theme.color(color), bold)
            x += text.measure(value, size, bold=bold)
        if job.text and box.right() - x > 4 * GAP:
            x += 2 * GAP if runs else 0.0
            text.draw(canvas, text.ellipsize(job.text, size, box.right() - x),
                      x, base, size, theme.color(theme.BASE_MUTED))

    # ── the status plate ────────────────────────────────────────────────
    def paint_compact(self, canvas: skia.Canvas, box: skia.Rect, job: Job,
                      accent: str) -> None:
        """`transcribe 3/4  ━━━━━━━────  43% 0:12`"""
        size = ACT_SIZE
        base = text.baseline_in(box, size)
        cell = text.advance(size)
        title = job.stage or job.title()
        if job.stage in job.stages:
            title += f" {job.stages.index(job.stage) + 1}/{len(job.stages)}"
        if not job.live:
            title = job.title()
        label = text.ellipsize(title, size, STATUS_LABEL_COLS * cell)
        text.draw(canvas, label, box.left(), base, size,
                  theme.color(theme.HUD_FG))
        x = box.left() + text.measure(label, size) + GAP
        if job.live:
            eta = job.eta()
            right = " ".join(s for s in (job.percent(), clock(eta) if eta
                                         is not None else "") if s) \
                or clock(job.elapsed())
        else:
            right = job.summary()
        right = text.ellipsize(right, size, max(0.0, box.right() - x - GAP))
        rw = text.measure(right, size)
        text.draw(canvas, right, box.right() - rw, base, size,
                  theme.color(theme.HUD_FG if job.live else theme.BASE_MUTED))
        bar_right = box.right() - rw - GAP
        if job.live and bar_right - x >= BAR_MIN / 2:
            self.paint_bar(canvas, skia.Rect.MakeLTRB(x, box.top(), bar_right,
                                                      box.bottom()), job)


def _stage_runs(job: Job, accent: str) -> list[tuple[str, str, bool]]:
    if not job.stages:
        return [(job.stage, accent, True)] if job.stage else []
    here = job.stages.index(job.stage) if job.stage in job.stages else -1
    runs: list[tuple[str, str, bool]] = []
    for i, name in enumerate(job.stages):
        if i:
            runs.append((" › ", theme.BASE_MUTED, False))
        if i == here and job.live:
            runs.append((name, accent, True))
        else:
            runs.append((name, theme.HUD_FG if i < here or not job.live
                         else theme.BASE_MUTED, False))
    return runs


