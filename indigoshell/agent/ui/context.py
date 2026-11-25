"""How full the chat is: a short meter in her plate's heading.

The hub keeps one chat and compacts it as it grows (lilac's PROTOCOL.md,
"The chat"): `state.context` says how many tokens it holds (`used`)
against the most it lets it hold (`cap`, 500k), and from how many it
compacts it after an idle hour (`compact_at`, 200k). The user watches
this meter to decide when to ask her to compact, so it says exactly
that and nothing more: METER_W of the shell's level cells (whole
pixels, lit left to right, as every level in the shell is) in her
colour, faint, a short tick at `compact_at`, and the number after it
("212k"), left of the model in her heading (the user's pick, from
prototypes/visuals/render_call_chat.py's sheet). `used` unknown (just
compacted, not yet measured): no cell lit, no number.

While the hub compacts, a short run of brighter cells sweeps back
through the lit ones, right to left, a cell a step: the progress bars'
sweep (bars.py), going the way the chat is about to. When it is done,
the plate says so in a small note under the turn (`note`).
"""

import time

import skia

from ... import text, theme
from ...widgets.meters import tick_count
from .bars import CELL_GAP, CELL_W, SWEEP_CELLS, SWEEP_STEP_S
from .style import TAG_SIZE

METER_W = 96                # the cells' width in the heading
CELL_H = 3
TICK_H = 7                  # the mark at compact_at, centred on the cells
NUMBER_GAP = 8              # cells ↔ the number
SETTING_GAP = 14            # the number ↔ the model, as the clock keeps it
LIT_ALPHA = 0.55
UNLIT_ALPHA = 0.13
TICK = theme.YELLOW_DIM     # a cap, as the volume's cap line is yellow
SWEEP_FPS = 15              # a frame per sweep step


def k_tokens(n: int | None) -> str:
    """212000 -> "212k"."""
    if n is None:
        return ""
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def note(compacted: dict | None) -> str:
    """The note a compaction leaves: "compacted 212k → 38k"."""
    if not compacted:
        return ""
    before, after = compacted.get("before"), compacted.get("after")
    words = "compacted"
    if before is not None:
        words += f" {k_tokens(before)}"
        if after is not None:
            words += f" → {k_tokens(after)}"
    return words


def shown(agent) -> dict | None:
    """The context to draw, if the hub has told one."""
    ctx = getattr(agent, "context", None)
    return ctx if ctx and ctx.get("cap") else None


def fps(agent) -> int:
    return SWEEP_FPS if getattr(agent, "compacting", False) \
        and shown(agent) is not None else 0


def heading(canvas: skia.Canvas, box: skia.Rect, agent, accent: str,
            right: float) -> float:
    """The meter and its number in the heading `box`, ending at `right`.
    Where it begins, for what goes left of it (the turn clock); `right`
    itself with no context told."""
    ctx = shown(agent)
    if ctx is None:
        return right
    used = ctx.get("used")
    right -= SETTING_GAP
    if used is not None:
        number = k_tokens(used)
        right -= text.measure(number, TAG_SIZE)
        text.draw(canvas, number, right, text.baseline_in(box, TAG_SIZE),
                  TAG_SIZE, theme.color(theme.BASE_MUTED))
        right -= NUMBER_GAP
    cells = skia.Rect.MakeLTRB(right - METER_W, box.top(), right,
                               box.bottom())
    paint(canvas, cells, agent, accent)
    return cells.left()


def paint(canvas: skia.Canvas, r: skia.Rect, agent, accent: str,
          now: float | None = None) -> None:
    """The cells on whole pixels inside `r` (centred in its height), and
    the tick."""
    ctx = shown(agent)
    if ctx is None:
        return
    stride = CELL_W + CELL_GAP
    n = tick_count(r.width(), CELL_W, CELL_GAP)
    if n <= 0:
        return
    cap = ctx["cap"]
    used = ctx.get("used")
    lit = 0 if used is None else max(0, min(n, round(used / cap * n)))
    if used:
        lit = max(lit, 1)                       # a chat that holds anything
    x0 = round(r.left())
    y = round(r.centerY() - CELL_H / 2)
    off = skia.Paint(AntiAlias=False)
    off.setColor(theme.color(accent, UNLIT_ALPHA))
    on = skia.Paint(AntiAlias=False)
    on.setColor(theme.color(accent, LIT_ALPHA))
    hot = skia.Paint(AntiAlias=False)
    hot.setColor(theme.color(theme.lerp(accent, "#ffffff", 0.35)))
    sweep: range = range(0)
    if getattr(agent, "compacting", False) and lit:
        now = time.monotonic() if now is None else now
        steps = int(now / SWEEP_STEP_S) % (lit + SWEEP_CELLS)
        head = lit - 1 - steps + SWEEP_CELLS    # right to left
        sweep = range(max(0, head - SWEEP_CELLS + 1), min(lit, head + 1))
    paths = {"off": skia.Path(), "on": skia.Path(), "hot": skia.Path()}
    for i in range(n):
        which = "hot" if i in sweep else "on" if i < lit else "off"
        paths[which].addRect(skia.Rect.MakeXYWH(x0 + i * stride, y,
                                                CELL_W, CELL_H))
    canvas.drawPath(paths["off"], off)
    canvas.drawPath(paths["on"], on)
    canvas.drawPath(paths["hot"], hot)
    mark = ctx.get("compact_at")
    if mark and mark < cap:
        cell = round(mark / cap * n)
        tx = x0 + cell * stride - (CELL_GAP + 1) // 2
        tick = skia.Paint(AntiAlias=False)
        tick.setColor(theme.color(TICK))
        canvas.drawRect(skia.Rect.MakeXYWH(
            tx, round(r.centerY() - TICK_H / 2), 1, TICK_H), tick)
