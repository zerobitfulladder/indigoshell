"""Windows: a `WindowSpec`, and the live `Window` it becomes on the canvas.

There is no Bar class. A bar is a `WindowSpec` whose layer is DOCK, whose
anchor is an edge, and which reserves space; a notification is the same
type with a different layer and no reservation; a chord menu is the same
type that takes the keyboard. The differences are fields, not Python
subclasses. The vocabulary — anchor, layer, exclusive zone — is
layer-shell's, which is what the canvas speaks (backend/wayland.py).

A live `Window` is a region of the one canvas (canvas.py `Scene`): its
box is placed by its anchor, margins and size on the canvas, its pixels
are a surface of its own (persistent between frames), and the canvas
composes them where its damage says. Everything else — the frame clock,
damage, the widget tree, painting, the effects, the pointer's
press/motion/release/hold routing, the keyboard taken while it is open,
the clicks outside it caught — is here.
"""

import asyncio
import logging
import math
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto

import skia

from . import theme
from .widgets.base import Insets, KeyEvent, Size, Widget
from .widgets.holo import Projection, raster

log = logging.getLogger(__name__)

# A running frame clock at least this fast paints new damage on its next
# tick (within 1/20s) rather than in a frame of its own — see damage().
COALESCE_FPS = 20
# The most one frame may move a spawn or despawn along, in frame
# intervals. A dropped frame passes unnoticed; a real stall holds the
# effect where it was instead of skipping it — see `_advance_phase`.
MAX_PHASE_STEP = 2.0
_ESCAPE = 0xFF1B


class Layer(Enum):
    """Stacking role among the shell's windows on the canvas."""

    DESKTOP = "desktop"    # wallpaper level
    BOTTOM = "bottom"      # below normal windows
    NORMAL = "normal"
    DOCK = "dock"          # bars and panels, above normal windows
    OVERLAY = "overlay"    # notifications, above everything


class Anchor(Flag):
    """Screen edges the window sticks to. Combine for corners."""

    NONE = 0
    TOP = auto()
    BOTTOM = auto()
    LEFT = auto()
    RIGHT = auto()


@dataclass(frozen=True)
class WindowSpec:
    """Everything that distinguishes one kind of window from another."""

    name: str
    layer: Layer = Layer.NORMAL
    anchor: Anchor = Anchor.NONE
    # None on an axis means "stretch to the monitor on that axis".
    size: tuple[int | None, int | None] = (None, None)
    margin: Insets = field(default_factory=Insets)

    # Reserve screen space so the compositor tiles windows clear of us.
    # This is the single flag that turns a floating window into a bar.
    exclusive: bool = False

    background: str = theme.BG
    # Accent rule along the edge that faces the screen interior. Window
    # chrome, like a border — not content.
    rule: str | None = None
    rule_thick: int = 2
    rule_glow: float = 0.0

    # Inner gutter between the window edge and its content widget.
    padding: Insets = field(default_factory=Insets)
    content: Widget | None = None
    # Post-process passes, applied in order. Any with a duration drives
    # the spawn/despawn animation.
    effects: tuple = ()
    # Resolution the effect chain runs at, as a fraction of the window.
    # SkSL on the CPU raster backend costs per pixel, and a full-size
    # pass over a big panel can't hit its frame rate — halving this is a
    # 4x saving, and a 160ms glitch hides the softness.
    effect_scale: float = 1.0

    # Close when a click lands outside. The canvas catches every click
    # while it is open, so it only makes sense for transient windows.
    dismiss_on_outside_click: bool = False
    # Take the keyboard while open, like a chord menu. Implies the window
    # is transient — a keyboard held forever locks the session.
    grab_keyboard: bool = False
    # Chords that close a window holding the keyboard, besides Escape: its
    # own keybinding. With the keyboard held here the compositor never
    # sees the second press, so the window has to know it. Written like
    # "super+a": modifiers (super, ctrl, alt, shift), then one key.
    close_keys: tuple[str, ...] = ()

    # Ask the compositor to blur whatever is behind the window, so a
    # translucent plate reads as frosted glass. For a plate that is not
    # the window's full rectangle, the content should provide
    # `blur_shape() -> skia.Path` in window coordinates: the blur goes
    # behind that path, rasterised to whole pixels, or the transparent
    # margin around the plates would show as a blurred box. A click in
    # the window's rectangle outside the path is outside the window. The
    # blur is only in force while the window is still: the spawn and
    # despawn run unblurred, since their tear reaches past the plates
    # into the bleed margin.
    blur: bool = False

    # Project the window as a hologram, the way the call is drawn
    # (widgets/holo.py): a faint raster over whatever the content paints,
    # and every few seconds a scan band that runs down it, tearing the
    # image as it passes. Drawn by the window over the content, so no
    # widget has to know; transparent pixels stay transparent. The call
    # draws its own, per plate, and leaves this off.
    holo: bool = False

    autostart: bool = False
    singleton: bool = True
    # Keep the window, its surface and its attached content between
    # opens: a close parks the window off the canvas instead of
    # destroying it, and the next open puts it back. Opening then costs a
    # repaint instead of the setup and the content's first layout. For
    # panels that are opened repeatedly; one-shot windows like toasts,
    # and kinds whose spec is rebuilt per open, do not benefit and are
    # never parked.
    keep_alive: bool = False


