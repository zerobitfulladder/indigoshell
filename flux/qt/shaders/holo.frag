#version 440
// The agent's projection over Flux's page while she drives it
// (flux/qt/effects.py): the tint and the raster (the shell's
// indigoshell/widgets/holo.py `raster`, painted SrcATop as a `holo`
// window does), and the scan (holo.py `_SCAN`, line for line) in the
// strip under the band. Built with `qsb --qt6` into holo.frag.qsb
// (effects.py --build-shaders).
//
// Outside the strip the output is translucent: Qt lays it over the live
// page. Inside it the output replaces the page, as Projection's kSrc draw
// does, from `strip`: the page's pixels, rows texTop .. texTop + texH
// (the whole page, read back when it last drew). What the scan leaves
// transparent (a dropped slice, a tear gap at an edge) shows `bg`.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 res;        // the page, px
    float scanning;  // 1 while a band is on the page ("active" is reserved in GLSL)
    float head;      // the band's centre, page y
    float t;
    float seed;
    float head_px;
    float wake;
    float shift;
    float split;
    float rate;
    float pitch;
    float stripTop;  // the strip the scan redraws: first row, page y
    float stripH;    // its height, px
    float texTop;    // the page texture's first row, page y
    float texH;      // its height, px
    vec4 tint;       // premultiplied: the tint's colour * strength, strength
    vec4 line;       // premultiplied: the raster's line colour * alpha, alpha
    vec4 bg;         // what is behind the page, opaque
    vec4 keep;       // x, y, w, h px: left transparent
};

layout(binding = 1) uniform sampler2D strip;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }

vec4 page(vec2 p) {
    if (p.x < 0.0 || p.x >= res.x || p.y < texTop || p.y >= texTop + texH)
        return vec4(0.0);
    return texture(strip, vec2((floor(p.x) + 0.5) / res.x,
                               (floor(p.y - texTop) + 0.5) / texH));
}

bool lined(float y) { return mod(floor(y), pitch) < 0.5; }

// What the window shows at p before the scan: the page, the tint, the
// raster. The scan snapshots exactly that.
vec4 projected(vec2 p) {
    vec4 c = page(p);
    c.rgb = tint.rgb * c.a + c.rgb * (1.0 - tint.a);
    if (lined(p.y))
        c.rgb = line.rgb * c.a + c.rgb * (1.0 - line.a);
    return c;
}

void main() {
    vec2 xy = qt_TexCoord0 * res;
    if (xy.x >= keep.x && xy.y >= keep.y && xy.x < keep.x + keep.z && xy.y < keep.y + keep.w) {
        fragColor = vec4(0.0);
        return;
    }
    if (scanning < 0.5 || xy.y < stripTop || xy.y >= stripTop + stripH) {
        // tint + raster, to be laid over the live page
        vec4 o = tint;
        if (lined(xy.y))
            o = vec4(line.rgb + o.rgb * (1.0 - line.a), 1.0 - (1.0 - line.a) * (1.0 - o.a));
        fragColor = o * qt_Opacity;
        return;
    }

    // ---- holo.py _SCAN, from here to the drop ----
    float d = head - xy.y;                       // > 0: already scanned
    float band = exp(-(d * d) / (head_px * head_px));
    float lit = d >= 0.0 ? exp(-d / (wake * 0.35)) : band;
    float roll = floor(t * rate) + seed;

    float g = floor(xy.y / 9.0);
    float s = floor(xy.y / 3.0);
    float dg = head - (g + 0.5) * 9.0;
    float ds = head - (s + 0.5) * 3.0;
    float bg_ = exp(-(dg * dg) / (head_px * head_px));
    float bs = exp(-(ds * ds) / (head_px * head_px));
    float hs = max(bs, (ds >= 0.0 ? exp(-ds / (wake * 0.35)) : bs) * 0.45);
    float seg_len = 24.0 + 96.0 * h1(s * 2.3 + roll * 0.9);
    float seg = floor((xy.x + h1(s * 4.1 + roll) * seg_len) / seg_len);
    float piece = s * 131.0 + seg;
    float on_g = step(1.0 - 0.7 * bg_, h1(g * 3.7 + roll));
    float on_p = step(1.0 - 0.6 * bs, h1(piece * 1.9 + roll * 0.5));
    float sg = h1(g * 7.1 + roll * 1.3) < 0.5 ? -1.0 : 1.0;
    float sp_ = h1(piece * 5.3 + roll * 0.7) < 0.5 ? -1.0 : 1.0;
    float dx = sg * mix(4.0, shift, h1(g * 2.9 + roll)) * on_g
             + sp_ * mix(2.0, shift * 0.6, h1(piece * 6.7 + roll)) * on_p;
    vec2 p = vec2(xy.x + floor(dx), xy.y);

    float sp = floor(split * hs + 0.5);
    vec4 c = projected(p);
    vec4 r = projected(p + vec2(sp, 0.0));
    vec4 b = projected(p - vec2(sp, 0.0));
    float a = max(c.a, max(r.a, b.a));
    vec3 rgb = vec3(r.r, c.g, b.b);

    float row = step(mod(xy.y, pitch), 1.0);
    float k = band * 0.9 + lit * (0.10 + 0.45 * row);
    rgb = rgb * (1.0 + k) + vec3(a) * (0.16 * band);

    float drop = step(0.9, h1(piece * 9.1 + roll * 0.3)) * step(0.4, bs);
    rgb *= 1.0 - 0.75 * drop;
    a *= 1.0 - 0.75 * drop;
    vec4 o = vec4(min(rgb, vec3(a)), a);
    // ---- end of _SCAN ----

    fragColor = (o + (1.0 - o.a) * bg) * qt_Opacity;
}
