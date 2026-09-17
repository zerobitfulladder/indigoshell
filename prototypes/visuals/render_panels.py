"""Proposal sheets for panel open/close transitions.

A stand-in panel built from the real HUD chrome is snapshotted once, then
each effect runs over it at seven `appear` values — exactly the way the
window's post-process pass does it — and the frames are laid out as a
strip. `panel_open.png` is appear 0 -> 1, `panel_close.png` is 1 -> 0.

    PYTHONPATH=../.. python render_panels.py
"""
import skia

from common import C, fill_rect, sheet, txt
from indigoshell import effects, theme
from indigoshell.effects import Effect, ScanLock, compile_sksl
from indigoshell.widgets.base import Insets, Size
from indigoshell.widgets.hud import (HudCard, key_label, meta_label,
                                      section_header, value_label)
from indigoshell.widgets.label import Label
from indigoshell.widgets.layout import Align, Box, Column, Row, Spacer
from indigoshell.widgets.meters import BarMeter

# ── the stand-in panel ──────────────────────────────────────────────────
PANEL_W, PANEL_H = 330, 250
MARGIN = 28              # room for effects that reach outside the panel


def build_panel():
    def bar(pct, color=theme.CYAN_BRIGHT):
        m = BarMeter(color=color, min_width=250, tick=4, gap=2, thick=6)
        m.set_value(pct)
        return m

    def row(key, meta, value):
        return Row([key_label(key, width_chars=5), meta_label(meta), Spacer(),
                    value_label(value)], spacing=8, align=Align.BASELINE)

    cpu = HudCard(Column([row("CPU", "Ryzen 9 7940HS", "42%"), bar(42)],
                         spacing=6, align=Align.STRETCH))
    ram = HudCard(Column([row("RAM", "27.3 GB total", "63%"),
                          bar(63, theme.VIOLET_BRIGHT)],
                         spacing=6, align=Align.STRETCH),
                  accent=theme.VIOLET_BRIGHT)
    disk = HudCard(Column([row("DISK", "/ nvme0n1p2", "71%"),
                           bar(71, theme.YELLOW_BRIGHT)],
                          spacing=6, align=Align.STRETCH),
                   accent=theme.MAGENTA_BRIGHT)
    content = Column([section_header("HARDWARE", "// night-city-01"),
                      cpu, ram, disk],
                     spacing=8, align=Align.STRETCH)
    box = Box(content, padding=Insets.all(14), background=theme.POPUP_BG,
              border=theme.POPUP_BORDER, border_width=2.0,
              bevel=theme.POPUP_BEVEL, bevel_corners=theme.POPUP_BEVEL_CORNERS)
    return box


def snapshot_panel():
    box = build_panel()
    box.measure(Size(PANEL_W, PANEL_H))
    box.arrange(skia.Rect.MakeXYWH(MARGIN, MARGIN, PANEL_W, PANEL_H))
    w, h = PANEL_W + 2 * MARGIN, PANEL_H + 2 * MARGIN
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(0, 0, 0, 0))
    box.paint(c)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


# ── a generic SkSL effect for prototyping ───────────────────────────────
class Fx(Effect):
    def __init__(self, source, duration, **uniforms):
        self.duration = duration
        self.uniforms = uniforms
        self._effect = compile_sksl(source)

    def shader(self, src, w, h, *, t, appear, seed=0.0):
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t))
        b.setUniform("appear", float(appear))
        b.setUniform("seed", float(seed))
        for k, v in self.uniforms.items():
            b.setUniform(k, list(map(float, v)) if isinstance(v, (tuple, list)) else float(v))
        return b.makeShader()


HEAD = """
uniform shader src; uniform float2 res; uniform float t; uniform float appear; uniform float seed;
float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }
half4 keep(half4 c){ c.rgb = min(c.rgb, half3(c.a)); return c; }
"""

# A. CRT power: a bright line grows, then expands vertically with a
#    small overshoot and a bloom that settles. Runs backwards for close.
CRT = HEAD + """
uniform float3 spark;
half4 main(float2 xy) {
    float p1 = clamp(appear / 0.3, 0.0, 1.0);            // line grows
    float p2 = clamp((appear - 0.3) / 0.7, 0.0, 1.0);     // opens up
    float e2 = 1.0 - pow(1.0 - p2, 3.0);
    float sy = max(0.006, e2 * (1.0 + 0.06 * sin(p2 * 3.14159)));
    float sx = 0.05 + 0.95 * (1.0 - pow(1.0 - p1, 2.0));
    float2 c = res * 0.5;
    float2 suv = (xy - c) / float2(sx, sy) + c;
    half4 col = half4(0.0);
    if (suv.x >= 0.0 && suv.x <= res.x && suv.y >= 0.0 && suv.y <= res.y)
        col = src.eval(suv);
    // the collapsed image is a hot line: bloom scaled by how flat it is
    float fl = 1.0 - e2;
    col.rgb += half3(spark) * half(fl * 0.9) * col.a;
    col.rgb = mix(col.rgb, half3(1.0) * col.a, half(fl * 0.5));
    // the line itself, drawn over everything while the panel is flat
    float lw = sx * res.x * 0.5;
    float line = (1.0 - smoothstep(1.0, 2.5, abs(xy.y - c.y))) * step(abs(xy.x - c.x), lw) * step(0.0, 0.08 - sy) * step(0.001, appear);
    col = half4(mix(col.rgb, half3(spark), half(line)), max(col.a, half(line)));
    return keep(col);
}
"""

