"""Reusable cyberpunk HUD chrome shared across panel popups.

Two pieces:

  • TabBar  — minimal tab strip; letter-spaced labels with a single
              cyan rail under the active tab.
  • HudCard — translucent panel with the popup's bevel language and
              a left-edge accent stripe that wraps the bottom-left cut.
"""

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

from .. import theme
from .base import beveled_path, paint


__all__ = ["TabBar", "HudCard", "plain_label", "section_header"]


def plain_label(text: str, css_class: str | None = None, *, xalign: float = 0.0) -> Gtk.Label:
    """A simple label with vcenter and an optional css class — used for
    the `label-key`/`empty`/etc. roles defined by each panel's CSS."""
    lbl = Gtk.Label(label=text)
    if css_class:
        lbl.get_style_context().add_class(css_class)
    lbl.set_xalign(xalign)
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


def section_header(title: str, subtitle: str) -> Gtk.Widget:
    """`TITLE  subtitle` — minimal cyan title + dim subtitle, no rail."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.set_margin_top(2)
    title_lbl = Gtk.Label()
    title_lbl.set_markup(
        f"<span color='{theme.CYAN_BRIGHT}' weight='bold' "
        f"letter_spacing='2048'>{title}</span>"
    )
    title_lbl.get_style_context().add_class("panel-title")
    title_lbl.set_valign(Gtk.Align.CENTER)
    sub_lbl = plain_label(subtitle, "panel-subtitle")
    row.pack_start(title_lbl, False, False, 0)
    row.pack_start(sub_lbl, False, False, 0)
    return row


class TabBar(Gtk.EventBox):
    """Letter-spaced tab strip with a single cyan rail under the active tab.
    Hover hint in violet, idle in muted. Calls `on_switch(name)` on change."""

    TAB_W = 132
    TAB_H = 32

    def __init__(self, names: list[str], on_switch) -> None:
        super().__init__()
        self.set_visible_window(False)
        self._on_switch = on_switch
        self._active = names[0]
        self._hover: str | None = None
        self._names = list(names)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.set_halign(Gtk.Align.START)
        self._tabs: dict[str, Gtk.EventBox] = {}
        for name in names:
            ev = Gtk.EventBox()
            ev.set_visible_window(False)
            ev.set_size_request(self.TAB_W, self.TAB_H)
            ev.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.ENTER_NOTIFY_MASK
                | Gdk.EventMask.LEAVE_NOTIFY_MASK
            )
            lbl = Gtk.Label()
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_xalign(0.5)
            ev.add(lbl)
            ev._lbl = lbl          # type: ignore[attr-defined]
            ev._name = name        # type: ignore[attr-defined]
            ev.connect("button-press-event", self._on_click, name)
            ev.connect("enter-notify-event", self._on_enter, name)
            ev.connect("leave-notify-event", self._on_leave, name)
            row.pack_start(ev, False, False, 0)
            self._tabs[name] = ev
        self.add(row)
        self.connect_after("draw", self._draw_rail)
        self._refresh()

    def set_active(self, name: str) -> None:
        """Programmatic select — does not fire `on_switch`."""
        if name not in self._tabs or name == self._active:
            return
        self._active = name
        self._refresh()
        self.queue_draw()

    def _on_click(self, _w, _e, name: str) -> bool:
        self._select(name)
        return True

    def _on_enter(self, _w, _e, name: str) -> bool:
        self._hover = name
        self._refresh()
        return False

    def _on_leave(self, _w, _e, _name: str) -> bool:
        self._hover = None
        self._refresh()
        return False

    def _select(self, name: str) -> None:
        if name == self._active:
            return
        self._active = name
        self._refresh()
        self.queue_draw()
        self._on_switch(name)

    def _refresh(self) -> None:
        for name, ev in self._tabs.items():
            active = name == self._active
            hovered = name == self._hover and not active
            if active:
                color = theme.CYAN_BRIGHT
            elif hovered:
                color = theme.VIOLET_BRIGHT
            else:
                color = theme.BASE_MUTED
            ev._lbl.set_markup(  # type: ignore[attr-defined]
                f"<span color='{color}' weight='bold' "
                f"letter_spacing='2048'>{name}</span>"
            )

    def _draw_rail(self, w, cr) -> bool:
        alloc = w.get_allocation()
        height = alloc.height
        idx = self._names.index(self._active)
        x0 = idx * self.TAB_W + 10
        seg_w = self.TAB_W - 20
        paint(cr, theme.CYAN_BRIGHT, 0.9)
        cr.rectangle(x0, height - 2, seg_w, 1.5)
        cr.fill()
        return False


class HudCard(Gtk.EventBox):
    """Translucent panel with the popup's bevel + a left-edge accent
    stripe that wraps the bottom-left corner cut."""

    BEVEL = 14
    STRIPE = 2.0

    def __init__(
        self,
        child: Gtk.Widget,
        *,
        accent: str = theme.HIGHLIGHT,
        fill: str | None = None,
        fill_alpha: float = 0.6,
    ) -> None:
        super().__init__()
        self.set_visible_window(False)
        self._accent = accent
        self._fill = fill or theme.BASE_BLACK
        self._fill_alpha = fill_alpha
        child.set_margin_top(10)
        child.set_margin_bottom(10)
        child.set_margin_start(16)
        child.set_margin_end(14)
        self.add(child)
        self.connect("draw", self._draw_card)

    def _draw_card(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        b = self.BEVEL

        beveled_path(cr, width, height, bevel=b,
                     corners=("top-right", "bottom-left"))
        paint(cr, self._fill, self._fill_alpha)
        cr.fill()

        # Accent band that follows the left edge and the bottom-left
        # bevel diagonal — wraps the corner instead of ending square.
        s = self.STRIPE
        paint(cr, self._accent, 0.95)
        cr.move_to(0,         0)
        cr.line_to(s,         0)
        cr.line_to(s,         height - b)
        cr.line_to(s + b,     height)
        cr.line_to(b,         height)
        cr.line_to(0,         height - b)
        cr.close_path()
        cr.fill()
        return False
