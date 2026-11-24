"""Flux in the shell: the top bar's content for a focused browser window,
and the launcher for its Ctrl+L, Ctrl+T and Ctrl+M.

A Flux window (flux/qt) connects to the shell's app service
(applink/service.py) and announces itself as a "flux" window; the
service gives it a `FluxWindow`, which takes the state the browser
pushes (protocol.py) into `FluxContent`. While that window has focus the
top bar's context area shows it (applink/context.py); the browser
itself draws nothing of it.

What it shows: the window's tabs from the left, each a box of its own
in a slot of the same width (SLOT_W, all narrower alike when they do
not fit, down to SLOT_MIN, then the run around the current one), a few
pixels apart, outlined with the agent chip's bevel at the top right:
its number in a spot of its own and its title, shortened with an
ellipsis to the box, in the muted tone, the outline dim; the current
tab's outline bright with a faint light inside, its title in the strong
colour and its number cyan in small corner brackets. At the right end
of the context area the current page's host, after an open padlock when
its connection is not secure and a bookmark when the page is one.
Loading goes round a box's own outline: a tab behind runs a comet round
its box; while the current one loads its outline lights round from the
bottom-left corner as the load goes, the rest of it dimmed. While the
browser's find is open it stands in the tabs' place as one lit box: a
search mark in the bracketed spot, `query▏` (the caret only while the
query is typed), the count small, "no matches" in the error colour.
Nothing is drawn along the bar's edge.

The mouse on the tabs (TabList): a left or right press switches to the
tab, a middle click closes it, a drag along the bar moves it, a drag
off the bar's bottom edge tears it out into a window of its own; each a
command to the browser (protocol.py: `tab`, `close`, `move`, `detach`)
naming the tab by its id. The press is held by the bar's window from
button down to button up (window.py, Widget.press); for the drag it
takes the keyboard too, so that Escape can put the tab back. A tab
under the pointer has its outline a step brighter and its title
brighter; a dragged tab lifts a little over the others, which close up,
a thin upright line between two boxes saying where it lands; past the
tear-out line it dims and says NEW WINDOW. Switching slides the current
tab's light to the new box; a tab closed wipes out and the others slide
over.

The agent. While the agent drives a tab (protocol `agent`), that tab's
number, its corner brackets and its box's outline are drawn in the
voice's colour
(protocol.VOICES); while the user has taken it over and the agent
waits, in that colour dimmed a third of the way to the bar's
background. The title keeps its own colour. The window's
actions (Ctrl+M) then begin with "Let Lilac drive again" (the voice's
name) for each tab taken over, which sends the browser `resume`. The
agent's own window (its announce names the voice: protocol `agent`,
"Alone") says whose it is before its tabs: the voice's name in its
colour, "Lilac", plain like the rest.

The launcher. The shell takes Ctrl+L, Ctrl+T and Ctrl+M from every main
browser window (protocol.KEYS, grabbed by the app service: applink/
service.py); a single window (a sign-in popup, the inspector) keeps
them. The press arrives here (`on_key`) with the keyboard: it is held
past the key's release at once (`hold_keyboard`), and the shell's
launcher opens (widgets/launcher.py, `open_launcher`) on Flux's addresses
(services/addresses.py: Ctrl+L with the page's address as the query,
selected; Ctrl+T empty) or its actions (a services/modes.Choices of "Add
bookmark" and "Remove bookmark", the one that does not apply dimmed),
and takes the keyboard with a grab of its own. Keys typed between the
press and that grab still come in on the browser's window (`on_typed`)
and go to the launcher's query in the order typed, ahead of the ones
typed there since. It drops from the top bar: centred on the browser's
window, its top at the bar's bottom edge, kept on that window's monitor
(window.Placement); with no top bar over the window, from the window's
own top edge. Picking a row sends the browser `load` or `bookmark`;
Shift+Delete on a bookmark's row in the address list sends `bookmark`
"remove" with that row's address. A new window opened with no address
asks for the same with a `launcher` message. With no launcher to show
the keyboard goes back and the browser is told to ask itself (`prompt`).

Import rule: of the browser package only `protocol` and `places` (both
pure) are imported here; nothing under `flux/qt` ever is, and
nothing there imports this module.
"""

import logging
import math
import time
from dataclasses import dataclass
from functools import partial

import skia
import xcffib.xproto as xp

from .. import text, theme
from .service import AppWindow
from ..services.addresses import BOOKMARK, AddressMode
from ..services.modes import Choice, Choices
from ..widgets.base import Insets, KeyEvent, Size, Widget
from ..widgets.layout import Align, Row, Spacer
from ..window import Placement
from flux import places, protocol

log = logging.getLogger(__name__)

# ── look ────────────────────────────────────────────────────────────────
# The bar's own vocabulary: its text size, its strong neon for the tab in
# front and the muted tone for the rest, its plain foreground for the
# host. Each tab is a box of its own (TabList).
TAB_FG = theme.BASE_MUTED
TAB_CURRENT_FG = theme.FG_STRONG
TAB_HOVER_FG = theme.FG             # the title of a tab under the pointer
NUMBER_FG = theme.BASE_MUTED
NUMBER_CURRENT_FG = theme.CYAN_BRIGHT
BRACKET_FG = theme.CYAN_MID         # round the current tab's number
HOST_FG = theme.FG
INSECURE_FG = theme.ERROR
INSECURE_MARK = ""            # an open padlock (Nerd Font: fa-unlock)
BOOKMARK_FG = theme.FG
BOOKMARK_MARK = BOOKMARK            # Nerd Font: fa-bookmark
FIND_QUERY_FG = theme.FG_STRONG
FIND_COUNT_FG = theme.CYAN_BRIGHT
NO_MATCHES_FG = theme.ERROR
SEARCH_MARK = ""              # Nerd Font: fa-search, in the find tab's number spot
SIZE = theme.FONT_SIZE
NUMBER_SIZE = 13                    # bold, as the meters' small figures
TAG_SIZE = 11                       # bold: "NEW WINDOW"
PAD = Insets()                      # the bar's own gutter is around it already
HOST_CAP = 0.34                     # the host takes at most this share of the row
PAUSED_DIM = 0.35                   # a taken-over tab's number: the voice's colour this far
                                    # towards the bar's background (halfway is too near the
                                    # muted tabs' colour to tell apart)