@dataclass(frozen=True)
class Placement:
    """Where one open of a window goes instead of where its anchor puts
    it: the content's top edge at `y`, centred on `x` (canvas
    coordinates), kept inside the canvas. Given to `Daemon.open`, kept
    while the window is open, and forgotten when it closes, so the next
    open goes by the anchor again.

    `below(x, y, w, h)` drops the window from a rect: centred under it,
    its top at the rect's bottom edge."""

    x: int
    y: int

    @classmethod
    def below(cls, x: int, y: int, w: int, h: int) -> "Placement":
        return cls(x + w // 2, y + h)


@dataclass(frozen=True)
class Monitor:
    """A box windows are placed in: the canvas, as the output it covers."""

    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    name: str = ""


def rects(region: skia.Region) -> list[skia.IRect]:
    """A region's rectangles."""
    out = []
    it = skia.Region.Iterator(region)
    while not it.done():
        out.append(it.rect())
        it.next()
    return out


def pixel_rects(path: skia.Path, clip: skia.IRect, dx: int = 0,
                dy: int = 0) -> tuple[tuple[int, int, int, int], ...]:
    """`path` as whole pixels inside `clip` (aliased: a 45° bevel becomes
    one thin rect per pixel row), as (x, y, w, h) rects moved by (dx,
    dy)."""
    region = skia.Region()
    region.setPath(path, skia.Region(clip))
    return tuple((r.left() + dx, r.top() + dy, r.width(), r.height()) for r in rects(region))


def _chord_is(key: KeyEvent, chord: str) -> bool:
    """Whether a key press is `chord` ("super+a"): exactly those
    modifiers held, and the key itself in either case."""
    *mods, name = chord.lower().split("+")
    held = {m for m in ("super", "ctrl", "alt", "shift") if getattr(key, m)}
    return (held == set(mods) and len(name) == 1
            and 0 < key.keysym < 0x100 and chr(key.keysym).lower() == name)


class Window:
    """A WindowSpec live on the canvas. See the module docstring.

    `x`, `y`, `width`, `height` are its box on the canvas, bleed
    included. Its pixels are its own surface (`surface`, persistent
    between frames), composed onto the canvas where it was damaged.
    """

    def __init__(self, scene, spec: WindowSpec,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.scene = scene              # canvas.Scene
        self.spec = spec
        self.loop = loop
        # The scene's number for it while it exists (`create` to
        # `destroy`); None before and after.
        self.wid: int | None = None
        self.x = self.y = 0
        self.width = self.height = 0
        self.surface: skia.Surface | None = None
        self.mapped = False
        self.content = spec.content
        # The scan over a `holo` window: a widget the window owns, outside
        # the content tree, attached, laid out, ticked and painted with it.
        self.holo = Projection() if spec.holo else None
        # Effects that displace pixels draw past the content's edge, so the
        # box is grown by `bleed` on every side and the content is inset
        # by it. `content_rect` is that inset area in window-local
        # coordinates; `content_root` is the same rect on the canvas,
        # which is what the reservations and placement care about.
        self.bleed = 0
        self.content_rect = skia.Rect.MakeEmpty()
        self.content_root = (0, 0, 0, 0)

        self._frame_queued = False      # a frame off the clock's grid
        self._ticking = False           # inside `_tick_widgets`
        self._relayout_queued = False
        # Dirty region for the next repaint, in window coordinates. A
        # *region*, not a bounding rect: the bar's animated widgets sit at
        # opposite ends of a 2552px strip, so their bounding box is the
        # whole bar and a rect would save nothing. The first frame has
        # nothing valid on the surface yet, so it starts as a full one.
        self._damage: skia.Region | None = None
        self._damage_all = True
        self._frame_handle: asyncio.TimerHandle | None = None
        self._frame_interval = 0.0
        self._next_frame_at = 0.0
        self._clock_fps = 0
        # The fastest rate any widget asked for at the last tick.
        self._widget_demand = 0
        self._t0 = 0.0
        self._t = 0.0
        self._hovered: Widget | None = None
        self._pressed: Widget | None = None
        # Widget holding the pointer for a drag, and the last pointer
        # position it saw — `self.x`/`self.y` are the *window's* origin,
        # not the cursor, so the release pass needs its own record.
        self._dragging: Widget | None = None
        self._drag_xy: tuple[float, float] = (0.0, 0.0)
        # A press a widget holds (`Widget.press`) and its button; whether
        # the keyboard is ours for it (`hold_keyboard`).
        self._held: Widget | None = None
        self._held_button = 0
        self._held_keys = False
        self._shown_sent = False
        # Hidden between opens with its content detached — see park().
        self._parked = False
        # This open's place, when it is not the anchor's — see Placement.
        self.placement: Placement | None = None

        # The canvas's GPU context (canvas.GpuScratch), or None: raster.
        self.gl = scene.gpu
        self.effects = tuple(spec.effects)
        self._transition = max((e.duration for e in self.effects), default=0.0)
        self._appear = 1.0
        self._phase = "idle"        # idle | enter | exit | gone
        # How much of the running spawn or despawn has played, and when
        # its last frame was — see `_advance_phase`.
        self._phase_elapsed = 0.0
        self._phase_at: float | None = None
        self._frozen: skia.Image | None = None
        self._out: skia.Surface | None = None
        self._sampling = skia.SamplingOptions()
        self._on_dismissed = None
        self._seed = 0.0
        # Whether it catches the clicks outside it (`dismiss_on_outside_click`),
        # and whether it holds the keyboard (`grab_keyboard`).
        self._grabbed = False
        self._kb_grabbed = False
        # Failures already logged — see `_complain`.
        self._complained: set[tuple] = set()
        # Whether the blur is in force — off during transitions, see
        # `WindowSpec.blur` — and the plates it is behind, (x, y, w, h) on
        # the canvas; empty while it is off.
        self._blur_on = False
        self._blur_rects: tuple = ()
        # What the canvas shows of it: an Image (a frozen or effected
        # frame) or its Surface; None until the first frame.
        self._picture = None
        # The damage the frame being drawn covers, window coordinates.
        self._dirty_local: skia.Region | None = None
        # Not shown for now (`suspend`): no canvas surface, or a
        # fullscreen window over a bar.
        self._suspended = False
        self._painted = 0               # pixels the frame drew, for the stats
        self._said: set[str] = set()
        self._z = 0                     # its place in the scene's stack (Scene.add)

    # ── geometry ────────────────────────────────────────────────────────
    def _compute_geometry(
        self, mon: Monitor,
        size: tuple[int | None, int | None] | None = None,
    ) -> tuple[int, int, int, int]:
        spec, m = self.spec, self.spec.margin
        want_w, want_h = size if size is not None else spec.size
        w = want_w if want_w is not None else mon.width - m.left - m.right
        h = want_h if want_h is not None else mon.height - m.top - m.bottom
        w, h = max(1, w), max(1, h)
        if self.placement is not None:
            return self._placed(w, h)

        a = spec.anchor
        if Anchor.LEFT in a and Anchor.RIGHT not in a:
            x = mon.x + m.left
        elif Anchor.RIGHT in a and Anchor.LEFT not in a:
            x = mon.x + mon.width - w - m.right
        else:
            x = mon.x + (mon.width - w) // 2

        if Anchor.TOP in a and Anchor.BOTTOM not in a:
            y = mon.y + m.top
        elif Anchor.BOTTOM in a and Anchor.TOP not in a:
            y = mon.y + mon.height - h - m.bottom
        else:
            y = mon.y + (mon.height - h) // 2
        return x, y, w, h

    def _placed(self, w: int, h: int) -> tuple[int, int, int, int]:
        """`placement`'s box: centred on its x, its top at its y, moved
        back inside the canvas. Only the content is kept inside; the
        bleed around it may run off the edge, as it does for an anchored
        window at the screen's edge."""
        p = self.placement
        mon = self.scene.primary_monitor()
        x = min(max(p.x - w // 2, mon.x), mon.x + max(0, mon.width - w))
        y = min(max(p.y, mon.y), mon.y + max(0, mon.height - h))
        return x, y, w, h

    def place(self, placement: "Placement | None") -> None:
        """Move the open window to `placement` (None: back to its anchor),
        at the size it has."""
        self.placement = placement
        if self.wid is None:
            return
        _cx, _cy, cw, ch = self.content_root
        self._apply_geometry(*self._compute_geometry(
            self.scene.primary_monitor(), size=(cw, ch)))

    def _compute_bleed(self, w: float, h: float) -> int:
        if not self.effects:
            return 0
        return int(math.ceil(max(e.bleed(w, h) for e in self.effects)))

    # ── creation ────────────────────────────────────────────────────────
    def box(self) -> skia.IRect:
        """Its box on the canvas, bleed included."""
        return skia.IRect.MakeXYWH(self.x, self.y, self.width, self.height)

    def input_box(self) -> skia.IRect:
        """Where it takes the pointer: its content, not the bleed."""
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

    def _layout_content(self) -> None:
        if self.content is None:
            return
        inner = self.spec.padding.deflate(self.content_rect)
        self.content.measure(Size(inner.width(), inner.height()))
        self.content.arrange(inner)
        if self.holo is not None:
            self.holo.arrange(self.content.rect)
        self._set_blur_shape()

    # ── blur: the compositor's, behind the plates ───────────────────────
    def _set_blur_shape(self) -> None:
        """The plates the compositor blurs behind (Scene.blur_rects),
        sent with the canvas's next commit: the content's `blur_shape()`
        as whole pixels, or its whole box — see `WindowSpec.blur`. Runs
        after every layout and move; the same plates are not sent
        again."""
        if not self.spec.blur or self.wid is None or self.content is None:
            return
        out: tuple = ()
        if self._blur_on:
            clip = skia.IRect.MakeWH(self.width, self.height)
            shape_of = getattr(self.content, "blur_shape", None)
            if shape_of is not None:
                out = pixel_rects(shape_of(), clip, self.x, self.y)
            else:
                cx, cy, cw, ch = self.content_root
                out = ((cx, cy, cw, ch),)
        if out == self._blur_rects:
            return
        self._blur_rects = out
        self.scene.blur_changed(self)

    def _set_blur(self, on: bool) -> None:
        """The blur in force, or lifted (the spawn and despawn run
        unblurred, their tear reaching past the plates)."""
        if not self.spec.blur or self.wid is None or on == self._blur_on:
            return
        self._blur_on = on
        self._set_blur_shape()

    def blur_rects(self) -> tuple:
        """The plates to blur behind, (x, y, w, h) on the canvas."""
        return self._blur_rects

    def reshape(self) -> None:
        """Send the plates again after the content moved its pieces
        itself, without a window relayout — the call overlay, which
        re-arranges its plates inside a window that keeps its size."""
        self._set_blur_shape()
    def _adopt_size(self, width: int, height: int) -> None:
        """Take on a new window size: surface, content rect, layout."""
        self.width, self.height = width, height
        b = self.bleed
        self.content_rect = skia.Rect.MakeXYWH(
            b, b, max(1, self.width - 2 * b), max(1, self.height - 2 * b))
        self.surface = self._make_surface(self.width, self.height)
        self._layout_content()
        # A fresh surface holds nothing, so a partial repaint would leave
        # uninitialised pixels everywhere it skipped.
        self.damage(None)

    def resize_content(self, cw: int, ch: int) -> None:
        """Resize to a new content size, re-anchored per the spec.

        For windows whose content set changes while open (the tray panel
        gaining a row) or is only known once measured (the network
        panel, which sizes itself to its interface list). Adopted at
        once: called from `attach()`, the first frame is already laid
        out for the new size.
        """
        if self.wid is None:
            return
        cw, ch = max(1, int(cw)), max(1, int(ch))
        cx, cy, cw, ch = self._compute_geometry(
            self.scene.primary_monitor(), size=(cw, ch))
        self._apply_geometry(cx, cy, cw, ch)

    def _apply_geometry(self, cx: int, cy: int, cw: int, ch: int) -> None:
        """Move/resize the live window to a new content box, if it differs."""
        if (cx, cy, cw, ch) == self.content_root:
            return
        old = self.box()
        self.bleed = b = self._compute_bleed(cw, ch)
        self.content_root = (cx, cy, cw, ch)
        self.x, self.y = cx - b, cy - b
        self._adopt_size(cw + 2 * b, ch + 2 * b)
        self._picture = None            # the old size's, until the next frame
        self.scene.moved(self, old)

    # ── lifecycle ───────────────────────────────────────────────────────
    def show(self) -> None:
        assert self.wid is not None
        self._suspended = False
        self._t0 = time.monotonic()
        self._seed = random.random() * 997.0
        if self._transition > 0:
            self._appear = 0.0
            self._begin_phase("enter")

        # Paint and freeze the content *before* it goes on the canvas.
        #
        # The widget tree paint is the expensive half of a frame — tens
        # of ms for a large panel, plus the first shader compile — and
        # doing it after leaves an empty window on screen for exactly
        # that long. The animation then started against a window the user
        # had already seen appear, so its opening frames were effectively
        # spent before anything was drawn. Painting first means the
        # window and the first animated frame land together.
        #
        # Freezing is the same trick dismiss() uses for the despawn: the
        # transition is a per-pixel pass over a still image, so
        # repainting the tree under it every frame buys nothing.
        if self.surface is not None:
            if self._relayout_queued:
                self._relayout_queued = False
                self._layout_content()
            self._census()
            self.paint(self.surface.getCanvas(), self.width, self.height)
            self.surface.flushAndSubmit()
            if self._phase == "enter":
                self._frozen = self.surface.makeImageSnapshot()

        self.scene.add(self)
        self.mapped = True
        if self._phase != "enter":
            self._set_blur(True)

        # The spawn's own time starts with its first frame on the clock,
        # not here: this frame draws it at zero, which for the panels'
        # effect is a transparent window, and nothing that happens between
        # the two is charged to it — see `_advance_phase`.
        self.redraw()
        if self.spec.dismiss_on_outside_click:
            self._grab("pointer")
        if self.spec.grab_keyboard:
            self._grab("keyboard")
        self._pace()
        if self._phase != "enter":
            self._notify_shown()
        if not self.scene.shows(self):
            self.suspend()

    def _notify_shown(self) -> None:
        """Tell the content it is visible and idle — once per open."""
        if self._shown_sent or self.content is None:
            return
        self._shown_sent = True
        try:
            self.content.on_shown()
        except Exception:
            log.exception("on_shown failed in %s", self.spec.name)

    def dismiss(self, on_done) -> None:
        """Play the despawn animation, then call `on_done`.

        The last painted frame is frozen and the exit pass runs over that
        still image, so the widget tree is never touched again once
        dismissal starts — which matters because `spec.content` is a
        single shared instance that a re-opened window may already own.
        """
        self._on_dismissed = on_done
        # The content hears first, while the tree is still this window's
        # (a reopen may give it to another window before the despawn ends).
        hook = getattr(self.content, "on_dismiss", None)
        if hook is not None:
            try:
                hook()
            except Exception:
                log.exception("on_dismiss failed in %s", self.spec.name)
        if (self._transition <= 0 or not self.mapped
                or self.loop is None or self.surface is None):
            self._end_hold(cancel=True)
            self._finish_dismiss()
            return
        self._end_hold(cancel=True)
        self._ungrab("pointer")
        self._ungrab("keyboard")
        self._set_blur(False)
        self._seed = random.random() * 997.0
        self._frozen = self.surface.makeImageSnapshot()
        self._begin_phase("exit")
        log.debug("%s despawning over %.2fs", self.spec.name, self._transition)
        self._stop_clock()
        self._pace()

    # ── the keyboard taken, the clicks outside caught ───────────────────
    def _grab(self, what: str) -> None:
        """Take the keyboard ("keyboard": the canvas asks the compositor
        for it while anyone holds it), or catch the clicks outside
        ("pointer": the canvas's input region is the whole canvas
        meanwhile, and a click outside every window closes this one)."""
        if self.wid is None or not self.mapped or self._dismissing():
            return
        if what == "pointer":
            self.scene.catch_outside(self, self._outside_click, self)
            self._grabbed = True
        else:
            if not self.scene.keyboard.available:
                self._say_once("keyboard", "%s: no keymap library (python-xkbcommon); "
                               "no keyboard on the canvas", self.spec.name)
                return
            self.scene.keyboard.take(self, self)
            self._kb_grabbed = True
        log.debug("%s took the %s", self.spec.name,
                  "outside clicks" if what == "pointer" else "keyboard")

    def _ungrab(self, what: str) -> None:
        if what == "pointer":
            if self._grabbed:
                self._grabbed = False
                self.scene.release_outside(self)
        elif self._kb_grabbed:
            self._kb_grabbed = False
            self.scene.keyboard.give_back(self)

    @property
    def keyboard_grabbed(self) -> bool:
        return self._kb_grabbed

    def _outside_click(self) -> None:
        if self._phase not in ("exit", "gone") and self.mapped:
            log.debug("%s dismissed by outside click", self.spec.name)
            self._request_close()

    def _say_once(self, what: str, message: str, *args) -> None:
        if what not in self._said:
            self._said.add(what)
            log.info(message, *args)

    def _finish_dismiss(self) -> None:
        callback, self._on_dismissed = self._on_dismissed, None
        self._phase = "gone"
        if callback is not None:
            callback()

    def hide(self) -> None:
        if self.wid is None:
            return
        self._stop_clock()
        self.scene.remove(self)
        self.mapped = False
        self._suspended = False

    def suspend(self) -> bool:
        """Not shown for now (the canvas went away, or a fullscreen
        window hides this bar): stop painting until `resume`. Changes
        meanwhile are damage. True when it was showing."""
        if self._suspended or not self.mapped:
            return False
        self._suspended = True
        self._end_hold(cancel=True)
        self._set_hover(None)
        self._stop_clock()
        self.mapped = False
        return True

    def resume(self) -> bool:
        if not self._suspended:
            return False
        self._suspended = False
        self.mapped = True
        self.damage(None)
        return True

    def relocate(self) -> None:
        """Re-place on the canvas after the screen changed — a display
        switched, a resolution set.

        Dimensions the spec leaves to the monitor (the bar's width) are
        recomputed; fixed ones keep the size the window has, which for a
        content-sized window is what `resize_content` gave it, not the
        spec's placeholder. The reservations follow (Scene.moved).
        """
        if self.wid is None:
            return
        _cx, _cy, cw, ch = self.content_root
        want_w, want_h = self.spec.size
        size = (cw if want_w is not None else None,
                ch if want_h is not None else None)
        cx, cy, cw, ch = self._compute_geometry(
            self.scene.primary_monitor(), size=size)
        self._apply_geometry(cx, cy, cw, ch)     # repaints, too
    def park(self) -> None:
        """Hide, and keep everything expensive for the next `show()`.

        What a closed panel pays to come back is the window, its surface
        and the content's first layout, so those stay. The content
        is detached exactly as `destroy()` would, so a parked panel's
        widgets run no subscriptions or samplers while it is out of
        sight, and `unpark()` re-attaches them just as a fresh `create()`
        would have. Everything the last open or dismiss left behind —
        the frozen despawn frame, the phase, the shown flag — is reset
        so the next `show()` starts as if from `create()`.
        """
        self.hide()
        self._end_hold(cancel=True)
        self._ungrab("pointer")
        self._ungrab("keyboard")
        self._set_blur(False)
        self._frozen = None
        self._phase = "idle"
        self._appear = 1.0
        self._shown_sent = False
        self.placement = None           # the next open goes by the anchor
        self._hovered = self._pressed = self._dragging = None
        self._damage = None
        self._damage_all = True
        self._relayout_queued = True
        if self.content is not None:
            try:
                self.content.detach()
            except Exception:
                log.exception("detach failed for %s", self.spec.name)
            if self.holo is not None:
                self.holo.detach()
        self._parked = True

    def unpark(self) -> None:
        """Undo `park()`: re-attach the content and re-place the window,
        in case the monitor layout changed while it was hidden."""
        if not self._parked:
            return
        self._parked = False
        if self.content is not None:
            self.content.attach(self)
            if self.holo is not None:
                self.holo.attach(self)
        # Re-anchor at the size the window has — attach() may just have
        # resized it to its content — not at the spec's size, which for a
        # content-sized window is only a placeholder.
        _cx, _cy, cw, ch = self.content_root
        cx, cy, cw, ch = self._compute_geometry(
            self.scene.primary_monitor(), size=(cw, ch))
        self._apply_geometry(cx, cy, cw, ch)
        # No layout here: park() left `_relayout_queued` set, and show()
        # lays out once before it paints. Doing it here as well cost the
        # settings panel a second 5ms pass on every open.
        self._damage_all = True

    def destroy(self) -> None:
        if self.wid is None:
            return
        self._end_hold(cancel=True)
        self._ungrab("pointer")
        self._ungrab("keyboard")
        self._stop_clock()
        self.scene.remove(self)
        self.surface = self._frozen = self._out = None
        self._small_in = self._small_out = None
        self._picture = None
        self._blur_on, self._blur_rects = False, ()
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
    # ── painting ────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas, w: int, h: int,
              dirty: "skia.Region | None" = None) -> None:
        """Window chrome, then content — all inside the content rect.

        The bleed margin is cleared to transparent and left alone; only
        an effect may draw there.

        `dirty` restricts the repaint to a region. `self.surface` persists
        between frames, so everything outside it is still the last frame's
        pixels and is left untouched.

        The region is painted one rectangle at a time rather than through
        a single `clipRegion`. A multi-rect clip is not a fast path — the
        GPU backend builds a clip mask for it, and measured on this bar
        that cost more than the pixels it saved: 15.7% of a core against
        10.1% for a plain rect clip over a larger area. A handful of
        rectangular clips gets both, because containers cull to the clip
        so each extra pass walks almost nothing.
        """
        if dirty is None:
            self._paint_region(canvas, w, h, None)
            return
        it = skia.Region.Iterator(dirty)
        while not it.done():
            self._paint_region(canvas, w, h, skia.Rect(it.rect()))
            it.next()

    def _paint_region(self, canvas: skia.Canvas, w: int, h: int,
                      clip: "skia.Rect | None") -> None:
        spec = self.spec
        saved = canvas.save()
        try:
            if clip is not None:
                canvas.clipRect(clip)
            canvas.clear(skia.ColorSetARGB(0, 0, 0, 0))
            bg = skia.Paint(AntiAlias=False)
            bg.setColor(theme.color(spec.background))
            bg.setBlendMode(skia.BlendMode.kSrc)
            canvas.drawRect(self.content_rect, bg)
            if spec.rule:
                self._paint_rule(canvas, w, h)
            if self.content is not None:
                self.content.paint(canvas)
                if self.holo is not None:
                    self._paint_holo(canvas)
        except Exception:
            # One widget that cannot paint costs what would have been
            # drawn after it here, not the frame: the rest is presented,
            # and the window still opens.
            self._complain("paint")
        finally:
            # Whatever it left on the stack: the next region must not
            # inherit its clip.
            canvas.restoreToCount(saved)

    def _paint_holo(self, canvas: skia.Canvas) -> None:
        """`WindowSpec.holo`: the raster over what the content just
        painted, then the scan over both. `SrcATop` puts the lines only
        where there are pixels to project onto — the plates, not the
        transparent gaps or the bleed margin. Over the text too, which
        at the raster's 7% reads as the same grain, not as a change in
        the type."""
        lines = skia.Paint(Shader=raster(theme.POPUP_RASTER))
        lines.setBlendMode(skia.BlendMode.kSrcATop)
        canvas.drawRect(self.content_rect, lines)
        self.holo.paint(canvas)

    def _paint_rule(self, canvas: skia.Canvas, w: int, h: int) -> None:
        spec = self.spec
        t = spec.rule_thick
        a = spec.anchor
        r = self.content_rect
        ox, oy, w, h = r.left(), r.top(), r.width(), r.height()
        # The rule sits on the edge facing the screen interior — the
        # opposite side from whatever we're anchored to.
        if Anchor.BOTTOM in a and Anchor.TOP not in a:
            rect = skia.Rect.MakeXYWH(ox, oy, w, t)
        elif Anchor.TOP in a and Anchor.BOTTOM not in a:
            rect = skia.Rect.MakeXYWH(ox, oy + h - t, w, t)
        elif Anchor.LEFT in a and Anchor.RIGHT not in a:
            rect = skia.Rect.MakeXYWH(ox + w - t, oy, t, h)
        elif Anchor.RIGHT in a and Anchor.LEFT not in a:
            rect = skia.Rect.MakeXYWH(ox, oy, t, h)
        else:
            rect = skia.Rect.MakeXYWH(ox, oy, w, t)

        if spec.rule_glow > 0:
            glow = skia.Paint(AntiAlias=True)
            glow.setColor(theme.color(spec.rule, alpha=0.55))
            glow.setMaskFilter(skia.MaskFilter.MakeBlur(
                skia.kNormal_BlurStyle, spec.rule_glow))
            canvas.drawRect(rect, glow)

        crisp = skia.Paint(AntiAlias=False)
        crisp.setColor(theme.color(spec.rule))
        canvas.drawRect(rect, crisp)

    def redraw(self) -> None:
        """Paint what is dirty and hand it to the canvas. Frames call
        this; nothing else should, or it paints widgets no tick has
        looked at — see `_tick_widgets`."""
        t0 = time.perf_counter()
        self._painted = 0
        self._redraw()
        if self._painted:
            ms = (time.perf_counter() - t0) * 1000
            self.scene.stats.paint(self.spec.name, ms)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("%s painted in %.2f ms (%d px)", self.spec.name, ms,
                          self._painted)

    def _redraw(self) -> None:
        if self.surface is None or self.wid is None or not self.mapped:
            return

        # Nothing to present: every frame comes through here, and one
        # with no damage must not snapshot, blit and *swap* — a second
        # swap in a frame is what turns a 30fps clock into a visibly
        # uneven one.
        if not (self._damage_all
                or (self._damage is not None and not self._damage.isEmpty())
                or self._frozen is not None
                or self._phase in ("enter", "exit")
                or any(e.active(self._appear) for e in self.effects)):
            return

        if self.gl is not None:
            self.gl.make_current()

        if self._frozen is not None:
            frame = self._frozen
        else:
            if self._relayout_queued:
                self._relayout_queued = False
                self._layout_content()
                self._damage_all = True     # layout can move anything
            dirty = self._take_damage()
            if dirty is not None and not dirty.isEmpty():
                # No submit of its own: the canvas's present submits the
                # one context's work, this paint with it (a submit per
                # window per frame cost 0.8 ms on the GPU for nothing).
                self.paint(self.surface.getCanvas(), self.width, self.height,
                           dirty)
            frame = self.surface.makeImageSnapshot()

        passes = [e for e in self.effects if e.active(self._appear)]
        if not passes:
            self._show_frame(frame)
            return

        # kDecal, not kClamp: effects that displace their sample (tear,
        # chromatic split) must read transparent past the window edge. With
        # kClamp every out-of-range pixel repeats the same edge column, and
        # a hard border smears into solid bars.
        #
        # effect_scale exists to make the per-pixel pass affordable on the
        # CPU. On the GPU it is free, so run at full resolution and keep
        # the detail the downscale was throwing away.
        scale = 1.0 if self.gl is not None else \
            max(0.1, min(1.0, self.spec.effect_scale))
        ew = max(1, int(self.width * scale))
        eh = max(1, int(self.height * scale))

        if scale < 1.0:
            # Downscale the content once, run the chain over the smaller
            # surface, then upscale the result on the way out.
            small = self._scratch("_small_in", ew, eh)
            sc = small.getCanvas()
            sc.clear(skia.ColorSetARGB(0, 0, 0, 0))
            sc.save()
            sc.scale(scale, scale)
            sc.drawImage(frame, 0, 0, self._sampling)
            sc.restore()
            small.flushAndSubmit()
            source = small.makeImageSnapshot()
        else:
            source = frame

        shader = source.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal,
                                   self._sampling)
        for effect in passes:
            shader = effect.shader(shader, ew, eh,
                                   t=self._t, appear=self._appear,
                                   seed=self._seed)
        paint = skia.Paint(Shader=shader)
        # kSrc, not the default SrcOver: compositing each post-processed
        # frame onto the previous one accumulates ghosts and saturates
        # alpha towards opaque within a few frames.
        paint.setBlendMode(skia.BlendMode.kSrc)

        if scale < 1.0:
            staged = self._scratch("_small_out", ew, eh)
            staged.getCanvas().drawPaint(paint)
            staged.flushAndSubmit()
            out = self._out_surface()
            oc = out.getCanvas()
            oc.clear(skia.ColorSetARGB(0, 0, 0, 0))
            oc.save()
            oc.scale(1.0 / scale, 1.0 / scale)
            oc.drawImage(staged.makeImageSnapshot(), 0, 0, self._sampling)
            oc.restore()
        else:
            out = self._out_surface()
            out.getCanvas().drawPaint(paint)
        self._show_out(out)

    def _show_frame(self, frame: skia.Image) -> None:
        """Hand the canvas a frame no pass touched (`redraw`): the
        surface itself, damaged where the paint was, or a frozen frame
        whole."""
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

    def _show_out(self, out: skia.Surface) -> None:
        """Hand the canvas the effect chain's output, `_out_surface()`."""
        self._dirty_local = None
        self._picture = out
        self._painted = self.width * self.height
        self.scene.update(self, None)

    def picture(self) -> skia.Image | None:
        """What the canvas shows of this window now."""
        p = self._picture
        if p is None:
            return None
        if isinstance(p, skia.Surface):
            return p.makeImageSnapshot()
        return p

    def _make_surface(self, w: int, h: int) -> skia.Surface:
        """An offscreen surface on whichever backend the canvas uses."""
        if self.gl is not None:
            return self.gl.offscreen(w, h)
        return skia.Surface(w, h)

    def _scratch(self, attr: str, w: int, h: int) -> skia.Surface:
        surface = getattr(self, attr, None)
        if surface is None or surface.width() != w or surface.height() != h:
            surface = self._make_surface(w, h)
            setattr(self, attr, surface)
        return surface

    def _out_surface(self) -> skia.Surface:
        if self._out is None or self._out.width() != self.width \
                or self._out.height() != self.height:
            self._out = self._make_surface(self.width, self.height)
        return self._out
    # ── damage ──────────────────────────────────────────────────────────
    def damage(self, widget=None, *, layout: bool = False) -> None:
        """Mark a region dirty. Coalesced — a burst of widget changes in
        one event costs a single frame, not one repaint each.

        `widget=None` (or a relayout, which can move anything) dirties the
        whole window. Otherwise only that widget's `paint_bounds()` is
        repainted; everything else is still valid in `self.surface`, which
        persists between frames.
        """
        self._mark(widget, layout=layout)
        self._damaged()

    def damage_rect(self, rect: skia.Rect) -> None:
        """Mark a bare rectangle dirty: somewhere a widget used to be,
        which no widget's `paint_bounds()` covers any more."""
        self._mark_box(rect.roundOut())
        self._damaged()

    def invalidate(self, layout: bool = False) -> None:
        """Whole-window repaint. Kept for callers that have no widget to
        attribute the change to."""
        self.damage(None, layout=layout)

    def _mark(self, widget=None, *, layout: bool = False) -> None:
        if layout or widget is None:
            self._relayout_queued = self._relayout_queued or layout
            self._damage_all = True
        else:
            # roundOut, not round: a widget on a half-pixel boundary must
            # own the whole pixel it tints, or it leaves a seam behind.
            self._mark_box(widget.paint_bounds().roundOut())

    def _mark_box(self, box: skia.IRect) -> None:
        if self._damage_all:
            return
        if self._damage is None:
            self._damage = skia.Region()
            self._damage.setRect(box)
        else:
            self._damage.op(box, skia.Region.kUnion_Op)

    def _has_damage(self) -> bool:
        return (self._damage_all or self._relayout_queued
                or (self._damage is not None and not self._damage.isEmpty()))

    def _damaged(self) -> None:
        """Get the new damage painted: by the clock's next tick if that
        is soon, in a frame of its own otherwise.

        While the clock runs fast its next tick is at most 1/20s away,
        and a frame per change on top of it only doubles the frames —
        the call overlay streaming a reply was painting ~70 times a
        second that way, every arriving chunk plus every 30fps tick.
        Below COALESCE_FPS (a 2fps meter, a stopped clock) the wait would
        be visible. That frame is also what starts a stopped clock or
        speeds up a slow one: it ticks the widgets, so one that has just
        begun to animate — and invalidated, as the contract asks — is
        seen at once rather than at the slow clock's next tick.
        """
        if self._ticking or self._clock_fps >= COALESCE_FPS:
            return
        self._frame_soon()

    def _take_damage(self) -> "skia.Region | None":
        """The region to repaint this frame, or None if nothing is dirty;
        kept as what the frame hands the canvas (`_show_frame`)."""
        if self._damage_all:
            self._damage_all = False
            self._damage = None
            region = skia.Region()
            region.setRect(skia.IRect.MakeWH(self.width, self.height))
        else:
            region, self._damage = self._damage, None
        self._dirty_local = skia.Region(region) if region is not None else None
        return region

    # ── frames ──────────────────────────────────────────────────────────
    # Everything that paints the live widget tree is a frame: step the
    # spawn or despawn, tick the widgets, paint what is dirty. The clock
    # runs frames on a fixed grid while anything animates; damage that
    # cannot wait for it gets a frame of its own in between. One path, so
    # the widgets are always ticked before they are painted — which is
    # what lets `_tick_widgets` promise that nothing is left on screen
    # mid-motion.
    def _frame_soon(self) -> None:
        if self._frame_queued or self.loop is None:
            return
        self._frame_queued = True
        self.loop.call_soon(self._frame_now)

    def _frame_now(self) -> None:
        """A frame off the clock's grid."""
        self._frame_queued = False
        if self.mapped and self._frame():
            self._pace()

    def _on_frame(self) -> None:
        """The clock's tick."""
        self._frame_handle = None
        if not self.mapped:
            self._clock_fps = 0
            return
        if self._frame():
            self._pace()

    def _frame(self) -> bool:
        """One frame. False if the window finished its despawn in it.

        A widget that raises must not take the clock with it. The tick
        would never be rescheduled, and with `_clock_fps` still saying
        the clock runs, every later change would wait for a tick that
        never comes: a window frozen until it is closed.
        """
        try:
            return self._run_frame()
        except Exception:
            self._complain("a frame")
            return self._phase != "gone"

    def _run_frame(self) -> bool:
        self._t = time.monotonic() - self._t0

        if self._phase == "enter":
            self._appear = min(1.0, self._advance_phase() / self._transition)
            if self._appear >= 1.0:
                self._phase = "idle"
                # Hand the window back to the live widget tree. Anything
                # that landed while frozen — an async GPU sample, a
                # resolved optimus-manager mode — repainted into the
                # still image and was swallowed, so re-measure as well.
                self._frozen = None
                self._relayout_queued = True
                self._set_blur(True)
                # Deferred until here so the loop was free for the whole
                # animation — see Widget.on_shown.
                self._notify_shown()
        elif self._phase == "exit":
            self._appear = max(
                0.0, 1.0 - self._advance_phase() / self._transition)
            if self._appear <= 0.0:
                self.redraw()          # final, fully dissolved frame
                self._stop_clock()
                self._finish_dismiss()
                return False

        if self._frozen is None and self.content is not None:
            self._tick_widgets()
        # Only presents if something changed. A window whose widgets are
        # all quiet still ticks — cheaply — until its demand drops to
        # zero and the clock stops on its own.
        self.redraw()
        return True

    def _begin_phase(self, phase: str) -> None:
        self._phase = phase
        self._phase_elapsed = 0.0
        self._phase_at = None

    def _advance_phase(self) -> float:
        """Seconds of the running spawn or despawn played so far, this
        frame included.

        Counted in frames' own time, not from a start stamp. The first
        frame is one interval in, whenever it comes — so whatever stood
        between showing the window and its first tick (a grab's round
        trip, another window's heavy frame, a busy compositor) costs the
        effect nothing. After that a frame adds the real time since the
        last one, so the effect lasts what it says on a healthy loop, but
        never more than MAX_PHASE_STEP intervals: a stall holds the
        effect where it was. Read off the wall clock instead, a spawn
        whose loop was blocked for 150ms came back four-fifths played —
        the window was transparent until then, so it was seen to pop in
        with the last flicker of its effect.
        """
        now = time.monotonic()
        interval = self._frame_interval or 1.0 / 60
        step = interval if self._phase_at is None else now - self._phase_at
        self._phase_at = now
        self._phase_elapsed += min(step, MAX_PHASE_STEP * interval)
        return self._phase_elapsed

    def _widgets(self):
        """Every widget the frame clock ticks: the content's tree, then
        the window's own scan on a `holo` window."""
        yield from self.content.walk()
        if self.holo is not None:
            yield self.holo

    def _census(self) -> None:
        """For the first paint of an open, which no tick comes before:
        note which widgets it will catch moving (see `_tick_widgets`),
        and that none of them has a previous frame."""
        demand = 0
        if self.content is not None:
            for widget in self._widgets():
                widget._last_animate_t = None
                fps = self._fps_of(widget)
                if fps > 0:
                    widget._animating = True
                    demand = max(demand, fps)
        self._widget_demand = demand

    def _tick_widgets(self) -> None:
        """Call `animate` on each widget at *its* rate, not the window's.

        The clock runs at the fastest rate anything asked for, so without
        this a meter wanting 2fps was animated 30 times a second — its
        declared rate did nothing but pin the clock. Each widget carries
        its own due time, so the schedules stay independent.

        Due times are `time.monotonic()`, not seconds since the window
        opened. The widgets outlive the window's opens — a parked panel
        keeps its tree, and so does a spec whose window is rebuilt — and
        a due time counted from the open was still "minute five" when the
        next open began again at zero. Nothing was due until the new open
        had lasted as long as the old one: no spinner, no caret, nothing
        that moves on its own, only what input happened to repaint.

        **The settle frame.** A widget whose `animation_fps` has fallen
        to 0 since it was last looked at gets one more `animate()` and a
        repaint of everything it may have touched. Its last frame before
        that was painted while it was still moving — a comet two-thirds
        round, text half decoded — and with no further frame that is
        what would stay on screen. The rate is read here, before the
        paint, so an effect that is over by this reading is drawn over;
        one that ends between two ticks, or whose last frames a stall
        swallowed, is caught at the next. Widgets therefore never keep
        themselves alive past the end of an effect to clean up after it.
        """
        t, now = self._t, time.monotonic()
        demand = 0
        self._ticking = True
        try:
            for widget in self._widgets():
                fps = self._fps_of(widget)
                if fps <= 0:
                    if widget._animating:
                        widget._animating = False
                        self._animate(widget, t)
                    # Idle. Forget its last frame so `tick_dt` reports
                    # zero when it next animates, as its contract says.
                    # Left set, the gap since it went idle came back as
                    # one full MAX_TICK_DT of elapsed time on its first
                    # frame — which skipped the first ~45% of a 0.55s
                    # lyric reveal whenever no beat had kept the widget
                    # ticking in between.
                    widget._last_animate_t = None
                    continue
                demand = max(demand, fps)
                widget._animating = True
                interval = 1.0 / fps
                if interval < widget._animate_every:
                    # It sped up. Its slot was set on the slower grid —
                    # up to a second away for a 1fps widget — and an
                    # effect shorter than that would be over before its
                    # first frame.
                    widget._animate_due = min(widget._animate_due, now)
                widget._animate_every = interval
                # Half an interval of tolerance. `now` is when the
                # callback ran, which is always a little *after* the slot
                # it was scheduled for, so a widget wanting exactly the
                # clock rate is never quite due on the frame that should
                # carry it — it slips to the next one, and every other
                # frame gets dropped. Asking for 30 got 19.
                if now + interval * 0.5 < widget._animate_due:
                    continue
                # Advance on the widget's own grid so the error cannot
                # accumulate, but never behind `now` — a widget that was
                # idle for a while must not then fire a burst catching up.
                widget._animate_due = max(widget._animate_due + interval, now)
                self._animate(widget, t)
        finally:
            self._ticking = False
        self._widget_demand = demand

    def _fps_of(self, widget: Widget) -> int:
        try:
            return widget.animation_fps
        except Exception:
            self._complain(f"animation_fps of {type(widget).__name__}")
            return 0

    def _animate(self, widget: Widget, t: float) -> None:
        try:
            widget.animate(t)
        except Exception:
            self._complain(f"animate() of {type(widget).__name__}")
        # Assume an animated frame changed something. A widget could
        # report this precisely instead, but then one that forgot would
        # silently stop updating — a failure that looks like a frozen
        # clock, not like a bug. Being conservative here costs a repaint
        # of that widget's own rect and keeps the contract for new
        # widgets down to "declare a rate".
        self._mark(widget)

    def _complain(self, what: str) -> None:
        """Log the exception being handled — once. A frame that fails
        does so again 30 times a second."""
        key = (what, repr(sys.exc_info()[1]))
        if key in self._complained:
            return
        if len(self._complained) > 64:
            self._complained.clear()
        self._complained.add(key)
        log.exception("%s failed in %s (reported once)", what, self.spec.name)

    # ── frame clock ─────────────────────────────────────────────────────
    def _effect_fps(self) -> int:
        """What the window's own passes want: the transition's rate while
        one plays, the continuous ones' otherwise."""
        playing = self._phase in ("enter", "exit")
        return max((e.fps for e in self.effects
                    if e.is_transition == playing), default=0)

    def _demand(self) -> int:
        """Fastest rate anything in this window wants right now. The
        widgets' is what the last tick found; a transition plays over a
        still image, so they want nothing during one."""
        widgets = self._widget_demand if self._frozen is None else 0
        return max(widgets, self._effect_fps())

    def _pace(self) -> None:
        """Run the clock at the rate the window wants now: start it,
        change its rate, or let it stop."""
        fps = self._demand()
        if fps <= 0:
            if self._clock_fps:
                log.debug("%s idle, clock stopped", self.spec.name)
            self._stop_clock()
            # Damage raised after this frame's paint was counting on the
            # tick that now is not coming.
            if self._has_damage():
                self._frame_soon()
            return
        if fps != self._clock_fps:
            log.debug("%s frame clock %d -> %d fps (%s)", self.spec.name,
                      self._clock_fps, fps, self._phase)
            if fps > self._clock_fps:
                # Starting, or faster: the next tick is one new interval
                # from now, not at the end of a slot on the slower grid.
                if self._frame_handle is not None:
                    self._frame_handle.cancel()
                    self._frame_handle = None
                self._next_frame_at = time.monotonic()
            # Slower keeps `_next_frame_at` on its grid, so a widget
            # going quiet cannot make the next frame land early and
            # stutter whatever is still moving.
            self._clock_fps = fps
            self._frame_interval = 1.0 / fps
        if self._frame_handle is None:
            self._schedule_frame()

    def _stop_clock(self) -> None:
        if self._frame_handle is not None:
            self._frame_handle.cancel()
            self._frame_handle = None
        self._clock_fps = 0

    def _schedule_frame(self) -> None:
        """Aim at the next slot on a fixed grid, not `interval` from now.

        `_schedule_frame` is called at the end of a frame, so a plain
        `call_later(interval)` makes the period *work + interval* rather
        than `interval`. At 15.7ms of frame work against a 16.7ms budget
        that halves the rate — 34fps where 60 was asked for. That is the
        whole of the "not fluent" feel; it is a scheduling bug, not a
        shader one.
        """
        if self.loop is None:
            return
        now = time.monotonic()
        self._next_frame_at += self._frame_interval
        if self._next_frame_at <= now:
            # A frame overran. Skip the slots we missed instead of firing
            # a burst of zero-delay callbacks that can never catch up and
            # would starve the loop of everything else.
            behind = now - self._next_frame_at
            self._next_frame_at += (
                int(behind / self._frame_interval) + 1) * self._frame_interval
        self._frame_handle = self.loop.call_later(
            self._next_frame_at - now, self._on_frame)

    # ── the keyboard, from the canvas's KeyboardRouter ──────────────────
    def key_press(self, key: KeyEvent) -> None:
        if not self._suspended:
            self._on_key(key)

    def key_release(self, key: KeyEvent) -> None:
        if not self._suspended:
            self._on_key_release(key)

    # ── the pointer, from the canvas's PointerRouter ────────────────────
    def pointer_motion(self, x: float, y: float) -> None:
        if not self._suspended:
            self._motion(x, y)

    def pointer_leave(self) -> None:
        self._set_hover(None)

    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None:
        if self._suspended:
            return
        if pressed:
            self._on_press(button, x, y)
        else:
            self._on_release(button, x, y)

    def pointer_lost(self) -> None:
        """The press's release will not come (the compositor broke the
        pointer's implicit grab): let go of everything it started."""
        if self._held is not None:
            log.info("%s: the press's grab was broken; its hold let go", self.spec.name)
            self._end_hold(cancel=True)
        if self._dragging is not None:
            self._deliver_drag(*self._drag_xy, commit=True)
            self._dragging = None
        self._unpress()

    # ── events ──────────────────────────────────────────────────────────
    def _motion(self, x: float, y: float) -> None:
        """The pointer moved to (x, y), window coordinates."""
        # A drag owns the pointer until release: motion goes to the
        # captured widget wherever the cursor is, and hover stops
        # tracking, or dragging a slider past its own edge would
        # both drop the drag and light up whatever is underneath.
        if self._held is not None:
            self._held_motion(x, y)
        elif self._dragging is not None:
            self._deliver_drag(x, y, commit=False)
        else:
            self._set_hover(self._pick(x, y))
            self._hover_motion(x, y)

    def _dismissing(self) -> bool:
        # Keys are dropped only while dismissing: the exit pass runs over
        # a still image and the widget tree may already belong to a
        # freshly opened window. The *enter* pass is frozen too, but its
        # tree is ours, so keys are delivered — a launcher opened by a
        # keybinding is typed into at once, and dropping the first 0.2s
        # of keys loses letters. What they change is painted when the
        # entrance unfreezes, which re-lays out and repaints everything.
        return self._phase in ("exit", "gone")

    def _on_key(self, key: KeyEvent) -> None:
        if self._held_keys:
            self._held_key(key)
            return
        if not self.spec.grab_keyboard:
            # Only a held press brings keys to a window that does not take
            # the keyboard (`hold_keyboard`); one typed as it ended is
            # late, and must not reach the Escape below, which would close
            # the window (the top bar).
            log.debug("%s: a key after its hold ended, dropped", self.spec.name)
            return
        if self.content is None or self._dismissing():
            return
        if any(_chord_is(key, chord) for chord in self.spec.close_keys):
            self._request_close()
            return
        try:
            if not self.content.key(key):
                # Escape always closes a window holding the keyboard, even
                # if nothing in the tree wanted it — otherwise a keyboard
                # held with no exit is a locked session.
                if key.keysym == _ESCAPE:
                    self._request_close()
        except Exception:
            log.exception("key handler failed in %s", self.spec.name)

    def _on_key_release(self, key: KeyEvent) -> None:
        if self._held_keys or not self.spec.grab_keyboard:
            return              # a held press's keyboard: releases are nobody's
        if self.content is None or self._dismissing():
            return
        try:
            self.content.key_release(key)
        except Exception:
            log.exception("key release handler failed in %s", self.spec.name)

    def _pick(self, x: float, y: float) -> Widget | None:
        # A dismissing window is a still image on its way out; its widget
        # tree may already belong to a freshly opened window.
        if self.content is None or self._frozen is not None:
            return None
        return self.content.hit(x, y)

    def _set_hover(self, widget: Widget | None) -> None:
        if widget is self._hovered:
            return
        if self._hovered is not None:
            self._hovered.set_hovered(False)
        self._hovered = widget
        if widget is not None:
            widget.set_hovered(True)

    def _on_press(self, button: int, x: float, y: float) -> None:
        if self._held is not None:
            # Another button while a press is held: the held one decides.
            return
        if (self.spec.dismiss_on_outside_click and self.spec.blur
                and self._phase not in ("exit", "gone")):
            # A window that blurs behind its plates is only as big as they
            # are: a click in the rest of its rect is outside it.
            shape_of = getattr(self.content, "blur_shape", None)
            if shape_of is not None and not shape_of().contains(x, y):
                log.debug("%s dismissed by outside click", self.spec.name)
                self._request_close()
                return

        widget = self._pick(x, y)
        if widget is None:
            # A click that hit nothing in a window that stays open still
            # dismisses the panels listening for outside clicks: it landed
            # on a window of ours, so the canvas did not count it as one.
            self._dismiss_others()
            return
        # Scroll arrives as buttons 4-7, not as a separate event type.
        if button in (1, 2, 3):
            self._pressed = widget
            widget.pressed = True
            widget.invalidate()
            if self._offer_press(widget, button, x, y):
                return
        if button == 1 and widget.on_drag is not None:
            self._dragging = widget
            self._deliver_drag(x, y, commit=False)
            return          # a drag press is not also a click
        log.debug("button %d on %s", button, type(widget).__name__)
        try:
            widget.click(button, x, y)
        except Exception:
            log.exception("click handler failed on %s", type(widget).__name__)

    def _request_close(self) -> None:
        from .core.daemon import get_daemon
        get_daemon().close(self.spec.name)

    def _dismiss_others(self) -> None:
        if self.spec.dismiss_on_outside_click:
            return
        from .core.daemon import get_daemon
        get_daemon().dismiss_others(self.spec.name)

    def _deliver_drag(self, x: float, y: float, *, commit: bool) -> None:
        widget = self._dragging
        if widget is None or widget.on_drag is None:
            return
        self._drag_xy = (x, y)
        try:
            widget.on_drag(widget, x, y, commit)
        except Exception:
            log.exception("drag handler failed in %s", self.spec.name)

    def _on_release(self, button: int, x: float, y: float) -> None:
        if self._held is not None:
            if button != self._held_button:
                return          # another button's, while the held one is down
            # Let go of everything first: the widget's answer may fail.
            widget = self._end_hold()
            try:
                widget.release(button, x, y)
            except Exception:
                log.exception("release handler failed on %s", type(widget).__name__)
            self._unpress()
            return
        if self._dragging is not None:
            # The commit pass is what lets a control write once at the
            # end of a drag instead of firing a subprocess per motion
            # event — `brightnessctl` at 60 Hz would fork a hundred
            # processes crossing one slider.
            self._deliver_drag(*self._drag_xy, commit=True)
            self._dragging = None
        self._unpress()

    def _unpress(self) -> None:
        if self._pressed is not None:
            self._pressed.pressed = False
            self._pressed.invalidate()
            self._pressed = None

    # ── a held press (`Widget.press`) ───────────────────────────────────
    # From the press to its button's release the widget gets the motion
    # and the release wherever the pointer goes: the compositor keeps the
    # pointer for the surface a press went to until every button is up
    # (the implicit grab), and the canvas's PointerRouter sends that
    # surface's motion and release to the window the press went to. If
    # the compositor breaks it, `pointer_lost` lets go. The keyboard is
    # taken only when the widget asks (`hold_keyboard`, for Escape), and
    # given back on every way out of the hold: its release, Escape, the
    # grab broken, the widget leaving the window, an exception in its
    # handlers, the window parked, dismissed or destroyed.
    def _offer_press(self, widget: Widget, button: int, x: float, y: float) -> bool:
        """Whether `widget` holds the press (no click then)."""
        try:
            taken = widget.press(button, x, y)
        except Exception:
            log.exception("press handler failed on %s", type(widget).__name__)
            return True         # neither a hold nor a click after a half-done press
        if not taken:
            return False
        self._held, self._held_button = widget, button
        log.debug("button %d held by %s", button, type(widget).__name__)
        # As a press on the bar's empty part always did: open panels go.
        self._dismiss_others()
        return True

    def hold_keyboard(self, widget: Widget) -> bool:
        """Take the keyboard while `widget` holds a press, so that Escape
        can cancel it (`Widget.cancel`); the other keys typed meanwhile go
        to its `key` and no further. Taken on top of whoever has it, and
        given back when the hold ends, however it ends. False without a
        keyboard on the canvas: the hold goes on without Escape."""
        if widget is not self._held or self.wid is None:
            return False
        if self._held_keys:
            return True
        if not self.scene.keyboard.available:
            return False
        self.scene.keyboard.take(("hold", self), self)
        self._held_keys = True
        return True

    def release_hold(self, widget: Widget) -> None:
        """`widget` lets go of the press it holds before its release
        (its state went out from under it: the tab it drags closed)."""
        if widget is self._held:
            self._end_hold()

    @property
    def holding(self) -> Widget | None:
        return self._held

    def _end_hold(self, *, cancel: bool = False) -> Widget | None:
        """The hold over: the keyboard given back; with `cancel` the
        widget told (`Widget.cancel`). The widget that held it, or None."""
        widget, self._held = self._held, None
        self._held_button = 0
        if self._held_keys:
            self._held_keys = False
            self.scene.keyboard.give_back(("hold", self))
        if cancel and widget is not None:
            log.debug("%s's held press cancelled", type(widget).__name__)
            try:
                widget.cancel()
            except Exception:
                log.exception("cancel handler failed on %s", type(widget).__name__)
        return widget

    def _held_motion(self, x: float, y: float) -> None:
        widget = self._held
        if widget.window is not self:   # taken out of this window's tree meanwhile
            self._end_hold(cancel=True)
            return
        try:
            widget.motion(x, y)
        except Exception:
            log.exception("motion handler failed on %s", type(widget).__name__)
            self._end_hold(cancel=True)

    def _held_key(self, key: KeyEvent) -> None:
        widget = self._held
        if widget is None:
            return
        if key.keysym == _ESCAPE:
            self._end_hold(cancel=True)
            return
        try:
            widget.key(key)
        except Exception:
            log.exception("key handler failed on %s", type(widget).__name__)
            self._end_hold(cancel=True)

    def _hover_motion(self, x: float, y: float) -> None:
        widget = self._hovered
        if widget is None:
            return
        try:
            widget.motion(x, y)
        except Exception:
            log.exception("motion handler failed on %s", type(widget).__name__)
