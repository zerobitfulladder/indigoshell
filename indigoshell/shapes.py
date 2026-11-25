"""Shared geometry.

The beveled rectangle is the shell's signature outline — corners sliced
at 45° rather than rounded. Returned as a `skia.Path` rather than
stroked in place, so one path can stroke the border, clip the backdrop
and hit-test the widget.

The square light is the shell's only dot: never a circle (the user's
rule). Wherever a dot says something — a state, a radio item, the
newest sample, a list bullet — it is the network's light: a 5 px square,
filled when on, its 1 px outline when off.
"""

import skia

from . import text

Corners = tuple[str, ...]
ALL_CORNERS: Corners = ("top-left", "top-right", "bottom-left", "bottom-right")


def beveled(x: float, y: float, w: float, h: float, *,
            bevel: int, corners: Corners = ALL_CORNERS,
            inset: float = 0.0) -> skia.Path:
    """Closed beveled rectangle, clockwise from the top-left.

    `inset` shrinks the rect uniformly, so a stroke of width N drawn at
    inset N/2 stays fully inside the widget's allocation instead of
    straddling its edge and getting clipped.
    """
    x0, y0 = x + inset, y + inset
    x1, y1 = x + w - inset, y + h - inset
    b = min(bevel, int((x1 - x0) // 2), int((y1 - y0) // 2))

    path = skia.Path()
    if b <= 0 or not corners:
        path.addRect(skia.Rect.MakeLTRB(x0, y0, x1, y1))
        return path

    if "top-left" in corners:
        path.moveTo(x0, y0 + b)
        path.lineTo(x0 + b, y0)
    else:
        path.moveTo(x0, y0)
    if "top-right" in corners:
        path.lineTo(x1 - b, y0)
        path.lineTo(x1, y0 + b)
    else:
        path.lineTo(x1, y0)
    if "bottom-right" in corners:
        path.lineTo(x1, y1 - b)
        path.lineTo(x1 - b, y1)
    else:
        path.lineTo(x1, y1)
    if "bottom-left" in corners:
        path.lineTo(x0 + b, y1)
        path.lineTo(x0, y1 - b)
    else:
        path.lineTo(x0, y1)
    path.close()
    return path


LIGHT = 5           # a light's side, px: the network's two lights


def light(x: float, cy: float, side: int = LIGHT) -> skia.Rect:
    """A light's square: `side` px, its left edge at `x`, centred on
    `cy`, on whole pixels so its edges stay crisp."""
    return skia.Rect.MakeXYWH(round(x), round(cy - side / 2), side, side)


def paint_light(canvas: skia.Canvas, rect: skia.Rect, color: int, *,
                on: bool = True) -> None:
    """Fill a light's square when it is on; when off, its 1 px outline."""
    paint = skia.Paint(AntiAlias=True, Color=color)
    if on:
        canvas.drawRect(rect, paint)
        return
    paint.setStyle(skia.Paint.kStroke_Style)
    paint.setStrokeWidth(1.0)
    canvas.drawRect(rect.makeInset(0.5, 0.5), paint)


def text_light(canvas: skia.Canvas, x: float, baseline: float, size: float,
               color: int, *, on: bool = True) -> None:
    """A light where text would put a dot glyph (● ○ •): centred in the
    text cell at `x`, half the cap height above `baseline` — where the
    network's lights sit by their name."""
    cell = text.advance(size)
    paint_light(canvas, light(x + (cell - LIGHT) / 2, baseline - 0.35 * size),
                color, on=on)
