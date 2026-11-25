#!/usr/bin/env python3
"""indigoshell's one-canvas layout on a wlr-layer-shell compositor (niri).

The design under test: the shell draws *everything* — both bars, the
dropdown, any panel — on one overlay surface above the windows, and
reserves the bars' strips with separate invisible surfaces so the
compositor tiles windows between them.

    reserve-top     layer bottom, anchored top+left+right,    exclusive 42
    reserve-bottom  layer bottom, anchored bottom+left+right, exclusive 42
                    (both: 1x1 transparent buffer, empty input region)
    overlay         layer overlay, anchored on all four edges,
                    exclusive -1 (ignores the reservations, covers them),
                    size from the configure, input region = what is drawn

Run inside a compositor with WAYLAND_DISPLAY pointing at it:

    overlay.py [--gpu] [--blur] [--kb exclusive|on_demand] [--catch-outside]
               [--hide-on-fullscreen] [--layer overlay|top] [--verbose]

Commands, one per line on stdin (SIGUSR1 also toggles the dropdown):

    toggle | open | close | up | down    the dropdown
    kb exclusive|on_demand               keyboard mode used on open
    catch on|off                         input region = whole surface while open
    blur on|off                          blur region behind the dropdown
    full                                 one full redraw
    bench [N]                            time full and clock-only redraws,
                                         here and offscreen at 2560x1440
    stats                                frame time summary so far
    quit
"""

import argparse
import datetime
import mmap
import os
import selectors
import signal
import struct
import sys
import time

import skia
from pywayland import ffi
from pywayland.client import Display

from indigoshell import shapes, text, theme

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocols  # noqa: E402

wl = protocols.load()
core = wl.wayland
LS = wl.wlr_layer_shell_unstable_v1
Anchor = LS.ZwlrLayerSurfaceV1.anchor
Kb = LS.ZwlrLayerSurfaceV1.keyboard_interactivity

BAR = theme.BAR_HEIGHT
BTN_LEFT = 0x110
KEY_ESC, KEY_ENTER, KEY_UP, KEY_DOWN = 1, 28, 103, 108

# Launcher look (indigoshell/widgets/launcher.py): colours and metrics only.
ROW_H, ROW_GAP, PAD_X, GAP, BEVEL = 40, 4, 12, 12, 8
NAME_SIZE, META_SIZE = theme.FONT_SIZE - 2, theme.FONT_SIZE - 4
SELECTED = (theme.CYAN_BRIGHT, 0.22)
BORDER = (theme.CYAN_BRIGHT, 0.85)
ROWS = [
    ("kitty", "Terminal emulator"),
    ("Flux", "Web browser"),
    ("Files", "File manager"),
    ("Spotify", "Music player"),
    ("Visual Studio Code", "Code editor"),
    ("GIMP", "Image editor"),
    ("Settings", "indigoshell"),
    ("Lock", "Session"),
]


def ptr(proxy) -> int:
    return int(ffi.cast("uintptr_t", proxy._ptr))


def irect(r: skia.Rect) -> skia.IRect:
    return r.roundOut()


