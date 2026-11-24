"""Tech-look variations for the call overlay, and ways for its reply to
arrive — all at 1x, from the real call widgets.

Every look is painted by the shipped widgets (`CallHost`, `AgentPlate`,
`YouPlate`, `Chip`, `CallStatus`) with faked agent state. A look only
adds to them, through hooks this script installs: the plates'
`paint_plate` / `paint_heading` calls are routed through the active
`Look`, which can swap the backing, paint under the text or over the
finished plate, restyle rows, or repaint a chip. Nothing under
`indigoshell/` is touched.

    call_looks.png          each look: working, a short reply, hung up
    call_looks_bright.png   the busiest looks over a bright web page
    gif/call_look_*.gif     the looks whose point is motion
    gif/call_reveal_*.gif   ways for the reply text to arrive

    PYTHONPATH=../.. ../../.venv/bin/python render_call_looks.py [sheet|gif|reveal]
"""
import copy
import math
import os
import random
import subprocess
import sys
import time

import skia

from common import C, txt   # also quiets logging
from indigoshell import shapes, text, theme
from indigoshell.agent.activity import Activity
from indigoshell.effects import GlitchWipe, paint_through
from indigoshell.widgets.base import Size, Widget
from indigoshell.agent.ui import agent_plate as ap_mod
from indigoshell.agent.ui import call as call_mod
from indigoshell.agent.ui import status as st_mod
from indigoshell.agent.ui import style
from indigoshell.agent.ui import you_plate as yp_mod
from indigoshell.agent.ui.agent_plate import AgentPlate
from indigoshell.agent.ui.call import CallHost
from indigoshell.agent.ui.chip import Chip
from indigoshell.agent.ui.rows import Row, paint_line
from indigoshell.agent.ui.status import CallStatus
from indigoshell.agent.ui.style import (
    AGENT_ACCENT, BEVEL, CHIP, CORNERS, FRAME_W, GAP, NAME_SIZE, PAD_X,
    PAD_Y, PLATE_W, ROW_GAP, SPIN, TAG_SIZE, BODY_SIZE, ACT_SIZE, TOOL_FG,
    UNBOUNDED, YOU_ACCENT, accent_of, blink_on, frame_inset, line_h,
    plate_fill, plate_path)
from indigoshell.agent.ui.you_plate import YouPlate
from render_call import FakeWindow, agent_in as hub_agent
from render_call import backdrop as dark_backdrop

# A clock the frames own: every widget reads time.monotonic().
NOW = [1000.0]
time.monotonic = lambda: NOW[0]

MARGIN = 24
BLUR_SIGMA = 9.0
SQ2 = math.sqrt(2.0)
GIF_FPS = 30
ORIG_PLATE = style.paint_plate
ORIG_HEADING = style.paint_heading


# ── drawing helpers ─────────────────────────────────────────────────────
def P(color, alpha=None, width=0.0, aa=True, blur=0.0):
    p = skia.Paint(AntiAlias=aa)
    p.setColor(C(color, alpha))
    if width:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(width)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    return p


def bevel_at(d, bevel=BEVEL):
    """The bevel of a path `d` px inside a beveled one, so the two 45°
    cuts stay parallel (an inset rect with the same bevel does not)."""
    return max(0.0, bevel - d * (2 - SQ2))


def inner_path(r, d, bevel=BEVEL, corners=CORNERS):
    return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                          bevel=bevel_at(d, bevel), corners=corners, inset=d)


def corner_marks(r, d, arm, bevel=BEVEL, corners=CORNERS,
                 which=("top-left", "top-right", "bottom-right", "bottom-left")):
    """Brackets `d` px inside a beveled rect: an L on a square corner, a
    short arm-cut-arm on a beveled one."""
    x0, y0, x1, y1 = r.left() + d, r.top() + d, r.right() - d, r.bottom() - d
    b = bevel_at(d, bevel)
    path = skia.Path()
    for name, cx, cy, sx, sy in (("top-left", x0, y0, 1, 1),
                                 ("top-right", x1, y0, -1, 1),
                                 ("bottom-right", x1, y1, -1, -1),
                                 ("bottom-left", x0, y1, 1, -1)):
        if name not in which:
            continue
        if name in corners:
            h = arm * 0.5
            path.moveTo(cx + sx * (b + h), cy)
            path.lineTo(cx + sx * b, cy)
            path.lineTo(cx, cy + sy * b)
            path.lineTo(cx, cy + sy * (b + h))
        else:
            path.moveTo(cx + sx * arm, cy)
            path.lineTo(cx, cy)
            path.lineTo(cx, cy + sy * arm)
    return path


def side_band(r, side, s=2.0, bevel=BEVEL):
    """The panels' HudCard stripe: a band down one edge that wraps that
    side's 45° cut instead of stopping square."""
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    b = bevel
    p = skia.Path()
    if side == "left":                       # wraps the bottom-left cut
        p.moveTo(x0, y0)
        p.lineTo(x0 + s, y0)
        p.lineTo(x0 + s, y1 - b)
        p.lineTo(x0 + s + b, y1)
        p.lineTo(x0 + b, y1)
        p.lineTo(x0, y1 - b)
    else:                                    # right edge, wraps top-right
        p.moveTo(x1, y1)
        p.lineTo(x1 - s, y1)
        p.lineTo(x1 - s, y0 + b)
        p.lineTo(x1 - s - b, y0)
        p.lineTo(x1 - b, y0)
        p.lineTo(x1, y0 + b)
    p.close()
    return p


def ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def plate_rect(w):
    return w._plate_rect() if isinstance(w, CallStatus) else w.rect


def accent_for(w):
    return YOU_ACCENT if isinstance(w, YouPlate) else accent_of(w.agent)


def tint_for(w):
    return YOU_ACCENT if isinstance(w, YouPlate) else AGENT_ACCENT


def chip_alpha(chip):
    _a, _l, mode = chip.look()
    if mode == "dim":
        return 0.45
    lit = mode != "blink" or blink_on(time.monotonic() - chip.ring_since())
    return 1.0 if lit else 0.3


def chip_arcs(chip, c, accent, alpha):
    r = chip.rect
    radius = chip.size * (0.38 if chip.size >= 56 else 0.41)
    arc = P(accent, alpha, max(2.0, chip.size * 0.05))
    oval = skia.Rect.MakeLTRB(r.centerX() - radius, r.centerY() - radius,
                              r.centerX() + radius, r.centerY() + radius)
    for base in (chip.angle, chip.angle + 180.0):
        c.drawArc(oval, base - 32.0, 64.0, False, arc)


