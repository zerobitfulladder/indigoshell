"""One notification toast. Mirrors the prior dunst look.

Layout (body wraps under the heading row):

    ┌────────────────────────────────────────────┐
    │ // SUMMARY app_name                        │
    │                                            │
    │ body line 1                                │
    │ body line 2                                │
    │ [Action 1]  [Action 2]                     │
    └────────────────────────────────────────────┘

A thin beveled frame in the urgency color wraps the whole thing (bevel
corners + thickness in theme.NOTIF_BEVEL/_BORDER_THICK; matches the
chord-menu rows). Click anywhere dismisses (matches dunst
`mouse_left_click = close_current`).
"""

import html
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .. import theme
from ..services.notifications import (
    Notification,
    REASON_DISMISSED,
    REASON_EXPIRED,
    URGENCY_CRITICAL,
    URGENCY_LOW,
)
from .base import beveled_path, beveled_polyline, paint, stroke_partial


_FRAME_BY_URGENCY = {
    URGENCY_LOW:      theme.NOTIF_FRAME_LOW,
    URGENCY_CRITICAL: theme.NOTIF_FRAME_CRITICAL,
}

_BODY_FG_BY_URGENCY = {
    URGENCY_CRITICAL: theme.NOTIF_BODY_FG_CRITICAL,
}


def _body_fg(urgency: int) -> str:
    return _BODY_FG_BY_URGENCY.get(urgency, theme.NOTIF_BODY_FG_NORMAL)


def _frame_color(urgency: int) -> str:
    return _FRAME_BY_URGENCY.get(urgency, theme.NOTIF_FRAME_NORMAL)


