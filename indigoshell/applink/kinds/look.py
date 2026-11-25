"""The look every app's items share in the top bar: the bar's sizes and
colours, the colour roles and icon names an app may use (PROTOCOL.md,
section 2), and the pieces the kinds paint with.

Each tab is a box of its own: an outline at the agent chip's height with
its bevel at the top right, the current one bright with a faint light
inside and a little glow, the others dim. Loading goes round a box's own
outline: a tab behind runs a comet round its box; the current one's
outline lights round from the bottom-left corner as the load goes, the
rest of it dimmed. An open input is one such lit box the section's width
(input.py). Each piece is one small function, the place to change it.
"""

import math
from dataclasses import dataclass

import skia

from ... import text, theme
from ...core import persona

# ── the bar's vocabulary ────────────────────────────────────────────────
# Its text size, its strong neon for what is in front and the muted tone
# for the rest, its plain foreground for text.
SIZE = theme.FONT_SIZE
NUMBER_SIZE = 13                    # bold, as the meters' small figures
TAG_SIZE = 11                       # bold: "NEW WINDOW"
GAP = theme.SPACING_MD              # between two items of a run
END_SHARE = 0.34                    # `end` takes at most this share of the section

TAB_FG = theme.BASE_MUTED
TAB_CURRENT_FG = theme.FG_STRONG
TAB_HOVER_FG = theme.FG             # the title of a tab under the pointer
NUMBER_FG = theme.BASE_MUTED
NUMBER_CURRENT_FG = theme.CYAN_BRIGHT
BRACKET_FG = theme.CYAN_MID         # round the current tab's number
PAUSED_DIM = 0.35                   # a taken-over tab's number: her colour this far
                                    # towards the bar's background (halfway is too near the
                                    # muted tabs' colour to tell apart)

# The tabs' slots: all the same width, SLOT_W while they fit, narrower
# alike down to SLOT_MIN, then the run of them around the current tab.
# Each slot is a box of its own, SLOT_GAP apart. Inside one: SLOT_PAD,
# the number's spot (bracketed on the current tab), NUMBER_GAP, the
# title, SLOT_PAD; the number never moves.
SLOT_W = 240
SLOT_MIN = 110
SLOT_GAP = 6                        # between two boxes
SLOT_PAD = 12
NUMBER_SPOT = 20
NUMBER_GAP = 6
BRACKET_ARM = 4

# The boxes: each tab enclosed by its own outline, the agent chip's
# height and bevel (top right), centred on the bar.
BOX_H = 32
BOX_BEVEL = 8
BOX_THICK = 2
OUTLINE = theme.FG_STRONG
OUTLINE_DIM_A = 0.28                # the tabs behind
OUTLINE_HOVER_A = 0.6               # ... one under the pointer
OUTLINE_GLOW = 3.0                  # the current one's glow: blur sigma
OUTLINE_GLOW_A = 0.45
FILL_A = 0.10                       # the light inside the current one, at its foot
LOAD_FG = theme.MAGENTA_BLOOM       # a load, going round a box's outline
LOAD_DIM_A = 0.35                   # the current one's outline not yet lit
COMET_LEN = 0.22                    # a tab behind loading: this share of its outline
LIFT = 3                            # a dragged tab, this far up
MARKER_W = 2                        # where it lands: an upright line between two boxes
TEAR_A = 0.45                       # a tab tearing out, dimmed this far
TEAR_TAG = "NEW WINDOW"
TEAR_FG = theme.CYAN_BRIGHT

# Motion, on the window's frame clock; nothing runs while nothing moves.
SWITCH_S = 0.11                     # the current tab's light sliding to another box
SLIDE_S = 0.11                      # boxes sliding to new places (a tab closed, a drag)
WIPE_S = 0.08                       # a closed tab wiping out; the others slide after it
COMET_S = 1.1                       # a load's comet, once round its box
MOTION_FPS = 60
COMET_FPS = 30

# ── colour roles and icons ──────────────────────────────────────────────
ROLE_COLORS = {"fg": theme.FG, "strong": theme.FG_STRONG, "muted": theme.BASE_MUTED,
               "info": theme.CYAN_BRIGHT, "accent": theme.FG_ACCENT, "warn": theme.YELLOW_MID,
               "error": theme.ERROR, "ok": theme.LIME_BRIGHT}