def chip_label(chip, c, label, color, alpha):
    r = chip.rect
    size = max(8.0, round(chip.size * 0.17))
    track = 0.6 if chip.size >= 56 else 0.0
    w = text.measure(label, size, bold=True, tracking=track)
    text.draw(c, label, r.centerX() - w / 2,
              text.baseline_in(r, size, bold=True), size,
              C(color, alpha), bold=True, tracking=track)


def chip_face(chip, c, *, face=0.30, inset=True, texture=False, bezel=False,
              label_fg=None):
    """A chip with a stronger translucent face, a lighter inset border
    inside its frame, and optionally a dot-grid texture or bezel ticks."""
    accent, label, mode = chip.look()
    r = chip.rect
    lit = mode != "blink" or blink_on(NOW[0] - chip.ring_since())
    chip._painted_dim = not lit
    alpha = 1.0 if lit else 0.3
    if mode == "dim":
        alpha = 0.45
    path = chip.path()
    c.drawPath(path, P(*plate_fill(chip.tint)))
    c.drawPath(path, P(accent, face * alpha + (0.12 if mode == "blink" and lit else 0)))
    if texture:
        dots = skia.Path()
        x0, y0 = int(r.left()) + 2, int(r.top()) + 2
        for y in range(y0, int(r.bottom()) - 1, 3):
            for x in range(x0, int(r.right()) - 1, 3):
                dots.addRect(skia.Rect.MakeXYWH(x, y, 1, 1))
        c.save()
        c.clipPath(inner_path(r, 4), doAntiAlias=True)
        c.drawPath(dots, P(theme.lerp(accent, theme.HUD_FG_BRIGHT, 0.3),
                           0.16 * alpha, aa=False))
        c.restore()
    c.drawPath(plate_path(r, inset=frame_inset()), P(accent, alpha, FRAME_W))
    if inset:
        c.drawPath(inner_path(r, 5.5),
                   P(theme.lerp(accent, theme.HUD_FG_BRIGHT, 0.35), 0.55 * alpha, 1.0))
    if bezel:
        cx, cy = r.centerX(), r.centerY()
        ticks = skia.Path()
        for i in range(32):
            a = math.radians(i * 360 / 32)
            r0, r1 = ((0.405, 0.46) if i % 4 == 0 else (0.42, 0.455))
            r0, r1 = r0 * chip.size, r1 * chip.size
            ticks.moveTo(cx + r0 * math.cos(a), cy + r0 * math.sin(a))
            ticks.lineTo(cx + r1 * math.cos(a), cy + r1 * math.sin(a))
        c.drawPath(ticks, P(accent, 0.38 * alpha, 1.0))
    chip_arcs(chip, c, accent, alpha)
    chip_label(chip, c, label, label_fg or accent, alpha)


class ThemeProxy:
    """`theme`, with a few names overridden (to tint one plate's text)."""

    def __init__(self, **over):
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(theme, name)


# ── the hooks ───────────────────────────────────────────────────────────
class Look:
    """Today's call. Subclasses override the hooks they need."""
    key = "current"
    name = "CURRENT"
    note = "today's call, for reference"
    host_cls = CallHost
    you_text = None             # tint for your words, if any

    def plate(self, w, c, r, frame, fill):
        ORIG_PLATE(c, r, frame, fill)

    def under(self, w, c, r, frame):
        pass

    def heading(self, w, c, box, size, parts):
        return ORIG_HEADING(c, box, size, parts)

    def over(self, w, c):
        pass

    def rows(self, w, rows):
        return rows

    def chip(self, chip, c, base):
        base(c)

    def host_over(self, host, c):
        pass


LOOK: Look = Look()
REVEAL = None                    # a reply-reveal, while rendering those
ARRIVALS: dict[str, list[float]] = {}


def _hooked(w, mod, canvas, base):
    op, oh = mod.paint_plate, mod.paint_heading

    def plate(c, r, frame, fill):
        LOOK.plate(w, c, r, frame, fill)
        LOOK.under(w, c, r, frame)

    mod.paint_plate = plate
    mod.paint_heading = lambda c, box, size, parts: LOOK.heading(w, c, box, size, parts)
    try:
        base(canvas)
    finally:
        mod.paint_plate, mod.paint_heading = op, oh
    LOOK.over(w, canvas)


class LookAgentPlate(AgentPlate):
    def _build(self):
        rows = LOOK.rows(self, super()._build())
        if REVEAL is not None:
            for row in rows:
                if row.key.startswith("m"):
                    arr = ARRIVALS.setdefault(row.key, [])
                    if len(row.src) > len(arr):
                        arr.extend([NOW[0]] * (len(row.src) - len(arr)))
        return rows

    def paint(self, canvas):
        _hooked(self, ap_mod, canvas, super().paint)

    def _paint_line(self, canvas, box, row, sel, accent):
        if REVEAL is not None and row.key.startswith("m"):
            REVEAL.paint(self, canvas, box, row, sel, accent)
        else:
            paint_line(canvas, box, row, sel, accent)


class LookYouPlate(YouPlate):
    def paint(self, canvas):
        tint = LOOK.you_text
        saved = yp_mod.theme
        if tint:
            yp_mod.theme = ThemeProxy(HUD_FG=tint)
            self.input.color = tint
        try:
            _hooked(self, yp_mod, canvas, super().paint)
        finally:
            yp_mod.theme = saved
            self.input.color = theme.HUD_FG_BRIGHT


class LookChip(Chip):
    def paint(self, canvas):
        LOOK.chip(self, canvas, super().paint)


class LookStatus(CallStatus):
    def paint(self, canvas):
        _hooked(self, st_mod, canvas, super().paint)


call_mod.AgentPlate = LookAgentPlate
call_mod.YouPlate = LookYouPlate
call_mod.Chip = LookChip
st_mod.Chip = LookChip


# ── A  BRACKETS ─────────────────────────────────────────────────────────
class Brackets(Look):
    key = "brackets"
    name = "A  BRACKETS"
    note = "corner brackets inside the frame; an inset hairline in each chip"

    def over(self, w, c):
        r = plate_rect(w)
        c.drawPath(corner_marks(r, 6, 12), P(accent_for(w), 0.85, 1.5))

    def chip(self, chip, c, base):
        base(c)
        accent, _l, _m = chip.look()
        c.drawPath(inner_path(chip.rect, 5.5), P(accent, 0.4 * chip_alpha(chip), 1.0))


# ── B  ID TAGS ──────────────────────────────────────────────────────────
TURN = 7