class NotificationToast(Gtk.EventBox):
    """A single toast. Wraps content in an EventBox so clicks dismiss it."""

    def __init__(
        self,
        notif: Notification,
        on_dismiss: Callable[[int, int], None],
        on_action: Callable[[int, str], None],
        on_hover_change: Callable[[bool], None] | None = None,
        timeout_ms: int = 0,
        show_icon: bool = False,
    ) -> None:
        super().__init__()
        self.notif = notif
        self.on_dismiss = on_dismiss
        self.on_action = on_action
        self.on_hover_change = on_hover_change
        self.show_icon = show_icon
        self._hovered = False
        self._timeout_ms = 0
        self._elapsed_ms = 0
        self._tick_source: int | None = None
        self._last_tick_ms: int | None = None
        self._paused = False
        self.set_visible_window(True)
        # We paint the background ourselves so the fill follows the
        # bevel; tell GTK not to draw its theme/CSS background under us.
        self.set_app_paintable(True)
        self.get_style_context().add_class("notif")

        self._build_ui()
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("button-press-event", self._on_click)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("destroy", self._on_destroy)
        # Two-stage draw: clear-to-transparent + beveled fill BEFORE
        # children render (so labels paint on top); border + meter
        # AFTER, so they sit on top of everything.
        self.connect("draw", self._draw_background)
        self.connect_after("draw", self._draw_decoration)
        self.start_timer(timeout_ms)

    def update(self, notif: Notification, timeout_ms: int) -> None:
        """Mutate this toast in place when an app reuses the id."""
        self.notif = notif
        for child in list(self.get_children()):
            self.remove(child)
        self._build_ui()
        self.show_all()
        self.start_timer(timeout_ms)

    # Lock width at NOTIF_WIDTH — set_size_request is only a MIN, and
    # labels' natural widths would otherwise grow the toast for long
    # unbroken summaries/bodies. Returning the same value for min and
    # natural forces GTK to allocate exactly this; labels inside
    # ellipsize (head) and wrap (body) into the remaining space.
    def do_get_preferred_width(self):
        w = theme.NOTIF_WIDTH
        return (w, w)

    # ── build ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # GTK 3 EventBox doesn't honor CSS padding for child layout —
        # the bg/border still paint via CSS, but inner content sits
        # flush. Push it inward with widget margin.
        col.set_margin_start(theme.NOTIF_PADDING_X)
        col.set_margin_end(theme.NOTIF_PADDING_X)
        col.set_margin_top(theme.NOTIF_PADDING_Y)
        # Reserve room below the body so it doesn't collide with the meter.
        extra = (
            theme.NOTIF_METER_THICK + theme.NOTIF_METER_INSET_Y
            if self.notif.value is not None else 0
        )
        col.set_margin_bottom(theme.NOTIF_PADDING_Y + extra)

        head_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if self.show_icon:
            image = self._build_image()
            if image is not None:
                head_row.pack_start(image, False, False, 0)

        head = Gtk.Label()
        head.set_xalign(0.0)
        head.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        head.set_max_width_chars(1)  # natural width = ~1 char; fills via allocation
        head.set_markup(self._heading_markup())
        head_row.pack_start(head, True, True, 0)
        col.pack_start(head_row, False, False, 0)

        if self.notif.body:
            body = Gtk.Label()
            body.set_xalign(0.0)
            body.set_line_wrap(True)
            body.set_line_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
            body.set_max_width_chars(1)  # wrap to allocation, not to natural text width
            body_markup = (
                f"<span foreground='{_body_fg(self.notif.urgency)}'>"
                f"{self._body_markup()}</span>"
            )
            try:
                body.set_markup(body_markup)
            except Exception:
                body.set_markup(
                    f"<span foreground='{_body_fg(self.notif.urgency)}'>"
                    f"{html.escape(self.notif.body)}</span>"
                )
            col.pack_start(body, False, False, 0)

        # Action row — skip "default" (handled by body click).
        visible_actions = [
            (k, label) for k, label in self.notif.actions if k != "default"
        ]
        if visible_actions:
            actions_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            actions_row.set_margin_top(6)
            for key, label in visible_actions:
                btn = Gtk.Button(label=label)
                btn.get_style_context().add_class("notif-action")
                btn.connect("clicked", self._on_action_click, key)
                actions_row.pack_start(btn, False, False, 0)
            col.pack_start(actions_row, False, False, 0)

        self.add(col)

    def _heading_markup(self) -> str:
        """`// SUMMARY app_name` — matches dunst's `format = ...` recipe."""
        parts = [
            f"<span foreground='{theme.NOTIF_SEPARATOR_FG}' weight='bold'>//</span>",
            f"<span foreground='{theme.NOTIF_SUMMARY_FG}' weight='bold'>"
            f"{html.escape(self.notif.summary or '')}</span>",
        ]
        if self.notif.app_name:
            parts.append(
                f"<span foreground='{theme.NOTIF_APPNAME_FG}'>"
                f"{html.escape(self.notif.app_name)}</span>"
            )
        return " ".join(parts)

    def _body_markup(self) -> str:
        """Spec says body MAY contain a small markup subset. Pango handles
        it; fall back to escaped plain text if parsing fails."""
        return self.notif.body

    def _build_image(self) -> Gtk.Widget | None:
        pixbuf = None
        if self.notif.image_data is not None:
            pixbuf = _pixbuf_from_image_data(self.notif.image_data)
        if pixbuf is None and self.notif.image_path:
            pixbuf = _pixbuf_from_path(self.notif.image_path)
        if pixbuf is None and self.notif.app_icon:
            pixbuf = _pixbuf_from_path(self.notif.app_icon)
            if pixbuf is None:
                img = Gtk.Image.new_from_icon_name(self.notif.app_icon, Gtk.IconSize.DIALOG)
                img.set_pixel_size(32)
                img.set_valign(Gtk.Align.START)
                return img
        if pixbuf is None:
            return None
        scaled = _scale_pixbuf(pixbuf, 32)
        img = Gtk.Image.new_from_pixbuf(scaled)
        img.set_valign(Gtk.Align.START)
        return img

    # ── bar-style decoration (cairo) ────────────────────────────────
    def _draw_background(self, w, cr) -> bool:
        """Fill the beveled path with the toast bg color. Runs BEFORE
        children draw so labels render on top. With app_paintable=True
        on an RGBA-visual toplevel, the surface starts transparent —
        no OPERATOR_CLEAR needed, and using it here actually scrubs
        the toasts above us when they share an offscreen surface."""
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        beveled_path(
            cr, width, height,
            bevel=theme.NOTIF_BEVEL,
            corners=theme.NOTIF_BEVEL_CORNERS,
        )
        paint(cr, theme.NOTIF_BG)
        cr.fill()
        return False

    def _draw_decoration(self, w, cr) -> bool:
        """Beveled frame in urgency color, plus a CPU-style segmented
        progress meter at the bottom when a `value` hint is set."""
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        accent = _frame_color(self.notif.urgency)

        # ── beveled border (matches Menu rows) ──
        line_w = theme.NOTIF_BORDER_THICK
        beveled_path(
            cr, width, height,
            bevel=theme.NOTIF_BEVEL,
            corners=theme.NOTIF_BEVEL_CORNERS,
            inset=line_w / 2,
        )
        paint(cr, accent)
        cr.set_line_width(line_w)
        cr.stroke()

        # ── timer trace overdraws the urgency border clockwise from
        # top-left as time elapses; full perimeter → expire. ──
        if self._timeout_ms > 0:
            self._draw_timer_trace(cr, width, height, line_w)

        # ── segmented progress meter (only when value is set) ──
        if self.notif.value is not None:
            self._draw_meter(cr, width, height, accent)
        return False

    def _draw_timer_trace(self, cr, width: int, height: int, line_w: float) -> None:
        progress = min(1.0, self._elapsed_ms / self._timeout_ms)
        if progress <= 0:
            return
        pts = beveled_polyline(
            width, height,
            bevel=theme.NOTIF_BEVEL,
            corners=theme.NOTIF_BEVEL_CORNERS,
            inset=line_w / 2,
        )
        total = 0.0
        for i in range(len(pts) - 1):
            dx = pts[i + 1][0] - pts[i][0]
            dy = pts[i + 1][1] - pts[i][1]
            total += (dx * dx + dy * dy) ** 0.5
        paint(cr, theme.NOTIF_TIMER_BORDER_FG)
        cr.set_line_width(line_w)
        stroke_partial(cr, pts, total * progress)

    def _draw_meter(self, cr, width: int, height: int, accent: str) -> None:
        n   = theme.NOTIF_METER_SEGMENTS
        gap = theme.NOTIF_METER_GAP
        h   = theme.NOTIF_METER_THICK
        # Inset from the side margins so it sits between the bottom corners.
        side = theme.NOTIF_PADDING_X
        usable_w = max(1, width - 2 * side)
        tick_w = max(1.0, (usable_w - gap * (n - 1)) / n)
        ratio = max(0.0, min(1.0, self.notif.value / 100.0))
        fill_end_x = ratio * usable_w
        # Sit a few px above the bottom corner brackets.
        y = height - int(theme.NOTIF_BORDER_THICK) - h - theme.NOTIF_METER_INSET_Y
        for i in range(n):
            x = i * (tick_w + gap)
            mid = x + tick_w / 2
            paint(cr, accent if mid <= fill_end_x else theme.NOTIF_METER_DIM)
            cr.rectangle(side + x, y, tick_w, h)
            cr.fill()

    # ── timer / hover ───────────────────────────────────────────────
    def start_timer(self, timeout_ms: int) -> None:
        """(Re)start the dismiss timer. 0 = persistent (no animation)."""
        self.stop_timer()
        self._timeout_ms = max(0, int(timeout_ms))
        self._elapsed_ms = 0
        if self._timeout_ms > 0:
            self._last_tick_ms = GLib.get_monotonic_time() // 1000
            self._paused = self._hovered  # respect current hover state
            self._tick_source = GLib.timeout_add(
                theme.NOTIF_TIMER_TICK_MS, self._tick
            )
        self.queue_draw()

    def stop_timer(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        self._last_tick_ms = None

    def set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        if not paused:
            # Reset the baseline so we don't credit the paused interval.
            self._last_tick_ms = GLib.get_monotonic_time() // 1000

    def _tick(self) -> bool:
        now = GLib.get_monotonic_time() // 1000
        if self._last_tick_ms is not None and not self._paused:
            self._elapsed_ms += now - self._last_tick_ms
        self._last_tick_ms = now
        self.queue_draw()
        if self._elapsed_ms >= self._timeout_ms:
            self._tick_source = None
            self.on_dismiss(self.notif.id, REASON_EXPIRED)
            return False
        return True

    def _on_enter(self, _w, ev) -> bool:
        # Crossings into child widgets (action buttons) report INFERIOR.
        if ev.detail == Gdk.NotifyType.INFERIOR:
            return False
        if not self._hovered:
            self._hovered = True
            if self.on_hover_change is not None:
                self.on_hover_change(True)
        return False

    def _on_leave(self, _w, ev) -> bool:
        if ev.detail == Gdk.NotifyType.INFERIOR:
            return False
        if self._hovered:
            self._hovered = False
            if self.on_hover_change is not None:
                self.on_hover_change(False)
        return False

    def _on_destroy(self, _w) -> None:
        self.stop_timer()

    # ── click handlers ──────────────────────────────────────────────
    def _on_click(self, _w, _ev):
        for key, _label in self.notif.actions:
            if key == "default":
                self.on_action(self.notif.id, key)
                return True
        self.on_dismiss(self.notif.id, REASON_DISMISSED)
        return True

    def _on_action_click(self, _btn, key):
        self.on_action(self.notif.id, key)


# ── pixbuf helpers ──────────────────────────────────────────────────
def _pixbuf_from_image_data(image_data) -> GdkPixbuf.Pixbuf | None:
    try:
        w, h, rowstride, has_alpha, bits, channels, data = image_data
        gbytes = GLib.Bytes.new(bytes(data))
        return GdkPixbuf.Pixbuf.new_from_bytes(
            gbytes, GdkPixbuf.Colorspace.RGB,
            bool(has_alpha), int(bits), int(w), int(h), int(rowstride),
        )
    except Exception:
        return None


def _pixbuf_from_path(path: str) -> GdkPixbuf.Pixbuf | None:
    if path.startswith("file://"):
        path = path[len("file://"):]
    try:
        return GdkPixbuf.Pixbuf.new_from_file(path)
    except Exception:
        return None


def _scale_pixbuf(pixbuf: GdkPixbuf.Pixbuf, target_h: int) -> GdkPixbuf.Pixbuf:
    sw, sh = pixbuf.get_width(), pixbuf.get_height()
    if sh <= target_h:
        return pixbuf
    scale = target_h / sh
    return pixbuf.scale_simple(int(sw * scale), target_h, GdkPixbuf.InterpType.BILINEAR)
