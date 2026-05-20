"""System tray — split across two widgets.

`Systray` is the bar-side indicator: a stack of horizontal bars that
lights from the bottom as items register. Clicking opens
`SystrayPanel`, a vertical menu-styled list of registered app names
with full click/scroll/menu dispatch.

Both widgets share `services.systray.get_broker()` — they subscribe
independently so the panel can be destroyed/recreated without the
indicator losing its count.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from ..services.systray import TrayItem, get_broker
from .base import Widget, beveled_path, paint
from .menu import _RGBA_BORDER


class Systray(Widget):
    """Stack of horizontal bars, lit from the bottom up as items
    register. Click toggles the panel (a separate PopupKind). Click is
    a no-op when no items are registered.

    Visual recipe matches Volume / Workspaces — see their `_draw`
    methods for the family resemblance."""

    def __init__(
        self,
        popup_name: str = "systray-panel",
        bars: int = 5,
        cell_thick: int = 4,
        gap: int = 2,
        width: int = 10,
        **kwargs,
    ):
        from ..api import toggle  # local import: api → daemon → widgets cycle
        open_panel = toggle(popup_name)

        def _click(source):
            # Suppress opens when no items are registered; an empty panel
            # would just be the padding wrapper, with nothing actionable.
            if not self._broker.items():
                return
            open_panel(source)
        _click._indigo_popup_name = popup_name
        kwargs.setdefault("on_left_click", _click)

        super().__init__(**kwargs)
        self.popup_name = popup_name
        self.bars = max(1, bars)
        self.cell_thick = max(1, cell_thick)
        self.gap = max(0, gap)
        self.w = max(4, width)
        self._broker = get_broker()
        self._count = 0
        self._ev: Gtk.Widget | None = None

    def build_widget(self) -> Gtk.Widget:
        # Width fixed; height left unconstrained so the indicator stretches
        # to fill the bar's full height (no vertical margin around the bars).
        filler = Gtk.Box()
        filler.set_size_request(self.w, -1)
        return filler

    def build(self) -> Gtk.Widget:
        w = super().build()  # wraps in EventBox because we set hover handlers
        # Override the base widget's CENTER/no-expand defaults so the
        # event box (and its child) fill vertically.
        w.set_valign(Gtk.Align.FILL)
        w.set_vexpand(True)
        if self._named_widget is not None:
            self._named_widget.set_valign(Gtk.Align.FILL)
            self._named_widget.set_vexpand(True)
        w.connect_after("draw", self._draw)
        self._ev = w
        return w

    def start(self) -> None:
        super().start()
        self._broker.start()
        self._broker.subscribe(self._on_added, self._on_removed, self._on_changed)
        self._count = len(self._broker.items())
        if self._ev is not None:
            self._ev.queue_draw()

    def stop(self) -> None:
        self._broker.unsubscribe(self._on_added, self._on_removed, self._on_changed)
        super().stop()

    # ── broker callbacks ─────────────────────────────────────────────
    def _on_added(self, _item: TrayItem) -> None: GLib.idle_add(self._refresh_count)
    def _on_removed(self, _bus_name: str) -> None: GLib.idle_add(self._refresh_count)
    def _on_changed(self, _item: TrayItem) -> None: pass  # count unchanged

    def _refresh_count(self) -> bool:
        self._count = len(self._broker.items())
        if self._ev is not None:
            self._ev.queue_draw()
        return False

    # ── drawing (same recipe as Volume / Workspaces indicators) ──────
    def _draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n = self.bars
        cell_h = max(1.0, (height - self.gap * (n - 1)) / n)
        lit_count = min(self._count, n)
        for i in range(n):
            # i=0 is the bottom-most cell; lit fills bottom-up.
            y = height - (i + 1) * cell_h - i * self.gap
            color = theme.ERROR if i < lit_count else theme.MAGENTA_DIM
            paint(cr, color)
            cr.rectangle(0, y, width, cell_h)
            cr.fill()
        return False


# ── panel widget (popup content) ─────────────────────────────────────
class SystrayPanel(Widget):
    """Vertical list of beveled rows — one per tray item — styled to
    match the chord-menu popups. Each row shows the app name
    (right-aligned) with a yellow `//` marker on the right.

    Left-click activates the item (or opens its menu, if it has one);
    right-click always opens the menu. Scroll forwards through to the
    item."""

    ROW_SPACING = 4
    ROW_HEIGHT = 38
    BEVEL = 8
    BEVEL_CORNERS = ("top-right", "bottom-left")
    MENU_GAP = 10  # px between a row and its app-supplied context menu

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._box: Gtk.Box | None = None
        self._rows: dict[str, Gtk.Widget] = {}
        self._broker = get_broker()
        self._open_menu: Gtk.Menu | None = None

    def build_widget(self) -> Gtk.Widget:
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.ROW_SPACING)
        return self._box

    def start(self) -> None:
        super().start()
        self._broker.start()
        self._broker.subscribe(self._on_added, self._on_removed, self._on_changed)
        # Populate synchronously so the popup's natural size is correct
        # when PopupKind reads it for first-open positioning. Going
        # through GLib.idle_add here would make the first window measure
        # itself as an empty padding wrapper.
        for item in self._broker.items():
            self._add_row(item)

    def stop(self) -> None:
        # Drop cached rows: on non-persistent popups, the Gtk widgets
        # get destroyed but this instance is reused on the next open.
        # Stale bus_names in _rows would cause _add_row to skip every item.
        self._broker.unsubscribe(self._on_added, self._on_removed, self._on_changed)
        self._rows.clear()
        self._box = None
        self._open_menu = None
        super().stop()

    # ── broker callbacks ─────────────────────────────────────────────
    def _on_added(self, item: TrayItem) -> None:   GLib.idle_add(self._add_row, item)
    def _on_removed(self, bus_name: str) -> None:  GLib.idle_add(self._remove_row, bus_name)
    def _on_changed(self, item: TrayItem) -> None: GLib.idle_add(self._refresh_row, item)

    # ── row management ───────────────────────────────────────────────
    def _add_row(self, item: TrayItem) -> bool:
        if self._box is None or item.bus_name in self._rows:
            return False
        row = self._build_row(item)
        self._rows[item.bus_name] = row
        self._box.pack_start(row, False, False, 0)
        row.show_all()
        self._refit_popup()
        return False

    def _remove_row(self, bus_name: str) -> bool:
        row = self._rows.pop(bus_name, None)
        if row is not None and self._box is not None:
            self._box.remove(row)
            row.destroy()
            if self._rows:
                self._refit_popup()
            else:
                self._close_popup()
        return False

    def _close_popup(self) -> None:
        if self._box is None:
            return
        top = self._box.get_toplevel()
        if not isinstance(top, Gtk.Window) or not top.get_realized():
            return
        from ..core.daemon import get_daemon  # deferred: avoid import cycle
        d = get_daemon()
        name = next((n for n, w in d.instances.items() if w is top), None)
        if name is not None:
            d.close(name)

    def _refit_popup(self) -> None:
        """Persistent popups keep their previous size+position when the
        content set changes while the window is mapped. Ask the daemon
        to resize-and-reposition against the registered anchor."""
        if self._box is None:
            return
        top = self._box.get_toplevel()
        if not isinstance(top, Gtk.Window) or not top.get_realized():
            return
        from ..core.daemon import get_daemon  # deferred: avoid import cycle
        d = get_daemon()
        name = next((n for n, w in d.instances.items() if w is top), None)
        if name is None:
            return
        kind = d.kinds.get(name)
        anchor = d.anchors.get(name)
        if kind is not None and hasattr(kind, "refit"):
            kind.refit(top, anchor)

    def _refresh_row(self, item: TrayItem) -> bool:
        row = self._rows.get(item.bus_name)
        if row is None:
            return False
        label = getattr(row, "_text_label", None)
        if label is not None:
            label.set_text(self._row_text(item))
        self._set_tooltip(row, item)
        return False

    def _build_row(self, item: TrayItem) -> Gtk.Widget:
        ev = Gtk.EventBox()
        ev.set_visible_window(False)
        ev.set_size_request(-1, self.ROW_HEIGHT)
        ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        inner.set_margin_start(18)
        inner.set_margin_end(18)
        inner.set_valign(Gtk.Align.CENTER)

        text_lbl = Gtk.Label(label=self._row_text(item))
        text_lbl.get_style_context().add_class("menu-label")
        text_lbl.set_single_line_mode(True)
        text_lbl.set_valign(Gtk.Align.CENTER)
        text_lbl.set_xalign(1.0)

        mark_lbl = Gtk.Label()
        mark_lbl.set_markup(f"<span color='{theme.YELLOW_MID}'>//</span>")
        mark_lbl.get_style_context().add_class("menu-key")
        mark_lbl.set_single_line_mode(True)
        mark_lbl.set_valign(Gtk.Align.CENTER)

        inner.pack_start(text_lbl, True, True, 0)  # expand: pushes // to the right edge
        inner.pack_end(mark_lbl, False, False, 0)
        ev.add(inner)
        ev._text_label = text_lbl  # type: ignore[attr-defined]

        ev.connect("draw", self._draw_row)
        ev.connect("button-press-event", self._on_click, item.bus_name)
        ev.connect("scroll-event", self._on_scroll, item.bus_name)
        self._set_tooltip(ev, item)
        return ev

    def _row_text(self, item: TrayItem) -> str:
        return item.title or item.tooltip_title or item.id or "?"

    def _set_tooltip(self, ev: Gtk.Widget, item: TrayItem) -> None:
        body = item.tooltip_body
        title = item.tooltip_title or item.title or item.id
        if body:
            ev.set_tooltip_markup(f"<b>{_escape(title)}</b>\n{_escape(body)}")
        elif title and title != self._row_text(item):
            ev.set_tooltip_text(title)
        else:
            ev.set_has_tooltip(False)

    # ── styling ──────────────────────────────────────────────────────
    def default_css(self) -> str:
        sel = f"#{self.name}"
        return (
            f"{sel} {{ background: transparent; }}"
            f"{sel} .menu-key {{"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" font-weight: bold;"
            f" }}"
            f"{sel} .menu-label {{"
            f" color: {theme.FG_STRONG};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" letter-spacing: 1px;"
            f" }}"
        )

    def _draw_row(self, widget: Gtk.Widget, cr) -> bool:
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        line_w = 1.2
        inset = line_w / 2
        beveled_path(cr, w, h, bevel=self.BEVEL, corners=self.BEVEL_CORNERS, inset=inset)
        cr.set_source_rgba(*_RGBA_BORDER)
        cr.set_line_width(line_w)
        cr.stroke()
        return False  # let children render on top

    # ── input dispatch ───────────────────────────────────────────────
    def _on_click(self, _w, event, bus_name: str) -> bool:
        x, y = int(event.x_root), int(event.y_root)
        item = next((i for i in self._broker.items() if i.bus_name == bus_name), None)
        if event.button == 1:
            if item is not None and (item.item_is_menu or not self._supports_activate(item)):
                if not self._popup_menu(bus_name, event):
                    self._broker.context_menu(bus_name, x, y)
            else:
                self._broker.activate(bus_name, x, y)
            return True
        if event.button == 2:
            self._broker.secondary_activate(bus_name, x, y)
            return True
        if event.button == 3:
            if not self._popup_menu(bus_name, event):
                self._broker.context_menu(bus_name, x, y)
            return True
        return False

    def _on_scroll(self, _w, event, bus_name: str) -> bool:
        if event.direction == Gdk.ScrollDirection.UP:    self._broker.scroll(bus_name, -1, "vertical")
        elif event.direction == Gdk.ScrollDirection.DOWN:  self._broker.scroll(bus_name,  1, "vertical")
        elif event.direction == Gdk.ScrollDirection.LEFT:  self._broker.scroll(bus_name, -1, "horizontal")
        elif event.direction == Gdk.ScrollDirection.RIGHT: self._broker.scroll(bus_name,  1, "horizontal")
        return True

    def _popup_menu(self, bus_name: str, event) -> bool:
        menu = self._broker.build_menu(bus_name)
        if menu is None:
            return False
        _install_menu_css()
        _tag_menu_recursively(menu)
        row = self._rows.get(bus_name)
        self._open_menu = menu
        menu.connect("hide", lambda _m: setattr(self, "_open_menu", None))
        menu.attach_to_widget(row or self._box, None)
        if row is not None:
            # popup_at_widget places the menu flush against the row.
            # popup_at_rect with a 1px sliver placed `MENU_GAP` to the
            # left of the row gives the menu a visible breathing gap.
            alloc = row.get_allocation()
            gap = self.MENU_GAP
            rect = Gdk.Rectangle()
            rect.x = alloc.x - gap
            rect.y = alloc.y
            rect.width = 1
            rect.height = alloc.height
            gdk_window = row.get_window()
            if gdk_window is not None:
                menu.popup_at_rect(
                    gdk_window, rect,
                    Gdk.Gravity.NORTH_WEST, Gdk.Gravity.NORTH_EAST, event,
                )
            else:
                menu.popup_at_widget(row, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.NORTH_EAST, event)
        else:
            menu.popup_at_pointer(event)
        return True

    def _supports_activate(self, item: TrayItem) -> bool:
        return not item.menu_path


# ── menu styling (one-shot global CSS) ───────────────────────────────
_MENU_CSS_CLASS = "indigo-tray-menu"
_menu_css_installed = False


def _install_menu_css() -> None:
    global _menu_css_installed
    if _menu_css_installed:
        return
    cls = _MENU_CSS_CLASS
    css = (
        f"menu.{cls} {{"
        f"  background-color: rgba(23, 6, 32, 0.92);"
        f"  border: 1.5px solid {theme.HIGHLIGHT};"
        f"  padding: 4px;"
        f"}}"
        f"menu.{cls} menuitem {{"
        f"  background-color: transparent;"
        f"  color: {theme.CYAN_MID};"
        f"  font-family: {theme.FONT};"
        f"  font-size: {theme.FONT_SIZE - 6}px;"
        f"  padding: 4px 12px;"
        f"  min-height: 0;"
        f"}}"
        f"menu.{cls} menuitem label {{"
        f"  padding: 0;"
        f"}}"
        f"menu.{cls} menuitem:hover {{"
        f"  background-color: rgba(5, 217, 232, 0.18);"
        f"  color: {theme.CYAN_BRIGHT};"
        f"}}"
        f"menu.{cls} menuitem:disabled,"
        f"menu.{cls} menuitem:disabled label {{"
        f"  color: {theme.CYAN_DIM};"
        f"}}"
        f"menu.{cls} separator {{"
        f"  background-color: {theme.YELLOW_FAINT};"
        f"  min-height: 1px;"
        f"  margin: 4px 8px;"
        f"}}"
    )
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _menu_css_installed = True


def _tag_menu_recursively(menu: Gtk.Menu) -> None:
    menu.get_style_context().add_class(_MENU_CSS_CLASS)
    for child in menu.get_children():
        sub = child.get_submenu() if hasattr(child, "get_submenu") else None
        if sub is not None:
            _tag_menu_recursively(sub)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