class Tags(Look):
    key = "tags"
    name = "B  ID TAGS"
    note = "a beveled TX / RX tag in each plate's square corner, carrying the turn number"
    SIZE = 10

    def over(self, w, c):
        r = plate_rect(w)
        accent = accent_for(w)
        label = f"TX·{TURN:02d}" if isinstance(w, YouPlate) else f"RX·{TURN:02d}"
        tw = text.measure(label, self.SIZE, True, tracking=0.8)
        h, cut = 15, 5
        x1, y1 = r.right(), r.bottom()
        x0, y0 = x1 - tw - 14, y1 - h
        tab = skia.Path()
        tab.moveTo(x0, y1)
        tab.lineTo(x0, y0 + cut)
        tab.lineTo(x0 + cut, y0)
        tab.lineTo(x1, y0)
        tab.lineTo(x1, y1)
        tab.close()
        c.drawPath(tab, P(accent))
        box = skia.Rect.MakeLTRB(x0 + 2, y0, x1, y1)
        text.draw(c, label, x0 + 8, text.baseline_in(box, self.SIZE, True) + 0.5,
                  self.SIZE, C(theme.BASE_BLACK), bold=True, tracking=0.8)


# ── C  TRACE ────────────────────────────────────────────────────────────
class Trace(Look):
    key = "trace"
    name = "C  TRACE"
    note = ("the toast's border trace as the turn clock: the frame dims and is "
            "redrawn clockwise, one lap a minute; it closes when the answer lands")
    LAP = 60.0
    FINISH_S = 0.45
    COMET_S = 0.45

    def __init__(self):
        self.finish = None      # (t0, lap, f) when a turn just ended
        self.sent_at = None     # when your line left (the comet)

    def progress(self, a):
        if a.busy:
            e = NOW[0] - a.turn_started
            return int(e // self.LAP), (e % self.LAP) / self.LAP, 0.0
        if self.finish is not None:
            t0, lap, f = self.finish
            k = (NOW[0] - t0) / self.FINISH_S
            if k < 1.0:
                return lap, f, ease_out(k)
        return None

    def over(self, w, c):
        r = plate_rect(w)
        border = plate_path(r, inset=frame_inset())
        m = skia.PathMeasure(border, False)
        L = m.getLength()
        if isinstance(w, YouPlate):
            self._comet(c, m, L)
            return
        got = self.progress(w.agent)
        if got is None:
            return
        lap, f, done = got
        accent = accent_of(w.agent)
        dim = theme.lerp(accent, plate_fill(AGENT_ACCENT)[0], 0.8)
        c.drawPath(border, P(dim, None, FRAME_W))
        if lap % 2 == 0:        # even laps draw the frame in...
            lo, hi = 0.0, f * L + (L - f * L) * done
            head = hi
        else:                   # ...odd laps take it back out
            lo, hi = f * L * (1 - done), L
            head = lo
        seg = skia.Path()
        if m.getSegment(lo, hi, seg, True):
            c.drawPath(seg, P(accent, None, FRAME_W))
        if done < 1.0:
            tip = skia.Path()
            a0, a1 = (head - 22, head) if lap % 2 == 0 else (head, head + 22)
            if m.getSegment(max(0.0, a0), min(L, a1), tip, True):
                c.drawPath(tip, P(theme.YELLOW_BRIGHT, 0.7, 4.0, blur=3.0))
                c.drawPath(tip, P(theme.YELLOW_BRIGHT, None, FRAME_W + 0.5))

    def _comet(self, c, m, L):
        if self.sent_at is None:
            return
        k = (NOW[0] - self.sent_at) / self.COMET_S
        if not 0.0 <= k < 1.0:
            return
        head = ease_out(k) * L
        tail = 160.0
        for i in range(8):
            a = head - tail * (1 - i / 8)
            b = head - tail * (1 - (i + 1) / 8)
            seg = skia.Path()
            if m.getSegment(max(0.0, a), max(0.0, b), seg, True):
                c.drawPath(seg, P(theme.MAGENTA_BLOOM, (i + 1) / 8 * (1 - k * 0.3), FRAME_W + 0.5))


# ── D  SECTIONS ─────────────────────────────────────────────────────────
def section(c, left, top, right, bottom, color, bevel=8):
    r = skia.Rect.MakeLTRB(left, top, right, bottom)
    c.drawPath(shapes.beveled(left, top, r.width(), r.height(),
                              bevel=bevel, corners=CORNERS),
               P(theme.BASE_BLACK, 0.5))
    c.drawPath(side_band(r, "left", 2.0, bevel), P(color, 0.95))


class Sections(Look):
    key = "sections"
    name = "D  SECTIONS"
    note = ("the panels' section boxes: steps in a dark card with a cyan rule that "
            "wraps its cut, your last line in a magenta one")

    def rows(self, w, rows):
        for i, row in enumerate(rows):
            if row.kind == "rule":
                rows[i] = Row("gap", row.h, label="rule")
        return rows

    def under(self, w, c, r, frame):
        left, right = r.left() + PAD_X, r.right() - PAD_X
        if isinstance(w, AgentPlate):
            laid = w._layout()
            idx = next((i for i, (row, _y) in enumerate(laid)
                        if row.kind == "gap" and row.label == "rule"), None)
            if idx is None or idx <= 1:
                return
            section(c, left - 9, laid[1][1] - 2, right + 7, laid[idx][1] + 2, TOOL_FG)
        elif isinstance(w, YouPlate):
            heard = w._heard()
            if not heard:
                return
            y = r.top() + PAD_Y + line_h(NAME_SIZE, True) + 6
            lh = line_h(BODY_SIZE) + 3
            section(c, left - 9, y - 2, right + 7, y + len(heard) * lh + 2, YOU_ACCENT)
        elif isinstance(w, CallStatus) and w.agent.busy and not w.agent.ringing:
            x = r.left() + PAD_X - 4
            top = r.top() + PAD_Y - 4 + line_h(TAG_SIZE + 2, True) + 4
            section(c, x - 5, top - 1, r.right() - PAD_X + 6,
                    top + line_h(ACT_SIZE) + 3, TOOL_FG, bevel=6)


# ── E  SIGNAL ───────────────────────────────────────────────────────────
METER_H, METER_GAP = 12, 5


class SignalHost(CallHost):
    """The call with a live meter under each chip."""

    def measure(self, avail):
        lane = CHIP + METER_GAP + METER_H
        top = max(self.you_plate.measure(avail).height, lane)
        bottom = max(self.agent_plate.measure(avail).height, lane)
        return Size(CHIP + GAP + PLATE_W + GAP + CHIP, top + ROW_GAP + bottom)

    def arrange(self, rect):
        self.rect = rect
        lane = CHIP + METER_GAP + METER_H
        x0, y = rect.left(), rect.top()
        px = x0 + CHIP + GAP
        yh = self.you_plate.measure(UNBOUNDED).height
        self.you_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, yh))
        cx = px + PLATE_W + GAP
        self.you_chip.arrange(skia.Rect.MakeXYWH(cx, y, CHIP, CHIP))
        self.you_meter = skia.Rect.MakeXYWH(cx, y + CHIP + METER_GAP, CHIP, METER_H)
        y += max(yh, lane) + ROW_GAP
        ah = self.agent_plate.measure(UNBOUNDED).height
        self.agent_chip.arrange(skia.Rect.MakeXYWH(x0, y, CHIP, CHIP))
        self.agent_meter = skia.Rect.MakeXYWH(x0, y + CHIP + METER_GAP, CHIP, METER_H)
        self.agent_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, ah))

    def blur_shape(self):
        path = super().blur_shape()
        for m in (self.you_meter, self.agent_meter):
            path.addPath(shapes.beveled(m.left(), m.top(), m.width(), m.height(),
                                        bevel=4, corners=CORNERS))
        return path

    def paint(self, canvas):
        super().paint(canvas)
        LOOK.host_over(self, canvas)


