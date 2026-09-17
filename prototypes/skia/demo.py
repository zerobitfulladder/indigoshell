"""Skia render of indigoshell's BatteryMeter, glow-off vs glow-on.

Ports the state table + animation logic from widgets/battery_meter.py
verbatim (sweep / blink / shimmer), renders each state side by side
(flat cairo-equivalent vs. Skia blur-mask glow), using the real INDIGO
theme palette. Emits PNG frames; ffmpeg assembles them into GIFs.

Run:  uv run --no-project --with skia-python python demo.py <scale> <outdir>
"""

import importlib.util
import math
import sys
from pathlib import Path

import skia

# ── real theme, imported straight from the project ──────────────────────
spec = importlib.util.spec_from_file_location(
    "indigo_theme", "/home/lavender/Projects/indigoshell/indigoshell/theme.py"
)
theme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(theme)

SCALE = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
OUTDIR = Path(sys.argv[2] if len(sys.argv) > 2 else "frames")
OUTDIR.mkdir(parents=True, exist_ok=True)

# ── geometry: BatteryMeter defaults ─────────────────────────────────────
CELLS, CELL_THICK, GAP = 16, 2, 2
METER_H, CORNER_ARM, CORNER_T = 22, 4, 1
PAD_X, PAD_Y = 5, 3
METER_W = CELLS * CELL_THICK + (CELLS - 1) * GAP + 2 * PAD_X


def rgb(hex_str):
    h = hex_str.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def color(c, alpha=255):
    return skia.ColorSetARGB(alpha, *c)


# ── state table: verbatim port of BatteryMeter._state_config ────────────
def state_config(percent, charging, present=True):
    if not present:
        c = rgb(theme.FG_MUTED)
        return dict(mode="static", sweep=c, lit=c, empty=c, bracket=c, dir=0, ms=0)
    if charging and percent >= 99:
        return dict(mode="shimmer", sweep=rgb(theme.LIME_BRIGHT), lit=rgb(theme.LIME_DIM),
                    empty=rgb(theme.LIME_DIM), bracket=rgb(theme.LIME_DIM), dir=0, ms=33)
    if charging:
        return dict(mode="sweep", sweep=rgb(theme.CYAN_BRIGHT), lit=rgb(theme.CYAN_DIM),
                    empty=rgb(theme.MAGENTA_DIM), bracket=rgb(theme.CYAN_DIM), dir=1, ms=70)
    if percent < 20:
        return dict(mode="blink", sweep=rgb(theme.MAGENTA_BRIGHT), lit=rgb(theme.MAGENTA_BRIGHT),
                    empty=rgb(theme.MAGENTA_DIM), bracket=rgb(theme.MAGENTA_DIM), dir=0, ms=150)
    if percent < 50:
        return dict(mode="sweep", sweep=rgb(theme.YELLOW_BRIGHT), lit=rgb(theme.YELLOW_DIM),
                    empty=rgb(theme.MAGENTA_DIM), bracket=rgb(theme.YELLOW_DIM), dir=-1, ms=90)
    return dict(mode="sweep", sweep=rgb(theme.CYAN_BRIGHT), lit=rgb(theme.CYAN_DIM),
                empty=rgb(theme.MAGENTA_DIM), bracket=rgb(theme.CYAN_DIM), dir=-1, ms=110)


class Row:
    def __init__(self, label, percent, charging):
        self.label = label
        self.percent = percent
        self.cfg = state_config(percent, charging)
        self.lit_count = max(1, int(percent / 100.0 * CELLS))
        self.sweep_pos = 0 if self.cfg["dir"] > 0 else self.lit_count
        self.blink_on = True
        self.phase = 0.0
        self.acc = 0.0

    def advance(self, dt_ms):
        cfg = self.cfg
        if cfg["ms"] <= 0:
            return
        self.acc += dt_ms
        while self.acc >= cfg["ms"]:
            self.acc -= cfg["ms"]
            if cfg["mode"] == "sweep":
                nxt = self.sweep_pos + cfg["dir"]
                if nxt < 0:
                    nxt = self.lit_count
                elif nxt > self.lit_count:
                    nxt = 0
                self.sweep_pos = nxt
            elif cfg["mode"] == "blink":
                self.blink_on = not self.blink_on
            elif cfg["mode"] == "shimmer":
                self.phase = (self.phase + 0.08) % (2 * math.pi)

    # returns (rgb, glow_strength 0..1) — the pure "which color is cell i"
    def cell(self, i):
        cfg = self.cfg
        if i >= self.lit_count:
            return cfg["empty"], 0.0
        if cfg["mode"] == "sweep":
            if i < self.sweep_pos:
                head = 1.0 if i == self.sweep_pos - 1 else 0.55
                return cfg["sweep"], head
            return cfg["lit"], 0.0
        if cfg["mode"] == "blink":
            return (cfg["sweep"], 1.0) if self.blink_on else (cfg["empty"], 0.0)
        if cfg["mode"] == "shimmer":
            t = (math.sin(self.phase + i * 0.45) + 1) / 2
            return lerp(cfg["lit"], cfg["sweep"], t), t
        return cfg["lit"], 0.0