# Nerd Font glyphs (FontAwesome and Codicons), by the names apps use;
# the unsaved dot is a square (■), never a circle (the user's rule).
ICONS = {"bookmark": "\uf02e", "unlocked": "\uf09c", "search": "\uf002", "replace": "\uf0ec",
         "play": "\uf04b", "history": "\uf1da", "unsaved": "\u25a0", "branch": "\ue0a0",
         "error": "\uf057", "warning": "\uf071", "info": "\uf05a", "case": "\ueab1",
         "regex": "\ueb38", "file": "\uf15b", "folder": "\uf07b"}
CHECK = "\uf00c"                    # a row that is `on`
SUBMENU = "\uf054"                  # a row that opens a submenu
_said: set[str] = set()


def color_of(role, default: str = "fg") -> str:
    """The theme colour for a role (PROTOCOL.md, section 2): `agent` the
    agent's own (core/persona.py); an unknown role `default`'s."""
    if isinstance(role, str):
        if role == "agent":
            return persona.color()
        if role in ROLE_COLORS:
            return ROLE_COLORS[role]
    return ROLE_COLORS.get(default, theme.FG)


def glyph(name) -> str:
    """An icon's glyph; "" (one debug line, once) for a name the table
    does not have."""
    g = ICONS.get(name) if isinstance(name, str) else None
    if g is None and name not in _said:
        import logging
        _said.add(name)
        logging.getLogger(__name__).debug("no icon %r in the bar's table; drawn as nothing", name)
    return g or ""


def blend(a: str, b: str, t: float) -> str:
    """#rrggbb `t` of the way from `a` to `b`."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def agent_color(agent) -> str | None:
    """The colour of a tab's number for the agent's hold on it: hers
    while she drives, dimmed while the user has taken it over."""
    if not isinstance(agent, dict):
        return None
    color = persona.color()
    return color if agent.get("state") == "driving" else blend(color, theme.BAR_BG, PAUSED_DIM)


def title_in(title: str, room: float) -> str:
    """A title cut with an ellipsis to `room` px (nothing when not even
    the ellipsis fits)."""
    if text.measure(title, SIZE) <= room:
        return title
    return text.ellipsize(title, SIZE, room + 0.5) if text.measure("…", SIZE) <= room else ""


def paint_of(color: str, alpha: float | None = None, *, stroke: float | None = None,
           blur: float | None = None) -> skia.Paint:
    p = skia.Paint(AntiAlias=True)
    p.setColor(theme.color(color, None if alpha is None else round(alpha, 2)))
    if stroke is not None:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(stroke)
        p.setStrokeJoin(skia.Paint.kMiter_Join)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    return p


def baseline(cy: float, size: float, bold: bool = False) -> float:
    m = text.font(size, bold).getMetrics()
    return cy - (m.fAscent + m.fDescent) / 2


@dataclass(frozen=True)
class Band:
    """Where the boxes sit across a rect the bar's height."""
    top: float
    bottom: float
    cy: float               # the text's centre line

    @staticmethod
    def of(rect: skia.Rect) -> "Band":
        top = rect.top() + round((rect.height() - BOX_H) / 2)
        return Band(top, top + BOX_H, top + BOX_H / 2)


def box_path(x: float, w: float, band: Band, lift: float = 0.0) -> skia.Path:
    """A tab's outline, its stroke inside the box: from the bottom-left
    corner up, across the top to the bevel, down and back along the
    bottom (the way a load goes round it)."""
    h = BOX_THICK / 2
    l, r, t, b = x + h, x + w - h, band.top + h - lift, band.bottom - h - lift
    p = skia.Path()
    p.moveTo(l, b)
    p.lineTo(l, t)
    p.lineTo(r - BOX_BEVEL, t)
    p.lineTo(r, t + BOX_BEVEL)
    p.lineTo(r, b)
    p.close()
    return p


def paint_box(canvas: skia.Canvas, x: float, w: float, band: Band, *, alpha: float,
              color: str = OUTLINE) -> None:
    """A tab behind: its outline, dim (`alpha`; brighter under the
    pointer, in the agent's colour on a tab she holds)."""
    canvas.drawPath(box_path(x, w, band), paint_of(color, alpha, stroke=BOX_THICK))


