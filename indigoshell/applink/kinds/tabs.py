"""The `tabs` kind: an app's tabs from the left of its section, a box
each, one look for every app (PROTOCOL.md, section 2).

Each tab is a box of its own in a slot of the same width (SLOT_W, all
narrower alike when they do not fit, down to SLOT_MIN, then the run
around the current one), a few pixels apart, outlined with the agent
chip's bevel at the top right: its image in a spot of its own (the
`image` the app sent for it, a page's icon, glowing cyan: glow.py; its
`letter` in the same glow while it has none) in small corner brackets,
and its title, shortened with an ellipsis to the box, in the muted
tone, the outline dim, the glow and brackets a step down; the current
tab's outline bright with a faint light inside, its title in the strong
colour, its image and brackets at full. A tab's `badge` sits at its box's right end. Loading
goes round a box's own outline: a tab behind that is `busy` runs a comet
round its box; the current one's `progress` lights its outline round
from the bottom-left corner, the rest of it dimmed. While the agent
drives a tab (`agent`), its image glows in her colour (core/persona.py)
and its corner brackets and its box's outline are drawn in it; while
the user has taken it over, in that colour dimmed a third of the way to
the bar's background. The keys that pick a tab by its place (Flux's
Alt+1..9) are the app's; the bar shows no numbers.

The mouse (the bar's window holds the press: Widget.press). A left or
right press on a tab other than the current one sends `select`; a middle
click (let go over the same tab) `close` when the app `can` close.
Pressed and moved DRAG_START_PX, a tab the app can `move` lifts and
follows the pointer along the bar, the others closed up and a marker
between two of them where it would land, and letting go over the bar
sends `move` with the tab it lands `before` (null: the end); dragged
more than TEAR_OUT_PX below the bar and let go, one the app can `detach`
is torn out (`detach`; never a window's only tab). Escape while dragging
puts it back (the window takes the keyboard for it, when it can). Every
event names the tab by its id, so tabs coming or going meanwhile change
nothing of what the click meant; a tab dragged that goes away lets go of
the press. A tab under the pointer has its outline a step brighter and
its title brighter; a dragged tab lifts a little over the others; past
the tear-out line it dims and says NEW WINDOW.

Motion (`animation_fps`, nothing while nothing moves): switching slides
the current tab's light to the new box (SWITCH_S); a tab gone wipes out
(WIPE_S), then the others slide to close the gap (SLIDE_S), as they
slide closed up under a dragged tab. Positions are glides by tab id
(`Glide`), retargeted at each paint from where the tabs are now.
"""

import logging
import math
import time
from dataclasses import dataclass

import skia

from ... import text
from ...widgets.base import Size, Widget
from . import Item
from .look import (COMET_FPS, COMET_S, MARKER_W, MOTION_FPS, OUTLINE, OUTLINE_DIM_A,
                   OUTLINE_HOVER_A, SIZE, SLIDE_S, SLOT_GAP, SLOT_MIN, SLOT_PAD, SLOT_W, SPOT,
                   SPOT_GAP, SWITCH_S, WIPE_S, Band, Glide, agent_color, badge_room, color_of,
                   glyph, paint_box, paint_cell, paint_comet, paint_current, paint_lifted,
                   paint_marker, paint_trace, paint_wipe, spot_rect, tab_glow, title_in)

log = logging.getLogger(__name__)

DRAG_START_PX = 6                   # a press moved this far lifts the tab
TEAR_OUT_PX = 40                    # let go this far below the bar: a window of its own

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


@dataclass
class TabDrag:
    """A tab pressed with button 1 or 3, by its id, and where the
    pointer has been since (window-local)."""
    tab: object
    button: int
    x0: float
    y0: float
    grab: float             # the press's x in the tab's slot
    x: float
    y: float
    lifted: bool = False    # moved DRAG_START_PX: the tab follows the pointer


