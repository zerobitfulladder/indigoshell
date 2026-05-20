"""Notification stack window — bottom-left, matches dunst origin.

A single floating Gtk.Window hosts a vertical stack of NotificationToast
widgets. Owns the D-Bus service and per-toast dismiss timers.
"""

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

from .. import theme
from ..services.notifications import (
    Notification,
    NotificationServer,
    REASON_CLOSED,
    URGENCY_CRITICAL,
    URGENCY_LOW,
)
from ..widgets.notification import NotificationToast, _frame_color
from .base import WindowKind


_CSS = f"""
.notif {{
    /* Background is painted by NotificationToast._draw_background so the
       fill follows the beveled corners; using CSS background-color here
       would paint a full rectangle that leaks through the cut corners. */
    background-color: transparent;
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

        self._toasts: dict[int, NotificationToast] = {}
        # Hovering any toast pauses the dismiss timer on ALL of them;
        # leaving the last one resumes everyone.
        self._hover_count = 0

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
        """Anchor bottom-right, offset (NOTIF_OFFSET_X, NOTIF_OFFSET_Y)."""
        screen = Gdk.Screen.get_default()
        monitor = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor)
        self._box.show_all()
        _, natural = self.get_preferred_size()
        w = natural.width  if natural.width  > 0 else theme.NOTIF_WIDTH
        h = natural.height if natural.height > 0 else 0
        x = geo.x + geo.width  - theme.NOTIF_OFFSET_X - w
        y = geo.y + geo.height - theme.NOTIF_OFFSET_Y - h
        self.move(x, y)

    # ── server callbacks ────────────────────────────────────────────
    def _on_notify(self, notif: Notification) -> None:
        timeout_ms = self._resolve_timeout(notif)
        existing = self._toasts.get(notif.id)
        if existing is not None:
            _apply_urgency_class(existing, notif.urgency)
            existing.update(notif, timeout_ms)
            if self._hover_count > 0:
                existing.set_paused(True)
            self._reposition()
            return

        toast = NotificationToast(
            notif,
            on_dismiss=self._on_dismiss,
            on_action=self._on_action,
            on_hover_change=self._on_hover_change,
            timeout_ms=timeout_ms,
        )
        if self._hover_count > 0:
            toast.set_paused(True)
        _apply_urgency_class(toast, notif.urgency)
        # Newest at the bottom (closest to the bar/screen edge).
        self._box.pack_start(toast, False, False, 0)
        toast.show_all()
        self._toasts[notif.id] = toast
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
    def _resolve_timeout(self, notif: Notification) -> int:
        """Sender's expire_timeout (ms), falling back to urgency defaults
        when negative. Returns 0 to mean "never auto-dismiss"."""
        timeout = notif.expire_timeout
        if timeout < 0:
            if notif.urgency == URGENCY_CRITICAL:
                return theme.NOTIF_TIMEOUT_CRITICAL
            if notif.urgency == URGENCY_LOW:
                return theme.NOTIF_TIMEOUT_LOW_MS
            return theme.NOTIF_TIMEOUT_NORMAL_MS
        return timeout

    def _on_hover_change(self, hovered: bool) -> None:
        if hovered:
            self._hover_count += 1
            if self._hover_count == 1:
                for toast in self._toasts.values():
                    toast.set_paused(True)
        else:
            self._hover_count = max(0, self._hover_count - 1)
            if self._hover_count == 0:
                for toast in self._toasts.values():
                    toast.set_paused(False)

    def _remove(self, nid: int, reason: int) -> None:
        toast = self._toasts.pop(nid, None)
        if toast is None:
            return
        if toast._hovered:
            # Toast is being destroyed without a leave-notify; release
            # its hover credit so the rest don't stay paused forever.
            self._on_hover_change(False)
        self._box.remove(toast)
        toast.destroy()
        self._server.emit_closed(nid, reason)
        if self._toasts:
            self._reposition()
        else:
            self._hide_if_empty()

    def _on_destroy(self, _w):
        for toast in self._toasts.values():
            toast.stop_timer()
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