# ── the scene: everything the shell draws, on one canvas ──────────────
class Scene:
    def __init__(self, mode_label: str) -> None:
        self.w = self.h = 0
        self.mode_label = mode_label
        self.open = False
        self.selected = 0
        self.blur = False
        self.hidden = False            # a fullscreen window is active
        self.catch_outside = False     # input region = whole surface while open
        self.now = datetime.datetime.now()

    def layout(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.top = skia.Rect.MakeXYWH(0, 0, w, BAR)
        self.bottom = skia.Rect.MakeXYWH(0, h - BAR, w, BAR)
        dw = min(900, round(w * 0.6))
        n = len(ROWS)
        dh = 12 + 30 + 8 + n * ROW_H + (n - 1) * ROW_GAP + 12
        self.drop = skia.Rect.MakeXYWH(0, BAR, dw, dh)
        size = text.pt(18)
        cw = text.advance(size, bold=True, chars=8)
        x = w - theme.BAR_MARGIN - theme.SPACING_LG - cw
        self.clock_size = size
        self.clock = skia.Rect.MakeXYWH(x, 0, cw, BAR - theme.BAR_RULE_THICK)

    # Regions the compositor needs: what takes input, what is blurred.
    def input_rects(self) -> list[skia.IRect]:
        if self.hidden:
            return []
        if self.open and self.catch_outside:
            return [skia.IRect.MakeWH(self.w, self.h)]
        rects = [irect(self.top), irect(self.bottom)]
        if self.open:
            rects.append(irect(self.drop))
        return rects

    def blur_rects(self) -> list[skia.IRect]:
        return [irect(self.drop)] if self.open and self.blur and not self.hidden else []

    def row_rect(self, i: int) -> skia.Rect:
        d = self.drop
        y = d.top() + 12 + 30 + 8 + i * (ROW_H + ROW_GAP)
        return skia.Rect.MakeXYWH(d.left() + 12, y, d.width() - 24, ROW_H)

    def row_at(self, x: float, y: float) -> int | None:
        for i in range(len(ROWS)):
            if self.row_rect(i).contains(x, y):
                return i
        return None

    # ── paint ────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas, clip: skia.IRect | None) -> None:
        canvas.save()
        if clip is not None:
            canvas.clipRect(skia.Rect.Make(clip))
        canvas.clear(skia.ColorTRANSPARENT)
        if not self.hidden:
            self._bar(canvas, self.top, rule_at_bottom=True)
            self._top_content(canvas)
            self._bar(canvas, self.bottom, rule_at_bottom=False)
            self._bottom_content(canvas)
            if self.open and not canvas.quickReject(self.drop):
                self._dropdown(canvas)
        canvas.restore()

    def _bar(self, canvas, r: skia.Rect, rule_at_bottom: bool) -> None:
        if canvas.quickReject(r):
            return
        bg = skia.Paint(Color=theme.color(theme.BAR_BG))
        bg.setBlendMode(skia.BlendMode.kSrc)
        canvas.drawRect(r, bg)
        t = theme.BAR_RULE_THICK
        rule = skia.Rect.MakeXYWH(r.left(), r.bottom() - t if rule_at_bottom else r.top(),
                                  r.width(), t)
        canvas.save()
        canvas.clipRect(r)              # the glow stays inside the bar, as on X11
        glow = skia.Paint(AntiAlias=True, Color=theme.color(theme.BAR_RULE, alpha=0.55))
        glow.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle,
                                                    theme.BAR_RULE_GLOW))
        canvas.drawRect(rule, glow)
        canvas.drawRect(rule, skia.Paint(Color=theme.color(theme.BAR_RULE)))
        canvas.restore()

    def _top_content(self, canvas) -> None:
        r = self.top
        x = r.left() + theme.BAR_MARGIN + theme.SPACING_LG
        size = theme.FONT_SIZE
        base = text.baseline_in(skia.Rect.MakeXYWH(0, 0, r.width(), BAR - 2), size)
        label = "indigoshell on niri"
        text.draw(canvas, label, x, base, size, theme.color(theme.FG_STRONG), bold=True)
        x += text.measure(label, size, bold=True) + theme.SPACING_XL
        hint = "click: apps" if not self.open else "esc: close"
        text.draw(canvas, hint, x, base, theme.FONT_SIZE - 4, theme.color(theme.BASE_MUTED))
        # Date, left of the clock: changes once a day, not part of the tick.
        c = self.clock
        date = self.now.strftime("%a %d %b")
        ds = text.pt(11)
        text.draw(canvas, date, c.left() - theme.SPACING_SM - text.measure(date, ds),
                  text.baseline_in(c, self.clock_size, bold=True), ds,
                  theme.color(theme.BASE_MUTED))
        if not canvas.quickReject(c):
            value = self.now.strftime("%H:%M:%S")
            text.draw(canvas, value, c.right() - text.measure(value, self.clock_size, bold=True),
                      text.baseline_in(c, self.clock_size, bold=True), self.clock_size,
                      theme.color(theme.CYAN_BRIGHT), bold=True)

    def _bottom_content(self, canvas) -> None:
        r = self.bottom
        if canvas.quickReject(r):
            return
        size = theme.FONT_SIZE - 2
        base = text.baseline_in(skia.Rect.MakeLTRB(r.left(), r.top() + 2, r.right(), r.bottom()),
                                size)
        x = r.left() + theme.BAR_MARGIN + theme.SPACING_LG
        for i, name in enumerate(("1", "2", "3", "4", "5")):
            col = theme.WORKSPACE_CURRENT_FG if i == 0 else (
                theme.WORKSPACE_OCCUPIED_FG if i < 3 else theme.WORKSPACE_EMPTY_FG)
            text.draw(canvas, name, x, base, theme.FONT_SIZE, theme.color(col), bold=True)
            x += text.advance(theme.FONT_SIZE, True) + theme.SPACING_MD
        x += theme.BAR_SPACING
        text.draw(canvas, "one overlay surface · bars reserve 42 px", x, base, size,
                  theme.color(theme.HUD_FG))
        info = f"{self.w}×{self.h} · {self.mode_label}"
        text.draw(canvas, info, r.right() - theme.BAR_MARGIN - theme.SPACING_LG
                  - text.measure(info, size), base, size, theme.color(theme.BASE_MUTED))

    def _dropdown(self, canvas) -> None:
        d = self.drop
        path = shapes.beveled(d.left(), d.top(), d.width(), d.height(),
                              bevel=theme.POPUP_BEVEL, corners=theme.POPUP_BEVEL_CORNERS,
                              inset=1.0)
        fill = skia.Paint(AntiAlias=True)
        # Frosted: the plate has to be see-through for a blur behind it
        # to show; POPUP_BG's ~95% alpha would hide it.
        fill.setColor(theme.color("#170620", alpha=0.55) if self.blur
                      else theme.color(theme.POPUP_BG))
        fill.setBlendMode(skia.BlendMode.kSrc)
        canvas.drawPath(path, fill)
        stroke = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeWidth=2.0)
        stroke.setColor(theme.color(theme.POPUP_BORDER))
        canvas.drawPath(path, stroke)

        # The launcher's query line and divider.
        q = skia.Rect.MakeXYWH(d.left() + 12 + PAD_X, d.top() + 12, d.width() - 48, 30)
        text.draw(canvas, "RUN", q.left(), text.baseline_in(q, theme.FONT_SIZE), theme.FONT_SIZE,
                  theme.color(theme.CYAN_MID), bold=True, tracking=1.0)
        text.draw(canvas, "apps on the overlay", q.left() + 60,
                  text.baseline_in(q, theme.FONT_SIZE), theme.FONT_SIZE,
                  theme.color(theme.YELLOW_MID))
        canvas.drawRect(skia.Rect.MakeXYWH(d.left() + 12, q.bottom() + 4, d.width() - 24, 1),
                        skia.Paint(Color=theme.color(theme.CYAN_DIM)))

        for i, (name, meta) in enumerate(ROWS):
            r = self.row_rect(i)
            sel = i == self.selected
            if sel:
                slab = shapes.beveled(r.left(), r.top(), r.width(), r.height(), bevel=BEVEL,
                                      corners=("top-right", "bottom-left"), inset=0.6)
                canvas.drawPath(slab, skia.Paint(AntiAlias=True, Color=theme.color(*SELECTED)))
                canvas.drawPath(slab, skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                                                 StrokeWidth=1.2, Color=theme.color(*BORDER)))
            icon = skia.Rect.MakeXYWH(r.left() + PAD_X, r.centerY() - 13, 26, 26)
            initial = name[:1]
            text.draw(canvas, initial,
                      icon.centerX() - text.measure(initial, NAME_SIZE, True) / 2,
                      text.baseline_in(icon, NAME_SIZE, True), NAME_SIZE,
                      theme.color(theme.BASE_MUTED), bold=True)
            x = icon.right() + GAP
            text.draw(canvas, name, x, text.baseline_in(r, NAME_SIZE), NAME_SIZE,
                      theme.color(theme.YELLOW_BRIGHT if sel else theme.HUD_FG_BRIGHT))
            text.draw(canvas, meta, r.right() - PAD_X - text.measure(meta, META_SIZE),
                      text.baseline_in(r, META_SIZE), META_SIZE,
                      theme.color(theme.CYAN_MID if sel else theme.BASE_MUTED))


