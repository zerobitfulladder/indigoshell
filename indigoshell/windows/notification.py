"""Notification stack window — bottom-left, matches dunst origin.

A single floating Gtk.Window hosts a vertical stack of NotificationToast
widgets. Owns the D-Bus service and per-toast dismiss timers.
"""

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from ..services.notifications import (
    Notification,
    NotificationServer,
    REASON_CLOSED,
    REASON_EXPIRED,
    URGENCY_CRITICAL,
    URGENCY_LOW,
)
from ..widgets.notification import NotificationToast, _frame_color
from .base import WindowKind


_CSS = f"""
.notif {{
    background-color: {theme.NOTIF_BG};
    border-radius: 0;
}}
.notif-action {{
    background-image: none;
    background-color: {theme.NOTIF_ACTION_BG};
    color: {theme.NOTIF_ACTION_FG};
    border: 0;
    border-radius: 0;
    padding: 4px 10px;
    box-shadow: none;
}}
.notif-action:hover {{
    background-color: {theme.NOTIF_FRAME_NORMAL};
    color: {theme.BASE_BLACK};
}}
"""


class NotificationStack(Gtk.Window):
    """Floating stack window. Stays hidden when empty."""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.stick()

        # Transparent window: only the toast frames paint. The gap
        # between stacked toasts shows the desktop, like dunst.
        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        # Install the toast CSS once, app-wide.
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=theme.NOTIF_GAP)
        self.add(self._box)
        self._box.show()

        # id → (toast widget, timer source id or None)
        self._toasts: dict[int, tuple[NotificationToast, int | None]] = {}

        self._server = NotificationServer(
            on_notify=self._on_notify,
            on_close_request=self._on_close_request,
        )
        self._server.start()

        self.connect("destroy", self._on_destroy)

    # daemon.open() calls show_all() unconditionally; gate it.
    def show_all(self):
        if self._toasts:
            super().show_all()

    def _present(self) -> None:
        self._box.show_all()
        super().show_all()
        self._reposition()

    def _hide_if_empty(self) -> None:
        if not self._toasts:
            self.hide()

    def _reposition(self) -> None:
        """Anchor bottom-left, offset (NOTIF_OFFSET_X, NOTIF_OFFSET_Y)."""
        screen = Gdk.Screen.get_default()
        monitor = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor)
        self._box.show_all()
        _, natural = self.get_preferred_size()
        w = natural.width  if natural.width  > 0 else theme.NOTIF_WIDTH
        h = natural.height if natural.height > 0 else 0
        x = geo.x + theme.NOTIF_OFFSET_X
        y = geo.y + geo.height - theme.NOTIF_OFFSET_Y - h
        self.move(x, y)

    # ── server callbacks ────────────────────────────────────────────
    def _on_notify(self, notif: Notification) -> None:
        existing = self._toasts.get(notif.id)
        if existing is not None:
            toast, timer_id = existing
            if timer_id is not None:
                GLib.source_remove(timer_id)
            _apply_urgency_class(toast, notif.urgency)
            toast.update(notif)
            new_timer = self._schedule_timeout(notif)
            self._toasts[notif.id] = (toast, new_timer)
            self._reposition()
            return

        toast = NotificationToast(
            notif,
            on_dismiss=self._on_dismiss,
            on_action=self._on_action,
        )
        _apply_urgency_class(toast, notif.urgency)
        # Newest at the bottom (closest to the bar/screen edge).
        self._box.pack_start(toast, False, False, 0)
        toast.show_all()
        timer_id = self._schedule_timeout(notif)
        self._toasts[notif.id] = (toast, timer_id)
        self._present()

    def _on_close_request(self, nid: int) -> None:
        self._remove(nid, REASON_CLOSED)

    # ── toast callbacks ─────────────────────────────────────────────
    def _on_dismiss(self, nid: int, reason: int) -> None:
        self._remove(nid, reason)

    def _on_action(self, nid: int, key: str) -> None:
        self._server.emit_action(nid, key)
        self._remove(nid, REASON_CLOSED)

    # ── lifetime ────────────────────────────────────────────────────
    def _schedule_timeout(self, notif: Notification) -> int | None:
        timeout = notif.expire_timeout
        if timeout < 0:
            if notif.urgency == URGENCY_CRITICAL:
                timeout = theme.NOTIF_TIMEOUT_CRITICAL
            elif notif.urgency == URGENCY_LOW:
                timeout = theme.NOTIF_TIMEOUT_LOW_MS
            else:
                timeout = theme.NOTIF_TIMEOUT_NORMAL_MS
        if timeout == 0:
            return None  # spec: 0 means never
        nid = notif.id
        return GLib.timeout_add(timeout, lambda: self._expire(nid))

    def _expire(self, nid: int) -> bool:
        self._remove(nid, REASON_EXPIRED)
        return False

    def _remove(self, nid: int, reason: int) -> None:
        entry = self._toasts.pop(nid, None)
        if entry is None:
            return
        toast, timer_id = entry
        if timer_id is not None:
            GLib.source_remove(timer_id)
        self._box.remove(toast)
        toast.destroy()
        self._server.emit_closed(nid, reason)
        if self._toasts:
            self._reposition()
        else:
            self._hide_if_empty()

    def _on_destroy(self, _w):
        for _toast, timer_id in self._toasts.values():
            if timer_id is not None:
                GLib.source_remove(timer_id)
        self._toasts.clear()
        self._server.stop()


def _apply_urgency_class(toast: NotificationToast, urgency: int) -> None:
    ctx = toast.get_style_context()
    for cls in ("urgency-low", "urgency-normal", "urgency-critical"):
        ctx.remove_class(cls)
    cls = {
        URGENCY_LOW:      "urgency-low",
        URGENCY_CRITICAL: "urgency-critical",
    }.get(urgency, "urgency-normal")
    ctx.add_class(cls)


class NotificationKind(WindowKind):
    name = "notifications"
    autostart = True
    singleton = True

    def build(self, store, params, *, anchor=None, config=None):
        return NotificationStack()

    def teardown(self, window):
        window.destroy()
