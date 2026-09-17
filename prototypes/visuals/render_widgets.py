"""Proposal sheets for the bar widgets: current look + alternatives.

    PYTHONPATH=../.. python render_widgets.py
"""
import math
import random

import skia

from common import (BAR_H, C, Proto, arc, ascent, bar_strip, brackets,
                    cap_center_baseline, fill_path, fill_rect, glow_rect,
                    render_proto, render_widget, sheet, stroke_path, ticks_h,
                    tw, txt)
from indigoshell2 import shapes, text as T, theme
from indigoshell2.widgets.media import Media
from indigoshell2.widgets.meters import StatMeter
from indigoshell2.widgets.network import Network
from indigoshell2.widgets.volume import Volume
from indigoshell2.widgets.workspaces import Workspaces

rng = random.Random(7)

# ═══════════════════════════════════════════════════════════════════════
# Workspaces — 6 desktops, current = 3rd, windows per desktop as below
# ═══════════════════════════════════════════════════════════════════════
WS_N, WS_CUR, WS_PER = 6, 2, [2, 0, 3, 1, 0, 4]


def ws_current():
    w = Workspaces(size=22, spacing=1)
    w._count, w._current = WS_N, WS_CUR
    w._per_desktop, w._urgent = WS_PER, [False] * WS_N
    return render_widget(w)


def ws_tabs():
    """Chamfered chips with numbers; the current one filled + underlined."""
    cw, ch, gap, bevel = 20, 15, 4, 4

    def paint(c, x, y):
        for i in range(WS_N):
            cx = x + i * (cw + gap)
            path = shapes.beveled(cx, y, cw, ch, bevel=bevel,
                                  corners=("top-right",))
            cur = i == WS_CUR
            occ = WS_PER[i] > 0
            if cur:
                fill_path(c, path, theme.MAGENTA_MID)
                fg = theme.BASE_BLACK
            else:
                stroke_path(c, shapes.beveled(cx, y, cw, ch, bevel=bevel,
                                              corners=("top-right",), inset=0.5),
                            theme.YELLOW_DIM if occ else theme.MAGENTA_DIM, 1.0)
                fg = theme.YELLOW_DIM if occ else theme.MAGENTA_DIM
            s = str(i + 1)
            txt(c, s, cx + (cw - tw(s, 10, True)) / 2,
                cap_center_baseline(y, ch, 10, True), 10, fg, bold=True)
            if cur:
                glow_rect(c, cx, y + ch + 2, cw, 2, theme.CYAN_BRIGHT, 2.5)
                fill_rect(c, cx, y + ch + 2, cw, 2, theme.CYAN_BRIGHT)
        # window-count pips under occupied chips
        for i in range(WS_N):
            if i == WS_CUR or WS_PER[i] == 0:
                continue
            cx = x + i * (cw + gap)
            for k in range(min(WS_PER[i], 4)):
                fill_rect(c, cx + 2 + k * 4, y + ch + 3, 2, 1, theme.YELLOW_DIM)
    return Proto(WS_N * cw + (WS_N - 1) * gap, ch + 5, paint)


def ws_hex():
    """Terminal readout: 01 02 [03] 04 …, rail with a cursor segment."""
    size, gap = 12, 10
    cellw = tw("00", size, True)
    bw = tw("[", size)

    def paint(c, x, y):
        base = y + 13
        for i in range(WS_N):
            cx = x + i * (cellw + gap + 2 * bw)
            cur = i == WS_CUR
            occ = WS_PER[i] > 0
            s = f"{i + 1:02d}"
            if cur:
                txt(c, "[", cx, base, size, theme.CYAN_BRIGHT)
                txt(c, s, cx + bw, base, size, theme.MAGENTA_BRIGHT, bold=True)
                txt(c, "]", cx + bw + cellw, base, size, theme.CYAN_BRIGHT)
            else:
                txt(c, s, cx + bw, base, size,
                    theme.YELLOW_DIM if occ else theme.MAGENTA_DIM, bold=occ)
                for k in range(min(WS_PER[i], 4)):
                    fill_rect(c, cx + bw + k * 4, y, 2, 2, theme.YELLOW_DIM)
        total = WS_N * (cellw + 2 * bw) + (WS_N - 1) * gap
        fill_rect(c, x, y + 18, total, 1, theme.MAGENTA_DIM)
        cx = x + WS_CUR * (cellw + gap + 2 * bw)
        fill_rect(c, cx, y + 17, cellw + 2 * bw, 3, theme.CYAN_BRIGHT)
    return Proto(WS_N * (cellw + 2 * bw) + (WS_N - 1) * gap, 21, paint)


