"""The one canvas: how the shell's windows live on Wayland.

On X every WindowSpec is an X window of its own (window.py). On a
layer-shell compositor (niri) the shell draws everything on ONE
full-screen surface on the `overlay` layer, above every window, and a
WindowSpec becomes a *region* of it: positioned by its anchor, margins
and size exactly as its X window would be on a monitor the size of the
canvas, painted into pixels of its own, and composed onto the canvas
where its damage says. The bars only *reserve* their strips, through
invisible surfaces of their own (backend/wayland.py), so the compositor
tiles windows between them; the canvas covers the strips and draws the
bars there. The canvas's input region is exactly what is drawn: a click
anywhere else reaches the windows below.

    Scene          the regions on the canvas, bottom to top; their
                   damage in canvas coordinates; the input region and
                   the reservations they make; the pointer's routing.
                   No Wayland here: backend/wayland.py presents what it
                   composes, and the tests compose it off screen.
    Region         a Window (window.py) whose pixels are a region of the
                   canvas. Everything that is not X — the frame clock,
                   damage, the widget tree, painting, the effects, the
                   pointer's press/motion/release/hold routing — is
                   Window's own code; Region replaces what X did: map,
                   unmap, move, present, grabs.
    PointerRouter  wl_pointer's events to the region under the pointer,
                   with X's implicit grab: from a press until every
                   button is up, motion and the release go to the region
                   the press went to, wherever the pointer is.

Phase 1 puts only the bars on the canvas (`on_canvas`); every other kind
is refused at open (`NotOnCanvas`) and named once at start. Phase 2's
panels, launcher, call, toasts and the drop-down are regions like these:
the canvas already z-orders by layer, composes regions with bleed and
effects, and keeps the input region equal to what is drawn; they add an
outside-click rule (a click outside an open drop-down closes it and is
swallowed: the input region becomes the whole canvas while one is open)
and the keyboard (layer-shell keyboard_interactivity).
"""

import itertools
import logging
import time
from dataclasses import dataclass

import skia

from .backend.x11 import Monitor
from .window import Anchor, Layer, Window, WindowSpec

log = logging.getLogger(__name__)

TRANSPARENT = skia.ColorSetARGB(0, 0, 0, 0)

# Bottom to top. Within a layer, the region shown last is on top.
_LAYER_RANK = {Layer.DESKTOP: 0, Layer.BOTTOM: 1, Layer.NORMAL: 2,
               Layer.DOCK: 3, Layer.OVERLAY: 4}

# Linux input event codes (wl_pointer.button) to X's button numbers,
# which is what the widgets speak (Widget.click, press, release).
BUTTONS = {0x110: 1, 0x112: 2, 0x111: 3, 0x113: 8, 0x114: 9}
# A wheel's click, in wl_pointer.axis units, when no discrete step came
# with it (a touchpad's continuous scroll): libinput's 15 per detent.
AXIS_STEP = 15.0


class NotOnCanvas(RuntimeError):
    """This kind of window is not drawn on the Wayland canvas yet."""


def edge_of(anchor: Anchor) -> str | None:
    """The one screen edge a bar is anchored to ("top", "bottom", "left",
    "right"), or None when the anchor names no single edge. The
    perpendicular edges may be named too (top+left+right is the top)."""
    vertical = {Anchor.TOP: "top", Anchor.BOTTOM: "bottom"}
    horizontal = {Anchor.LEFT: "left", Anchor.RIGHT: "right"}
    v = [name for flag, name in vertical.items() if flag in anchor]
    h = [name for flag, name in horizontal.items() if flag in anchor]
    if len(v) == 1 and len(h) != 1:
        return v[0]
    if len(h) == 1 and len(v) != 1:
        return h[0]
    return None


def on_canvas(spec: WindowSpec) -> bool:
    """Whether the canvas draws this kind yet. Phase 1: the bars, the
    kinds that reserve one edge of the screen."""
    return spec.exclusive and edge_of(spec.anchor) is not None


def reserved(edge: str, content: tuple[int, int, int, int],
             size: tuple[int, int]) -> int:
    """How far from `edge` a bar whose content box is `content` (x, y,
    w, h on the canvas) reaches into a canvas of `size`: its exclusive
    zone, margin included — what X's strut says."""
    x, y, w, h = content
    cw, ch = size
    return max(0, {"top": y + h, "bottom": ch - y,
                   "left": x + w, "right": cw - x}[edge])


