from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

from ..style import child_style_to_css, style_to_css
from ..widgets.base import Widget
from .base import WindowKind


class PopupKind(WindowKind):
    """A floating window anchored to a source widget.

    Registered in the daemon by name. Triggered with daemon.toggle(name, anchor=gtk_widget).
    Anchor is the source Gtk.Widget; if None, the popup centers on the primary monitor.

    persistent=True: closing hides (keeps the content + any child processes alive);
                     reopening reuses the cached window.
    persistent=False: closing destroys; reopening builds fresh.
    """

    singleton = True

    def __init__(
        self,
        name: str,
        content: Widget,
        *,
        offset: int = 4,
        edge_margin: int = 0,
        persistent: bool = False,
        transparent: bool = False,
        blur: bool = False,
        bg: str | None = None,
        padding: int = 0,
        wm_class: str = "indigoshell-popup",
    ) -> None:
        self.name = name
        self.content = content
        self.offset = offset
        self.edge_margin = edge_margin
        self.persistent = persistent
        self.transparent = transparent
        self.blur = blur
        self.bg = bg
        self.padding = padding
        self.wm_class = wm_class
        self._cached: Gtk.Window | None = None

    def build(self, store, params: dict, *, anchor: Any = None, config: dict | None = None) -> Gtk.Window:
        if self.persistent and self._cached is not None:
            win = self._cached
        else:
            win = self._construct(anchor)
            if self.persistent:
                self._cached = win
        self._position(win, anchor)
        return win

    def teardown(self, window: Gtk.Window) -> None:
        if self.persistent:
            window.hide()
        else:
            for w in self.content.walk():
                w.stop()
            window.destroy()
            self._cached = None

    # ── construction ─────────────────────────────────────────────────────
    def _construct(self, anchor: Gtk.Widget | None) -> Gtk.Window:
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_decorated(False)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_wmclass(self.wm_class, self.wm_class)

        if self.transparent:
            screen = win.get_screen()
            visual = screen.get_rgba_visual()
            if visual:
                win.set_visual(visual)
            win.set_app_paintable(True)
            if self.bg is not None:
                win.connect("draw", self._draw_bg)

        if self.blur:
            win.connect("realize", self._apply_blur)

        if anchor is not None:
            toplevel = anchor.get_toplevel()
            if isinstance(toplevel, Gtk.Window):
                win.set_transient_for(toplevel)

        win.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        win.connect("key-press-event", self._on_key)

        gtk_child = self.content.build()
        self.content.start()
        self._install_content_css()

        if self.padding:
            wrapper = Gtk.Box()
            wrapper.set_margin_top(self.padding)
            wrapper.set_margin_bottom(self.padding)
            wrapper.set_margin_start(self.padding)
            wrapper.set_margin_end(self.padding)
            wrapper.pack_start(gtk_child, True, True, 0)
            win.add(wrapper)
        else:
            win.add(gtk_child)

        # Persistent: intercept WM close so the window hides instead of dying.
        # Non-persistent: stop content widgets on destroy.
        if self.persistent:
            win.connect("delete-event", self._on_delete)
        else:
            win.connect("destroy", self._on_destroyed)

        return win

    # ── positioning ──────────────────────────────────────────────────────
    def _position(self, win: Gtk.Window, anchor: Gtk.Widget | None) -> None:
        # Map off-screen first so the WM doesn't briefly center the window;
        # then read the actual allocated size and move to the correct spot.
        win.move(-100000, -100000)
        win.show_all()
        pw, ph = win.get_size()

        display = Gdk.Display.get_default()

        if anchor is None or anchor.get_window() is None:
            # No anchor: center on primary monitor.
            monitor = display.get_primary_monitor() if display else None
            if monitor is not None:
                geo = monitor.get_geometry()
                win.move(geo.x + (geo.width - pw) // 2, geo.y + (geo.height - ph) // 2)
            return

        alloc = anchor.get_allocation()
        _ok, sx, sy = anchor.get_window().get_origin()
        src_x = sx + alloc.x
        src_right = src_x + alloc.width

        # Use the anchor's toplevel (the bar) edges as vertical anchor so the
        # popup sits flush with the bar's outer edge, not somewhere inside.
        toplevel = anchor.get_toplevel()
        if isinstance(toplevel, Gtk.Window) and toplevel.get_window():
            _ok2, _bx, by = toplevel.get_window().get_origin()
            _bw, bh = toplevel.get_size()
        else:
            by = sy
            bh = alloc.height

        below_y = by + bh + self.offset
        above_y = by - ph - self.offset

        monitor = display.get_monitor_at_point(src_x, sy + alloc.y) if display else None
        if monitor is not None:
            geo = monitor.get_geometry()
            m = self.edge_margin

            x_left = src_x
            x_right = src_right - pw
            if x_left + pw <= geo.x + geo.width - m:
                x = x_left
            else:
                x = x_right

            source_mid_y = sy + alloc.y + alloc.height // 2
            anchor_above = source_mid_y > geo.y + geo.height // 2
            y = above_y if anchor_above else below_y

            x = max(geo.x + m, min(x, geo.x + geo.width - pw - m))
            y = max(geo.y + m, min(y, geo.y + geo.height - ph - m))
        else:
            x = src_x
            y = below_y

        win.move(x, y)

    # ── event handlers ───────────────────────────────────────────────────
    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if self._daemon is not None:
                self._daemon.close(self.name)
            return True
        return False

    def _on_delete(self, _win, _e) -> bool:
        # Persistent: route through daemon so its instance map stays in sync.
        if self._daemon is not None:
            self._daemon.close(self.name)
        return True  # block default destroy

    def _on_destroyed(self, _w):
        for w in self.content.walk():
            w.stop()

    # ── visuals ──────────────────────────────────────────────────────────
    def _draw_bg(self, _w, cr) -> bool:
        import cairo

        from ..style import css_color

        rgba = Gdk.RGBA()
        ok = rgba.parse(css_color(self.bg or "rgba(0,0,0,0.7)"))
        if not ok:
            rgba.parse("rgba(0,0,0,0.7)")
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        return False

    def _install_content_css(self) -> None:
        css = ""
        for w in self.content.walk():
            css += w.default_css()
            if w.style is not None:
                css += style_to_css(w.name, w.style)
            if w.hover_style is not None:
                css += style_to_css(w.name, w.hover_style, state_class="hover")
            if w.active_style is not None:
                css += style_to_css(w.name, w.active_style, state_class="active")
            for child_class, child_style in w.child_styles.items():
                css += child_style_to_css(w.name, child_class, child_style)
        if not css:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _apply_blur(self, win) -> None:
        from Xlib import Xatom
        from Xlib import display as xdisplay

        gdk_window = win.get_window()
        if gdk_window is None:
            return
        xid = gdk_window.get_xid()
        d = xdisplay.Display()
        try:
            xwin = d.create_resource_object("window", xid)
            xwin.change_property(
                d.intern_atom("_KDE_NET_WM_BLUR_BEHIND_REGION"),
                Xatom.CARDINAL, 32, [],
            )
            d.sync()
        finally:
            d.close()
