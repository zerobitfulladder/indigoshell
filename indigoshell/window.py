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
import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto

import skia
import xcffib.shape

from . import theme
from .backend.gl import backend as gl_backend
from .backend.x11 import Display, Monitor, shape_ext, xp
from .widgets.base import Insets, Size, Widget

log = logging.getLogger(__name__)


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
        """Whether we must raise ourselves.

        An override-redirect window is invisible to the window manager, so
        _NET_WM_STATE_ABOVE is ignored for it and stacking falls back to
        raw X order — whatever was raised last wins. Managed windows (the
        bar) get their layer honoured and need none of this.
        """
        return self.override_redirect and self.layer in (Layer.DOCK, Layer.OVERLAY)


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
        # Effects that displace pixels draw past the content's edge, so the
        # X window is grown by `bleed` on every side and the content is
        # inset by it. `content_rect` is that inset area in window-local
        # coordinates; `content_root` is the same rect in root coordinates,
        # which is what struts and placement care about.
        self.bleed = 0
        self.content_rect = skia.Rect.MakeEmpty()
        self.content_root = (0, 0, 0, 0)

        self._redraw_queued = False
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

        self.gl = None               # backend.gl.Backend, or None for raster
        self.gl_surface = None
        self.effects = tuple(spec.effects)
        self._transition = max((e.duration for e in self.effects), default=0.0)
        self._appear = 1.0
        self._phase = "idle"        # idle | enter | exit | gone
        self._phase_t0 = 0.0
        self._frozen: skia.Image | None = None
        self._out: skia.Surface | None = None
        self._sampling = skia.SamplingOptions()
        self._on_dismissed = None
        self._seed = 0.0
        self._grabbed = False
        self._kb_grabbed = False

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
        if self.spec.keep_above:
            # VisibilityNotify is how an unmanaged window learns something
            # was stacked over it; there is no other signal.
            events |= xp.EventMask.VisibilityChange
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
        # uninitialised pixels everywhere it skipped.
        self._damage_all = True

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
            self._phase = "enter"

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
            self.paint(self.surface.getCanvas(), self.width, self.height)
            self.surface.flushAndSubmit()
            if self._phase == "enter":
                self._frozen = self.surface.makeImageSnapshot()

        self.display.conn.core.MapWindow(self.wid)
        self.display.flush()
        self.mapped = True
        if self.spec.keep_above:
            self.raise_()

        # Clock starts once the window is up and the costly work is done,
        # so `_appear` (wall-clock driven, so a late frame can't stretch
        # the animation) isn't charged for setup it didn't cause.
        self._phase_t0 = time.monotonic()
        self.redraw()
        # Grabs after the first frame is out, not before: each grab waits
        # on a server reply, and with the compositor busy that wait was
        # measured at up to 16ms — time the window spent mapped but not
        # yet painted. A click landing in that gap is not worth it.
        if self.spec.dismiss_on_outside_click:
            self._grab_pointer()
        if self.spec.grab_keyboard:
            self._grab_keyboard()
        self._start_clock()
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
        if (self._transition <= 0 or not self.mapped
                or self.loop is None or self.surface is None):
            self._finish_dismiss()
            return
        self._ungrab_pointer()
        self._ungrab_keyboard()
        self._seed = random.random() * 997.0
        self._frozen = self.surface.makeImageSnapshot()
        self._phase = "exit"
        self._phase_t0 = time.monotonic()
        log.debug("%s despawning over %.2fs", self.spec.name, self._transition)
        self._stop_clock()
        self._start_clock()

    # ── stacking and grabs ──────────────────────────────────────────────
    def raise_(self) -> None:
        if self.wid is None:
            return
        self.display.conn.core.ConfigureWindow(
            self.wid, xp.ConfigWindow.StackMode, [xp.StackMode.Above])
        self.display.flush()

    def _grab_pointer(self) -> None:
        """Grab the pointer so clicks anywhere else reach us.

        owner_events=True keeps clicks on our *own* windows going to those
        windows, so the bar stays usable while a panel is open — the
        trade-off is that the panel never sees them, and the window that
        did has to dismiss us on our behalf (Daemon.dismiss_others).

        Grabs race with whatever else is grabbing, so retry briefly.
        """
        if self.wid is None:
            return
        for attempt in range(10):
            try:
                reply = self.display.conn.core.GrabPointer(
                    True, self.wid,
                    xp.EventMask.ButtonPress | xp.EventMask.ButtonRelease,
                    xp.GrabMode.Async, xp.GrabMode.Async,
                    xp.Atom._None, xp.Atom._None, xp.Time.CurrentTime).reply()
            except Exception:
                log.debug("%s pointer grab errored", self.spec.name,
                          exc_info=True)
                return
            if reply.status == xp.GrabStatus.Success:
                self._grabbed = True
                log.debug("%s grabbed the pointer", self.spec.name)
                return
            time.sleep(0.01 * (attempt + 1))
        log.warning("%s could not grab the pointer; outside clicks will not "
                    "dismiss it", self.spec.name)

    def _grab_keyboard(self) -> None:
        """Take the keyboard. Retried because grabs race with whatever
        else is grabbing at the same moment (a WM chord, a menu closing)."""
        if self.wid is None:
            return
        for attempt in range(10):
            try:
                reply = self.display.conn.core.GrabKeyboard(
                    False, self.wid, xp.Time.CurrentTime,
                    xp.GrabMode.Async, xp.GrabMode.Async).reply()
            except Exception:
                log.debug("%s keyboard grab errored", self.spec.name,
                          exc_info=True)
                return
            if reply.status == xp.GrabStatus.Success:
                self._kb_grabbed = True
                log.debug("%s grabbed the keyboard", self.spec.name)
                return
            time.sleep(0.01 * (attempt + 1))
        log.warning("%s could not grab the keyboard", self.spec.name)

    def _ungrab_keyboard(self) -> None:
        if not self._kb_grabbed:
            return
        self._kb_grabbed = False
        try:
            self.display.conn.core.UngrabKeyboard(xp.Time.CurrentTime)
            self.display.flush()
        except Exception:
            log.debug("keyboard ungrab failed", exc_info=True)

    def _ungrab_pointer(self) -> None:
        if not self._grabbed:
            return
        self._grabbed = False
        try:
            self.display.conn.core.UngrabPointer(xp.Time.CurrentTime)
            self.display.flush()
        except Exception:
            log.debug("ungrab failed", exc_info=True)

    def _finish_dismiss(self) -> None:
        callback, self._on_dismissed = self._on_dismissed, None
        self._phase = "gone"
        if callback is not None:
            callback()

    def hide(self) -> None:
        if self.wid is None or not self.mapped:
            return
        self._stop_clock()
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
        self._apply_geometry(cx, cy, cw, ch)
        if self.spec.exclusive:
            self._set_struts()
        if self.mapped:
            self._damage_all = True
            self._schedule_redraw()

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
        self._frozen = None
        self._phase = "idle"
        self._appear = 1.0
        self._shown_sent = False
        self._hovered = self._pressed = self._dragging = None
        self._damage = None
        self._damage_all = True
        self._relayout_queued = True
        if self.content is not None:
            try:
                self.content.detach()
            except Exception:
                log.exception("detach failed for %s", self.spec.name)
        self._parked = True

    def unpark(self) -> None:
        """Undo `park()`: re-attach the content and re-place the window,
        in case the monitor layout changed while it was hidden."""
        if not self._parked:
            return
        self._parked = False
        if self.content is not None:
            self.content.attach(self)
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
        self.display.off(self.wid)
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
        canvas.save()
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
        canvas.restore()

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
        self._redraw_queued = False
        if self.surface is None or self.wid is None or not self.mapped:
            return

        # Nothing to present. Without this the frame loop swaps twice per
        # frame: `_tick_widgets` damages each widget it animates, and each
        # `damage()` queues a `call_soon(redraw)` on top of the redraw
        # `_on_frame` performs itself. The second one finds no damage and
        # paints nothing — but still snapshots, blits and *swaps*, and a
        # second vsync-synchronised swap is what turns a 30fps clock into
        # a visibly uneven one.
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
        if layout or widget is None:
            self._relayout_queued = self._relayout_queued or layout
            self._damage_all = True
        elif not self._damage_all:
            # roundOut, not round: a widget on a half-pixel boundary must
            # own the whole pixel it tints, or it leaves a seam behind.
            box = widget.paint_bounds().roundOut()
            if self._damage is None:
                self._damage = skia.Region()
                self._damage.setRect(box)
            else:
                self._damage.op(box, skia.Region.kUnion_Op)
        self._schedule_redraw()
        # A widget that starts animating raises its `animation_fps` and
        # invalidates; this is what gets the clock going again if it had
        # stopped because nothing was moving.
        self._ensure_clock()

    def invalidate(self, layout: bool = False) -> None:
        """Whole-window repaint. Kept for callers that have no widget to
        attribute the change to."""
        self.damage(None, layout=layout)

    def _schedule_redraw(self) -> None:
        if self._redraw_queued or self.loop is None:
            return
        self._redraw_queued = True
        self.loop.call_soon(self.redraw)

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

    # ── frame clock ─────────────────────────────────────────────────────
    def _widget_fps(self) -> int:
        if self.content is None or self._frozen is not None:
            return 0
        return max((w.animation_fps for w in self.content.walk()), default=0)

    def _effect_fps(self) -> int:
        live = [e for e in self.effects
                if e.is_transition or e.active(self._appear)]
        if self._phase in ("enter", "exit"):
            return max((e.fps for e in self.effects if e.is_transition),
                       default=0)
        return max((e.fps for e in live if not e.is_transition), default=0)

    def _needs_clock(self) -> bool:
        return self._phase in ("enter", "exit") \
            or self._widget_fps() > 0 or self._effect_fps() > 0

    def _demand(self) -> int:
        """Fastest rate anything in this window wants right now."""
        return max(self._widget_fps(), self._effect_fps())

    def _start_clock(self) -> None:
        if self.loop is None:
            return
        fps = self._demand()
        if fps <= 0:
            return
        self._clock_fps = fps
        self._frame_interval = 1.0 / fps
        self._next_frame_at = time.monotonic()
        # A stopped clock means nothing was animating, so no widget has a
        # "previous frame" — see `_tick_widgets`, which does the same for
        # widgets that idle while the clock runs for others.
        if self.content is not None:
            for widget in self.content.walk():
                widget._last_animate_t = None
        log.debug("%s frame clock at %d fps (%s)",
                  self.spec.name, fps, self._phase)
        self._schedule_frame()

    def _ensure_clock(self) -> None:
        """Start the clock if something now wants frames.

        `animation_fps` is read fresh every frame, so a widget can drop to
        0 and let the clock stop. Nothing would ever start it again — the
        clock is what re-reads the demand — so waking it is tied to
        `damage()`, which a widget that starts animating calls anyway.

        Gated on `_clock_fps` rather than on `_frame_handle`: the handle
        is None *during* a frame too, and damage raised from `animate()`
        would otherwise schedule a second one alongside it.
        """
        if self._clock_fps == 0 and self.mapped:
            self._start_clock()

    def _rerate_clock(self) -> None:
        """Follow a change in demand without dropping the schedule.

        Re-rating rather than stop/start keeps `_next_frame_at` on its
        grid, so a widget going quiet cannot make the next frame land
        early and stutter whatever is still moving.
        """
        fps = self._demand()
        if fps <= 0 or fps == self._clock_fps:
            return
        log.debug("%s frame clock %d -> %d fps", self.spec.name,
                  self._clock_fps, fps)
        self._clock_fps = fps
        self._frame_interval = 1.0 / fps

    def _stop_clock(self) -> None:
        if self._frame_handle is not None:
            self._frame_handle.cancel()
            self._frame_handle = None
        self._clock_fps = 0

    def _schedule_frame(self) -> None:
        """Aim at the next slot on a fixed grid, not `interval` from now.

        `_schedule_frame` is called at the end of `_on_frame`, so a plain
        `call_later(interval)` makes the period *work + interval* rather
        than `interval`. At 15.7ms of frame work against a 16.7ms budget
        that halves the rate — 34fps where 60 was asked for — and because
        `_appear` is driven by wall clock the animation still finishes on
        time, simply with half the frames it wanted. That is the whole of
        the "not fluent" feel; it is a scheduling bug, not a shader one.
        """
        assert self.loop is not None
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

    def _on_frame(self) -> None:
        self._frame_handle = None
        if not self.mapped:
            return
        self._t = time.monotonic() - self._t0

        # Driven by wall clock, not by frame count: a late frame must not
        # stretch the transition. Under load the animation drops frames
        # and still finishes in `duration`.
        if self._phase == "enter":
            elapsed = time.monotonic() - self._phase_t0
            self._appear = min(1.0, elapsed / self._transition)
            if self._appear >= 1.0:
                self._phase = "idle"
                # Hand the window back to the live widget tree. Anything
                # that landed while frozen — an async GPU sample, a
                # resolved optimus-manager mode — repainted into the
                # still image and was swallowed, so re-measure as well.
                self._frozen = None
                self._relayout_queued = True
                # Deferred until here so the loop was free for the whole
                # animation — see Widget.on_shown.
                self._notify_shown()
        elif self._phase == "exit":
            elapsed = time.monotonic() - self._phase_t0
            self._appear = max(0.0, 1.0 - elapsed / self._transition)
            if self._appear <= 0.0:
                self.redraw()          # final, fully dissolved frame
                self._stop_clock()
                self._finish_dismiss()
                return

        if self._frozen is None and self.content is not None:
            self._tick_widgets()

        # Only produce a frame if something actually changed. A window
        # whose widgets are all quiet still ticks — cheaply — until its
        # demand drops to zero and the clock stops on its own.
        if self._damage_all or self._damage is not None \
                or self._phase in ("enter", "exit") or self._effect_fps() > 0:
            self.redraw()

        self._rerate_clock()
        if self._needs_clock():
            self._schedule_frame()
        else:
            self._clock_fps = 0
            log.debug("%s idle, clock stopped", self.spec.name)

    def _tick_widgets(self) -> None:
        """Call `animate` on each widget at *its* rate, not the window's.

        The clock runs at the fastest rate anything asked for, so without
        this a meter wanting 2fps was animated 30 times a second — its
        declared rate did nothing but pin the clock. Each widget carries
        its own due time, so the schedules stay independent.
        """
        t = self._t
        for widget in self.content.walk():
            fps = widget.animation_fps
            if fps <= 0:
                # Idle. Forget its last frame so `tick_dt` reports zero
                # when it next animates, as its contract says. Left set,
                # the gap since it went idle came back as one full
                # MAX_TICK_DT of elapsed time on its first frame — which
                # skipped the first ~45% of a 0.55s lyric reveal whenever
                # no beat had kept the widget ticking in between.
                widget._last_animate_t = None
                continue
            interval = 1.0 / fps
            # Half an interval of tolerance. `t` is when the callback ran,
            # which is always a little *after* the slot it was scheduled
            # for, so a widget wanting exactly the clock rate is never
            # quite due on the frame that should carry it — it slips to
            # the next one, and every other frame gets dropped. Asking
            # for 30 got 19.
            if t + interval * 0.5 < widget._next_animate_at:
                continue
            # Advance on the widget's own grid so the error cannot
            # accumulate, but never behind `t` — a widget that was idle
            # for a while must not then fire a burst catching up.
            widget._next_animate_at = max(
                widget._next_animate_at + interval, t)
            widget.animate(t)
            # Assume an animated frame changed something. A widget could
            # report this precisely instead, but then one that forgot
            # would silently stop updating — a failure that looks like a
            # frozen clock, not like a bug. Being conservative here costs
            # a repaint of that widget's own rect and keeps the contract
            # for new widgets down to "declare a rate".
            self.damage(widget)

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
                self._damage_all = True
                self.redraw()
        elif isinstance(ev, xp.ConfigureNotifyEvent):
            if (ev.width, ev.height) != (self.width, self.height):
                log.debug("%s resized %dx%d -> %dx%d", self.spec.name,
                          self.width, self.height, ev.width, ev.height)
                self._adopt_size(ev.width, ev.height)
                self.redraw()
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
        elif isinstance(ev, xp.MappingNotifyEvent):
            self.display.invalidate_keymap()
        elif isinstance(ev, xp.LeaveNotifyEvent):
            self._set_hover(None)
        elif isinstance(ev, xp.VisibilityNotifyEvent):
            # Something stacked over us. Managed windows never get here;
            # for an override-redirect window this is the only way back up.
            if ev.state != xp.Visibility.Unobscured and self._phase != "exit":
                self.raise_()

    def _on_key(self, ev) -> None:
        if self.content is None or self._frozen is not None:
            return
        shift = bool(ev.state & xp.KeyButMask.Shift)
        keysym = self.display.keysym(ev.detail, shift)
        try:
            if not self.content.key(keysym, shift):
                # Escape always closes a grabbing window, even if nothing
                # in the tree wanted it — otherwise a grab with no exit
                # is a locked session.
                if keysym == 0xFF1B:
                    self._request_close()
        except Exception:
            log.exception("key handler failed in %s", self.spec.name)

    def _on_key_release(self, ev) -> None:
        if self.content is None or self._frozen is not None:
            return
        shift = bool(ev.state & xp.KeyButMask.Shift)
        keysym = self.display.keysym(ev.detail, shift)
        try:
            self.content.key_release(keysym, shift)
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
            if not self.content_rect.contains(ev.event_x, ev.event_y):
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
