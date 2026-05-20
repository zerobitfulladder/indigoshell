"""Modal chord menu with press-to-arm / release-to-fire semantics.

Interaction:
  • key-DOWN on a registered key (or hover on a row) arms it,
    highlights it, and starts a rapid pulse.
  • key-UP on the same key (or click on the row) fires the action
    and closes the popup.
  • Escape (handled by PopupKind) destroys the window — pressing it
    while a row is armed cancels because release-after-destroy can't
    reach a torn-down handler.
  • Mouse-leave on an armed row also disarms.

Each row paints its own beveled background via Cairo; the host popup
should be fully transparent (no bg) so only the rows are visible.

Pair with PopupKind(name=<n>, content=Menu(popup_name=<n>, items=...)).
Daemon close is deferred-imported inside the commit paths to avoid a
widgets ← core.daemon ← registry ← windows ← widgets cycle.
"""

from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from .base import Widget, beveled_path


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str
    action: Callable[[], None]


# Row background colors (r, g, b, a) — pre-resolved from the cyberpunk
# palette so the cairo draw path stays allocation-free.
_RGBA_IDLE   = (23 / 255,  6 / 255,  32 / 255, 0.65)   # violet-black slab
_RGBA_ARMED  = ( 5 / 255, 217 / 255, 232 / 255, 0.22)  # cyan-bright
_RGBA_PULSE  = (252 / 255, 238 / 255,  12 / 255, 0.40) # yellow-bright
_RGBA_PICKED = (255 / 255, 42 / 255, 109 / 255, 0.32)  # magenta-bright steady
_RGBA_REJECT = (255 / 255, 42 / 255, 109 / 255, 0.75)  # magenta-bright "no"
_RGBA_BORDER = ( 5 / 255, 217 / 255, 232 / 255, 0.85)  # cyan-bright frame

_MODIFIER_KEYS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
    "Caps_Lock", "Num_Lock", "ISO_Level3_Shift",
})


