"""The top bar's context area: what the focused indigoshell app shows.

`AppContext` is a widget for a bar's row (config_default's `top_bar`).
It takes all the room the row leaves it and fills it with the focused
app window's own `content` (service.AppWindow), which that window's kind
draws: Flux's tabs and host (applink/flux.py). When the
window with focus is no app's, it is empty: the bar's background.

Focus moving swaps the content: the one shown is attached to the bar's
window, the one left is detached and forgets the window, so an app
window in the background keeps its content up to date without
repainting the bar for it.

The pointer reaches the shown content as any widget in the bar: the
bar's window hit-tests through this widget into it (Flux's tabs take
their presses and drags: Widget.press). Content swapped out while it
holds a press lets go of it in its `detach`, which the swap calls.
"""

import logging

import skia

from ..widgets.base import Size, Widget

log = logging.getLogger(__name__)


def _unhost(widget: Widget) -> None:
    """Detach a subtree and make it forget the window, so a change to it
    while it is not shown damages nothing."""
    try:
        widget.detach()
    except Exception:
        log.exception("detaching an app's content failed")
    for w in widget.walk():
        w.window = None
    widget._parent = None


class AppContext(Widget):
    """The focused app window's content, or nothing."""

    def __init__(self, service) -> None:
        super().__init__(flex=1)
        self.service = service
        self.shown: Widget | None = None

    def children(self):
        return (self.shown,) if self.shown is not None else ()

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        self.window = window
        self.service.context = self
        self.service.follow(self._on_focus)
        self._on_focus(self.service.focused)

    def detach(self) -> None:
        self.service.unfollow(self._on_focus)
        if self.service.context is self:
            self.service.context = None
        if self.shown is not None:
            _unhost(self.shown)
            self.shown = None

    def _on_focus(self, app) -> None:
        content = getattr(app, "content", None)
        if content is self.shown:
            return
        if self.shown is not None:
            _unhost(self.shown)
        self.shown = content
        if content is not None and self.window is not None:
            content._parent = self
            content.attach(self.window)
        self.invalidate(layout=True)

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        if self.shown is not None:
            self.shown.measure(avail)
        # Nothing of its own along the row (the row's leftover is its
        # width, by flex); the bar's whole height across it, so a kind
        # can draw along the bar's bottom edge.
        return Size(0.0, avail.height)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.shown is not None:
            self.shown.measure(Size(rect.width(), rect.height()))
            self.shown.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        if self.shown is not None:
            canvas.save()
            canvas.clipRect(self.rect)
            self.shown.paint(canvas)
            canvas.restore()