# ── renderers: SHM (CPU raster) and EGL (GPU) ──────────────────────────
class ShmBuffer:
    def __init__(self, shm, w: int, h: int) -> None:
        self.w, self.h = w, h
        stride, size = w * 4, w * 4 * h
        fd = os.memfd_create("indigoshell-overlay", os.MFD_CLOEXEC)
        os.ftruncate(fd, size)
        self.map = mmap.mmap(fd, size)
        pool = shm.create_pool(fd, size)
        self.buffer = pool.create_buffer(0, w, h, stride, core.WlShm.format.argb8888)
        pool.destroy()
        os.close(fd)
        self.busy = False
        self.buffer.dispatcher["release"] = self._release
        info = skia.ImageInfo.Make(w, h, skia.kBGRA_8888_ColorType, skia.kPremul_AlphaType)
        self.surface = skia.Surface.MakeRasterDirect(info, self.map, stride)
        # What in this buffer is older than the newest frame.
        self.stale = skia.Region(skia.IRect.MakeWH(w, h))

    def _release(self, *_):
        self.busy = False

    def destroy(self) -> None:
        self.surface = None
        self.buffer.destroy()
        self.map.close()


class ShmRenderer:
    name = "SHM"
    MAX_BUFFERS = 4

    def __init__(self, app) -> None:
        self.app = app
        self.buffers: list[ShmBuffer] = []
        self.size = (0, 0)

    def resize(self, w: int, h: int) -> None:
        for b in self.buffers:
            b.destroy()
        self.buffers = []
        self.size = (w, h)

    def _free_buffer(self) -> ShmBuffer | None:
        for b in self.buffers:
            if not b.busy:
                return b
        if len(self.buffers) < self.MAX_BUFFERS:
            b = ShmBuffer(self.app.shm, *self.size)
            self.buffers.append(b)
            mb = sum(x.w * x.h * 4 for x in self.buffers) / 2**20
            self.app.log(f"SHM: buffer {len(self.buffers)} allocated (all busy), "
                         f"{mb:.1f} MiB mapped")
            return b
        return None

    def present(self, damage: skia.Region) -> float | None:
        """Paint `damage` (plus whatever this buffer missed) and commit.
        Returns the paint time in ms, or None if every buffer is busy."""
        for b in self.buffers:
            b.stale.op(damage, skia.Region.kUnion_Op)
        buf = self._free_buffer()
        if buf is None:
            return None
        buf.stale.op(damage, skia.Region.kUnion_Op)
        canvas = buf.surface.getCanvas()
        t0 = time.perf_counter()
        it = skia.Region.Iterator(buf.stale)
        while not it.done():
            self.app.scene.paint(canvas, it.rect())
            it.next()
        ms = (time.perf_counter() - t0) * 1000
        buf.stale.setEmpty()
        s = self.app.surface
        s.attach(buf.buffer, 0, 0)
        it = skia.Region.Iterator(damage)
        while not it.done():
            r = it.rect()
            s.damage_buffer(r.left(), r.top(), r.width(), r.height())
            it.next()
        self.app.apply_pending_state()
        s.commit()
        buf.busy = True
        return ms

    def canvas_for_bench(self) -> skia.Canvas:
        # A buffer of its own, mapped like the real ones but never
        # attached: the compositor may still be reading the live ones.
        self._bench = ShmBuffer(self.app.shm, *self.size)
        return self._bench.surface.getCanvas()

    @staticmethod
    def offscreen(w: int, h: int) -> skia.Surface:
        return skia.Surface.MakeRaster(skia.ImageInfo.Make(
            w, h, skia.kBGRA_8888_ColorType, skia.kPremul_AlphaType))

    def sync(self) -> None:
        pass


