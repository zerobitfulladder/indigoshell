"""The call's context bar and its scrollback at 1x, from the real
widgets, beside the alternatives that were weighed.

    PYTHONPATH=../.. ../../.venv/bin/python render_call_chat.py

Two sheets, each a row per variant and a column per state:

- `call_context.png`: how full the chat is. A (built): a faint row of
  cells under her heading, a tick at compact_at, no number. B: the
  same row at the plate's foot, with "61k / 500k" after it. C: a short
  meter in the heading, before the model, with the number.
- `call_scrollback.png`: the plate scrolled back. S1 (built): each
  exchange under a muted `// 21:14` row. S2: a hairline rule across with
  the time at its right end. S3: no row of its own, the time at the right
  of your first line.

The live code draws A and S1; B, C, S2 and S3 are drawn here by
patching the plate's paint, so they are honest previews of the same
widgets, not sketches.
"""
import time

import skia

from common import txt
from indigoshell import text, theme
from indigoshell.agent.activity import Activity
from indigoshell.agent.ui import agent_plate as plate_mod, context
from indigoshell.agent.ui.agent_plate import CONTEXT_H, AgentPlate
from indigoshell.agent.ui.call import CallHost
from indigoshell.agent.ui.rows import Row
from indigoshell.agent.ui.style import BODY_SIZE, TAG_SIZE, line_h, own_accent
from indigoshell.widgets.base import Size, Widget
from render_call import FakeWindow, agent_in, shot

NOW = time.time()
CELLS = context.paint           # the real cells, for the variants to reuse


def call(agent, setup=None):
    """render_call's call, with a look at the host before the shot."""
    host = CallHost(agent)
    Widget.attach(host, FakeWindow())
    if setup is not None:
        setup(host)
    size = host.measure(Size(10_000, 10_000))
    return shot(host, int(size.width), int(size.height))


def exchanges():
    """Four earlier exchanges and how the hub keeps them."""
    day = 86400
    return [
        {"id": 811, "at": NOW - day - 3 * 3600, "source": "user",
         "device": "iris", "lines": ["is the pi still hot after the update"],
         "reply": "Cooled off, choom. **52 °C** and falling; the fan curve "
                  "kicked in at 60.", "attached": 0, "shown": []},
        {"id": 812, "at": NOW - 5 * 3600, "source": "hub",
         "device": None, "lines": ["job transcribe talk.mp4 ended: ok"],
         "reply": "Transcript's done: `~/Videos/talk.txt`, 41 minutes, "
                  "clean.", "attached": 0, "shown": []},
        {"id": 813, "at": NOW - 40 * 60, "source": "user",
         "device": "indigo", "lines": ["what's using my disk",
                                       "just home"],
         "reply": "Projects is the big one at **21G**. The cache is 1.2G:\n\n"
                  "- `.cache` 1.2G\n- `Videos` 3.4G\n- `Projects` 21G",
         "attached": 0, "shown": []},
        {"id": 814, "at": NOW - 12 * 60, "source": "user",
         "device": "indigo", "lines": ["clear the thumbnails then"],
         "reply": "Flatlined them. 380M back.", "attached": 0, "shown": []},
    ]


def hub(**state):
    """The call's agent as a page of scrollback and a turn on show left
    it, on the line."""
    ctx = state.pop("context", {"used": 61_000, "compact_at": 200_000,
                                "cap": 500_000, "window": 1_000_000,
                                "from": None})
    xs = state.pop("exchanges", exchanges())
    edge = state.pop("edge", None)
    defaults = dict(phase="idle", connected=True, device="indigo", model="opus",
                    effort="medium", model_label="opus 5.5",
                    heard="and the downloads folder?", at=NOW - 3 * 60,
                    turn_started=time.monotonic() - 60,
                    activity=[Activity("tool", "$ du -sh ~/Downloads",
                                       result="2.9G")],
                    message="**2.9G** in Downloads, mostly two ISOs from "
                            "March. Want them gone?")
    defaults.update(state)
    a = agent_in(**defaults)
    a.context = ctx
    a.exchanges = xs
    a.more_back = edge is None
    a.edge = edge
    a.want_older = lambda: False        # nothing to ask: the page is here
    return a


