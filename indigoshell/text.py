"""Font loading and text measurement.

`drawString` is used deliberately here: it is one glyph per codepoint
with no shaping and no fallback, which is exactly right for text we own
(labels, numbers, Nerd Font icons) and exactly wrong for text that comes
from other applications. When notification bodies and MPRIS titles land,
they go through `skia.textlayout` instead — it does HarfBuzz shaping and
walks a fallback chain, so a CJK or emoji character renders instead of
turning into tofu.

The call overlay's text is somewhere in between: it comes from the
agent, but it lives on a monospace grid where selection and copy count
one cell per character. For that, `draw`/`measure` fall back per
character to whichever installed font has a glyph the theme font lacks
(an emoji, say), and `pad_wide` gives a wide character the second cell
it needs on the grid — see `PAD`.
"""

import logging
import os
import unicodedata

import skia

from . import theme

log = logging.getLogger(__name__)

# Skia's text sizes are in pixels. A size quoted in *points* — as the
# theme's older values are — renders at `points * DPI/72`, so passing 14
# straight to Skia makes that label 25% too small at 96 DPI. Anything
# still carrying a point value converts through `pt()` below.
#
# 96 is the fallback when Xft.dpi is unset, which is the case here.
DPI = float(os.environ.get("INDIGOSHELL_DPI", 96.0))
PX_PER_PT = DPI / 72.0


def pt(points: float) -> float:
    """Point size -> device pixels."""
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


# ── characters the theme font lacks ──────────────────────────────────
# Stands in the second grid cell of a wide character (an emoji, CJK):
# a Unicode noncharacter, so it never occurs in real text. It is drawn
# as nothing, copied as nothing, and otherwise counts as a cell.
PAD = "\ufdd0"

_has: dict[tuple[bool, str], bool] = {}
_fallback: dict[str, skia.Typeface | None] = {}
_fallback_fonts: dict[tuple[str, float], skia.Font] = {}


def is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def pad_wide(value: str) -> str:
    """`value` with a PAD after every wide character (once: padding
    padded text changes nothing)."""
    if value.isascii():
        return value
    out = []
    for i, ch in enumerate(value):
        out.append(ch)
        if is_wide(ch) and value[i + 1:i + 2] != PAD:
            out.append(PAD)
    return "".join(out)


def unpad(value: str) -> str:
    return value.replace(PAD, "")


def has_glyph(ch: str, bold: bool = False) -> bool:
    key = (bold, ch)
    hit = _has.get(key)
    if hit is None:
        hit = _has[key] = font(14, bold).unicharToGlyph(ord(ch)) != 0
    return hit


def fallback_font(ch: str, size: float) -> skia.Font | None:
    """A font that has `ch`, from whatever is installed, or None."""
    if ch not in _fallback:
        tf = skia.FontMgr().matchFamilyStyleCharacter(
            "", skia.FontStyle(), ["en"], ord(ch))
        _fallback[ch] = tf
    tf = _fallback[ch]
    if tf is None:
        return None
    key = (tf.getFamilyName(), size)
    hit = _fallback_fonts.get(key)
    if hit is None:
        hit = skia.Font(tf, size)
        hit.setSubpixel(True)
        hit.setEdging(skia.Font.Edging.kAntiAlias)
        _fallback_fonts[key] = hit
    return hit


def grid_font(ch: str, size: float, width: float) -> skia.Font | None:
    """A fallback font for `ch` at `size` or smaller, so the glyph is no
    wider than `width` — its cells on the grid."""
    f = fallback_font(ch, size)
    if f is None:
        return None
    w = f.measureText(ch)
    return f if w <= width + 0.5 or w <= 0 else fallback_font(
        ch, round(size * width / w * 4) / 4)


def _runs(text: str, size: float, bold: bool) -> list[tuple[str, skia.Font]]:
    """`text` split into runs of one font: the theme font, or a fallback
    for characters it lacks. PADs are dropped."""
    main = font(size, bold)
    runs: list[tuple[str, skia.Font]] = []
    for ch in text:
        if ch == PAD:
            continue
        f = main if has_glyph(ch, bold) else (fallback_font(ch, size) or main)
        if runs and runs[-1][1] is f:
            runs[-1] = (runs[-1][0] + ch, f)
        else:
            runs.append((ch, f))
    return runs


def measure(text: str, size: float, bold: bool = False,
            tracking: float = 0.0) -> float:
    if text.isascii():
        width = font(size, bold).measureText(text)
    else:
        width = sum(f.measureText(run) for run, f in _runs(text, size, bold))
        text = unpad(text)
    if tracking and len(text) > 1:
        # Gaps between glyphs only — no trailing space, so a tracked
        # label still butts up cleanly against whatever follows it.
        width += tracking * (len(text) - 1)
    return width


def advance(size: float, bold: bool = False, chars: int = 1) -> float:
    """Width of `chars` monospace cells, used to reserve a label a
    fixed-width cell so its neighbours don't shift as digits change."""
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
    if not text.isascii():
        for run, rf in _runs(text, size, bold):
            if tracking:
                for ch in run:
                    canvas.drawString(ch, x, baseline, rf, paint)
                    x += rf.measureText(ch) + tracking
            else:
                canvas.drawString(run, x, baseline, rf, paint)
                x += rf.measureText(run)
        return
    if not tracking:
        canvas.drawString(text, x, baseline, f, paint)
        return
    # Skia has no letter-spacing knob, so tracked text is laid out a
    # glyph at a time. That drops kerning, which costs nothing here:
    # tracking is only used on short monospace all-caps keys, where the
    # font has no kern pairs to lose anyway.
    for ch in text:
        canvas.drawString(ch, x, baseline, f, paint)
        x += f.measureText(ch) + tracking


def wrap(value: str, size: float, max_w: float, bold: bool = False) -> list[str]:
    """Greedy word wrap to `max_w` px; a word longer than a line breaks
    mid-word. Newlines in `value` are kept as line breaks."""
    out: list[str] = []
    for raw in value.splitlines() or [""]:
        cur = ""
        for word in raw.split(" "):
            candidate = f"{cur} {word}" if cur else word
            if measure(candidate, size, bold) <= max_w:
                cur = candidate
                continue
            if cur:
                out.append(cur)
                cur = ""
            while len(word) > 1 and measure(word, size, bold) > max_w:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if measure(word[:mid], size, bold) <= max_w:
                        lo = mid
                    else:
                        hi = mid - 1
                out.append(word[:lo])
                word = word[lo:]
            cur = word
        out.append(cur)
    return out


def ellipsize(value: str, size: float, max_w: float, bold: bool = False) -> str:
    """`value` cut to fit `max_w` px, with an ellipsis if anything went."""
    if measure(value, size, bold) <= max_w:
        return value
    lo, hi = 0, len(value)
    while lo < hi:                      # longest prefix that fits with "…"
        mid = (lo + hi + 1) // 2
        if measure(value[:mid] + "…", size, bold) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return value[:lo].rstrip() + "…"