class Signal(Look):
    key = "signal"
    name = "E  SIGNAL"
    note = ("chips as instruments: a bezel of ticks, a lighter inset border, and a "
            "segmented live meter under each, lit by whoever is on the line")
    host_cls = SignalHost
    N = 9

    def __init__(self):
        self.keys: list[float] = []     # your keystrokes (for the gif)

    def chip(self, chip, c, base):
        chip_face(chip, c, face=0.22, inset=True, bezel=True)

    def agent_levels(self, a, t):
        n = self.N
        if a.phase == "error":
            return "flat", [1.0] + [0.0] * (n - 1), style.FAIL_ACCENT
        if a.approval is not None:
            on = blink_on(t - a.ring_started)
            return "flat", [1.0 if on else 0.0] * n, style.ASK_ACCENT
        if a.busy and a.phase == "speaking":
            lv = [0.18 + 0.82 * abs(math.sin(t * 6.3 + i * 1.7) * math.sin(t * 2.9 + i * 0.8))
                  for i in range(n)]
            return "eq", lv, AGENT_ACCENT
        if a.busy or a.phase == "dialing":
            ping = (t * 0.9 + 0.5) % 2.0          # a scanner, back and forth
            centre = (ping if ping <= 1.0 else 2.0 - ping) * (n - 1)
            return "flat", [max(0.0, 1 - abs(i - centre) / 1.8) for i in range(n)], AGENT_ACCENT
        if a.phase == "offline":
            return "flat", [0.0] * n, AGENT_ACCENT
        return "flat", [0.55] * n, AGENT_ACCENT

    def you_levels(self, host, t):
        n = self.N
        level = sum(math.exp(-(t - k) / 0.22) for k in self.keys if k <= t)
        if not self.keys and host.you_plate.input.text:
            level = 1.6
        lit = min(n, round(level * 2.6))
        return "flat", [1.0 if i < lit else 0.0 for i in range(n)], YOU_ACCENT

    def host_over(self, host, c):
        t = NOW[0]
        self.meter(c, host.agent_meter, AGENT_ACCENT, *self.agent_levels(host.agent, t))
        self.meter(c, host.you_meter, YOU_ACCENT, *self.you_levels(host, t))

    def meter(self, c, r, tint, mode, levels, color):
        c.drawPath(shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                                  bevel=4, corners=CORNERS), P(*plate_fill(tint)))
        tick, gap = 4, 2
        n = len(levels)
        span = n * (tick + gap) - gap
        x = round(r.left() + (r.width() - span) / 2)
        top, h = r.top() + 3, r.height() - 6
        off, glow, lit = skia.Path(), skia.Path(), []
        for i, lv in enumerate(levels):
            rx = x + i * (tick + gap)
            if mode == "eq":
                th = max(1.0, round(h * lv))
                off.addRect(skia.Rect.MakeXYWH(rx, top, tick, h - th))
                rect = skia.Rect.MakeXYWH(rx, top + h - th, tick, th)
                glow.addRect(rect)
                lit.append((rect, 1.0))
            else:
                rect = skia.Rect.MakeXYWH(rx, top, tick, h)
                if lv <= 0.02:
                    off.addRect(rect)
                else:
                    glow.addRect(rect)
                    lit.append((rect, lv))
        c.drawPath(off, P(theme.BASE_MUTED, 0.4, aa=False))
        c.drawPath(glow, P(color, 0.6, blur=2.2))
        for rect, lv in lit:
            c.drawRect(rect, P(theme.BASE_SURFACE, aa=False))
            c.drawRect(rect, P(color, lv, aa=False))


# ── F  OPEN LINE ────────────────────────────────────────────────────────
AGENT_TEXT = theme.lerp(theme.HUD_FG_BRIGHT, theme.VIOLET_BRIGHT, 0.32)
YOU_TEXT = theme.lerp(theme.HUD_FG_BRIGHT, theme.MAGENTA_BLOOM, 0.45)
TAB_SIZE = NAME_SIZE


def tab_h():
    return line_h(TAB_SIZE, True) + 4


class OpenHost(CallHost):
    """Names in tabs above the chips, level with the plates' headings."""

    def _chip_y(self, py):
        hh = line_h(NAME_SIZE, True) + 6
        ty = py + PAD_Y + (hh - tab_h()) / 2
        return ty, ty + tab_h() + 4

    def measure(self, avail):
        _ty, cy = self._chip_y(0)
        lane = cy + CHIP
        top = max(self.you_plate.measure(avail).height, lane)
        bottom = max(self.agent_plate.measure(avail).height, lane)
        return Size(CHIP + GAP + PLATE_W + GAP + CHIP, top + ROW_GAP + bottom)

    def arrange(self, rect):
        self.rect = rect
        x0, y = rect.left(), rect.top()
        px = x0 + CHIP + GAP
        th = tab_h()
        yh = self.you_plate.measure(UNBOUNDED).height
        self.you_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, yh))
        ty, cy = self._chip_y(y)
        cx = px + PLATE_W + GAP
        self.you_chip.arrange(skia.Rect.MakeXYWH(cx, cy, CHIP, CHIP))
        tw = text.measure("YOU", TAB_SIZE, True) + 12
        self.you_tab = skia.Rect.MakeXYWH(cx + CHIP - tw, ty, tw, th)
        y += max(yh, cy - y + CHIP) + ROW_GAP
        ah = self.agent_plate.measure(UNBOUNDED).height
        ty, cy = self._chip_y(y)
        self.agent_chip.arrange(skia.Rect.MakeXYWH(x0, cy, CHIP, CHIP))
        tw = text.measure(self.agent.name, TAB_SIZE, True) + 12
        self.agent_tab = skia.Rect.MakeXYWH(x0, ty, tw, th)
        self.agent_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, ah))

    def _tab_path(self, r):
        return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                              bevel=6, corners=CORNERS)

    def blur_shape(self):
        path = super().blur_shape()
        for r in (self.you_tab, self.agent_tab):
            path.addPath(self._tab_path(r))
        return path

    def paint(self, canvas):
        super().paint(canvas)
        for r, name, accent, tint in (
                (self.you_tab, "YOU", YOU_ACCENT, YOU_ACCENT),
                (self.agent_tab, self.agent.name, accent_of(self.agent), AGENT_ACCENT)):
            canvas.drawPath(self._tab_path(r), P(*plate_fill(tint)))
            canvas.drawRect(skia.Rect.MakeXYWH(r.left() + 6, r.bottom() - 2,
                                               r.width() - 12, 2), P(accent, aa=False))
            text.draw(canvas, name, r.left() + 6,
                      text.baseline_in(r, TAB_SIZE, True) - 1, TAB_SIZE,
                      C(accent), bold=True)


