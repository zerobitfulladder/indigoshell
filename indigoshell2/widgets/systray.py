"""System tray — split across three widgets.

`Systray` is the bar-side indicator: a stack of horizontal bars that
lights from the bottom as items register. Clicking toggles `TrayPanel`,
a vertical menu-styled list of registered app names with full
click/scroll/menu dispatch — v1's SystrayPanel, drawn in Skia.
`TrayMenu` renders an item's DBusMenu (v1 delegated this to Gtk.Menu;
here it is a window of our own, styled like v1's menu CSS).

The panel and menu windows are sized by their content, which is only
known at open time — so their `WindowSpec`s are built on each open and
registered into `daemon.kinds` rather than declared in the config. All
three widgets share `services.systray.get_broker()`; the indicator's
subscription keeps the broker alive while panels come and go.
"""

import asyncio
import logging
import math

import skia

from .. import shapes, text, theme
from ..services.dbusmenu import MenuNode
from ..services.systray import TrayItem, get_broker
from ..window import Anchor, Layer, WindowSpec
from .base import Insets, Size, Widget
from .layout import Align, Box, Column

log = logging.getLogger(__name__)

PANEL_WINDOW = "systray-panel"
MENU_WINDOW = "systray-menu"

# Big-but-finite measuring box for "what's your natural size".
_UNBOUNDED = Size(10_000.0, 10_000.0)


def _measure_size(widget: Widget) -> tuple[int, int]:
    natural = widget.measure(_UNBOUNDED)
    return math.ceil(natural.width), math.ceil(natural.height)


class Systray(Widget):
    """Stack of horizontal bars, lit from the bottom up as items
    register. Click toggles the panel; a no-op when no items are
    registered — an empty panel would just be the padding wrapper.

    Visual recipe matches Volume / Workspaces — see their paint methods
    for the family resemblance."""

    def __init__(self, *, bars: int = 5, gap: int = 2, width: int = 10,
                 panel_effects: tuple = (), effect_scale: float = 0.35,
                 **kwargs) -> None:
        def _click(_source, self=self):
            self._toggle_panel()
        _click._indigo_window = PANEL_WINDOW
        kwargs.setdefault("on_left_click", _click)
        super().__init__(**kwargs)
        self.bars = max(1, bars)
        self.gap = max(0, gap)
        self.w = max(4, width)
        self.panel_effects = tuple(panel_effects)
        self.effect_scale = effect_scale
        self._broker = get_broker()

    def attach(self, window) -> None:
        super().attach(window)
        self._broker.start()
        self._broker.subscribe(self._on_added, self._on_removed,
                               self._on_changed)

    def detach(self) -> None:
        self._broker.unsubscribe(self._on_added, self._on_removed,
                                 self._on_changed)
        super().detach()

    # ── broker callbacks ─────────────────────────────────────────────
    def _on_added(self, _item: TrayItem) -> None: self.invalidate()
    def _on_removed(self, _bus_name: str) -> None: self.invalidate()
    def _on_changed(self, _item: TrayItem) -> None: pass  # count unchanged

    # ── layout / paint (same recipe as Volume / Workspaces) ──────────
    def measure(self, avail: Size) -> Size:
        # Width fixed; height stretches to fill the bar, so the stack
        # runs edge to edge with no vertical margin around the bars.
        return Size(self.w, avail.height)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        n = self.bars
        cell_h = max(1.0, (r.height() - self.gap * (n - 1)) / n)
        lit_count = min(len(self._broker.items()), n)
        paint = skia.Paint(AntiAlias=False)
        for i in range(n):
            # i=0 is the bottom-most cell; lit fills bottom-up.
            y = r.bottom() - (i + 1) * cell_h - i * self.gap
            color = theme.ERROR if i < lit_count else theme.MAGENTA_DIM
            paint.setColor(theme.color(color))
            canvas.drawRect(
                skia.Rect.MakeXYWH(r.left(), y, r.width(), cell_h), paint)

    # ── panel window ─────────────────────────────────────────────────
    def _toggle_panel(self) -> None:
        from ..core.daemon import get_daemon
        d = get_daemon()
        if PANEL_WINDOW in d.instances:
            d.close(PANEL_WINDOW)
            return
        if not self._broker.items():
            return
        d.kinds[PANEL_WINDOW] = self._panel_spec()
        d.open(PANEL_WINDOW)

    def _panel_spec(self) -> WindowSpec:
        # No popup chrome — the beveled rows float directly on the
        # desktop, so the window is exactly the row stack.
        content = TrayPanel()
        return WindowSpec(
            name=PANEL_WINDOW,
            layer=Layer.OVERLAY,
            anchor=Anchor.BOTTOM | Anchor.RIGHT,
            size=_measure_size(content),
            margin=Insets(right=theme.BAR_MARGIN,
                          bottom=theme.BAR_HEIGHT + theme.POPUP_GAP),
            override_redirect=True,
            focusable=False,
            dismiss_on_outside_click=True,
            grab_keyboard=True,          # Escape closes
            background="#00000000",
            effects=self.panel_effects,
            effect_scale=self.effect_scale,
            content=content,
        )


