"""The one canvas: where the shell's windows live.

The shell draws everything on ONE full-screen surface on layer-shell's
`overlay` layer, above every window, and each live window (window.py
`Window`) is a *region* of it: positioned by its anchor, margins and
size on a monitor the size of the canvas, painted into pixels of its
own, and composed onto the canvas where its damage says. The bars only
*reserve* their strips, through invisible surfaces of their own
(backend/wayland.py), so the compositor tiles windows between them; the
canvas covers the strips and draws the bars there. The canvas's input
region is exactly what is drawn: a click anywhere else reaches the
windows below.

    Scene          the windows on the canvas, bottom to top; their
                   damage in canvas coordinates; the input region and
                   the reservations they make; the pointer's routing.
                   No Wayland here: backend/wayland.py presents what it
                   composes, and the tests compose it off screen.
    PointerRouter  wl_pointer's events to the window under the pointer,
                   with the compositor's implicit grab: from a press
                   until every button is up, motion and the release go to
                   the window the press went to, wherever the pointer
                   is. A press on no window while something catches
                   outside clicks is that click: they are told, and it
                   goes nowhere.
    KeyboardRouter wl_keyboard's keys (backend/wlkeys.py makes them
                   KeyEvents) to whoever took the keyboard last: a window
                   that takes it (`grab_keyboard`), a press held for
                   Escape (`hold_keyboard`), the top bar's open input.

The keyboard and outside clicks. While a window that takes the keyboard
is open the canvas asks for it (layer-shell keyboard_interactivity
`exclusive`), and gives it back (`none`) when the last one closes, so
the compositor returns the focus to the window that had it. While a
window that closes on an outside click is open the canvas's input
region is the whole canvas: a click outside every window closes it and
is swallowed, and clicks on the other windows (the bars) reach them.
With several open only the newest is told (the systray's menu, not the
panel under it).

Blur is the compositor's. A `blur` window's plates (its content's
`blur_shape()`, rasterised to whole pixels, so a 45° bevel is a stair of
one-pixel rows) are where the compositor blurs what is behind the
canvas (ext-background-effect's blur region), while the window is still:
not during its spawn or despawn.

A fullscreen window: the bars are not drawn over it, but whatever the
user calls up is (`shows`): every window that is not a bar — the
launcher, the chord menu, the panels, the call, the drop-down, the
toasts — and a bar while its open input catches the outside clicks (the
top bar's). The canvas surface is there only while one of those is
(`wants_surface`), so a fullscreen window alone is scanned out.
"""

import itertools
import logging
import time

import skia

from .window import Anchor, Layer, Monitor, Window, WindowSpec, rects

log = logging.getLogger(__name__)

TRANSPARENT = skia.ColorSetARGB(0, 0, 0, 0)

# Bottom to top. Within a layer, the region shown last is on top.
_LAYER_RANK = {Layer.DESKTOP: 0, Layer.BOTTOM: 1, Layer.NORMAL: 2,
               Layer.DOCK: 3, Layer.OVERLAY: 4}

# Linux input event codes (wl_pointer.button) to the button numbers the
# widgets speak (Widget.click, press, release): 1 left, 2 middle, 3
# right, 8 back, 9 forward; the wheel is 4-7 (`scroll_buttons`).
BUTTONS = {0x110: 1, 0x112: 2, 0x111: 3, 0x113: 8, 0x114: 9}
# A wheel's click, in wl_pointer.axis units, when no discrete step came
# with it (a touchpad's continuous scroll): libinput's 15 per detent.
AXIS_STEP = 15.0


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


def is_bar(spec: WindowSpec) -> bool:
    """A bar: a kind that reserves one edge of the screen."""
    return spec.exclusive and edge_of(spec.anchor) is not None


def reserved(edge: str, content: tuple[int, int, int, int],
             size: tuple[int, int]) -> int:
    """How far from `edge` a bar whose content box is `content` (x, y,
    w, h on the canvas) reaches into a canvas of `size`: its exclusive
    zone, margin included."""
    x, y, w, h = content
    cw, ch = size
    return max(0, {"top": y + h, "bottom": ch - y,
                   "left": x + w, "right": cw - x}[edge])


def button_of(code: int) -> int | None:
    return BUTTONS.get(code)


