import itertools
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from ..style import Style

_id_gen = itertools.count()


class Widget:
    """Base class for bar widgets.

    Subclasses implement `build_widget()` to return a Gtk widget, and optionally
    `tick()` plus `interval_ms` to schedule periodic updates.

    Event handlers (`on_left_click`, etc.) receive the source `Widget` as their
    sole argument; if any are set, `build()` wraps the widget in a transparent
    `Gtk.EventBox`.
    """

    interval_ms: int | None = None
    expand: bool = False

    def __init__(
        self,
        style: Style | None = None,
        *,
        on_left_click: Callable | None = None,
        on_right_click: Callable | None = None,
        on_middle_click: Callable | None = None,
        on_scroll_up: Callable | None = None,
        on_scroll_down: Callable | None = None,
        on_hover_enter: Callable | None = None,
        on_hover_leave: Callable | None = None,
        hover_style: Style | None = None,
        active_style: Style | None = None,
        child_styles: dict[str, Style] | None = None,
        # If True, the widget (and its event-box wrapper) fills the bar's
        # full height instead of hugging its content. Lets hover/active
        # backgrounds paint as a full-height block, not just behind text.
        vfill: bool = False,
    ):
        self.style = style
        self.hover_style = hover_style
        self.active_style = active_style
        self.vfill = vfill
        self.child_styles = child_styles or {}
        self.name = f"iw{next(_id_gen)}"
        self.gtk_widget: Gtk.Widget | None = None
        self._named_widget: Gtk.Widget | None = None
        self._timer_id: int | None = None
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.on_middle_click = on_middle_click
        self.on_scroll_up = on_scroll_up
        self.on_scroll_down = on_scroll_down
        self.on_hover_enter = on_hover_enter
        self.on_hover_leave = on_hover_leave

    def build(self) -> Gtk.Widget:
        w = self.build_widget()
        w.set_name(self.name)
        valign = Gtk.Align.FILL if self.vfill else Gtk.Align.CENTER
        w.set_valign(valign)
        w.set_vexpand(self.vfill)
        self._named_widget = w
        if self._needs_event_box():
            w = self._wrap_events(w)
            w.set_valign(valign)
            w.set_vexpand(self.vfill)
        self.gtk_widget = w
        return w

    def _has_events(self) -> bool:
        return any(
            (
                self.on_left_click,
                self.on_right_click,
                self.on_middle_click,
                self.on_scroll_up,
                self.on_scroll_down,
                self.on_hover_enter,
                self.on_hover_leave,
            )
        )

    def _needs_event_box(self) -> bool:
        return (
            self._has_events()
            or self.hover_style is not None
            or self.active_style is not None
        )

    def _wrap_events(self, inner: Gtk.Widget) -> Gtk.Widget:
        ev = Gtk.EventBox()
        ev.add(inner)
        ev.set_visible_window(False)
        ev.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        ev.connect("button-press-event", self._dispatch_button)
        ev.connect("button-release-event", self._on_button_release)
        ev.connect("scroll-event", self._dispatch_scroll)
        ev.connect("enter-notify-event", self._dispatch_hover_enter)
        ev.connect("leave-notify-event", self._dispatch_hover_leave)
        return ev

    def _class_ctx(self):
        return self._named_widget.get_style_context() if self._named_widget else None

    def _dispatch_hover_enter(self, _w, _event) -> bool:
        ctx = self._class_ctx()
        if ctx:
            ctx.add_class("hover")
        if self.on_hover_enter:
            self.on_hover_enter(self)
        return False

    def _dispatch_hover_leave(self, _w, _event) -> bool:
        ctx = self._class_ctx()
        if ctx:
            ctx.remove_class("hover")
            ctx.remove_class("active")
        if self.on_hover_leave:
            self.on_hover_leave(self)
        return False

    def _dispatch_button(self, _w, event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        ctx = self._class_ctx()
        if ctx:
            ctx.add_class("active")
        if event.button == 1 and self.on_left_click:
            self.on_left_click(self)
            return True
        if event.button == 2 and self.on_middle_click:
            self.on_middle_click(self)
            return True
        if event.button == 3 and self.on_right_click:
            self.on_right_click(self)
            return True
        return False

    def _on_button_release(self, _w, _event) -> bool:
        ctx = self._class_ctx()
        if ctx:
            ctx.remove_class("active")
        return False

    def _dispatch_scroll(self, _w, event) -> bool:
        if event.direction == Gdk.ScrollDirection.UP and self.on_scroll_up:
            self.on_scroll_up(self)
            return True
        if event.direction == Gdk.ScrollDirection.DOWN and self.on_scroll_down:
            self.on_scroll_down(self)
            return True
        return False

    def build_widget(self) -> Gtk.Widget:
        raise NotImplementedError

    def default_css(self) -> str:
        """CSS owned by this widget. Must be scoped to `#self.name`."""
        return ""

    def tick(self) -> bool:
        return True

    def start(self) -> None:
        if self.interval_ms and self._timer_id is None:
            self.tick()
            self._timer_id = GLib.timeout_add(self.interval_ms, self.tick)

    def stop(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def walk(self):
        yield self


def paint(cr, hex_color: str, alpha: float | None = None) -> None:
    """Set Cairo source from a hex color. `alpha` overrides the hex alpha
    channel when provided; otherwise we use whatever Gdk.RGBA parsed."""
    rgba = Gdk.RGBA()
    rgba.parse(hex_color)
    a = rgba.alpha if alpha is None else alpha
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, a)


def beveled_path(
    cr,
    w: float,
    h: float,
    *,
    bevel: int,
    corners: tuple[str, ...],
    inset: float = 0.0,
) -> None:
    """Add a closed beveled-rectangle subpath to `cr`. Each corner name in
    `corners` ("top-left" | "top-right" | "bottom-left" | "bottom-right")
    is sliced at 45° by `bevel` px; remaining corners stay square.
    `inset` shrinks the rect uniformly — useful so a stroke stays crisp
    inside the widget's allocation."""
    x0, y0 = inset, inset
    x1, y1 = w - inset, h - inset
    b = min(bevel, int((x1 - x0) // 2), int((y1 - y0) // 2))
    if b <= 0 or not corners:
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        return
    if "top-left" in corners:
        cr.move_to(x0, y0 + b); cr.line_to(x0 + b, y0)
    else:
        cr.move_to(x0, y0)
    if "top-right" in corners:
        cr.line_to(x1 - b, y0); cr.line_to(x1, y0 + b)
    else:
        cr.line_to(x1, y0)
    if "bottom-right" in corners:
        cr.line_to(x1, y1 - b); cr.line_to(x1 - b, y1)
    else:
        cr.line_to(x1, y1)
    if "bottom-left" in corners:
        cr.line_to(x0 + b, y1); cr.line_to(x0, y1 - b)
    else:
        cr.line_to(x0, y1)
    cr.close_path()


def beveled_polyline(
    w: int,
    h: int,
    *,
    bevel: int,
    corners: tuple[str, ...],
    inset: float = 0.0,
) -> list[tuple[float, float]]:
    """Closed polyline (first point repeated last) clockwise from top-left,
    matching `beveled_path`'s corner cuts. Used to walk the perimeter by
    arc-length for animated traces."""
    x0, y0 = inset, inset
    x1, y1 = w - inset, h - inset
    b = min(bevel, int((x1 - x0) // 2), int((y1 - y0) // 2))
    pts: list[tuple[float, float]] = []
    if b <= 0 or not corners:
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        pts.append(pts[0])
        return pts
    if "top-left" in corners:
        pts.append((x0, y0 + b)); pts.append((x0 + b, y0))
    else:
        pts.append((x0, y0))
    if "top-right" in corners:
        pts.append((x1 - b, y0)); pts.append((x1, y0 + b))
    else:
        pts.append((x1, y0))
    if "bottom-right" in corners:
        pts.append((x1, y1 - b)); pts.append((x1 - b, y1))
    else:
        pts.append((x1, y1))
    if "bottom-left" in corners:
        pts.append((x0 + b, y1)); pts.append((x0, y1 - b))
    else:
        pts.append((x0, y1))
    pts.append(pts[0])
    return pts


def stroke_partial(cr, pts: list[tuple[float, float]], length: float) -> None:
    """Walk `pts` (from `beveled_polyline`) by `length` arc-length px,
    stroking the partial path. Used to draw a growing perimeter trace."""
    if length <= 0 or len(pts) < 2:
        return
    cr.move_to(*pts[0])
    remaining = length
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg = (dx * dx + dy * dy) ** 0.5
        if seg <= 0:
            continue
        if seg <= remaining:
            cr.line_to(x1, y1)
            remaining -= seg
            if remaining <= 0:
                break
        else:
            t = remaining / seg
            cr.line_to(x0 + dx * t, y0 + dy * t)
            break
    cr.stroke()


def make_label(text: str, css_class: str | None = None) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_valign(Gtk.Align.CENTER)
    label.set_xalign(0.5)
    label.set_yalign(0.5)
    if css_class:
        label.get_style_context().add_class(css_class)
    return label