# ── panel (popup content) ────────────────────────────────────────────
class TrayPanel(Column):
    """Vertical list of beveled rows — one per tray item — styled to
    match the chord-menu popups. Each row shows the app name
    (right-aligned) with a yellow `//` marker on the right.

    Left-click activates the item (or opens its menu, if it has one);
    right-click always opens the menu. Scroll forwards through to the
    item."""

    ROW_SPACING = 4
    MENU_GAP = 10  # px between a row and its app-supplied context menu

    def __init__(self) -> None:
        self._broker = get_broker()
        rows = [_TrayRow(self, item) for item in self._broker.items()]
        super().__init__(rows, spacing=self.ROW_SPACING, align=Align.STRETCH)

    def attach(self, window) -> None:
        super().attach(window)
        self._broker.subscribe(self._on_added, self._on_removed,
                               self._on_changed)

    def detach(self) -> None:
        self._broker.unsubscribe(self._on_added, self._on_removed,
                                 self._on_changed)
        # A menu is anchored to one of our rows; it doesn't outlive us.
        _close_menu_window()
        super().detach()

    # ── broker callbacks ─────────────────────────────────────────────
    def _on_added(self, item: TrayItem) -> None:
        if any(r.item.bus_name == item.bus_name for r in self._children):
            return
        self.replace([*self._children, _TrayRow(self, item)])
        self._refit()

    def _on_removed(self, bus_name: str) -> None:
        rows = [r for r in self._children if r.item.bus_name != bus_name]
        if len(rows) == len(self._children):
            return
        if not rows:
            from ..core.daemon import get_daemon
            get_daemon().close(PANEL_WINDOW)
            return
        self.replace(rows)
        self._refit()

    def _on_changed(self, _item: TrayItem) -> None:
        # A new title can change a row's width, so re-measure, not just
        # repaint.
        self.invalidate(layout=True)
        self._refit()

    def _refit(self) -> None:
        """The window was sized for the rows it opened with; re-anchor it
        against the registered edge when the set changes while mapped."""
        if self.window is None:
            return
        self.window.resize_content(*_measure_size(self.window.content))

    # ── menus ────────────────────────────────────────────────────────
    def open_menu(self, row: "_TrayRow", fallback_xy: tuple[int, int]) -> None:
        """Pop the item's DBusMenu next to its row. Falls back to the
        item's own `ContextMenu(x,y)` when it exports no usable layout —
        the legacy path most apps still implement."""
        bus_name = row.item.bus_name

        async def _open() -> None:
            nodes = await self._broker.fetch_menu(bus_name)
            if self.window is None:      # panel closed while fetching
                return
            if not nodes:
                self._broker.context_menu(bus_name, *fallback_xy)
                return
            self._open_menu_window(row, nodes)

        try:
            asyncio.get_running_loop().create_task(_open())
        except RuntimeError:
            pass

    def _open_menu_window(self, row: "_TrayRow", nodes: list[MenuNode]) -> None:
        from ..core.daemon import get_daemon
        d = get_daemon()
        if MENU_WINDOW in d.instances:
            d.close(MENU_WINDOW)
        content = _menu_box(TrayMenu(row.item.bus_name, nodes))
        w, h = _measure_size(content)
        win = self.window
        mon = d.display.primary_monitor()
        # The menu's top-right corner sits MENU_GAP left of the row's
        # top-left — v1's popup_at_rect gravity, in root coordinates.
        x = int(win.x + row.rect.left()) - self.MENU_GAP - w
        y = int(win.y + row.rect.top())
        x = max(mon.x, min(x, mon.x + mon.width - w))
        y = max(mon.y, min(y, mon.y + mon.height - h))
        d.kinds[MENU_WINDOW] = WindowSpec(
            name=MENU_WINDOW,
            layer=Layer.OVERLAY,
            anchor=Anchor.TOP | Anchor.LEFT,
            size=(w, h),
            margin=Insets(left=x - mon.x, top=y - mon.y),
            override_redirect=True,
            focusable=False,
            dismiss_on_outside_click=True,
            grab_keyboard=True,
            background="#00000000",
            content=content,
        )
        d.open(MENU_WINDOW)