RESUME_MARK = ""              # Nerd Font: fa-play

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
# height and bevel (top right), centred on the bar. The current one's
# outline bright with a faint light inside, the others' dim.
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

# The mouse on them (TabList).
DRAG_START_PX = 6                   # a press moved this far lifts the tab
TEAR_OUT_PX = 40                    # let go this far below the bar: a window of its own

# Motion, on the window's frame clock; nothing runs while nothing moves.
SWITCH_S = 0.11                     # the current tab's light sliding to another box
SLIDE_S = 0.11                      # boxes sliding to new places (a tab closed, a drag)
WIPE_S = 0.08                       # a closed tab wiping out; the others slide after it
COMET_S = 1.1                       # a load's comet, once round its box
MOTION_FPS = 60
COMET_FPS = 30


def _blend(a: str, b: str, t: float) -> str:
    """#rrggbb `t` of the way from `a` to `b`."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def agent_color(agent: dict | None) -> str | None:
    """The colour of a tab's number for the agent's hold on it: its
    voice's while it drives, dimmed while the user has taken it over."""
    if not agent:
        return None
    color = protocol.voice_color(agent.get("voice"))
    return color if agent.get("state") == "driving" else _blend(color, theme.BAR_BG, PAUSED_DIM)


# ── widgets ─────────────────────────────────────────────────────────────
# Page titles and hosts are text from elsewhere: drawn through the
# shell's per-character fallback, not shaped (see text.py).

