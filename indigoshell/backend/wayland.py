"""The shell's Wayland connection: the one canvas on wlr-layer-shell.

What canvas.py describes, on the wire (pywayland, with bindings made at
run time by wlproto.py):

| surface | layer | anchors | exclusive zone | buffer | input |
|---|---|---|---|---|---|
| `indigoshell` | overlay | all four, size from configure | -1 | the canvas | what is drawn |
| `indigoshell-reserve-<edge>` | bottom | the edge + both perpendicular | the bars' reach | 1x1 transparent wl_shm, stretched by wp_viewporter | empty |

The canvas is drawn by the GPU (EGL on the Wayland platform,
egl_wayland.py) with partial redraws: the regions' damage, plus what the
back buffer's age says it missed, is composed and swapped with that
damage. Without EGL it is drawn on the CPU into wl_shm buffers, three at
most, each with its own record of what it missed. Frames are made when
something changed and never otherwise: no frame callbacks, no timers.

The keyboard: wl_keyboard's keymap and modifiers go to xkbcommon
(wlkeys.py), its keys, as KeyEvents, to whoever took the canvas's
keyboard (canvas.py KeyboardRouter), repeated by our own timer at the
compositor's repeat_info. The canvas asks for the keyboard with
layer-shell's keyboard_interactivity: `exclusive` while anyone holds it,
`none` otherwise, so the compositor gives the focus back to the window
that had it. (On niri, `on_demand` would focus the canvas only when it
is clicked, and switching `exclusive` to `on_demand` once the focus came
drops it at once: prototypes/wayland/README.md.) The change rides on the
next commit, the frame that shows what took it.

While a window is fullscreen (wlr-foreign-toplevel: `fullscreen` and
`activated`; niri's IPC has no such flag) the bars are not drawn, and
the canvas is taken away entirely, so the compositor can scan the window
out directly, unless the user has called something up (canvas.py
`Scene.shows`): then the canvas is there with only that on it, and goes
again when the last of it closes. A fullscreen window stays so while the
canvas has the keyboard it took for what was called up: the compositor
deactivates the window when the canvas takes its keyboard, and the bars
must not come back for that. The EGL context outlives the surface,
parked on a 1x1 surface of its own, and so do the regions' pixels in it.
An output going away closes the canvas too; it is made again on
whatever output there is.

The event pump is loop-agnostic like backend/x11.py's: `fd` is wired
into the daemon's loop, `on_readable` reads and dispatches, `flush`
sends. EGL reads the same socket inside a swap, so events for the
default queue can land there with the fd left quiet; every present
queues a `drain` for that.
"""

import errno
import logging
import mmap
import os
import struct
import time

import skia

from ..canvas import AxisSteps, DamageHistory, GpuScratch, Scene, button_of, rects
from ..core.naming import APP, env
from .wlkeys import Keys, Repeat
from .x11 import DisplayError

log = logging.getLogger(__name__)

NAMESPACE = APP                       # the user's niri layer-rule matches it
EDGES = ("top", "bottom", "left", "right")
CONFIGURE_TIMEOUT_S = 5.0
# After the canvas lets the keyboard go, how long a fullscreen window it
# took it from has to be activated again before it counts as no longer
# fullscreen (Toplevels).
KEYBOARD_BACK_S = 0.5
REOPEN_S = (0.25, 0.5, 1.0, 2.0)      # after the canvas was closed under us

# zwlr_foreign_toplevel_handle_v1.state
STATE_ACTIVATED, STATE_FULLSCREEN = 2, 3
# wp_cursor_shape_device_v1.shape.default
CURSOR_DEFAULT = 1
# wl_seat.capability, wl_keyboard.keymap_format.xkb_v1
SEAT_POINTER, SEAT_KEYBOARD = 1, 2
KEYMAP_XKB_V1 = 1


class NoLayerShell(DisplayError):
    """The compositor offers no zwlr_layer_shell_v1."""


def backend_order(environ) -> list[str]:
    """Which display servers to try, in order. $INDIGOSHELL_BACKEND
    ("x11" or "wayland") forces one; otherwise Wayland first when
    $WAYLAND_DISPLAY is set (it is used only if its compositor offers
    layer-shell), else X11."""
    forced = (environ.get(env("BACKEND")) or "").strip().lower()
    if forced in ("x11", "wayland"):
        return [forced]
    if environ.get("WAYLAND_DISPLAY"):
        return ["wayland", "x11"]
    return ["x11"]