class _TrayRow(Widget):
    """One beveled row. Owns its input dispatch; the broker methods it
    forwards to want root coordinates, which is window origin plus the
    window-local click position."""

    HEIGHT = 38
    BEVEL = 8
    BEVEL_CORNERS = ("top-right", "bottom-left")
    PAD_X = 18
    GAP = 10          # between label and the // marker
    TRACKING = 1.0    # v1's letter-spacing on menu labels
    MARK = "//"
    MIN_WIDTH = 160
    LINE_W = 1.2

    def __init__(self, panel: TrayPanel, item: TrayItem) -> None:
        super().__init__()
        self.panel = panel
        self.item = item

    @property
    def interactive(self) -> bool:
        return True

    def _text(self) -> str:
        item = self.item
        return item.title or item.tooltip_title or item.id or "?"

    # ── layout / paint ───────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        w = (self.PAD_X * 2 + self.GAP
             + text.measure(self._text(), theme.FONT_SIZE,
                            tracking=self.TRACKING)
             + text.measure(self.MARK, theme.FONT_SIZE, bold=True))
        return Size(max(w, self.MIN_WIDTH), self.HEIGHT)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        # The rows used to sit on the popup window's backdrop; now that
        # they float bare, each carries that slab itself.
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(theme.POPUP_BG))
        canvas.drawPath(
            shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                           bevel=self.BEVEL, corners=self.BEVEL_CORNERS),
            fill)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(self.LINE_W)
        stroke.setColor(theme.color(theme.CYAN_BRIGHT, alpha=0.85))
        canvas.drawPath(
            shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                           bevel=self.BEVEL, corners=self.BEVEL_CORNERS,
                           inset=self.LINE_W / 2),
            stroke)
        baseline = text.baseline_in(r, theme.FONT_SIZE)
        mark_w = text.measure(self.MARK, theme.FONT_SIZE, bold=True)
        mark_x = r.right() - self.PAD_X - mark_w
        text.draw(canvas, self.MARK, mark_x, baseline, theme.FONT_SIZE,
                  theme.color(theme.YELLOW_MID), bold=True)
        label = self._text()
        label_w = text.measure(label, theme.FONT_SIZE, tracking=self.TRACKING)
        # Right-aligned against the marker, like v1's xalign=1.0 label.
        text.draw(canvas, label, mark_x - self.GAP - label_w, baseline,
                  theme.FONT_SIZE, theme.color(theme.FG_STRONG),
                  tracking=self.TRACKING)

    # ── input dispatch ───────────────────────────────────────────────
    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        broker = self.panel._broker
        name = self.item.bus_name
        win = self.window
        root_xy = (int(win.x + x), int(win.y + y)) if win is not None \
            else (int(x), int(y))
        if button in (1, 2, 3):
            # Clicking the row whose menu is already open toggles it
            # closed; any other click just dismisses the stray menu.
            was_ours = _open_menu_bus_name() == name
            _close_menu_window()
            if was_ours and button in (1, 3):
                return True
        if button == 1:
            # Items that export a menu usually don't implement Activate;
            # ItemIsMenu says so outright. Everything else gets Activate.
            if self.item.item_is_menu or self.item.menu_path:
                self.panel.open_menu(self, root_xy)
            else:
                broker.activate(name, *root_xy)
            return True
        if button == 2:
            broker.secondary_activate(name, *root_xy)
            return True
        if button == 3:
            if self.item.menu_path:
                self.panel.open_menu(self, root_xy)
            else:
                broker.context_menu(name, *root_xy)
            return True
        if button == 4:
            broker.scroll(name, -1, "vertical")
            return True
        if button == 5:
            broker.scroll(name, 1, "vertical")
            return True
        if button == 6:
            broker.scroll(name, -1, "horizontal")
            return True
        if button == 7:
            broker.scroll(name, 1, "horizontal")
            return True
        return False