# ── the context bar's variants ──────────────────────────────────────────
def bar_foot(canvas, box, a, accent):
    """B: the row at the plate's foot, cells, then "61k / 500k"."""
    ctx = context.shown(a)
    used = ctx.get("used")
    label = (f"{context.k_tokens(used)} / {context.k_tokens(ctx['cap'])}"
             if used is not None else f"— / {context.k_tokens(ctx['cap'])}")
    tw = text.measure(label, TAG_SIZE)
    cells = skia.Rect.MakeLTRB(box.left(), box.top(), box.right() - tw - 12,
                               box.bottom())
    CELLS(canvas, cells, a, accent)
    text.draw(canvas, label, box.right() - tw, text.baseline_in(box, TAG_SIZE),
              TAG_SIZE, theme.color(theme.BASE_MUTED))


def variant_rows(kind):
    """The plate's rows with the context row where variant `kind` has it."""
    built = AgentPlate._build_rows

    def rows(self):
        out = built(self)
        if kind == "A":
            return out
        out = [r for r in out if r.kind != "context"]
        if kind == "B" and context.shown(self.agent) is not None:
            out += [Row("gap", 4), Row("context", line_h(TAG_SIZE) + 2)]
        return out
    return rows


def variant_header(kind):
    header = AgentPlate._paint_header

    def paint(self, canvas, box, accent):
        if kind != "C" or context.shown(self.agent) is None:
            return header(self, canvas, box, accent)
        # C: a 96px meter and the number, left of the model.
        a = self.agent
        setting = f"{a.model_label()} · {a.effort_label()}"
        sw = text.measure(setting, TAG_SIZE)
        used = context.shown(a).get("used")
        num = context.k_tokens(used) if used is not None else ""
        nw = text.measure(num, TAG_SIZE) if num else 0
        meter = skia.Rect.MakeXYWH(box.right() - sw - 14 - 96 - (nw + 8 if num else 0),
                                   box.top(), 96, box.height())
        CELLS(canvas, meter, a, own_accent(a))
        if num:
            text.draw(canvas, num, meter.right() + 8,
                      text.baseline_in(box, TAG_SIZE), TAG_SIZE,
                      theme.color(theme.BASE_MUTED))
        return header(self, canvas, box, accent)
    return paint


def with_bar(kind, fn):
    patched = {"_build_rows": variant_rows(kind),
               "_paint_header": variant_header(kind)}
    saved = {k: getattr(AgentPlate, k) for k in patched}
    if kind == "B":
        def foot(canvas, box, a, accent, now=None):
            if box.height() > CONTEXT_H:
                return bar_foot(canvas, box, a, accent)
            return CELLS(canvas, box, a, accent, now)
        context.paint = foot
    for k, v in patched.items():
        setattr(AgentPlate, k, v)
    try:
        return fn()
    finally:
        for k, v in saved.items():
            setattr(AgentPlate, k, v)
        context.paint = CELLS


# ── the scrollback's variants ───────────────────────────────────────────
def stamp_rule(self, canvas, box, row):
    """S2: a hairline across, the time at its right end."""
    if not row.label:
        return
    lower = skia.Rect.MakeLTRB(box.left(), box.bottom() - line_h(TAG_SIZE) - 2,
                               box.right(), box.bottom())
    base = text.baseline_in(lower, TAG_SIZE)
    tw = text.measure(row.label, TAG_SIZE)
    text.draw(canvas, row.label, box.right() - tw, base, TAG_SIZE,
              theme.color(theme.BASE_MUTED))
    p = skia.Paint(AntiAlias=False)
    p.setColor(theme.color(theme.BASE_MUTED, 0.45))
    canvas.drawRect(skia.Rect.MakeXYWH(box.left(), round(lower.centerY()),
                                       box.width() - tw - 10, 1), p)