class Open(Look):
    key = "open"
    name = "F  OPEN LINE"
    note = ("frameless plates (still 88%) held by a HudCard rule on the chip side and "
            "brackets opposite; names above the chips; each speaker's text in its colour")
    host_cls = OpenHost
    you_text = YOU_TEXT

    def plate(self, w, c, r, frame, fill):
        c.drawPath(plate_path(r), P(*fill))
        side = "left" if isinstance(w, AgentPlate) else "right"
        c.drawPath(side_band(r, side, 2.0), P(frame))
        far = (("top-right", "bottom-right") if side == "left"
               else ("top-left", "bottom-left"))
        c.drawPath(corner_marks(r, 1.5, 14, which=far), P(frame, 0.9, 2.0))

    def heading(self, w, c, box, size, parts):
        if isinstance(w, AgentPlate) and w.history is None and parts \
                and parts[0][0] == w.agent.name:
            parts = parts[1:]
        elif isinstance(w, YouPlate):
            parts = (("you said" if w.agent.heard else "on the line",
                      theme.BASE_MUTED, False),)
        return ORIG_HEADING(c, box, size, parts)

    def rows(self, w, rows):
        for row in rows:
            if row.kind == "line" and row.key.startswith("m") \
                    and row.color == theme.HUD_FG_BRIGHT:
                row.color = AGENT_TEXT
        return rows

    def chip(self, chip, c, base):
        chip_face(chip, c, face=0.34, inset=True, texture=True,
                  label_fg=theme.HUD_FG_BRIGHT)


class Combined(Look):
    key = "combined"
    name = "G  A+C+E+F"
    note = ("the recommendation, together: A's brackets, C's trace clock, E's inset chips "
            "and live meters (no bezel), F's speaker-tinted text")
    host_cls = SignalHost
    you_text = YOU_TEXT

    def __init__(self):
        self.brackets, self.trace = Brackets(), Trace()
        self.signal, self.open = Signal(), Open()

    def over(self, w, c):
        self.brackets.over(w, c)
        self.trace.over(w, c)

    def rows(self, w, rows):
        return self.open.rows(w, rows)

    def chip(self, chip, c, base):
        chip_face(chip, c, face=0.22, inset=True)

    def host_over(self, host, c):
        self.signal.host_over(host, c)


LOOKS = [Look(), Brackets(), Tags(), Trace(), Sections(), Signal(), Open(), Combined()]


# ── scenes ──────────────────────────────────────────────────────────────
HEARD = "why does my wifi keep dropping every few minutes"
REPLY = ("Copy that. The card is **fine**; the drops are *power saving*. "
         "`iwlwifi` parks the radio after idle minutes and the AP drops you.\n\n"
         "- every ~4 min, and only on battery\n"
         "- fix: `nmcli connection modify Home 802-11-wireless.powersave 2`")


def agent_in(**state):
    return hub_agent(model="opus", effort="medium", **state)


def working_agent():
    return agent_in(phase="working", busy=True, turn_started=NOW[0] - 41,
                    heard=HEARD,
                    activity=[Activity("note", "Checking the driver and the logs first."),
                              Activity("tool", "$ journalctl -b -u NetworkManager --since -1h"),
                              Activity("tool", "$ iw dev wlp0s20f3 link")])


def reply_agent():
    return agent_in(phase="idle", busy=False, heard=HEARD,
                    activity=[Activity("tool", "$ iw dev wlp0s20f3 link",
                                       detail="iw dev wlp0s20f3 link", result="Connected"),
                              Activity("tool", "$ journalctl -k -g iwlwifi",
                                       detail="journalctl -k -g iwlwifi", result="...")],
                    message=REPLY)


def status_agent():
    return agent_in(phase="working", busy=True, turn_started=NOW[0] - 132,
                    activity=[Activity("tool", "$ sudo pacman -Syu --noconfirm")])


# ── rendering ───────────────────────────────────────────────────────────
def bright_backdrop(w, h):
    """A light web page: white, a header, headline, grey paragraphs, a photo."""
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(C("#f6f7f9"))
    p = skia.Paint(AntiAlias=False)

    def rect(x, y, rw, rh, col):
        p.setColor(C(col))
        c.drawRect(skia.Rect.MakeXYWH(x, y, rw, rh), p)

    rect(0, 0, w, 46, "#ffffff")
    rect(0, 46, w, 1, "#d0d7de")
    rect(18, 14, 84, 18, "#2f6fde")
    for i in range(5):
        rect(130 + i * 92, 19, 64, 8, "#57606a")
    rect(30, 76, min(560, w - 60), 20, "#1f2328")
    rect(30, 104, min(380, w - 60), 20, "#1f2328")
    y = 146
    for i in range(60):
        if y > h:
            break
        if i % 6 == 5:
            y += 14
            continue
        rect(30, y, (420 + (i * 97) % 260) if i % 6 != 4 else 300, 7, "#57606a")
        y += 17
    # a photo, the kind of saturated block that fights an overlay
    img = skia.Rect.MakeXYWH(max(30, w - 420), 70, 380, 230)
    shader = skia.GradientShader.MakeLinear(
        [skia.Point(img.left(), img.top()), skia.Point(img.right(), img.bottom())],
        [C("#4aa3ff"), C("#ffd27a"), C("#ff7a59")])
    c.drawRect(img, skia.Paint(Shader=shader))
    rect(img.left(), img.bottom() + 10, 300, 7, "#8c959f")
    rect(30, h - 120, w - 60, 60, "#eaeef2")
    rect(46, h - 100, 180, 18, "#1a7f37")
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def shot(widget, back=dark_backdrop, size=None, margin=MARGIN):
    m = widget.measure(UNBOUNDED)
    w, h = size or (math.ceil(m.width), math.ceil(m.height))
    surface = skia.Surface(w + 2 * margin, h + 2 * margin)
    c = surface.getCanvas()
    bg = back(w + 2 * margin, h + 2 * margin)
    c.drawImage(bg, 0, 0)
    widget.measure(Size(w, h))
    widget.arrange(skia.Rect.MakeXYWH(margin, margin, m.width, m.height))
    c.save()
    c.clipPath(widget.blur_shape(), doAntiAlias=False)
    blur = skia.Paint()
    blur.setImageFilter(skia.ImageFilters.Blur(BLUR_SIGMA, BLUR_SIGMA))
    c.drawImage(bg, 0, 0, skia.SamplingOptions(), blur)
    widget.paint(c)
    c.restore()
    surface.flushAndSubmit()
    return surface.makeImageSnapshot()