# ── menu (popup content) ─────────────────────────────────────────────
# v1 styled Gtk menus with CSS; these constants are that stylesheet.
_MENU_BG = "#170620EB"                  # rgba(23, 6, 32, 0.92)
_MENU_FONT_SIZE = theme.FONT_SIZE - 6
_MENU_PAD = 4                           # menu border padding
_MENU_ITEM_PAD_X = 12
_MENU_ITEM_PAD_Y = 4
_MENU_INDENT = 12                       # per submenu level


def _menu_box(menu: "TrayMenu") -> Box:
    return Box(
        menu,
        padding=Insets.all(_MENU_PAD),
        background=_MENU_BG,
        border=theme.HIGHLIGHT,
        border_width=1.5,
    )


class TrayMenu(Column):
    """An item's DBusMenu, rendered as rows. Gtk hung submenus off to
    the side; here they expand inline, indented under their parent —
    one window, one grab, no cascade choreography."""

    def __init__(self, bus_name: str, nodes: list[MenuNode]) -> None:
        self.bus_name = bus_name
        self.nodes = nodes
        self._expanded: set[int] = set()
        super().__init__(self._build_rows(), spacing=0, align=Align.STRETCH)

    def _build_rows(self) -> list[Widget]:
        rows: list[Widget] = []

        def add(nodes: list[MenuNode], depth: int) -> None:
            for node in nodes:
                if node.separator:
                    rows.append(_MenuSeparator())
                    continue
                rows.append(_MenuItemRow(self, node, depth))
                if node.id in self._expanded and node.children:
                    add(node.children, depth + 1)

        add(self.nodes, 0)
        return rows

    def toggle_submenu(self, node: MenuNode) -> None:
        if node.id in self._expanded:
            self._expanded.discard(node.id)
        else:
            self._expanded.add(node.id)
        self.replace(self._build_rows())
        if self.window is not None:
            self.window.resize_content(*_measure_size(self.window.content))

    def activate(self, node: MenuNode) -> None:
        get_broker().menu_event(self.bus_name, node.id)
        _close_menu_window()

    def detach(self) -> None:
        super().detach()
        # One client holds one pointer/keyboard grab, so ours replaced
        # the panel's when we opened and released it outright when we
        # closed. Give the grabs back or the panel stops dismissing on
        # outside clicks and stops seeing Escape.
        from ..core.daemon import get_daemon
        panel = get_daemon().instances.get(PANEL_WINDOW)
        if panel is not None and panel.mapped:
            if panel.spec.dismiss_on_outside_click:
                panel._grab_pointer()
            if panel.spec.grab_keyboard:
                panel._grab_keyboard()