class Menu(Widget):
    PULSE_MS = 80
    ROW_SPACING = 4
    ROW_HEIGHT = 38   # pin uniform height so per-menu Pango variance doesn't bleed through
    REJECT_FLASH_MS = 70
    REJECT_TOGGLES = 4  # on/off/on/off → ~280ms total

    def __init__(
        self,
        popup_name: str,
        items: list[MenuItem],
        *,
        bevel: int = 8,
        bevel_corners: tuple[str, ...] = ("top-right", "bottom-left"),
        auto_close: bool = True,
        on_cancel: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.popup_name = popup_name
        self.items = list(items)
        self.bevel = max(0, bevel)
        self.bevel_corners = tuple(bevel_corners)
        # When False, _commit runs the item action but leaves the popup
        # open. Used by PipelineKind so the menu stays visible while a
        # follow-up toast renders below it.
        self.auto_close = auto_close
        # Optional Escape handler. When set, Escape is consumed by the
        # menu (does NOT propagate to PopupKind's close handler) and
        # fires on_cancel — used by Pipeline to treat Escape as
        # "pick the cancel option" instead of dismissing the cascade.
        self.on_cancel = on_cancel
        # Becomes False after the first commit when auto_close=False
        # (pipeline mode): the menu stays visible as history but stops
        # accepting input — you can't pick a second option once the
        # flow has moved on to the next stage.
        self._active: bool = True
        # Pipeline mode: the key of the picked item stays "lit" with
        # the steady picked-row color so the user can see (in the
        # frozen menu) which option moved the flow forward.
        self._picked: str | None = None
        self._rows: dict[str, Gtk.Widget] = {}
        self._toplevel: Gtk.Window | None = None
        self._press_id: int | None = None
        self._release_id: int | None = None
        self._armed: str | None = None
        self._pulse_id: int | None = None
        self._pulse_on: bool = False
        self._reject_id: int | None = None
        self._reject_on: bool = False
        self._reject_remaining: int = 0

    # ── construction ─────────────────────────────────────────────────────
    def build_widget(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.ROW_SPACING)
        for item in self.items:
            box.pack_start(self._build_row(item), False, False, 0)
        return box

    @staticmethod
    def _row_label(text: str, css_class: str) -> Gtk.Label:
        """Label with single-line-mode and zero padding so its allocated
        height is purely the font's line height — kills the per-menu Pango
        variance that otherwise leaks through to the row's natural size."""
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class(css_class)
        lbl.set_single_line_mode(True)
        lbl.set_valign(Gtk.Align.CENTER)
        return lbl

    def _build_row(self, item: MenuItem) -> Gtk.Widget:
        ev = Gtk.EventBox()
        ev.set_visible_window(False)
        ev.set_size_request(-1, self.ROW_HEIGHT)
        ev.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        inner.set_margin_start(18)
        inner.set_margin_end(18)
        inner.set_valign(Gtk.Align.CENTER)

        text_lbl = self._row_label(item.label, "menu-label")
        text_lbl.set_xalign(1.0)

        key_lbl = Gtk.Label()
        key_lbl.set_markup(
            f"<span color='{theme.YELLOW_MID}'>//</span> "
            f"<span color='{theme.HIGHLIGHT}'>{item.key}</span>"
        )
        key_lbl.get_style_context().add_class("menu-key")
        key_lbl.set_single_line_mode(True)
        key_lbl.set_valign(Gtk.Align.CENTER)

        inner.pack_start(text_lbl, True, True, 0)  # expand: pushes [N] to the right
        inner.pack_end(key_lbl, False, False, 0)
        ev.add(inner)

        # Paint our beveled bg *before* children render.
        ev.connect("draw", self._draw_row, item.key)
        ev.connect("enter-notify-event", self._on_row_enter, item.key)
        ev.connect("leave-notify-event", self._on_row_leave, item.key)
        ev.connect("button-release-event", self._on_row_click, item.key)

        self._rows[item.key] = ev
        return ev

    # ── styling ──────────────────────────────────────────────────────────
    def default_css(self) -> str:
        sel = f"#{self.name}"
        return (
            f"{sel} {{ background: transparent; }}"
            f"{sel} .menu-key {{"
            f" color: {theme.HIGHLIGHT};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" font-weight: bold;"
            f" }}"
            f"{sel} .menu-label {{"
            f" color: {theme.FG_STRONG};"
            f" font-family: {theme.FONT};"
            f" font-size: {theme.FONT_SIZE}px;"
            f" letter-spacing: 1px;"
            f" }}"
            f"{sel} .armed .menu-key,"
            f"{sel} .armed .menu-label {{ color: {theme.FG_ACCENT}; }}"
        )

    # ── row drawing ──────────────────────────────────────────────────────
    def _draw_row(self, widget: Gtk.Widget, cr, key: str) -> bool:
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        # Inset by half the stroke width so the border stays crisp
        # inside the row's allocation instead of bleeding outside.
        line_w = 1.2
        inset = line_w / 2
        beveled_path(cr, w, h, bevel=self.bevel, corners=self.bevel_corners, inset=inset)
        r, g, b, a = self._row_rgba(key)
        cr.set_source_rgba(r, g, b, a)
        cr.fill_preserve()
        cr.set_source_rgba(*_RGBA_BORDER)
        cr.set_line_width(line_w)
        cr.stroke()
        return False  # let children render on top

    def _row_rgba(self, key: str) -> tuple[float, float, float, float]:
        if self._reject_on:
            return _RGBA_REJECT
        if self._picked == key:
            return _RGBA_PICKED
        if self._armed != key:
            return _RGBA_IDLE
        return _RGBA_PULSE if self._pulse_on else _RGBA_ARMED

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        super().start()
        if self.gtk_widget is None:
            return
        top = self.gtk_widget.get_toplevel()
        if not isinstance(top, Gtk.Window):
            return
        self._toplevel = top
        self._press_id = top.connect("key-press-event", self._on_press)
        self._release_id = top.connect("key-release-event", self._on_release)

    def stop(self) -> None:
        self._disarm()
        if self._reject_id is not None:
            GLib.source_remove(self._reject_id)
            self._reject_id = None
        if self._toplevel is not None:
            if self._press_id is not None:
                self._toplevel.disconnect(self._press_id)
            if self._release_id is not None:
                self._toplevel.disconnect(self._release_id)
        self._press_id = None
        self._release_id = None
        self._toplevel = None
        super().stop()

    # ── keyboard handling ────────────────────────────────────────────────
    def _on_press(self, _w, event) -> bool:
        # Pipeline mode: the menu locks itself after the first commit so
        # the user can't fire another action while a downstream stage
        # is running. Eating every key here also prevents Escape from
        # closing the cascade once it has progressed.
        if not self._active:
            return True
        key = self._normalize(event.keyval)
        if key in self._rows:
            if self._armed != key:
                self._arm(key)
            return True
        if key == "Escape":
            if self.on_cancel is not None:
                # Pipeline mode with declared cancel: Escape is "pick the
                # cancel option" — consume the event so PopupKind doesn't
                # close, then let the orchestrator run the cancel command.
                try:
                    self.on_cancel()
                except Exception:
                    pass
                return True
            if not self.auto_close:
                # Flow menu without a cancel command — swallow Escape.
                # Flow menus stay visible until the cascade finishes; a
                # bare Escape mustn't quietly close just this one popup
                # and orphan the rest of the cascade behind it.
                return True
            # Non-flow menu (e.g. power / layout / profile): let
            # PopupKind close on Escape, original behavior.
            return False
        # Lone modifier presses: ignore silently — they're rarely a mistake.
        if key in _MODIFIER_KEYS:
            return False
        self._flash_reject()
        return True

    def _on_release(self, _w, event) -> bool:
        if not self._active:
            return True
        key = self._normalize(event.keyval)
        if self._armed is None or key != self._armed:
            return False
        self._commit(key)
        return True

    # ── mouse handling ───────────────────────────────────────────────────
    def _on_row_enter(self, _w, _e, key: str) -> bool:
        if not self._active:
            return False
        self._arm(key)
        return False

    def _on_row_leave(self, _w, _e, key: str) -> bool:
        if not self._active:
            return False
        if self._armed == key:
            self._disarm()
        return False

    def _on_row_click(self, _w, event, key: str) -> bool:
        if not self._active or event.button != 1:
            return False
        self._commit(key)
        return True

    # ── arm / pulse / commit ─────────────────────────────────────────────
    def _arm(self, key: str) -> None:
        if self._armed == key:
            return
        if self._armed is not None:
            prev = self._rows.get(self._armed)
            if prev is not None:
                prev.get_style_context().remove_class("armed")
                prev.queue_draw()
        self._armed = key
        row = self._rows.get(key)
        if row is not None:
            row.get_style_context().add_class("armed")
            row.queue_draw()
        self._pulse_on = False
        if self._pulse_id is None:
            self._pulse_id = GLib.timeout_add(self.PULSE_MS, self._tick_pulse)

    def _disarm(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        if self._armed is not None:
            row = self._rows.get(self._armed)
            if row is not None:
                row.get_style_context().remove_class("armed")
                row.queue_draw()
        self._armed = None
        self._pulse_on = False

    def _tick_pulse(self) -> bool:
        if self._armed is None:
            self._pulse_id = None
            return False
        row = self._rows.get(self._armed)
        if row is None:
            self._pulse_id = None
            return False
        self._pulse_on = not self._pulse_on
        row.queue_draw()
        return True

    # ── reject flash ─────────────────────────────────────────────────────
    def _flash_reject(self) -> None:
        if self._reject_id is not None:
            GLib.source_remove(self._reject_id)
            self._reject_id = None
        self._reject_remaining = self.REJECT_TOGGLES
        self._reject_on = True
        self._redraw_all()
        self._reject_id = GLib.timeout_add(self.REJECT_FLASH_MS, self._tick_reject)

    def _tick_reject(self) -> bool:
        self._reject_remaining -= 1
        if self._reject_remaining <= 0:
            self._reject_on = False
            self._reject_id = None
            self._redraw_all()
            return False
        self._reject_on = not self._reject_on
        self._redraw_all()
        return True

    def _redraw_all(self) -> None:
        for row in self._rows.values():
            row.queue_draw()

    def _commit(self, key: str) -> None:
        item = next((i for i in self.items if i.key == key), None)
        self._disarm()
        if item is None:
            return
        try:
            item.action()
        finally:
            if self.auto_close:
                from ..core.daemon import get_daemon  # deferred (import cycle)
                get_daemon().close(self.popup_name)
            else:
                # Pipeline mode: lock the menu so further keys/clicks
                # are ignored — the flow has progressed past this stage.
                # Mark the picked row so it stays lit as visual history.
                # Release the popup's seat grab so the newly-opened
                # next-stage popup can claim keyboard focus normally.
                self._active = False
                self._picked = key
                row = self._rows.get(key)
                if row is not None:
                    row.queue_draw()
                self._release_grab()

    def _release_grab(self) -> None:
        """Ask the hosting PopupKind to drop its seat grab. Used when
        the menu transitions out of being the active flow item so the
        next popup can receive keyboard input."""
        from ..core.daemon import get_daemon
        kind = get_daemon().kinds.get(self.popup_name)
        # Only PopupKind has _release_grab; getattr keeps the static
        # type checker happy and the runtime guarded in one shot.
        release = getattr(kind, "_release_grab", None)
        if callable(release):
            try:
                release()
            except Exception:
                pass

    @staticmethod
    def _normalize(keyval: int) -> str:
        name = Gdk.keyval_name(keyval) or ""
        return name[3:] if name.startswith("KP_") else name
