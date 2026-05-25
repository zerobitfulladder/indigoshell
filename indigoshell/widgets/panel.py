"""Declarative panel widget.

A `Panel` interprets a tree of frozen dataclasses (Label, Divider, Action,
Toggle, Submenu, Value, Meter, Row, Card, Embed) into Gtk widgets. Each
`Screen` is a list of items; selecting a `Submenu` pushes its target onto
an internal stack, Backspace/Left/h pops, Esc closes the popup. The same
key bindings, plus mouse clicks, drive activation of Action / Toggle rows.

Live items (Value, Meter, and any Embed'd Widget) refresh on the panel's
own tick — pull-based, no observable machinery.

Pair with `PopupKind(name=<n>, content=Panel(popup_name=<n>, root=<s>))`
with `grab=True` to make the panel fully modal.
"""

from dataclasses import dataclass, field
from typing import Callable, Union

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from .base import Widget, beveled_path


# ── DSL ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Label:
    text: str
    style: str = "heading"          # "heading" | "body" | "muted"


@dataclass(frozen=True)
class Divider:
    pass


@dataclass(frozen=True)
class Action:
    label: str
    on_activate: Callable[[], None]
    key: str | None = None          # single-char hotkey
    hint: str | None = None         # right-aligned annotation
    close_on_activate: bool = True


@dataclass(frozen=True)
class Toggle:
    label: str
    get: Callable[[], bool]
    set: Callable[[bool], None]
    key: str | None = None


@dataclass(frozen=True)
class Submenu:
    label: str
    target: "Screen"
    key: str | None = None


@dataclass(frozen=True)
class Value:
    get: Callable[[], str]
    color: Union[str, Callable[[], str], None] = None
    xalign: float = 0.0


@dataclass(frozen=True)
class Meter:
    get: Callable[[], float]
    max: float = 100.0
    color: Union[str, Callable[[], str]] = theme.CYAN_BRIGHT
    width: int = 180
    height: int = 8


@dataclass(frozen=True)
class Row:
    cells: list                     # of any item
    spacing: int = 12


@dataclass(frozen=True)
class Card:
    title: str
    items: list


@dataclass(frozen=True)
class Embed:
    widget: Widget


@dataclass(frozen=True)
class Screen:
    title: str
    items: list


Item = Union[Label, Divider, Action, Toggle, Submenu, Value, Meter, Row, Card, Embed]


# ── row palette (matches Menu) ──────────────────────────────────────────

_RGBA_ROW_IDLE   = (23 / 255,  6 / 255,  32 / 255, 0.65)
_RGBA_ROW_ARMED  = ( 5 / 255, 217 / 255, 232 / 255, 0.22)
_RGBA_ROW_BORDER = ( 5 / 255, 217 / 255, 232 / 255, 0.85)

_ROW_HEIGHT = 38
_ROW_BEVEL = 8
_ROW_BEVEL_CORNERS = ("top-right", "bottom-left")


# ── Panel widget ────────────────────────────────────────────────────────