def scroll_buttons(axis: int, steps: int) -> list[int]:
    """The wheel's buttons for `steps` wheel clicks on `axis` (0 vertical,
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


class PointerRouter:
    """The canvas's pointer, routed to regions in their own coordinates."""

    def __init__(self, scene: "Scene") -> None:
        self.scene = scene
        self.xy: tuple[float, float] | None = None
        self.over: "Window | None" = None        # hovered
        self.grab: "Window | None" = None        # had the press
        self.buttons: set[int] = set()

    @staticmethod
    def _local(region: "Window", x: float, y: float) -> tuple[float, float]:
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
            if not self.buttons:
                target = self.scene.region_at(x, y)
                if target is None:
                    # On no region: an outside click when something
                    # catches them (the input region is the whole canvas
                    # then). Swallowed, its release with it.
                    if self.scene.outside_click():
                        self.buttons.add(button)
                    return
                self.grab = target
            self.buttons.add(button)
            target = self.grab
            if target is not None:      # None: an outside click's, swallowed
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
            # The grab over: the window now under the pointer is the
            # hovered one again.
            self.motion(x, y)

    def scroll(self, buttons: list[int]) -> None:
        if self.xy is None or not buttons:
            return
        x, y = self.xy
        if self.buttons and self.grab is None:
            return                      # in an outside click's press: swallowed
        target = self.grab or self.scene.region_at(x, y)
        if target is None:
            # The wheel outside every window closes an open panel as a
            # click does.
            self.scene.outside_click()
            return
        for b in buttons:
            target.pointer_button(b, True, *self._local(target, x, y))
            target.pointer_button(b, False, *self._local(target, x, y))

    def forget(self, region: "Window") -> None:
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


class KeyboardRouter:
    """Who has the canvas's keyboard: the takers, each a token and a
    handler with `key_press(KeyEvent)` and `key_release(KeyEvent)`; the
    one that took it last gets the keys. Taking it again moves a taker
    to the top. The canvas asks the compositor for the keyboard while
    there is a taker (`wanted`) and the backend can read keys
    (`available`: a keymap library; without one asking would only take
    the keys away from the windows)."""

    def __init__(self, on_change=None) -> None:
        self.on_change = on_change
        self.takers: dict = {}          # token -> handler, oldest first
        self.available = False
        self.focused = False            # the compositor's keyboard focus is on the canvas
        self.released_at = -1e9         # when the last taker let go (monotonic)

    @property
    def wanted(self) -> bool:
        return self.available and bool(self.takers)

    def take(self, token, handler) -> None:
        had = self.wanted
        self.takers.pop(token, None)
        self.takers[token] = handler
        if self.wanted != had and self.on_change is not None:
            self.on_change()

    def give_back(self, token) -> None:
        if token not in self.takers:
            return
        had = self.wanted
        del self.takers[token]
        if had and not self.wanted:
            self.released_at = time.monotonic()
        if self.wanted != had and self.on_change is not None:
            self.on_change()

    def holds(self, token) -> bool:
        return token in self.takers

    def target(self):
        return next(reversed(self.takers.values()), None)

    def press(self, key) -> bool:
        """A key went down on the canvas: to the newest taker. False
        when nobody has the keyboard (the key is dropped)."""
        return self._deliver("key_press", key)

    def release(self, key) -> bool:
        return self._deliver("key_release", key)

    def _deliver(self, what: str, key) -> bool:
        handler = self.target()
        if handler is None:
            return False
        try:
            getattr(handler, what)(key)
        except Exception:
            log.exception("a key for %s failed", type(handler).__name__)
        return True


class GpuScratch:
    """What a Window on the GPU canvas takes as its `gl`: the one
    context is always current, so there is nothing to make current, and
    its offscreen surfaces live in that context."""

    def __init__(self, gl) -> None:
        self._gl = gl

    def make_current(self) -> bool:
        return True

    def offscreen(self, width: int, height: int) -> skia.Surface:
        return self._gl.offscreen(width, height)

    def finish(self) -> None:
        """Everything drawn so far done on the GPU."""
        self._gl.finish()


WARM_PX = 64