class GpuRenderer:
    name = "GPU"

    def __init__(self, app) -> None:
        import egl
        self.app = app
        self.gl = egl.WaylandGL(ptr(app.display))
        self.history: list[skia.Region] = []       # damage of recent frames, newest last
        self.size = (0, 0)

    def resize(self, w: int, h: int) -> None:
        if self.gl.surface is None:
            self.gl.create(ptr(self.app.surface), w, h)
            self.name = f"GPU {self.gl.renderer}"
        else:
            self.gl.resize(w, h)
        self.size = (w, h)
        self.history = []

    def present(self, damage: skia.Region) -> float:
        age = self.gl.buffer_age()
        paint = skia.Region(damage)
        if age == 0 or age > len(self.history) + 1:
            paint = skia.Region(skia.IRect.MakeWH(*self.size))
        else:
            for old in self.history[len(self.history) - (age - 1):]:
                paint.op(old, skia.Region.kUnion_Op)
        canvas = self.gl.skia_surface.getCanvas()
        t0 = time.perf_counter()
        it = skia.Region.Iterator(paint)
        while not it.done():
            self.app.scene.paint(canvas, it.rect())
            it.next()
        self.gl.context.flushAndSubmit()
        ms = (time.perf_counter() - t0) * 1000
        self.history = (self.history + [skia.Region(damage)])[-4:]
        rects = []
        it = skia.Region.Iterator(damage)
        while not it.done():
            rects.append(it.rect())
            it.next()
        self.app.apply_pending_state()
        self.gl.swap(rects)              # attaches, damages and commits
        return ms

    def canvas_for_bench(self) -> skia.Canvas:
        return self.gl.skia_surface.getCanvas()

    def offscreen(self, w: int, h: int) -> skia.Surface:
        return skia.Surfaces.MakeRenderTarget(
            self.gl.context, skia.Budgeted.kYes,
            skia.ImageInfo.Make(w, h, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType))

    def sync(self) -> None:
        self.gl.finish()


# ── the client ────────────────────────────────────────────────────────
class Strip:
    """An invisible reservation: exclusive zone, no pixels, no input."""

    def __init__(self, app, edge: int, name: str) -> None:
        self.app = app
        self.surface = app.compositor.create_surface()
        empty = app.compositor.create_region()
        self.surface.set_input_region(empty)
        empty.destroy()
        self.layer = app.layer_shell.get_layer_surface(
            self.surface, None, LS.ZwlrLayerShellV1.layer.bottom, name)
        self.layer.set_anchor(edge | Anchor.left | Anchor.right)
        self.layer.set_size(0, BAR)
        self.layer.set_exclusive_zone(BAR)
        self.layer.set_keyboard_interactivity(Kb.none)
        self.layer.dispatcher["configure"] = self._configure
        self.layer.dispatcher["closed"] = lambda *_: app.log(f"{name}: closed")
        self.viewport = None
        self.buffer = None
        self.name = name
        self.surface.commit()

    def _configure(self, _layer, serial: int, w: int, h: int) -> None:
        app = self.app
        self.layer.ack_configure(serial)
        if self.buffer is None:
            # One transparent pixel, stretched to the strip by a viewport.
            # niri 26.04 has wp_viewporter but no single-pixel-buffer, so
            # the pixel is a 1x1 wl_shm buffer (a fresh memfd is zeroes).
            if app.viewporter is not None:
                self.viewport = app.viewporter.get_viewport(self.surface)
                if app.single_pixel is not None:
                    self.buffer = app.single_pixel.create_u32_rgba_buffer(0, 0, 0, 0)
                else:
                    self.shm = ShmBuffer(app.shm, 1, 1)
                    self.buffer = self.shm.buffer
            else:
                self.shm = ShmBuffer(app.shm, max(w, 1), max(h, 1))
                self.buffer = self.shm.buffer
            self.surface.attach(self.buffer, 0, 0)
            app.log(f"{self.name}: {'1x1 buffer + viewport' if self.viewport else 'full buffer'}")
        if self.viewport is not None:
            self.viewport.set_destination(max(w, 1), max(h, 1))
        self.surface.commit()
        app.log(f"{self.name}: configure {w}x{h}")


