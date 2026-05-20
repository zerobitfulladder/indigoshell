"""Floating eye-dropper that follows the cursor.

Opened via `indigoshell open color-picker` (bind to a WM key). Grabs the
pointer with a crosshair cursor, samples the X root window each frame to
build a magnified view around the cursor, and shows the center pixel's
hex + RGB. Left-click copies the hex to the clipboard; Escape / right-click
cancels.

Window-level rather than a widget because every interesting behavior here
is window-scoped: pointer grab, per-frame `Gtk.Window.move`, keep_above +
sticky, and click-to-dismiss.
"""

import math
import time
from typing import Any

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from ..style import css_color
from ..widgets.base import beveled_path
from .base import WindowKind

# ── Loupe geometry ──────────────────────────────────────────────────────
SAMPLE_N        = 13      # NxN pixels sampled around the cursor (odd → has a center)
CELL_PX         = 14      # on-screen size of each sampled pixel
GRID_PAD        = 10      # padding around the magnified grid
SWATCH_PX       = 36      # color swatch square
READOUT_H       = 56      # text+swatch row height
TICK_MS         = 16      # ~60fps; lightweight Cairo draws

GRID_PX         = SAMPLE_N * CELL_PX
WIN_W           = GRID_PX + GRID_PAD * 2
WIN_H           = GRID_PX + GRID_PAD * 2 + READOUT_H

# Distance from cursor to the picker's top-left corner. Positive offsets
# keep the cursor outside the window so we never sample our own pixels.
CURSOR_OFFSET   = (24, 24)


