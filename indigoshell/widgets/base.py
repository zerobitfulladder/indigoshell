"""The widget contract.

Four methods, and everything else is built from them:

    measure(avail) -> Size    what I'd like to be
    arrange(rect)             where I actually am; containers recurse
    paint(canvas)             draw myself
    hit(x, y) -> Widget|None  who owns this point

Containers are widgets too, so a bar, a panel and a notification all hold
the same type. This is also the plugin API, which is why it stays this
small and why nothing in it mentions X11 or the window that hosts it.
"""

from dataclasses import dataclass
from typing import Callable, Iterable

import skia

# Ceiling on one frame's elapsed time. Long enough that a widget dropping
# a frame or two still advances smoothly, short enough that resuming
# after an idle spell does not jump.
MAX_TICK_DT = 0.25


@dataclass(frozen=True)
class Size:
    width: float
    height: float


@dataclass(frozen=True)
class Insets:
    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0

    @staticmethod
    def all(v: int) -> "Insets":
        return Insets(v, v, v, v)

    @staticmethod
    def xy(h: int, v: int) -> "Insets":
        return Insets(v, h, v, h)

    @property
    def horizontal(self) -> int:
        return self.left + self.right

    @property
    def vertical(self) -> int:
        return self.top + self.bottom

    def deflate(self, rect: skia.Rect) -> skia.Rect:
        return skia.Rect.MakeLTRB(
            rect.left() + self.left, rect.top() + self.top,
            rect.right() - self.right, rect.bottom() - self.bottom,
        )