# B. Boot wipe: a cyan scan head sweeps down; below it the panel is a dim
#    wireframe ghost with scanlines, behind it fresh rows fringe and glow.
BOOT = HEAD + """
uniform float3 spark;
half4 main(float2 xy) {
    float head = appear * (res.y + 60.0) - 30.0;
    half4 col = src.eval(xy);
    float d = head - xy.y;                         // >0: revealed
    if (d < 0.0) {
        // ghost: outline-ish placeholder with scanlines
        float scan = step(1.5, mod(xy.y, 3.0));
        col = half4(half3(spark) * half(0.10) * col.a, col.a * half(0.10)) * half(scan);
        return keep(col);
    }
    float fresh = exp(-d / 45.0);
    float split = 5.0 * fresh;
    float jx = (h1(floor(xy.y / 3.0) + floor(t * 30.0) + seed) - 0.5) * 14.0 * fresh;
    half4 cc = src.eval(xy + float2(jx, 0.0));
    half4 cr = src.eval(xy + float2(jx + split, 0.0));
    half4 cb = src.eval(xy + float2(jx - split, 0.0));
    col = half4(cr.r, cc.g, cb.b, max(cc.a, max(cr.a, cb.a)));
    col.rgb += half3(spark) * half(fresh * 0.6) * col.a;
    // the head: a bright line with a glow trailing above it
    float line = 1.0 - smoothstep(0.0, 2.0, abs(d));
    float glow = exp(-d / 14.0) * 0.5;
    float cur = clamp(line + glow, 0.0, 1.0) * step(0.0, appear) * step(appear, 0.999);
    // only across the panel's own columns: use the ghost's coverage row
    half cov = src.eval(float2(res.x * 0.5, clamp(xy.y, 0.0, res.y))).a;
    cur *= step(0.01, float(cov)) * step(20.0, xy.x) * step(xy.x, res.x - 20.0);
    col = half4(mix(col.rgb, half3(spark), half(cur)), max(col.a, half(cur)));
    return keep(col);
}
"""

# C. Hologram: appears at full size at once but unstable — strobing,
#    jittering sideways, chromatic, scanlined, with a rolling bright band.
HOLO = HEAD + """
uniform float3 spark;
half4 main(float2 xy) {
    float unl = 1.0 - appear;
    float fr = floor(t * 40.0) + seed;
    float strobe = 1.0 - step(1.0 - 0.55 * unl, h1(fr)) * 0.75;
    float jx = (h1(fr * 1.7) - 0.5) * 16.0 * unl * step(0.6, h1(fr * 2.3));
    float split = 7.0 * unl;
    float2 p = xy + float2(jx, 0.0);
    half4 cc = src.eval(p);
    half4 cr = src.eval(p + float2(split, 0.0));
    half4 cb = src.eval(p - float2(split, 0.0));
    half4 col = half4(cr.r, cc.g, cb.b, max(cc.a, max(cr.a, cb.a)));
    float scan = 1.0 - 0.35 * unl * step(1.0, mod(xy.y, 2.0));
    float band = exp(-abs(xy.y - fract(t * 0.9 + seed) * res.y) / 18.0) * (0.15 + 0.5 * unl);
    col.rgb *= half(scan);
    col.rgb += half3(spark) * half(band) * col.a;
    col *= half(smoothstep(0.0, 0.25, appear) * strobe);
    return keep(col);
}
"""

# D. Blinds: vertical strips slide in from alternating edges, staggered,
#    with an ease-out and a colour fringe while moving.
BLINDS = HEAD + """
uniform float3 spark;
uniform float strips;
half4 main(float2 xy) {
    float s = floor(xy.x / (res.x / strips));
    float thr = h1(s * 3.7 + seed) * 0.45;
    float p = clamp((appear - thr) / 0.55, 0.0, 1.0);
    float e = 1.0 - pow(1.0 - p, 3.0);
    float dir = mod(s, 2.0) * 2.0 - 1.0;
    float off = (1.0 - e) * res.y * dir;
    float2 suv = xy + float2(0.0, off);
    if (suv.y < 0.0 || suv.y > res.y) return half4(0.0);
    float mov = 1.0 - e;
    float split = 4.0 * mov;
    half4 cc = src.eval(suv);
    half4 cr = src.eval(suv + float2(split, 0.0));
    half4 cb = src.eval(suv - float2(split, 0.0));
    half4 col = half4(cr.r, cc.g, cb.b, max(cc.a, max(cr.a, cb.a)));
    col.rgb += half3(spark) * half(mov * 0.5) * col.a;
    return keep(col);
}
"""

