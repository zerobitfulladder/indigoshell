"""Declarative panel.

A `Panel` interprets a tree of frozen dataclasses — Label, Divider,
Action, Toggle, Submenu, Value, Meter, Row, Card, Embed — into widgets.
Selecting a `Submenu` pushes its target onto a stack; Backspace/Left/h
pops; Escape closes. The same bindings plus mouse clicks activate Action
and Toggle rows.

The split is two-layer on purpose: the DSL items are immutable
*descriptions*, and widgets are the live objects they're interpreted
into. Config authors write descriptions; only the
interpreter knows about layout and painting.

Live items (Value, Meter, and any Embed'd Widget) refresh on the panel's
own tick — pull-based, no observable machinery.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Union

import skia

from .. import shapes, theme
from .base import Insets, Size, Widget
from .label import Label as TextLabel
from .layout import Align, Box, Column, Divider as Rule, Row as HRow, Spacer
from .meters import BarMeter

log = logging.getLogger(__name__)


# ── DSL ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Label:
    text: str
    style: str = "heading"          # "heading" | "body" | "muted"


@dataclass(frozen=True)
class Divider:
    pass


@dataclass(frozen=True)
class Action:
    label: str
    on_activate: Callable[[], None]
    key: str | None = None          # single-char hotkey
    hint: str | None = None         # right-aligned annotation
    close_on_activate: bool = True


@dataclass(frozen=True)
class Toggle:
    label: str
    get: Callable[[], bool]
    set: Callable[[bool], None]
    key: str | None = None


@dataclass(frozen=True)
class Submenu:
    label: str
    target: "Screen"
    key: str | None = None


@dataclass(frozen=True)
class Value:
    get: Callable[[], str]
    color: Union[str, Callable[[], str], None] = None
    xalign: float = 0.0


@dataclass(frozen=True)
class Meter:
    get: Callable[[], float]
    max: float = 100.0
    color: Union[str, Callable[[], str]] = theme.CYAN_BRIGHT
    width: int = 180
    height: int = 8


@dataclass(frozen=True)
class Row:
    cells: list
    spacing: int = 12


@dataclass(frozen=True)
class Card:
    title: str
    items: list


@dataclass(frozen=True)
class Embed:
    widget: Widget


@dataclass(frozen=True)
class Screen:
    title: str
    items: list


Item = Union[Label, Divider, Action, Toggle, Submenu, Value, Meter,
             Row, Card, Embed]


# ── row palette (matches the chord menu) ────────────────────────────────
ROW_IDLE = "#170620"
ROW_IDLE_ALPHA = 0.65
ROW_ARMED_ALPHA = 0.22
ROW_BORDER_ALPHA = 0.85

ROW_HEIGHT = 38
ROW_BEVEL = 8
ROW_BEVEL_CORNERS = ("top-right", "bottom-left")

SCREEN_SPACING = 8
ROW_SPACING = 4

# Keysyms
_ESC, _RETURN, _BACKSPACE = 0xFF1B, 0xFF0D, 0xFF08
_LEFT, _UP, _RIGHT, _DOWN = 0xFF51, 0xFF52, 0xFF53, 0xFF54


def _iter_items(items):
    for item in items:
        if isinstance(item, Card):
            yield item
            yield from _iter_items(item.items)
        elif isinstance(item, Row):
            yield item
            yield from _iter_items(item.cells)
        else:
            yield item


def _selectable(item) -> bool:
    return isinstance(item, (Action, Toggle, Submenu))


class _RowBox(Box):
    """One selectable row: beveled plate that lights when armed."""

    def __init__(self, child: Widget, panel: "Panel", index: int) -> None:
        super().__init__(child,
                         padding=Insets.xy(14, 8),
                         background=ROW_IDLE,
                         bevel=ROW_BEVEL,
                         bevel_corners=ROW_BEVEL_CORNERS,
                         min_size=Size(0, ROW_HEIGHT))
        self._panel = panel
        self._index = index
        self.on_left_click = lambda _w: panel.activate(index)

    @property
    def armed(self) -> bool:
        return self._panel.selection == self._index

    def paint(self, canvas: skia.Canvas) -> None:
        path = shapes.beveled(self.rect.left(), self.rect.top(),
                              self.rect.width(), self.rect.height(),
                              bevel=self.bevel, corners=self.bevel_corners)
        fill = skia.Paint(AntiAlias=True)
        if self.armed:
            fill.setColor(theme.color(theme.CYAN_BRIGHT, ROW_ARMED_ALPHA))
        else:
            fill.setColor(theme.color(ROW_IDLE, ROW_IDLE_ALPHA))
        canvas.drawPath(path, fill)
        if self.armed:
            stroke = skia.Paint(AntiAlias=True)
            stroke.setColor(theme.color(theme.CYAN_BRIGHT, ROW_BORDER_ALPHA))
            stroke.setStyle(skia.Paint.kStroke_Style)
            stroke.setStrokeWidth(1.0)
            canvas.drawPath(shapes.beveled(
                self.rect.left(), self.rect.top(),
                self.rect.width(), self.rect.height(),
                bevel=self.bevel, corners=self.bevel_corners, inset=0.5),
                stroke)
        if self.child is not None:
            self.child.paint(canvas)


class Panel(Widget):
    animation_fps = 4          # live items refresh every 250ms

    def __init__(self, popup_name: str, root: Screen, *,
                 width: int = 380, **kwargs) -> None:
        super().__init__(**kwargs)
        self.popup_name = popup_name
        self.root = root
        self.width = width

        self._stack: list[list] = [[root, 0]]
        self._live: list[Callable[[], None]] = []
        self._rows: list[tuple] = []       # (item, index) per selectable
        self._column: Column | None = None
        self._embeds: list[Widget] = []
        self._build()

    # ── navigation state ────────────────────────────────────────────────
    @property
    def screen(self) -> Screen:
        return self._stack[-1][0]

    @property
    def selection(self) -> int:
        return self._stack[-1][1]

    @selection.setter
    def selection(self, value: int) -> None:
        self._stack[-1][1] = value

    def children(self):
        return (self._column,) if self._column is not None else ()

    # ── build ───────────────────────────────────────────────────────────
    def _build(self) -> None:
        self._live = []
        self._rows = []
        self._embeds = []
        kids: list[Widget] = [
            TextLabel(self.screen.title, size=theme.FONT_SIZE_LG, bold=True,
                      color=theme.HIGHLIGHT),
            Spacer(size=4),
        ]
        for item in self.screen.items:
            widget = self._interpret(item)
            if widget is not None:
                kids.append(widget)
        kids.append(Spacer())
        self._column = Column(kids, spacing=ROW_SPACING, align=Align.STRETCH)
        if self.window is not None:
            self._column.attach(self.window)

    def _interpret(self, item) -> Widget | None:
        if isinstance(item, Divider):
            return Rule(theme.CYAN_DIM)

        if isinstance(item, Label):
            color = {"heading": theme.HIGHLIGHT,
                     "body": theme.FG_STRONG,
                     "muted": theme.FG_MUTED}.get(item.style, theme.FG_STRONG)
            return TextLabel(item.text, size=theme.FONT_SIZE,
                             bold=item.style == "heading", color=color)

        if isinstance(item, Value):
            getter = item.get
            label = TextLabel(getter(), size=theme.FONT_SIZE,
                              color=item.color or theme.MAGENTA_BRIGHT,
                              align="right" if item.xalign > 0.5 else "left")
            self._live.append(lambda l=label, g=getter: l.set_value(g()))
            return label

        if isinstance(item, Meter):
            meter = BarMeter(color=(item.color if not callable(item.color)
                                    else theme.CYAN_BRIGHT),
                             thick=item.height, min_width=item.width)
            getter, top = item.get, item.max or 100.0
            self._live.append(
                lambda m=meter, g=getter, mx=top: m.set_value(g() / mx * 100.0))
            return meter

        if isinstance(item, Row):
            cells = [self._interpret(c) for c in item.cells]
            return HRow([c for c in cells if c is not None],
                        spacing=item.spacing, align=Align.CENTER)

        if isinstance(item, Card):
            kids: list[Widget] = [
                TextLabel(item.title, size=theme.FONT_SIZE, bold=True,
                          color=theme.HIGHLIGHT),
                Spacer(size=4),
            ]
            for sub in item.items:
                widget = self._interpret(sub)
                if widget is not None:
                    kids.append(widget)
            return Box(Column(kids, spacing=ROW_SPACING, align=Align.STRETCH),
                       padding=Insets.all(10),
                       background=theme.BASE_SHADOW,
                       bevel=ROW_BEVEL, bevel_corners=ROW_BEVEL_CORNERS)

        if isinstance(item, Embed):
            self._embeds.append(item.widget)
            return item.widget

        if _selectable(item):
            return self._build_row(item)
        return None

    def _build_row(self, item) -> Widget:
        index = len(self._rows)
        self._rows.append((item, index))

        cells: list[Widget] = [
            TextLabel(item.label, size=theme.FONT_SIZE,
                      color=lambda i=index: self._row_color(i)),
            Spacer(),
        ]
        if isinstance(item, Toggle):
            getter = item.get
            state = TextLabel(lambda g=getter: "ON" if g() else "OFF",
                              size=theme.FONT_SIZE, bold=True, align="right",
                              color=lambda g=getter: (theme.LIME_BRIGHT if g()
                                                      else theme.BASE_MUTED))
            self._live.append(lambda s=state: s.invalidate())
            cells.append(state)
        elif isinstance(item, Submenu):
            cells.append(TextLabel("›", size=theme.FONT_SIZE, bold=True,
                                   color=lambda i=index: self._row_color(i)))
        elif isinstance(item, Action) and item.hint:
            cells.append(TextLabel(item.hint, size=theme.FONT_SIZE, bold=True,
                                   align="right", color=theme.HIGHLIGHT))
        if getattr(item, "key", None):
            cells.append(TextLabel(f"[{item.key}]", size=theme.FONT_SIZE,
                                   color=theme.BASE_MUTED))

        return _RowBox(HRow(cells, spacing=8, align=Align.CENTER), self, index)

    def _row_color(self, index: int) -> str:
        return theme.FG_ACCENT if self.selection == index else theme.FG_STRONG

    # ── activation ──────────────────────────────────────────────────────
    def activate(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        self.selection = index
        item, _ = self._rows[index]
        if isinstance(item, Submenu):
            self._stack.append([item.target, 0])
            self._build()
            self._relayout()
            return
        if isinstance(item, Toggle):
            item.set(not item.get())
            self.invalidate()
            return
        if isinstance(item, Action):
            try:
                item.on_activate()
            except Exception:
                log.exception("action %r failed", item.label)
            if item.close_on_activate:
                self._close()

    def _pop(self) -> bool:
        if len(self._stack) <= 1:
            return False
        self._stack.pop()
        self._build()
        self._relayout()
        return True

    def _close(self) -> None:
        from ..core.daemon import get_daemon
        get_daemon().close(self.popup_name)

    def _relayout(self) -> None:
        if self.window is not None:
            self._column.attach(self.window)
            self.invalidate(layout=True)

    # ── keyboard ────────────────────────────────────────────────────────
    def key(self, keysym: int, shift: bool = False) -> bool:
        count = len(self._rows)
        if keysym == _ESC:
            self._close()
            return True
        if keysym in (_BACKSPACE, _LEFT) or keysym == ord("h"):
            return self._pop()
        if keysym == _DOWN or keysym == ord("j"):
            if count:
                self.selection = (self.selection + 1) % count
                self.invalidate()
            return True
        if keysym == _UP or keysym == ord("k"):
            if count:
                self.selection = (self.selection - 1) % count
                self.invalidate()
            return True
        if keysym in (_RETURN, _RIGHT) or keysym == ord("l"):
            self.activate(self.selection)
            return True
        # Single-character hotkeys declared on the items themselves.
        if 0x20 <= keysym <= 0x7E:
            char = chr(keysym)
            for item, index in self._rows:
                if getattr(item, "key", None) == char:
                    self.activate(index)
                    return True
        return False

    # ── live refresh ────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        for refresh in self._live:
            try:
                refresh()
            except Exception:
                log.exception("panel live refresh failed")

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        return Size(avail.width, avail.height)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self._column is not None:
            self._column.measure(Size(rect.width(), rect.height()))
            self._column.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        if self._column is not None:
            self._column.paint(canvas)