def paint_current(canvas: skia.Canvas, x: float, w: float, band: Band,
                  lift: float = 0.0) -> None:
    """The tab in front: the faint light inside, the bright outline with
    a little glow."""
    path = box_path(x, w, band, lift)
    light = skia.Paint(AntiAlias=True, Shader=skia.GradientShader.MakeLinear(
        [(0, band.top - lift), (0, band.bottom - lift)],
        [theme.color(OUTLINE, 0.0), theme.color(OUTLINE, FILL_A)]))
    canvas.drawPath(path, light)
    canvas.drawPath(path, paint_of(OUTLINE, OUTLINE_GLOW_A, stroke=BOX_THICK, blur=OUTLINE_GLOW))
    canvas.drawPath(path, paint_of(OUTLINE, stroke=BOX_THICK))


def _segment(path: skia.Path, a: float, b: float) -> skia.Path:
    """The part of a closed path from `a` to `b` (shares of its length,
    0..1; past 1 wraps round)."""
    m = skia.PathMeasure(path, True)
    total = m.getLength()
    out = skia.Path()
    if b <= 1:
        m.getSegment(total * a, total * b, out, True)
    else:
        m.getSegment(total * a, total, out, True)
        m.getSegment(0, total * (b - 1), out, True)
    return out


def paint_trace(canvas: skia.Canvas, x: float, w: float, band: Band, frac: float) -> None:
    """The tab in front loading: its outline lit from the bottom-left
    corner round, `frac` of the way, the rest dimmed."""
    path = box_path(x, w, band)
    canvas.drawPath(path, paint_of(theme.BAR_BG, 1 - LOAD_DIM_A, stroke=BOX_THICK + 1))
    if frac <= 0:
        return
    lit = _segment(path, 0.0, min(1.0, frac))
    canvas.drawPath(lit, paint_of(LOAD_FG, stroke=BOX_THICK + 1, blur=3))
    canvas.drawPath(lit, paint_of(LOAD_FG, stroke=BOX_THICK))


def paint_comet(canvas: skia.Canvas, x: float, w: float, band: Band, phase: float) -> None:
    """A tab behind loading: a bright run going round its outline, its
    head `phase` (0..1) of the way, its tail fading."""
    path = box_path(x, w, band)
    pieces = 6
    for k in range(pieces):
        a = phase + COMET_LEN * k / pieces
        canvas.drawPath(_segment(path, a % 1.0, a % 1.0 + COMET_LEN / pieces),
                        paint_of(LOAD_FG, (k + 1) / pieces, stroke=BOX_THICK))
    head = (phase + COMET_LEN) % 1.0
    canvas.drawPath(_segment(path, max(0.0, head - 0.02), head + (0.0 if head >= 0.02 else 0)),
                    paint_of("#ffffff", 0.8, stroke=BOX_THICK))


def paint_marker(canvas: skia.Canvas, x: float, rect: skia.Rect) -> None:
    """Where a dragged tab lands: an upright line between two boxes, the
    bar's height, over the dragged box (which is nearly always over it)."""
    p = skia.Paint(AntiAlias=False)
    p.setColor(theme.color(OUTLINE))
    canvas.drawRect(skia.Rect.MakeXYWH(round(x - MARKER_W / 2), rect.top() + 1, MARKER_W,
                                       rect.height() - 2), p)


def brackets(canvas: skia.Canvas, spot: skia.Rect, color: str) -> None:
    """Corner brackets round the number's spot, as the meters' frame."""
    x, y, w, h, a = spot.left(), spot.top(), spot.width(), spot.height(), BRACKET_ARM
    path = skia.Path()
    for rx, ry, rw, rh in ((0, 0, a, 1), (0, 0, 1, a), (w - a, 0, a, 1), (w - 1, 0, 1, a),
                           (0, h - 1, a, 1), (0, h - a, 1, a), (w - a, h - 1, a, 1),
                           (w - 1, h - a, 1, a)):
        path.addRect(skia.Rect.MakeXYWH(x + rx, y + ry, rw, rh))
    p = skia.Paint(AntiAlias=False)
    p.setColor(theme.color(color))
    canvas.drawPath(path, p)


def number_spot(x: float, cy: float) -> skia.Rect:
    return skia.Rect.MakeXYWH(x + SLOT_PAD, cy - NUMBER_SPOT / 2, NUMBER_SPOT, NUMBER_SPOT)