def button_of(code: int) -> int | None:
    return BUTTONS.get(code)


def scroll_buttons(axis: int, steps: int) -> list[int]:
    """X's wheel buttons for `steps` wheel clicks on `axis` (0 vertical,
    1 horizontal): 4 up, 5 down, 6 left, 7 right; negative steps go up
    or left, as wl_pointer.axis values do."""
    if axis == 0:
        button = 4 if steps < 0 else 5
    else:
        button = 6 if steps < 0 else 7
    return [button] * abs(steps)


class AxisSteps:
    """wl_pointer's scroll as whole wheel clicks: the discrete steps a
    wheel sends (axis_discrete, before its axis event in the same
    frame), else the continuous values summed into AXIS_STEP units."""

    def __init__(self) -> None:
        self._rest = [0.0, 0.0]
        self._discrete = [False, False]

    def discrete(self, axis: int, steps: int) -> list[int]:
        if axis not in (0, 1):
            return []
        self._discrete[axis] = True
        self._rest[axis] = 0.0
        return scroll_buttons(axis, steps)

    def value(self, axis: int, value: float) -> list[int]:
        if axis not in (0, 1):
            return []
        if self._discrete[axis]:
            return []           # the same click, already counted
        self._rest[axis] += value
        steps = int(self._rest[axis] / AXIS_STEP)
        if not steps:
            return []
        self._rest[axis] -= steps * AXIS_STEP
        return scroll_buttons(axis, steps)

    def frame(self) -> None:
        self._discrete = [False, False]

    def stop(self, axis: int) -> None:
        if axis in (0, 1):
            self._rest[axis] = 0.0


def rects(region: skia.Region) -> list[skia.IRect]:
    out = []
    it = skia.Region.Iterator(region)
    while not it.done():
        out.append(it.rect())
        it.next()
    return out


class DamageHistory:
    """What a back buffer of a given age is missing (EGL_EXT_buffer_age:
    the buffer holds the frame from `age` presents ago, so it lacks the
    damage of the `age - 1` frames since)."""

    KEEP = 4

    def __init__(self) -> None:
        self.frames: list[skia.Region] = []

    def clear(self) -> None:
        self.frames = []

    def repaint(self, damage: skia.Region, age: int,
                full: skia.IRect) -> skia.Region:
        """The region to paint into a buffer of `age` for a frame whose
        own damage is `damage`. Age 0 (unknown) or older than what is
        kept: the whole canvas."""
        if age <= 0 or age > len(self.frames) + 1:
            return skia.Region(full)
        out = skia.Region(damage)
        for old in self.frames[len(self.frames) - (age - 1):] if age > 1 else []:
            out.op(old, skia.Region.kUnion_Op)
        return out

    def push(self, damage: skia.Region) -> None:
        self.frames = (self.frames + [skia.Region(damage)])[-self.KEEP:]


@dataclass
class _Button:
    """What Window's press and release paths read off an X button
    event."""
    detail: int
    event_x: float
    event_y: float


