"""Windows.

There is no Bar class. A bar is a `WindowSpec` whose layer is DOCK, whose
anchor is an edge, and which reserves space; a notification is the same
type with a different layer and no reservation; a chord menu is the same
type with override-redirect and a seat grab. The differences are X11
properties, not Python subclasses.

The vocabulary is borrowed from Wayland's layer-shell — anchor, layer,
exclusive zone — because it is the clearest description of what these
windows are, and it maps onto EWMH without loss.
"""

import asyncio
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto

import skia
import xcffib.shape

from . import theme
from .backend.gl import backend as gl_backend
from .backend.x11 import Display, Monitor, keysym_text, shape_ext, xp
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
# A refused pointer or keyboard grab is tried again this often, for this
# long — see `_grab`.
GRAB_RETRY_S = 0.01
GRAB_GIVE_UP_S = 2.0


class Layer(Enum):
    """Stacking role. Maps to _NET_WM_WINDOW_TYPE plus _NET_WM_STATE."""

    DESKTOP = "desktop"    # wallpaper level
    BOTTOM = "bottom"      # below normal windows
    NORMAL = "normal"
    DOCK = "dock"          # bars and panels, above normal windows
    OVERLAY = "overlay"    # notifications, above everything


_LAYER_TYPE = {
    Layer.DESKTOP: "_NET_WM_WINDOW_TYPE_DESKTOP",
    Layer.BOTTOM:  "_NET_WM_WINDOW_TYPE_NORMAL",
    Layer.NORMAL:  "_NET_WM_WINDOW_TYPE_NORMAL",
    Layer.DOCK:    "_NET_WM_WINDOW_TYPE_DOCK",
    Layer.OVERLAY: "_NET_WM_WINDOW_TYPE_NOTIFICATION",
}
_LAYER_STATE = {
    Layer.BOTTOM:  ("_NET_WM_STATE_BELOW",),
    Layer.DOCK:    ("_NET_WM_STATE_ABOVE",),
    Layer.OVERLAY: ("_NET_WM_STATE_ABOVE",),
}


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

    # Reserve screen space so maximised windows don't overlap us. This is
    # the single flag that turns a floating window into a bar.
    exclusive: bool = False
    # Bypass the window manager entirely — for menus and popups that must
    # appear exactly where they're put.
    override_redirect: bool = False
    focusable: bool = True
    sticky: bool = True          # visible on every desktop

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

    # Close when a click lands outside. Needs a pointer grab, so it only
    # makes sense for transient windows.
    dismiss_on_outside_click: bool = False
    # Take the keyboard while open, like a chord menu. Implies the window
    # is transient — a grab held forever locks the session.
    grab_keyboard: bool = False
    # Chords that close a grabbing window, besides Escape: its own
    # keybinding. With the keyboard held here the window manager never
    # sees the second press, so the window has to know it. Written like
    # "super+a": modifiers (super, ctrl, alt, shift), then one key.
    close_keys: tuple[str, ...] = ()

    # Ask the compositor to blur whatever is behind the window, so a
    # translucent plate reads as frosted glass. picom here only blurs
    # windows that carry _KDE_NET_WM_BLUR_BEHIND_REGION (the exclude rule
    # in its config), and it blurs the window's whole *bounding shape* —
    # for a plate that is not the window's full rectangle, the content
    # should provide `blur_shape() -> skia.Path` in window coordinates.
    # The window then cuts its SHAPE bounding region to that path, or the
    # transparent margin around the plates would show as a blurred box.
    # The cut clips drawing too, so it is only in force while the window
    # is still: the spawn and despawn run uncut and unblurred, since their
    # tear reaches past the plates into the bleed margin.
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
    # Keep the X window, its GL surface and its warmed-up GPU state
    # between opens: a close parks the window unmapped instead of
    # destroying it, and the next open maps it again. Opening then costs
    # a repaint, ~20ms, instead of ~130ms of setup that blocked the loop
    # and froze the bar. For panels that are opened repeatedly; one-shot
    # windows like toasts, and kinds whose spec is rebuilt per open, do
    # not benefit and are never parked.
    keep_alive: bool = False

    @property
    def keep_above(self) -> bool:
        """Whether we must hold ourselves on top (`Display.keep_above`).

        An override-redirect window is invisible to the window manager, so
        _NET_WM_STATE_ABOVE is ignored for it and stacking falls back to
        raw X order — whatever was raised last wins. Managed windows (the
        bar) get their layer honoured and need none of this.
        """
        return self.override_redirect and self.layer in (Layer.DOCK, Layer.OVERLAY)