ROWS = [
    Row("CHARGING 65%", 65, True),
    Row("BATTERY 82%", 82, False),
    Row("BATTERY 35%", 35, False),
    Row("LOW 12%", 12, False),
    Row("FULL 100% AC", 100, True),
]

# ── sheet layout (1x units) ─────────────────────────────────────────────
PAD, HEADER_H, LABEL_H, ROW_GAP = 14, 16, 12, 10
COL_GAP = 36
COL1_X = PAD
COL2_X = PAD + METER_W + COL_GAP
SHEET_W = COL2_X + METER_W + PAD
SHEET_H = PAD + HEADER_H + len(ROWS) * (LABEL_H + METER_H + ROW_GAP) + PAD


def draw_text(canvas, text, x, y, size, col, alpha=255):
    try:
        font = skia.Font()
        font.setSize(size)
        p = skia.Paint(AntiAlias=True)
        p.setColor(color(col, alpha))
        canvas.drawString(text, x, y, font, p)
    except Exception:
        pass  # labels are nice-to-have; never break the demo over fonts


def draw_meter(canvas, ox, oy, row, glow_enabled):
    p = skia.Paint(AntiAlias=False)

    # corner brackets
    p.setColor(color(row.cfg["bracket"]))
    a, t, w, h = CORNER_ARM, CORNER_T, METER_W, METER_H
    for r in ((0, 0, a, t), (0, 0, t, a),
              (w - a, 0, a, t), (w - t, 0, t, a),
              (0, h - t, a, t), (0, h - a, t, a),
              (w - a, h - t, a, t), (w - t, h - a, t, a)):
        canvas.drawRect(skia.Rect.MakeXYWH(ox + r[0], oy + r[1], r[2], r[3]), p)

    # cells: optional glow passes underneath, crisp core on top
    x = ox + PAD_X
    inner_top, inner_h = oy + PAD_Y, METER_H - 2 * PAD_Y
    for i in range(CELLS):
        c, gt = row.cell(i)
        rect = skia.Rect.MakeXYWH(x, inner_top, CELL_THICK, inner_h)
        if glow_enabled and gt > 0:
            halo = skia.Paint(AntiAlias=True)
            halo.setColor(color(c, int(40 + 90 * gt)))
            halo.setMaskFilter(skia.MaskFilter.MakeBlur(
                skia.kNormal_BlurStyle, 1.2 + 3.0 * gt))
            canvas.drawRect(rect, halo)
            tight = skia.Paint(AntiAlias=True)
            tight.setColor(color(c, int(140 * gt)))
            tight.setMaskFilter(skia.MaskFilter.MakeBlur(
                skia.kNormal_BlurStyle, 0.8))
            canvas.drawRect(rect, tight)
        p.setColor(color(c))
        canvas.drawRect(rect, p)
        x += CELL_THICK + GAP


DT_MS = 70
N_FRAMES = 60  # 4.2 s loop

surface = skia.Surface(int(SHEET_W * SCALE), int(SHEET_H * SCALE))
canvas = surface.getCanvas()
muted, strong = rgb(theme.BASE_MUTED), rgb(theme.MAGENTA_BRIGHT)

for f in range(N_FRAMES):
    canvas.clear(color(rgb(theme.BASE_BLACK)))
    canvas.save()
    canvas.scale(SCALE, SCALE)

    draw_text(canvas, "FLAT (cairo)", COL1_X, PAD + 8, 8, muted)
    draw_text(canvas, "GLOW (skia)", COL2_X, PAD + 8, 8, strong)

    y = PAD + HEADER_H
    for row in ROWS:
        draw_text(canvas, row.label, COL1_X, y + LABEL_H - 3, 7, muted)
        draw_meter(canvas, COL1_X, y + LABEL_H, row, glow_enabled=False)
        draw_meter(canvas, COL2_X, y + LABEL_H, row, glow_enabled=True)
        y += LABEL_H + METER_H + ROW_GAP
        row.advance(DT_MS)

    canvas.restore()
    surface.makeImageSnapshot().save(str(OUTDIR / f"frame_{f:03d}.png"), skia.kPNG)

print(f"wrote {N_FRAMES} frames to {OUTDIR} ({int(SHEET_W*SCALE)}x{int(SHEET_H*SCALE)})")