def warm_shaders(gpu: GpuScratch, specs) -> int:
    """Have the GPU compile, at start, the programs the first open of a
    panel would otherwise stop for (50-80 ms on its first frames): each
    kind's spawn and despawn chain as Window.redraw runs it (the frozen
    frame as a decal-tiled image shader under the passes, drawn with
    kSrc), and the hologram's raster and scan (widgets/holo.py) as a
    `holo` window paints them. Drawn small and thrown away. Returns how
    many chains were drawn."""
    from . import theme
    from .widgets.holo import Projection, raster

    src = gpu.offscreen(WARM_PX, WARM_PX)
    c = src.getCanvas()
    c.clear(TRANSPARENT)
    c.drawRect(skia.Rect.MakeXYWH(8, 8, 48, 48), skia.Paint(Color=skia.ColorWHITE))
    frame = src.makeImageSnapshot()
    out = gpu.offscreen(WARM_PX, WARM_PX)
    sampling = skia.SamplingOptions()
    chains: set = set()
    holo = False
    for spec in specs:
        holo = holo or spec.holo
        for appear in (0.5, 1.0):
            passes = tuple(e for e in spec.effects if e.active(appear))
            key = tuple(id(e) for e in passes)
            if not passes or key in chains:
                continue
            chains.add(key)
            shader = frame.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal, sampling)
            try:
                for effect in passes:
                    shader = effect.shader(shader, WARM_PX, WARM_PX, t=0.1, appear=appear,
                                           seed=1.0)
            except Exception:
                log.debug("warming %s's effects failed", spec.name, exc_info=True)
                continue
            paint = skia.Paint(Shader=shader)
            paint.setBlendMode(skia.BlendMode.kSrc)
            out.getCanvas().drawPaint(paint)
    if holo:
        # Window._paint_holo: the raster over the content, then the scan.
        oc = out.getCanvas()
        lines = skia.Paint(Shader=raster(theme.POPUP_RASTER))
        lines.setBlendMode(skia.BlendMode.kSrcATop)
        oc.drawRect(skia.Rect.MakeWH(WARM_PX, WARM_PX), lines)
        scan = Projection()
        scan.rect = skia.Rect.MakeWH(WARM_PX, WARM_PX)
        scan._y = WARM_PX / 2
        scan.paint(oc)
    gpu.finish()
    return len(chains)


