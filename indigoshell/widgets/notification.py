"""One notification toast. Mirrors the prior dunst look.

Layout (body wraps under the heading row):

    ┌────────────────────────────────────────────┐
    │ // SUMMARY app_name                        │
    │                                            │
    │ body line 1                                │
    │ body line 2                                │
    │ [Action 1]  [Action 2]                     │
    └────────────────────────────────────────────┘

A 4px frame in the urgency color wraps the whole thing. Click anywhere
dismisses (matches dunst `mouse_left_click = close_current`).
"""

import html
from typing import Callable

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk

from .. import theme
from ..services.notifications import (
    Notification,
    REASON_DISMISSED,
    URGENCY_CRITICAL,
    URGENCY_LOW,
)
from .base import paint


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
        show_icon: bool = False,
    ) -> None:
        super().__init__()
        self.notif = notif
        self.on_dismiss = on_dismiss
        self.on_action = on_action
        self.show_icon = show_icon
        self.set_visible_window(True)
        self.set_size_request(theme.NOTIF_WIDTH, -1)
        self.get_style_context().add_class("notif")

        self._build_ui()
        self.connect("button-press-event", self._on_click)
        # Bar-style decoration: corner brackets + bottom underline accent.
        self.connect_after("draw", self._draw_decoration)

    def update(self, notif: Notification) -> None:
        """Mutate this toast in place when an app reuses the id."""
        self.notif = notif
        for child in list(self.get_children()):
            self.remove(child)
        self._build_ui()
        self.show_all()

    # ── build ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # GTK 3 EventBox doesn't honor CSS padding for child layout —
        # the bg/border still paint via CSS, but inner content sits
        # flush. Push it inward with widget margin.
        col.set_margin_start(theme.NOTIF_PADDING_X)
        col.set_margin_end(theme.NOTIF_PADDING_X)
        col.set_margin_top(theme.NOTIF_PADDING_Y)
        # Reserve extra room only when we'll paint the progress meter.
        extra = (theme.NOTIF_METER_THICK + 6) if self.notif.value is not None else 0
        col.set_margin_bottom(theme.NOTIF_PADDING_Y + extra)

        head_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if self.show_icon:
            image = self._build_image()
            if image is not None:
                head_row.pack_start(image, False, False, 0)

        head = Gtk.Label()
        head.set_xalign(0.0)
        head.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        head.set_markup(self._heading_markup())
        head_row.pack_start(head, True, True, 0)
        col.pack_start(head_row, False, False, 0)

        if self.notif.body:
            body = Gtk.Label()
            body.set_xalign(0.0)
            body.set_line_wrap(True)
            body.set_line_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
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
    def _draw_decoration(self, w, cr) -> bool:
        """Corner brackets in urgency color, plus a CPU-style segmented
        progress meter at the bottom when a `value` hint is set."""
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        accent = _frame_color(self.notif.urgency)

        # ── corner brackets ──
        arm = theme.NOTIF_CORNER_ARM
        t = theme.NOTIF_CORNER_THICK
        paint(cr, accent)
        # top-left
        cr.rectangle(0, 0, arm, t)
        cr.rectangle(0, 0, t, arm)
        # top-right
        cr.rectangle(width - arm, 0, arm, t)
        cr.rectangle(width - t, 0, t, arm)
        # bottom-left
        cr.rectangle(0, height - t, arm, t)
        cr.rectangle(0, height - arm, t, arm)
        # bottom-right
        cr.rectangle(width - arm, height - t, arm, t)
        cr.rectangle(width - t, height - arm, t, arm)
        cr.fill()

        # ── segmented progress meter (only when value is set) ──
        if self.notif.value is not None:
            self._draw_meter(cr, width, height, accent)
        return False

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
        y = height - theme.NOTIF_CORNER_THICK - h - 3
        for i in range(n):
            x = i * (tick_w + gap)
            mid = x + tick_w / 2
            paint(cr, accent if mid <= fill_end_x else theme.NOTIF_METER_DIM)
            cr.rectangle(side + x, y, tick_w, h)
            cr.fill()

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