class _MenuItemRow(Widget):
    def __init__(self, menu: TrayMenu, node: MenuNode, depth: int) -> None:
        super().__init__()
        self.menu = menu
        self.node = node
        self.depth = depth

    @property
    def interactive(self) -> bool:
        return True

    def _label(self) -> str:
        prefix = ""
        n = self.node
        if n.toggle_type == "checkmark":
            prefix = {1: "✓ ", -1: "− "}.get(n.toggle_state, "  ")
        elif n.toggle_type == "radio":
            prefix = "● " if n.toggle_state == 1 else "○ "
        return prefix + _strip_mnemonic(n.label)

    def measure(self, avail: Size) -> Size:
        w = (_MENU_ITEM_PAD_X * 2 + self.depth * _MENU_INDENT
             + text.measure(self._label(), _MENU_FONT_SIZE))
        if self.node.has_submenu:
            w += _MENU_ITEM_PAD_X + text.measure("▸", _MENU_FONT_SIZE)
        h = text.line_height(_MENU_FONT_SIZE) + _MENU_ITEM_PAD_Y * 2
        return Size(w, h)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        n = self.node
        if self.hovered and n.enabled:
            fill = skia.Paint(AntiAlias=False)
            fill.setColor(theme.color(theme.CYAN_BRIGHT, alpha=0.18))
            canvas.drawRect(r, fill)
        if not n.enabled:
            color = theme.CYAN_DIM
        elif self.hovered:
            color = theme.CYAN_BRIGHT
        else:
            color = theme.CYAN_MID
        baseline = text.baseline_in(r, _MENU_FONT_SIZE)
        text.draw(canvas, self._label(),
                  r.left() + _MENU_ITEM_PAD_X + self.depth * _MENU_INDENT,
                  baseline, _MENU_FONT_SIZE, theme.color(color))
        if n.has_submenu:
            arrow = "▾" if n.id in self.menu._expanded else "▸"
            arrow_w = text.measure(arrow, _MENU_FONT_SIZE)
            text.draw(canvas, arrow, r.right() - _MENU_ITEM_PAD_X - arrow_w,
                      baseline, _MENU_FONT_SIZE, theme.color(color))

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1:
            return False
        if not self.node.enabled:
            return True
        if self.node.has_submenu and self.node.children:
            self.menu.toggle_submenu(self.node)
        else:
            self.menu.activate(self.node)
        return True


class _MenuSeparator(Widget):
    MARGIN_Y = 4
    MARGIN_X = 8

    def measure(self, avail: Size) -> Size:
        return Size(0.0, 1.0 + self.MARGIN_Y * 2)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(theme.YELLOW_FAINT))
        canvas.drawRect(
            skia.Rect.MakeXYWH(r.left() + self.MARGIN_X, r.centerY() - 0.5,
                               max(0.0, r.width() - self.MARGIN_X * 2), 1.0),
            paint)


def _close_menu_window() -> None:
    from ..core.daemon import get_daemon
    d = get_daemon()
    if MENU_WINDOW in d.instances:
        d.close(MENU_WINDOW)


def _open_menu_bus_name() -> str | None:
    """Which item the currently open menu window belongs to, if any."""
    from ..core.daemon import get_daemon
    win = get_daemon().instances.get(MENU_WINDOW)
    if win is None:
        return None
    menu = getattr(win.content, "child", None)
    return menu.bus_name if isinstance(menu, TrayMenu) else None


def _strip_mnemonic(s: str) -> str:
    """Drop the spec's `_` accelerator markers; `__` is a literal `_`."""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "_":
            if i + 1 < len(s) and s[i + 1] == "_":
                out.append("_")
                i += 2
                continue
            i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)
