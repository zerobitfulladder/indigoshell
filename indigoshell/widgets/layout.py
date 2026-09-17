"""Layout containers.

Everything here is a `Widget`, so containers nest inside containers and a
bar row holds the same type a panel column does. Sizing is the two-pass
box model: `measure` asks children what they'd like, `arrange` tells them
what they get.
"""

from enum import Enum

import skia

from .. import shapes, theme
from .base import Insets, Size, Widget


class Align(Enum):
    """Cross-axis placement of a child inside its container."""

    START = "start"
    CENTER = "center"
    END = "end"
    STRETCH = "stretch"
    # Align children on their text baselines rather than their boxes.
    # Two labels at different sizes bottom-aligned do NOT share a
    # baseline — their descents differ — so this is its own mode.
    BASELINE = "baseline"


class _Axis(Widget):
    """Shared machinery for Row and Column.

    `_main`/`_cross` map the abstract main/cross axes onto x/y, so the
    layout maths is written once and Column is Row with the axes swapped.
    """

    horizontal = True

    def __init__(self, children, *, spacing: int = 0,
                 padding: Insets = Insets(), align: Align = Align.CENTER,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._children = list(children)
        self.spacing = spacing
        self.padding = padding
        self.align = align
        self._measured: list[Size] = []
        self._max_baseline = 0.0

    def children(self):
        return self._children

    def replace(self, children) -> None:
        """Swap the whole child list — for content that only exists once
        an async query lands. The new children are attached to the same
        host window, since the tree walk that normally does it at window
        creation has already run by then."""
        for child in self._children:
            child.detach()
        self._children = list(children)
        self._measured = []
        if self.window is not None:
            for child in self._children:
                child.attach(self.window)
        self.invalidate(layout=True)

    def measure(self, avail: Size) -> Size:
        inner = Size(max(0.0, avail.width - self.padding.horizontal),
                     max(0.0, avail.height - self.padding.vertical))
        self._measured = [c.measure(inner) for c in self._children]
        gaps = self.spacing * max(0, len(self._children) - 1)
        if self.horizontal:
            main = sum(s.width for s in self._measured) + gaps
            cross = max((s.height for s in self._measured), default=0.0)
            return Size(main + self.padding.horizontal,
                        cross + self.padding.vertical)
        main = sum(s.height for s in self._measured) + gaps
        cross = max((s.width for s in self._measured), default=0.0)
        return Size(cross + self.padding.horizontal,
                    main + self.padding.vertical)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        inner = self.padding.deflate(rect)
        if not self._children:
            return
        if len(self._measured) != len(self._children):
            self.measure(Size(rect.width(), rect.height()))

        gaps = self.spacing * (len(self._children) - 1)
        sizes = [s.width if self.horizontal else s.height for s in self._measured]
        extent = inner.width() if self.horizontal else inner.height()
        flex_total = sum(c.flex for c in self._children)
        # Leftover goes only to flex children; fixed children keep exactly
        # what they measured. Clamped at zero so an overfull container
        # overflows rather than assigning negative widths.
        leftover = max(0.0, extent - sum(sizes) - gaps)

        if self.align is Align.BASELINE:
            self._max_baseline = max(
                (c.baseline(s) or 0.0
                 for c, s in zip(self._children, self._measured)),
                default=0.0)

        pos = inner.left() if self.horizontal else inner.top()
        for child, size, base in zip(self._children, self._measured, sizes):
            main = base
            if child.flex and flex_total:
                main += leftover * child.flex / flex_total
            child.arrange(self._place(inner, pos, main, size, child))
            pos += main + self.spacing

    def _place(self, inner: skia.Rect, pos: float, main: float,
               size: Size, child: Widget | None = None) -> skia.Rect:
        cross_extent = inner.height() if self.horizontal else inner.width()
        want = size.height if self.horizontal else size.width
        if self.align is Align.BASELINE and self.horizontal:
            base = child.baseline(size) if child is not None else None
            if base is not None:
                return skia.Rect.MakeXYWH(
                    pos, inner.top() + self._max_baseline - base, main, want)
            cross, cross_off = want, (cross_extent - want) / 2
            return skia.Rect.MakeXYWH(pos, inner.top() + cross_off, main, cross)
        if self.align is Align.STRETCH:
            cross_off, cross = 0.0, cross_extent
        elif self.align is Align.CENTER:
            cross, cross_off = want, (cross_extent - want) / 2
        elif self.align is Align.END:
            cross, cross_off = want, cross_extent - want
        else:
            cross, cross_off = want, 0.0

        if self.horizontal:
            return skia.Rect.MakeXYWH(pos, inner.top() + cross_off, main, cross)
        return skia.Rect.MakeXYWH(inner.left() + cross_off, pos, cross, main)

    def paint(self, canvas: skia.Canvas) -> None:
        # Skip children the current clip excludes. Skia would reject their
        # draws anyway, but only after Python has walked the subtree and
        # issued every call — which for a partial repaint is most of the
        # cost the clip was meant to avoid.
        clip = canvas.getLocalClipBounds()
        for child in self._children:
            if child.paint_bounds().intersects(clip):
                child.paint(canvas)


class Row(_Axis):
    horizontal = True


class Column(_Axis):
    horizontal = False


class Spacer(Widget):
    """Flexible gap. `Spacer()` eats leftover space; `Spacer(size=8)` is a
    fixed gap."""

    def __init__(self, size: float = 0.0, flex: int = 1) -> None:
        super().__init__(flex=0 if size else flex)
        self.size = size

    def measure(self, avail: Size) -> Size:
        return Size(self.size, self.size)

    def paint(self, canvas: skia.Canvas) -> None:
        pass


class Divider(Widget):
    """Hairline across the container's cross axis."""

    def __init__(self, color: str = theme.BASE_MUTED, thickness: float = 1.0,
                 horizontal: bool = True) -> None:
        # flex=0: a divider is fixed along the main axis. Stretching it
        # across the container is the *cross* axis, which Align.STRETCH
        # handles — giving it flex makes it swallow leftover main-axis
        # space and paint as a block.
        super().__init__(flex=0)
        self.color = color
        self.thickness = thickness
        self.horizontal = horizontal

    def measure(self, avail: Size) -> Size:
        if self.horizontal:
            return Size(0.0, self.thickness)
        return Size(self.thickness, 0.0)

    def paint(self, canvas: skia.Canvas) -> None:
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(self.color))
        canvas.drawRect(self.rect, paint)


