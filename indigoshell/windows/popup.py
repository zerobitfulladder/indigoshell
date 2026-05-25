import math
import time
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from ..style import child_style_to_css, style_to_css
from ..widgets.base import Widget, beveled_path
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
        corner: str | None = None,
        corner_margin: tuple[int, int] = (0, 0),
        # Horizontal anchoring against the bar widget:
        #   "auto"  — left-align if the popup fits, else right-align (legacy)
        #   "right" — popup's left edge aligns with the anchor's left edge
        #             (extends to the right; matches the bar widget start-x)
        #   "left"  — popup's right edge aligns with the anchor's right edge
        #             (extends to the left; matches the bar widget end-x)
        align: str = "auto",
        persistent: bool = False,
        # Default panel look: tinted semi-transparent bg with picom
        # blur behind it, beveled corners, cyan stroked border.
        # Transparent window so Cairo controls the popup chrome.
        transparent: bool = True,
        blur: bool = True,
        bg: str | None = theme.POPUP_BG,
        bevel: int = theme.POPUP_BEVEL,
        bevel_corners: tuple[str, ...] = theme.POPUP_BEVEL_CORNERS,
        border: str | None = theme.POPUP_BORDER,
        border_thick: float = 1.5,
        # Pulse the border alpha while mapped. Period in seconds.
        glow: bool = True,
        glow_period: float = 2.4,
        glow_min_alpha: float = 0.45,
        grab: bool = False,
        # Close the popup when the user clicks outside its window. Uses a
        # pointer-only seat grab so the keyboard stays free for other apps,
        # but the popup intercepts every button-press until released. As a
        # side effect, mouse motion doesn't generate enter events on other
        # windows, so focus-follow-mouse setups don't accidentally close the
        # popup just because the cursor drifted away.
        close_on_outside_click: bool = False,
        padding: int = 0,
        wm_class: str = "indigoshell-popup",
        # WM type hint. DIALOG is friendly to keep_above on most WMs;
        # UTILITY is stickier on a few that demote DIALOG when another
        # window claims focus. NOTIFICATION is most semantically correct
        # for toasts but some compositors render it non-interactive.
        type_hint: "Gdk.WindowTypeHint" = Gdk.WindowTypeHint.DIALOG,
    ) -> None:
        self.name = name
        self.content = content
        self.offset = offset
        self.edge_margin = edge_margin
        self.corner = corner
        self.corner_margin = corner_margin
        self.align = align if align in ("auto", "left", "right") else "auto"
        self.persistent = persistent
        self.transparent = transparent
        self.blur = blur
        self.bg = bg
        self.bevel = max(0, bevel)
        self.bevel_corners = tuple(bevel_corners)
        self.border = border
        self.border_thick = max(0.0, border_thick)
        self.glow = glow and border is not None
        self.glow_period = max(0.3, float(glow_period))
        self.glow_min_alpha = max(0.0, min(1.0, float(glow_min_alpha)))
        self._glow_started_at: float = 0.0
        self._glow_timer_id: int | None = None
        self.grab = grab
        self.close_on_outside_click = close_on_outside_click
        self._seat: Any = None
        self.padding = padding
        self.wm_class = wm_class
        self.type_hint = type_hint
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
        self._release_grab()
        if self.persistent:
            window.hide()
        else:
            for w in self.content.walk():
                w.stop()
            window.destroy()
            self._cached = None

    # ── seat grab ────────────────────────────────────────────────────────
    def _on_map_grab(self, win: Gtk.Window, _event) -> bool:
        gdk_window = win.get_window()
        display = Gdk.Display.get_default()
        if gdk_window is None or display is None:
            return False
        seat = display.get_default_seat()
        # owner_events=True: events over our own widgets are dispatched
        # normally (so Menu rows keep receiving hover/click); external
        # input is redirected to the popup window.
        status = seat.grab(
            gdk_window,
            Gdk.SeatCapabilities.ALL,
            True,
            None, None, None,
        )
        if status == Gdk.GrabStatus.SUCCESS:
            self._seat = seat
        # present_with_time + explicit raise: qtile ignores keep_above for
        # inter-floating stacking, but does honor _NET_ACTIVE_WINDOW with a
        # valid timestamp. Without this, a new popup can map *under* an
        # already-open floating window until that window's state changes.
        win.present_with_time(Gdk.CURRENT_TIME)
        gdk_window.raise_()
        return False

    def _release_grab(self) -> None:
        if self._seat is not None:
            self._seat.ungrab()
            self._seat = None

    def _on_outside_click(self, _w, _event) -> bool:
        # Reached only for clicks that weren't consumed by a child widget,
        # i.e. clicks outside the menu content (redirected to us by the grab).
        if self._daemon is not None:
            self._daemon.close(self.name)
        return True

    def _on_outside_click_geom(self, win: Gtk.Window, event) -> bool:
        # Under a pointer-only seat grab with owner_events=True, presses on
        # other windows arrive here with coordinates relative to `win`. Inside
        # the popup, presses on a child widget would normally be consumed
        # before bubbling up — but presses on empty padding/gaps also reach
        # here, and we don't want those to close the panel. So only close
        # when the coordinates fall outside the popup's allocated rect.
        alloc = win.get_allocation()
        if 0 <= event.x < alloc.width and 0 <= event.y < alloc.height:
            return False
        if self._daemon is not None:
            self._daemon.close(self.name)
        return True

    def _on_map_grab_pointer(self, win: Gtk.Window, _event) -> bool:
        # Pointer-only seat grab: catches clicks outside the popup so we can
        # close it, but leaves the keyboard alone so the user can keep typing
        # in whatever app they had focused. With owner_events=True, clicks
        # inside the popup dispatch to its children normally; clicks outside
        # arrive at the popup's button-press-event → _on_outside_click.
        gdk_window = win.get_window()
        display = Gdk.Display.get_default()
        if gdk_window is None or display is None:
            return False
        seat = display.get_default_seat()
        status = seat.grab(
            gdk_window,
            Gdk.SeatCapabilities.ALL_POINTING,
            True,
            None, None, None,
        )
        if status == Gdk.GrabStatus.SUCCESS:
            self._seat = seat
        win.present_with_time(Gdk.CURRENT_TIME)
        gdk_window.raise_()
        return False

    # ── construction ─────────────────────────────────────────────────────
    def _construct(self, anchor: Gtk.Widget | None) -> Gtk.Window:
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_decorated(False)
        win.set_type_hint(self.type_hint)
        win.set_keep_above(True)
        # stick() == _NET_WM_STATE_STICKY: appear on every workspace,
        # not just the one this popup was spawned on. Critical for
        # toasts/runners triggered by a global keybind from any group.
        win.stick()
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_wmclass(self.wm_class, self.wm_class)
        win.get_style_context().add_class("indigo-popup-window")
        self._install_window_css()

        if self.transparent:
            screen = win.get_screen()
            visual = screen.get_rgba_visual()
            if visual:
                win.set_visual(visual)
            win.set_app_paintable(True)
            if self.bg is not None or self.border is not None:
                win.connect("draw", self._draw_bg)
                win.connect_after("draw", self._draw_overlay)

        if self.glow:
            win.connect("map",   self._on_glow_map)
            win.connect("unmap", self._on_glow_unmap)

        if self.blur:
            win.connect("realize", self._apply_blur)

        if anchor is not None:
            toplevel = anchor.get_toplevel()
            if isinstance(toplevel, Gtk.Window):
                win.set_transient_for(toplevel)

        win.add_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        win.connect("key-press-event", self._on_key)
        if self.grab:
            win.connect("map-event", self._on_map_grab)
            win.connect("button-press-event", self._on_outside_click)
        if self.close_on_outside_click and not self.grab:
            win.connect("map-event", self._on_map_grab_pointer)
            win.connect("button-press-event", self._on_outside_click_geom)

        gtk_child = self.content.build()
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

        # Start AFTER parenting so widgets that need to reach their
        # toplevel (e.g. Menu wiring key handlers) see the popup window.
        self.content.start()

        # Persistent: intercept WM close so the window hides instead of dying.
        # Non-persistent: stop content widgets on destroy.
        if self.persistent:
            win.connect("delete-event", self._on_delete)
        else:
            win.connect("destroy", self._on_destroyed)

        return win

    # ── positioning ──────────────────────────────────────────────────────
    def _position(self, win: Gtk.Window, anchor: Gtk.Widget | None) -> None:
        # Cached persistent popups: the previous allocation may be
        # stale if the child tree changed while hidden (e.g. tray rows
        # added/removed). Pre-resize to the child's current preferred
        # size so the post-show `get_size()` reflects the new natural
        # size, not the old allocation.
        if self.persistent and win.get_realized():
            child = win.get_child()
            if child is not None:
                _min, nat = child.get_preferred_size()
                win.resize(max(1, nat.width), max(1, nat.height))
        # Map off-screen first so the WM doesn't briefly center the window;
        # then read the actual allocated size and move to the correct spot.
        win.move(-100000, -100000)
        win.show_all()
        pw, ph = win.get_size()
        self._place(win, anchor, pw, ph)
        # Raise above any other floating windows. See _on_map_grab for why
        # keep_above alone isn't enough on qtile. Non-grab popups don't run
        # the map handler, so the same fix has to live here too.
        win.present_with_time(Gdk.CURRENT_TIME)
        gdk_win = win.get_window()
        if gdk_win is not None:
            gdk_win.raise_()

    def refit(self, win: Gtk.Window, anchor: Gtk.Widget | None) -> None:
        """Resize an already-mapped (or hidden cached) window to its
        natural content size and reposition. Used when a persistent
        popup's content changes (tray items add/remove).

        Queries the *child's* preferred size — `Gtk.Window` caches its
        own preferred size from the last allocation cycle, so asking
        the window directly on a cached popup returns the stale size
        after the child tree has changed."""
        child = win.get_child()
        if child is None:
            return
        _min, nat = child.get_preferred_size()
        pw = max(1, nat.width)
        ph = max(1, nat.height)
        win.resize(pw, ph)
        self._place(win, anchor, pw, ph)

    def _place(self, win: Gtk.Window, anchor: Gtk.Widget | None, pw: int, ph: int) -> None:
        display = Gdk.Display.get_default()

        if anchor is None or anchor.get_window() is None:
            monitor = display.get_primary_monitor() if display else None
            if monitor is None:
                return
            geo = monitor.get_geometry()
            if self.corner is not None:
                self._move_to_corner(win, pw, ph, geo)
            else:
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

            x_left = src_x                # popup's left edge at anchor's left
            x_right = src_right - pw      # popup's right edge at anchor's right
            if self.align == "right":
                x = x_left
            elif self.align == "left":
                x = x_right
            else:  # auto: prefer left-align unless it would clip the screen
                x = x_left if x_left + pw <= geo.x + geo.width - m else x_right

            source_mid_y = sy + alloc.y + alloc.height // 2
            anchor_above = source_mid_y > geo.y + geo.height // 2
            y = above_y if anchor_above else below_y

            x = max(geo.x + m, min(x, geo.x + geo.width - pw - m))
            y = max(geo.y + m, min(y, geo.y + geo.height - ph - m))
        else:
            x = src_x
            y = below_y

        win.move(x, y)

    def _move_to_corner(self, win: Gtk.Window, pw: int, ph: int, geo) -> None:
        mx, my = self.corner_margin
        corner = self.corner or ""
        x = geo.x + mx if corner.endswith("left") else geo.x + geo.width - pw - mx
        y = geo.y + my if corner.startswith("top") else geo.y + geo.height - ph - my
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
    def _draw_bg(self, w, cr) -> bool:
        """Paint the panel fill before GTK renders child widgets."""
        import cairo

        from ..style import css_color

        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height

        # Clear the whole surface first; the overlay pass paints the bevel
        # cutouts/border after children render.
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_SOURCE)

        beveled = self.bevel > 0 and self.bevel_corners
        line_w = self.border_thick if self.border else 0.0
        inset = line_w / 2  # single inset for both fill and stroke (original)

        # ── fill ──
        if self.bg is not None:
            rgba = Gdk.RGBA()
            ok = rgba.parse(css_color(self.bg))
            if not ok:
                rgba.parse("rgba(0,0,0,0.7)")
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
            if beveled:
                beveled_path(
                    cr, width, height,
                    bevel=self.bevel,
                    corners=self.bevel_corners,
                    inset=inset,
                )
                cr.fill()
            else:
                cr.paint()

        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    def _draw_overlay(self, w, cr) -> bool:
        """Paint cut corners and border above child widgets."""
        import cairo

        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height

        beveled = self.bevel > 0 and self.bevel_corners
        line_w = self.border_thick if self.border else 0.0
        inset = line_w / 2

        border_rgba = self._border_rgba() if line_w > 0 else None

        # Picom blurs the popup's rectangular bounds. Instead of leaving
        # cut corners transparent, paint them with the same pulsing accent
        # as the border so the rectangle blur is intentionally masked.
        if beveled:
            if border_rgba is not None:
                cr.set_operator(cairo.OPERATOR_SOURCE)
                cr.set_source_rgba(*border_rgba)
            else:
                cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.rectangle(0, 0, width, height)
            beveled_path(
                cr, width, height,
                bevel=self.bevel,
                corners=self.bevel_corners,
                inset=inset,
            )
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.fill()
            cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        cr.set_operator(cairo.OPERATOR_OVER)

        # ── border stroke (alpha pulses while glow is on) ──
        if border_rgba is not None:
            cr.set_source_rgba(*border_rgba)
            cr.set_line_width(line_w)
            if beveled:
                beveled_path(
                    cr, width, height,
                    bevel=self.bevel,
                    corners=self.bevel_corners,
                    inset=inset,
                )
            else:
                cr.rectangle(inset, inset, width - line_w, height - line_w)
            cr.stroke()
        return False

    def _border_rgba(self) -> tuple[float, float, float, float] | None:
        if self.border is None:
            return None
        from ..style import css_color

        rgba = Gdk.RGBA()
        if not rgba.parse(css_color(self.border)):
            rgba.parse("rgba(255,255,255,1.0)")
        phase = self._glow_phase_value() if self.glow else 1.0
        alpha_mul = self.glow_min_alpha + (1.0 - self.glow_min_alpha) * phase
        return rgba.red, rgba.green, rgba.blue, rgba.alpha * alpha_mul

    # ── border glow ──────────────────────────────────────────────────
    def _glow_phase_value(self) -> float:
        """0..1 cosine envelope: 0 at the trough, 1 at the peak.
        Time-based so it stays in phase regardless of frame rate."""
        if self._glow_started_at == 0.0:
            return 1.0
        t = time.monotonic() - self._glow_started_at
        return 0.5 - 0.5 * math.cos(2 * math.pi * t / self.glow_period)

    def _on_glow_map(self, _win) -> None:
        self._glow_started_at = time.monotonic()
        if self._glow_timer_id is None:
            # 33ms ≈ 30fps — high enough for a smooth pulse, low enough
            # that the queue_draw doesn't burn CPU. Per-popup timer; the
            # `_daemon.instances` lookup gives us the active window
            # for both persistent and non-persistent kinds.
            self._glow_timer_id = GLib.timeout_add(33, self._glow_tick)

    def _on_glow_unmap(self, _win) -> None:
        self._stop_glow_timer()

    def _glow_tick(self) -> bool:
        if self._daemon is None:
            return False
        win = self._daemon.instances.get(self.name)
        if win is None:
            self._glow_timer_id = None
            return False
        win.queue_draw()
        return True

    def _stop_glow_timer(self) -> None:
        if self._glow_timer_id is not None:
            GLib.source_remove(self._glow_timer_id)
            self._glow_timer_id = None

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

    def _install_window_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
window.indigo-popup-window {
    background-color: transparent;
    background-image: none;
}
""")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

    def _apply_blur(self, win) -> None:
        try:
            from Xlib import Xatom
            from Xlib import display as xdisplay
        except ImportError:
            return

        gdk_window = win.get_window()
        if gdk_window is None:
            return
        xid = gdk_window.get_xid()
        try:
            d = xdisplay.Display()
        except Exception:
            return
        try:
            xwin = d.create_resource_object("window", xid)
            # Picom v13 uses this atom as a per-window blur opt-in. The
            # region is intentionally full-window; bevel cutouts are masked
            # in our Cairo overlay because Picom blurs rectangular bounds.
            xwin.change_property(
                d.intern_atom("_KDE_NET_WM_BLUR_BEHIND_REGION"),
                Xatom.CARDINAL, 32, [0, 0, 32767, 32767],
            )
            d.sync()
        finally:
            d.close()