class PointerRouter:
    """The canvas's pointer, routed to regions in their own coordinates."""

    def __init__(self, scene: "Scene") -> None:
        self.scene = scene
        self.xy: tuple[float, float] | None = None
        self.over: "Region | None" = None        # hovered
        self.grab: "Region | None" = None        # had the press
        self.buttons: set[int] = set()

    @staticmethod
    def _local(region: "Region", x: float, y: float) -> tuple[float, float]:
        return x - region.x, y - region.y

    def enter(self, x: float, y: float) -> None:
        self.motion(x, y)

    def motion(self, x: float, y: float) -> None:
        self.xy = (x, y)
        if self.grab is not None:
            self.grab.pointer_motion(*self._local(self.grab, x, y))
            return
        region = self.scene.region_at(x, y)
        if region is not self.over:
            if self.over is not None:
                self.over.pointer_leave()
            self.over = region
        if region is not None:
            region.pointer_motion(*self._local(region, x, y))

    def leave(self) -> None:
        """The pointer left the canvas. Mid-press, the compositor broke
        the press's grab (a keybinding, a workspace switch): the release
        will never come."""
        if self.grab is not None:
            grab, self.grab = self.grab, None
            self.buttons.clear()
            grab.pointer_lost()
        if self.over is not None:
            self.over.pointer_leave()
            self.over = None
        self.xy = None

    def button(self, button: int, pressed: bool) -> None:
        if self.xy is None:
            return
        x, y = self.xy
        if pressed:
            target = self.grab or self.scene.region_at(x, y)
            if target is None:
                return
            if not self.buttons:
                self.grab = target
            self.buttons.add(button)
            target.pointer_button(button, True, *self._local(target, x, y))
            return
        if button not in self.buttons:
            return
        self.buttons.discard(button)
        target = self.grab
        if target is not None:
            target.pointer_button(button, False, *self._local(target, x, y))
        if not self.buttons:
            self.grab = None
            # As X does when its grab ends: the region now under the
            # pointer is the hovered one again.
            self.motion(x, y)

    def scroll(self, buttons: list[int]) -> None:
        if self.xy is None or not buttons:
            return
        x, y = self.xy
        target = self.grab or self.scene.region_at(x, y)
        if target is None:
            return
        for b in buttons:
            target.pointer_button(b, True, *self._local(target, x, y))
            target.pointer_button(b, False, *self._local(target, x, y))

    def forget(self, region: "Region") -> None:
        """`region` is off the canvas: no hover or grab on it any more."""
        if self.over is region:
            self.over = None
        if self.grab is region:
            self.grab = None
            self.buttons.clear()

    def reset(self) -> None:
        """The canvas went away (a fullscreen window): nothing is under
        the pointer, and a press in progress is lost."""
        self.leave()


class GpuScratch:
    """What a Region on the GPU canvas takes as its `gl` (Window's GL
    backend): the one context is always current, so there is nothing
    to make current, and its offscreen surfaces live in that context."""

    def __init__(self, gl) -> None:
        self._gl = gl

    def make_current(self, _surface=None) -> bool:
        return True

    def offscreen(self, width: int, height: int) -> skia.Surface:
        return self._gl.offscreen(width, height)


