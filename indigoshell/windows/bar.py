import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from .. import theme
from ..style import build_css, child_style_to_css, style_to_css
from ..widgets.base import Widget
from .base import WindowKind


class Bar(Gtk.Window):
    def __init__(self, config: dict):
        super().__init__(title="IndigoBar")
        self.config = config
        # Bar-scoped shared services. Widgets fetch them through their
        # own `get_*()` accessors (still singletons), but holding refs
        # here makes ownership explicit and gives us a single teardown
        # point — see BarKind.teardown.
        from ..services.beat import get_detector
        from ..services.music import get_status
        self._cava = get_detector()
        self._music = get_status()
        self.height = config.get("height", theme.BAR_HEIGHT)
        self.position = config.get("position", theme.BAR_POSITION)
        self.margin = config.get("margin", theme.BAR_MARGIN)
        self.font = config.get("font", theme.FONT)
        self.font_size = config.get("font_size", theme.FONT_SIZE)
        self.bar_bg = config.get("background", theme.BAR_BG)
        self.bar_radius = config.get("radius", theme.BAR_RADIUS)
        self.transparent = config.get("transparent", theme.BAR_TRANSPARENT)

        if self.transparent:
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual()
            if visual:
                self.set_visual(visual)
            self.set_app_paintable(True)

        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.stick()

        self.screen_width = 0
        self.screen_geo = None
        self._remap_pending = False
        self._apply_geometry()

        # The display can change under us — resolution switches emit
        # "size-changed", plug/unplug and monitor reconfiguration emit
        # "monitors-changed". The bar caches the primary monitor's
        # geometry, so without these it keeps a stale width/position and
        # reserves the wrong strut. Recompute on either.
        screen = Gdk.Screen.get_default()
        self._screen_handlers = [
            screen.connect("size-changed", self._on_display_changed),
            screen.connect("monitors-changed", self._on_display_changed),
        ]
        self.connect("destroy", self._disconnect_screen)

        self.connect("realize", self._set_strut)
        # Catch bar-level button presses so clicks on empty bar space (or on
        # widgets that don't consume the press) dismiss any open
        # dismiss-on-click panel. Same-client clicks bypass the popup's
        # pointer grab (owner_events=True), so the panel itself wouldn't
        # see these — the bar has to do it.
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_bar_click)

        self._widgets: list[Widget] = []
        self._build_layout()
        self._apply_css()

    def _apply_geometry(self):
        """Size and place the bar against the current primary monitor.

        Safe to call repeatedly: used at construction and whenever the
        display geometry changes.
        """
        screen = Gdk.Screen.get_default()
        monitor = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor)
        self.screen_width = geo.width
        self.screen_geo = geo
        y = geo.y if self.position == "top" else geo.y + geo.height - self.height
        self.set_size_request(self.screen_width, self.height)
        self.set_default_size(self.screen_width, self.height)
        # resize() matters once realized — set_size_request only sets the
        # minimum, the WM won't shrink a DOCK back down on its own.
        if self.get_realized():
            self.resize(self.screen_width, self.height)
        self.move(geo.x, y)

    def _on_display_changed(self, _screen):
        # Never re-stamp the strut on a mapped window: qtile's strut
        # accounting is additive — each _NET_WM_STRUT_PARTIAL write on a
        # managed window adds the full strut to the screen's reserved
        # space without freeing the previous reservation (it only frees
        # on unmanage), so an in-place re-stamp grows the gap by one bar
        # height per display change. Unmap → update → remap instead: the
        # unmap makes the WM release the old reservation, the remap
        # reserves the new one exactly once. size-changed and
        # monitors-changed can both fire for a single xrandr call —
        # coalesce the burst into one remap via an idle callback.
        if self._remap_pending:
            return
        self._remap_pending = True
        GLib.idle_add(self._remap_for_display_change)

    def _remap_for_display_change(self):
        self._remap_pending = False
        if not self.get_realized():
            return False
        # Skip no-op bursts (e.g. re-selecting the already-active
        # output): the strut and bar placement derive entirely from the
        # primary monitor's geometry, so equal geometry means nothing to
        # do — and no bar blink.
        screen = Gdk.Screen.get_default()
        geo = screen.get_monitor_geometry(screen.get_primary_monitor())
        old = self.screen_geo
        if old and (geo.x, geo.y, geo.width, geo.height) == (
            old.x, old.y, old.width, old.height,
        ):
            return False
        self.hide()
        # _set_strut writes through its own X connection; force a
        # round-trip on GDK's connection first so the server has
        # processed the unmap before the strut lands. Otherwise the WM
        # can see the new strut while the window is still managed and
        # double-count the reservation.
        Gdk.Display.get_default().sync()
        self._apply_geometry()
        self._set_strut(self)
        self.show()
        return False

    def _disconnect_screen(self, _w):
        screen = Gdk.Screen.get_default()
        for hid in getattr(self, "_screen_handlers", []):
            screen.disconnect(hid)
        self._screen_handlers = []

    def _on_bar_click(self, _w, _event) -> bool:
        from ..core.daemon import get_daemon
        get_daemon().close_outside_click_popups()
        return False

    def _build_layout(self):
        from ..core.daemon import get_daemon

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_margin_start(self.margin)
        outer.set_margin_end(self.margin)
        self.add(outer)

        daemon = get_daemon()
        for widget in self.config.get("widgets", []):
            outer.pack_start(widget.build(), widget.expand, widget.expand, 0)
            widget.start()
            daemon.register_anchors(widget)
            self._widgets.append(widget)

    def _apply_css(self):
        css = build_css(self.font, self.font_size, self.bar_bg, self.bar_radius)
        for widget in self._widgets:
            for w in widget.walk():
                css += w.default_css()
        for widget in self._widgets:
            for w in widget.walk():
                if w.style is not None:
                    css += style_to_css(w.name, w.style)
                if w.hover_style is not None:
                    css += style_to_css(w.name, w.hover_style, state_class="hover")
                if w.active_style is not None:
                    css += style_to_css(w.name, w.active_style, state_class="active")
                for child_class, child_style in w.child_styles.items():
                    css += child_style_to_css(w.name, child_class, child_style)

        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _set_strut(self, _widget):
        gdk_window = self.get_window()
        if not gdk_window:
            return

        if self.position == "top":
            strut = [0, 0, self.height, 0, 0, 0, 0, 0, 0, self.screen_width - 1, 0, 0]
            strut_basic = [0, 0, self.height, 0]
        else:
            strut = [0, 0, 0, self.height, 0, 0, 0, 0, 0, 0, 0, self.screen_width - 1]
            strut_basic = [0, 0, 0, self.height]

        from Xlib import display as xdisplay, Xatom

        xid = gdk_window.get_xid()
        d = xdisplay.Display()
        try:
            xwin = d.create_resource_object("window", xid)
            xwin.change_property(
                d.intern_atom("_NET_WM_STRUT_PARTIAL"),
                Xatom.CARDINAL, 32, strut,
            )
            xwin.change_property(
                d.intern_atom("_NET_WM_STRUT"),
                Xatom.CARDINAL, 32, strut_basic,
            )
            d.sync()
        finally:
            d.close()


class BarKind(WindowKind):
    name = "bar"
    autostart = True
    singleton = True

    def build(self, store, params, *, anchor=None, config=None):
        return Bar(config or {})

    def teardown(self, window: Gtk.Window) -> None:
        # Stop every child widget so background threads/subprocesses
        # (cava, playerctl --follow, pactl subscribe, ...) get torn
        # down. Without this, daemon reload leaks them as zombies.
        if isinstance(window, Bar):
            for w in window._widgets:
                for sub in w.walk():
                    try:
                        sub.stop()
                    except Exception:
                        pass
            # Force-stop bar-owned brokers after widget unsubscribes,
            # so cava + playerctl die even if a stray subscription
            # somehow lingers.
            try:
                window._cava._stop()
            except Exception:
                pass
            try:
                window._music._stop()
            except Exception:
                pass
        window.destroy()
