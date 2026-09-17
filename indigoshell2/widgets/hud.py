"""Shared HUD chrome for panel popups.

`HudCard` is a translucent plate in the popup's bevel language, with a
left-edge accent stripe that wraps the bottom-left corner cut rather than
ending square — the detail that makes the cards read as one HUD.
"""

import skia

from .. import shapes, text, theme
from .base import Insets, Size, Widget
from .label import Label
from .layout import Align, Box, Row, Spacer

BEVEL = 14
STRIPE = 2.0
CORNERS = ("top-right", "bottom-left")


KEY_SIZE = theme.FONT_SIZE - 3
KEY_TRACKING = 1.0


def key_label(value: str, *, width_chars: int = 0, bold: bool = False) -> Label:
    """The cyan `KEY` role used down the left of every card row.

    v1 styled this `.label-key { color: CYAN_DIM; font-size: 11px;
    letter-spacing: 1px }` and the first port reproduced it faithfully,
    tracking aside. It turns out that was never readable: CYAN_DIM on the
    card plate is about 2.1:1, well under the 4.5:1 body-text floor, and
    11px gives the eye nothing to work with. So this deliberately departs
    from v1 — CYAN_MID lifts it to ~7.3:1 at the panel's own body size.

    `width_chars` reserves a fixed cell the way v1's `set_width_chars(5)`
    did, so a column of keys of different lengths leaves whatever follows
    them starting at the same x.
    """
    return Label(value, size=KEY_SIZE, color=theme.CYAN_MID, bold=bold,
                 tracking=KEY_TRACKING,
                 min_width=text.advance(KEY_SIZE, bold, width_chars)
                 if width_chars else 0.0)


def meta_label(value: str = "", *, color: str = theme.BASE_MUTED) -> Label:
    """Secondary text sitting beside a key — a GPU model, a RAM total, a
    mount point. v1 drew these as plain labels at the card's body size,
    so they take neither the key colour nor its tracking; styling them as
    keys stretches a long value out and miscolours it as a heading."""
    return Label(value, size=KEY_SIZE, color=color)


def value_label(text="", *, color: str = theme.YELLOW_BRIGHT,
                bold: bool = True, align: str = "right") -> Label:
    return Label(text, size=theme.FONT_SIZE - 3, bold=bold,
                 color=color, align=align)


def section_header(title: str, subtitle: str = "") -> Widget:
    """`TITLE  subtitle` — cyan title, dim subtitle, no rail."""
    cells: list[Widget] = [
        Label(title, size=theme.FONT_SIZE - 5, bold=True,
              color=theme.YELLOW_BRIGHT),
    ]
    if subtitle:
        cells.append(Label(subtitle, size=theme.FONT_SIZE - 5,
                           color=theme.BASE_MUTED))
    cells.append(Spacer())
    return Row(cells, spacing=10, align=Align.CENTER)


class HudCard(Box):
    def __init__(self, child: Widget, *, accent: str = theme.HIGHLIGHT,
                 fill: str | None = None, fill_alpha: float = 0.6,
                 **kwargs) -> None:
        super().__init__(child,
                         padding=Insets(top=10, right=14, bottom=10, left=16),
                         bevel=BEVEL, bevel_corners=CORNERS, **kwargs)
        self.accent = accent
        self.fill = fill or theme.BASE_BLACK
        self.fill_alpha = fill_alpha

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        x, y, w, h = r.left(), r.top(), r.width(), r.height()

        plate = skia.Paint(AntiAlias=True)
        plate.setColor(theme.color(self.fill, self.fill_alpha))
        canvas.drawPath(shapes.beveled(x, y, w, h, bevel=BEVEL,
                                       corners=CORNERS), plate)

        # Accent band down the left edge, following the bottom-left bevel
        # diagonal so it wraps the corner instead of stopping square.
        s, b = STRIPE, BEVEL
        stripe = skia.Path()
        stripe.moveTo(x, y)
        stripe.lineTo(x + s, y)
        stripe.lineTo(x + s, y + h - b)
        stripe.lineTo(x + s + b, y + h)
        stripe.lineTo(x + b, y + h)
        stripe.lineTo(x, y + h - b)
        stripe.close()
        band = skia.Paint(AntiAlias=True)
        band.setColor(theme.color(self.accent, 0.95))
        canvas.drawPath(stripe, band)

        if self.child is not None:
            self.child.paint(canvas)