def make_call(agent, typed="", spin=0.0):
    host = LOOK.host_cls(agent)
    Widget.attach(host, FakeWindow())
    if typed:
        host.you_plate.input.set_text(typed)
    host.agent_chip.angle = -90 + spin
    return host


def make_status(agent, spin=0.0):
    widget = LookStatus(agent)
    Widget.attach(widget, FakeWindow())
    widget.chip.angle = -90 + spin
    return widget


def use(look):
    global LOOK
    LOOK = look


def compose(rows, out, title, captions):
    """rows: [(name, note, [images])], one labelled row per look."""
    gap, head, top = 24, 40, 52
    w = max(sum(i.width() for i in imgs) + gap * (len(imgs) + 1) for _n, _t, imgs in rows)
    h = top + sum(max(i.height() for i in imgs) + head for _n, _t, imgs in rows) + gap
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    txt(c, title, gap, 32, 16, theme.HUD_FG_BRIGHT, bold=True)
    y = top
    for n, (name, note, imgs) in enumerate(rows):
        x = gap
        txt(c, name, x, y + 26, 15, theme.YELLOW_BRIGHT, bold=True)
        nx = x + text.measure(name, 15, True) + 18
        txt(c, note, nx, y + 26, 12, theme.HUD_FG)
        for i, img in enumerate(imgs):
            if n == 0 and captions:
                cap = captions[i]
                txt(c, cap, x + img.width() - text.measure(cap, 11) - MARGIN,
                    y + 26, 11, theme.BASE_MUTED)
            c.drawImage(img, x, y + head)
            x += img.width() + gap
        y += max(i.height() for i in imgs) + head
    s.flushAndSubmit()
    s.makeImageSnapshot().save(out, skia.kPNG)
    print("wrote", out, f"{w}x{h}")


def render_sheets():
    rows = []
    for look in LOOKS:
        use(look)
        a = shot(make_call(working_agent(), spin=55))
        b = shot(make_call(reply_agent(), typed="do it"))
        st = shot(make_status(status_agent(), spin=70))
        rows.append((look.name, look.note, [a, b, st]))
    use(Look())
    compose(rows, "call_looks.png",
            "call overlay: tech-look variants at 1x (dark stand-in desktop, blur emulated)",
            ["working", "short reply", "hung up"])

    rows = []
    for look in [l for l in LOOKS if l.key in ("current", "signal", "open", "combined")]:
        use(look)
        a = shot(make_call(working_agent(), spin=55), back=bright_backdrop)
        b = shot(make_call(reply_agent(), typed="do it"), back=bright_backdrop)
        st = shot(make_status(status_agent(), spin=70), back=bright_backdrop)
        rows.append((look.name, "over a bright web page", [a, b, st]))
    use(Look())
    compose(rows, "call_looks_bright.png",
            "the most decorated looks over a bright page: legibility check at 1x",
            ["working", "short reply", "hung up"])


# ── gifs ────────────────────────────────────────────────────────────────
def encode(path, frames, w, h, fps=GIF_FPS):
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(fps),
           "-i", "-",
           "-filter_complex",
           "[0:v]split[a][b];[a]palettegen=stats_mode=full[p];"
           "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
           "-loop", "0", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = 0
    for img in frames:
        p.stdin.write(img.tobytes())
        n += 1
    p.stdin.close()
    p.wait()
    print(f"wrote {path}: {n} frames, {w}x{h}, {n / fps:.2f}s loop")


def crop(img, rect):
    s = skia.Surface(int(rect.width()), int(rect.height()))
    s.getCanvas().drawImage(img, -rect.left(), -rect.top())
    s.flushAndSubmit()
    return s.makeImageSnapshot()


T0 = 2000.0