# E. Decode cells: the panel materialises as coarse cells in random
#    order, each a solid tile first, then real pixels — the lyric reveal
#    at panel scale.
CELLS = HEAD + """
uniform float3 spark;
uniform float3 alt;
uniform float cell;
half4 main(float2 xy) {
    float2 id = floor(xy / cell);
    float k = dot(id, float2(1.0, 113.0));
    float thr = h1(k * 0.37 + seed) * 0.6;
    float p = (appear - thr) / 0.4;
    if (p <= 0.0) return half4(0.0);
    p = min(p, 1.0);
    half4 col = src.eval(xy);
    if (p < 0.55) {
        float2 c0 = (id + 0.5) * cell;
        float q = cell * 0.3;
        half cov = max(max(src.eval(c0 + float2(-q, -q)).a, src.eval(c0 + float2(q, -q)).a),
                       max(src.eval(c0 + float2(-q,  q)).a, src.eval(c0 + float2(q,  q)).a));
        float lit = 0.5 + 0.5 * h1(k * 1.3 + floor(t * 18.0));
        float3 tone = mix(spark, alt, step(0.8, h1(k * 0.7 + seed)));
        half a = half(step(0.1, float(cov))) * half(0.55);
        return keep(half4(half3(tone) * a * half(lit), a));
    }
    float heat = (1.0 - p) / 0.45;
    float split = 3.0 * heat;
    half4 cr = src.eval(xy + float2(split, 0.0));
    half4 cb = src.eval(xy - float2(split, 0.0));
    col = half4(cr.r, col.g, cb.b, max(col.a, max(cr.a, cb.a)));
    col.rgb += half3(spark) * half(heat * 0.5) * col.a;
    return keep(col);
}
"""

CYAN = (0.02, 0.85, 0.91)
MAG = (1.0, 0.165, 0.427)
EFFECTS = [
    ("current  ScanLock|bands snap in, tear + rgb", ScanLock(duration=0.22, noise=0.25, tear=0.10)),
    ("A  CRT power|line, then opens; bloom", Fx(CRT, 0.32, spark=CYAN)),
    ("B  boot wipe|scan head, wireframe ghost", Fx(BOOT, 0.38, spark=CYAN)),
    ("C  hologram|strobe, jitter, rolling band", Fx(HOLO, 0.40, spark=CYAN)),
    ("D  blinds|strips slide in, staggered", Fx(BLINDS, 0.34, spark=CYAN, strips=12.0)),
    ("E  decode cells|tiles in random order", Fx(CELLS, 0.40, spark=CYAN, alt=MAG, cell=10.0)),
]
STEPS = [0.0, 0.17, 0.34, 0.5, 0.67, 0.84, 1.0]


def desktop_bg(w, h):
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(C(theme.BASE_SHADOW))
    for gx in range(0, w, 24):
        fill_rect(c, gx, 0, 1, h, theme.BASE_GUTTER, 0.6)
    for gy in range(0, h, 24):
        fill_rect(c, 0, gy, w, 1, theme.BASE_GUTTER, 0.6)
    return s.makeImageSnapshot()


def run_effect(panel, effect, appear, t, seed=0.37 * 997):
    w, h = panel.width(), panel.height()
    sampling = skia.SamplingOptions()
    shader = panel.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal, sampling)
    shader = effect.shader(shader, w, h, t=t, appear=appear, seed=seed)
    out = skia.Surface(w, h)
    paint = skia.Paint(Shader=shader)
    paint.setBlendMode(skia.BlendMode.kSrc)
    out.getCanvas().drawPaint(paint)
    out.flushAndSubmit()
    return out.makeImageSnapshot()


def strip(panel, effect, steps, closing):
    w, h = panel.width(), panel.height()
    gap = 6
    s = skia.Surface((w + gap) * len(steps) - gap, h)
    c = s.getCanvas()
    bg = desktop_bg(w, h)
    for i, a in enumerate(steps):
        appear = (1.0 - a) if closing else a
        # wall clock as the window would see it: elapsed fraction of duration
        t = 2.0 + a * effect.duration
        x = i * (w + gap)
        c.drawImage(bg, x, 0)
        if appear >= 0.999 and not closing:
            frame = panel
        elif appear <= 0.001 and closing:
            frame = None
        else:
            frame = run_effect(panel, effect, appear, t)
        if frame is not None:
            c.drawImage(frame, x, 0)
        txt(c, f"{appear:.2f}", x + 6, 14, 10, theme.HUD_FG)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


if __name__ == "__main__":
    panel = snapshot_panel()
    for closing, name in ((False, "panel_open.png"), (True, "panel_close.png")):
        rows = [(label, strip(panel, fx, STEPS, closing)) for label, fx in EFFECTS]
        sheet(rows, name, scale=1, label_w=200,
              title=("Panel close (appear 1 -> 0)" if closing
                     else "Panel open (appear 0 -> 1)"))
