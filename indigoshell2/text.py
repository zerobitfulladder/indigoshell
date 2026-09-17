"""Font loading and text measurement.

`drawString` is used deliberately here: it is one glyph per codepoint
with no shaping and no fallback, which is exactly right for text we own
(labels, numbers, Nerd Font icons) and exactly wrong for text that comes
from other applications. When notification bodies and MPRIS titles land,
they go through `skia.textlayout` instead — it does HarfBuzz shaping and
walks a fallback chain, so a CJK or emoji character renders instead of
turning into tofu.
"""

import logging
import os

import skia

from . import theme

log = logging.getLogger(__name__)

# Pango sizes are in *points*; Skia's are in pixels. v1 wrote
# `size='14000'` (14pt) and Pango rendered it at 14 * DPI/72, so passing
# 14 straight to Skia makes every label 25% too small at 96 DPI. Widgets
# ported from Pango markup keep their point values and convert here.
#
# 96 is Pango's default when Xft.dpi is unset, which is the case here.
DPI = float(os.environ.get("INDIGOSHELL2_DPI", 96.0))
PX_PER_PT = DPI / 72.0


def pt(points: float) -> float:
    """Point size -> device pixels, matching what Pango did in v1."""
    return points * PX_PER_PT


_typefaces: dict[bool, skia.Typeface] = {}
_fonts: dict[tuple[float, bool], skia.Font] = {}


def typeface(bold: bool = False) -> skia.Typeface:
    hit = _typefaces.get(bold)
    if hit is not None:
        return hit
    style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
    mgr = skia.FontMgr()
    for family in (theme.FONT, *theme.FONT_FALLBACKS):
        try:
            tf = mgr.matchFamilyStyle(family, style)
        except Exception:
            tf = None
        if tf is not None:
            if family != theme.FONT:
                log.warning("font %r unavailable, using %r",
                            theme.FONT, tf.getFamilyName())
            _typefaces[bold] = tf
            return tf
    raise RuntimeError(f"no usable font found (tried {theme.FONT!r} and fallbacks)")


def font(size: float, bold: bool = False) -> skia.Font:
    """Cached font. Paint code asks for the same few sizes every frame."""
    key = (size, bold)
    hit = _fonts.get(key)
    if hit is None:
        hit = skia.Font(typeface(bold), size)
        hit.setSubpixel(True)
        hit.setEdging(skia.Font.Edging.kAntiAlias)
        _fonts[key] = hit
    return hit


def measure(text: str, size: float, bold: bool = False,
            tracking: float = 0.0) -> float:
    width = font(size, bold).measureText(text)
    if tracking and len(text) > 1:
        # Gaps between glyphs only — no trailing space, so a tracked
        # label still butts up cleanly against whatever follows it.
        width += tracking * (len(text) - 1)
    return width


def advance(size: float, bold: bool = False, chars: int = 1) -> float:
    """Width of `chars` monospace cells — the Skia equivalent of GTK's
    `set_width_chars`, used to reserve a fixed-width label cell."""
    return font(size, bold).measureText("0") * chars


def line_height(size: float, bold: bool = False) -> float:
    m = font(size, bold).getMetrics()
    return m.fDescent - m.fAscent


def baseline_in(rect: skia.Rect, size: float, bold: bool = False) -> float:
    """Y coordinate that vertically centers a line of text in `rect`.

    fAscent is negative (it goes up from the baseline), so the visual
    centre sits at half the sum, not half the difference.
    """
    m = font(size, bold).getMetrics()
    return rect.centerY() - (m.fAscent + m.fDescent) / 2


def draw(canvas: skia.Canvas, text: str, x: float, baseline: float,
         size: float, color: int, bold: bool = False,
         tracking: float = 0.0) -> None:
    paint = skia.Paint(AntiAlias=True)
    paint.setColor(color)
    f = font(size, bold)
    if not tracking:
        canvas.drawString(text, x, baseline, f, paint)
        return
    # Pango's `letter_spacing` had no Skia equivalent, so tracked text is
    # laid out a glyph at a time. That drops kerning, which costs nothing
    # here: tracking is only used on short monospace all-caps keys, where
    # the font has no kern pairs to lose anyway.
    for ch in text:
        canvas.drawString(ch, x, baseline, f, paint)
        x += f.measureText(ch) + tracking