def gif_trace():
    look = Trace()
    use(look)
    agent = agent_in(phase="idle", busy=False)
    host = make_call(agent)
    acts = [(0.78, Activity("note", "Checking the driver and the logs first.")),
            (1.8, Activity("tool", "$ journalctl -b -u NetworkManager --since -1h")),
            (3.0, Activity("tool", "$ iw dev wlp0s20f3 link"))]
    SEND, DONE, END = 0.7, 4.8, 7.4
    # the frame is sized to the tallest moment: the answer
    agent.heard, agent.message = HEARD, REPLY
    agent.activity.extend(a for _t, a in acts[1:])
    full = host.measure(UNBOUNDED)
    size = (math.ceil(full.width), math.ceil(full.height))
    agent.heard, agent.message = "", ""
    agent.activity.clear()
    host.you_plate.input.set_text(HEARD)

    frames = []
    angle = -90.0
    sent = done = False
    n = int(END * GIF_FPS)
    for i in range(n):
        t = i / GIF_FPS
        NOW[0] = T0 + t
        if t >= SEND and not sent:
            sent = True
            host.you_plate.input.set_text("")
            agent.heard = HEARD
            agent.busy, agent.phase, agent.turn_started = True, "working", NOW[0]
            look.sent_at = NOW[0]
            kick0 = NOW[0]
        for at, act in acts:
            if sent and not done and t >= at and act not in agent.activity:
                agent.activity.append(act)
        if t >= DONE and not done:
            done = True
            e = NOW[0] - agent.turn_started
            look.finish = (NOW[0], int(e // look.LAP), (e % look.LAP) / look.LAP)
            agent.busy, agent.phase = False, "idle"
            agent.activity.popleft()
            agent.message = REPLY
            host._nudge_t0 = NOW[0]
        if agent.busy:
            angle += SPIN["working"] / GIF_FPS
        else:
            off = (angle + 90.0) % 180.0
            off = off - 180.0 if off > 90.0 else off
            angle -= off * min(1.0, style.SETTLE_RATE / GIF_FPS)
        host.agent_chip.angle = angle
        if sent:
            k = min(1.0, (NOW[0] - kick0) / style.KICK_S)
            host.you_chip.angle = -90 + 180 * ease_out(k)
        frames.append(shot(host, size=size))
    w, h = frames[0].width(), frames[0].height()
    encode("gif/call_look_trace.gif", iter(frames), w, h)
    use(Look())


def stream_schedule(message, seed=7, start=0.3):
    """(time, length) steps: the reply arriving in token-sized chunks."""
    rng = random.Random(seed)
    out, t, n = [], start, 0
    while n < len(message):
        n = min(len(message), n + rng.randint(2, 7))
        out.append((t, n))
        t += rng.uniform(0.045, 0.11)
    return out


def shown_at(schedule, t):
    n = 0
    for at, k in schedule:
        if at <= t:
            n = k
    return n


def gif_signal():
    look = Signal()
    use(look)
    agent = agent_in(phase="idle", busy=False)
    host = make_call(agent)
    TYPE0, SEND, SPEAK, DONE, END = 0.2, 1.5, 3.2, 6.0, 7.6
    agent.heard, agent.message = HEARD, REPLY
    agent.activity.extend([Activity("tool", "$ iw dev wlp0s20f3 link"),
                           Activity("tool", "$ journalctl -k -g iwlwifi")])
    full = host.measure(UNBOUNDED)
    size = (math.ceil(full.width), math.ceil(full.height))
    agent.heard, agent.message = "", ""
    acts = list(agent.activity)
    agent.activity.clear()
    typed = "why does my wifi keep dropping every few minutes"
    per = (SEND - TYPE0 - 0.15) / len(typed)
    look.keys = [T0 + TYPE0 + i * per for i in range(len(typed))]
    sched = stream_schedule(REPLY, start=SPEAK)
    frames = []
    angle = -90.0
    n = int(END * GIF_FPS)
    for i in range(n):
        t = i / GIF_FPS
        NOW[0] = T0 + t
        if t < SEND:
            k = max(0, min(len(typed), int((t - TYPE0) / per) + 1)) if t >= TYPE0 else 0
            host.you_plate.input.set_text(typed[:k])
        elif not agent.busy and t < DONE:
            host.you_plate.input.set_text("")
            agent.heard = HEARD
            agent.busy, agent.phase, agent.turn_started = True, "working", NOW[0]
        if agent.busy:
            if t >= SEND + 0.4 and len(agent.activity) < 1:
                agent.activity.append(acts[0])
            if t >= SEND + 1.1 and len(agent.activity) < 2:
                agent.activity.append(acts[1])
            if t >= SPEAK:
                agent.phase = "speaking"
                agent.message = REPLY[:shown_at(sched, t)]
            if t >= DONE:
                agent.busy, agent.phase = False, "idle"
                agent.message = REPLY
        mode = agent.phase if agent.busy else "still"
        if mode in SPIN:
            angle += SPIN[mode] / GIF_FPS
        else:
            off = (angle + 90.0) % 180.0
            off = off - 180.0 if off > 90.0 else off
            angle -= off * min(1.0, style.SETTLE_RATE / GIF_FPS)
        host.agent_chip.angle = angle
        frames.append(shot(host, size=size))
    w, h = frames[0].width(), frames[0].height()
    encode("gif/call_look_signal.gif", iter(frames), w, h)
    use(Look())


# ── reply reveals ───────────────────────────────────────────────────────
def arrival(row, i):
    arr = ARRIVALS.get(row.key, [])
    return arr[i] if i < len(arr) else NOW[0]


def split_at(row, hold):
    """First index on the row younger than `hold` (arrivals only grow
    along a row, so everything after it is younger too)."""
    for i in range(row.start, row.end):
        if NOW[0] - arrival(row, i) < hold:
            return i
    return row.end


def chunks(row, a):
    """Runs of characters that arrived together, from index `a` on."""
    i = a
    while i < row.end:
        t0 = arrival(row, i)
        j = i
        while j < row.end and arrival(row, j) == t0:
            j += 1
        yield i, j, NOW[0] - t0
        i = j


def piece(row, a, b):
    r = copy.copy(row)
    r.start, r.end = a, b
    r.x = row.x + (a - row.start) * text.advance(row.size)
    r.cursor = False
    if a != row.start:
        r.bullet, r.bar = "", None
    return r


def block_cursor(c, box, row, accent):
    if not row.cursor:
        return
    x0 = box.left() + row.x + (row.end - row.start) * text.advance(row.size)
    c.drawRect(skia.Rect.MakeXYWH(x0 + 2, box.top() + 3, 8, box.height() - 6),
               P(accent, aa=False))


def bar_cursor(c, box, row, accent):
    """GlitchWipe's cursor: a 2px bar with a glow trailing behind it."""
    if not row.cursor:
        return
    x = box.left() + row.x + (row.end - row.start) * text.advance(row.size) + 1
    top, bottom = box.top() + 2, box.bottom() - 2
    glow = skia.GradientShader.MakeLinear(
        [skia.Point(x - 22, 0), skia.Point(x, 0)],
        [C(accent, 0.0), C(accent, 0.42)])
    c.drawRect(skia.Rect.MakeLTRB(x - 22, top, x, bottom), skia.Paint(Shader=glow))
    c.drawRect(skia.Rect.MakeXYWH(x, top - 1, 2, bottom - top + 2), P(accent, aa=False))
    c.drawRect(skia.Rect.MakeXYWH(x - 1, top - 1, 4, bottom - top + 2), P(accent, 0.5, blur=2.0))


class ChunkWipe(GlitchWipe):
    """The lyrics' GlitchWipe with its own cursor off: per chunk there
    would be one bar each, so a single bar leads the stream instead.
    (The shader draws its cursor only between `inset` and h - inset.)"""

    def shader(self, src, w, h, *, t, appear, seed=0.0):
        self.bleed = lambda _w, _h: 1e4
        try:
            return super().shader(src, w, h, t=t, appear=appear, seed=seed)
        finally:
            del self.bleed


class DecodeReveal:
    key = "decode"
    name = "R1  DECODE"
    HOLD = 0.42

    def __init__(self):
        # One GlitchWipe pass per arriving chunk: cells, tears and fringes
        # by the chunk's own age, so a pause in the stream never leaves
        # text undecoded.
        self.wipe = ChunkWipe(duration=self.HOLD, scan=0.12, settle=0.88,
                              shift=3.0, split=2.0, after=0.0,
                              spark=(0.36, 0.90, 0.94),
                              cursor=(0.725, 0.404, 1.0))

    def paint(self, plate, c, box, row, sel, accent):
        k = split_at(row, self.HOLD)
        paint_line(c, box, piece(row, row.start, k), sel, accent)
        cell = text.advance(row.size)
        x0 = box.left() + row.x
        for a, b, age in chunks(row, k):
            sub = piece(row, a, b)
            left = box.left() if a == row.start else x0 + (a - row.start) * cell
            rect = skia.Rect.MakeLTRB(left, box.top(), x0 + (b - row.start) * cell,
                                      box.bottom())
            paint_through(c, rect, lambda cv, s=sub: paint_line(cv, box, s, None, accent),
                          [self.wipe], t=NOW[0], appear=max(0.002, age / self.HOLD),
                          seed=(a * 7.31) % 13.0)
        bar_cursor(c, box, row, accent)


class ScrambleReveal:
    key = "scramble"
    name = "R2  SCRAMBLE"
    HOLD = 0.16
    CHARSET = "01@#$%&*+=<>{}[]|/\\?!~^"
    PALETTE = (theme.MAGENTA_BRIGHT, theme.CYAN_BRIGHT, theme.YELLOW_BRIGHT,
               theme.VIOLET_BRIGHT, theme.CYAN_MID)

    def paint(self, plate, c, box, row, sel, accent):
        k = split_at(row, self.HOLD)
        paint_line(c, box, piece(row, row.start, k), sel, accent)
        cell = text.advance(row.size)
        x0 = box.left() + row.x
        base = text.baseline_in(box, row.size)
        rng = random.Random(hash((row.key, row.start, int(NOW[0] * 28))))
        if k == row.start and row.bullet:
            paint_line(c, box, piece(row, row.start, row.start), None, accent)
        for i in range(k, row.end):
            if row.src[i].isspace():
                continue
            text.draw(c, rng.choice(self.CHARSET), x0 + (i - row.start) * cell,
                      base, row.size, C(rng.choice(self.PALETTE)))
        block_cursor(c, box, row, accent)


class PhosphorReveal:
    key = "phosphor"
    name = "R3  PHOSPHOR"
    HOLD = 0.55

    def paint(self, plate, c, box, row, sel, accent):
        k = split_at(row, self.HOLD)
        paint_line(c, box, piece(row, row.start, k), sel, accent)
        cell = text.advance(row.size)
        x0 = box.left() + row.x
        for a, b, age in chunks(row, k):
            heat = 1.0 - age / self.HOLD
            sub = piece(row, a, b)
            glow = copy.copy(sub)
            glow.color = theme.CYAN_BRIGHT
            bounds = skia.Rect.MakeLTRB(x0 + (a - row.start) * cell - 8, box.top() - 6,
                                        x0 + (b - row.start) * cell + 8, box.bottom() + 6)
            layer = skia.Paint()
            layer.setImageFilter(skia.ImageFilters.Blur(3.0, 3.0))
            layer.setAlphaf(0.85 * heat)
            c.saveLayer(bounds, layer)
            paint_line(c, box, glow, None, accent)
            c.restore()
            sub.color = theme.lerp(row.color, theme.CYAN_BLOOM, heat ** 1.5)
            paint_line(c, box, sub, None, accent)
        if row.cursor:
            x = x0 + (row.end - row.start) * cell + 2
            c.drawRect(skia.Rect.MakeXYWH(x - 2, box.top() + 1, 12, box.height() - 2),
                       P(accent, 0.45, blur=3.0))
        block_cursor(c, box, row, accent)


REVEALS = [DecodeReveal(), ScrambleReveal(), PhosphorReveal()]
REVEAL_MSG = ("Copy that. The card is **fine**; the drops are *power saving*. "
              "`iwlwifi` parks the radio after a few idle minutes and the AP "
              "drops you.\n\n"
              "- happens every ~4 min, only on battery\n"
              "- the fix is one line, per connection:\n\n"
              "```\nnmcli connection modify Home 802-11-wireless.powersave 2\n```")


def gif_reveal(reveal):
    global REVEAL
    use(Look())
    agent = agent_in(phase="speaking", busy=True, turn_started=T0 - 12, heard=HEARD,
                     activity=[Activity("tool", "$ iw dev wlp0s20f3 link"),
                               Activity("tool", "$ journalctl -k -g iwlwifi")])
    host = make_call(agent)
    agent.message = REVEAL_MSG
    full = host.measure(UNBOUNDED)
    size = (math.ceil(full.width), math.ceil(full.height))
    sched = stream_schedule(REVEAL_MSG, seed=11, start=0.35)
    end = sched[-1][0] + 1.6
    REVEAL = reveal
    ARRIVALS.clear()
    frames = []
    angle = -90.0
    n = int(end * GIF_FPS)
    for i in range(n):
        t = i / GIF_FPS
        NOW[0] = T0 + t
        k = shown_at(sched, t)
        agent.message = REVEAL_MSG[:k]
        streaming = k < len(REVEAL_MSG)
        agent.phase = "speaking" if streaming else "idle"
        agent.busy = streaming
        if streaming:
            angle += SPIN["speaking"] / GIF_FPS
        else:
            off = (angle + 90.0) % 180.0
            off = off - 180.0 if off > 90.0 else off
            angle -= off * min(1.0, style.SETTLE_RATE / GIF_FPS)
        host.agent_chip.angle = angle
        img = shot(host, size=size)
        # just the agent's row: the reply is the subject
        top = host.agent_plate.rect.top() - 14
        frames.append(crop(img, skia.Rect.MakeLTRB(0, top, img.width(), img.height())))
    REVEAL = None
    w, h = frames[0].width(), frames[0].height()
    encode(f"gif/call_reveal_{reveal.key}.gif", iter(frames), w, h)
    dump = os.environ.get("DUMP_FRAMES")
    if dump:                    # raw frames, to check against the gif
        for t in (1.2, 1.6, 2.4, 4.2):
            frames[int(t * GIF_FPS)].save(f"{dump}/{reveal.key}_{t}.png", skia.kPNG)


def render_reveals():
    for r in REVEALS:
        gif_reveal(r)


if __name__ == "__main__":
    what = sys.argv[1:] or ["sheet", "gif", "reveal"]
    if "sheet" in what:
        render_sheets()
    if "gif" in what:
        gif_trace()
        gif_signal()
    if "reveal" in what:
        render_reveals()
