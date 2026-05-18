import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk

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

        screen = Gdk.Screen.get_default()
        monitor = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor)
        self.screen_width = geo.width
        self.screen_geo = geo
        y = geo.y if self.position == "top" else geo.y + geo.height - self.height
        self.move(geo.x, y)
        self.set_size_request(self.screen_width, self.height)
        self.set_default_size(self.screen_width, self.height)

        self.connect("realize", self._set_strut)

        self._widgets: list[Widget] = []
        self._build_layout()
        self._apply_css()

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