def ws_ticks():
    """Signal-meter ticks: height = windows, current = tall magenta + cyan cap."""
    tk, gap, H = 5, 6, 20

    def paint(c, x, y):
        total = WS_N * tk + (WS_N - 1) * gap
        fill_rect(c, x, y + H + 1, total, 1, theme.MAGENTA_DIM)
        for i in range(WS_N):
            cx = x + i * (tk + gap)
            cur = i == WS_CUR
            n = min(WS_PER[i], 4)
            if cur:
                fill_rect(c, cx, y, tk, H, theme.MAGENTA_BRIGHT)
                glow_rect(c, cx, y, tk, 2, theme.CYAN_BRIGHT, 2.0)
                fill_rect(c, cx, y, tk, 2, theme.CYAN_BRIGHT)
                fill_rect(c, cx - 1, y + H + 3, tk + 2, 1, theme.CYAN_BRIGHT)
            elif n:
                h = 5 + 4 * n
                fill_rect(c, cx, y + H - h, tk, h, theme.YELLOW_DIM)
            else:
                fill_rect(c, cx, y + H - 3, tk, 3, theme.MAGENTA_DIM)
    return Proto(WS_N * tk + (WS_N - 1) * gap, H + 4, paint)


def ws_rail():
    """One segmented rail; the current segment is a bright cursor block."""
    seg, gap, H = 26, 3, 6

    def paint(c, x, y):
        for i in range(WS_N):
            cx = x + i * (seg + gap)
            cur = i == WS_CUR
            occ = WS_PER[i] > 0
            if cur:
                glow_rect(c, cx, y + 6, seg, H, theme.MAGENTA_BRIGHT, 3.0)
                fill_rect(c, cx, y + 6, seg, H, theme.MAGENTA_BRIGHT)
                fill_rect(c, cx + seg / 2 - 1, y + 1, 2, 3, theme.CYAN_BRIGHT)
            else:
                fill_rect(c, cx, y + 8, seg, 2,
                          theme.YELLOW_DIM if occ else theme.MAGENTA_DIM)
            for k in range(min(WS_PER[i], 4)):
                if not cur:
                    fill_rect(c, cx + 2 + k * 5, y + 13, 3, 2, theme.YELLOW_DIM)
            s = str(i + 1)
            txt(c, s, cx + (seg - tw(s, 8)) / 2, y + 26, 8,
                theme.CYAN_MID if cur else theme.BASE_MUTED)
    return Proto(WS_N * seg + (WS_N - 1) * gap, 27, paint)


# ═══════════════════════════════════════════════════════════════════════
# Gauges — CPU 42%, RAM 63%, TEMP 71°
# ═══════════════════════════════════════════════════════════════════════
GAUGES = [
    # label, value text, pct, bright, dim, label colour
    ("CPU", "42%", 42, theme.CYAN_BRIGHT, theme.CYAN_DIM, theme.CYAN_DIM),
    ("RAM", "63%", 63, theme.VIOLET_BRIGHT, theme.VIOLET_DIM, theme.VIOLET),
    ("TEMP", "71°", 68, theme.YELLOW_BRIGHT, theme.CYAN_DIM, theme.CYAN_DIM),
]
HIST = {lab: [max(0, min(100, pct + rng.gauss(0, 9) + 14 * math.sin(i / 5)))
              for i in range(36)] for lab, _, pct, *_ in GAUGES}


def cluster(protos, spacing=20):
    w = sum(p.w for p in protos) + spacing * (len(protos) - 1)
    h = max(p.h for p in protos)

    def paint(c, x, y):
        for p in protos:
            p.paint(c, x, y + (h - p.h) / 2)
            x += p.w + spacing
    return Proto(w, h, paint)


