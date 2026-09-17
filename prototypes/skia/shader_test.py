"""SkSL runtime-shader test on the CPU raster backend.

Renders an animated neon energy-band effect (INDIGO palette) as PNG
frames, timing the per-frame cost. Proves custom pixel shaders work
without any GPU/GL context.

Run:  uv run --no-project --with skia-python python shader_test.py
"""

import time
from pathlib import Path

import skia

W, H = 624, 160
OUT = Path("shader_frames")
OUT.mkdir(exist_ok=True)

SKSL = """
uniform float2 res;
uniform float t;

half4 main(float2 xy) {
    float2 uv = xy / res;

    // traveling energy band: a bright ridge snaking across the strip
    float ridge = 0.5 + 0.28 * sin(uv.x * 5.0 + t * 1.7)
                       * sin(t * 0.9 + uv.x * 2.0);
    float band  = smoothstep(0.38, 0.0, abs(uv.y - ridge));

    // palette: MAGENTA_BRIGHT #ff2a6d <-> CYAN_BRIGHT #05d9e8
    half3 magenta = half3(1.000, 0.165, 0.427);
    half3 cyan    = half3(0.020, 0.851, 0.910);
    float wave = 0.5 + 0.5 * sin(uv.x * 8.0 - t * 2.3);
    half3 col = mix(magenta, cyan, wave) * band * band;

    // secondary faint echo band, phase-shifted, yellow #fcee0c
    float ridge2 = 0.5 + 0.30 * sin(uv.x * 3.0 - t * 1.1 + 2.0);
    float band2  = smoothstep(0.12, 0.0, abs(uv.y - ridge2));
    col += half3(0.988, 0.933, 0.047) * band2 * 0.35;

    // CRT scanlines + soft vignette
    float scan = 0.82 + 0.18 * sin(xy.y * 3.14159);
    float vig  = 1.0 - 0.55 * pow(abs(uv.x - 0.5) * 2.0, 3.0);
    col *= scan * vig;

    half3 base = half3(0.020, 0.012, 0.063);   // BASE_BLACK #050310
    return half4(base + col, 1.0);
}
"""

effect = skia.RuntimeEffect.MakeForShader(SKSL)
builder = skia.RuntimeEffectBuilder(effect)

surface = skia.Surface(W, H)
canvas = surface.getCanvas()

N = 48
times = []
for f in range(N):
    t = f * 0.09
    builder.setUniform("res", [float(W), float(H)])
    builder.setUniform("t", t)
    paint = skia.Paint(Shader=builder.makeShader())
    t0 = time.perf_counter()
    canvas.drawPaint(paint)          # fill the whole surface via the shader
    surface.flushAndSubmit()
    times.append(time.perf_counter() - t0)
    surface.makeImageSnapshot().save(str(OUT / f"frame_{f:03d}.png"), skia.kPNG)

avg = sum(times) / len(times) * 1000
print(f"{N} frames at {W}x{H}, CPU raster: avg {avg:.2f} ms/frame "
      f"(min {min(times)*1000:.2f}, max {max(times)*1000:.2f})")