class App:
    def __init__(self, args) -> None:
        self.args = args
        self.globals: dict[str, tuple[int, int]] = {}
        self.display = Display(os.environ.get("WAYLAND_DISPLAY"))
        self.display.connect()
        self.registry = self.display.get_registry()
        self.registry.dispatcher["global"] = self._global
        self.registry.dispatcher["global_remove"] = lambda *a: None
        self.display.roundtrip()
        self._print_globals()

        def bind(iface, cls, want: int):
            g = self.globals.get(iface)
            if g is None:
                return None
            return self.registry.bind(g[0], cls, min(want, g[1]))

        self.compositor = bind("wl_compositor", core.WlCompositor, 6)
        self.shm = bind("wl_shm", core.WlShm, 1)
        self.layer_shell = bind("zwlr_layer_shell_v1", LS.ZwlrLayerShellV1, 5)
        self.viewporter = bind("wp_viewporter", wl.viewporter.WpViewporter, 1)
        self.single_pixel = bind("wp_single_pixel_buffer_manager_v1",
                                 wl.single_pixel_buffer_v1.WpSinglePixelBufferManagerV1, 1)
        self.effects = bind("ext_background_effect_manager_v1",
                            wl.ext_background_effect_v1.ExtBackgroundEffectManagerV1, 1)
        self.cursor_shape = bind("wp_cursor_shape_manager_v1",
                                 wl.cursor_shape_v1.WpCursorShapeManagerV1, 1)
        self.toplevels_mgr = bind("zwlr_foreign_toplevel_manager_v1",
                                  wl.wlr_foreign_toplevel_management_unstable_v1
                                  .ZwlrForeignToplevelManagerV1, 3)
        self.seat = bind("wl_seat", core.WlSeat, 7)
        if self.compositor is None or self.layer_shell is None:
            raise SystemExit("needs wl_compositor and zwlr_layer_shell_v1")
        self.log(f"layer-shell v{min(5, self.globals['zwlr_layer_shell_v1'][1])}")

        self.blur_caps = 0
        if self.effects is not None:
            self.effects.dispatcher["capabilities"] = self._effect_caps
        self.pointer = self.keyboard = self.cursor_device = None
        self.pointer_xy = (0.0, 0.0)
        self.pointer_in = False
        if self.seat is not None:
            self.seat.dispatcher["capabilities"] = self._seat_caps
            self.seat.dispatcher["name"] = lambda *a: None
        self.toplevels: dict[int, dict] = {}
        if self.toplevels_mgr is not None:
            self.toplevels_mgr.dispatcher["toplevel"] = self._toplevel
            self.toplevels_mgr.dispatcher["finished"] = lambda *a: None

        self.renderer = GpuRenderer(self) if args.gpu else ShmRenderer(self)
        self.scene = Scene("SHM" if not args.gpu else "GPU")
        self.scene.blur = args.blur
        self.scene.catch_outside = args.catch_outside
        self.frames: list[tuple[str, float]] = []

        self.strips = [Strip(self, Anchor.top, "indigoshell-reserve-top"),
                       Strip(self, Anchor.bottom, "indigoshell-reserve-bottom")]

        self.surface = self.compositor.create_surface()
        self.layer = self.layer_shell.get_layer_surface(
            self.surface, None, LS.ZwlrLayerShellV1.layer[args.layer], "indigoshell")
        self.layer.set_anchor(Anchor.top | Anchor.bottom | Anchor.left | Anchor.right)
        self.layer.set_size(0, 0)
        self.layer.set_exclusive_zone(-1)
        self.layer.set_keyboard_interactivity(Kb.none)
        self.layer.dispatcher["configure"] = self._configure
        self.layer.dispatcher["closed"] = lambda *_: self.quit("overlay closed")
        self.effect = (self.effects.get_background_effect(self.surface)
                       if self.effects is not None else None)
        self.kb_mode = Kb.none
        self.pending_state = True
        self.configured = False
        self.damage = skia.Region()
        self.running = True
        self.surface.commit()

    # ── registry ─────────────────────────────────────────────────────
    def _global(self, _reg, name: int, interface: str, version: int) -> None:
        self.globals[interface] = (name, version)

    def _print_globals(self) -> None:
        print("globals offered by the compositor:", flush=True)
        for iface, (_, ver) in sorted(self.globals.items()):
            print(f"  {iface:48s} v{ver}")
        sys.stdout.flush()

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def _effect_caps(self, _mgr, flags: int) -> None:
        self.blur_caps = flags
        self.log(f"background-effect capabilities: {flags} (blur={'yes' if flags & 1 else 'no'})")

    # ── overlay surface ──────────────────────────────────────────────
    def _configure(self, _layer, serial: int, w: int, h: int) -> None:
        self.layer.ack_configure(serial)
        self.log(f"overlay: configure {w}x{h}")
        if (w, h) != (self.scene.w, self.scene.h):
            self.scene.layout(w, h)
            self.renderer.resize(w, h)
            if self.args.gpu:
                gl = self.renderer.gl
                self.log(f"GPU: EGL {gl.egl_version} {gl.egl_vendor}; GL_VENDOR {gl.vendor}; "
                         f"GL_RENDERER {gl.renderer}; {gl.gl_version}")
                self.log(f"GPU: buffer_age={'yes' if gl.has_age else 'no'}, "
                         f"swap_with_damage={gl.swap_damage_name or 'no'}")
                if gl.software:
                    self.log("WARNING: GL renderer is a software rasteriser")
                self.scene.mode_label = f"GPU · {gl.renderer.split('/')[0]}"
            self.pending_state = True
            self.invalidate(skia.IRect.MakeWH(w, h))
        self.configured = True

    def invalidate(self, r: skia.IRect) -> None:
        self.damage.op(r, skia.Region.kUnion_Op)

    def apply_pending_state(self) -> None:
        """Double-buffered surface state that rides on the next commit."""
        if not self.pending_state:
            return
        self.pending_state = False
        region = self.compositor.create_region()
        for r in self.scene.input_rects():
            region.add(r.left(), r.top(), r.width(), r.height())
        self.surface.set_input_region(region)
        region.destroy()
        if self.effect is not None:
            rects = self.scene.blur_rects()
            if rects:
                blur = self.compositor.create_region()
                for r in rects:
                    blur.add(r.left(), r.top(), r.width(), r.height())
                self.effect.set_blur_region(blur)
                blur.destroy()
            else:
                self.effect.set_blur_region(None)
        want = Kb.none
        if self.scene.open and not self.scene.hidden:
            want = Kb.exclusive if self.args.kb == "exclusive" else Kb.on_demand
        if want != self.kb_mode:
            self.layer.set_keyboard_interactivity(want)
            self.kb_mode = want
            self.log(f"keyboard_interactivity -> {Kb(want).name}")

    def frame(self, kind: str) -> None:
        if not self.configured or self.damage.isEmpty():
            return
        damage, self.damage = self.damage, skia.Region()
        ms = self.renderer.present(damage)
        if ms is None:
            self.damage = damage        # every buffer busy: try again next tick
            return
        self.frames.append((kind, ms))
        b = damage.getBounds()
        if kind != "clock" or self.args.verbose:
            self.log(f"frame {kind}: painted in {ms:.2f} ms, damage {b.width()}x{b.height()}"
                     f"+{b.left()}+{b.top()}")

    # ── dropdown ─────────────────────────────────────────────────────
    def set_open(self, value: bool) -> None:
        if value == self.scene.open:
            return
        self.scene.open = value
        self.pending_state = True
        self.invalidate(irect(self.scene.drop))
        self.invalidate(irect(self.scene.top))          # the hint text changes
        self.log(f"dropdown {'open' if value else 'closed'}")
        self.frame("dropdown")

    def move(self, delta: int) -> None:
        self.select((self.scene.selected + delta) % len(ROWS))

    def select(self, i: int) -> None:
        if i == self.scene.selected:
            return
        for j in (self.scene.selected, i):
            self.invalidate(irect(self.scene.row_rect(j).makeOutset(2, 2)))
        self.scene.selected = i
        if self.scene.open:
            self.frame("row")

    def pick(self) -> None:
        self.log(f"picked: {ROWS[self.scene.selected][0]}")
        self.set_open(False)

    # ── input ────────────────────────────────────────────────────────
    def _seat_caps(self, _seat, caps: int) -> None:
        if caps & 1 and self.pointer is None:
            self.pointer = self.seat.get_pointer()
            p = self.pointer.dispatcher
            p["enter"] = self._ptr_enter
            p["leave"] = self._ptr_leave
            p["motion"] = self._ptr_motion
            p["button"] = self._ptr_button
            if self.cursor_shape is not None:
                self.cursor_device = self.cursor_shape.get_pointer(self.pointer)
        if caps & 2 and self.keyboard is None:
            self.keyboard = self.seat.get_keyboard()
            k = self.keyboard.dispatcher
            k["keymap"] = lambda _k, fmt, fd, size: os.close(fd)
            k["enter"] = self._kb_enter
            k["leave"] = self._kb_leave
            k["key"] = self._key

    def _ptr_enter(self, _p, serial, surface, x, y) -> None:
        self.pointer_in = surface is not None and ptr(surface) == ptr(self.surface)
        self.pointer_xy = (x, y)
        if self.cursor_device is not None:
            self.cursor_device.set_shape(serial, 1)   # default arrow
        self.log(f"pointer: enter overlay at {x:.0f},{y:.0f}")

    def _ptr_leave(self, *_):
        self.pointer_in = False
        self.log("pointer: leave overlay")

    def _ptr_motion(self, _p, _t, x, y) -> None:
        self.pointer_xy = (x, y)
        if self.scene.open:
            i = self.scene.row_at(x, y)
            if i is not None:
                self.select(i)

    def _kb_enter(self, *_):
        self.log("keyboard: enter (overlay has focus)")

    def _kb_leave(self, *_):
        self.log("keyboard: leave")
        # Focus went elsewhere (on_demand: the user clicked a window).
        # Nothing else tells a layer surface about a click outside it.
        if self.scene.open:
            self.log("dropdown lost keyboard focus: closing")
            self.set_open(False)

    def _ptr_button(self, _p, serial, _t, button, state) -> None:
        if button != BTN_LEFT or state != 1:
            return
        x, y = self.pointer_xy
        self.log(f"pointer: click on overlay at {x:.0f},{y:.0f}")
        if self.scene.top.contains(x, y):
            self.set_open(not self.scene.open)
        elif self.scene.open and self.scene.drop.contains(x, y):
            i = self.scene.row_at(x, y)
            if i is not None:
                self.select(i)
                self.pick()
        elif self.scene.open:
            # Only reachable with --catch-outside: the whole surface takes
            # input while the dropdown is open, like an X11 pointer grab.
            self.log("click outside the dropdown: closing (click swallowed)")
            self.set_open(False)

    def _key(self, _k, _serial, _t, key, state) -> None:
        if state != 1:
            return
        self.log(f"keyboard: key {key}")
        if key == KEY_ESC:
            self.set_open(False)
        elif key == KEY_UP:
            self.move(-1)
        elif key == KEY_DOWN:
            self.move(1)
        elif key == KEY_ENTER and self.scene.open:
            self.pick()

    # ── fullscreen tracking (wlr-foreign-toplevel) ───────────────────
    def _toplevel(self, _mgr, handle) -> None:
        info = {"title": "", "app_id": "", "state": set(), "handle": handle}
        key = ptr(handle)
        self.toplevels[key] = info
        d = handle.dispatcher
        d["title"] = lambda _h, t: info.__setitem__("title", t)
        d["app_id"] = lambda _h, a: info.__setitem__("app_id", a)
        d["output_enter"] = d["output_leave"] = d["parent"] = lambda *a: None

        def state(_h, raw):
            values = set(struct.unpack(f"{len(raw) // 4}I", raw)) if isinstance(
                raw, (bytes, bytearray)) else set(raw)
            info["state"] = values

        def done(_h):
            names = sorted(wl.wlr_foreign_toplevel_management_unstable_v1
                           .ZwlrForeignToplevelHandleV1.state(s).name for s in info["state"])
            self.log(f"toplevel {info['app_id']!r} {info['title']!r}: {names}")
            self._check_fullscreen()

        def closed(_h):
            self.toplevels.pop(key, None)
            self._check_fullscreen()

        d["state"] = state
        d["done"] = done
        d["closed"] = closed

    def _check_fullscreen(self) -> None:
        fs = any({2, 3} <= t["state"] for t in self.toplevels.values())
        if self.args.hide_on_fullscreen and fs != self.scene.hidden:
            self.scene.hidden = fs
            self.pending_state = True
            self.invalidate(skia.IRect.MakeWH(self.scene.w, self.scene.h))
            self.log("fullscreen window active: canvas cleared" if fs
                     else "no fullscreen window: canvas back")
            self.frame("fullscreen")

    # ── commands ─────────────────────────────────────────────────────
    def command(self, line: str) -> None:
        parts = line.split()
        if not parts:
            return
        cmd, rest = parts[0], parts[1:]
        if cmd == "toggle":
            self.set_open(not self.scene.open)
        elif cmd == "open":
            self.set_open(True)
        elif cmd == "close":
            self.set_open(False)
        elif cmd in ("up", "down"):
            self.move(-1 if cmd == "up" else 1)
        elif cmd == "kb" and rest:
            self.args.kb = rest[0]
            self.log(f"keyboard mode on open: {self.args.kb}")
        elif cmd == "catch" and rest:
            self.scene.catch_outside = rest[0] == "on"
            self.pending_state = True
            self.log(f"catch outside clicks: {rest[0]}")
        elif cmd == "blur":
            self.scene.blur = (rest or ["on"])[0] == "on"
            self.pending_state = True
            self.invalidate(irect(self.scene.drop))
            self.frame("blur")
            self.log(f"blur {'on' if self.scene.blur else 'off'} (compositor caps {self.blur_caps})")
        elif cmd == "full":
            self.invalidate(skia.IRect.MakeWH(self.scene.w, self.scene.h))
            self.frame("full")
        elif cmd == "bench":
            self.bench(int(rest[0]) if rest else 100)
        elif cmd == "stats":
            self.stats()
        elif cmd == "quit":
            self.quit("quit")
        else:
            self.log(f"unknown command {cmd!r}")

    def stats(self) -> None:
        by: dict[str, list[float]] = {}
        for kind, ms in self.frames:
            by.setdefault(kind, []).append(ms)
        for kind, v in sorted(by.items()):
            v = sorted(v)
            self.log(f"stats {kind}: n={len(v)} median {v[len(v) // 2]:.3f} ms, "
                     f"max {v[-1]:.3f} ms")

    def _time(self, scene: Scene, canvas, clip, n: int, sync) -> float:
        best = []
        for _ in range(n):
            t0 = time.perf_counter()
            scene.paint(canvas, clip)
            sync()
            best.append((time.perf_counter() - t0) * 1000)
        best.sort()
        return best[len(best) // 2]

    def bench(self, n: int) -> None:
        """Median paint time of a full and a clock-only redraw, with the
        dropdown open and closed, at this size and at 2560x1440. GPU
        times include waiting for the GPU (flush + glFinish)."""
        r = self.renderer
        sync = r.sync
        here = (self.scene.w, self.scene.h, r.canvas_for_bench())
        off = r.offscreen(2560, 1440)
        for w, h, canvas in (here, (2560, 1440, off.getCanvas())):
            for open_ in (False, True):
                s = Scene(self.scene.mode_label)
                s.layout(w, h)
                s.open = open_
                s.blur = self.scene.blur
                full = self._time(s, canvas, None, n, sync)
                clock = self._time(s, canvas, irect(s.clock), n, sync)
                self.log(f"bench {r.name.split(' ')[0]} {w}x{h} dropdown "
                         f"{'open ' if open_ else 'closed'}: full {full:.3f} ms, "
                         f"clock-only {clock:.3f} ms  (median of {n})")
        # The live canvas now shows bench frames: repaint it properly.
        if self.args.gpu:
            self.renderer.history = []
        else:
            for b in self.renderer.buffers:
                b.stale = skia.Region(skia.IRect.MakeWH(self.scene.w, self.scene.h))
        self.invalidate(skia.IRect.MakeWH(self.scene.w, self.scene.h))
        self.frame("full")

    def quit(self, why: str) -> None:
        self.log(f"exiting: {why}")
        self.running = False

    # ── loop ─────────────────────────────────────────────────────────
    def run(self) -> None:
        sel = selectors.DefaultSelector()
        fd = self.display.get_fd()
        sel.register(fd, selectors.EVENT_READ, "wayland")
        if not os.isatty(0):
            os.set_blocking(0, False)
            sel.register(0, selectors.EVENT_READ, "stdin")
        rsig, wsig = os.pipe()
        os.set_blocking(rsig, False)
        os.set_blocking(wsig, False)
        signal.set_wakeup_fd(wsig)
        signal.signal(signal.SIGUSR1, lambda *_: None)
        signal.signal(signal.SIGTERM, lambda *_: None)
        signal.signal(signal.SIGINT, lambda *_: None)
        sel.register(rsig, selectors.EVENT_READ, "signal")
        pending = b""
        self.display.roundtrip()
        while self.running:
            self.display.dispatch(block=False)
            self.frame("full" if not self.frames else "update")
            self.display.flush()
            now = time.time()
            timeout = max(0.0, 1.0 - (now % 1.0)) + 0.002
            for key, _ in sel.select(timeout):
                if key.data == "wayland":
                    self.display.read()
                    if self.display.dispatch(block=False) < 0:
                        self.quit("wayland connection lost")
                elif key.data == "stdin":
                    try:
                        chunk = os.read(0, 4096)
                    except BlockingIOError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(0)
                        continue
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        self.command(line.decode(errors="replace"))
                elif key.data == "signal":
                    for s in os.read(rsig, 64):
                        if s == signal.SIGUSR1:
                            self.set_open(not self.scene.open)
                        elif s in (signal.SIGTERM, signal.SIGINT):
                            self.quit(signal.Signals(s).name)
            now = datetime.datetime.now()
            if self.configured and now.replace(microsecond=0) != \
                    self.scene.now.replace(microsecond=0):
                day_changed = now.date() != self.scene.now.date()
                self.scene.now = now
                if not self.scene.hidden:
                    self.invalidate(irect(self.scene.top) if day_changed
                                    else irect(self.scene.clock))
                    self.frame("clock")
        self.stats()
        if isinstance(self.renderer, GpuRenderer):
            self.renderer.gl.destroy()
        self.display.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gpu", action="store_true", help="EGL + Skia GPU instead of wl_shm")
    ap.add_argument("--blur", action="store_true", help="blur behind the dropdown's rect")
    ap.add_argument("--kb", choices=("exclusive", "on_demand"), default="exclusive",
                    help="keyboard interactivity while the dropdown is open")
    ap.add_argument("--layer", choices=("overlay", "top"), default="overlay",
                    help="layer of the one canvas (the design says overlay)")
    ap.add_argument("--hide-on-fullscreen", action="store_true",
                    help="clear the canvas while a fullscreen window is active")
    ap.add_argument("--catch-outside", action="store_true",
                    help="while the dropdown is open, take input on the whole surface "
                         "so a click outside it closes it")
    ap.add_argument("--verbose", action="store_true", help="log every clock frame too")
    App(ap.parse_args()).run()


if __name__ == "__main__":
    main()