def g_current():
    cpu = StatMeter("CPU", lambda: 42.0)
    ram = StatMeter("RAM", lambda: 63.0, bright_color=theme.VIOLET_BRIGHT,
                    dim_color=theme.VIOLET_DIM, label_color=theme.VIOLET,
                    value_color=theme.VIOLET_BRIGHT)
    temp = StatMeter("TEMP", lambda: 71.0, value_format="{:.0f}°",
                     dim_color=theme.CYAN_DIM, label_color=theme.CYAN_DIM,
                     to_pct=lambda t: (t - 30) * (100 / 60),
                     gradient=((0.0, theme.CYAN_BRIGHT), (0.5, theme.YELLOW_BRIGHT),
                               (0.8, theme.ERROR)))
    from indigoshell2.widgets.base import Size
    protos = []
    for w in (cpu, ram, temp):
        s = w.measure(Size(1000, BAR_H))
        def paint(c, x, y, w=w, s=s):
            w.arrange(skia.Rect.MakeXYWH(x, y, s.width, s.height))
            w.paint(c)
        protos.append(Proto(s.width, s.height, paint))
    return render_proto(cluster(protos))


def g_dials():
    """270° arc dial with the value beside it and the label under."""
    def one(lab, val, pct, bright, dim, labc):
        r = 13
        vsize, lsize = T.pt(12), T.pt(8)
        vw = max(tw(val, vsize, True), tw(lab, lsize, False, 1.0))
        w = 2 * r + 6 + vw

        def paint(c, x, y):
            cx, cy = x + r + 1, y + r + 1
            arc(c, cx, cy, r, 135, 270, dim, 3, 0.8, round_cap=False)
            arc(c, cx, cy, r, 135, 270 * pct / 100, bright, 3, round_cap=False)
            # needle dot at the head
            a = math.radians(135 + 270 * pct / 100)
            fill_rect(c, cx + math.cos(a) * r - 1.5, cy + math.sin(a) * r - 1.5,
                      3, 3, theme.HUD_FG_BRIGHT, aa=True)
            tx = x + 2 * r + 7
            txt(c, val, tx, y + 14, vsize, bright, bold=True)
            txt(c, lab, tx, y + 26, lsize, labc, tracking=1.0)
        return Proto(w, 2 * r + 3, paint)
    return render_proto(cluster([one(*g) for g in GAUGES], 18))


def g_brackets():
    """Key/value line over a bracket-framed bar with a peak marker."""
    def one(lab, val, pct, bright, dim, labc):
        W, BH = 72, 7
        lsize, vsize = T.pt(8), T.pt(12)

        def paint(c, x, y):
            txt(c, lab, x + 1, y + ascent(lsize) , lsize, labc, tracking=1.5)
            txt(c, val, x + W - tw(val, vsize, True), y + ascent(vsize, True),
                vsize, bright, bold=True)
            by = y + 16
            brackets(c, x, by, W, BH + 6, dim, arm=4, thick=1)
            ticks_h(c, x + 4, by + 3, W - 8, BH, tick=3, gap=1, pct=pct,
                    lit=bright, dim=dim, dim_alpha=0.6)
            peak = min(100, pct + 22) / 100
            px = x + 4 + (W - 8) * peak
            fill_rect(c, px, by + 1, 1, BH + 4, theme.YELLOW_BRIGHT)
        return Proto(W, 16 + BH + 6, paint)
    return render_proto(cluster([one(*g) for g in GAUGES], 18))


def g_spark():
    """Value + label on one line over a one-minute sparkline."""
    def one(lab, val, pct, bright, dim, labc):
        W, SH = 74, 12
        lsize, vsize = T.pt(8), T.pt(12)
        hist = HIST[lab]

        def paint(c, x, y):
            txt(c, val, x, y + ascent(vsize, True), vsize, bright, bold=True)
            txt(c, lab, x + tw(val, vsize, True) + 5, y + ascent(vsize, True) - 1,
                lsize, labc, tracking=1.0)
            sy = y + 17
            fill_rect(c, x, sy + SH, W, 1, dim, 0.8)
            pts = [(x + i * W / (len(hist) - 1), sy + SH - SH * v / 100)
                   for i, v in enumerate(hist)]
            area = skia.Path()
            area.moveTo(pts[0][0], sy + SH)
            for px, py in pts:
                area.lineTo(px, py)
            area.lineTo(pts[-1][0], sy + SH)
            area.close()
            fill_path(c, area, bright, 0.18)
            line = skia.Path()
            line.moveTo(*pts[0])
            for p in pts[1:]:
                line.lineTo(*p)
            stroke_path(c, line, bright, 1.2)
            fill_rect(c, pts[-1][0] - 1.5, pts[-1][1] - 1.5, 3, 3, theme.HUD_FG_BRIGHT, aa=True)
        return Proto(W, 17 + SH + 1, paint)
    return render_proto(cluster([one(*g) for g in GAUGES], 18))


