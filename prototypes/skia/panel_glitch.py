"""Braindance-style OSD panel (tabs / sections / settings rows, looks only)
rendered with Skia, then post-processed per-frame through an SkSL runtime
shader on the CPU raster backend:

  - RGB channel separation (calm baseline + violent bursts)
  - horizontal glitch-band displacement + teal corruption bands
  - CRT scanlines + slow rolling refresh line
  - noise flicker, vignette
  - per-band materialize on open (like braindance's 0.3s panel appear)
  - whole-panel vertical jitter during bursts (canvas-side)

Panel design mirrors ~/Projects/VitureBD/wreath/braindance render.py
set_panel(): same palette, tab strip, section bands, selection accent,
chevroned values, FPS footer.

Run:  uv run --no-project --with skia-python python panel_glitch.py
"""

import time
from pathlib import Path

import skia

# ── Night City palette (braindance render.py _PAL) ──────────────────────
PAL = {
    "bg": (10, 4, 24), "border": (255, 42, 109), "magenta": (255, 42, 109),
    "cyan": (5, 217, 232), "yellow": (252, 238, 12), "lime": (204, 255, 0),
    "violet": (185, 103, 255), "fg": (160, 168, 200), "fg_hi": (200, 208, 232),
    "muted": (90, 74, 120), "error": (255, 0, 60),
    "sel_bg": (36, 24, 64), "sect_bg": (26, 16, 48), "black": (5, 3, 16),
}


def C(name, a=255):
    return skia.ColorSetARGB(a, *PAL[name])


W, H = 620, 420                    # sheet; panel inset by MARGIN
MARGIN = 24
PW, PH = W - 2 * MARGIN, H - 2 * MARGIN
OUT = Path("panel_frames")
OUT.mkdir(exist_ok=True)


def load_typeface(bold=False):
    style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
    for fam in ("JetBrainsMono Nerd Font", "JetBrains Mono", "Hack",
                "DejaVu Sans Mono", "monospace"):
        try:
            tf = skia.FontMgr().matchFamilyStyle(fam, style)
        except Exception:
            tf = None
        if tf is not None:
            return tf
    return None


TF, TF_B = load_typeface(), load_typeface(bold=True)
F = skia.Font(TF, 15)              # pfont
F_B = skia.Font(TF_B or TF, 18)    # pfont_b
F_S = skia.Font(TF, 12)            # pfont_s


def text(canvas, s, x, y, f, color):
    p = skia.Paint(AntiAlias=True)
    p.setColor(color)
    canvas.drawString(s, x, y, f, p)


def fill(canvas, x, y, w, h, color):
    p = skia.Paint(AntiAlias=False)
    p.setColor(color)
    canvas.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


def stroke_rect(canvas, x, y, w, h, color, sw):
    p = skia.Paint(AntiAlias=False)
    p.setColor(color)
    p.setStyle(skia.Paint.kStroke_Style)
    p.setStrokeWidth(sw)
    canvas.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


# ── panel content: DEPTH tab of the braindance menu, animated ──────────
TAB_NAMES = ["DISPLAY", "DEPTH", "3D", "TEAR", "REFINE", "DEBUG"]
TAB_IDX = 1