class Box(Widget):
    """A single child plus decoration: padding, background, beveled border.

    Hit-testing uses the beveled path rather than the bounding rect, so a
    click in a cut corner falls through to whatever is behind — the
    clickable area matches the drawn shape exactly.
    """

    def __init__(self, child: Widget | None = None, *,
                 padding: Insets = Insets(),
                 background: str | None = None,
                 border: str | None = None,
                 border_width: float = 1.0,
                 bevel: int = 0,
                 bevel_corners: shapes.Corners = shapes.ALL_CORNERS,
                 hover_background: str | None = None,
                 min_size: Size | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.child = child
        self.padding = padding
        self.background = background
        self.border = border
        self.border_width = border_width
        self.bevel = bevel
        self.bevel_corners = bevel_corners
        self.hover_background = hover_background
        self.min_size = min_size

    def children(self):
        return (self.child,) if self.child is not None else ()

    def measure(self, avail: Size) -> Size:
        inner = Size(max(0.0, avail.width - self.padding.horizontal),
                     max(0.0, avail.height - self.padding.vertical))
        got = self.child.measure(inner) if self.child else Size(0.0, 0.0)
        w = got.width + self.padding.horizontal
        h = got.height + self.padding.vertical
        if self.min_size is not None:
            w, h = max(w, self.min_size.width), max(h, self.min_size.height)
        return Size(w, h)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.child is not None:
            self.child.arrange(self.padding.deflate(rect))

    def path(self) -> skia.Path:
        return shapes.beveled(
            self.rect.left(), self.rect.top(),
            self.rect.width(), self.rect.height(),
            bevel=self.bevel, corners=self.bevel_corners)

    def paint(self, canvas: skia.Canvas) -> None:
        bg = self.background
        if self.hovered and self.hover_background:
            bg = self.hover_background
        if bg or self.border:
            path = self.path()
            if bg:
                fill = skia.Paint(AntiAlias=True)
                fill.setColor(theme.color(bg))
                canvas.drawPath(path, fill)
            if self.border:
                stroke = skia.Paint(AntiAlias=True)
                stroke.setColor(theme.color(self.border))
                stroke.setStyle(skia.Paint.kStroke_Style)
                stroke.setStrokeWidth(self.border_width)
                canvas.drawPath(
                    shapes.beveled(self.rect.left(), self.rect.top(),
                                   self.rect.width(), self.rect.height(),
                                   bevel=self.bevel, corners=self.bevel_corners,
                                   inset=self.border_width / 2),
                    stroke)
        if self.child is not None \
                and self.child.paint_bounds().intersects(
                    canvas.getLocalClipBounds()):
            self.child.paint(canvas)

    def hit(self, x: float, y: float) -> Widget | None:
        if self.bevel and not self.path().contains(x, y):
            return None
        return super().hit(x, y)


class Brackets(Box):
    """A child framed by four corner brackets — the battery meter's
    `[    ]` silhouette as a container.

    Only the corners are drawn, so it groups without boxing: a cluster of
    meters reads as one instrument without the weight of a full border.
    Click handlers given here cover the brackets' whole footprint.
    """

    def __init__(self, child: Widget | None = None, *,
                 color: str = theme.CYAN_DIM, arm: int = 6, thick: int = 1,
                 padding: Insets = Insets.xy(8, 4), **kwargs) -> None:
        super().__init__(child, padding=padding, **kwargs)
        self.color = color
        self.arm = arm
        self.thick = thick

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        # Snapped: a 1px arm at a half-pixel offset lands as two faint ones.
        x, y = round(r.left()), round(r.top())
        w, h = round(r.width()), round(r.height())
        a, t = self.arm, self.thick
        arms = skia.Path()
        for rx, ry, rw, rh in (
            (0, 0, a, t), (0, 0, t, a),
            (w - a, 0, a, t), (w - t, 0, t, a),
            (0, h - t, a, t), (0, h - a, t, a),
            (w - a, h - t, a, t), (w - t, h - a, t, a),
        ):
            arms.addRect(skia.Rect.MakeXYWH(x + rx, y + ry, rw, rh))
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(self.color))
        canvas.drawPath(arms, paint)
        if self.child is not None:
            self.child.paint(canvas)