def g_columns():
    """Icon + vertical bar per stat, mixer-style, values as tiny caps."""
    ICON = {"CPU": "\uf2db", "RAM": "\U000f035b", "TEMP": "\uf2c9"}

    def one(lab, val, pct, bright, dim, labc):
        W, BH, BW = 34, 22, 6
        vsize, isize = T.pt(8), 12

        def paint(c, x, y):
            bx = x + W - BW
            n = 7
            for k in range(n):
                yy = y + BH - (k + 1) * 3
                fill_rect(c, bx, yy, BW, 2,
                          bright if (k + 0.5) / n <= pct / 100 else dim,
                          None if (k + 0.5) / n <= pct / 100 else 0.7)
            txt(c, ICON[lab], x, y + 12, isize, labc)
            txt(c, val, x, y + BH + 1, vsize, bright, bold=True)
        return Proto(W, BH + 3, paint)
    return render_proto(cluster([one(*g) for g in GAUGES], 14))


# ═══════════════════════════════════════════════════════════════════════
# Media — "Tears In Rain — Vangelis" over a cava frame
# ═══════════════════════════════════════════════════════════════════════
TITLE = "Tears In Rain — Vangelis"
BANDS = tuple(int(1000 + 9000 * (0.35 + 0.65 * abs(math.sin(i * 0.55 + 0.7))
                                * (1 - i / 40))) for i in range(30))
M_W = None


def m_current():
    global M_W
    m = Media(player="x", max_chars=26, size=theme.FONT_SIZE_LG,
              baseline_shift=1.0, show_cava_bg=True, beat_pulse=True)
    m._title, m._title_w = TITLE, tw(TITLE, theme.FONT_SIZE_LG, True)
    m._bands, m._peak, m._active = BANDS, float(max(BANDS)), True
    from indigoshell2.widgets.base import Size
    M_W = m.measure(Size(1000, BAR_H)).width
    return render_widget(m)


def _title_over(c, x, y, w, size=theme.FONT_SIZE_LG, color=theme.MUSIC_FG,
                bold=True, dy=1.0):
    m = T.font(size, bold).getMetrics()
    base = y + BAR_H / 2 - (m.fTop + m.fBottom) / 2 + dy
    txt(c, TITLE, x + (w - tw(TITLE, size, bold)) / 2, base, size, color, bold=bold)


def m_mirror():
    """Bars mirrored about the centre line, brighter with height."""
    def paint(c, x, y):
        n = len(BANDS)
        bw = (M_W - (n - 1)) / n
        peak = max(BANDS)
        cy = y + BAR_H / 2
        for i, b in enumerate(BANDS):
            h = max(1, b / peak * (BAR_H / 2 - 2))
            col = theme.lerp(theme.MAGENTA_DIM, theme.MAGENTA_MID, min(0.55, h / 20))
            fill_rect(c, x + i * (bw + 1), cy - h, bw, 2 * h, col, 0.8)
        fill_rect(c, x, cy, M_W, 1, theme.CYAN_BRIGHT, 0.35)
        _title_over(c, x, y, M_W)
    return Proto(M_W, BAR_H, paint)