class Scene:
    """The regions on the canvas and what the compositor needs of them.

    The canvas is the one monitor (`primary_monitor`), and `flush`
    pushes requests out.
    """

    def __init__(self, loop=None, *, output: str = "wayland") -> None:
        self.loop = loop
        self.width = self.height = 0
        self.output = output
        self.regions: list[Window] = []         # on the canvas, bottom to top
        self.damage = skia.Region()
        self.hidden = False                     # no canvas surface: nothing shows
        self.fullscreen = False                 # a fullscreen window: only what is called up shows
        self.pointer = PointerRouter(self)
        self.keyboard = KeyboardRouter(self._keyboard_changed)
        # Who closes on a click outside every window:
        # token -> callable, oldest first; and the region each belongs
        # to (its own, or the top bar for its open input), if any.
        self.catchers: dict = {}
        self.catcher_regions: dict = {}
        self.gpu: GpuScratch | None = None      # a Window's `gl`; None: raster
        # The backend's: a present is due; the input region or the
        # reservations changed; the keyboard is wanted or not any more;
        # send what is queued.
        self.on_damage = None
        self.on_layout = None
        self.on_keyboard = None
        self.on_flush = None
        self._ids = itertools.count(1)
        self._order = itertools.count()
        self.stats = Stats()

    # ── the one monitor ─────────────────────────────────────────────────
    def primary_monitor(self) -> Monitor:
        return Monitor(0, 0, self.width, self.height, primary=True, name=self.output)

    def flush(self) -> None:
        if self.on_flush is not None:
            self.on_flush()


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
    def add(self, region: "Window") -> None:
        if region in self.regions:
            return
        region._z = next(self._order)
        self.regions.append(region)
        self.regions.sort(key=lambda r: (_LAYER_RANK.get(r.spec.layer, 2), r._z))
        self._damage_box(region.box())
        self._layout()

    def remove(self, region: "Window") -> None:
        if region not in self.regions:
            return
        self.regions.remove(region)
        self.pointer.forget(region)
        self._damage_box(region.box())
        self._layout()

    def moved(self, region: "Window", old: skia.IRect) -> None:
        if region not in self.regions:
            return
        self._damage_box(old)
        self._damage_box(region.box())
        self._layout()

    def update(self, region: "Window", local: skia.Region | None) -> None:
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

    def region_at(self, x: float, y: float) -> "Window | None":
        for region in reversed(self.regions):
            if self.shows(region) and region.takes_input(x, y):
                return region
        return None

    # ── what shows ──────────────────────────────────────────────────────
    def over_fullscreen(self, region: "Window") -> bool:
        """Whether `region` shows over a fullscreen window: what the user
        calls up — every region but a bar, and a bar while its open
        input catches the outside clicks (the top bar's)."""
        if not is_bar(region.spec):
            return True
        return any(owner is region for owner in self.catcher_regions.values())

    def shows(self, region: "Window") -> bool:
        """Whether `region` is drawn and takes input now."""
        if self.hidden:
            return False
        return not self.fullscreen or self.over_fullscreen(region)

    def wants_surface(self) -> bool:
        """Whether the canvas needs its surface: always, but over a
        fullscreen window only while something is called up."""
        return not self.fullscreen or any(self.over_fullscreen(r) for r in self.regions)

    def set_fullscreen(self, fullscreen: bool) -> None:
        if fullscreen == self.fullscreen:
            return
        self.fullscreen = fullscreen
        self._settle()
        self._layout()

    def _settle(self) -> None:
        """Every region's clock running or stopped as `shows` says; one
        that stops showing while the canvas stays is cleared off it.
        Without the surface everything is stopped already (`suspend`), and
        a region being shown stops itself once it has taken its grabs
        (Window.show)."""
        if self.hidden:
            return
        for region in list(self.regions):
            if self.shows(region):
                if region.resume():
                    self._damage_box(region.box())
            elif region.suspend():
                self.pointer.forget(region)
                self._damage_box(region.box())

    # ── outside clicks and the keyboard ─────────────────────────────────
    def catch_outside(self, token, callback, region: "Window | None" = None) -> None:
        """Call `callback()` for a click outside every region, until
        `release_outside(token)`; the input region is the whole canvas
        meanwhile, so that click comes here and goes no further. `region`:
        the region it belongs to (shown over a fullscreen window
        meanwhile, if it is a bar)."""
        had = bool(self.catchers)
        self.catchers.pop(token, None)
        self.catchers[token] = callback
        self.catcher_regions[token] = region
        if self.fullscreen:
            self._settle()
        if not had or self.fullscreen:
            self._layout()

    def release_outside(self, token) -> None:
        self.catcher_regions.pop(token, None)
        if self.catchers.pop(token, None) is None:
            return
        if self.fullscreen:
            self._settle()
        if not self.catchers or self.fullscreen:
            self._layout()

    def outside_click(self) -> bool:
        """A click on no window: the newest catcher told, and only it:
        the systray's menu over its panel closes, the panel stays, and
        catches the next one once the menu has let go. False when nobody
        catches them (the click was not ours to take)."""
        if not self.catchers:
            return False
        token, callback = next(reversed(self.catchers.items()))
        log.debug("an outside click: %s closes (%d catching)",
                  getattr(getattr(token, "spec", None), "name", type(token).__name__),
                  len(self.catchers))
        try:
            callback()
        except Exception:
            log.exception("an outside click's handler failed")
        return True

    def _keyboard_changed(self) -> None:
        if self.on_keyboard is not None:
            self.on_keyboard()

    # ── what the compositor needs ───────────────────────────────────────
    def input_rects(self) -> list[skia.IRect]:
        """Where the canvas takes the pointer: what its regions draw,
        their content boxes (a bleed margin is only room for an effect
        to draw in); all of it while outside clicks are caught."""
        if self.hidden:
            return []
        if self.catchers:
            return [self.bounds]        # outside clicks are ours (catch_outside)
        region = skia.Region()
        for r in self.regions:
            if self.shows(r):
                region.op(r.input_box(), skia.Region.kUnion_Op)
        region.op(self.bounds, skia.Region.kIntersect_Op)
        return rects(region)

    def blur_rects(self) -> list[skia.IRect]:
        """Where the compositor blurs what is behind the canvas: the
        plates of the `blur` regions shown (Window.blur_rects)."""
        if self.hidden:
            return []
        region = skia.Region()
        for r in self.regions:
            if self.shows(r):
                for x, y, w, h in r.blur_rects():
                    region.op(skia.IRect.MakeXYWH(x, y, w, h), skia.Region.kUnion_Op)
        region.op(self.bounds, skia.Region.kIntersect_Op)
        return rects(region)

    def blur_changed(self, region: "Window") -> None:
        """`region`'s plates moved, or its blur went on or off."""
        if region in self.regions:
            self._layout()

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
                if not skia.IRect.Intersects(region.box(), rect) or not self.shows(region):
                    continue
                key = id(region)
                if key not in pictures:
                    pictures[key] = region.picture()
                image = pictures[key]
                if image is not None:
                    canvas.drawImage(image, region.x, region.y)
            canvas.restore()
        return area

    # ── the canvas surface gone and back ────────────────────────────────
    def suspend(self) -> None:
        """The canvas surface is gone (a fullscreen window with nothing
        called up, an output gone): the regions stop their clocks and
        the pointer is nobody's."""
        self.hidden = True
        self.pointer.reset()
        for region in list(self.regions):
            region.suspend()

    def resume(self) -> None:
        """The canvas surface is back: what shows starts again."""
        self.hidden = False
        self._settle()
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