class ColorPickerKind(WindowKind):
    singleton = True

    def __init__(self, name: str = "color-picker") -> None:
        self.name = name
        self._timer_id: int | None = None
        self._seat: Any = None
        self._cursor: tuple[int, int] = (0, 0)
        self._pixbuf = None             # current SAMPLE_N x SAMPLE_N sample
        self._center_rgb: tuple[int, int, int] = (0, 0, 0)
        self._glow_started_at: float = 0.0
        self._win: Gtk.Window | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def build(self, store, params: dict, *, anchor: Any = None, config: dict | None = None) -> Gtk.Window:
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self._win = win
        win.set_decorated(False)
        win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        win.set_keep_above(True)
        win.stick()
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_wmclass("indigoshell-popup", "indigoshell-color-picker")
        win.set_accept_focus(True)
        win.set_default_size(WIN_W, WIN_H)
        win.set_size_request(WIN_W, WIN_H)
        win.set_resizable(False)

        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        win.set_app_paintable(True)

        area = Gtk.DrawingArea()
        area.set_size_request(WIN_W, WIN_H)
        area.connect("draw", self._draw)
        win.add(area)

        win.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        win.connect("button-press-event", self._on_click)
        win.connect("key-press-event", self._on_key)
        win.connect("map-event", self._on_map)
        win.connect("destroy", self._on_destroyed)

        # Seed the position so the first paint isn't at (0,0).
        cx, cy = self._read_cursor()
        self._cursor = (cx, cy)
        self._sample(cx, cy)
        win.move(cx + CURSOR_OFFSET[0], cy + CURSOR_OFFSET[1])
        return win

    def teardown(self, window: Gtk.Window) -> None:
        self._stop_timer()
        self._release_grab()
        self._win = None
        window.destroy()

    # ── grab + cursor ────────────────────────────────────────────────────
    def _on_map(self, win: Gtk.Window, _event) -> bool:
        gdk_window = win.get_window()
        display = Gdk.Display.get_default()
        if gdk_window is None or display is None:
            return False
        # Replace the pointer cursor with a crosshair while picking.
        gdk_window.set_cursor(Gdk.Cursor.new_from_name(display, "crosshair"))
        seat = display.get_default_seat()
        status = seat.grab(
            gdk_window,
            Gdk.SeatCapabilities.ALL,
            True,
            Gdk.Cursor.new_from_name(display, "crosshair"),
            None, None,
        )
        if status == Gdk.GrabStatus.SUCCESS:
            self._seat = seat
        self._glow_started_at = time.monotonic()
        if self._timer_id is None:
            self._timer_id = GLib.timeout_add(TICK_MS, self._tick)
        return False

    def _release_grab(self) -> None:
        if self._seat is not None:
            self._seat.ungrab()
            self._seat = None

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _on_destroyed(self, _w) -> None:
        self._stop_timer()
        self._release_grab()
        self._win = None

    # ── per-frame update ────────────────────────────────────────────────
    def _tick(self) -> bool:
        win = self._win
        if win is None:
            self._timer_id = None
            return False
        cx, cy = self._read_cursor()
        if (cx, cy) != self._cursor:
            self._cursor = (cx, cy)
            x, y = self._placement(cx, cy)
            win.move(x, y)
        self._sample(cx, cy)
        win.queue_draw()
        return True

    def _read_cursor(self) -> tuple[int, int]:
        display = Gdk.Display.get_default()
        if display is None:
            return (0, 0)
        pointer = display.get_default_seat().get_pointer()
        # get_position returns (screen, x, y) for the pointer device.
        _screen, x, y = pointer.get_position()
        return (int(x), int(y))

    def _placement(self, cx: int, cy: int) -> tuple[int, int]:
        """Offset from cursor, flipped at monitor edges so the picker
        never lands off-screen."""
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_point(cx, cy) if display else None
        ox, oy = CURSOR_OFFSET
        x = cx + ox
        y = cy + oy
        if monitor is not None:
            geo = monitor.get_geometry()
            if x + WIN_W > geo.x + geo.width:
                x = cx - WIN_W - ox
            if y + WIN_H > geo.y + geo.height:
                y = cy - WIN_H - oy
            x = max(geo.x, x)
            y = max(geo.y, y)
        return (x, y)

    def _sample(self, cx: int, cy: int) -> None:
        root = Gdk.get_default_root_window()
        if root is None:
            return
        half = SAMPLE_N // 2
        pb = Gdk.pixbuf_get_from_window(root, cx - half, cy - half, SAMPLE_N, SAMPLE_N)
        self._pixbuf = pb
        if pb is None:
            return
        data = pb.get_pixels()
        stride = pb.get_rowstride()
        nchan = pb.get_n_channels()
        o = half * stride + half * nchan
        self._center_rgb = (data[o], data[o + 1], data[o + 2])

    # ── input ────────────────────────────────────────────────────────────
    def _on_click(self, _win, event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 1:
            r, g, b = self._center_rgb
            hex_value = f"#{r:02X}{g:02X}{b:02X}"
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(hex_value, -1)
            clipboard.store()
            if self._daemon is not None:
                self._daemon.close(self.name)
            return True
        if event.button == 3:
            if self._daemon is not None:
                self._daemon.close(self.name)
            return True
        return False

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if self._daemon is not None:
                self._daemon.close(self.name)
            return True
        return False

    # ── paint ────────────────────────────────────────────────────────────
    def _draw(self, _w, cr) -> bool:
        width, height = WIN_W, WIN_H

        # Transparent base — Cairo paints the panel chrome.
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_SOURCE)

        bevel = theme.POPUP_BEVEL
        corners = theme.POPUP_BEVEL_CORNERS
        border_thick = 1.5
        inset = border_thick / 2

        # Background fill, clipped to the beveled path.
        bg = Gdk.RGBA()
        if not bg.parse(css_color(theme.POPUP_BG)):
            bg.parse("rgba(0,0,0,0.7)")
        cr.set_source_rgba(bg.red, bg.green, bg.blue, bg.alpha)
        beveled_path(cr, width, height, bevel=bevel, corners=corners, inset=inset)
        cr.fill()

        cr.set_operator(cairo.OPERATOR_OVER)

        # ── magnified pixel grid ──
        gx = GRID_PAD
        gy = GRID_PAD
        pb = self._pixbuf
        if pb is not None:
            data = pb.get_pixels()
            stride = pb.get_rowstride()
            nchan = pb.get_n_channels()
            for j in range(SAMPLE_N):
                row = j * stride
                py = gy + j * CELL_PX
                for i in range(SAMPLE_N):
                    o = row + i * nchan
                    r = data[o] / 255.0
                    g = data[o + 1] / 255.0
                    b = data[o + 2] / 255.0
                    cr.set_source_rgb(r, g, b)
                    cr.rectangle(gx + i * CELL_PX, py, CELL_PX, CELL_PX)
                    cr.fill()
        else:
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.rectangle(gx, gy, GRID_PX, GRID_PX)
            cr.fill()

        # Center-pixel marker (the sampled pixel).
        half = SAMPLE_N // 2
        mx = gx + half * CELL_PX
        my = gy + half * CELL_PX
        marker = Gdk.RGBA()
        marker.parse(css_color(theme.HIGHLIGHT))
        cr.set_source_rgba(marker.red, marker.green, marker.blue, 1.0)
        cr.set_line_width(1.5)
        cr.rectangle(mx - 0.5, my - 0.5, CELL_PX + 1, CELL_PX + 1)
        cr.stroke()

        # Grid frame.
        cr.set_source_rgba(marker.red, marker.green, marker.blue, 0.35)
        cr.set_line_width(1.0)
        cr.rectangle(gx - 0.5, gy - 0.5, GRID_PX + 1, GRID_PX + 1)
        cr.stroke()

        # ── readout row (swatch + hex + rgb) ──
        rr, gg, bb = self._center_rgb
        hex_value = f"#{rr:02X}{gg:02X}{bb:02X}"
        rgb_value = f"rgb({rr}, {gg}, {bb})"

        rx = GRID_PAD
        ry = GRID_PAD + GRID_PX + 10
        # Swatch.
        cr.set_source_rgb(rr / 255, gg / 255, bb / 255)
        cr.rectangle(rx, ry, SWATCH_PX, SWATCH_PX)
        cr.fill()
        cr.set_source_rgba(marker.red, marker.green, marker.blue, 0.6)
        cr.set_line_width(1.0)
        cr.rectangle(rx - 0.5, ry - 0.5, SWATCH_PX + 1, SWATCH_PX + 1)
        cr.stroke()

        # Text.
        text_x = rx + SWATCH_PX + 12
        cr.select_font_face(
            theme.FONT.split(",")[0].strip(),
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_BOLD,
        )
        cr.set_font_size(15)
        cr.set_source_rgba(marker.red, marker.green, marker.blue, 1.0)
        cr.move_to(text_x, ry + 16)
        cr.show_text(hex_value)

        cr.select_font_face(
            theme.FONT.split(",")[0].strip(),
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_NORMAL,
        )
        cr.set_font_size(12)
        accent = Gdk.RGBA()
        accent.parse(css_color(theme.FG_STRONG))
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.95)
        cr.move_to(text_x, ry + 34)
        cr.show_text(rgb_value)

        # ── border stroke + bevel cutouts (glow alpha) ──
        phase = self._glow_phase()
        alpha = 0.55 + 0.45 * phase
        cr.set_source_rgba(marker.red, marker.green, marker.blue, alpha)
        cr.set_line_width(border_thick)
        beveled_path(cr, width, height, bevel=bevel, corners=corners, inset=inset)
        cr.stroke()
        return False

    def _glow_phase(self) -> float:
        if self._glow_started_at == 0.0:
            return 1.0
        t = time.monotonic() - self._glow_started_at
        return 0.5 - 0.5 * math.cos(2 * math.pi * t / 2.4)