class Panel(Widget):
    interval_ms = 250
    SCREEN_SPACING = 8
    ROW_SPACING = 4

    def __init__(
        self,
        popup_name: str,
        root: Screen,
        *,
        width: int = 380,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.popup_name = popup_name
        self.root = root
        self.width = width

        # Navigation stack of (screen, selected_index) pairs.
        self._stack: list[tuple[Screen, int]] = [(root, 0)]

        # Per-screen state, rebuilt on each push/pop/rebuild:
        self._outer: Gtk.Box | None = None
        self._title_label: Gtk.Label | None = None
        self._content_box: Gtk.Box | None = None
        self._selectables: list[tuple[Item, Gtk.Widget]] = []
        self._live_refresh: list[Callable[[], None]] = []
        self._active_embeds: list[Widget] = []

        # All embedded widgets reachable from the tree (collected once, used
        # by PopupKind to install CSS up front).
        self._all_embeds: list[Widget] = []
        self._collect_embeds(root, seen=set())

        # Toplevel key wiring (set in start/stop).
        self._toplevel: Gtk.Window | None = None
        self._press_id: int | None = None

    # ── walk for popup CSS install ──────────────────────────────────────
    def walk(self):
        yield self
        for w in self._all_embeds:
            yield from w.walk()

    def _collect_embeds(self, screen: Screen, seen: set[int]) -> None:
        if id(screen) in seen:
            return
        seen.add(id(screen))
        for item in _iter_items(screen.items):
            if isinstance(item, Embed):
                self._all_embeds.append(item.widget)
            elif isinstance(item, Submenu):
                self._collect_embeds(item.target, seen)

    # ── build ───────────────────────────────────────────────────────────
    def build_widget(self) -> Gtk.Widget:
        # Reset nav state on every (re)build so reopening always lands on root.
        self._stack = [(self.root, 0)]
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.SCREEN_SPACING)
        outer.set_size_request(self.width, -1)
        title = Gtk.Label()
        title.get_style_context().add_class("panel-title")
        title.set_xalign(0.0)
        title.set_margin_start(2)
        title.set_margin_end(2)
        title.set_margin_bottom(4)
        outer.pack_start(title, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.ROW_SPACING)
        outer.pack_start(body, True, True, 0)
        self._outer = outer
        self._title_label = title
        self._content_box = body
        self._render_current()
        return outer

    def default_css(self) -> str:
        sel = f"#{self.name}"
        return (
            f"{sel} {{ background: transparent; }}"
            f"{sel} .panel-title {{"
            f" color: {theme.HIGHLIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE_LG}px;"
            f" font-weight: bold;"
            f" letter-spacing: 2px;"
            f" }}"
            f"{sel} .panel-row-label {{"
            f" color: {theme.FG_STRONG};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" letter-spacing: 1px;"
            f" }}"
            f"{sel} .panel-row-hint {{"
            f" color: {theme.HIGHLIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" font-weight: bold;"
            f" }}"
            f"{sel} .armed .panel-row-label,"
            f"{sel} .armed .panel-row-hint {{ color: {theme.FG_ACCENT}; }}"
            f"{sel} .panel-label-heading {{"
            f" color: {theme.HIGHLIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" font-weight: bold;"
            f" letter-spacing: 1px;"
            f" }}"
            f"{sel} .panel-label-body {{"
            f" color: {theme.FG_STRONG};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" }}"
            f"{sel} .panel-label-muted {{"
            f" color: {theme.FG_MUTED};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" }}"
            f"{sel} .panel-value {{"
            f" color: {theme.MAGENTA_BRIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" }}"
            f"{sel} .panel-card-title {{"
            f" color: {theme.HIGHLIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" font-weight: bold;"
            f" letter-spacing: 2px;"
            f" }}"
            f"{sel} .panel-divider {{"
            f" background-color: {theme.CYAN_DIM};"
            f" min-height: 1px;"
            f" }}"
        )

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        super().start()
        if self.gtk_widget is None:
            return
        top = self.gtk_widget.get_toplevel()
        if isinstance(top, Gtk.Window):
            self._toplevel = top
            self._press_id = top.connect("key-press-event", self._on_key)
        self._start_active_embeds()

    def stop(self) -> None:
        if self._toplevel is not None and self._press_id is not None:
            self._toplevel.disconnect(self._press_id)
        self._toplevel = None
        self._press_id = None
        self._stop_active_embeds()
        super().stop()

    def tick(self) -> bool:
        for fn in self._live_refresh:
            try:
                fn()
            except Exception:
                pass
        for w in self._active_embeds:
            try:
                w.tick()
            except Exception:
                pass
        return True

    # ── navigation ──────────────────────────────────────────────────────
    def _push(self, screen: Screen) -> None:
        self._stack.append((screen, 0))
        self._render_current()

    def _pop(self) -> bool:
        if len(self._stack) <= 1:
            return False
        self._stack.pop()
        self._render_current()
        return True

    def _current(self) -> Screen:
        return self._stack[-1][0]

    def _sel(self) -> int:
        return self._stack[-1][1]

    def _set_sel(self, idx: int) -> None:
        screen, _ = self._stack[-1]
        self._stack[-1] = (screen, idx)
        self._apply_armed()

    # ── render ──────────────────────────────────────────────────────────
    def _render_current(self) -> None:
        if self._content_box is None or self._title_label is None:
            return
        # Tear down the previous screen's embeds and live refreshers.
        self._stop_active_embeds()
        self._active_embeds = []
        self._live_refresh = []
        self._selectables = []
        for child in self._content_box.get_children():
            self._content_box.remove(child)

        screen = self._current()
        crumb = " › ".join(s.title for s, _ in self._stack)
        self._title_label.set_text(crumb)

        for item in screen.items:
            w = self._render_item(item)
            if w is not None:
                self._content_box.pack_start(w, False, False, 0)

        # Clamp / default selection.
        sel = self._sel()
        if not self._selectables:
            sel = -1
        elif sel < 0 or sel >= len(self._selectables):
            sel = 0
        screen, _ = self._stack[-1]
        self._stack[-1] = (screen, sel)

        self._content_box.show_all()
        self._apply_armed()
        # Start the embeds belonging to this screen.
        if self._toplevel is not None:
            self._start_active_embeds()

    def _render_item(self, item: Item) -> Gtk.Widget | None:
        if isinstance(item, Label):
            return self._render_label(item)
        if isinstance(item, Divider):
            return self._render_divider()
        if isinstance(item, Action):
            return self._render_action(item)
        if isinstance(item, Toggle):
            return self._render_toggle(item)
        if isinstance(item, Submenu):
            return self._render_submenu(item)
        if isinstance(item, Value):
            return self._render_value(item)
        if isinstance(item, Meter):
            return self._render_meter(item)
        if isinstance(item, Row):
            return self._render_row(item)
        if isinstance(item, Card):
            return self._render_card(item)
        if isinstance(item, Embed):
            return self._render_embed(item)
        return None

    def _render_label(self, item: Label) -> Gtk.Widget:
        lbl = Gtk.Label(label=item.text)
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class(f"panel-label-{item.style}")
        return lbl

    def _render_divider(self) -> Gtk.Widget:
        sep = Gtk.Box()
        sep.get_style_context().add_class("panel-divider")
        sep.set_size_request(-1, 1)
        sep.set_margin_top(2)
        sep.set_margin_bottom(2)
        return sep

    # ── button-style rows (Action / Toggle / Submenu) ───────────────────
    def _make_row(self, label: str, hint: str | None, item: Item,
                  hint_color: str | None = None) -> Gtk.Widget:
        ev = Gtk.EventBox()
        ev.set_visible_window(False)
        ev.set_size_request(-1, _ROW_HEIGHT)
        ev.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.set_valign(Gtk.Align.CENTER)

        text = Gtk.Label(label=label)
        text.set_single_line_mode(True)
        text.set_valign(Gtk.Align.CENTER)
        text.set_xalign(0.0)
        text.get_style_context().add_class("panel-row-label")
        inner.pack_start(text, True, True, 0)

        hint_label: Gtk.Label | None = None
        if hint is not None:
            hl = Gtk.Label()
            color = hint_color or theme.HIGHLIGHT
            hl.set_markup(f"<span color='{color}'>{_pango_escape(hint)}</span>")
            hl.get_style_context().add_class("panel-row-hint")
            hl.set_single_line_mode(True)
            hl.set_valign(Gtk.Align.CENTER)
            inner.pack_end(hl, False, False, 0)
            hint_label = hl

        # Optional single-char key hint after the right-side text.
        key = getattr(item, "key", None)
        if key:
            key_lbl = Gtk.Label()
            key_lbl.set_markup(
                f"<span color='{theme.YELLOW_MID}'>//</span> "
                f"<span color='{theme.HIGHLIGHT}'>{_pango_escape(key)}</span>"
            )
            key_lbl.get_style_context().add_class("panel-row-hint")
            key_lbl.set_single_line_mode(True)
            key_lbl.set_valign(Gtk.Align.CENTER)
            inner.pack_end(key_lbl, False, False, 0)

        ev.add(inner)
        ev.connect("draw", self._draw_row, ev)
        ev.connect("enter-notify-event", self._on_row_enter, item)
        ev.connect("button-release-event", self._on_row_click, item)

        # Stash the hint label so Toggle can re-render on/off state.
        ev._panel_hint_label = hint_label  # type: ignore[attr-defined]

        self._selectables.append((item, ev))
        return ev

    def _render_action(self, item: Action) -> Gtk.Widget:
        return self._make_row(item.label, item.hint, item)

    def _render_toggle(self, item: Toggle) -> Gtk.Widget:
        on = bool(_safe_call(item.get, False))
        row = self._make_row(
            item.label,
            "[ON]" if on else "[OFF]",
            item,
            hint_color=theme.CYAN_BRIGHT if on else theme.FG_MUTED,
        )

        def refresh():
            new_on = bool(_safe_call(item.get, False))
            hint = row._panel_hint_label  # type: ignore[attr-defined]
            if hint is None:
                return
            color = theme.CYAN_BRIGHT if new_on else theme.FG_MUTED
            text = "[ON]" if new_on else "[OFF]"
            hint.set_markup(f"<span color='{color}'>{text}</span>")

        self._live_refresh.append(refresh)
        return row

    def _render_submenu(self, item: Submenu) -> Gtk.Widget:
        return self._make_row(item.label, "›", item)

    # ── live items ──────────────────────────────────────────────────────
    def _render_value(self, item: Value) -> Gtk.Widget:
        lbl = Gtk.Label()
        lbl.set_xalign(item.xalign)
        lbl.get_style_context().add_class("panel-value")
        lbl.set_single_line_mode(True)

        def refresh():
            text = str(_safe_call(item.get, ""))
            color = item.color() if callable(item.color) else item.color
            if color:
                lbl.set_markup(f"<span color='{color}'>{_pango_escape(text)}</span>")
            else:
                lbl.set_text(text)

        refresh()
        self._live_refresh.append(refresh)
        return lbl

    def _render_meter(self, item: Meter) -> Gtk.Widget:
        area = Gtk.DrawingArea()
        area.set_size_request(item.width, item.height)
        area.set_valign(Gtk.Align.CENTER)

        state = {"value": float(_safe_call(item.get, 0.0))}

        def on_draw(_w, cr):
            alloc = area.get_allocation()
            w, h = alloc.width, alloc.height
            pct = max(0.0, min(1.0, state["value"] / max(0.001, item.max)))
            # Background channel.
            cr.set_source_rgba(*_RGBA_ROW_IDLE)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            # Filled bar.
            color = item.color() if callable(item.color) else item.color
            rgba = Gdk.RGBA()
            rgba.parse(color)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
            cr.rectangle(0, 0, w * pct, h)
            cr.fill()
            # Border.
            cr.set_source_rgba(*_RGBA_ROW_BORDER)
            cr.set_line_width(1.0)
            cr.rectangle(0.5, 0.5, w - 1, h - 1)
            cr.stroke()
            return False

        area.connect("draw", on_draw)

        def refresh():
            state["value"] = float(_safe_call(item.get, 0.0))
            area.queue_draw()

        self._live_refresh.append(refresh)
        return area

    # ── layout ──────────────────────────────────────────────────────────
    def _render_row(self, item: Row) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=item.spacing)
        for cell in item.cells:
            w = self._render_item(cell)
            if w is not None:
                expand = isinstance(cell, (Meter, Label))
                box.pack_start(w, expand, expand, 0)
        return box

    def _render_card(self, item: Card) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        title = Gtk.Label(label=item.title)
        title.set_xalign(0.0)
        title.get_style_context().add_class("panel-card-title")
        outer.pack_start(title, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.ROW_SPACING)
        body.set_margin_start(8)
        for sub in item.items:
            w = self._render_item(sub)
            if w is not None:
                body.pack_start(w, False, False, 0)
        outer.pack_start(body, False, False, 0)

        # Card chrome: beveled border drawn via a wrapper EventBox.
        wrap = Gtk.EventBox()
        wrap.set_visible_window(False)
        wrap.add(outer)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        wrap.connect("draw", self._draw_card)
        return wrap

    def _render_embed(self, item: Embed) -> Gtk.Widget:
        w = item.widget.build()
        self._active_embeds.append(item.widget)
        return w

    # ── selection / activation ──────────────────────────────────────────
    def _apply_armed(self) -> None:
        sel = self._sel()
        for idx, (_, row) in enumerate(self._selectables):
            ctx = row.get_style_context()
            if idx == sel:
                ctx.add_class("armed")
            else:
                ctx.remove_class("armed")
            row.queue_draw()

    def _activate(self, item: Item) -> None:
        if isinstance(item, Submenu):
            self._push(item.target)
            return
        if isinstance(item, Toggle):
            try:
                item.set(not bool(_safe_call(item.get, False)))
            except Exception:
                pass
            # Refresh hint in place.
            for it, row in self._selectables:
                if it is item:
                    self._refresh_toggle_hint(item, row)
                    break
            return
        if isinstance(item, Action):
            try:
                item.on_activate()
            except Exception:
                pass
            if item.close_on_activate:
                from ..core.daemon import get_daemon
                get_daemon().close(self.popup_name)

    def _refresh_toggle_hint(self, item: "Toggle", row: Gtk.Widget) -> None:
        on = bool(_safe_call(item.get, False))
        hint = row._panel_hint_label  # type: ignore[attr-defined]
        if hint is None:
            return
        color = theme.CYAN_BRIGHT if on else theme.FG_MUTED
        text = "[ON]" if on else "[OFF]"
        hint.set_markup(f"<span color='{color}'>{text}</span>")

    # ── input ───────────────────────────────────────────────────────────
    def _on_key(self, _w, event) -> bool:
        name = Gdk.keyval_name(event.keyval) or ""
        if name in ("Down", "j", "Tab"):
            self._move_sel(+1)
            return True
        if name in ("Up", "k", "ISO_Left_Tab"):
            self._move_sel(-1)
            return True
        if name in ("Return", "KP_Enter", "Right", "l"):
            sel = self._sel()
            if 0 <= sel < len(self._selectables):
                self._activate(self._selectables[sel][0])
            return True
        if name in ("BackSpace", "Left", "h"):
            if self._pop():
                return True
            return False  # at root — let Escape-style close happen via Esc
        # Direct single-key activation.
        if len(name) == 1:
            for item, _row in self._selectables:
                key = getattr(item, "key", None)
                if key == name:
                    self._activate(item)
                    return True
        return False

    def _move_sel(self, delta: int) -> None:
        n = len(self._selectables)
        if n == 0:
            return
        self._set_sel((self._sel() + delta) % n)

    def _on_row_enter(self, _w, _e, item: Item) -> bool:
        for idx, (it, _) in enumerate(self._selectables):
            if it is item:
                self._set_sel(idx)
                break
        return False

    def _on_row_click(self, _w, event, item: Item) -> bool:
        if event.button != 1:
            return False
        self._activate(item)
        return True

    # ── drawing ─────────────────────────────────────────────────────────
    def _draw_row(self, widget: Gtk.Widget, cr, row: Gtk.Widget) -> bool:
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        line_w = 1.2
        inset = line_w / 2
        beveled_path(cr, w, h, bevel=_ROW_BEVEL, corners=_ROW_BEVEL_CORNERS, inset=inset)
        armed = "armed" in row.get_style_context().list_classes()
        if armed:
            cr.set_source_rgba(*_RGBA_ROW_ARMED)
        else:
            cr.set_source_rgba(*_RGBA_ROW_IDLE)
        cr.fill_preserve()
        cr.set_source_rgba(*_RGBA_ROW_BORDER)
        cr.set_line_width(line_w)
        cr.stroke()
        return False

    def _draw_card(self, widget: Gtk.Widget, cr) -> bool:
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        line_w = 1.0
        inset = line_w / 2
        beveled_path(cr, w, h, bevel=10, corners=("top-left", "bottom-right"), inset=inset)
        cr.set_source_rgba(*_RGBA_ROW_IDLE)
        cr.fill_preserve()
        cr.set_source_rgba(*_RGBA_ROW_BORDER)
        cr.set_line_width(line_w)
        cr.stroke()
        return False

    # ── embed lifecycle ─────────────────────────────────────────────────
    def _start_active_embeds(self) -> None:
        for w in self._active_embeds:
            try:
                w.start()
            except Exception:
                pass

    def _stop_active_embeds(self) -> None:
        for w in self._active_embeds:
            try:
                w.stop()
            except Exception:
                pass


# ── helpers ─────────────────────────────────────────────────────────────


def _iter_items(items):
    """Recurse into Row/Card so collectors see all leaf items."""
    for it in items:
        yield it
        if isinstance(it, Row):
            yield from _iter_items(it.cells)
        elif isinstance(it, Card):
            yield from _iter_items(it.items)


def _safe_call(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _pango_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