def fit_slots(n: int, current: int, width: float) -> tuple[int, int, int]:
    """Lay `n` tabs out in `width` px as equal slots: (the first one
    shown, how many, each one's width). SLOT_W each when they fit;
    otherwise all narrower alike, down to SLOT_MIN; below that, as many
    SLOT_MIN slots as fit, the run around the current tab. Slot k of the
    run starts at k * (width + SLOT_GAP), whole pixels."""
    if n <= 0 or width <= 0:
        return 0, 0, 0
    pitch = (width + SLOT_GAP) / n
    w = int(min(SLOT_W, pitch - SLOT_GAP))
    if w >= SLOT_MIN:
        return 0, n, w
    w = int(min(SLOT_MIN, width))
    count = max(1, min(n, int((width + SLOT_GAP) // (w + SLOT_GAP))))
    first = max(0, min(max(0, min(current, n - 1)) - count // 2, n - count))
    return first, count, w


def slot_at(x: float, count: int, w: int) -> int | None:
    """Which of `count` slots of width `w` holds local x (a gap goes with
    the slot before it); None past the last."""
    if count <= 0 or x < 0:
        return None
    k = int(x // (w + SLOT_GAP))
    if k >= count or x >= k * (w + SLOT_GAP) + w + (SLOT_GAP if k < count - 1 else 0):
        return None
    return k


def landing(at: float, count: int, w: int) -> int:
    """The slot of a run of `count` that a tab dragged with its left edge
    at local x `at` drops into: the one its middle is over."""
    return max(0, min(count - 1, int((at + w / 2) // (w + SLOT_GAP))))


def title_in(title: str, room: float) -> str:
    """A tab's title cut with an ellipsis to `room` px (nothing when not
    even the ellipsis fits)."""
    if text.measure(title, SIZE) <= room:
        return title
    return text.ellipsize(title, SIZE, room + 0.5) if text.measure("…", SIZE) <= room else ""


def fit_find(find: dict, width: float) -> list[tuple[str, str, float, float]]:
    """The find line as one tab, in `width` px: (role, text, x, width)
    for "mark" (the search glyph in the number's spot), "query" (with the
    caret while typing) and "count" (when there is one; small, bold). A
    query too long for the room loses its start, so the end, where typing
    happens, stays in view. The tab ends SLOT_PAD after the last piece."""
    caret = protocol.FIND_CARET if find.get("typing") else ""
    count = protocol.find_count(find)
    qx = SLOT_PAD + NUMBER_SPOT + NUMBER_GAP
    count_w = text.measure(count, NUMBER_SIZE, True) if count else 0.0
    room = (width - qx - SLOT_PAD - text.measure(caret, SIZE)
            - (NUMBER_GAP * 2 + count_w if count else 0))
    query = find.get("query", "")
    if text.measure(query, SIZE) > room:
        while query and text.measure("…" + query, SIZE) > room:
            query = query[1:]
        query = "…" + query if query else ""
    out = [("mark", SEARCH_MARK, float(SLOT_PAD), float(NUMBER_SPOT))]
    qw = text.measure(query + caret, SIZE)
    out.append(("query", query + caret, float(qx), qw))
    if count:
        out.append(("count", count, qx + qw + NUMBER_GAP * 2, count_w))
    return out


# ── the tabs' look ──────────────────────────────────────────────────────
# Each tab a box of its own in its slot, outlined: the current one's
# outline bright with a faint light inside, the others' dim. Loading goes
# round a box's own outline. Each piece is one small function, the place
# to change it.

def _paint(color: str, alpha: float | None = None, *, stroke: float | None = None,
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


def _baseline(cy: float, size: float, bold: bool = False) -> float:
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
    pointer, in the voice's colour on a tab the agent holds)."""
    canvas.drawPath(box_path(x, w, band), _paint(color, alpha, stroke=BOX_THICK))


def paint_current(canvas: skia.Canvas, x: float, w: float, band: Band,
                  lift: float = 0.0) -> None:
    """The tab in front: the faint light inside, the bright outline with
    a little glow."""
    path = box_path(x, w, band, lift)
    light = skia.Paint(AntiAlias=True, Shader=skia.GradientShader.MakeLinear(
        [(0, band.top - lift), (0, band.bottom - lift)],
        [theme.color(OUTLINE, 0.0), theme.color(OUTLINE, FILL_A)]))
    canvas.drawPath(path, light)
    canvas.drawPath(path, _paint(OUTLINE, OUTLINE_GLOW_A, stroke=BOX_THICK, blur=OUTLINE_GLOW))
    canvas.drawPath(path, _paint(OUTLINE, stroke=BOX_THICK))


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
    canvas.drawPath(path, _paint(theme.BAR_BG, 1 - LOAD_DIM_A, stroke=BOX_THICK + 1))
    if frac <= 0:
        return
    lit = _segment(path, 0.0, min(1.0, frac))
    canvas.drawPath(lit, _paint(LOAD_FG, stroke=BOX_THICK + 1, blur=3))
    canvas.drawPath(lit, _paint(LOAD_FG, stroke=BOX_THICK))


def paint_comet(canvas: skia.Canvas, x: float, w: float, band: Band, phase: float) -> None:
    """A tab behind loading: a bright run going round its outline, its
    head `phase` (0..1) of the way, its tail fading."""
    path = box_path(x, w, band)
    pieces = 6
    for k in range(pieces):
        a = phase + COMET_LEN * k / pieces
        canvas.drawPath(_segment(path, a % 1.0, a % 1.0 + COMET_LEN / pieces),
                        _paint(LOAD_FG, (k + 1) / pieces, stroke=BOX_THICK))
    head = (phase + COMET_LEN) % 1.0
    canvas.drawPath(_segment(path, max(0.0, head - 0.02), head + (0.0 if head >= 0.02 else 0)),
                    _paint("#ffffff", 0.8, stroke=BOX_THICK))


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
    text.draw(canvas, number, spot.centerX() - w / 2, _baseline(spot.centerY(), NUMBER_SIZE, bold),
              NUMBER_SIZE, theme.color(color), bold)


def paint_cell(canvas: skia.Canvas, x: float, cy: float, number: str, title: str, *,
               current: bool, hovered: bool, mark: str | None) -> None:
    """One tab's number in its spot (bracketed on the current tab, and in
    the voice's colour on a tab the agent holds) and its title."""
    spot = number_spot(x, cy)
    if current or mark:
        brackets(canvas, spot, mark or BRACKET_FG)
    paint_number(canvas, number, spot, mark or (NUMBER_CURRENT_FG if current else NUMBER_FG))
    fg = TAB_CURRENT_FG if current else TAB_HOVER_FG if hovered else TAB_FG
    text.draw(canvas, title, x + SLOT_PAD + NUMBER_SPOT + NUMBER_GAP, _baseline(cy, SIZE), SIZE,
              theme.color(fg))


def paint_lifted(canvas: skia.Canvas, x: float, w: float, band: Band, number: str,
                 title: str, mark: str | None, *, out: bool, right: float) -> None:
    """The tab being dragged: its box LIFT up, over what it passes; while
    letting go would tear it out, dimmed and saying so beside it (before
    it when there is no room after, up to `right`)."""
    canvas.drawPath(box_path(x, w, band, LIFT), _paint(theme.BAR_BG, 1.0 if out else 0.92))
    if out:
        canvas.saveLayerAlpha(None, round(255 * (1 - TEAR_A)))
    paint_current(canvas, x, w, band, LIFT)
    paint_cell(canvas, x, band.cy - LIFT, number, title, current=True, hovered=False,
               mark=mark)
    if out:
        canvas.restore()
        tag_w = text.measure(TEAR_TAG, TAG_SIZE, True)
        tx = x + w + 10 if x + w + 10 + tag_w <= right else x - 10 - tag_w
        text.draw(canvas, TEAR_TAG, tx, _baseline(band.cy - LIFT, TAG_SIZE, True), TAG_SIZE,
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


def paint_find(canvas: skia.Canvas, x: float, pieces: list, band: Band) -> float:
    """The find line as one box from x, lit as the current tab's: the
    search mark in the bracketed spot, the query with its caret, the
    count small; its right edge."""
    end = max(px + pw for _role, _t, px, pw in pieces) + SLOT_PAD
    paint_current(canvas, x, end, band)
    for role, piece, px, _pw in pieces:
        if role == "mark":
            spot = number_spot(x, band.cy)
            brackets(canvas, spot, BRACKET_FG)
            paint_number(canvas, piece, spot, NUMBER_CURRENT_FG, bold=False)
        elif role == "query":
            text.draw(canvas, piece, x + px, _baseline(band.cy, SIZE), SIZE,
                      theme.color(FIND_QUERY_FG))
        else:
            color = NO_MATCHES_FG if piece == protocol.NO_MATCHES else FIND_COUNT_FG
            text.draw(canvas, piece, x + px, _baseline(band.cy, NUMBER_SIZE, True),
                      NUMBER_SIZE, theme.color(color), True)
    return x + end


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


@dataclass
class TabDrag:
    """A tab pressed with button 1 or 3, by its id, and where the
    pointer has been since (window-local)."""
    tab: int
    button: int
    x0: float
    y0: float
    grab: float             # the press's x in the tab's slot
    x: float
    y: float
    lifted: bool = False    # moved DRAG_START_PX: the tab follows the pointer


class TabList(Widget):
    """The window's tabs from the left of the context area, each a box
    of its own in a slot of the same width (`fit_slots`): the current
    one's outline bright with a faint light inside, the others' dim; the
    find line in their place while find is open, as one lit box.

    Loading goes round a box's own outline: a tab behind runs a comet
    round its box, and the current tab's outline lights from its
    bottom-left corner round as the page loads, the rest of it dimmed.

    Motion (`animation_fps`, nothing while nothing moves): switching
    slides the current tab's light to the new box (SWITCH_S); a tab gone
    wipes out (WIPE_S), then the others slide to close the gap (SLIDE_S),
    as they slide closed up under a dragged tab. Positions are glides by
    tab id (`Glide`), retargeted at each paint from where the tabs are
    now.

    The mouse (the window holds the press: Widget.press). A left or right
    press on a tab switches to it at once; a middle click closes it (let
    go over the same tab). Pressed and moved DRAG_START_PX, the tab lifts
    and follows the pointer along the bar, the others closed up and a
    marker between two of them where it would land, and letting go over
    the bar moves it there; dragged
    more than TEAR_OUT_PX below the bar and let go, it tears out into a
    window of its own (not a window's only tab). Escape while dragging
    puts it back (the window takes the keyboard for it, when it can).
    Each is a command to the browser naming the tab by its id (`send`,
    FluxWindow's), so tabs coming or going meanwhile change nothing of
    what the click meant; a tab dragged that goes away lets go of the
    press.
    """

    def __init__(self) -> None:
        super().__init__(flex=1)
        self.titles: list[str] = []
        self.ids: list[int] = []
        self.current = 0
        self.agents: list[dict | None] = []
        self.find: dict | None = None
        # The slots shown: the first tab's index, how many, their width;
        # each one's label.
        self.first = self.count = self.slot_w = 0
        self.labels: list[str] = []
        self.find_drawn: list[tuple[str, str, float, float]] = []
        self.hover: int | None = None           # the tab under the pointer
        self.drag: TabDrag | None = None        # a left or right press on a tab
        self.closing: int | None = None         # the tab a middle press is on
        self.send = None                        # send(name, **args): to the browser
        self.loading: list[bool] = []
        self.progress: int | None = None        # the current tab's load, while it loads
        # Motion: each tab's slot x as drawn (by id), the current tab's
        # light's x,
        # the tabs gone and wiping out (since, index, title, x), what the
        # last paint drew (id -> (index, title, x)), and whether something
        # moved since (the next frame retargets).
        self._glides: dict[int, Glide] = {}
        self._front: Glide | None = None
        self._leaving: list[tuple[float, int, str, float]] = []
        self._drawn_ids: dict[int, tuple[int, str, float]] = {}
        self._due = False
        self._fresh = True                      # nothing drawn yet: no motion into place

    def set_tabs(self, titles: list[str], current: int,
                 agents: list[dict | None] | None = None,
                 ids: list[int] | None = None,
                 loading: list[bool] | None = None) -> None:
        agents = list(agents) if agents is not None else [None] * len(titles)
        ids = list(ids) if ids is not None else list(range(len(titles)))
        loading = list(loading) if loading is not None else [False] * len(titles)
        if titles != self.titles or current != self.current or ids != self.ids:
            self.titles, self.current, self.agents, self.ids = list(titles), current, agents, ids
            self.loading = loading
            self._moved(layout=True)
        elif agents != self.agents or loading != self.loading:
            self.agents, self.loading = agents, loading
            self.invalidate()
        self._still_there()

    def set_find(self, find: dict | None) -> None:
        if find != self.find:
            self.find = dict(find) if find else None
            self._moved(layout=True)
        self._still_there()

    def set_progress(self, value: int | None) -> None:
        if value != self.progress:
            self.progress = value
            self.invalidate()

    def _moved(self, layout: bool = False) -> None:
        """Something may have moved: the next frame retargets the glides."""
        self._due = True
        self.invalidate(layout=layout)

    def measure(self, avail: Size) -> Size:
        return Size(0.0, text.line_height(SIZE))

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.find is not None:
            self.first = self.count = self.slot_w = 0
            self.labels = []
            self.find_drawn = fit_find(self.find, rect.width())
            return
        self.first, self.count, self.slot_w = fit_slots(len(self.titles), self.current,
                                                        rect.width())
        room = self.slot_w - 2 * SLOT_PAD - NUMBER_SPOT - NUMBER_GAP
        self.labels = [title_in(self.titles[i], room)
                       for i in range(self.first, self.first + self.count)]
        self.find_drawn = []

    # ── geometry ────────────────────────────────────────────────────────
    @property
    def pitch(self) -> int:
        return self.slot_w + SLOT_GAP

    def tab_at(self, x: float, y: float | None = None) -> int | None:
        """The tab whose slot holds window x (and y, when given); None
        between none, and while find shows."""
        if self.find is not None or not self.count:
            return None
        if y is not None and not self.rect.top() <= y < self.rect.bottom():
            return None
        k = slot_at(x - self.rect.left(), self.count, self.slot_w)
        return None if k is None else self.first + k

    def arrangement(self) -> tuple[list[tuple[int, int]], dict | None]:
        """Each shown tab's slot, as (index, x from the left of the list);
        while one is lifted, the others closed up, and the lifted one:
        {"index", "x" (its box's, following the pointer), "to" (the index
        it lands at), "slot_x" (where: the left edge of the place between
        two boxes it goes into), "out" (letting go tears it out)}."""
        run = range(self.first, self.first + self.count)
        d = self.drag
        held = self.ids.index(d.tab) if d is not None and d.tab in self.ids else None
        if d is None or not d.lifted or held not in run:
            return [(i, (i - self.first) * self.pitch) for i in run], None
        at = max(0.0, min(d.x - d.grab - self.rect.left(), (self.count - 1) * self.pitch))
        out = self._tears_out(d.y)
        k = landing(at, self.count, self.slot_w)
        others = [i for i in run if i != held]
        return ([(i, p * self.pitch) for p, i in enumerate(others)],
                {"index": held, "x": at, "to": self.first + k, "slot_x": k * self.pitch,
                 "out": out})

    def _tears_out(self, y: float) -> bool:
        return len(self.titles) > 1 and y > self.rect.bottom() + TEAR_OUT_PX

    def _label(self, i: int) -> str:
        k = i - self.first
        return self.labels[k] if 0 <= k < len(self.labels) else ""

    def _mark(self, i: int) -> str | None:
        return agent_color(self.agents[i]) if i < len(self.agents) else None

    # ── the mouse ───────────────────────────────────────────────────────
    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.send is not None and self.tab_at(x, y) is not None else None

    def press(self, button: int, x: float, y: float) -> bool:
        i = self.tab_at(x, y)
        if i is None or self.send is None or button not in (1, 2, 3):
            return False
        tab = self.ids[i]
        if button == 2:
            self.closing = tab
            return True
        if i != self.current:
            self._send("tab", tab=tab)
        left = self.rect.left() + (i - self.first) * self.pitch
        self.drag = TabDrag(tab, button, x, y, x - left, x, y)
        return True

    def motion(self, x: float, y: float) -> None:
        d = self.drag
        if d is None:
            if self.closing is None:
                self._set_hover(self.tab_at(x, y))
            return
        d.x, d.y = x, y
        if not d.lifted:
            if math.hypot(x - d.x0, y - d.y0) < DRAG_START_PX:
                return
            d.lifted = True
            self.hover = None
            hold = getattr(self.window, "hold_keyboard", None)
            if hold is not None:
                hold(self)              # for Escape; the drag works without it
        self._moved()

    def release(self, button: int, x: float, y: float) -> None:
        d, closing = self.drag, self.closing
        if closing is not None:
            self.closing = None
            if button == 2 and self._id_at(x, y) == closing:
                self._send("close", tab=closing)
            return
        if d is None or button != d.button:
            return
        d.x, d.y = x, y
        _placed, lift = self.arrangement()
        self.drag = None
        if not d.lifted:
            return                      # a click: switched at the press
        self._moved()
        if lift is None:
            return                      # its tab went from the list meanwhile
        # It goes down where it was let go: torn out, it wipes out from
        # there once the browser says it went; moved, it slides into its
        # place, lit.
        self._glides[d.tab] = Glide(SLIDE_S, lift["x"])
        if not lift["out"]:
            self._front = Glide(SWITCH_S, lift["x"])
            self._glides[d.tab].to(lift["slot_x"], time.monotonic())
            self._front.to(lift["slot_x"], time.monotonic())
        if lift["out"]:
            self._send("detach", tab=d.tab, **self._root(x, y))
        elif lift["to"] != lift["index"]:
            self._send("move", tab=d.tab, to=lift["to"])

    def cancel(self) -> None:
        if self.drag is not None or self.closing is not None:
            self.drag = self.closing = None
            self._moved()

    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if not value:
            self._set_hover(None)

    def attach(self, window) -> None:
        super().attach(window)
        # Shown again (focus came back): everything where it is now, no
        # motion for what changed while it was not shown.
        self._glides.clear()
        self._leaving.clear()
        self._drawn_ids.clear()
        self._front = None
        self._fresh = True

    def detach(self) -> None:
        self._let_go()
        self.hover = None
        super().detach()

    def _set_hover(self, index: int | None) -> None:
        if index != self.hover:
            self.hover = index
            self.invalidate()

    def _id_at(self, x: float, y: float) -> int | None:
        i = self.tab_at(x, y)
        return None if i is None else self.ids[i]

    def _still_there(self) -> None:
        """A press on a tab that is gone (or under the find line) lets go."""
        held = self.drag.tab if self.drag is not None else self.closing
        if held is not None and (held not in self.ids or self.find is not None):
            log.debug("the tab under the pointer went; its press let go")
            self._let_go()

    def _let_go(self) -> None:
        """Drop the press, and the window's hold on it."""
        if self.drag is None and self.closing is None:
            return
        self.drag = self.closing = None
        release = getattr(self.window, "release_hold", None)
        if release is not None:
            release(self)
        self._moved()

    def _send(self, name: str, **args) -> None:
        if self.send is not None:
            self.send(name, **args)

    def _root(self, x: float, y: float) -> dict:
        """A window-local point in root coordinates (qtile does not
        reparent: the window's origin is its root position)."""
        w = self.window
        if w is None or not hasattr(w, "x"):
            return {}
        return {"x": int(w.x + x), "y": int(w.y + y)}

    # ── motion ──────────────────────────────────────────────────────────
    @property
    def animation_fps(self) -> int:
        now = time.monotonic()
        if (self._due or self._leaving or (self._front is not None and self._front.moving(now))
                or any(g.moving(now) for g in self._glides.values())):
            return MOTION_FPS
        return COMET_FPS if self._comets() else 0

    def animate(self, t: float) -> None:
        self.invalidate()

    def settle(self) -> None:
        """Everything where it is going, at once (offline renders, tests)."""
        self._retarget(time.monotonic())
        for g in (*self._glides.values(), *((self._front,) if self._front else ())):
            g.settle()
        self._leaving.clear()

    def _comets(self) -> list[int]:
        """The tabs behind that load, shown."""
        if self.find is not None:
            return []
        run = range(self.first, self.first + self.count)
        return [i for i in run if i != self.current and i < len(self.loading) and self.loading[i]]

    def _retarget(self, now: float) -> None:
        """The glides sent to where the tabs are now; a tab gone from the
        list starts wiping out from where it was drawn."""
        placed, lift = self.arrangement()
        fresh, self._fresh = self._fresh, False
        # A tab closed wipes out first, and then the others close the gap:
        # sliding at once, they would pass over it.
        gone = [] if fresh else [(tid, v) for tid, v in self._drawn_ids.items()
                                  if tid not in self.ids]
        for tid, (i, title, x) in gone:
            g = self._glides.get(tid)
            self._leaving.append((now, i, title, g.at(now) if g else x))
        delay = WIPE_S if gone else 0.0
        drawn = {}
        for i, x in placed:
            tid = self.ids[i]
            g = self._glides.get(tid)
            if g is None or fresh:
                self._glides[tid] = Glide(SLIDE_S, x)
            else:
                g.to(x, now, delay)
            drawn[tid] = (i, self._label(i), x)
        for tid in [t for t in self._glides if t not in drawn]:
            del self._glides[tid]
        if lift is not None:
            drawn[self.ids[lift["index"]]] = (lift["index"], self._label(lift["index"]),
                                              lift["x"])
        self._drawn_ids = drawn
        self._leaving = [w for w in self._leaving if now - w[0] < WIPE_S]
        target = 0.0 if self.find is not None else (
            (self.current - self.first) * self.pitch
            if self.first <= self.current < self.first + self.count else None)
        if target is not None:
            if self._front is None or fresh:
                self._front = Glide(SWITCH_S, target)
            else:
                self._front.to(target, now, delay)
        self._due = False

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        now = time.monotonic()
        self._retarget(now)
        r = self.rect
        band = Band.of(r)
        canvas.save()
        canvas.clipRect(r)
        if self.find_drawn:
            x = r.left() + (self._front.at(now) if self._front is not None else 0.0)
            paint_find(canvas, x, self.find_drawn, band)
            canvas.restore()
            return
        placed, lift = self.arrangement()
        w = self.slot_w

        def at(i: int, x: float) -> float:
            g = self._glides.get(self.ids[i])
            return r.left() + (g.at(now) if g is not None else x)

        for i, x in placed:
            mark = self._mark(i)
            paint_box(canvas, at(i, x), w, band, color=mark or OUTLINE,
                      alpha=OUTLINE_HOVER_A if i == self.hover and lift is None
                      else OUTLINE_DIM_A)
        if lift is None and self._front is not None and \
                self.first <= self.current < self.first + self.count:
            fx = r.left() + self._front.at(now)
            paint_current(canvas, fx, w, band)
            if self.progress is not None:
                paint_trace(canvas, fx, w, band, max(0.0, min(1.0, self.progress / 100)))
        phase = (now / COMET_S) % 1.0
        for i in self._comets():
            x = dict(placed).get(i)
            if x is not None:
                paint_comet(canvas, at(i, x), w, band, phase)
        for i, x in placed:
            paint_cell(canvas, at(i, x), band.cy, str(i + 1), self._label(i),
                       current=i == self.current and lift is None,
                       hovered=i == self.hover, mark=self._mark(i))
        for since, i, title, x in self._leaving:
            paint_wipe(canvas, r.left() + x, w, band, str(i + 1), title,
                       min(1.0, (now - since) / WIPE_S))
        if lift is not None:
            i = lift["index"]
            paint_lifted(canvas, r.left() + lift["x"], w, band, str(i + 1), self._label(i),
                         self._mark(i), out=lift["out"], right=r.right())
            if not lift["out"]:
                paint_marker(canvas, r.left() + max(MARKER_W / 2,
                                                     lift["slot_x"] - SLOT_GAP / 2), r)
        canvas.restore()

    def layout(self) -> list[dict]:
        """Where each tab (or piece of the find line) is, for tests to find
        in pixels: a tab's slot, its number's spot, its title as drawn."""
        r = self.rect
        if self.find_drawn:
            return [{"role": role, "text": piece, "x": r.left() + x, "w": w}
                    for role, piece, x, w in self.find_drawn]
        placed, _lift = self.arrangement()
        cy = Band.of(r).cy
        out = []
        for i, x in placed:
            left = r.left() + x
            spot = number_spot(left, cy)
            title = self._label(i)
            out.append({
                "index": i, "tab": self.ids[i] if i < len(self.ids) else None,
                "text": f"{i + 1} {title}" if title else str(i + 1), "title": title,
                "x": left + SLOT_PAD + NUMBER_SPOT + NUMBER_GAP, "w": text.measure(title, SIZE),
                "slot_x": left, "slot_w": self.slot_w,
                "number_x": spot.left(), "number_w": spot.width(),
                "current": i == self.current, "hovered": i == self.hover,
                "loading": i < len(self.loading) and self.loading[i],
                "agent": self.agents[i] if i < len(self.agents) else None})
        return out

    def lift_layout(self) -> dict | None:
        """The lifted tab, window x, for tests; None when none is."""
        _placed, lift = self.arrangement()
        if lift is None:
            return None
        r = self.rect
        return {**lift, "x": r.left() + lift["x"], "slot_x": r.left() + lift["slot_x"]}


class Host(Widget):
    """The current page's host, plain, at the right; before it a bookmark
    when the page is one, and an open padlock when it came over an
    insecure connection."""

    def __init__(self) -> None:
        super().__init__()
        self.host = ""
        self.secure = True
        self.bookmarked = False
        # host as drawn, its x, the padlock's width, the bookmark's x and width
        self.drawn = ("", 0.0, 0.0, 0.0, 0.0)

    def set(self, host: str, secure: bool, bookmarked: bool = False) -> None:
        if (host, secure, bookmarked) != (self.host, self.secure, self.bookmarked):
            self.host, self.secure, self.bookmarked = host, secure, bookmarked
            self.invalidate(layout=True)

    def _marks(self) -> tuple[str, str]:
        return (BOOKMARK_MARK + " " if self.bookmarked else "",
                "" if self.secure else INSECURE_MARK + " ")

    def measure(self, avail: Size) -> Size:
        width = text.measure("".join(self._marks()) + self.host, SIZE)
        return Size(min(width, avail.width * HOST_CAP), text.line_height(SIZE))

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        x = r.left()
        bookmark, padlock = self._marks()
        book_w = text.measure(bookmark, SIZE) if bookmark else 0.0
        if bookmark:
            text.draw(canvas, bookmark, x, base, SIZE, theme.color(BOOKMARK_FG))
        lock_w = text.measure(padlock, SIZE) if padlock else 0.0
        if padlock:
            text.draw(canvas, padlock, x + book_w, base, SIZE, theme.color(INSECURE_FG))
        marks_w = book_w + lock_w
        value = text.ellipsize(self.host, SIZE, max(0.0, r.width() - marks_w) + 0.5)
        if value and text.measure(value, SIZE) <= r.width() - marks_w + 0.5:
            text.draw(canvas, value, x + marks_w, base, SIZE, theme.color(HOST_FG))
        self.drawn = (value, x + marks_w, lock_w, x, book_w)

    def layout(self) -> dict:
        """Where the host was drawn, for tests to find in pixels."""
        value, x, lock_w, book_x, book_w = self.drawn
        return {"text": value, "x": x, "w": text.measure(value, SIZE) if value else 0.0,
                "mark_w": lock_w, "secure": self.secure, "bookmarked": self.bookmarked,
                "bookmark_x": book_x, "bookmark_w": book_w}


class Owner(Widget):
    """Whose window it is, before its tabs: the agent's name in its
    voice's colour, for the agent's own window."""

    def __init__(self, voice: str) -> None:
        super().__init__()
        self.label = protocol.voice_name(voice)
        self.color = protocol.voice_color(voice)

    def measure(self, avail: Size) -> Size:
        return Size(text.measure(self.label, SIZE), text.line_height(SIZE))

    def paint(self, canvas: skia.Canvas) -> None:
        text.draw(canvas, self.label, self.rect.left(), text.baseline_in(self.rect, SIZE), SIZE,
                  theme.color(self.color))


class FluxContent(Widget):
    """Tabs and host in a row: what the top bar shows for one browser
    window; for the agent's own window (`owner`, its voice), its name
    first. The current tab's load is drawn by the tabs, round its box."""

    def __init__(self, owner: str | None = None) -> None:
        super().__init__()
        self.tabs = TabList()
        self.host = Host()
        self.owner = Owner(owner) if owner else None
        lead = [self.owner, Spacer(size=theme.SPACING_MD)] if self.owner else []
        self.row = Row([*lead, self.tabs, Spacer(size=theme.SPACING_MD), self.host],
                       padding=PAD, align=Align.STRETCH)
        self.progress: int | None = None    # the current tab's load; None when idle

    def set_progress(self, value: int | None) -> None:
        self.progress = value
        self.tabs.set_progress(value)

    def children(self):
        return (self.row,)

    def measure(self, avail: Size) -> Size:
        self.row.measure(avail)
        return avail

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self.row.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        self.row.paint(canvas)


# ── one browser window ──────────────────────────────────────────────────

class FluxWindow(AppWindow):
    """One browser window: what it shows in the top bar, and its
    launcher."""

    kind = protocol.KIND

    def __init__(self, link, wid: int, announce: dict) -> None:
        super().__init__(link, wid, announce)
        single = announce.get(protocol.SINGLE)
        if not isinstance(single, bool):
            log.debug("browser 0x%x announced no `single`; taken as one (its keys left to it)",
                      wid)
            single = True
        self.single = single
        # Only a main window's keys are the shell's (protocol.py).
        self.keys = {} if single else dict(protocol.KEYS)
        agent = announce.get(protocol.AGENT)
        self.agent = protocol.voice_id(agent) if isinstance(agent, str) and agent else None
        self.content = FluxContent(owner=self.agent)
        self.content.tabs.send = self._tab_command
        self.request = None             # the launcher mode last opened for this window
        self.state = protocol.state([], 0, 100, True, url="", bookmarked=False, find=None)

    # ── messages from the browser ───────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        if self.closed:
            return
        kind = msg[protocol.TYPE]
        if kind == protocol.STATE:
            problems = protocol.state_problems(msg)
            if problems:
                log.warning("state from browser 0x%x ignored: %s", self.wid,
                            "; ".join(problems))
                return
            state = {k: msg[k] for k in (protocol.TYPE, *protocol.STATE_KEYS)}
            state[protocol.AGENT] = protocol.agents_of(msg)
            self._show(state)
        elif kind == protocol.DOWNLOAD:
            log.debug("download in browser 0x%x: %s %s %s", self.wid, msg.get("name"),
                      msg.get("state"), msg.get("percent"))
        elif kind == protocol.LAUNCHER:
            action = msg.get("action")
            if action in protocol.ACTIONS:
                self._open(action, pressed=False)
            else:
                log.debug("launcher for %r not understood", action)
        else:
            log.debug("browser 0x%x: message %r not understood", self.wid, kind)

    def _show(self, state: dict) -> None:
        if state == self.state:
            return
        self.state = state
        tabs, current = state[protocol.TABS], state[protocol.CURRENT]
        self.content.tabs.set_tabs([str(t[protocol.TITLE]) for t in tabs], current,
                                   protocol.agents_of(state),
                                   [t[protocol.TAB_ID] for t in tabs],
                                   [bool(t[protocol.LOADING]) for t in tabs])
        here = tabs[current] if tabs else {protocol.HOST: "", protocol.LOADING: False}
        self.content.host.set(str(here[protocol.HOST]), bool(state[protocol.SECURE]),
                              bool(state[protocol.BOOKMARKED]))
        self.content.tabs.set_find(state[protocol.FIND])
        progress = state[protocol.PROGRESS]
        self.content.set_progress(progress if here[protocol.LOADING] and progress < 100
                                  else None)

    # ── to the browser ──────────────────────────────────────────────────
    def command(self, name: str, **args) -> None:
        """Ask the browser window to do something: protocol.COMMANDS."""
        self.send(protocol.command(name, **args))

    # ── the launcher, for the browser ───────────────────────────────────
    def on_key(self, name: str, key: KeyEvent) -> None:
        """Ctrl+L, Ctrl+T or Ctrl+M pressed in the window; the same again
        while its launcher is up (typed before the launcher had the
        keyboard) is a key for the launcher like any other."""
        if self._showing():
            self.on_typed(key)
        else:
            self._open(name, pressed=True)

    def on_typed(self, key: KeyEvent) -> None:
        """A key typed after the chord, before the launcher had the
        keyboard: into its query, in order."""
        mode = self.request
        if mode is None or not mode.type_ahead([key]):
            log.debug("a key typed into browser 0x%x with no launcher for it; dropped",
                      self.wid)

    def _showing(self) -> bool:
        mode = self.request
        host = getattr(mode, "host", None)
        return host is not None and host.shows(mode)

    def _open(self, action: str, *, pressed: bool) -> None:
        """The launcher for `action`; `pressed`: opened for its key, the
        keyboard the shell's since the press."""
        if pressed:
            # First: a passive grab ends with the chord's key, and every
            # key typed after that, until the launcher takes the keyboard,
            # would go to the page.
            self.service.hold_keyboard(self)
        # From here on every way out must give the keyboard back unless
        # the launcher took it: an exception left unhandled here would
        # leave the whole desktop's keyboard with the shell.
        mode = None
        try:
            mode = self._mode(action)
            self.request = mode
            shown = self._show_launcher(mode, self.placement())
        except Exception:
            log.exception("the launcher failed to open for browser 0x%x", self.wid)
            shown = False
        if not shown:
            self.request = None
            if pressed:
                self.service.release_keyboard()     # before anything else can fail
            if mode is not None:
                mode.close()
            log.info("no launcher to show %s in for browser 0x%x; it asks itself",
                     action, self.wid)
            self.command(protocol.PROMPT, action=action)
            return
        if pressed and not self._launcher_has_keyboard(mode):
            # Never left holding the keyboard with nothing to type into.
            log.warning("the launcher did not take the keyboard; keys go to browser "
                        "0x%x again", self.wid)
            self.service.release_keyboard()

    def _mode(self, action: str):
        if action == "actions":
            return self.actions()
        where = "tab" if action == "new_tab" else "here"
        url = str(self.state.get(protocol.URL) or "")
        text = url if where == "here" and url != "about:blank" else ""
        return AddressMode(text=text, on_load=partial(self._load, where),
                           on_remove=self._remove_bookmark)

    def _show_launcher(self, mode, placement) -> bool:
        """How a mode is shown: the service's `show_launcher` when it has
        one (tests put a recorder there), else the daemon's launcher."""
        show = getattr(self.service, "show_launcher", None) or show_launcher
        return show(mode, placement)

    @staticmethod
    def _launcher_has_keyboard(mode) -> bool:
        window = getattr(getattr(mode, "host", None), "window", None)
        return bool(getattr(window, "keyboard_grabbed", False))

    def actions(self) -> Choices:
        """The window's actions, for its current page; first, while the
        user has taken a tab over from the agent, handing it back."""
        url = str(self.state.get(protocol.URL) or "")
        marked = bool(self.state.get(protocol.BOOKMARKED))
        page = places.recordable(url)
        host = places.host_of(url)
        tabs = self.state.get(protocol.TABS) or []
        resume = [Choice(f"Let {protocol.voice_name(a.get('voice'))} drive again",
                         partial(self._resume, i), glyph=RESUME_MARK,
                         detail=" · ".join(filter(None, (f"tab {i + 1}",
                                                       str(tabs[i].get(protocol.HOST) or "")
                                                       if i < len(tabs) else ""))))
                  for i, a in enumerate(protocol.agents_of(self.state))
                  if a and a.get("state") == "paused"]
        return Choices([
            *resume,
            Choice("Add bookmark", partial(self._bookmark, "add"),
                   enabled=page and not marked, detail=host, glyph=BOOKMARK_MARK),
            Choice("Remove bookmark", partial(self._bookmark, "remove"),
                   enabled=page and marked, detail=host, glyph=BOOKMARK_MARK),
        ], placeholder="actions")

    def _load(self, where: str, url: str) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; %s not opened", url)
            return
        self.command("load", url=url, where=where)

    def _bookmark(self, op: str) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; no bookmark %s", op)
            return
        self.command("bookmark", op=op)

    def _tab_command(self, name: str, **args) -> None:
        """The mouse on the top bar's tabs (TabList): `tab`, `close`,
        `move` or `detach` for the browser."""
        if self.closed:
            log.info("the browser window is gone; %s not sent", name)
            return
        log.debug("browser 0x%x: %s %s", self.wid, name, args)
        self.command(name, **args)

    def _resume(self, index: int) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; tab %d not handed back", index + 1)
            return
        self.command(protocol.RESUME, index=index)

    def _remove_bookmark(self, url: str) -> bool:
        """Shift+Delete on a bookmark in the address list: the browser
        that asked takes it out of the file. False when it is gone."""
        if self.closed:
            log.info("the browser window that asked is gone; bookmark %s not removed", url)
            return False
        self.command("bookmark", op="remove", url=url)
        return True

    def placement(self) -> Placement | None:
        """Where the launcher drops from: centred on the browser's window,
        its top at the top bar's bottom edge when the bar is over that
        window, else at the window's own top edge. qtile does not
        reparent, so the window's root position is one
        TranslateCoordinates away; None when it is gone (or no X window:
        an offscreen test browser)."""
        d = self.service.display
        if d is None:
            return None
        try:
            at = d.conn.core.TranslateCoordinates(self.wid, d.root, 0, 0).reply()
            width = d.conn.core.GetGeometry(self.wid).reply().width
        except (xp.WindowError, xp.DrawableError):
            return None
        finally:
            d.wake()
        x = at.dst_x + width // 2
        bar = self.service.bar_bottom(x)
        return Placement(x, at.dst_y if bar is None else bar)

    # ── teardown ────────────────────────────────────────────────────────
    def close(self) -> None:
        if self.closed:
            return
        super().close()
        if self.request is not None:
            self.request.cancel()       # its launcher closes, if it still shows it
            self.request = None


def show_launcher(mode, placement) -> bool:
    """The daemon's launcher, on `mode`, at `placement`."""
    from ..core.daemon import get_daemon
    from ..widgets.launcher import open_launcher
    return open_launcher(get_daemon(), mode, placement=placement)