def m_scope():
    """Oscilloscope trace with a glow, faint grid, title over it."""
    def paint(c, x, y):
        for gx in range(0, int(M_W), 16):
            fill_rect(c, x + gx, y + 2, 1, BAR_H - 2, theme.MAGENTA_DIM, 0.5)
        cy = y + BAR_H / 2
        n = len(BANDS)
        peak = max(BANDS)
        pts = []
        for i in range(n * 3):
            b = BANDS[(i // 3) % n]
            amp = b / peak * 14 * math.sin(i * 1.9)
            pts.append((x + i * M_W / (n * 3 - 1), cy + amp))
        path = skia.Path()
        path.moveTo(*pts[0])
        for p in pts[1:]:
            path.lineTo(*p)
        glow = skia.Paint(AntiAlias=True)
        glow.setColor(C(theme.MAGENTA_BRIGHT, 0.55))
        glow.setStyle(skia.Paint.kStroke_Style)
        glow.setStrokeWidth(3)
        glow.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 2.5))
        c.drawPath(path, glow)
        stroke_path(c, path, theme.MAGENTA_MID, 1.3)
        _title_over(c, x, y, M_W)
    return Proto(M_W, BAR_H, paint)


def m_plate():
    """HUD plate: bevelled card, accent stripe, PLAYING tag, bars inside."""
    def paint(c, x, y):
        py, ph = y + 5, BAR_H - 10
        plate = shapes.beveled(x, py, M_W, ph, bevel=8,
                               corners=("top-right", "bottom-left"))
        fill_path(c, plate, theme.BASE_SHADOW, 0.9)
        c.save()
        c.clipPath(plate, True)
        n = len(BANDS)
        bw = (M_W - (n - 1)) / n
        peak = max(BANDS)
        for i, b in enumerate(BANDS):
            h = max(1, b / peak * ph)
            fill_rect(c, x + i * (bw + 1), py + ph - h, bw, h, theme.MAGENTA_DIM, 0.7)
        c.restore()
        stroke_path(c, shapes.beveled(x, py, M_W, ph, bevel=8,
                                      corners=("top-right", "bottom-left"), inset=0.5),
                    theme.CYAN_DIM, 1.0)
        fill_rect(c, x, py, 2, ph - 8, theme.CYAN_BRIGHT)
        txt(c, " PLAYING", x + 8, py + 9, 7, theme.CYAN_MID, tracking=1.5)
        m = T.font(15, True).getMetrics()
        base = py + ph - 5
        txt(c, TITLE, x + (M_W - tw(TITLE, 15, True)) / 2, base, 15, theme.MUSIC_FG, bold=True)
    return Proto(M_W, BAR_H, paint)


def m_matrix():
    """LED-matrix spectrum with peak-hold dots, title over it."""
    def paint(c, x, y):
        n = len(BANDS)
        colw = M_W / n
        peak = max(BANDS)
        rows = 9
        for i, b in enumerate(BANDS):
            lit = int(round(b / peak * rows))
            hold = min(rows - 1, lit + 1 + (i % 2))
            for r in range(rows):
                yy = y + BAR_H - 3 - (r + 1) * 4
                on = r < lit
                fill_rect(c, x + i * colw + 1, yy, colw - 2, 3,
                          theme.MAGENTA_MID if on else theme.MAGENTA_DIM,
                          0.5 if on else 0.35)
            yy = y + BAR_H - 3 - (hold + 1) * 4
            fill_rect(c, x + i * colw + 1, yy, colw - 2, 3, theme.CYAN_BRIGHT, 0.55)
        _title_over(c, x, y, M_W)
    return Proto(M_W, BAR_H, paint)


# ═══════════════════════════════════════════════════════════════════════
# Volume — 72%
# ═══════════════════════════════════════════════════════════════════════
VOL = 72


def v_current():
    v = Volume()
    v._percent = float(VOL)
    return render_widget(v)


def v_wedge():
    """Speaker icon + rising wedge of cells."""
    n, cw, gap = 10, 3, 1
    H = 18

    def paint(c, x, y):
        txt(c, "", x, y + 14, 13, theme.VIOLET_BRIGHT)
        bx = x + 18
        for i in range(n):
            h = 4 + (H - 4) * i / (n - 1)
            on = (i + 0.5) / n <= VOL / 100
            fill_rect(c, bx + i * (cw + gap), y + H - h, cw, h,
                      theme.VIOLET_BRIGHT if on else theme.VIOLET_DIM)
    return Proto(18 + n * (cw + gap), H, paint)