class TabsItem(Item):
    """An app's tabs, a box each (the module docstring)."""

    kind = "tabs"

    def __init__(self, item_id) -> None:
        super().__init__(item_id, flex=1)
        self.titles: list[str] = []
        self.ids: list = []
        self.current = -1                       # index of the current tab; -1 for none
        self.agents: list[dict | None] = []
        self.busy: list[bool] = []
        self.badges: list[tuple[str, str] | None] = []
        self.images: list = []                  # each tab's image id (None: none)
        self.letters: list[str | None] = []     # ... and its letter, while it has none
        self.progress: float | None = None      # the current tab's load, while it shows one
        self.can: frozenset = frozenset()
        # The slots shown: the first tab's index, how many, their width;
        # each one's label.
        self.first = self.count = self.slot_w = 0
        self.labels: list[str] = []
        self.hover: int | None = None           # the tab under the pointer
        self.drag: TabDrag | None = None        # a left or right press on a tab
        self.closing = None                     # the tab a middle press is on
        # Motion: each tab's slot x as drawn (by id), the current tab's
        # light's x, the tabs gone and wiping out (since, title, x, image
        # glow: the app's image may be gone with the tab), what the last
        # paint drew (id -> (index, title, x, glow)), and whether
        # something moved since (the next frame retargets).
        self._glides: dict = {}
        self._front: Glide | None = None
        self._leaving: list[tuple[float, str, float, object]] = []
        self._drawn_ids: dict = {}
        self._due = False
        self._fresh = True                      # nothing drawn yet: no motion into place

    # ── the app's item ──────────────────────────────────────────────────
    def set(self, spec: dict) -> None:
        if spec == self.spec:
            return
        self.spec = spec
        tabs = [t for t in spec.get("tabs") or () if isinstance(t, dict)]
        titles = [str(t.get("title") or "") for t in tabs]
        ids = [t.get("id") for t in tabs]
        current = ids.index(spec.get("current")) if spec.get("current") in ids else -1
        agents = [t.get("agent") if isinstance(t.get("agent"), dict) else None for t in tabs]
        busy = [bool(t.get("busy")) for t in tabs]
        badges = [(glyph(t["badge"].get("icon")), color_of(t["badge"].get("color")))
                  if isinstance(t.get("badge"), dict) else None for t in tabs]
        images = [t.get("image") for t in tabs]
        letters = [t["letter"] if isinstance(t.get("letter"), str) else None for t in tabs]
        here = tabs[current] if current >= 0 else {}
        progress = here.get("progress")
        progress = float(progress) if isinstance(progress, (int, float)) \
            and not isinstance(progress, bool) and progress < 100 else None
        self.can = frozenset(c for c in spec.get("can") or () if isinstance(c, str))
        if titles != self.titles or current != self.current or ids != self.ids \
                or badges != self.badges:
            self.titles, self.current, self.ids, self.badges = titles, current, ids, badges
            self._moved(layout=True)
        elif (agents != self.agents or busy != self.busy or images != self.images
              or letters != self.letters):
            self.invalidate()
        self.agents, self.busy, self.images, self.letters = agents, busy, images, letters
        if progress != self.progress:
            self.progress = progress
            self.invalidate()
        self._still_there()

    def _moved(self, layout: bool = False) -> None:
        """Something may have moved: the next frame retargets the glides."""
        self._due = True
        self.invalidate(layout=layout)

    def natural(self) -> float:
        return 0.0                      # it takes what the run's other items leave

    def measure(self, avail: Size) -> Size:
        return Size(0.0, text.line_height(SIZE))

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self.first, self.count, self.slot_w = fit_slots(len(self.titles), max(0, self.current),
                                                        rect.width())
        room = self.slot_w - 2 * SLOT_PAD - SPOT - SPOT_GAP
        self.labels = [title_in(self.titles[i], room - badge_room(self.badges[i]))
                       for i in range(self.first, self.first + self.count)]

    # ── geometry ────────────────────────────────────────────────────────
    @property
    def pitch(self) -> int:
        return self.slot_w + SLOT_GAP

    def tab_at(self, x: float, y: float | None = None) -> int | None:
        """The tab whose slot holds window x (and y, when given); None
        between none."""
        if not self.count:
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
        return ("detach" in self.can and len(self.titles) > 1
                and y > self.rect.bottom() + TEAR_OUT_PX)

    def _label(self, i: int) -> str:
        k = i - self.first
        return self.labels[k] if 0 <= k < len(self.labels) else ""

    def _mark(self, i: int) -> str | None:
        return agent_color(self.agents[i]) if i < len(self.agents) else None

    def _badge(self, i: int):
        return self.badges[i] if i < len(self.badges) else None

    def _glow(self, i: int):
        """Tab i's image (or letter) glowing in its colour (look.tab_glow)."""
        image_id = self.images[i] if i < len(self.images) else None
        image = self.image_of(image_id) if image_id is not None and self.image_of else None
        return tab_glow(image, self.letters[i] if i < len(self.letters) else None,
                        self._mark(i))

    # ── the mouse ───────────────────────────────────────────────────────
    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.emit is not None and self.tab_at(x, y) is not None else None

    def press(self, button: int, x: float, y: float) -> bool:
        i = self.tab_at(x, y)
        if i is None or self.emit is None or button not in (1, 2, 3):
            return False
        tab = self.ids[i]
        if button == 2:
            if "close" not in self.can:
                return False
            self.closing = tab
            return True
        if i != self.current:
            self._emit("select", tab=tab)
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
            if not self.can & {"move", "detach"} or \
                    math.hypot(x - d.x0, y - d.y0) < DRAG_START_PX:
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
                self._emit("close", tab=closing)
            return
        if d is None or button != d.button:
            return
        d.x, d.y = x, y
        _placed, lift = self.arrangement()
        self.drag = None
        if not d.lifted:
            return                      # a click: selected at the press
        self._moved()
        if lift is None:
            return                      # its tab went from the list meanwhile
        # It goes down where it was let go: torn out, it wipes out from
        # there once the app says it went; moved, it slides into its
        # place, lit.
        moves = "move" in self.can and not lift["out"]
        self._glides[d.tab] = Glide(SLIDE_S, lift["x"])
        if moves:
            self._front = Glide(SWITCH_S, lift["x"])
            self._glides[d.tab].to(lift["slot_x"], time.monotonic())
            self._front.to(lift["slot_x"], time.monotonic())
        if lift["out"]:
            self._emit("detach", tab=d.tab)
        elif moves and lift["to"] != lift["index"]:
            others = [t for t in self.ids if t != d.tab]
            to = lift["to"]
            self._emit("move", tab=d.tab, before=others[to] if to < len(others) else None)

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

    def _id_at(self, x: float, y: float):
        i = self.tab_at(x, y)
        return None if i is None else self.ids[i]

    def _still_there(self) -> None:
        """A press on a tab that is gone lets go."""
        held = self.drag.tab if self.drag is not None else self.closing
        if held is not None and held not in self.ids:
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
        """The tabs behind that are busy, shown."""
        run = range(self.first, self.first + self.count)
        return [i for i in run if i != self.current and i < len(self.busy) and self.busy[i]]

    def _retarget(self, now: float) -> None:
        """The glides sent to where the tabs are now; a tab gone from the
        list starts wiping out from where it was drawn."""
        placed, lift = self.arrangement()
        fresh, self._fresh = self._fresh, False
        # A tab closed wipes out first, and then the others close the gap:
        # sliding at once, they would pass over it.
        gone = [] if fresh else [(tid, v) for tid, v in self._drawn_ids.items()
                                  if tid not in self.ids]
        for tid, (_i, title, x, image) in gone:
            g = self._glides.get(tid)
            self._leaving.append((now, title, g.at(now) if g else x, image))
        delay = WIPE_S if gone else 0.0
        drawn = {}
        for i, x in placed:
            tid = self.ids[i]
            g = self._glides.get(tid)
            if g is None or fresh:
                self._glides[tid] = Glide(SLIDE_S, x)
            else:
                g.to(x, now, delay)
            drawn[tid] = (i, self._label(i), x, self._glow(i))
        for tid in [t for t in self._glides if t not in drawn]:
            del self._glides[tid]
        if lift is not None:
            i = lift["index"]
            drawn[self.ids[i]] = (i, self._label(i), lift["x"], self._glow(i))
        self._drawn_ids = drawn
        self._leaving = [w for w in self._leaving if now - w[0] < WIPE_S]
        target = ((self.current - self.first) * self.pitch
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
        drawn = self._drawn_ids
        for i, x in placed:
            paint_cell(canvas, at(i, x), band.cy, self._label(i),
                       current=i == self.current and lift is None, hovered=i == self.hover,
                       mark=self._mark(i), image=drawn[self.ids[i]][3], badge=self._badge(i),
                       w=w)
        for since, title, x, image in self._leaving:
            paint_wipe(canvas, r.left() + x, w, band, title, image,
                       min(1.0, (now - since) / WIPE_S))
        if lift is not None:
            i = lift["index"]
            paint_lifted(canvas, r.left() + lift["x"], w, band, self._label(i), self._mark(i),
                         drawn[self.ids[i]][3], out=lift["out"], right=r.right(),
                         badge=self._badge(i))
            if not lift["out"]:
                paint_marker(canvas, r.left() + max(MARKER_W / 2,
                                                     lift["slot_x"] - SLOT_GAP / 2), r)
        canvas.restore()

    def layout(self) -> list[dict]:
        """Where each tab is, for tests to find in pixels: a tab's slot,
        its image's spot (and what shows there: "image", "letter" or
        None), its title as drawn."""
        r = self.rect
        placed, _lift = self.arrangement()
        cy = Band.of(r).cy
        out = []
        for i, x in placed:
            left = r.left() + x
            spot = spot_rect(left, cy)
            title = self._label(i)
            image_id = self.images[i] if i < len(self.images) else None
            shows = ("image" if image_id is not None and self.image_of
                     and self.image_of(image_id) is not None
                     else "letter" if i < len(self.letters) and self.letters[i] else None)
            out.append({
                "index": i, "tab": self.ids[i] if i < len(self.ids) else None,
                "text": title, "title": title,
                "x": left + SLOT_PAD + SPOT + SPOT_GAP, "w": text.measure(title, SIZE),
                "slot_x": left, "slot_w": self.slot_w,
                "spot_x": spot.left(), "spot_y": spot.top(), "spot_w": spot.width(),
                "shows": shows, "image": image_id,
                "current": i == self.current, "hovered": i == self.hover,
                "busy": i < len(self.busy) and self.busy[i],
                "agent": self.agents[i] if i < len(self.agents) else None,
                "badge": self._badge(i)})
        return out

    def lift_layout(self) -> dict | None:
        """The lifted tab, window x, for tests; None when none is."""
        _placed, lift = self.arrangement()
        if lift is None:
            return None
        r = self.rect
        return {**lift, "x": r.left() + lift["x"], "slot_x": r.left() + lift["slot_x"]}
