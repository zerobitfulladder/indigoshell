"""An app window's section of the top bar: `BarView`, what the app's
`bar` says, drawn (PROTOCOL.md, sections 2 and 6).

The view keeps one widget per item, by id (applink/kinds): an item that
keeps its id and kind keeps its widget, with its hover, a drag in
progress and its motion; one that changes kind is made anew, and one
gone is let go. `start` runs from the section's left, `end` against its
right; between two items of a run the bar's spacing. `end` takes at most
a third of the section (END_SHARE), its leftmost text giving way first;
`start` the rest, a `tabs` item taking what its run's other items leave.
All of it is clipped to the section.

While one of the window's inputs is open the view shows its box
(`box`, kinds/input.py) in place of the items, the whole section's
width; the items stay as they are, hidden, and come back as they were.

What the user does reaches the app as events naming the item (`emit`,
the AppWindow's), or opens one of the bar's inputs or menus (`on_open`,
the item that opens it with it, for a menu dropping under it). Items
lit while what they open shows: `set_lit`.
"""

import logging

import skia

from ..widgets.base import Size, Widget
from . import kinds
from .kinds.look import END_SHARE, GAP

log = logging.getLogger(__name__)


class BarView(Widget):
    """An app window's section: its bar's items, or its open input."""

    def __init__(self, emit=None, on_open=None) -> None:
        super().__init__(flex=1)
        self.emit = emit                # emit(event dict): to the app
        self.on_open = on_open          # on_open(what id, item): an item opens it
        self.bar: dict | None = None
        self.items: dict = {}           # id -> kinds.Item
        self.start: list = []
        self.end: list = []
        self.box: Widget | None = None  # the open input's box, shown instead
        self._unknown: set[str] = set()

    # ── the app's bar ───────────────────────────────────────────────────
    def set_bar(self, bar: dict) -> None:
        """Show `bar` (checked already: applink/bar.py), diffing by id."""
        if bar == self.bar:
            return
        self.bar = bar
        old, self.items = self.items, {}
        runs = []
        for run in ("start", "end"):
            widgets = []
            for spec in bar.get(run) or ():
                kind, item_id = spec.get("kind"), spec.get("id")
                widget = old.pop(item_id, None)
                if widget is not None and widget.kind != kind:
                    self._drop(widget)
                    widget = None
                if widget is None:
                    widget = kinds.make(kind, item_id)
                    if widget is None:
                        if kind not in self._unknown:
                            self._unknown.add(kind)
                            log.debug("an item of kind %r: not one the shell knows; skipped", kind)
                        continue
                    widget.emit = self._emitter(item_id)
                    widget.open = self._open
                    if self.window is not None and self.box is None:
                        widget._parent = self
                        widget.attach(self.window)
                widget.set(spec)
                self.items[item_id] = widget
                widgets.append(widget)
            runs.append(widgets)
        for widget in old.values():
            self._drop(widget)
        self.start, self.end = runs
        self.invalidate(layout=True)

    def _emitter(self, item_id):
        def emit(type_: str, **fields) -> None:
            if self.emit is not None:
                self.emit({"type": type_, "item": item_id, **fields})
        return emit

    def _open(self, what, item) -> None:
        if self.on_open is not None:
            self.on_open(what, item)

    def _drop(self, widget) -> None:
        if widget.window is not None:
            try:
                widget.detach()
            except Exception:
                log.exception("letting go of an item failed")
            for w in widget.walk():
                w.window = None
        widget._parent = None

    def item(self, item_id):
        return self.items.get(item_id)

    def set_lit(self, ids) -> None:
        """The items whose input or menu shows (a button lit for it)."""
        for item_id, widget in self.items.items():
            widget.set_lit(item_id in ids)

    # ── the open input ──────────────────────────────────────────────────
    def show_box(self, box: Widget | None) -> None:
        """The section as `box` (None: the items again)."""
        if box is self.box:
            return
        if self.box is not None:
            self._drop(self.box)
        self.box = box
        if self.window is not None:
            if box is not None:
                for widget in self.items.values():
                    widget.set_hovered(False)
                box._parent = self
                box.attach(self.window)
            else:
                for widget in self.items.values():
                    widget._parent = self
                    widget.attach(self.window)
        self.invalidate(layout=True)

    # ── tree ────────────────────────────────────────────────────────────
    def children(self):
        if self.box is not None:
            return (self.box,)
        return (*self.start, *self.end)

    def detach(self) -> None:
        for widget in (*self.items.values(), *((self.box,) if self.box else ())):
            try:
                widget.detach()
            except Exception:
                log.exception("detaching a bar item failed")
            for w in widget.walk():
                w.window = None

    # ── layout ──────────────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        return Size(avail.width, avail.height)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.box is not None:
            self.box.measure(Size(rect.width(), rect.height()))
            self.box.arrange(rect)
            return
        width, h = rect.width(), rect.height()
        # The end run: natural widths, its leftmost text giving way first
        # when they pass END_SHARE of the section.
        ends = [w.natural() for w in self.end]
        cap = width * END_SHARE
        over = sum(ends) + GAP * max(0, len(ends) - 1) - cap
        for i in range(len(ends)):
            if over <= 0:
                break
            cut = min(over, ends[i])
            ends[i] -= cut
            over -= cut
        x = rect.right()
        for widget, w in reversed(list(zip(self.end, ends))):
            widget.measure(Size(w, h))
            widget.arrange(skia.Rect.MakeLTRB(x - w, rect.top(), x, rect.bottom()))
            x -= w + GAP
        end_left = x + GAP if self.end else rect.right()
        # The start run: its tabs take what the others leave.
        right = end_left - (GAP if self.end else 0)
        fixed = [0.0 if w.kind == "tabs" else w.natural() for w in self.start]
        flexes = sum(1 for w in self.start if w.kind == "tabs")
        gaps = GAP * max(0, len(self.start) - 1)
        spare = max(0.0, right - rect.left() - sum(fixed) - gaps)
        x = rect.left()
        for widget, w in zip(self.start, fixed):
            if widget.kind == "tabs":
                w = spare / flexes
            w = max(0.0, min(w, right - x))
            widget.measure(Size(w, h))
            widget.arrange(skia.Rect.MakeLTRB(x, rect.top(), x + w, rect.bottom()))
            x += w + GAP

    def paint(self, canvas: skia.Canvas) -> None:
        canvas.save()
        canvas.clipRect(self.rect)
        for widget in self.children():
            widget.paint(canvas)
        canvas.restore()