@dataclass(frozen=True)
class Placement:
    """Where one open of a window goes instead of where its anchor puts
    it: the content's top edge at `y`, centred on `x` (root coordinates),
    kept inside the monitor that holds that point. Given to
    `Daemon.open`, kept while the window is open, and forgotten when it
    closes, so the next open goes by the anchor again.

    `below(x, y, w, h)` drops the window from a rect: centred under it,
    its top at the rect's bottom edge."""

    x: int
    y: int

    @classmethod
    def below(cls, x: int, y: int, w: int, h: int) -> "Placement":
        return cls(x + w // 2, y + h)


def key_event(display: Display, ev) -> KeyEvent:
    """An X key press or release as a KeyEvent, through the keymap."""
    state = ev.state
    keysym = display.keysym(ev.detail, state)
    ctrl = bool(state & xp.KeyButMask.Control)
    alt = bool(state & display.mod_mask("alt"))
    sup = bool(state & display.mod_mask("super"))
    return KeyEvent(
        keysym=keysym,
        text="" if (ctrl or alt or sup) else keysym_text(keysym),
        shift=bool(state & xp.KeyButMask.Shift),
        ctrl=ctrl, alt=alt, super=sup)


def _chord_is(key: KeyEvent, chord: str) -> bool:
    """Whether a key press is `chord` ("super+a"): exactly those
    modifiers held, and the key itself in either case."""
    *mods, name = chord.lower().split("+")
    held = {m for m in ("super", "ctrl", "alt", "shift") if getattr(key, m)}
    return (held == set(mods) and len(name) == 1
            and 0 < key.keysym < 0x100 and chr(key.keysym).lower() == name)


class Window:
    """A live X11 window with a Skia surface behind it."""

    def __init__(self, display: Display, spec: WindowSpec,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.display = display
        self.spec = spec
        self.loop = loop
        self.wid: int | None = None
        self.gc: int | None = None
        self.x = self.y = 0
        self.width = self.height = 0
        self.depth = display.screen.root_depth
        self.surface: skia.Surface | None = None
        self.mapped = False
        self.content = spec.content
        # The scan over a `holo` window: a widget the window owns, outside
        # the content tree, attached, laid out, ticked and painted with it.
        self.holo = Projection() if spec.holo else None
        # Effects that displace pixels draw past the content's edge, so the
        # X window is grown by `bleed` on every side and the content is
        # inset by it. `content_rect` is that inset area in window-local
        # coordinates; `content_root` is the same rect in root coordinates,
        # which is what struts and placement care about.
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
        self._shown_sent = False
        # Hidden between opens with its content detached — see park().
        self._parked = False
        # This open's place, when it is not the anchor's — see Placement.
        self.placement: Placement | None = None

        self.gl = None               # backend.gl.Backend, or None for raster
        self.gl_surface = None
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
        self._grabbed = False
        self._kb_grabbed = False
        # Grabs still being retried, by "pointer" / "keyboard".
        self._grab_retry: dict[str, asyncio.TimerHandle] = {}
        # Failures already logged — see `_complain`.
        self._complained: set[tuple] = set()
        # Last bounding shape sent to the server, so a relayout that
        # leaves the plates where they were costs no request.
        self._bounding: tuple | None = None
        # Whether the blur and the cut to `blur_shape()` are in force —
        # off during transitions, see `WindowSpec.blur`.
        self._blur_on = False

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
        back inside the monitor that holds the point. Only the content
        is kept inside; the bleed around it may run off the edge, as it
        does for an anchored window at the screen's edge."""
        p = self.placement
        monitors = self.display.monitors()
        mon = next((m for m in monitors if m.x <= p.x < m.x + m.width
                    and m.y <= p.y < m.y + m.height), monitors[0])
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
            self.display.primary_monitor(), size=(cw, ch)))

    def _compute_bleed(self, w: float, h: float) -> int:
        if not self.effects:
            return 0
        return int(math.ceil(max(e.bleed(w, h) for e in self.effects)))

    # ── creation ────────────────────────────────────────────────────────
    def create(self, monitor: Monitor | None = None) -> None:
        d = self.display
        conn = d.conn
        mon = monitor or d.primary_monitor()
        cx, cy, cw, ch = self._compute_geometry(mon)
        self.bleed = self._compute_bleed(cw, ch)
        b = self.bleed
        self.content_root = (cx, cy, cw, ch)
        self.content_rect = skia.Rect.MakeXYWH(b, b, cw, ch)
        self.x, self.y = cx - b, cy - b
        self.width, self.height = cw + 2 * b, ch + 2 * b

        # A 32-bit visual is what makes per-pixel alpha real; without one
        # the window is opaque whatever the compositor does. On the GPU
        # path the visual is not ours to choose — it has to be the one
        # EGL's config reports, or creating the window surface is a
        # BadMatch.
        self.gl = gl_backend(d)
        visual = d.screen.root_visual
        argb = d.argb_visual()
        if argb is not None:
            self.depth, visual = argb
        if self.gl is not None:
            visual, self.depth = self.gl.visual_id, 32

        self.wid = conn.generate_id()
        cmap = conn.generate_id()
        conn.core.CreateColormap(xp.ColormapAlloc._None, cmap, d.root, visual)

        events = (xp.EventMask.Exposure | xp.EventMask.StructureNotify)
        if self.content is not None:
            # Motion and Leave drive hover; without them a widget can
            # never learn the pointer left it and stays lit forever.
            events |= (xp.EventMask.ButtonPress | xp.EventMask.ButtonRelease
                       | xp.EventMask.PointerMotion | xp.EventMask.LeaveWindow)
        if self.spec.grab_keyboard:
            events |= xp.EventMask.KeyPress
        # BackPixel *and* BorderPixel are both required on a depth-32
        # window: without an explicit border pixel the server inherits
        # the parent's, whose depth doesn't match, and CreateWindow
        # fails with BadMatch.
        conn.core.CreateWindow(
            self.depth, self.wid, d.root,
            self.x, self.y, self.width, self.height, 0,
            xp.WindowClass.InputOutput, visual,
            xp.CW.BackPixel | xp.CW.BorderPixel | xp.CW.OverrideRedirect
            | xp.CW.EventMask | xp.CW.Colormap,
            [0, 0, int(self.spec.override_redirect), events, cmap],
        )
        self.gc = conn.generate_id()
        conn.core.CreateGC(self.gc, self.wid, 0, [])

        self._set_properties()
        self._set_input_shape()
        d.on(self.wid, self._on_event)

        if self.gl is not None:
            # EGL looks the window up over its own connection, so the
            # server must have processed CreateWindow before we ask. A
            # round-trip is the cheapest way to be sure.
            conn.core.GetInputFocus().reply()
            try:
                self.gl_surface = self.gl.create(self.wid, self.width,
                                                 self.height)
            except Exception:
                log.exception("GL surface failed for %s; using raster",
                              self.spec.name)
                self.gl_surface = None
            if self.gl_surface is None:
                self.gl = None
        self.surface = self._make_surface(self.width, self.height)
        if self.content is not None:
            self.content.attach(self)
            if self.holo is not None:
                self.holo.attach(self)
            self._layout_content()
        log.info("created %s 0x%x %dx%d+%d+%d depth %d on %s%s",
                 self.spec.name, self.wid, self.width, self.height,
                 self.x, self.y, self.depth, mon.name or "screen",
                 f" (+{self.bleed}px bleed)" if self.bleed else "")

    def _layout_content(self) -> None:
        if self.content is None:
            return
        inner = self.spec.padding.deflate(self.content_rect)
        self.content.measure(Size(inner.width(), inner.height()))
        self.content.arrange(inner)
        if self.holo is not None:
            self.holo.arrange(self.content.rect)
        self._set_bounding_shape()

    def _set_bounding_shape(self) -> None:
        """Cut the window to its content's `blur_shape()` — see
        `WindowSpec.blur`. Runs after every layout, since that is when
        the plates move; a shape that comes out the same is not resent.

        The path is rasterised into a `skia.Region` (aliased, so it
        covers whole pixels the way SHAPE does) and sent as its
        rectangles: a 45° bevel becomes one thin rect per pixel row,
        a few dozen per plate.
        """
        if not self.spec.blur or self.wid is None or self.content is None:
            return
        shape_of = getattr(self.content, "blur_shape", None)
        path = shape_of() if (shape_of is not None and self._blur_on) \
            else None
        ext = shape_ext(self.display.conn)
        if ext is None:
            return
        if path is None:
            rects: tuple = ((0, 0, self.width, self.height),)
        else:
            region = skia.Region()
            region.setPath(path, skia.Region(
                skia.IRect.MakeWH(self.width, self.height)))
            out = []
            it = skia.Region.Iterator(region)
            while not it.done():
                r = it.rect()
                out.append((r.left(), r.top(), r.width(), r.height()))
                it.next()
            rects = tuple(out) or ((0, 0, 1, 1),)
        if rects == self._bounding:
            return
        self._bounding = rects
        xrects = [xp.RECTANGLE.synthetic(*r) for r in rects]
        ext.Rectangles(
            xcffib.shape.SO.Set, xcffib.shape.SK.Bounding,
            xp.ClipOrdering.Unsorted, self.wid, 0, 0, len(xrects), xrects)
        self.display.flush()

    def _set_blur(self, on: bool) -> None:
        """Put the blur and the cut into force, or lift both.

        picom only tests that _KDE_NET_WM_BLUR_BEHIND_REGION exists — and
        a property with zero items does not count, so the empty region
        KWin reads as "whole window" would leave picom's opt-in rule
        excluding us. One rect (x, y, w, h) spanning the window satisfies
        both. picom re-reads the rule when the property changes, so this
        can follow the transitions. Ordered so the blur never covers the
        uncut window: cut, then blur on; blur off, then uncut.
        """
        if not self.spec.blur or self.wid is None or on == self._blur_on:
            return
        d = self.display
        atom = "_KDE_NET_WM_BLUR_BEHIND_REGION"
        self._blur_on = on
        if on:
            self._set_bounding_shape()
            d.set_prop(self.wid, atom, xp.Atom.CARDINAL,
                       [0, 0, self.width, self.height])
        else:
            d.conn.core.DeleteProperty(self.wid, d.atom(atom))
            self._set_bounding_shape()
        d.flush()

    def reshape(self) -> None:
        """Re-cut the window to its content's `blur_shape()` after the
        content moved its pieces itself, without a window relayout — the
        call overlay, which re-arranges its plates inside a window that
        keeps its size."""
        self._set_bounding_shape()

    def _set_properties(self) -> None:
        d, spec, wid = self.display, self.spec, self.wid
        assert wid is not None

        d.set_text_prop(wid, xp.Atom.WM_NAME, xp.Atom.STRING, spec.name)
        d.set_text_prop(wid, d.atom("_NET_WM_NAME"), d.atom("UTF8_STRING"),
                        spec.name)
        # Window-manager rules match on WM_CLASS, so the instance is the
        # window's own name and the class is the app.
        d.set_wm_class(wid, spec.name, _wm_class_name())
        d.set_prop(wid, "_NET_WM_PID", xp.Atom.CARDINAL, [os.getpid()])

        d.set_prop(wid, "_NET_WM_WINDOW_TYPE", xp.Atom.ATOM,
                   [d.atom(_LAYER_TYPE[spec.layer])])

        states = list(_LAYER_STATE.get(spec.layer, ()))
        if spec.sticky:
            states.append("_NET_WM_STATE_STICKY")
        if not spec.focusable:
            states += ["_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"]
        if states:
            d.set_prop(wid, "_NET_WM_STATE", xp.Atom.ATOM,
                       [d.atom(s) for s in states])
        if spec.sticky:
            # 0xFFFFFFFF is EWMH's "all desktops".
            d.set_prop(wid, "_NET_WM_DESKTOP", xp.Atom.CARDINAL, [0xFFFFFFFF])

        if spec.exclusive:
            self._set_struts()

    def _set_input_shape(self) -> None:
        """Confine input to the content rect.

        The bleed margin is transparent, but an X window swallows clicks
        across its whole rectangle regardless of what it painted — so an
        override-redirect popup above everything would eat clicks in a
        50px invisible halo. SHAPE's input region is the only way to say
        "these pixels aren't mine".
        """
        if self.bleed <= 0 or self.wid is None:
            return
        ext = shape_ext(self.display.conn)
        if ext is None:
            log.warning("no SHAPE extension; %s will swallow clicks in its "
                        "%dpx bleed margin", self.spec.name, self.bleed)
            return
        _, _, cw, ch = self.content_root
        rects = [xp.RECTANGLE.synthetic(self.bleed, self.bleed, cw, ch)]
        # rectangles_len is an explicit argument, like PutImage's data_len.
        ext.Rectangles(
            xcffib.shape.SO.Set, xcffib.shape.SK.Input,
            xp.ClipOrdering.Unsorted, self.wid, 0, 0, len(rects), rects)
        log.debug("%s input shape confined to %dx%d+%d+%d",
                  self.spec.name, cw, ch, self.bleed, self.bleed)

    def _set_struts(self) -> None:
        """Reserve our slice of the screen.

        Values are relative to the whole X screen, not the monitor — on a
        stacked multi-head layout a bar on the primary monitor's bottom
        edge may have another monitor below it, and reserving
        `bar_height` there would carve space out of the wrong screen row.
        """
        d, wid = self.display, self.wid
        assert wid is not None
        screen_w, screen_h = d.geometry()
        left = right = top = bottom = 0
        strut = [0] * 12  # l, r, t, b, then per-edge start/end pairs
        # Reserve the content, not the window: the bleed margin is a
        # transparent drawing allowance, and reserving it would push
        # tiled windows away by an invisible extra 50px.
        cx, cy, cw, ch = self.content_root

        a = self.spec.anchor
        if Anchor.TOP in a and Anchor.BOTTOM not in a:
            top = cy + ch
            strut[8], strut[9] = cx, cx + cw - 1
        elif Anchor.BOTTOM in a and Anchor.TOP not in a:
            bottom = screen_h - cy
            strut[10], strut[11] = cx, cx + cw - 1
        elif Anchor.LEFT in a and Anchor.RIGHT not in a:
            left = cx + cw
            strut[4], strut[5] = cy, cy + ch - 1
        elif Anchor.RIGHT in a and Anchor.LEFT not in a:
            right = screen_w - cx
            strut[6], strut[7] = cy, cy + ch - 1
        else:
            log.warning("%s is exclusive but not anchored to a single edge; "
                        "reserving nothing", self.spec.name)
            return

        strut[0], strut[1], strut[2], strut[3] = left, right, top, bottom
        d.set_prop(wid, "_NET_WM_STRUT_PARTIAL", xp.Atom.CARDINAL, strut)
        d.set_prop(wid, "_NET_WM_STRUT", xp.Atom.CARDINAL, strut[:4])
        log.debug("%s struts l/r/t/b = %d/%d/%d/%d",
                  self.spec.name, left, right, top, bottom)

    def _adopt_size(self, width: int, height: int) -> None:
        """Take on a new window size: surface, content rect, layout.

        Shared by the ConfigureNotify handler and `resize_content`, which
        has to apply it immediately rather than wait for the notify — see
        there.
        """
        self.width, self.height = width, height
        b = self.bleed
        self.content_rect = skia.Rect.MakeXYWH(
            b, b, max(1, self.width - 2 * b), max(1, self.height - 2 * b))
        self.surface = self._make_surface(self.width, self.height)
        if self.gl_surface is not None:
            # The framebuffer follows the window, but the Skia render
            # target wrapping it carries the old size.
            self.gl.make_current(self.gl_surface)
            self.gl_surface.resize(self.width, self.height)
        self._layout_content()
        # A fresh surface holds nothing, so a partial repaint would leave
        # uninitialised pixels everywhere it skipped. And nothing else
        # will ask for the frame: the ConfigureNotify this provokes finds
        # the size already adopted.
        self.damage(None)

    def resize_content(self, cw: int, ch: int) -> None:
        """Resize to a new content size, re-anchored per the spec.

        For windows whose content set changes while mapped (the tray
        panel gaining a row) or is only known once measured (the network
        panel, which sizes itself to its interface list).

        The new size is adopted *immediately*, not on the ConfigureNotify
        it provokes. Waiting leaves the content laid out for the old size
        for however long the round trip takes — and when this is called
        from `attach()`, that is the whole of the first frame, so the
        panel maps with its content arranged for a taller window and
        visibly snaps once the notify lands.
        """
        if self.wid is None:
            return
        cw, ch = max(1, int(cw)), max(1, int(ch))
        cx, cy, cw, ch = self._compute_geometry(
            self.display.primary_monitor(), size=(cw, ch))
        self._apply_geometry(cx, cy, cw, ch)

    def _apply_geometry(self, cx: int, cy: int, cw: int, ch: int) -> None:
        """Move/resize the live window to a new content box, if it differs."""
        if (cx, cy, cw, ch) == self.content_root:
            return
        self.bleed = b = self._compute_bleed(cw, ch)
        self.content_root = (cx, cy, cw, ch)
        self.x, self.y = cx - b, cy - b
        self.display.conn.core.ConfigureWindow(
            self.wid,
            xp.ConfigWindow.X | xp.ConfigWindow.Y
            | xp.ConfigWindow.Width | xp.ConfigWindow.Height,
            [self.x, self.y, cw + 2 * b, ch + 2 * b])
        self._set_input_shape()
        self.display.flush()
        self._adopt_size(cw + 2 * b, ch + 2 * b)

    # ── lifecycle ───────────────────────────────────────────────────────
    def show(self) -> None:
        assert self.wid is not None
        self._t0 = time.monotonic()
        self._seed = random.random() * 997.0
        if self._transition > 0:
            self._appear = 0.0
            self._begin_phase("enter")

        # Paint and freeze the content *before* mapping.
        #
        # The widget tree paint is the expensive half of a frame — tens
        # of ms for a large panel, plus the first shader compile — and
        # doing it after MapWindow leaves an empty window on screen for
        # exactly that long. The animation then started against a window
        # the user had already seen appear, so its opening frames were
        # effectively spent before anything was drawn. Painting first
        # means the map and the first animated frame land together.
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

        self.display.conn.core.MapWindow(self.wid)
        self.display.flush()
        self.mapped = True
        if self._phase != "enter":
            self._set_blur(True)
        if self.spec.keep_above:
            self.display.keep_above(self.wid)

        # The spawn's own time starts with its first frame on the clock,
        # not here: this frame draws it at zero, which for the panels'
        # effect is a transparent window, and nothing that happens between
        # the two is charged to it — see `_advance_phase`.
        self.redraw()
        # Grabs after the first frame is out, not before: each grab waits
        # on a server reply, and with the compositor busy that wait was
        # measured at up to 16ms — time the window spent mapped but not
        # yet painted. A click landing in that gap is not worth it.
        if self.spec.dismiss_on_outside_click:
            self._grab_pointer()
        if self.spec.grab_keyboard:
            self._grab_keyboard()
        self._pace()
        if self._phase != "enter":
            self._notify_shown()

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
            self._finish_dismiss()
            return
        self._ungrab_pointer()
        self._ungrab_keyboard()
        self._set_blur(False)
        self._seed = random.random() * 997.0
        self._frozen = self.surface.makeImageSnapshot()
        self._begin_phase("exit")
        log.debug("%s despawning over %.2fs", self.spec.name, self._transition)
        self._stop_clock()
        self._pace()

    # ── grabs ───────────────────────────────────────────────────────────
    def _grab_pointer(self) -> None:
        """Grab the pointer so clicks anywhere else reach us.

        owner_events=True keeps clicks on our *own* windows going to those
        windows, so the bar stays usable while a panel is open — the
        trade-off is that the panel never sees them, and the window that
        did has to dismiss us on our behalf (Daemon.dismiss_others).
        """
        self._grab("pointer")

    def _grab_keyboard(self) -> None:
        """Take the keyboard, so keys reach a window that has no focus."""
        self._grab("keyboard")

    def _grab(self, what: str, since: float | None = None) -> None:
        """Take the pointer or the keyboard, trying again for a while if
        something else holds it.

        Something usually does. A window opened from a keybinding is
        shown while that key is still down, and the window manager's own
        grab on the key lasts until it comes up — a tenth of a second or
        more, during which ours is refused. The retries are timers, not
        sleeps: the window is on screen and animating meanwhile. Sleeping
        through them (10, 20, 30… ms) froze the whole shell for that
        long, and since the spawn was already counting, a panel opened
        from the keyboard was first seen with most of its spawn played.
        """
        self._grab_retry.pop(what, None)
        if self.wid is None or not self.mapped or self._dismissing():
            return
        core = self.display.conn.core
        try:
            if what == "pointer":
                reply = core.GrabPointer(
                    True, self.wid,
                    xp.EventMask.ButtonPress | xp.EventMask.ButtonRelease,
                    xp.GrabMode.Async, xp.GrabMode.Async,
                    xp.Atom._None, xp.Atom._None, xp.Time.CurrentTime).reply()
            else:
                reply = core.GrabKeyboard(
                    False, self.wid, xp.Time.CurrentTime,
                    xp.GrabMode.Async, xp.GrabMode.Async).reply()
        except Exception:
            log.debug("%s %s grab errored", self.spec.name, what,
                      exc_info=True)
            return
        # The reply may have pulled events off the wire behind the fd.
        self.display.wake()
        if reply.status == xp.GrabStatus.Success:
            if what == "pointer":
                self._grabbed = True
            else:
                self._kb_grabbed = True
                # Keys only reach us under a grab, so re-reading the
                # keymap here covers every layout switch since the last
                # one. MappingNotify alone is not enough: once a client
                # has initialised XKB (detectable auto-repeat) the server
                # may send it XKB notifies instead, which we do not
                # select. The reload is lazy — one round trip on the
                # first key press.
                self.display.invalidate_keymap()
            log.debug("%s grabbed the %s", self.spec.name, what)
            return
        now = time.monotonic()
        since = now if since is None else since
        if self.loop is None or now - since > GRAB_GIVE_UP_S:
            log.warning("%s could not grab the %s%s", self.spec.name, what,
                        "; outside clicks will not dismiss it"
                        if what == "pointer" else "")
            return
        self._grab_retry[what] = self.loop.call_later(
            GRAB_RETRY_S, self._grab, what, since)

    @property
    def keyboard_grabbed(self) -> bool:
        return self._kb_grabbed

    def _ungrab(self, what: str) -> None:
        retry = self._grab_retry.pop(what, None)
        if retry is not None:
            retry.cancel()
        held = self._grabbed if what == "pointer" else self._kb_grabbed
        if not held:
            return
        core = self.display.conn.core
        try:
            if what == "pointer":
                self._grabbed = False
                core.UngrabPointer(xp.Time.CurrentTime)
            else:
                self._kb_grabbed = False
                core.UngrabKeyboard(xp.Time.CurrentTime)
            self.display.flush()
        except Exception:
            log.debug("%s ungrab failed", what, exc_info=True)

    def _ungrab_keyboard(self) -> None:
        self._ungrab("keyboard")

    def _ungrab_pointer(self) -> None:
        self._ungrab("pointer")

    def _finish_dismiss(self) -> None:
        callback, self._on_dismissed = self._on_dismissed, None
        self._phase = "gone"
        if callback is not None:
            callback()

    def hide(self) -> None:
        if self.wid is None or not self.mapped:
            return
        self._stop_clock()
        self.display.keep_above(self.wid, False)
        self.display.conn.core.UnmapWindow(self.wid)
        self.display.flush()
        self.mapped = False

    def relocate(self) -> None:
        """Re-place on the current primary monitor after the screen
        changed — a display switched, a resolution set.

        Dimensions the spec leaves to the monitor (the bar's width) are
        recomputed; fixed ones keep the size the window has, which for a
        content-sized window is what `resize_content` gave it, not the
        spec's placeholder. Struts follow, since they are positions.
        """
        if self.wid is None:
            return
        _cx, _cy, cw, ch = self.content_root
        want_w, want_h = self.spec.size
        size = (cw if want_w is not None else None,
                ch if want_h is not None else None)
        cx, cy, cw, ch = self._compute_geometry(
            self.display.primary_monitor(), size=size)
        if (cx, cy, cw, ch) == self.content_root:
            return
        self._apply_geometry(cx, cy, cw, ch)     # repaints, too
        if self.spec.exclusive:
            self._set_struts()

    def park(self) -> None:
        """Hide, and keep everything expensive for the next `show()`.

        What a closed panel pays to come back is the window, the EGL
        surface and the GPU's first-use costs, so those stay. The content
        is detached exactly as `destroy()` would, so a parked panel's
        widgets run no subscriptions or samplers while it is out of
        sight, and `unpark()` re-attaches them just as a fresh `create()`
        would have. Everything the last open or dismiss left behind —
        the frozen despawn frame, the phase, the shown flag — is reset
        so the next `show()` starts as if from `create()`.
        """
        self.hide()
        self._ungrab_pointer()
        self._ungrab_keyboard()
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
            self.display.primary_monitor(), size=(cw, ch))
        self._apply_geometry(cx, cy, cw, ch)
        # No layout here: park() left `_relayout_queued` set, and show()
        # lays out once before it paints. Doing it here as well cost the
        # settings panel a second 5ms pass on every open.
        self._damage_all = True

    def destroy(self) -> None:
        if self.wid is None:
            return
        self._ungrab_pointer()
        self._ungrab_keyboard()
        self._stop_clock()
        if self.gl_surface is not None:
            # Drop every GPU-backed surface while this window's context is
            # still current, so their textures are released against the
            # right context rather than lingering in the shared cache.
            self.gl.make_current(self.gl_surface)
            self.surface = self._frozen = self._out = None
            self._small_in = self._small_out = None
            self.gl_surface.destroy()
            self.gl_surface = None
            self.gl = None
        if self.content is not None and not self._parked:
            try:
                self.content.detach()
            except Exception:
                log.exception("detach failed for %s", self.spec.name)
            if self.holo is not None:
                self.holo.detach()
        self.display.off(self.wid)
        self.display.keep_above(self.wid, False)
        try:
            self.display.conn.core.DestroyWindow(self.wid)
            self.display.flush()
        except Exception:
            log.debug("destroy failed for 0x%x", self.wid, exc_info=True)
        log.info("destroyed %s 0x%x", self.spec.name, self.wid)
        self.wid = None
        self.surface = None
        self.mapped = False

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
        """Paint what is dirty and present it. Frames call this; nothing
        else should, or it paints widgets no tick has looked at — see
        `_tick_widgets`."""
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
            self.gl.make_current(self.gl_surface)

        if self._frozen is not None:
            frame = self._frozen
        else:
            if self._relayout_queued:
                self._relayout_queued = False
                self._layout_content()
                self._damage_all = True     # layout can move anything
            dirty = self._take_damage()
            if dirty is not None and not dirty.isEmpty():
                self.paint(self.surface.getCanvas(), self.width, self.height,
                           dirty)
                self.surface.flushAndSubmit()
            frame = self.surface.makeImageSnapshot()

        passes = [e for e in self.effects if e.active(self._appear)]
        if not passes:
            if self.gl_surface is not None:
                # Straight to the window's framebuffer, then swap.
                canvas = self.gl_surface.skia_surface.getCanvas()
                canvas.clear(skia.ColorSetARGB(0, 0, 0, 0))
                canvas.drawImage(frame, 0, 0, self._sampling)
                self.gl_surface.swap()
            else:
                self._present(frame.tobytes())
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

        if self.gl_surface is not None:
            # `out` is already the window's framebuffer — swap, don't
            # read back. This is the whole saving: no tobytes(), no
            # 2.3MB of PutImage, no 40 X requests per frame.
            self.gl_surface.swap()
            return
        out.flushAndSubmit()
        self._present(out.makeImageSnapshot().tobytes())

    def _make_surface(self, w: int, h: int) -> skia.Surface:
        """An offscreen surface on whichever backend this window uses."""
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
        # On the GPU path the window's own framebuffer *is* the output —
        # there is nothing to composite into and nothing to read back.
        if self.gl_surface is not None:
            return self.gl_surface.skia_surface
        if self._out is None or self._out.width() != self.width \
                or self._out.height() != self.height:
            self._out = skia.Surface(self.width, self.height)
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
        """The region to repaint this frame, or None if nothing is dirty."""
        if self._damage_all:
            self._damage_all = False
            self._damage = None
            full = skia.Region()
            full.setRect(skia.IRect.MakeWH(self.width, self.height))
            return full
        region, self._damage = self._damage, None
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

    def _present(self, pixels: bytes) -> None:
        """Upload the frame.

        skia's N32 is BGRA premultiplied on little-endian, which is
        exactly what X expects for ZPixmap at depth 24 and 32 — so the
        buffer goes up untouched. Rows are chunked because a single
        PutImage has to fit inside one X request.
        """
        assert self.wid is not None and self.gc is not None
        conn = self.display.conn
        stride = self.width * 4
        rows_per_chunk = max(1, 60000 // stride)
        for y0 in range(0, self.height, rows_per_chunk):
            rows = min(rows_per_chunk, self.height - y0)
            chunk = pixels[y0 * stride:(y0 + rows) * stride]
            conn.core.PutImage(
                xp.ImageFormat.ZPixmap, self.wid, self.gc,
                self.width, rows, 0, y0, 0, self.depth, len(chunk), chunk)
        self.display.flush()

    # ── events ──────────────────────────────────────────────────────────
    def _on_event(self, ev) -> None:
        if isinstance(ev, xp.ExposeEvent):
            # Expose means the server wants content it no longer has, so
            # this is one of the few genuine whole-window repaints — the
            # damage region says what *we* changed, not what was lost.
            # Coalesced: only the last rectangle of a burst needs it.
            if ev.count == 0:
                self.damage(None)
        elif isinstance(ev, xp.ConfigureNotifyEvent):
            if (ev.width, ev.height) != (self.width, self.height):
                log.debug("%s resized %dx%d -> %dx%d", self.spec.name,
                          self.width, self.height, ev.width, ev.height)
                self._adopt_size(ev.width, ev.height)
            self.x, self.y = ev.x, ev.y
        elif isinstance(ev, xp.ButtonPressEvent):
            self._on_press(ev)
        elif isinstance(ev, xp.ButtonReleaseEvent):
            self._on_release()
        elif isinstance(ev, xp.MotionNotifyEvent):
            # A drag owns the pointer until release: motion goes to the
            # captured widget wherever the cursor is, and hover stops
            # tracking, or dragging a slider past its own edge would
            # both drop the drag and light up whatever is underneath.
            if self._dragging is not None:
                self._deliver_drag(ev.event_x, ev.event_y, commit=False)
            else:
                self._set_hover(self._pick(ev.event_x, ev.event_y))
        elif (ev.response_type & 0x7F) == 3:
            # KeyRelease. Checked by opcode, ahead of the press branch:
            # xcffib gives releases the KeyPressEvent shape, and a grab
            # delivers both whatever the window's event mask says.
            self._on_key_release(ev)
        elif isinstance(ev, xp.KeyPressEvent):
            self._on_key(ev)
        elif isinstance(ev, xp.LeaveNotifyEvent):
            self._set_hover(None)

    def _key_event(self, ev) -> KeyEvent:
        return key_event(self.display, ev)

    def _dismissing(self) -> bool:
        # Keys are dropped only while dismissing: the exit pass runs over
        # a still image and the widget tree may already belong to a
        # freshly opened window. The *enter* pass is frozen too, but its
        # tree is ours, so keys are delivered — a launcher opened by a
        # keybinding is typed into at once, and dropping the first 0.2s
        # of keys loses letters. What they change is painted when the
        # entrance unfreezes, which re-lays out and repaints everything.
        return self._phase in ("exit", "gone")

    def _on_key(self, ev) -> None:
        if self.content is None or self._dismissing():
            return
        key = self._key_event(ev)
        if any(_chord_is(key, chord) for chord in self.spec.close_keys):
            self._request_close()
            return
        try:
            if not self.content.key(key):
                # Escape always closes a grabbing window, even if nothing
                # in the tree wanted it — otherwise a grab with no exit
                # is a locked session.
                if key.keysym == 0xFF1B:
                    self._request_close()
        except Exception:
            log.exception("key handler failed in %s", self.spec.name)

    def _on_key_release(self, ev) -> None:
        if self.content is None or self._dismissing():
            return
        try:
            self.content.key_release(self._key_event(ev))
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

    def _on_press(self, ev) -> None:
        if self.spec.dismiss_on_outside_click and self._phase not in ("exit", "gone"):
            # Under the grab, clicks anywhere else in the session arrive
            # here with coordinates relative to us — so "outside" is just
            # a rect test.
            inside = self.content_rect.contains(ev.event_x, ev.event_y)
            # A window cut to its plates (`blur`) is only as big as they
            # are: a click in the cut-away part of its rect is outside.
            shape_of = getattr(self.content, "blur_shape", None)
            if inside and self.spec.blur and shape_of is not None:
                inside = shape_of().contains(ev.event_x, ev.event_y)
            if not inside:
                log.debug("%s dismissed by outside click", self.spec.name)
                self._request_close()
                return

        widget = self._pick(ev.event_x, ev.event_y)
        if widget is None:
            # A click that hit nothing in a window that stays open still
            # has to dismiss any panel that's listening for outside clicks,
            # because the grab never showed it to them.
            self._dismiss_others()
            return
        # Scroll arrives as buttons 4-7, not as a separate event type.
        if ev.detail in (1, 2, 3):
            self._pressed = widget
            widget.pressed = True
            widget.invalidate()
        if ev.detail == 1 and widget.on_drag is not None:
            self._dragging = widget
            self._deliver_drag(ev.event_x, ev.event_y, commit=False)
            return          # a drag press is not also a click
        log.debug("button %d on %s", ev.detail, type(widget).__name__)
        try:
            widget.click(ev.detail, ev.event_x, ev.event_y)
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

    def _on_release(self) -> None:
        if self._dragging is not None:
            # The commit pass is what lets a control write once at the
            # end of a drag instead of firing a subprocess per motion
            # event — `brightnessctl` at 60 Hz would fork a hundred
            # processes crossing one slider.
            self._deliver_drag(*self._drag_xy, commit=True)
            self._dragging = None
        if self._pressed is not None:
            self._pressed.pressed = False
            self._pressed.invalidate()
            self._pressed = None


def _wm_class_name() -> str:
    from .core.naming import APP
    return APP