def stamp_inline(self, canvas, box, row):
    """S3: the time at the right of the row below (your first line)."""
    if not row.label:
        return
    nxt = skia.Rect.MakeXYWH(box.left(), box.bottom(), box.width(),
                             line_h(BODY_SIZE) + 3)
    tw = text.measure(row.label, TAG_SIZE)
    text.draw(canvas, row.label, box.right() - tw,
              text.baseline_in(nxt, TAG_SIZE), TAG_SIZE,
              theme.color(theme.BASE_MUTED))


def with_stamp(kind, fn):
    saved = (AgentPlate._paint_stamp, plate_mod.STAMP_H)
    if kind == "S2":
        AgentPlate._paint_stamp = stamp_rule
    elif kind == "S3":
        AgentPlate._paint_stamp = stamp_inline
        plate_mod.STAMP_H = 10
    try:
        return fn()
    finally:
        AgentPlate._paint_stamp, plate_mod.STAMP_H = saved


def scrolled(a, steps):
    """The call, its plate scrolled back `steps` times (Ctrl+K)."""
    def setup(host):
        host.agent_plate._build()
        for _ in range(steps):
            host.agent_plate.older()
            host.agent_plate._build()
    return call(a, setup=setup)


# ── the sheets ──────────────────────────────────────────────────────────
def sheet(path, rows, title):
    gap, top, side = 24, 40, 30
    row_h = [max(img.height() for _, img in r) + top for _, r in rows]
    w = max(sum(img.width() for _, img in r) + gap * (len(r) + 1)
            for _, r in rows) + side
    h = sum(row_h) + gap + 30
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    txt(c, title, gap, 24, 14, theme.HUD_FG_BRIGHT, bold=True)
    y = 30
    for (name, r), rh in zip(rows, row_h):
        txt(c, name, 8, y + 26, 13, theme.YELLOW_BRIGHT, bold=True)
        x = gap + side
        for label, img in r:
            txt(c, label, x + 36, y + 26, 13, theme.HUD_FG_BRIGHT, bold=True)
            c.drawImage(img, x, y + top)
            x += img.width() + gap
        y += rh
    s.flushAndSubmit()
    s.makeImageSnapshot().save(path, skia.kPNG)
    print("wrote", path, f"{w}x{h}")


def context_states():
    m = time.monotonic()
    ctx = {"compact_at": 200_000, "cap": 500_000, "window": 1_000_000,
           "from": None}
    return [
        ("61k: early", lambda: call(hub(context={**ctx, "used": 61_000}))),
        ("240k: past the tick", lambda: call(hub(context={**ctx, "used": 240_000}))),
        ("470k: near the cap", lambda: call(hub(context={**ctx, "used": 470_000}))),
        ("compacting (sweep)", lambda: call(hub(
            context={**ctx, "used": 240_000}, compacting=True,
            phase="compacting"))),
        ("compacted: note, not measured", lambda: call(hub(
            context={**ctx, "used": None, "from": 813},
            compacted={"before": 240_000, "after": None, "at": m}))),
    ]


def back_states():
    return [
        ("live", lambda: call(hub())),
        ("^K once", lambda: scrolled(hub(), 1)),
        ("^K x3: a hub turn, older", lambda: scrolled(hub(), 3)),
        ("top: earlier is with her", lambda: scrolled(hub(
            edge="compacted", exchanges=exchanges()[2:]), 6)),
        ("top: the chat's start", lambda: scrolled(hub(edge="start"), 9)),
    ]


def main():
    rows = [(k, [(label, with_bar(k, fn)) for label, fn in context_states()])
            for k in ("A", "B", "C")]
    sheet("call_context.png", rows,
          "context bar: A under the heading (built) · B at the foot with "
          "the number · C a short meter in the heading")
    rows = [(k, [(label, with_stamp(k, fn)) for label, fn in back_states()])
            for k in ("S1", "S2", "S3")]
    sheet("call_scrollback.png", rows,
          "scrollback (Ctrl+K): S1 a muted // time row (built) · S2 a rule "
          "with the time · S3 the time beside your line")


if __name__ == "__main__":
    main()