def paint_number(canvas: skia.Canvas, number: str, spot: skia.Rect, color: str,
                 bold: bool = True) -> None:
    w = text.measure(number, NUMBER_SIZE, bold)
    text.draw(canvas, number, spot.centerX() - w / 2, baseline(spot.centerY(), NUMBER_SIZE, bold),
              NUMBER_SIZE, theme.color(color), bold)


def paint_cell(canvas: skia.Canvas, x: float, cy: float, number: str, title: str, *,
               current: bool, hovered: bool, mark: str | None,
               badge: tuple[str, str] | None = None, w: float = 0.0) -> None:
    """One tab's number in its spot (bracketed on the current tab, and in
    the agent's colour on a tab she holds), its title, and its
    badge (glyph, colour) at the box's right end."""
    spot = number_spot(x, cy)
    if current or mark:
        brackets(canvas, spot, mark or BRACKET_FG)
    paint_number(canvas, number, spot, mark or (NUMBER_CURRENT_FG if current else NUMBER_FG))
    fg = TAB_CURRENT_FG if current else TAB_HOVER_FG if hovered else TAB_FG
    text.draw(canvas, title, x + SLOT_PAD + NUMBER_SPOT + NUMBER_GAP, baseline(cy, SIZE), SIZE,
              theme.color(fg))
    if badge and badge[0] and w:
        gw = text.measure(badge[0], SIZE)
        text.draw(canvas, badge[0], x + w - SLOT_PAD - gw, baseline(cy, SIZE), SIZE,
                  theme.color(badge[1]))


def badge_room(badge: tuple[str, str] | None) -> float:
    """What a badge takes from its tab's title: its glyph and a gap."""
    return text.measure(badge[0], SIZE) + NUMBER_GAP if badge and badge[0] else 0.0


def paint_lifted(canvas: skia.Canvas, x: float, w: float, band: Band, number: str,
                 title: str, mark: str | None, *, out: bool, right: float,
                 badge: tuple[str, str] | None = None) -> None:
    """The tab being dragged: its box LIFT up, over what it passes; while
    letting go would tear it out, dimmed and saying so beside it (before
    it when there is no room after, up to `right`)."""
    canvas.drawPath(box_path(x, w, band, LIFT), paint_of(theme.BAR_BG, 1.0 if out else 0.92))
    if out:
        canvas.saveLayerAlpha(None, round(255 * (1 - TEAR_A)))
    paint_current(canvas, x, w, band, LIFT)
    paint_cell(canvas, x, band.cy - LIFT, number, title, current=True, hovered=False,
               mark=mark, badge=badge, w=w)
    if out:
        canvas.restore()
        tag_w = text.measure(TEAR_TAG, TAG_SIZE, True)
        tx = x + w + 10 if x + w + 10 + tag_w <= right else x - 10 - tag_w
        text.draw(canvas, TEAR_TAG, tx, baseline(band.cy - LIFT, TAG_SIZE, True), TAG_SIZE,
                  theme.color(TEAR_FG), True)


def paint_wipe(canvas: skia.Canvas, x: float, w: float, band: Band, number: str, title: str,
               k: float) -> None:
    """A closed tab wiping out, `k` (0..1) of the way: what is left of its
    box from the left."""
    canvas.save()
    canvas.clipRect(skia.Rect.MakeLTRB(x - 2, band.top - 2, x + w * (1 - k), band.bottom + 2))
    paint_box(canvas, x, w, band, alpha=OUTLINE_DIM_A)
    paint_cell(canvas, x, band.cy, number, title, current=False, hovered=False, mark=None)
    canvas.restore()


class Glide:
    """A number easing to where it is told to go over `seconds` (ease-out
    cubic), by the clock: `at(now)`. Time-based, so the frames the clock
    happens to give it only decide how often it is looked at."""

    def __init__(self, seconds: float, value: float) -> None:
        self.seconds = seconds
        self.start = self.target = value
        self.t0 = -math.inf

    def at(self, now: float) -> float:
        k = (now - self.t0) / self.seconds
        if k >= 1:
            return self.target
        e = 1 - (1 - max(0.0, k)) ** 3
        return self.start + (self.target - self.start) * e

    def to(self, target: float, now: float, delay: float = 0.0) -> None:
        """Go to `target`, from where it is now, `delay` s from now."""
        if target != self.target:
            self.start, self.target, self.t0 = self.at(now), target, now + delay

    def moving(self, now: float) -> bool:
        return now - self.t0 < self.seconds

    def settle(self) -> None:
        self.t0 = -math.inf
