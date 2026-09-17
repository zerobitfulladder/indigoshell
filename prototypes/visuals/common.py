"""Shared helpers for the visual proposal sheets.

Everything here renders offline on CPU raster — no X, no daemon. The
real widget classes are painted with faked state for the "current" rows;
the alternatives are sketches that reuse the theme, font and shapes.
"""
import logging
import math
logging.disable(logging.WARNING)

import skia

from indigoshell2 import shapes, text as T, theme
from indigoshell2.widgets.base import Size

BAR_H = theme.BAR_HEIGHT
PAD = 16


def C(spec, alpha=None):
    return theme.color(spec, alpha)


def fill_rect(c, x, y, w, h, color, alpha=None, aa=False):
    p = skia.Paint(AntiAlias=aa)
    p.setColor(C(color, alpha))
    c.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


def stroke_path(c, path, color, width=1.0, alpha=None, cap=None):
    p = skia.Paint(AntiAlias=True)
    p.setColor(C(color, alpha))
    p.setStyle(skia.Paint.kStroke_Style)
    p.setStrokeWidth(width)
    if cap is not None:
        p.setStrokeCap(cap)
    c.drawPath(path, p)


def fill_path(c, path, color, alpha=None):
    p = skia.Paint(AntiAlias=True)
    p.setColor(C(color, alpha))
    c.drawPath(path, p)


def glow_rect(c, x, y, w, h, color, sigma, alpha=0.55):
    p = skia.Paint(AntiAlias=True)
    p.setColor(C(color, alpha))
    p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma))
    c.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


def txt(c, s, x, baseline, size, color, bold=False, tracking=0.0, alpha=None):
    T.draw(c, s, x, baseline, size, C(color, alpha), bold, tracking)


def tw(s, size, bold=False, tracking=0.0):
    return T.measure(s, size, bold, tracking)


def ascent(size, bold=False):
    return -T.font(size, bold).getMetrics().fAscent


def cap_center_baseline(y, h, size, bold=False):
    """Baseline that centres the font's ink band in a box of height h."""
    m = T.font(size, bold).getMetrics()
    return y + h / 2 - (m.fTop + m.fBottom) / 2


def arc(c, cx, cy, r, start, sweep, color, width, alpha=None, round_cap=True):
    path = skia.Path()
    path.addArc(skia.Rect.MakeLTRB(cx - r, cy - r, cx + r, cy + r), start, sweep)
    stroke_path(c, path, color, width, alpha,
                skia.Paint.kRound_Cap if round_cap else skia.Paint.kButt_Cap)


def ticks_h(c, x, y, w, h, *, tick, gap, pct, lit, dim, dim_alpha=None):
    from indigoshell2.widgets.meters import _ticks
    _ticks(c, x, y, w, h, tick=tick, gap=gap, pct=pct, lit=lit, dim=dim,
           dim_alpha=dim_alpha)


def brackets(c, x, y, w, h, color, arm=4, thick=1):
    path = skia.Path()
    a, t = arm, thick
    for rx, ry, rw, rh in (
        (0, 0, a, t), (0, 0, t, a),
        (w - a, 0, a, t), (w - t, 0, t, a),
        (0, h - t, a, t), (0, h - a, t, a),
        (w - a, h - t, a, t), (w - t, h - a, t, a),
    ):
        path.addRect(skia.Rect.MakeXYWH(x + rx, y + ry, rw, rh))
    fill_path(c, path, color)


# ── bar strips ──────────────────────────────────────────────────────────
def bar_strip(width, paint_fn):
    """A bar-height slice with the bar's background and its top rule."""
    width = int(math.ceil(width))
    s = skia.Surface(width, BAR_H)
    c = s.getCanvas()
    c.clear(C(theme.BAR_BG))
    glow_rect(c, 0, 0, width, theme.BAR_RULE_THICK, theme.BAR_RULE,
              theme.BAR_RULE_GLOW)
    fill_rect(c, 0, 0, width, theme.BAR_RULE_THICK, theme.BAR_RULE)
    paint_fn(c)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def render_widget(widget, extra_w=0):
    """Measure/arrange/paint a real widget as the bar's Row would: its
    natural width, centred vertically."""
    size = widget.measure(Size(1000, BAR_H))
    w = size.width + 2 * PAD + extra_w

    def paint(c):
        y = (BAR_H - size.height) / 2
        widget.arrange(skia.Rect.MakeXYWH(PAD, y, size.width, size.height))
        widget.paint(c)
    return bar_strip(w, paint)


class Proto:
    """A sketch: natural size plus a paint(canvas, x, y) at that origin."""
    def __init__(self, w, h, paint):
        self.w, self.h, self.paint = w, h, paint


def render_proto(proto):
    def paint(c):
        proto.paint(c, PAD, (BAR_H - proto.h) / 2)
    return bar_strip(proto.w + 2 * PAD, paint)


# ── sheets ──────────────────────────────────────────────────────────────
def sheet(rows, out, *, scale=3, label_w=190, gap=8, title=None):
    """rows: [(label, image)] stacked vertically, images at `scale`."""
    lw = label_w
    w = lw + max(img.width() for _, img in rows) * scale + gap
    top = 34 if title else gap
    h = top + sum(img.height() * scale + gap for _, img in rows)
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    nearest = skia.SamplingOptions(skia.FilterMode.kNearest)
    if title:
        txt(c, title, gap, 24, 15, theme.HUD_FG_BRIGHT, bold=True)
    y = top
    for label, img in rows:
        ih = img.height() * scale
        # label, wrapped on "|" for a second line
        lines = label.split("|")
        by = y + ih / 2 - (len(lines) - 1) * 8
        for i, line in enumerate(lines):
            txt(c, line, gap, by + i * 16 + 5, 12 if i == 0 else 11,
                theme.HUD_FG_BRIGHT if i == 0 else theme.HUD_FG,
                bold=(i == 0))
        c.save()
        c.translate(lw, y)
        c.scale(scale, scale)
        c.drawImage(img, 0, 0, nearest)
        c.restore()
        y += ih + gap
    s.flushAndSubmit()
    s.makeImageSnapshot().save(out, skia.kPNG)
    print("wrote", out, f"{w}x{h}")