class Widget:
    # >0 asks the host window for a frame clock at this rate. Widgets that
    # only change on input stay at 0 and cost nothing when idle.
    #
    # Read every frame, so it may be a property: a widget that only
    # animates sometimes should return 0 the rest of the time and the
    # window will slow down or stop its clock. A widget that *raises* it
    # must also `invalidate()`, which is what wakes a stopped clock.
    animation_fps: int = 0

    # Damage from this widget's subtree is reported as damage to *this*
    # widget instead. Set it on a widget that draws outside its own
    # allocation — a post-process pass with bleed, a glow, a shadow —
    # because a child's rect does not cover the fringe drawn beyond it,
    # and a partial repaint clipped to the child would leave that stale.
    absorbs_damage: bool = False

    # Owned by the host window's frame clock: when this widget is next
    # due an `animate()` call. Kept on the widget rather than in a map
    # keyed by id() so a replaced subtree takes its schedule with it and
    # leaves nothing behind to leak.
    _next_animate_at: float = 0.0
    _last_animate_t: float | None = None

    def __init__(
        self,
        *,
        flex: int = 0,
        on_left_click: Callable | None = None,
        on_right_click: Callable | None = None,
        on_middle_click: Callable | None = None,
        on_scroll_up: Callable | None = None,
        on_scroll_down: Callable | None = None,
        on_drag: Callable | None = None,
    ) -> None:
        self.rect = skia.Rect.MakeEmpty()
        self.window = None          # set by attach()
        self._parent: "Widget | None" = None
        self.flex = flex            # share of leftover space along the main axis
        self.hovered = False
        self.pressed = False
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.on_middle_click = on_middle_click
        self.on_scroll_up = on_scroll_up
        self.on_scroll_down = on_scroll_down
        # Called with (widget, x, y) on press and on every motion while
        # the button is held, then once more on release with commit=True.
        # A widget that sets this captures the pointer for the drag, so a
        # slider keeps tracking after the cursor leaves its own rect.
        self.on_drag = on_drag

    # ── tree ────────────────────────────────────────────────────────────
    def children(self) -> Iterable["Widget"]:
        return ()

    def walk(self):
        yield self
        for child in self.children():
            yield from child.walk()

    def attach(self, window) -> None:
        self.window = window
        for child in self.children():
            child._parent = self
            child.attach(window)

    def on_shown(self) -> None:
        """Called once the window is mapped and its spawn animation has
        finished.

        Anything that costs the event loop — spawning subprocesses,
        starting subscriptions — belongs here rather than in `attach()`.
        The transition runs off a frozen snapshot, so a value that
        arrives during it cannot be seen anyway, while the work needed to
        fetch it blocks the loop that is trying to render the animation.
        A panel that launched twenty subprocesses from `attach()` simply
        never showed its spawn: `_appear` is driven by wall clock, so the
        frames the loop missed were frames the animation skipped.
        """
        for child in self.children():
            child.on_shown()

    def detach(self) -> None:
        """Release anything started in attach() — subscriptions, tasks.
        Called when the host window is destroyed."""
        for child in self.children():
            child.detach()

    # ── layout ──────────────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        raise NotImplementedError

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect

    def baseline(self, size: Size) -> float | None:
        """Text baseline offset from the top of the measured box, if this
        widget has one. Used by Align.BASELINE."""
        return None

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        raise NotImplementedError

    def paint_bounds(self) -> skia.Rect:
        """Every pixel this widget may touch when it paints.

        A partial repaint clips to the union of these, so a widget whose
        paint reaches past its allocation has to widen this or its
        fringes are left behind by the clip. Defaults to the allocation,
        which is right for anything that draws inside its own box.
        """
        return self.rect

    def invalidate(self, layout: bool = False) -> None:
        """Ask for a repaint of this widget, and optionally a re-measure.

        Text that changes width (a clock ticking to a wider minute, a
        stat going from 9% to 100%) changes the widget's *size*, so the
        container has to re-run measure/arrange — repainting alone would
        draw the new string into the old slot. A relayout can move
        anything, so it damages the whole window rather than one rect.

        Damage is attributed to the outermost ancestor that claims it —
        see `absorbs_damage`.
        """
        if self.window is None:
            return
        target, node = self, self._parent
        while node is not None:
            if node.absorbs_damage:
                target = node
            node = node._parent
        self.window.damage(target, layout=layout)

    def animate(self, t: float) -> None:
        """Called at `animation_fps` while it is > 0. `t` is seconds since
        the window opened.

        Anything advancing by elapsed time should take it from
        `tick_dt(t)` rather than differencing `t` itself.
        """

    def tick_dt(self, t: float) -> float:
        """Seconds since this widget's previous animated frame.

        Zero on the first frame after a pause (the window clears
        `_last_animate_t` for a widget while it is idle), and never more
        than one `MAX_TICK_DT` after a stalled frame. A widget that drops to `animation_fps` 0 stops
        being called at all, so the raw gap since its last frame is
        however long it was idle — differencing `t` by hand hands a
        scroll offset or a decay several seconds of "elapsed" time the
        moment it resumes, and the animation completes instantly instead
        of starting. That is what made the lyrics reveal jump straight to
        finished: it was idle 6s, so its first frame advanced the reveal
        by 6s of a 0.55s animation.
        """
        last = self._last_animate_t
        self._last_animate_t = t
        if last is None:
            return 0.0
        return min(max(0.0, t - last), MAX_TICK_DT)

    # ── input ───────────────────────────────────────────────────────────
    @property
    def interactive(self) -> bool:
        return any((
            self.on_left_click, self.on_right_click, self.on_middle_click,
            self.on_scroll_up, self.on_scroll_down, self.on_drag,
        ))

    def hit(self, x: float, y: float) -> "Widget | None":
        """Topmost interactive widget at this point.

        Uses the widget's own rect, so a shape with cut corners can
        override this and hit-test its actual `skia.Path` — the clickable
        area then matches the drawn area exactly, which a rectangular
        event box can never do.
        """
        if not self.rect.contains(x, y):
            return None
        for child in reversed(list(self.children())):
            found = child.hit(x, y)
            if found is not None:
                return found
        return self if self.interactive else None

    def key(self, keysym: int, shift: bool = False) -> bool:
        """Handle a key press; return True if consumed.

        Forwarded down the tree until something consumes it, so a panel
        deep inside a window still gets its arrow keys without the window
        knowing anything about focus.
        """
        for child in self.children():
            if child.key(keysym, shift):
                return True
        return False

    def key_release(self, keysym: int, shift: bool = False) -> bool:
        """Handle a key release; return True if consumed. Forwarded like
        `key`. Only a grabbing window receives releases — X reports both
        press and release to a keyboard grab regardless of event mask —
        and only press-to-arm / release-to-fire widgets care."""
        for child in self.children():
            if child.key_release(keysym, shift):
                return True
        return False

    def set_hovered(self, value: bool) -> None:
        if self.hovered != value:
            self.hovered = value
            if not value:
                self.pressed = False
            self.invalidate()

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        """Dispatch a press to the `on_*` handlers, which receive only the
        widget.

        A widget that draws several regions (workspace indicators, a chip
        row, a tab strip) wants the position instead, and gets it by
        overriding this rather than by setting a handler — `x`/`y` are
        window-local. Such a widget must override `hit()` too, since with
        no handler set it is not `interactive` and the default `hit()`
        would skip it.
        """
        handler = {
            1: self.on_left_click,
            2: self.on_middle_click,
            3: self.on_right_click,
            4: self.on_scroll_up,
            5: self.on_scroll_down,
        }.get(button)
        if handler is None:
            return False
        handler(self)
        return True
