"""Shared geometry.

The beveled rectangle is the shell's signature outline — corners sliced
at 45° rather than rounded. Returned as a `skia.Path` rather than
stroked in place, so one path can stroke the border, clip the backdrop
and hit-test the widget.
"""

import skia

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