def v_ring():
    """Ring gauge with the speaker glyph in the middle."""
    r = 10

    def paint(c, x, y):
        cx, cy = x + r + 1, y + r + 1
        arc(c, cx, cy, r, 135, 270, theme.VIOLET_DIM, 3, round_cap=False)
        arc(c, cx, cy, r, 135, 270 * VOL / 100, theme.VIOLET_BRIGHT, 3, round_cap=False)
        txt(c, "", cx - 4.5, cy + 3.5, 9, theme.VIOLET_BRIGHT)
    return Proto(2 * r + 2, 2 * r + 2, paint)


def v_rail():
    """VOL key, segmented rail with a chamfered end, numeric readout."""
    W, H = 44, 5

    def paint(c, x, y):
        txt(c, "VOL", x, y + 7, 7, theme.VIOLET, tracking=1.5)
        ry = y + 11
        ticks_h(c, x, ry, W, H, tick=3, gap=1, pct=VOL, lit=theme.VIOLET_BRIGHT,
                dim=theme.VIOLET_DIM, dim_alpha=0.8)
        cap = skia.Path()
        cap.moveTo(x + W + 2, ry)
        cap.lineTo(x + W + 5, ry)
        cap.lineTo(x + W + 5, ry + H - 3)
        cap.lineTo(x + W + 2, ry + H)
        cap.close()
        fill_path(c, cap, theme.VIOLET_DIM)
        txt(c, str(VOL), x + W + 9, ry + 6, T.pt(9), theme.VIOLET_BRIGHT, bold=True)
    return Proto(W + 9 + tw("100", T.pt(9), True), 17, paint)


def v_stack_icon():
    """Current stack, with the speaker glyph beside it and a level cap."""
    n, ct, gap, W = 8, 3, 1, 14

    def paint(c, x, y):
        txt(c, "", x, y + 20, 13, theme.VIOLET)
        bx = x + 18
        H = n * (ct + gap) - gap
        lit = int(VOL / 100 * n)
        for i in range(n):
            yy = y + H - (i + 1) * (ct + gap) + gap
            fill_rect(c, bx, yy, W, ct, theme.VIOLET_BRIGHT if i < lit else theme.VIOLET_DIM)
        yy = y + H - (lit + 1) * (ct + gap) + gap
        fill_rect(c, bx - 2, yy, W + 4, 1, theme.CYAN_BRIGHT)
    return Proto(18 + W + 2, n * (ct + gap) - gap, paint)


# ═══════════════════════════════════════════════════════════════════════
# Network — Starlink-5G / 192.168.1.42, signal 70%
# ═══════════════════════════════════════════════════════════════════════
NAME, IP, SIG = "Starlink-5G", "192.168.1.42", 70


def n_current():
    n = Network()
    n._ip.set_value(IP)
    n._name.set_value(NAME)
    return render_widget(n)


def n_signal():
    """Ascending signal bars beside the stacked name/IP."""
    def paint(c, x, y):
        for k in range(4):
            h = 4 + 3 * k
            on = (k + 1) / 4 <= SIG / 100 + 0.01
            fill_rect(c, x + k * 4, y + 15 - h, 3, h,
                      theme.CYAN_BRIGHT if on else theme.CYAN_DIM)
        tx = x + 20
        txt(c, IP, tx, y + 7, T.pt(8), theme.BASE_MUTED)
        txt(c, NAME, tx, y + 22, theme.FONT_SIZE, theme.FG)
    return Proto(20 + tw(NAME, theme.FONT_SIZE), 24, paint)


def n_rates():
    """Name over live up/down rates with tiny sparklines."""
    up = [rng.random() * 0.6 + 0.2 for _ in range(16)]
    dn = [abs(math.sin(i * 0.8)) * 0.8 + 0.1 for i in range(16)]

    def spark(c, x, y, w, h, vals, color):
        path = skia.Path()
        for i, v in enumerate(vals):
            px, py = x + i * w / (len(vals) - 1), y + h - v * h
            (path.moveTo if i == 0 else path.lineTo)(px, py)
        stroke_path(c, path, color, 1.0)

    def paint(c, x, y):
        txt(c, NAME, x, y + 12, theme.FONT_SIZE - 2, theme.FG)
        ry = y + 24
        s = T.pt(7)
        txt(c, " 1.2M", x, ry, s, theme.CYAN_MID)
        spark(c, x + 40, ry - 7, 22, 7, up, theme.CYAN_MID)
        txt(c, " 24.6M", x + 70, ry, s, theme.YELLOW_DIM)
        spark(c, x + 116, ry - 7, 22, 7, dn, theme.YELLOW_DIM)
    return Proto(max(tw(NAME, theme.FONT_SIZE - 2), 140), 26, paint)