class Scene:
    """The regions on the canvas and what the compositor needs of them.

    Also what a Region takes as its `display`: the canvas is the one
    monitor (`primary_monitor`), and `flush`/`pump` push requests out.
    """

    def __init__(self, loop=None, *, output: str = "wayland") -> None:
        self.loop = loop
        self.width = self.height = 0
        self.output = output
        self.regions: list[Region] = []         # on the canvas, bottom to top
        self.damage = skia.Region()
        self.hidden = False
        self.pointer = PointerRouter(self)
        self.gpu: GpuScratch | None = None      # a Region's `gl`; None: raster
        # The backend's: a present is due; the input region or the
        # reservations changed; send what is queued.
        self.on_damage = None
        self.on_layout = None
        self.on_flush = None
        self._ids = itertools.count(1)
        self._order = itertools.count()
        self.stats = Stats()

    # ── as a display ────────────────────────────────────────────────────
    def primary_monitor(self) -> Monitor:
        return Monitor(0, 0, self.width, self.height, primary=True, name=self.output)

    def monitors(self) -> list[Monitor]:
        return [self.primary_monitor()]

    def flush(self) -> None:
        if self.on_flush is not None:
            self.on_flush()

    pump = flush

    def keep_above(self, _wid, keep: bool = True) -> None:
        """Nothing to hold up: the canvas is on the overlay layer."""

    def next_id(self) -> int:
        return next(self._ids)

    # ── size ────────────────────────────────────────────────────────────
    @property
    def bounds(self) -> skia.IRect:
        return skia.IRect.MakeWH(self.width, self.height)

    def resize(self, width: int, height: int) -> bool:
        """The canvas is now `width` x `height`; its regions are placed
        again on it at once (they belong to it), and all of it is
        damaged. False when nothing changed."""
        if (width, height) == (self.width, self.height):
            return False
        self.width, self.height = width, height
        for region in list(self.regions):
            try:
                region.relocate()
            except Exception:
                log.exception("relocating %s on the canvas failed", region.spec.name)
        self.damage_all()
        self._layout()
        return True

    # ── regions ─────────────────────────────────────────────────────────
    def add(self, region: "Region") -> None:
        if region in self.regions:
            return
        region._z = next(self._order)
        self.regions.append(region)
        self.regions.sort(key=lambda r: (_LAYER_RANK.get(r.spec.layer, 2), r._z))
        self._damage_box(region.box())
        self._layout()

    def remove(self, region: "Region") -> None:
        if region not in self.regions:
            return
        self.regions.remove(region)
        self.pointer.forget(region)
        self._damage_box(region.box())
        self._layout()

    def moved(self, region: "Region", old: skia.IRect) -> None:
        if region not in self.regions:
            return
        self._damage_box(old)
        self._damage_box(region.box())
        self._layout()

    def update(self, region: "Region", local: skia.Region | None) -> None:
        """`region` has new pixels: `local` (its own coordinates), or all
        of them."""
        if region not in self.regions:
            return
        box = region.box()
        if local is None:
            self._damage_box(box)
            return
        moved = local.translate(region.x, region.y)
        moved.op(box, skia.Region.kIntersect_Op)
        if moved.isEmpty():
            return
        self.damage.op(moved, skia.Region.kUnion_Op)
        self._changed()

    def damage_all(self) -> None:
        self._damage_box(self.bounds)

    def _damage_box(self, box: skia.IRect) -> None:
        if box.isEmpty():
            return
        self.damage.op(box, skia.Region.kUnion_Op)
        self._changed()

    def _changed(self) -> None:
        if self.on_damage is not None:
            self.on_damage()

    def _layout(self) -> None:
        if self.on_layout is not None:
            self.on_layout()

    def take_damage(self) -> skia.Region:
        damage, self.damage = self.damage, skia.Region()
        damage.op(self.bounds, skia.Region.kIntersect_Op)
        return damage

    def region_at(self, x: float, y: float) -> "Region | None":
        for region in reversed(self.regions):
            if region.takes_input(x, y):
                return region
        return None

    # ── what the compositor needs ───────────────────────────────────────
    def input_rects(self) -> list[skia.IRect]:
        """Where the canvas takes the pointer: what its regions draw,
        their content boxes (a bleed margin is only room for an effect
        to draw in)."""
        if self.hidden:
            return []
        region = skia.Region()
        for r in self.regions:
            region.op(r.input_box(), skia.Region.kUnion_Op)
        region.op(self.bounds, skia.Region.kIntersect_Op)
        return rects(region)

    def reservations(self) -> dict[str, int]:
        """Edge -> how far the bars on it reach in: what the invisible
        strips reserve."""
        out: dict[str, int] = {}
        for r in self.regions:
            if not r.spec.exclusive:
                continue
            edge = edge_of(r.spec.anchor)
            if edge is None:
                continue
            out[edge] = max(out.get(edge, 0),
                            reserved(edge, r.content_root, (self.width, self.height)))
        return out

    # ── composing ───────────────────────────────────────────────────────
    def compose(self, canvas: skia.Canvas, paint: skia.Region) -> int:
        """Paint `paint` (canvas coordinates) of the canvas: cleared to
        transparent, then every region over it, bottom to top. Returns
        the pixels painted."""
        pictures: dict[int, skia.Image | None] = {}
        area = 0
        for rect in rects(paint):
            area += rect.width() * rect.height()
            canvas.save()
            canvas.clipRect(skia.Rect.Make(rect))
            canvas.clear(TRANSPARENT)
            for region in self.regions:
                if not skia.IRect.Intersects(region.box(), rect):
                    continue
                key = id(region)
                if key not in pictures:
                    pictures[key] = region.picture()
                image = pictures[key]
                if image is not None:
                    canvas.drawImage(image, region.x, region.y)
            canvas.restore()
        return area

    # ── a fullscreen window ─────────────────────────────────────────────
    def suspend(self) -> None:
        """The canvas is gone (a fullscreen window): the regions stop
        their clocks and the pointer is nobody's."""
        self.hidden = True
        self.pointer.reset()
        for region in self.regions:
            region.suspend()

    def resume(self) -> None:
        self.hidden = False
        for region in self.regions:
            region.resume()
        self.damage_all()


