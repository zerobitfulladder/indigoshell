"""Flux in the shell: the top bar's content for a focused browser window,
and the launcher for its Ctrl+L, Ctrl+T and Ctrl+M.

A Flux window (flux/qt) connects to the shell's app service
(applink/service.py) and announces itself as a "flux" window; the
service gives it a `FluxWindow`, which takes the state the browser
pushes (protocol.py) into `FluxContent`. While that window has focus the
top bar's context area shows it (applink/context.py); the browser
itself draws nothing of it.

What it shows is text, in the bar's vocabulary: the window's tabs from
the left, each `N Title` in a slot of the same width (SLOT_W, all
narrower alike when they do not fit, down to SLOT_MIN, then the run
around the current one), the current one in the strong colour and the
rest muted, titles shortened with an ellipsis to their slot; at the right end of the context area the current page's host,
after an open padlock when its connection is not secure and a bookmark
when the page is one; a thin line along the bar's bottom edge, under the
context area, while the current tab loads (the bar has no rule of its
own there, so the line reads as the one moving thing on the bar). While
the browser's find is open its line takes the tabs' place:
`find  query▏  3/12`, the caret only while the query is typed, "no
matches" in the error colour.

The mouse on the tabs (TabList): a left or right press switches to the
tab, a middle click closes it, a drag along the bar moves it, a drag
off the bar's bottom edge tears it out into a window of its own; each a
command to the browser (protocol.py: `tab`, `close`, `move`, `detach`)
naming the tab by its id. The press is held by the bar's window from
button down to button up (window.py, Widget.press); for the drag it
takes the keyboard too, so that Escape can put the tab back.

The agent. While the agent drives a tab (protocol `agent`), that tab's
number is drawn in the voice's colour (protocol.VOICES); while the user
has taken it over and the agent waits, in that colour dimmed a third of
the way to the bar's background. The title keeps its own colour. The window's
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
# The bar's own vocabulary and nothing else: its text size, its strong
# neon for the tab in front and the muted tone for the rest, its plain
# foreground for the host, its accent colour for the one moving thing.
# No plate, no frame, no separators.
TAB_FG = theme.BASE_MUTED
TAB_CURRENT_FG = theme.FG_STRONG
HOST_FG = theme.FG
INSECURE_FG = theme.ERROR
INSECURE_MARK = "\uf09c"            # an open padlock (Nerd Font: fa-unlock)
BOOKMARK_FG = theme.FG
BOOKMARK_MARK = BOOKMARK            # Nerd Font: fa-bookmark
FIND_FG = theme.BASE_MUTED          # "find", the count
FIND_QUERY_FG = theme.FG_STRONG
NO_MATCHES_FG = theme.ERROR
PROGRESS_FG = theme.BAR_RULE
PROGRESS_THICK = theme.BAR_RULE_THICK
SIZE = theme.FONT_SIZE
PAD = Insets()                      # the bar's own gutter is around it already
HOST_CAP = 0.34                     # the host takes at most this share of the row
PAUSED_DIM = 0.35                   # a taken-over tab's number: the voice's colour this far
                                    # towards the bar's background (halfway is too near the
                                    # muted tabs' colour to tell apart)
RESUME_MARK = "\uf04b"              # Nerd Font: fa-play

# The tabs' slots: all the same width, SLOT_W while they fit, narrower
# alike down to SLOT_MIN, then the run of them around the current tab.
SLOT_W = 240
SLOT_MIN = 110
SLOT_GAP = 2                        # between two slots
SLOT_PAD = 12                       # inside a slot, before and after its label

# The mouse on them (TabList).
DRAG_START_PX = 6                   # a press moved this far lifts the tab
TEAR_OUT_PX = 40                    # let go this far below the bar: a window of its own
TAB_HOVER_FG = "#787090"            # a muted tab under the pointer: a step brighter
TAB_CURRENT_HOVER_FG = theme.MAGENTA_BLOOM
MARKER_FG = theme.FG_STRONG         # where a dragged tab lands
MARKER_W = 2
TEAR_MARK = "\uf08e"                # Nerd Font: fa-external-link, "a new window"
TEAR_FG = theme.CYAN_BRIGHT
TEAR_DIM = 0.5                      # the lifted tab, this far to the background


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


def slot_label(index: int, title: str, room: float,
               measure=lambda s: text.measure(s, SIZE)) -> str:
    """`N Title` cut with an ellipsis to `room` px; the number alone,
    and then nothing, when not even `N …` fits."""
    label = f"{index + 1} {title}" if title else str(index + 1)
    if measure(label) <= room:
        return label
    if measure(f"{index + 1} …") <= room:
        return text.ellipsize(label, SIZE, room + 0.5)
    return str(index + 1) if measure(str(index + 1)) <= room else ""


def fit_find(find: dict, width: float,
             measure=lambda s: text.measure(s, SIZE)) -> list[tuple[str, str, float, float]]:
    """The find line in `width` px: (role, text, x, width) for "label",
    "query" (with the caret while typing) and "count" (when there is
    one). A query too long for the room loses its start, so the end,
    where typing happens, stays in view."""
    gap = measure("  ")
    caret = protocol.FIND_CARET if find.get("typing") else ""
    count = protocol.find_count(find)
    label = protocol.FIND_LABEL
    room = width - measure(label) - gap - measure(caret) - (gap + measure(count) if count else 0)
    query = find.get("query", "")
    if measure(query) > room:
        while query and measure("…" + query) > room:
            query = query[1:]
        query = "…" + query if query else ""
    out, x = [], 0.0
    for role, piece in (("label", label), ("query", query + caret), ("count", count)):
        if not piece:
            continue
        w = measure(piece)
        out.append((role, piece, x, w))
        x += w + gap
    return out


# ── the tabs' look (placeholders until the restyle) ─────────────────────
# Small on purpose: the mouse's states drawn in today's text look, each
# in one helper the new look replaces.

def tab_color(current: bool, hovered: bool) -> str:
    """A tab's title: strong in front, muted behind; a step brighter
    under the pointer."""
    if current:
        return TAB_CURRENT_HOVER_FG if hovered else TAB_CURRENT_FG
    return TAB_HOVER_FG if hovered else TAB_FG


def paint_label(canvas: skia.Canvas, label: str, index: int, x: float, base: float,
                color: str, mark: str | None) -> None:
    """`N Title` at x; the number in `mark` (the agent's colour) when
    there is one."""
    number = str(index + 1)
    if mark is not None and label.startswith(number):
        text.draw(canvas, number, x, base, SIZE, theme.color(mark))
        rest = label[len(number):]
        if rest:
            text.draw(canvas, rest, x + text.measure(number, SIZE), base, SIZE,
                      theme.color(color))
        return
    text.draw(canvas, label, x, base, SIZE, theme.color(color))


def paint_insertion(canvas: skia.Canvas, x: float, rect: skia.Rect) -> None:
    """Where a dragged tab lands: a thin upright line at x."""
    p = skia.Paint(AntiAlias=False)
    p.setColor(theme.color(MARKER_FG))
    h = rect.height()
    canvas.drawRect(skia.Rect.MakeXYWH(round(x), rect.top() + h * 0.25, MARKER_W, h * 0.5), p)


def paint_lifted(canvas: skia.Canvas, label: str, index: int, x: float, base: float,
                 mark: str | None, out: bool) -> None:
    """The tab being dragged, at the pointer; dimmed, with a new-window
    mark after it, while letting go would tear it out."""
    color = _blend(TAB_CURRENT_FG, theme.BAR_BG, TEAR_DIM) if out else TAB_CURRENT_FG
    paint_label(canvas, label, index, x, base, color, mark)
    if out:
        text.draw(canvas, " " + TEAR_MARK, x + text.measure(label, SIZE), base, SIZE,
                  theme.color(TEAR_FG))


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
    """The window's tabs from the left of the context area, each in a
    slot of the same width (`fit_slots`); the find line in their place
    while find is open.

    The mouse (the window holds the press: Widget.press). A left or right
    press on a tab switches to it at once; a middle click closes it (let
    go over the same tab). Pressed and moved DRAG_START_PX, the tab lifts
    and follows the pointer along the bar, the others making room where
    it would land, and letting go over the bar moves it there; dragged
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

    def set_tabs(self, titles: list[str], current: int,
                 agents: list[dict | None] | None = None,
                 ids: list[int] | None = None) -> None:
        agents = list(agents) if agents is not None else [None] * len(titles)
        ids = list(ids) if ids is not None else list(range(len(titles)))
        if titles != self.titles or current != self.current or ids != self.ids:
            self.titles, self.current, self.agents, self.ids = list(titles), current, agents, ids
            self.invalidate(layout=True)
        elif agents != self.agents:
            self.agents = agents
            self.invalidate()
        self._still_there()

    def set_find(self, find: dict | None) -> None:
        if find != self.find:
            self.find = dict(find) if find else None
            self.invalidate(layout=True)
        self._still_there()

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
        room = self.slot_w - 2 * SLOT_PAD
        self.labels = [slot_label(i, self.titles[i], room)
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
        while one is lifted, the others making room where it lands (closed
        up while it would tear out), and the lifted one: {"index", "x"
        (its slot's, following the pointer), "to" (the index it lands
        at), "slot_x" (where), "out" (letting go tears it out)}."""
        run = range(self.first, self.first + self.count)
        d = self.drag
        held = self.ids.index(d.tab) if d is not None and d.tab in self.ids else None
        if d is None or not d.lifted or held not in run:
            return [(i, (i - self.first) * self.pitch) for i in run], None
        at = max(0.0, min(d.x - d.grab - self.rect.left(), (self.count - 1) * self.pitch))
        out = self._tears_out(d.y)
        k = landing(at, self.count, self.slot_w)
        others = [i for i in run if i != held]
        spots = [p for p in range(self.count) if out or p != k]
        return ([(i, p * self.pitch) for i, p in zip(others, spots)],
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
        self.invalidate()

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
        self.invalidate()
        if lift is None:
            return                      # its tab went from the list meanwhile
        if lift["out"]:
            self._send("detach", tab=d.tab, **self._root(x, y))
        elif lift["to"] != lift["index"]:
            self._send("move", tab=d.tab, to=lift["to"])

    def cancel(self) -> None:
        if self.drag is not None or self.closing is not None:
            self.drag = self.closing = None
            self.invalidate()

    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if not value:
            self._set_hover(None)

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
        self.invalidate()

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

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        canvas.save()
        canvas.clipRect(r)
        placed, lift = self.arrangement()
        for i, x in placed:
            paint_label(canvas, self._label(i), i, r.left() + x + SLOT_PAD, base,
                        tab_color(i == self.current, i == self.hover), self._mark(i))
        if lift is not None:
            if not lift["out"]:
                paint_insertion(canvas, r.left() + lift["slot_x"], r)
            i = lift["index"]
            paint_lifted(canvas, self._label(i), i, r.left() + lift["x"] + SLOT_PAD, base,
                         self._mark(i), lift["out"])
        for role, piece, x, _w in self.find_drawn:
            color = {"label": FIND_FG, "query": FIND_QUERY_FG}.get(
                role, NO_MATCHES_FG if piece == protocol.NO_MATCHES else FIND_FG)
            text.draw(canvas, piece, r.left() + x, base, SIZE, theme.color(color))
        canvas.restore()

    def layout(self) -> list[dict]:
        """Where each tab (or piece of the find line) was drawn, for tests
        to find in pixels: a tab's slot, and its label's text and place
        in it."""
        r = self.rect
        if self.find_drawn:
            return [{"role": role, "text": piece, "x": r.left() + x, "w": w}
                    for role, piece, x, w in self.find_drawn]
        placed, _lift = self.arrangement()
        return [{"index": i, "text": self._label(i), "x": r.left() + x + SLOT_PAD,
                 "w": text.measure(self._label(i), SIZE), "slot_x": r.left() + x,
                 "slot_w": self.slot_w, "current": i == self.current,
                 "hovered": i == self.hover, "tab": self.ids[i] if i < len(self.ids) else None,
                 "number_w": text.measure(str(i + 1), SIZE),
                 "agent": self.agents[i] if i < len(self.agents) else None}
                for i, x in placed]

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
    """Tabs and host in a row, and the progress line along the bottom
    edge: what the top bar shows for one browser window; for the agent's
    own window (`owner`, its voice), its name first."""

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
        if value != self.progress:
            self.progress = value
            self.invalidate()

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
        if self.progress is not None:
            r = self.rect
            line = skia.Paint(AntiAlias=False)
            line.setColor(theme.color(PROGRESS_FG))
            done = round(r.width() * self.progress / 100)
            canvas.drawRect(skia.Rect.MakeXYWH(
                r.left(), r.bottom() - PROGRESS_THICK, done, PROGRESS_THICK), line)


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
                                   [t[protocol.TAB_ID] for t in tabs])
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