def n_tag():
    """NET key + live dot, name, IP with the host octet highlighted."""
    def paint(c, x, y):
        # left corner brackets only, like the battery meter's frame
        fill_rect(c, x, y, 5, 1, theme.CYAN_DIM)
        fill_rect(c, x, y + 27, 5, 1, theme.CYAN_DIM)
        fill_rect(c, x, y, 1, 5, theme.CYAN_DIM)
        fill_rect(c, x, y + 23, 1, 5, theme.CYAN_DIM)
        tx = x + 9
        txt(c, "NET", tx, y + 7, 7, theme.CYAN_MID, tracking=1.5)
        dx = tx + tw("NET", 7, False, 1.5) + 5
        glow_rect(c, dx, y + 1, 4, 4, theme.LIME_BRIGHT, 2.0)
        fill_rect(c, dx, y + 1, 4, 4, theme.LIME_BRIGHT, aa=True)
        txt(c, NAME, tx, y + 19, 13, theme.FG)
        head, _, host = IP.rpartition(".")
        s = T.pt(7)
        txt(c, head + ".", tx, y + 27, s, theme.BASE_MUTED)
        txt(c, host, tx + tw(head + ".", s), y + 27, s, theme.CYAN_MID, bold=True)
    return Proto(9 + max(tw(NAME, 13), 60), 28, paint)


def n_icon_stack():
    """Wifi glyph at bar-icon size, name/IP stacked, signal as the glyph's colour."""
    def paint(c, x, y):
        txt(c, "", x, y + 18, 16, theme.CYAN_BRIGHT)
        tx = x + 24
        txt(c, NAME, tx, y + 12, theme.FONT_SIZE - 2, theme.FG)
        txt(c, IP, tx, y + 23, T.pt(7), theme.BASE_MUTED)
    return Proto(24 + tw(NAME, theme.FONT_SIZE - 2), 24, paint)


# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sheet([("current", ws_current()),
           ("A  chamfered tabs|numbered chips, cyan underline", render_proto(ws_tabs())),
           ("B  hex readout|bracketed current, cursor rail", render_proto(ws_hex())),
           ("C  signal ticks|height = windows", render_proto(ws_ticks())),
           ("D  segmented rail|cursor block, pips per window", render_proto(ws_rail()))],
          "workspaces.png", title="Workspaces")
    sheet([("current", g_current()),
           ("A  arc dials", g_dials()),
           ("B  bracketed bars|+ peak marker", g_brackets()),
           ("C  sparklines|one-minute history", g_spark()),
           ("D  mixer columns|icon + vertical cells", g_columns())],
          "gauges.png", title="Gauges (CPU / RAM / TEMP)")
    sheet([("current", m_current()),
           ("A  mirrored spectrum", render_proto(m_mirror())),
           ("B  oscilloscope", render_proto(m_scope())),
           ("C  HUD plate|PLAYING tag, accent stripe", render_proto(m_plate())),
           ("D  LED matrix|peak-hold dots", render_proto(m_matrix()))],
          "media.png", title="Media")
    sheet([("current", v_current()),
           ("A  wedge + icon", render_proto(v_wedge())),
           ("B  ring", render_proto(v_ring())),
           ("C  rail + readout", render_proto(v_rail())),
           ("D  stack + icon|level cap line", render_proto(v_stack_icon()))],
          "volume.png", title="Volume")
    sheet([("current", n_current()),
           ("A  signal bars", render_proto(n_signal())),
           ("B  live rates|up/down sparklines", render_proto(n_rates())),
           ("C  NET tag|live dot, host octet", render_proto(n_tag())),
           ("D  icon + stack", render_proto(n_icon_stack()))],
          "network.png", title="Network")