def parse_states(raw) -> set[int]:
    """A wl_array of uint32 as pywayland hands it over: bytes, or a list."""
    if raw is None:
        return set()
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw)
        return set(struct.unpack(f"={len(raw) // 4}I", raw[:len(raw) // 4 * 4]))
    return {int(v) for v in raw}


def is_fullscreen(states) -> bool:
    """A toplevel that covers the output now: fullscreen and focused."""
    return {STATE_FULLSCREEN, STATE_ACTIVATED} <= set(states)


def _ptr(proxy) -> int:
    from pywayland import ffi
    return int(ffi.cast("uintptr_t", proxy._ptr))


def _same(a, b) -> bool:
    try:
        return a is not None and b is not None and _ptr(a) == _ptr(b)
    except Exception:
        return False


# ── wl_shm ──────────────────────────────────────────────────────────────
class ShmBuffer:
    def __init__(self, wl, shm, w: int, h: int, *, surface: bool = True) -> None:
        self.w, self.h = w, h
        stride, size = w * 4, w * 4 * h
        fd = os.memfd_create(f"{APP}-canvas", os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            self.map = mmap.mmap(fd, size)
            pool = shm.create_pool(fd, size)
            self.buffer = pool.create_buffer(0, w, h, stride,
                                             wl.wayland.WlShm.format.argb8888)
            pool.destroy()
        finally:
            os.close(fd)
        self.busy = False
        self.buffer.dispatcher["release"] = self._release
        self.on_release = None
        self.surface = None
        if surface:
            info = skia.ImageInfo.Make(w, h, skia.kBGRA_8888_ColorType,
                                       skia.kPremul_AlphaType)
            self.surface = skia.Surface.MakeRasterDirect(info, self.map, stride)
        # What in this buffer is older than the newest frame.
        self.stale = skia.Region(skia.IRect.MakeWH(w, h))

    def _release(self, *_):
        self.busy = False
        if self.on_release is not None:
            self.on_release()

    def destroy(self) -> None:
        self.surface = None
        try:
            self.buffer.destroy()
        except Exception:
            pass
        self.map.close()


class ShmTarget:
    """The canvas drawn on the CPU into wl_shm buffers."""

    name = "raster"
    MAX_BUFFERS = 3

    def __init__(self, canvas: "Canvas") -> None:
        self.canvas = canvas
        self.buffers: list[ShmBuffer] = []
        self.size = (0, 0)
        self.surface = None

    def scratch(self):
        return None                     # regions paint into raster surfaces

    def attach(self, surface, w: int, h: int) -> None:
        self.detach()
        self.surface = surface
        self.size = (w, h)

    def resize(self, w: int, h: int) -> None:
        self.attach(self.surface, w, h)

    def detach(self) -> None:
        for b in self.buffers:
            b.destroy()
        self.buffers = []
        self.surface = None

    def invalidate(self) -> None:
        for b in self.buffers:
            b.stale = skia.Region(skia.IRect.MakeWH(*self.size))

    def _free(self) -> ShmBuffer | None:
        for b in self.buffers:
            if not b.busy:
                return b
        if len(self.buffers) >= self.MAX_BUFFERS:
            return None
        c = self.canvas
        b = ShmBuffer(c.wl, c.shm, *self.size)
        b.on_release = c.present_soon
        self.buffers.append(b)
        log.debug("canvas: wl_shm buffer %d of %dx%d", len(self.buffers), *self.size)
        return b

    def present(self, damage: skia.Region) -> int | None:
        for b in self.buffers:
            b.stale.op(damage, skia.Region.kUnion_Op)
        buf = self._free()
        if buf is None:
            return None                 # every buffer still with the compositor
        buf.stale.op(damage, skia.Region.kUnion_Op)
        painted = self.canvas.scene.compose(buf.surface.getCanvas(), buf.stale)
        buf.stale.setEmpty()
        s = self.surface
        s.attach(buf.buffer, 0, 0)
        for r in rects(damage):
            s.damage_buffer(r.left(), r.top(), r.width(), r.height())
        self.canvas.apply_pending_state()
        s.commit()
        buf.busy = True
        return painted


class GpuTarget:
    """The canvas drawn by the GPU: EGL's window surface on it."""

    def __init__(self, canvas: "Canvas", gl) -> None:
        self.canvas = canvas
        self.gl = gl
        self.name = "GPU"
        self.history = DamageHistory()
        self._scratch = GpuScratch(gl)

    def scratch(self):
        return self._scratch

    def attach(self, surface, w: int, h: int) -> None:
        self.gl.attach(_ptr(surface), w, h)
        self.history.clear()

    def resize(self, w: int, h: int) -> None:
        self.gl.resize(w, h)
        self.history.clear()

    def detach(self) -> None:
        self.gl.detach()
        self.history.clear()

    def invalidate(self) -> None:
        self.history.clear()

    def present(self, damage: skia.Region) -> int:
        gl = self.gl
        age = gl.buffer_age()
        paint = self.history.repaint(damage, age, skia.IRect.MakeWH(gl.width, gl.height))
        painted = self.canvas.scene.compose(gl.skia_surface.getCanvas(), paint)
        self.history.push(damage)
        self.canvas.apply_pending_state()
        gl.swap(rects(damage))          # attaches, damages and commits
        return painted


# ── the strips ──────────────────────────────────────────────────────────
class Strip:
    """An invisible reservation: an exclusive zone on one edge, no
    pixels, no input."""

    def __init__(self, canvas: "Canvas", edge: str, px: int) -> None:
        self.canvas = canvas
        self.edge = edge
        self.px = 0
        c = canvas
        LS = c.wl.wlr_layer_shell_unstable_v1
        A = LS.ZwlrLayerSurfaceV1.anchor
        self.surface = c.compositor.create_surface()
        empty = c.compositor.create_region()
        self.surface.set_input_region(empty)
        empty.destroy()
        self.layer = c.layer_shell.get_layer_surface(
            self.surface, None, LS.ZwlrLayerShellV1.layer.bottom,
            f"{NAMESPACE}-reserve-{edge}")
        self.layer.set_anchor({
            "top": A.top | A.left | A.right,
            "bottom": A.bottom | A.left | A.right,
            "left": A.left | A.top | A.bottom,
            "right": A.right | A.top | A.bottom}[edge])
        self.layer.set_keyboard_interactivity(
            LS.ZwlrLayerSurfaceV1.keyboard_interactivity.none)
        self.layer.dispatcher["configure"] = self._configure
        self.layer.dispatcher["closed"] = self._closed
        self.viewport = None
        self.shm: ShmBuffer | None = None
        self.closed = False
        self.set(px, commit=False)
        self.surface.commit()

    def set(self, px: int, *, commit: bool = True) -> None:
        if px == self.px:
            return
        self.px = px
        if self.edge in ("top", "bottom"):
            self.layer.set_size(0, px)
        else:
            self.layer.set_size(px, 0)
        self.layer.set_exclusive_zone(px)
        if commit:
            self.surface.commit()
        log.debug("canvas: reserving %d px at the %s", px, self.edge)

    def _configure(self, _layer, serial: int, w: int, h: int) -> None:
        c = self.canvas
        self.layer.ack_configure(serial)
        w, h = max(w, 1), max(h, 1)
        if self.shm is None:
            # One transparent pixel, stretched to the strip by a
            # viewport: niri has wp_viewporter but no single-pixel-buffer
            # (a fresh memfd is zeroes). With no viewporter, a buffer the
            # strip's size, as transparent.
            if c.viewporter is not None:
                self.viewport = c.viewporter.get_viewport(self.surface)
                self.shm = ShmBuffer(c.wl, c.shm, 1, 1, surface=False)
            else:
                self.shm = ShmBuffer(c.wl, c.shm, w, h, surface=False)
            self.surface.attach(self.shm.buffer, 0, 0)
        if self.viewport is not None:
            self.viewport.set_destination(w, h)
        self.surface.commit()
        c.flush()

    def _closed(self, *_):
        log.info("canvas: the %s reservation was closed", self.edge)
        self.closed = True
        self.canvas.closed_under_us()

    def destroy(self) -> None:
        for obj in (self.viewport, self.layer, self.surface):
            if obj is not None:
                try:
                    obj.destroy()
                except Exception:
                    pass
        if self.shm is not None:
            self.shm.destroy()
        self.viewport = self.shm = None


# ── fullscreen windows ──────────────────────────────────────────────────
class Toplevels:
    """Every toplevel's state (wlr-foreign-toplevel), and whether one
    is fullscreen and focused.

    `held()` says whether the canvas has the keyboard: the compositor
    then deactivates the window that had it, and a fullscreen window so
    deactivated, with no other window activated instead, is still the
    one on screen under what was called up."""

    def __init__(self, wl, manager, on_change, held=None) -> None:
        self.wl = wl
        self.manager = manager
        self.on_change = on_change
        self.held = held or (lambda: False)
        self.handles: dict[int, dict] = {}
        self.fullscreen = False
        self._fs_key: int | None = None     # the fullscreen window's handle
        manager.dispatcher["toplevel"] = self._toplevel
        manager.dispatcher["finished"] = lambda *_: log.debug("foreign toplevels: finished")

    def _toplevel(self, _mgr, handle) -> None:
        key = _ptr(handle)
        info = {"app_id": "", "title": "", "state": set(), "pending": set(),
                "handle": handle}
        self.handles[key] = info
        d = handle.dispatcher
        d["title"] = lambda _h, t: info.__setitem__("title", t)
        d["app_id"] = lambda _h, a: info.__setitem__("app_id", a)
        d["output_enter"] = d["output_leave"] = d["parent"] = lambda *a: None

        def state(_h, raw):
            info["pending"] = parse_states(raw)

        def done(_h):
            info["state"] = info["pending"]
            self._check()

        def closed(_h):
            self.handles.pop(key, None)
            try:
                handle.destroy()
            except Exception:
                pass
            self._check()

        d["state"] = state
        d["done"] = done
        d["closed"] = closed

    def _check(self) -> None:
        key = next((k for k, t in self.handles.items() if is_fullscreen(t["state"])), None)
        fs = key is not None
        if not fs and self.fullscreen and self.held():
            # Deactivated for the canvas's keyboard: still fullscreen,
            # nothing else in front.
            was = self.handles.get(self._fs_key)
            if (was is not None and STATE_FULLSCREEN in was["state"]
                    and not any(STATE_ACTIVATED in t["state"]
                                for t in self.handles.values())):
                fs, key = True, self._fs_key
        self._fs_key = key
        if fs != self.fullscreen:
            self.fullscreen = fs
            who = self.handles.get(key) if key is not None else None
            log.debug("foreign toplevels: fullscreen %s%s", fs,
                      f" ({who['app_id']!r})" if who else "")
            self.on_change(fs)

    def recheck(self) -> None:
        """The canvas let the keyboard go: as the states say now."""
        self._check()


# ── the canvas ──────────────────────────────────────────────────────────
class Canvas:
    """The Wayland connection, the canvas surface and its reservations.

    `connect()` first (it raises NoLayerShell or DisplayError), then
    `start()`, which blocks until the compositor has sized the canvas.
    The regions live in `scene`.
    """

    def __init__(self, loop=None) -> None:
        self.loop = loop
        self.scene = Scene(loop)
        self.scene.on_damage = self.present_soon
        self.scene.on_layout = self._layout_changed
        self.scene.on_flush = self.flush
        self.display = None
        self.wl = None
        self.globals: dict[str, tuple[int, int]] = {}
        self.surface = None
        self.layer = None
        self.configured = False
        self.has_buffer = False
        self.pending_state = True
        self.target = None
        self.gl = None
        self.strips: dict[str, Strip] = {}
        self.toplevels: Toplevels | None = None
        self.pointer = self.cursor_device = None
        self.steps = AxisSteps()
        self.keyboard = None            # wl_keyboard
        try:
            self.keys: Keys | None = Keys()
        except ImportError:
            log.warning("canvas: no python-xkbcommon; nothing on the canvas takes the "
                        "keyboard")
            self.keys = None
        self.repeat = Repeat(loop, self._repeat_key)
        self.scene.keyboard.available = self.keys is not None
        self.scene.on_keyboard = self._keyboard_wanted
        self._kb_sent = None            # the overlay's keyboard_interactivity, as last sent
        self._kb_let_go = -1e9          # when it went from exclusive to none
        self.on_resize = None           # the daemon's screen-change hook
        self.fullscreen = False
        self._present_queued = False
        self._drain_queued = False
        self._writing = False
        self._reopen = None
        self._reopen_tries = 0
        self._closed = False
        self._idle_surface = None

    # ── connection ──────────────────────────────────────────────────────
    def connect(self) -> None:
        from pywayland.client import Display
        from . import wlproto

        name = os.environ.get("WAYLAND_DISPLAY")
        try:
            self.wl = wlproto.load()
        except Exception as e:
            raise DisplayError(f"no Wayland protocol bindings: {e}") from e
        try:
            self.display = Display(name)
            self.display.connect()
        except Exception as e:
            self.display = None
            raise DisplayError(f"cannot connect to Wayland display {name!r}: {e}") from e
        self.registry = self.display.get_registry()
        self.registry.dispatcher["global"] = self._global
        self.registry.dispatcher["global_remove"] = lambda *a: None
        self.display.roundtrip()
        if "zwlr_layer_shell_v1" not in self.globals:
            self.disconnect()
            raise NoLayerShell(f"the compositor on {name!r} offers no zwlr_layer_shell_v1")
        wl = self.wl
        self.compositor = self._bind("wl_compositor", wl.wayland.WlCompositor, 4)
        self.shm = self._bind("wl_shm", wl.wayland.WlShm, 1)
        LS = wl.wlr_layer_shell_unstable_v1
        self.layer_shell = self._bind("zwlr_layer_shell_v1", LS.ZwlrLayerShellV1, 4)
        self.viewporter = self._bind("wp_viewporter", wl.viewporter.WpViewporter, 1) \
            if hasattr(wl, "viewporter") else None
        self.cursor_shape = self._bind(
            "wp_cursor_shape_manager_v1", wl.cursor_shape_v1.WpCursorShapeManagerV1, 1) \
            if hasattr(wl, "cursor_shape_v1") else None
        self.seat = self._bind("wl_seat", wl.wayland.WlSeat, 7)
        ft = getattr(wl, "wlr_foreign_toplevel_management_unstable_v1", None)
        manager = self._bind("zwlr_foreign_toplevel_manager_v1",
                             ft.ZwlrForeignToplevelManagerV1, 3) if ft else None
        if self.compositor is None or self.shm is None:
            self.disconnect()
            raise DisplayError("the compositor offers no wl_compositor or wl_shm")
        if manager is not None:
            self.toplevels = Toplevels(wl, manager, self._fullscreen_changed,
                                       held=self._keyboard_held)
        else:
            log.warning("canvas: no wlr-foreign-toplevel; it stays over fullscreen windows")
        if self.seat is not None:
            self.seat.dispatcher["capabilities"] = self._seat_caps
            self.seat.dispatcher["name"] = lambda *a: None
        log.info("Wayland display %s: layer-shell v%d%s", name,
                 self.globals["zwlr_layer_shell_v1"][1],
                 "" if self.viewporter else ", no viewporter")

    def _global(self, _reg, name: int, interface: str, version: int) -> None:
        self.globals[interface] = (name, version)

    def _bind(self, iface: str, cls, want: int):
        g = self.globals.get(iface)
        if g is None:
            return None
        return self.registry.bind(g[0], cls, min(want, g[1]))

    @property
    def fd(self) -> int:
        return self.display.get_fd()

    def attach_loop(self, loop) -> None:
        self.loop = loop
        self.scene.loop = loop
        self.repeat.loop = loop

    def on_readable(self) -> None:
        if self._closed:
            return
        try:
            self.display.read()
            self.display.dispatch(block=False)
        except Exception as e:
            raise DisplayError(f"Wayland connection lost: {e}") from e
        self.flush()

    def drain(self) -> None:
        """Dispatch what EGL's reads queued for us (see the module
        docstring)."""
        self._drain_queued = False
        if self._closed:
            return
        try:
            self.display.dispatch(block=False)
        except Exception as e:
            log.error("Wayland connection lost: %s", e)
            return
        self.flush()

    def flush(self) -> None:
        if self._closed or self.display is None:
            return
        if self._send() == "full" and self.loop is not None and not self._writing:
            # The socket is full: send the rest when it drains.
            self._writing = True
            self.loop.add_writer(self.fd, self._writable)

    def _send(self) -> str:
        """"sent", "full" (try again when writable) or "broken" (the
        connection is gone; the next read says so)."""
        from pywayland import ffi
        if self.display.flush() >= 0:
            return "sent"
        return "full" if ffi.errno == errno.EAGAIN else "broken"

    def _writable(self) -> None:
        if self._closed or self.display is None or self._send() != "full":
            self._writing = False
            try:
                self.loop.remove_writer(self.fd)
            except Exception:
                pass

    def disconnect(self) -> None:
        if self.display is not None:
            try:
                self.display.disconnect()
            except Exception:
                pass
            self.display = None

    # ── the canvas surface ──────────────────────────────────────────────
    def start(self) -> None:
        """Make the canvas and wait until the compositor has sized it."""
        self.target = self._make_target()
        self.scene.gpu = self.target.scratch()
        self._make_overlay()
        deadline = time.monotonic() + CONFIGURE_TIMEOUT_S
        while not self.configured:
            if self.display.roundtrip() < 0:
                raise DisplayError("Wayland connection lost while starting")
            if time.monotonic() > deadline:
                raise DisplayError("the compositor never sized the canvas")
            if not self.configured:
                time.sleep(0.01)
        log.info("canvas: %dx%d, %s", self.scene.width, self.scene.height, self.target.name)

    def _make_target(self):
        if os.environ.get(env("GPU"), "1") in ("0", "no", "off"):
            log.info("canvas: GPU disabled by %s; drawing on the CPU", env("GPU"))
            return ShmTarget(self)
        try:
            from .egl_wayland import WaylandGL
            gl = WaylandGL(_ptr(self.display))
            self._idle_surface = self.compositor.create_surface()
            gl.start(_ptr(self._idle_surface))
        except Exception as e:
            log.warning("canvas: GPU unavailable (%s); drawing on the CPU", e)
            return ShmTarget(self)
        self.gl = gl
        target = GpuTarget(self, gl)
        target.name = f"GPU ({gl.renderer})"
        return target

    def _make_overlay(self) -> None:
        LS = self.wl.wlr_layer_shell_unstable_v1
        A = LS.ZwlrLayerSurfaceV1.anchor
        self.surface = self.compositor.create_surface()
        self.layer = self.layer_shell.get_layer_surface(
            self.surface, None, LS.ZwlrLayerShellV1.layer.overlay, NAMESPACE)
        self.layer.set_anchor(A.top | A.bottom | A.left | A.right)
        self.layer.set_size(0, 0)
        self.layer.set_exclusive_zone(-1)
        self.layer.set_keyboard_interactivity(
            LS.ZwlrLayerSurfaceV1.keyboard_interactivity.none)
        self._kb_sent = "none"          # the first frame's commit asks, if anyone holds it
        self.layer.dispatcher["configure"] = self._configure
        self.layer.dispatcher["closed"] = lambda *_: self.closed_under_us()
        self.configured = False
        self.has_buffer = False
        self.pending_state = True
        self.surface.commit()
        self.flush()

    def _drop_overlay(self) -> None:
        if self.surface is None:
            return
        try:
            if self.configured:
                self.target.detach()
        except Exception:
            log.exception("canvas: letting go of the surface failed")
        for obj in (self.layer, self.surface):
            try:
                obj.destroy()
            except Exception:
                pass
        self.layer = self.surface = None
        self.configured = self.has_buffer = False
        self._kb_sent = None
        self._keyboard_left()
        self.flush()

    def _configure(self, _layer, serial: int, w: int, h: int) -> None:
        self.layer.ack_configure(serial)
        if w <= 0 or h <= 0:
            log.warning("canvas: configured to %dx%d; keeping %dx%d", w, h,
                        self.scene.width, self.scene.height)
            w, h = self.scene.width or 1, self.scene.height or 1
        first = not self.configured
        sized = self.scene.width > 0
        if first:
            self.target.attach(self.surface, w, h)
        elif (w, h) != (self.scene.width, self.scene.height):
            self.target.resize(w, h)
        self.configured = True
        self._reopen_tries = 0
        log.debug("canvas: configure %dx%d%s", w, h, " (new surface)" if first else "")
        resized = self.scene.resize(w, h)
        if first:
            self.target.invalidate()
            self.pending_state = True
            if self.scene.hidden:
                self.scene.resume()
            self.scene.damage_all()
        if resized and sized and self.on_resize is not None:
            self.on_resize(None)
        self.present_soon()

    def closed_under_us(self) -> None:
        """The compositor closed the canvas or a strip (its output went
        away): make them again, on whatever output there is now."""
        if self._closed or self._reopen is not None:
            return
        log.info("canvas: closed by the compositor; making it again")
        self.scene.pointer.reset()
        self._drop_overlay()
        for strip in self.strips.values():
            strip.destroy()
        self.strips = {}
        delay = REOPEN_S[min(self._reopen_tries, len(REOPEN_S) - 1)]
        self._reopen_tries += 1
        if self.loop is not None:
            self._reopen = self.loop.call_later(delay, self._reopen_now)

    def _reopen_now(self) -> None:
        self._reopen = None
        if self._closed:
            return
        self._sync_strips()
        if self.scene.wants_surface():
            self._make_overlay()

    # ── frames ──────────────────────────────────────────────────────────
    def present_soon(self) -> None:
        if self._present_queued or self.loop is None or self._closed:
            return
        self._present_queued = True
        self.loop.call_soon(self._present)

    def _present(self) -> None:
        self._present_queued = False
        if self._closed or not self.configured or self.surface is None:
            return
        damage = self.scene.take_damage()
        if damage.isEmpty():
            if self.pending_state and self.has_buffer:
                self.apply_pending_state()
                self.surface.commit()
                self.flush()
            return
        t0 = time.perf_counter()
        try:
            painted = self.target.present(damage)
        except Exception:
            log.exception("canvas: a present failed")
            return
        if painted is None:
            self.scene.damage.op(damage, skia.Region.kUnion_Op)    # buffers busy
            return
        ms = (time.perf_counter() - t0) * 1000
        self.has_buffer = True
        area = sum(r.width() * r.height() for r in rects(damage))
        self.scene.stats.present(ms, painted, area)
        if log.isEnabledFor(logging.DEBUG):
            b = damage.getBounds()
            log.debug("canvas frame: %.2f ms, damage %d px in %d rect(s) "
                      "(bounds %dx%d+%d+%d), painted %d px", ms, area,
                      len(rects(damage)), b.width(), b.height(), b.left(), b.top(),
                      painted)
        self.flush()
        if self.loop is not None and not self._drain_queued:
            self._drain_queued = True
            self.loop.call_soon(self.drain)

    def apply_pending_state(self) -> None:
        """The canvas's double-buffered state that rides on the next
        commit: its input region (what is drawn, or all of it while
        outside clicks are caught) and whether it asks for the
        keyboard."""
        if not self.pending_state or self.surface is None:
            return
        self.pending_state = False
        region = self.compositor.create_region()
        for r in self.scene.input_rects():
            region.add(r.left(), r.top(), r.width(), r.height())
        self.surface.set_input_region(region)
        region.destroy()
        want = "exclusive" if self.scene.keyboard.wanted and not self.scene.hidden else "none"
        if want != self._kb_sent and self.layer is not None:
            K = self.wl.wlr_layer_shell_unstable_v1.ZwlrLayerSurfaceV1.keyboard_interactivity
            self.layer.set_keyboard_interactivity(getattr(K, want))
            was, self._kb_sent = self._kb_sent, want
            log.debug("canvas: keyboard_interactivity %s", want)
            if was == "exclusive" and self.toplevels is not None and self.loop is not None:
                # The compositor activates the window again in a moment;
                # until it has had that moment the window stays fullscreen.
                self._kb_let_go = time.monotonic()
                self.loop.call_later(KEYBOARD_BACK_S, self.toplevels.recheck)

    def _keyboard_held(self) -> bool:
        """Whether the canvas has the keyboard, or let it go only just
        (KEYBOARD_BACK_S): a fullscreen window deactivated meanwhile is
        still the one on screen."""
        return (self._kb_sent == "exclusive"
                or time.monotonic() - self._kb_let_go < KEYBOARD_BACK_S)

    def _keyboard_wanted(self) -> None:
        """Someone took the keyboard, or the last one gave it back: the
        canvas asks for it, or lets it go, with the next commit."""
        self.pending_state = True
        if not self.scene.keyboard.wanted:
            self.repeat.stop()
        self.present_soon()

    def _layout_changed(self) -> None:
        self.pending_state = True
        self._sync_strips()
        self._sync_surface()
        self.present_soon()

    def _sync_surface(self) -> None:
        """The canvas surface made or taken away as the scene wants it:
        over a fullscreen window, only while something is called up."""
        if self._closed or self.display is None or self._reopen is not None:
            return
        want = self.scene.wants_surface()
        if want and self.surface is None:
            log.info("canvas: called up over the fullscreen window; the canvas is back "
                     "with only that on it")
            self._make_overlay()        # what shows resumes once configured
        elif not want and self.surface is not None:
            log.info("canvas: nothing called up over the fullscreen window; the canvas "
                     "goes away")
            self.scene.suspend()
            self._drop_overlay()

    def _sync_strips(self) -> None:
        if self._closed or self.display is None:
            return
        want = self.scene.reservations()
        for edge in list(self.strips):
            if edge not in want or self.strips[edge].closed:
                self.strips.pop(edge).destroy()
        for edge, px in want.items():
            if px <= 0:
                continue
            strip = self.strips.get(edge)
            if strip is None:
                self.strips[edge] = Strip(self, edge, px)
            else:
                strip.set(px)
        self.flush()

    # ── a fullscreen window ─────────────────────────────────────────────
    def _fullscreen_changed(self, fullscreen: bool) -> None:
        if fullscreen == self.fullscreen:
            return
        self.fullscreen = fullscreen
        if fullscreen:
            log.info("canvas: a fullscreen window is focused; the bars go%s",
                     "" if self.scene.wants_surface() else ", and the canvas")
        else:
            log.info("canvas: no fullscreen window; the canvas is back")
        self.scene.set_fullscreen(fullscreen)   # its layout change syncs the surface
        self._sync_surface()

    # ── the pointer ─────────────────────────────────────────────────────
    def _seat_caps(self, _seat, caps: int) -> None:
        self._pointer_caps(bool(caps & SEAT_POINTER))
        self._keyboard_caps(bool(caps & SEAT_KEYBOARD))

    def _pointer_caps(self, has: bool) -> None:
        if has and self.pointer is None:
            self.pointer = self.seat.get_pointer()
            p = self.pointer.dispatcher
            p["enter"] = self._enter
            p["leave"] = self._leave
            p["motion"] = self._motion
            p["button"] = self._button
            p["axis"] = self._axis
            p["axis_discrete"] = self._axis_discrete
            p["axis_stop"] = lambda _p, _t, axis: self.steps.stop(axis)
            p["frame"] = lambda _p: self.steps.frame()
            p["axis_source"] = lambda *a: None
            if self.cursor_shape is not None:
                self.cursor_device = self.cursor_shape.get_pointer(self.pointer)
        elif not has and self.pointer is not None:
            self.scene.pointer.reset()
            try:
                self.pointer.release()
            except Exception:
                pass
            self.pointer = self.cursor_device = None

    def _enter(self, _p, serial, surface, x, y) -> None:
        if not _same(surface, self.surface):
            return
        if self.cursor_device is not None:
            self.cursor_device.set_shape(serial, CURSOR_DEFAULT)
        self.scene.pointer.enter(x, y)

    def _leave(self, _p, _serial, _surface) -> None:
        self.scene.pointer.leave()

    def _motion(self, _p, _time, x, y) -> None:
        self.scene.pointer.motion(x, y)

    def _button(self, _p, _serial, _time, code, state) -> None:
        button = button_of(code)
        if button is not None:
            self.scene.pointer.button(button, state == 1)

    def _axis(self, _p, _time, axis, value) -> None:
        self.scene.pointer.scroll(self.steps.value(axis, value))

    def _axis_discrete(self, _p, axis, discrete) -> None:
        self.scene.pointer.scroll(self.steps.discrete(axis, discrete))

    # ── the keyboard ────────────────────────────────────────────────────
    def _keyboard_caps(self, has: bool) -> None:
        if has and self.keyboard is None and self.keys is not None:
            self.keyboard = self.seat.get_keyboard()
            k = self.keyboard.dispatcher
            k["keymap"] = self._keymap
            k["enter"] = self._kb_enter
            k["leave"] = lambda _k, _serial, _surface: self._keyboard_left()
            k["key"] = self._key
            k["modifiers"] = self._modifiers
            k["repeat_info"] = lambda _k, rate, delay: self.repeat.info(rate, delay)
        elif not has and self.keyboard is not None:
            self._keyboard_left()
            try:
                self.keyboard.release()
            except Exception:
                pass
            self.keyboard = None

    def _keymap(self, _k, fmt: int, fd: int, size: int) -> None:
        try:
            if fmt != KEYMAP_XKB_V1 or size <= 0:
                log.warning("keyboard: a keymap in format %d; keys unread", fmt)
                return
            with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as m:
                text = bytes(m[:size])
        except (OSError, ValueError) as e:
            log.warning("keyboard: the keymap could not be read (%s)", e)
            return
        finally:
            os.close(fd)
        first = not self.keys.ready
        if self.keys.load(text):
            km = self.keys.keymap
            names = [km.layout_get_name(i) or "?" for i in range(km.num_layouts())]
            (log.info if first else log.debug)("keyboard: keymap with %s",
                                               ", ".join(names) or "no layout")

    def _kb_enter(self, _k, _serial, surface, _keys) -> None:
        if not _same(surface, self.surface):
            return
        self.scene.keyboard.focused = True
        log.debug("keyboard: focus on the canvas")

    def _keyboard_left(self) -> None:
        self.repeat.stop()
        if self.scene.keyboard.focused:
            self.scene.keyboard.focused = False
            log.debug("keyboard: focus left the canvas")

    def _modifiers(self, _k, _serial, depressed, latched, locked, group) -> None:
        self.keys.modifiers(depressed, latched, locked, group)

    def _key(self, _k, _serial, _time, key: int, state: int) -> None:
        if not self.scene.keyboard.focused:
            return
        ev = self.keys.event(key)
        if ev is None:
            return
        if state == 1:
            self.repeat.press(key, self.keys.repeats(key))
            self.scene.keyboard.press(ev)
        else:
            self.repeat.release(key)
            self.scene.keyboard.release(ev)
        self.flush()

    def _repeat_key(self, key: int) -> None:
        if not self.scene.keyboard.focused or not self.scene.keyboard.wanted:
            self.repeat.stop()
            return
        ev = self.keys.event(key)
        if ev is not None:
            self.scene.keyboard.press(ev)
            self.flush()

    # ── shutdown ────────────────────────────────────────────────────────
    def close(self) -> None:
        if self._closed:
            return
        log.info("canvas: %s", self.scene.stats.summary())
        self.repeat.stop()
        if self._reopen is not None:
            self._reopen.cancel()
            self._reopen = None
        for strip in self.strips.values():
            strip.destroy()
        self.strips = {}
        self._drop_overlay()
        if self.gl is not None:
            try:
                self.gl.destroy()
            except Exception:
                log.debug("canvas: EGL teardown failed", exc_info=True)
            self.gl = None
        if self._idle_surface is not None:
            try:
                self._idle_surface.destroy()
            except Exception:
                pass
        if self.display is not None:
            try:
                self.display.flush()
            except Exception:
                pass
        self._closed = True
        self.disconnect()


def connect(environ, loop=None) -> Canvas | None:
    """The canvas when the shell should draw on Wayland, else None (X11).
    See `backend_order`."""
    order = backend_order(environ)
    if order[0] != "wayland":
        return None
    canvas = Canvas(loop)
    try:
        canvas.connect()
    except NoLayerShell as e:
        if len(order) == 1:
            raise
        log.info("%s; using X11", e)
        return None
    except DisplayError as e:
        if len(order) == 1 or not environ.get("DISPLAY"):
            raise
        log.warning("%s; using X11", e)
        return None
    return canvas
