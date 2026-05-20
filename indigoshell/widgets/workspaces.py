import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from Xlib import X, display
from Xlib.protocol import event as xevent

from .. import theme
from ..style import Style
from .base import Widget, paint


class Workspaces(Widget):
    """Workspace indicators driven by EWMH root-window properties.

    Listens to PropertyNotify on the root window for _NET_CURRENT_DESKTOP,
    _NET_NUMBER_OF_DESKTOPS, and _NET_CLIENT_LIST, so updates are event-driven
    (no polling). Each indicator is clickable and switches via _NET_CURRENT_DESKTOP.

    CSS classes added per-indicator: `.current`, `.occupied`, `.empty`.
    """

    def __init__(
        self,
        label: str = "",
        style: Style | None = None,
        **kwargs,
    ):
        kwargs.setdefault("on_scroll_up", self._scroll_prev)
        kwargs.setdefault("on_scroll_down", self._scroll_next)
        super().__init__(style, **kwargs)
        self.label = label
        self.box: Gtk.Box | None = None
        self._display = None
        self._root = None
        self._watch_atoms: set = set()
        self._pending_desktop: int | None = None
        self._indicators: list[Gtk.EventBox] = []
        self._refresh_scheduled: bool = False
        self._clients_dirty: bool = True
        self._client_desktop_cache: dict[int, int] = {}
        self._client_urgent_cache: dict[int, bool] = {}
        self._watched_clients: set[int] = set()
        self._wm_desktop_atom = None
        self._wm_state_atom = None
        self._demands_attention_atom = None
        self._last_current: int | None = None
        # Ring-pattern blink for urgent workspaces.
        self._ring_idx: int = 0
        self._ring_visible: bool = True
        self._ring_timer: int | None = None
        self._any_urgent: bool = False

    def build_widget(self):
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        return self.box

    def default_css(self):
        return ""

    def _draw_indicator(self, area, cr, state) -> bool:
        alloc = area.get_allocation()
        w, h = alloc.width, alloc.height

        bars = 4
        gap_ratio = 0.35
        # Total drawn = bars * bar_h + (bars - 1) * gap = bar_h * (bars + (bars-1) * gap_ratio).
        bar_h = max(2.0, h / (bars + (bars - 1) * gap_ratio))
        gap = max(1.0, bar_h * gap_ratio)
        bar_w = w * 0.75
        x = (w - bar_w) / 2
        total = bar_h * bars + gap * (bars - 1)
        y0 = (h - total) / 2

        count = min(state["count"], bars)
        is_current = state["kind"] == "current"
        is_urgent = state.get("urgent", False)
        urgent_active = is_urgent and self._ring_visible

        if urgent_active:
            occupied_color = theme.WORKSPACE_URGENT_FG
            empty_color = theme.WORKSPACE_URGENT_FG
        elif is_current:
            occupied_color = theme.CYAN_MID
            empty_color = theme.MAGENTA_MID
        else:
            occupied_color = theme.WORKSPACE_OCCUPIED_FG
            empty_color = theme.WORKSPACE_EMPTY_FG

        for i in range(bars):
            y = y0 + (bars - 1 - i) * (bar_h + gap)
            color = occupied_color if i < count else empty_color
            self._fill_bar(cr, x, y, bar_w, bar_h, color)
        return False

    def _fill_bar(self, cr, x: float, y: float, w: float, h: float, hex_color: str) -> None:
        paint(cr, hex_color)
        cr.rectangle(x, y, w, h)
        cr.fill()

    def start(self):
        self._display = display.Display()
        self._root = self._display.screen().root
        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        self._wm_desktop_atom = self._display.intern_atom("_NET_WM_DESKTOP")
        self._wm_state_atom = self._display.intern_atom("_NET_WM_STATE")
        self._demands_attention_atom = self._display.intern_atom(
            "_NET_WM_STATE_DEMANDS_ATTENTION"
        )
        self._watch_atoms = {
            self._display.intern_atom("_NET_CURRENT_DESKTOP"),
            self._display.intern_atom("_NET_NUMBER_OF_DESKTOPS"),
            self._display.intern_atom("_NET_CLIENT_LIST"),
        }
        self._refresh()
        GLib.io_add_watch(self._display.fileno(), GLib.IO_IN, self._on_x_ready)
        # python-xlib buffers events internally during reply roundtrips
        # (every `_get_root_int` / `_is_window_urgent` call). When that
        # happens the fd goes quiet even though PropertyNotify events
        # sit in the Python-side queue, and the io_add_watch above
        # misses them — causing the widget to "forget" workspace
        # switches. A low-frequency tick drains anything stranded.
        GLib.timeout_add(50, self._drain_pending)

    def _drain_pending(self) -> bool:
        if self._display is None:
            return False
        if self._display.pending_events():
            self._on_x_ready(None, None)
        return True

    def _on_x_ready(self, _fd, _cond):
        client_list_atom = self._display.intern_atom("_NET_CLIENT_LIST")
        needs_refresh = False
        while self._display.pending_events():
            event = self._display.next_event()
            if event.type != X.PropertyNotify:
                continue
            if event.atom == self._wm_desktop_atom:
                xid = int(getattr(event.window, "id", 0)) or int(event.window)
                self._client_desktop_cache.pop(xid, None)
                self._clients_dirty = True
                needs_refresh = True
                continue
            if event.atom == self._wm_state_atom:
                xid = int(getattr(event.window, "id", 0)) or int(event.window)
                self._client_urgent_cache.pop(xid, None)
                self._clients_dirty = True
                needs_refresh = True
                continue
            if event.atom in self._watch_atoms:
                if event.atom == client_list_atom:
                    self._clients_dirty = True
                needs_refresh = True
        if needs_refresh:
            self._schedule_refresh()
        return True  # keep watching

    def _watch_client(self, xid: int) -> None:
        if xid in self._watched_clients:
            return
        try:
            w = self._display.create_resource_object("window", xid)
            w.change_attributes(event_mask=X.PropertyChangeMask)
            self._watched_clients.add(xid)
        except Exception:
            pass

    def _schedule_refresh(self):
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        GLib.idle_add(self._run_refresh)

    def _run_refresh(self):
        self._refresh_scheduled = False
        self._refresh()
        return False

    def _get_root_int(self, atom_name: str) -> int | None:
        atom = self._display.intern_atom(atom_name)
        prop = self._root.get_full_property(atom, X.AnyPropertyType)
        if prop and prop.value:
            return int(prop.value[0])
        return None

    def _get_root_xids(self, atom_name: str) -> list[int]:
        atom = self._display.intern_atom(atom_name)
        prop = self._root.get_full_property(atom, X.AnyPropertyType)
        if prop and prop.value:
            return list(prop.value)
        return []

    def _get_window_int(self, xid: int, atom_name: str) -> int | None:
        try:
            w = self._display.create_resource_object("window", xid)
            atom = self._display.intern_atom(atom_name)
            prop = w.get_full_property(atom, X.AnyPropertyType)
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        return None

    def _is_window_urgent(self, xid: int) -> bool:
        """Returns True if the window has _NET_WM_STATE_DEMANDS_ATTENTION."""
        try:
            w = self._display.create_resource_object("window", xid)
            prop = w.get_full_property(self._wm_state_atom, X.AnyPropertyType)
            if not prop or not prop.value:
                return False
            return self._demands_attention_atom in list(prop.value)
        except Exception:
            return False

    def _refresh(self) -> bool:
        if self.box is None:
            return False
        n = self._get_root_int("_NET_NUMBER_OF_DESKTOPS") or 0
        current = self._get_root_int("_NET_CURRENT_DESKTOP")
        clients = self._get_root_xids("_NET_CLIENT_LIST")

        if current is not None and current == self._pending_desktop:
            self._pending_desktop = None

        if self._clients_dirty:
            new_desktop: dict[int, int] = {}
            new_urgent: dict[int, bool] = {}
            for xid in clients:
                self._watch_client(xid)
                d = self._client_desktop_cache.get(xid)
                if d is None:
                    d = self._get_window_int(xid, "_NET_WM_DESKTOP")
                if d is not None:
                    new_desktop[xid] = d
                u = self._client_urgent_cache.get(xid)
                if u is None:
                    u = self._is_window_urgent(xid)
                new_urgent[xid] = u
            self._client_desktop_cache = new_desktop
            self._client_urgent_cache = new_urgent
            live = set(clients)
            self._watched_clients &= live
            self._clients_dirty = False

        per_desktop = [0] * n
        urgent_desktop = [False] * n
        for xid, d in self._client_desktop_cache.items():
            if 0 <= d < n:
                per_desktop[d] += 1
                if self._client_urgent_cache.get(xid):
                    urgent_desktop[d] = True

        size = 22
        while len(self._indicators) < n:
            i = len(self._indicators)
            # Empty Box provides allocation without grabbing pointer
            # events (Gtk.DrawingArea would, via its own GdkWindow) and
            # without font-metric height inflation (a Label would).
            filler = Gtk.Box()
            filler.set_size_request(size, size)
            filler.set_valign(Gtk.Align.CENTER)
            filler.set_halign(Gtk.Align.CENTER)
            ev = Gtk.EventBox()
            ev.add(filler)
            ev.set_visible_window(False)
            ev.set_valign(Gtk.Align.CENTER)
            ev.set_halign(Gtk.Align.CENTER)
            ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK)
            ev.connect("button-press-event", lambda _w, _e, idx=i: self._switch(idx))
            ev.connect("scroll-event", self._dispatch_scroll)
            state = {"count": 0, "kind": "empty", "urgent": False}
            ev.connect_after("draw", self._draw_indicator, state)
            ev._indigo_state = state
            ev._indigo_area = ev
            self.box.pack_start(ev, False, False, 0)
            self._indicators.append(ev)

        while len(self._indicators) > n:
            ev = self._indicators.pop()
            self.box.remove(ev)

        if current != self._last_current:
            # On switch, start bright. Beats will keep flipping it if
            # music is playing; otherwise it stays bright.
            self._pulse_on = True
            self._last_current = current

        for i, ev in enumerate(self._indicators):
            if i == current:
                kind = "current"
            elif per_desktop[i] > 0:
                kind = "occupied"
            else:
                kind = "empty"
            ev._indigo_state["count"] = per_desktop[i]
            ev._indigo_state["kind"] = kind
            ev._indigo_state["urgent"] = urgent_desktop[i]
            ev._indigo_area.queue_draw()

        self._sync_urgent_ring(any(urgent_desktop))
        self.box.show_all()
        return False

    # ── urgent "ring" blink ──────────────────────────────────────────
    def _sync_urgent_ring(self, any_urgent: bool) -> None:
        """Start the ring-pattern timer if a workspace just became
        urgent; stop it once no urgent workspaces remain."""
        if any_urgent and not self._any_urgent:
            self._start_ring()
        elif not any_urgent and self._any_urgent:
            self._stop_ring()
        self._any_urgent = any_urgent

    def _start_ring(self) -> None:
        pattern = theme.WORKSPACE_URGENT_RING
        if not pattern:
            self._ring_visible = True
            return
        self._ring_idx = 0
        ms, visible = pattern[0]
        self._ring_visible = visible
        self._ring_timer = GLib.timeout_add(ms, self._ring_tick)

    def _stop_ring(self) -> None:
        if self._ring_timer is not None:
            GLib.source_remove(self._ring_timer)
            self._ring_timer = None
        self._ring_visible = True
        # Repaint so any leftover "off" frame clears immediately.
        for ev in self._indicators:
            ev._indigo_area.queue_draw()

    def _ring_tick(self) -> bool:
        pattern = theme.WORKSPACE_URGENT_RING
        if not pattern:
            self._ring_timer = None
            return False
        self._ring_idx = (self._ring_idx + 1) % len(pattern)
        ms, visible = pattern[self._ring_idx]
        self._ring_visible = visible
        for ev in self._indicators:
            if ev._indigo_state.get("urgent"):
                ev._indigo_area.queue_draw()
        # Re-schedule with the new frame's duration; return False to
        # cancel the implicit re-run of the current timer.
        self._ring_timer = GLib.timeout_add(ms, self._ring_tick)
        return False

    def _scroll_prev(self, _w):
        self._step(-1)

    def _scroll_next(self, _w):
        self._step(1)

    def _step(self, delta: int):
        n = self._get_root_int("_NET_NUMBER_OF_DESKTOPS") or 0
        if n <= 0:
            return
        base = self._pending_desktop
        if base is None:
            base = self._get_root_int("_NET_CURRENT_DESKTOP") or 0
        target = (base + delta) % n
        self._pending_desktop = target
        self._switch(target)

    def _switch(self, idx: int):
        if not self._display or not self._root:
            return
        atom = self._display.intern_atom("_NET_CURRENT_DESKTOP")
        ev = xevent.ClientMessage(
            window=self._root,
            client_type=atom,
            data=(32, [idx, 0, 0, 0, 0]),
        )
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
        self._root.send_event(ev, event_mask=mask)
        self._display.flush()