def rows_for_frame(f):
    """Sections + settings with values that drift over the loop. The second
    glitch burst 'corrupts' TENSORRT to OFF; the f=70 pop restores it."""
    model = ["vitl14", "vitb14", "vits14"][(f // 48) % 3]
    trt_on = not (52 <= f < 70)
    ema = f"{0.55 + 0.05 * ((f // 20) % 3):.2f}"
    sel = (f // 16) % 4            # cycles over the 4 editable rows
    return [
        {"section": True, "label": "MODEL"},
        {"label": "MODEL", "value": model, "kind": "enum",
         "editable": True, "selected": sel == 0},
        {"section": True, "label": "PIPELINE"},
        {"label": "TENSORRT", "value": "ON" if trt_on else "OFF", "kind": "bool",
         "editable": True, "selected": sel == 1},
        {"label": "SYNC", "value": "OFF", "kind": "bool",
         "editable": True, "selected": sel == 2},
        {"label": "DEPTH EMA", "value": ema, "kind": "num",
         "editable": True, "selected": sel == 3},
    ]


def val_color(row):
    if not row["editable"]:
        return C("muted")
    if row["kind"] == "bool":
        return C("lime") if row["value"] == "ON" else C("muted")
    if row["kind"] == "enum":
        return C("cyan")
    return C("yellow")


def draw_ui(canvas, f, jitter_y):
    canvas.clear(C("black"))
    canvas.save()
    canvas.translate(MARGIN, MARGIN + jitter_y)

    fill(canvas, 0, 0, PW, PH, skia.ColorSetARGB(245, *PAL["bg"]))
    stroke_rect(canvas, 1, 1, PW - 2, PH - 2, C("border"), 2)

    pad = 28
    # title
    text(canvas, "BRAINDANCE", pad, 30, F_B, C("magenta"))
    tw = F_B.measureText("BRAINDANCE")
    text(canvas, "v4.0-NC", pad + tw + 14, 30, F_S, C("yellow"))

    # tab strip
    tx, ty = pad, 46
    for i, name in enumerate(TAB_NAMES):
        w = F.measureText(name) + 22
        if i == TAB_IDX:
            fill(canvas, tx, ty, w, 30, C("sel_bg"))
            fill(canvas, tx, ty + 28, w, 2, C("magenta"))
            text(canvas, name, tx + 11, ty + 20, F, C("fg_hi"))
        else:
            text(canvas, name, tx + 11, ty + 20, F, C("muted"))
        tx += w + 5
    fill(canvas, pad, ty + 36, PW - 2 * pad, 1, C("muted"))

    # settings column
    cw, lh = PW - 2 * pad, 34
    y = ty + 48
    for r in rows_for_frame(f):
        if r.get("section"):
            y += 6
            fill(canvas, pad, y, cw, 24, C("sect_bg"))
            fill(canvas, pad, y, 5, 24, C("cyan"))
            text(canvas, r["label"], pad + 14, y + 17, F_S, C("cyan"))
            y += 24 + 6
            continue
        sel = r["selected"]
        if sel:
            fill(canvas, pad, y - 4, cw, 30, C("sel_bg"))
            fill(canvas, pad, y - 4, 5, 30, C("magenta"))
        text(canvas, r["label"], pad + 16, y + 16, F,
             C("fg_hi") if sel else C("fg"))
        val, col = r["value"], val_color(r)
        vx = pad + cw - 150
        if sel and r["editable"]:
            text(canvas, "‹", vx - 22, y + 16, F, C("magenta"))
            text(canvas, val, vx, y + 16, F, col)
            w = F.measureText(val)
            text(canvas, "›", vx + w + 8, y + 16, F, C("magenta"))
        else:
            text(canvas, val, vx, y + 16, F, col)
        y += lh

    # footer
    fill(canvas, pad, PH - 44, PW - 2 * pad, 1, C("muted"))
    fps = 55 + (f % 7)
    fcol = C("lime") if fps >= 55 else C("yellow")
    text(canvas, "FPS", pad, PH - 20, F_S, C("fg"))
    text(canvas, f"{fps:d}", pad + 46, PH - 20, F_S, fcol)
    hints = "↑↓ ←→   TAB   ENTER"
    text(canvas, hints, PW - pad - F_S.measureText(hints), PH - 20, F_S,
         C("muted"))

    canvas.restore()


# ── SkSL post-processing shader ─────────────────────────────────────────
SKSL = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float g;        // glitch burst intensity 0..1
uniform float appear;   // materialize 0..1

float hash(float x) { return fract(sin(x) * 43758.5453); }

half4 main(float2 xy) {
    float2 uv = xy;
    float band = floor(xy.y / 7.0);

    // horizontal band displacement (bursts only)
    float seed = floor(t * 24.0);
    float h1   = hash(band * 91.7 + seed * 13.1);
    uv.x += (h1 - 0.5) * 46.0 * g * step(0.72, h1);

    // rgb separation: subtle always, violent in bursts
    float sep = 1.1 + 12.0 * g;
    half r  = src.eval(float2(uv.x - sep, uv.y)).r;
    half gr = src.eval(uv).g;
    half b  = src.eval(float2(uv.x + sep, uv.y)).b;
    half3 col = half3(r, gr, b);

    // rare corrupted teal bands during bursts
    float h2 = hash(band * 17.3 + seed * 7.7);
    if (g > 0.15 && h2 > 0.94) {
        half m = half(dot(col, half3(0.3333)));
        col = half3(m * 0.15, m * 1.05, m * 1.15);
    }

    // CRT scanlines + slow rolling refresh line
    col *= half(0.86 + 0.14 * sin(xy.y * 3.14159));
    float roll = fract(t * 0.25);
    float d = abs(xy.y / res.y - roll);
    col += half3(0.05, 0.30, 0.35) * half(smoothstep(0.015, 0.0, d));

    // noise flicker (stronger in bursts)
    float n = fract(sin(dot(xy, float2(12.9898, 78.233)) + t * 57.0) * 43758.5453);
    col += half3(half((n - 0.5) * (0.05 + 0.28 * g)));

    // vignette
    float2 cc = xy / res - 0.5;
    col *= half(1.0 - 0.55 * dot(cc, cc) * 2.0);

    // per-band materialize: bands pop in as `appear` rises
    if (appear < 1.0) {
        float on = step(hash(band * 3.7 + 5.0), appear);
        col *= half(on * (0.35 + 0.65 * appear));
    }

    return half4(col, 1);
}
"""

effect = skia.RuntimeEffect.MakeForShader(SKSL)
builder = skia.RuntimeEffectBuilder(effect)


def glitch_amp(f):
    amp = 0.0
    for start, length, peak in ((18, 9, 1.0), (52, 7, 0.8)):
        if start <= f < start + length:
            amp = max(amp, peak * (1.0 - (f - start) / length))
    if f in (38, 70):
        amp = max(amp, 0.55)
    return amp


N, FPS = 80, 20
ui_surface = skia.Surface(W, H)
out_surface = skia.Surface(W, H)
sampling = skia.SamplingOptions()
times = []

for f in range(N):
    t = f / FPS
    g = glitch_amp(f)
    appear = min(1.0, f / 6.0)
    jitter = (2 if f % 2 else -2) * g if g > 0.3 else 0.0

    draw_ui(ui_surface.getCanvas(), f, jitter)
    child = ui_surface.makeImageSnapshot().makeShader(
        skia.TileMode.kClamp, skia.TileMode.kClamp, sampling)

    builder.setChild("src", child)
    builder.setUniform("res", [float(W), float(H)])
    builder.setUniform("t", t)
    builder.setUniform("g", g)
    builder.setUniform("appear", appear)

    t0 = time.perf_counter()
    p = skia.Paint(Shader=builder.makeShader())
    out_surface.getCanvas().drawPaint(p)
    out_surface.flushAndSubmit()
    times.append(time.perf_counter() - t0)

    out_surface.makeImageSnapshot().save(str(OUT / f"frame_{f:03d}.png"), skia.kPNG)

avg = sum(times) / len(times) * 1000
print(f"{N} frames at {W}x{H}: post-process avg {avg:.2f} ms/frame "
      f"(min {min(times)*1000:.2f}, max {max(times)*1000:.2f}); "
      f"typeface={'mono' if TF else 'default'}")
