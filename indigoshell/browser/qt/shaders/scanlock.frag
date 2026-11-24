#version 440
// The panels' spawn glitch over Flux's page (browser/qt/effects.py): the
// shell's indigoshell/effects.py ScanLock (_SCAN_LOCK), line for line,
// played with config_default.PANEL_SPAWN's numbers. Built with
// `qsb --qt6` into scanlock.frag.qsb (effects.py --build-shaders).
//
// What differs from the SkSL, and why:
//   - src is a texture, not a shader: `page()` samples it at a pixel
//     position with skia's kDecal (transparent outside the page) and
//     nearest filtering (skia.SamplingOptions() is nearest).
//   - smoothstep with reversed edges is undefined in GLSL (defined in
//     SkSL), so `sstep` spells out SkSL's formula.
//   - The page under the effect is the agent's projection of it: the
//     page snapshot with the tint and the raster over it (`projected()`),
//     computed per sample so tears and splits carry them, as the shell's
//     window pass carries what the window drew.
//   - Behind a page there is nothing to show through, so what the effect
//     leaves transparent (rows not locked yet, tear gaps at the edges)
//     shows `bg` (Flux's background), faded in with the effect when
//     `baseFade` is 1.
//   - `keep` is a rect left alone (the Watch's mark).

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 res;        // the page, px
    float t;
    float appear;
    float rows;
    float tear;
    float split;
    vec4 spark;      // rgb, a unused
    float noise;
    float seed;
    vec4 tint;       // premultiplied: the tint's colour * strength, strength
    vec4 line;       // premultiplied: the raster's line colour * alpha, alpha
    float pitch;     // raster pitch, px
    vec4 bg;         // what is behind the page, opaque
    float baseFade;  // 0: bg always under the effect, 1: faded in with it
    vec4 keep;       // x, y, w, h px: left transparent
};

layout(binding = 1) uniform sampler2D src;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453123); }
float sstep(float e0, float e1, float x) {
    float k = clamp((x - e0) / (e1 - e0), 0.0, 1.0);
    return k * k * (3.0 - 2.0 * k);
}

vec4 page(vec2 p) {
    if (p.x < 0.0 || p.y < 0.0 || p.x >= res.x || p.y >= res.y)
        return vec4(0.0);
    return texture(src, (floor(p) + 0.5) / res);
}

// The page as the agent projects it: tint, then the raster line on every
// `pitch`-th row, both SrcATop (only where there is page).
vec4 projected(vec2 p) {
    vec4 c = page(p);
    c.rgb = tint.rgb * c.a + c.rgb * (1.0 - tint.a);
    if (mod(floor(p.y), pitch) < 0.5)
        c.rgb = line.rgb * c.a + c.rgb * (1.0 - line.a);
    return c;
}

void main() {
    vec2 xy = qt_TexCoord0 * res;
    if (xy.x >= keep.x && xy.y >= keep.y && xy.x < keep.x + keep.z && xy.y < keep.y + keep.w) {
        fragColor = vec4(0.0);
        return;
    }
    vec2 uv = xy / res;
    vec4 col = projected(xy);

    if (appear < 0.999) {
        float row  = floor(uv.y * rows);
        float thr  = h1(row * 1.93 + 0.5 + seed);          // per-row reveal threshold
        float lock = sstep(thr, thr + 0.18, appear);
        float unl  = 1.0 - lock;

        float j    = (h1(row + floor(t * 28.0)) - 0.5) * unl;
        vec2 tuv   = uv + vec2(j * tear, 0.0);
        float sp   = split * unl;

        vec4 ts = projected(tuv * res);
        vec4 sr = projected((tuv + vec2(sp, 0.0)) * res);
        vec4 sb = projected((tuv - vec2(sp, 0.0)) * res);
        vec3 tc = vec3(sr.r, ts.g, sb.b);

        if (noise > 0.0) {
            float stat = (h2(uv * vec2(700.0, 40.0) + t) - 0.5) * noise * unl;
            tc += vec3(stat) * ts.a;
        }

        col = vec4(mix(tc, col.rgb, lock), mix(ts.a, col.a, lock));

        float front = sstep(0.05, 0.0, abs(appear - thr)) * unl;
        col.rgb += spark.rgb * (front * 0.8) * col.a;

        col *= sstep(0.0, 0.12, appear);
    }
    col.rgb = min(col.rgb, vec3(col.a));   // keep premultiplied output valid

    // Flux: what shows where the projection is not.
    float b = mix(1.0, sstep(0.0, 0.12, appear), baseFade);
    vec4 o = col + (1.0 - col.a) * bg * b;
    fragColor = o * qt_Opacity;
}