class Stats:
    """Frame timings, for the log and the tests: each region's paint and
    each present of the canvas, in ms."""

    KEEP = 2000

    def __init__(self) -> None:
        self.paints: dict[str, list[float]] = {}
        self.presents: list[tuple[float, int, int]] = []    # ms, painted px, damaged px
        self.started = time.monotonic()

    def paint(self, name: str, ms: float) -> None:
        values = self.paints.setdefault(name, [])
        values.append(ms)
        if len(values) > self.KEEP:
            del values[:len(values) - self.KEEP]

    def present(self, ms: float, painted: int, damaged: int) -> None:
        self.presents.append((ms, painted, damaged))
        if len(self.presents) > self.KEEP:
            del self.presents[:len(self.presents) - self.KEEP]

    @staticmethod
    def median(values) -> float:
        values = sorted(values)
        return values[len(values) // 2] if values else 0.0

    def summary(self) -> str:
        parts = [f"{len(self.presents)} presents in "
                 f"{time.monotonic() - self.started:.0f}s, median "
                 f"{self.median(p[0] for p in self.presents):.2f} ms"]
        for name, values in sorted(self.paints.items()):
            parts.append(f"{name} painted {len(values)}x, median "
                         f"{self.median(values):.2f} ms")
        return "; ".join(parts)


class Region(Window):
    """A WindowSpec as a region of the canvas. See the module docstring.

    `x`, `y`, `width`, `height` are its box on the canvas (bleed
    included), as they are an X window's on the screen; `display` is the
    Scene. Its pixels are its own surface (`surface`, persistent between
    frames, like an X window's), composed onto the canvas where it was
    damaged.
    """

    def __init__(self, scene: Scene, spec: WindowSpec, loop=None) -> None:
        super().__init__(scene, spec, loop)
        self.scene = scene
        self.gl = scene.gpu
        self._z = 0
        self._picture = None            # an Image or a Surface: what is shown
        self._dirty_local: skia.Region | None = None
        self._suspended = False
        self._painted = 0
        self._said: set[str] = set()

    # ── geometry ────────────────────────────────────────────────────────
    def box(self) -> skia.IRect:
        return skia.IRect.MakeXYWH(self.x, self.y, self.width, self.height)

    def input_box(self) -> skia.IRect:
        cx, cy, cw, ch = self.content_root
        return skia.IRect.MakeXYWH(cx, cy, cw, ch)

    def takes_input(self, x: float, y: float) -> bool:
        return self.input_box().contains(int(x), int(y)) and self.content is not None

    def create(self, monitor: Monitor | None = None) -> None:
        mon = monitor or self.scene.primary_monitor()
        cx, cy, cw, ch = self._compute_geometry(mon)
        self.bleed = b = self._compute_bleed(cw, ch)
        self.content_root = (cx, cy, cw, ch)
        self.content_rect = skia.Rect.MakeXYWH(b, b, cw, ch)
        self.x, self.y = cx - b, cy - b
        self.width, self.height = cw + 2 * b, ch + 2 * b
        self.wid = self.scene.next_id()
        self.surface = self._make_surface(self.width, self.height)
        if self.content is not None:
            self.content.attach(self)
            if self.holo is not None:
                self.holo.attach(self)
            self._layout_content()
        log.info("created %s on the canvas %dx%d+%d+%d (%s)%s", self.spec.name,
                 self.width, self.height, self.x, self.y,
                 "GPU" if self.gl is not None else "raster",
                 f" (+{self.bleed}px bleed)" if self.bleed else "")

    def _apply_geometry(self, cx: int, cy: int, cw: int, ch: int) -> None:
        if (cx, cy, cw, ch) == self.content_root:
            return
        old = self.box()
        self.bleed = b = self._compute_bleed(cw, ch)
        self.content_root = (cx, cy, cw, ch)
        self.x, self.y = cx - b, cy - b
        self._adopt_size(cw + 2 * b, ch + 2 * b)
        self._picture = None            # the old size's, until the next frame
        self.scene.moved(self, old)

    def _set_struts(self) -> None:
        """The reservation follows the box: the strips are the scene's
        (`Scene.reservations`), sent when the layout changes."""
        self.scene._layout()

    # ── on and off the canvas ───────────────────────────────────────────
    def _map(self) -> None:
        self.scene.add(self)

    def _raise(self) -> None:
        pass

    def _unmap(self) -> None:
        self.scene.remove(self)

    def show(self) -> None:
        self._suspended = False
        super().show()
        if self.scene.hidden:
            self.suspend()

    def hide(self) -> None:
        if self.wid is None:
            return
        self._stop_clock()
        self.scene.remove(self)
        self.mapped = False
        self._suspended = False

    def suspend(self) -> None:
        """The canvas went away under a fullscreen window: stop painting
        until `resume`. Changes meanwhile are damage, as on a hidden X
        window."""
        if self._suspended or not self.mapped:
            return
        self._suspended = True
        self._end_hold(cancel=True)
        self._set_hover(None)
        self._stop_clock()
        self.mapped = False

    def resume(self) -> None:
        if not self._suspended:
            return
        self._suspended = False
        self.mapped = True
        self.damage(None)

    def destroy(self) -> None:
        if self.wid is None:
            return
        self._end_hold(cancel=True)
        self._stop_clock()
        self.scene.remove(self)
        self.surface = self._frozen = self._out = None
        self._small_in = self._small_out = None
        self._picture = None
        if self.content is not None and not self._parked:
            try:
                self.content.detach()
            except Exception:
                log.exception("detach failed for %s", self.spec.name)
            if self.holo is not None:
                self.holo.detach()
        log.info("destroyed %s on the canvas", self.spec.name)
        self.wid = None
        self.mapped = False
        self._suspended = False

    # ── frames ──────────────────────────────────────────────────────────
    def redraw(self) -> None:
        t0 = time.perf_counter()
        self._painted = 0
        super().redraw()
        if self._painted:
            ms = (time.perf_counter() - t0) * 1000
            self.scene.stats.paint(self.spec.name, ms)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("region %s painted in %.2f ms (%d px)", self.spec.name, ms,
                          self._painted)

    def _flush_paint(self) -> None:
        # The canvas's present submits the one context's work, this
        # region's paint with it: a submit of its own per frame cost
        # 0.8 ms on the GPU for nothing.
        pass

    def _take_damage(self):
        region = super()._take_damage()
        self._dirty_local = skia.Region(region) if region is not None else None
        return region

    def _show_frame(self, frame: skia.Image) -> None:
        dirty, self._dirty_local = self._dirty_local, None
        if self._frozen is not None:
            self._picture = frame
            self._painted = self.width * self.height
            self.scene.update(self, None)
            return
        self._picture = self.surface
        if dirty is None or dirty.isEmpty():
            return
        self._painted = sum(r.width() * r.height() for r in rects(dirty))
        self.scene.update(self, dirty)

    def _out_surface(self) -> skia.Surface:
        if self._out is None or self._out.width() != self.width \
                or self._out.height() != self.height:
            self._out = self._make_surface(self.width, self.height)
        return self._out

    def _show_out(self, out: skia.Surface) -> None:
        self._dirty_local = None
        self._picture = out
        self._painted = self.width * self.height
        self.scene.update(self, None)

    def picture(self) -> skia.Image | None:
        """What the canvas shows of this region now."""
        p = self._picture
        if p is None:
            return None
        if isinstance(p, skia.Surface):
            return p.makeImageSnapshot()
        return p

    # ── what X did and the canvas does not (yet) ────────────────────────
    def _say_once(self, what: str, message: str, *args) -> None:
        if what not in self._said:
            self._said.add(what)
            log.info(message, *args)

    def _set_bounding_shape(self) -> None:
        pass                    # phase 2: ext-background-effect's blur region

    def _set_blur(self, on: bool) -> None:
        pass

    def _grab(self, what: str, since: float | None = None) -> None:
        self._grab_retry.pop(what, None)
        self._say_once(what, "%s: no %s grab on the canvas yet", self.spec.name, what)

    def _watch_hold(self) -> None:
        # Wayland's implicit grab brings the release to the canvas, or a
        # leave when the compositor breaks it (`pointer_lost`): nothing
        # to poll for.
        pass

    def hold_keyboard(self, widget) -> bool:
        # Phase 2: keyboard_interactivity for the hold. Without it the
        # hold goes on without Escape, as when X refuses the keyboard.
        return False

    # ── the pointer, from PointerRouter ─────────────────────────────────
    def pointer_motion(self, x: float, y: float) -> None:
        if not self._suspended:
            self._motion(x, y)

    def pointer_leave(self) -> None:
        self._set_hover(None)

    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None:
        if self._suspended:
            return
        ev = _Button(button, x, y)
        if pressed:
            self._on_press(ev)
        else:
            self._on_release(ev)

    def pointer_lost(self) -> None:
        """The press's release will not come: let go of everything it
        started, as X does for a release it never saw."""
        if self._held is not None:
            log.info("%s: the press's grab was broken; its hold let go", self.spec.name)
            self._end_hold(cancel=True)
        if self._dragging is not None:
            self._deliver_drag(*self._drag_xy, commit=True)
            self._dragging = None
        self._unpress()
