"""Post-process effects.

An effect is one pass over the window's finished frame: it takes the
rendered image as a shader and returns a new shader. Windows hold a
tuple of them, so passes compose by chaining — the output of one becomes
the `src` of the next.

Two kinds, distinguished only by `duration`:

  duration > 0   a transition. Drives spawn (appear 0->1) and despawn
                 (appear 1->0), and stops running once appear reaches 1.
  duration == 0  continuous. Runs every frame for as long as the window
                 is up (an idle glow, a periodic glitch burst).

The SkSL runs on whichever backend the surface belongs to — the GL one
in practice, where a full-resolution pass costs ~0.08ms against ~25ms for
the same pass on raster. `effect_scale` exists only for the raster
fallback and is ignored on the GPU.

A pass normally covers a whole window. `paint_through` applies the same
chain to a single widget instead, for something like a text reveal that
must not glitch everything around it.
"""

import logging
import math

import skia

log = logging.getLogger(__name__)

_SAMPLING = skia.SamplingOptions()

_compiled: dict[str, skia.RuntimeEffect] = {}


def compile_sksl(source: str) -> skia.RuntimeEffect:
    """Compile once and cache. Compilation is not free, and every window
    of the same kind wants the same program."""
    hit = _compiled.get(source)
    if hit is None:
        hit = skia.RuntimeEffect.MakeForShader(source)
        if hit is None:
            raise RuntimeError("SkSL failed to compile")
        _compiled[source] = hit
    return hit


class Effect:
    fps: int = 60
    duration: float = 0.0

    @property
    def is_transition(self) -> bool:
        return self.duration > 0

    def active(self, appear: float) -> bool:
        """Whether this pass needs to run for the current `appear`."""
        return appear < 0.999 if self.is_transition else True

    def bleed(self, w: float, h: float) -> float:
        """How far outside the content this pass can reach, in pixels.

        A displacing effect draws fringes past the content's edge, and a
        window can't paint outside itself — so the window is grown by the
        largest bleed of its effects and the content is inset by it.
        Effects declare their own reach; nobody hand-tunes a margin.
        """
        return 0.0

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        """`seed` is re-rolled per spawn/despawn.

        Without it every transition replays the same hash sequence —
        `t` restarts at zero each time a window opens, so a "random"
        glitch is identical on every open.
        """
        raise NotImplementedError


# ── running a pass over one widget ──────────────────────────────────────
def paint_through(canvas: skia.Canvas, rect: skia.Rect, draw, passes, *,
                  t: float, appear: float, seed: float = 0.0) -> None:
    """Draw `draw(canvas)` through an effect chain, at widget scale.

    Windows post-process by snapshotting their whole surface; a widget
    cannot do that, because its pixels are already mixed into the bar
    alongside everybody else's. So it renders itself into an offscreen of
    its own first, and that offscreen is what becomes `uniform shader src`.

    This is what `saveLayer` with an image filter would do for us, and it
    is done by hand because skia-python 144 binds no runtime-shader
    filter — `skia.ImageFilters` exposes only `Blur`.

    `canvas.makeSurface` and not `skia.Surface`: the former returns a
    surface of the canvas's own kind, so on the GL backend the
    intermediate stays on the GPU. `skia.Surface` is always CPU raster,
    which would mean rasterising the text and uploading a texture every
    frame — the fallback, for a canvas that will not make one.

    Unlike the window's pass this composites SrcOver, not kSrc: a widget
    draws *onto* the bar, it does not replace the frame.
    """
    if not passes:
        draw(canvas)
        return

    w0, h0 = rect.width(), rect.height()
    bleed = int(math.ceil(max(e.bleed(w0, h0) for e in passes)))
    w = int(math.ceil(w0)) + 2 * bleed
    h = int(math.ceil(h0)) + 2 * bleed
    if w <= 0 or h <= 0:
        return

    info = skia.ImageInfo.MakeN32Premul(w, h)
    surface = canvas.makeSurface(info) or skia.Surface(w, h)
    inner = surface.getCanvas()
    inner.clear(skia.ColorSetARGB(0, 0, 0, 0))
    inner.translate(bleed - rect.left(), bleed - rect.top())
    draw(inner)
    surface.flushAndSubmit()

    # kDecal so a tear that reaches past the widget reads as transparent
    # rather than smearing the edge column, same as the window pass.
    shader = surface.makeImageSnapshot().makeShader(
        skia.TileMode.kDecal, skia.TileMode.kDecal, _SAMPLING)
    for effect in passes:
        shader = effect.shader(shader, w, h, t=t, appear=appear, seed=seed)

    canvas.save()
    canvas.translate(rect.left() - bleed, rect.top() - bleed)
    canvas.drawRect(skia.Rect.MakeWH(w, h), skia.Paint(Shader=shader))
    canvas.restore()


# ── scan-lock reveal ────────────────────────────────────────────────────
# Ported from the braindance HUD's PANEL_FS open animation (GLSL 330 ->
# SkSL). Horizontal bands snap in *out of order*, each with a sideways
# tear, an RGB split and static, resolving row by row — it reads as a
# signal locking in rather than a box sliding open.
#
# Three things differ from the GL original, all forced by Skia:
#   - src.eval() takes pixel coordinates, not normalised uv, so uv is
#     rebuilt from `res` and scaled back on every sample.
#   - Skia surfaces are premultiplied, so additive terms are scaled by
#     alpha and the result is clamped to it. Skipping that produces
#     colour brighter than its own alpha, which reads as white fringing.
#   - Samples are NOT clamped into range. The window feeds this a kDecal
#     shader, so a tear that reaches past the panel returns transparent.
#     Clamping instead (as the GL version does, harmlessly, over a
#     soft-edged HUD) makes every out-of-range pixel sample the same edge
#     column — and against a hard 2px border that smears into solid bars.

_SCAN_LOCK = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float appear;
uniform float rows;
uniform float tear;
uniform float split;
uniform float3 spark;
uniform float noise;
uniform float seed;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }

half4 main(float2 xy) {
    float2 uv = xy / res;
    half4 col = src.eval(xy);

    if (appear < 0.999) {
        float row  = floor(uv.y * rows);
        float thr  = h1(row * 1.93 + 0.5 + seed);          // per-row reveal threshold
        float lock = smoothstep(thr, thr + 0.18, appear);
        float unl  = 1.0 - lock;

        float j    = (h1(row + floor(t * 28.0)) - 0.5) * unl;
        float2 tuv = uv + float2(j * tear, 0.0);
        float sp   = split * unl;

        half4 ts = src.eval(tuv * res);
        half4 sr = src.eval((tuv + float2(sp, 0.0)) * res);
        half4 sb = src.eval((tuv - float2(sp, 0.0)) * res);
        half3 tc = half3(sr.r, ts.g, sb.b);

        if (noise > 0.0) {
            half stat = half((h2(uv * float2(700.0, 40.0) + t) - 0.5) * noise * unl);
            tc += half3(stat) * ts.a;
        }

        col = half4(mix(tc, col.rgb, half(lock)), mix(ts.a, col.a, half(lock)));

        float front = smoothstep(0.05, 0.0, abs(appear - thr)) * unl;
        col.rgb += half3(spark) * half(front * 0.8) * col.a;

        col *= half(smoothstep(0.0, 0.12, appear));
    }
    col.rgb = min(col.rgb, half3(col.a));   // keep premultiplied output valid
    return col;
}
"""


class ScanLock(Effect):
    def __init__(self, duration: float = 0.16, rows: int = 30,
                 fps: int = 60, spark: tuple[float, float, float] = (0.1, 0.95, 1.0),
                 noise: float = 0.0, tear: float = 0.6,
                 band_px: float | None = None, tear_px: float | None = None,
                 split_px: float | None = None):
        self.duration = duration
        self.rows = rows
        # Pixel sizing. `rows` and `tear` are fractions of the surface,
        # which made the effect a different animation on every window:
        # a 580px panel got 22px bands and a 35px throw where a 90px
        # notification toast got 6px bands and 29px. Setting these
        # instead fixes the band height and the sideways/RGB reach in
        # pixels, so every panel plays the toast's proportions. Set all
        # three or none; they override the fractions when set.
        self.band_px = band_px
        self.tear_px = tear_px
        self.split_px = split_px
        self.fps = fps
        self.spark = spark
        # Peak sideways displacement, as a fraction of width — a row can
        # slide by up to `tear / 2` either way.
        #
        # This is the effect's price tag, not just its look. Sampling
        # past the content edge is what makes the tear visible outside
        # the panel, so `bleed()` grows the X window by it on all sides,
        # and the shader then runs over that larger surface. At the
        # braindance default of 0.6 a 620x440 panel becomes a 1030x850
        # window and the pass costs 31ms/frame on CPU raster — three
        # times the un-bled cost, for margin that is almost entirely
        # empty. The original clamps instead and never leaves the panel
        # at all, so a small tear here is closer to it than a large one.
        self.tear = tear
        # Braindance overlays grey static on the unlocked rows (0.6). Off
        # by default here: over a sparse panel it reads as dirt rather
        # than signal noise.
        self.noise = noise
        self._effect = compile_sksl(_SCAN_LOCK)
        self._sampling = skia.SamplingOptions()

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        # Uniforms must go through RuntimeEffectBuilder — hand-packed
        # skia.Data binds silently wrong and the output comes out NaN-white.
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t))
        b.setUniform("appear", float(appear))
        rows = h / self.band_px if self.band_px else self.rows
        # `tear` is the full range, a row slides by up to half of it.
        tear = 2.0 * self.tear_px / w if self.tear_px is not None else self.tear
        split = self.split_px / w if self.split_px is not None else 0.03
        b.setUniform("rows", float(rows))
        b.setUniform("tear", float(tear))
        b.setUniform("split", float(split))
        b.setUniform("spark", list(self.spark))
        b.setUniform("noise", float(self.noise))
        b.setUniform("seed", float(seed))
        return b.makeShader()

    def bleed(self, w: float, h: float) -> float:
        if self.tear_px is not None and self.split_px is not None:
            return self.tear_px + self.split_px + 2.0
        return (self.tear / 2 + 0.03) * w      # peak tear + split


# ── glitchy colour separation ───────────────────────────────────────────
# Spawn/despawn as pure chromatic separation: the red and blue channels
# start flung apart and converge onto green as `appear` goes 0 -> 1. The
# offset is jittered per row and re-randomised a few times a second, so
# the channels don't slide in smoothly — they snap and stutter.
#
# Alpha is the max of the three samples, so the fringes carry their own
# coverage instead of being clipped to the centre sample's silhouette.

_COLOR_SPLIT = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float appear;
uniform float amount;
uniform float rows;
uniform float jitter;
uniform float green;
uniform float chaos;
uniform float rate;
uniform float seed;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }

half4 main(float2 xy) {
    float2 uv = xy / res;
    float unl = 1.0 - appear;

    // One random draw per row per re-roll window. `seed` differs on every
    // spawn, so no two transitions play the same pattern.
    float step_i = floor(t * rate) + seed;
    float row    = floor(uv.y * rows);
    float gr = h1(row * 1.93 + step_i);
    float gg = h1(row * 7.31 + step_i + 11.0);
    float gb = h1(row * 3.77 + step_i + 23.0);
    float gs = h1(row * 5.17 + step_i + 41.0);

    // Occasional rows separate the *other* way instead of travelling
    // further. Same disorder, but the worst-case displacement stays at
    // `amount`, which halves the bleed margin — and the bleed is what
    // drives the window size, and the window size is what the shader
    // cost scales with.
    float dir = 1.0 - 2.0 * step(1.0 - chaos, gs);

    float base = amount * unl * dir;
    float dr =  base * (1.0 - jitter + 2.0 * jitter * gr);
    float db = -base * (1.0 - jitter + 2.0 * jitter * gb);
    // Green drifts on its own, both directions, usually less far — it
    // stays readable as the anchor the other two split away from.
    float dg =  base * (gg - 0.5) * 2.0 * green;

    half4 cr = src.eval((uv + float2(dr, 0.0)) * res);
    half4 cg = src.eval((uv + float2(dg, 0.0)) * res);
    half4 cb = src.eval((uv + float2(db, 0.0)) * res);

    half3 col = half3(cr.r, cg.g, cb.b);
    half a    = max(cg.a, max(cr.a, cb.a));

    half fade = half(smoothstep(0.0, 0.35, appear));
    col *= fade;
    a   *= fade;

    col = min(col, half3(a));
    return half4(col, a);
}
"""


class ColorSplit(Effect):
    def __init__(self, duration: float = 0.16, amount: float = 0.07,
                 rows: int = 24, jitter: float = 0.55, green: float = 0.45,
                 chaos: float = 0.18, rate: float = 18.0, fps: int = 30):
        self.duration = duration
        self.amount = amount      # peak separation, in fractions of width
        self.rows = rows
        self.jitter = jitter      # 0 = uniform slide, 1 = fully random per row
        self.green = green        # how far the green anchor is allowed to drift
        self.chaos = chaos        # fraction of rows that blow out further
        self.rate = rate          # re-rolls per second
        self.fps = fps
        self._effect = compile_sksl(_COLOR_SPLIT)

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t))
        b.setUniform("appear", float(appear))
        b.setUniform("amount", float(self.amount))
        b.setUniform("rows", float(self.rows))
        b.setUniform("jitter", float(self.jitter))
        b.setUniform("green", float(self.green))
        b.setUniform("chaos", float(self.chaos))
        b.setUniform("rate", float(self.rate))
        b.setUniform("seed", float(seed))
        return b.makeShader()

    def bleed(self, w: float, h: float) -> float:
        # Worst case is now just the top of the jitter range — chaos rows
        # reverse rather than overshoot.
        return self.amount * (1.0 + self.jitter) * w


# ── idle glitch / aberration / edge glow ────────────────────────────────
# The always-on half of braindance's PANEL_FS: a subtle chromatic
# aberration that widens during an occasional slice-glitch burst, a noise
# flash under the burst, and a breathing edge glow on the panel border.
# Continuous (duration 0), so it composes under or over a transition.

_ABERRATION = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float3 glow;
uniform float base_ca;
uniform float burst_rate;
uniform float glow_width;
uniform float glow_strength;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }

half4 main(float2 xy) {
    float2 uv = xy / res;

    float win   = floor(t * 3.0);
    float burst = step(1.0 - burst_rate, h1(win)) * (0.4 + 0.6 * h1(win + 7.0));

    float slice = floor(uv.y * 26.0);
    float jit   = step(0.55, h1(slice * 1.7 + floor(t * 22.0)));
    uv.x += burst * (h1(slice + floor(t * 22.0)) - 0.5) * 0.08 * jit;

    float ca = base_ca + burst * 0.006;
    half4 m  = src.eval(uv * res);
    half r   = src.eval((uv + float2(ca, 0.0)) * res).r;
    half b   = src.eval((uv - float2(ca, 0.0)) * res).b;
    half4 col = half4(r, m.g, b, m.a);

    col.rgb += half3(half((h2(uv * res + t) - 0.5) * 0.18 * burst)) * col.a;

    float2 dd  = min(uv, 1.0 - uv);
    float edge = 1.0 - smoothstep(0.0, glow_width, min(dd.x, dd.y));
    float pulse = 0.55 + 0.45 * sin(t * 3.0);
    col.rgb += half3(glow) * half(edge * pulse * glow_strength) * col.a;

    col.rgb = min(col.rgb, half3(col.a));
    return col;
}
"""


class Aberration(Effect):
    duration = 0.0      # continuous

    def __init__(self, fps: int = 30,
                 glow: tuple[float, float, float] = (1.0, 0.16, 0.43),
                 glow_strength: float = 0.0,
                 base_ca: float = 0.0016,
                 burst_rate: float = 0.10,
                 glow_width: float = 0.05):
        self.fps = fps
        self.glow = glow
        # Braindance breathes a magenta glow along the panel border. Off
        # by default — it reads as a halo on an already-bordered panel.
        self.glow_strength = glow_strength
        self.base_ca = base_ca
        self.burst_rate = burst_rate
        self.glow_width = glow_width
        self._effect = compile_sksl(_ABERRATION)

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t) + float(seed))
        b.setUniform("glow", list(self.glow))
        b.setUniform("glow_strength", float(self.glow_strength))
        b.setUniform("base_ca", float(self.base_ca))
        b.setUniform("burst_rate", float(self.burst_rate))
        b.setUniform("glow_width", float(self.glow_width))
        return b.makeShader()

    def bleed(self, w: float, h: float) -> float:
        return (self.base_ca + 0.006 + 0.04) * w


# ── decode ──────────────────────────────────────────────────────────────
# A reveal built for one line of text rather than for a panel.
#
# ScanLock and ColorSplit both *run* at this scale and neither reads as a
# reveal: a lyric is ~14px tall, so a horizontal band of it contains part
# of every glyph, and the line arrives all at once and merely wobbles.
# Shearing those bands sideways doesn't glitch a glyph either, it doubles
# it. So the axes swap: the line is cut into *columns*, each arriving on
# its own threshold — ordered left to right, because that is how a lyric
# is read, but jittered enough to arrive out of order.
#
# The thing that makes or breaks this is that a column becomes **opaque
# almost immediately and stays corrupted for a long time afterwards**.
# The first version ramped alpha and corruption together over the same
# 10% of the reveal, which at the bar's 30fps is 1.3 frames — so peak
# corruption happened at near-zero opacity and for barely one frame, and
# the whole thing read as a plain word-by-word wipe. `span` now covers
# roughly half the reveal and `fade` is over in a tenth of it.
#
# Displacements are in **pixels**, not fractions. As fractions they were
# relative to the padded surface, which `bleed()` inflates — which made
# the amount depend on itself and threw glyphs 48px inside a 42px bar.

_DECODE = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float appear;
uniform float blocks;    // columns the line is cut into
uniform float stagger;   // how far a column's turn may stray from its
                         // position; 0 is a clean left-to-right wipe
uniform float span;      // fraction of the reveal one column takes to resolve
uniform float jump;      // vertical throw, in uv
uniform float slip;      // horizontal throw, in uv
uniform float split;     // rgb separation, in uv
uniform float vsplit;    // vertical part of the separation, in uv
uniform float prism;     // 0 = displace the source's own channels,
                         // 1 = paint the three ghosts as pure R/G/B
uniform float noise;
uniform float chaos;     // fraction of columns thrown much further
uniform float3 spark;
uniform float seed;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }

half4 main(float2 xy) {
    float2 uv = xy / res;
    float b = floor(uv.x * blocks);

    // Scaled by (1 - span) and clamped so the last column still reaches a
    // full resolve at appear == 1. Without that the right-hand end never
    // settles, and the widget's snap back to the direct paint path at the
    // end of the reveal shows up as a pop.
    float thr = clamp((b / blocks) * (1.0 - span)
                      + (h1(b * 2.7 + seed) - 0.5) * stagger,
                      0.0, 1.0 - span);
    float p = (appear - thr) / span;
    if (p <= 0.0) return half4(0.0);
    p = min(p, 1.0);

    float heat = 1.0 - p;                       // 1 on arrival, 0 resolved
    float fade = smoothstep(0.0, 0.10, p);      // opaque almost at once

    // Re-rolled every frame at 30fps, so no two frames of the burst show
    // the same displacement.
    float roll = floor(t * 45.0);
    float r1 = h1(b * 5.1 + roll + seed);
    float r2 = h1(b * 9.7 + roll * 1.7 + seed);
    float r3 = h1(b * 3.1 + roll * 0.6 + seed);

    float blow = step(1.0 - chaos, r3) * heat;
    float jy = ((r1 - 0.5) * jump + (r2 - 0.5) * jump * 2.2 * blow) * heat;
    float jx = (r2 - 0.5) * slip * heat;
    float2 suv = uv + float2(jx, jy);

    // The ghosts separate diagonally, not just left/right — on a line
    // this short a purely horizontal split is hard to tell from motion
    // blur on the glyph stems.
    float sp = split * heat;
    float vs = vsplit * heat;
    half4 cc = src.eval(suv * res);
    half4 cr = src.eval((suv + float2( sp, -vs)) * res);
    half4 cb = src.eval((suv + float2(-sp,  vs)) * res);

    // Displacing the source's own channels can only reveal colour that is
    // already there, and the lyrics are cyan — R is 5/255, so there is no
    // red to move, and G and B are both high and close, so their fringes
    // read as more cyan. `prism` instead paints each ghost as a pure
    // primary weighted by its own coverage, which gives real red/green/
    // blue separation on any source colour. Mixed in by heat, so the line
    // lands on its true colour as it resolves.
    half3 shifted = half3(cr.r, cc.g, cb.b);
    half3 primary = half3(cr.a, cc.a, cb.a);
    half3 rgb = mix(shifted, primary, half(clamp(prism * heat, 0.0, 1.0)));
    // Max, not the centre sample: the fringes must carry their own
    // coverage or they get clipped to the un-split silhouette.
    half a = max(cc.a, max(cr.a, cb.a));

    if (noise > 0.0) {
        half stat = half((h2(uv * float2(res.x * 0.6, 9.0) + t * 11.0) - 0.5)
                         * noise * heat);
        rgb += half3(stat) * a;
    }
    rgb += half3(spark) * half(heat * 0.6) * a;

    half4 col = half4(rgb, a) * half(fade);
    col.rgb = min(col.rgb, half3(col.a));   // keep premultiplied output valid
    return col;
}
"""


class Decode(Effect):
    """Line-scale reveal. Pair with a widget, not a window — see
    `paint_through`.

    `jump`, `slip` and `split` are in pixels. The default jump is sized so
    the worst case, a chaos column at full heat, throws about 11px — which
    is what fits above and below a line of bar text without leaving the
    bar and being clipped by the window.
    """

    def __init__(self, duration: float = 0.55, blocks: float = 24.0,
                 stagger: float = 0.20, span: float = 0.45,
                 jump: float = 7.0, slip: float = 3.0, split: float = 4.5,
                 vsplit: float = 2.0, prism: float = 0.9,
                 noise: float = 0.45, chaos: float = 0.25,
                 spark: tuple[float, float, float] = (0.05, 0.55, 0.6),
                 fps: int = 60):
        self.duration = duration
        self.blocks = blocks
        self.stagger = stagger
        self.span = span
        self.jump = jump
        self.slip = slip
        self.split = split
        self.vsplit = vsplit
        self.prism = prism
        self.noise = noise
        self.chaos = chaos
        self.spark = spark
        self.fps = fps
        self._effect = compile_sksl(_DECODE)

    @property
    def throw(self) -> float:
        """Worst-case vertical displacement, in pixels."""
        return self.jump * (0.5 + 0.5 * 2.2)

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t))
        b.setUniform("appear", float(appear))
        b.setUniform("blocks", float(self.blocks))
        b.setUniform("stagger", float(self.stagger))
        b.setUniform("span", float(self.span))
        # px -> uv here, so the amount does not depend on how much the
        # bleed happened to pad the surface by.
        b.setUniform("jump", float(self.jump / max(1.0, h)))
        b.setUniform("slip", float(self.slip / max(1.0, w)))
        b.setUniform("split", float(self.split / max(1.0, w)))
        b.setUniform("vsplit", float(self.vsplit / max(1.0, h)))
        b.setUniform("prism", float(self.prism))
        b.setUniform("noise", float(self.noise))
        b.setUniform("chaos", float(self.chaos))
        b.setUniform("spark", list(self.spark))
        b.setUniform("seed", float(seed))
        return b.makeShader()

    def bleed(self, w: float, h: float) -> float:
        return max(self.split + self.slip, self.throw + self.vsplit) + 2.0


# ── glitch wipe ─────────────────────────────────────────────────────────
# The second attempt at a line-scale reveal, after Decode. Decode threw
# glyphs about and split their colours; on a 15px line that came out as a
# doubled, smeared image with no shape to it. This one is built from the
# glitch-text idioms that *do* survive at that size, all of them
# horizontal:
#
#   - a cursor bar sweeps the line left to right, with a glow behind it;
#   - text materialises behind the bar as coarse solid cells — data
#     before it is decoded — and sharpens into glyphs;
#   - while a glyph is fresh, thin horizontal slices of it shift sideways
#     and occasionally drop out, and its colour channels fringe apart;
#   - a settling line still throws a few brief whole-line bursts.
#
# Nothing moves vertically. Heat is a function of how long ago the bar
# passed a pixel, not of where it is, so every pixel takes the same time
# to settle whatever the line's length. `scan` + `settle` == 1 keeps the
# last pixel resolving exactly at appear == 1, so the widget's switch to
# the direct paint path is invisible.

_GLITCH_WIPE = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float appear;
uniform float seed;
uniform float scan;      // fraction of the reveal the bar takes to cross
uniform float settle;    // fraction of the reveal one pixel takes to resolve
uniform float inset;     // bleed padding, px: the cursor stays inside it
uniform float cell;      // block size, px
uniform float blocky;    // fraction of a pixel's heat spent as blocks
uniform float slice_px;  // glitch slice height, px
uniform float seg_px;    // glitch slice length, px: slices are chunks, not bands
uniform float shift;     // peak sideways slice shift, px
uniform float density;   // fraction of slices shifted at a time
uniform float dropout;   // fraction of slice-rolls that go dark
uniform float rate;      // re-rolls per second
uniform float split;     // rgb separation, px
uniform float vsplit;    // vertical part of it, px
uniform float prism;
uniform float noise;
uniform float after;     // aftershock strength
uniform float after_rate;
uniform float bar_w;     // cursor bar width, px
uniform float glow_px;   // glow length behind the bar, px
uniform float3 spark;    // tint of hot pixels and colour of the blocks
uniform float3 cursor;   // colour of the bar

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }

half4 main(float2 xy) {
    // The bar starts one line-height left of the surface and leaves one
    // line-height past its right edge, so it enters and exits cleanly.
    float lead = res.y;
    float span = res.x + 2.0 * lead;
    float head = appear / scan * span - lead;      // bar x, px
    float ax   = (xy.x + lead) / span * scan;       // when the bar got here
    float roll = floor(t * rate) + seed;

    half4 col = half4(0.0);
    if (appear >= ax) {
        float heat = 1.0 - clamp((appear - ax) / settle, 0.0, 1.0);
        float burst = step(1.0 - after_rate, h1(roll * 0.37 + 3.0));
        heat = max(heat, burst * after * (1.0 - appear));

        // slices shift sideways; some are dark this roll. A slice is a
        // chunk `seg_px` long, not a band across the whole line: a band
        // dropping out took the same rows off every fresh glyph at once,
        // which read as the line being cut in half rather than torn.
        float slice = floor(xy.y / slice_px) * 917.0 + floor(xy.x / seg_px);
        float on = step(1.0 - density, h1(slice * 3.1 + roll));
        float dx = (h1(slice * 7.7 + roll * 1.3) - 0.5) * 2.0 * shift * heat * on;
        float drop = step(1.0 - dropout, h1(slice * 5.3 + roll * 0.7)) * step(0.45, heat);
        float2 sxy = xy + float2(dx, 0.0);

        // colour fringes, diagonal
        float sp = split * heat;
        float vs = vsplit * heat;
        half4 cc = src.eval(sxy);
        half4 cr = src.eval(sxy + float2( sp, -vs));
        half4 cb = src.eval(sxy + float2(-sp,  vs));
        half3 shifted = half3(cr.r, cc.g, cb.b);
        half3 primary = half3(cr.a, cc.a, cb.a);
        half3 rgb = mix(shifted, primary, half(clamp(prism * heat, 0.0, 1.0)));
        half a = max(cc.a, max(cr.a, cb.a));

        // blocks: coverage sampled on a coarse grid and thresholded, so
        // a fresh glyph is a run of solid cells rather than a shape
        float bk = smoothstep(1.0 - blocky, 1.0 - blocky + 0.12, heat);
        if (bk > 0.0) {
            float2 c0 = (floor(sxy / cell) + 0.5) * cell;
            float q = cell * 0.25;
            half cov = max(max(src.eval(c0 + float2(-q, -q)).a,
                               src.eval(c0 + float2( q, -q)).a),
                           max(src.eval(c0 + float2(-q,  q)).a,
                               src.eval(c0 + float2( q,  q)).a));
            half block = step(0.12, cov);
            // Cells are not one flat bar: each has its own brightness,
            // some are dark this roll, and a few take the cursor colour.
            float id = dot(floor(sxy / cell), float2(1.0, 131.0));
            float lit = 0.55 + 0.45 * h1(id * 1.7 + floor(roll * 0.5));
            float off = step(0.15, h1(id * 3.3 + roll));
            float mag = step(0.86, h1(id * 0.7 + seed));
            half3 bc = half3(mix(spark, cursor, mag)) * half(lit);
            block *= half(off);
            rgb = mix(rgb, bc * block, half(bk));
            a   = mix(a, block, half(bk));
        }

        rgb *= half(1.0 - drop);
        a   *= half(1.0 - drop);

        if (noise > 0.0) {
            rgb += half3(half((h2(xy + t * 11.0) - 0.5) * noise * heat)) * a;
        }
        rgb += half3(spark) * half(heat * 0.35) * a;
        col = half4(rgb, a);
    }

    // cursor: a bar at the head with a glow trailing it, over the
    // content rows only. Composited src-over on top of the text.
    float d = head - xy.x;
    float inrow = step(inset, xy.y) * step(xy.y, res.y - inset);
    float bar = smoothstep(bar_w, 0.0, abs(d));
    float glow = step(0.0, d) * exp(-d / max(1.0, glow_px)) * 0.45;
    float cur = clamp(bar + glow, 0.0, 1.0) * inrow;
    col = half4(half3(cursor) * half(cur) + col.rgb * half(1.0 - cur),
                half(cur) + col.a * half(1.0 - cur));

    col.rgb = min(col.rgb, half3(col.a));   // keep premultiplied output valid
    return col;
}
"""


class GlitchWipe(Effect):
    """Line-scale reveal: a cursor sweeps the line and the text decodes
    behind it. Pair with a widget, not a window — see `paint_through`.

    All distances are in pixels. `scan + settle` should be 1.0.
    """

    def __init__(self, duration: float = 0.8, scan: float = 0.55,
                 settle: float = 0.45,
                 cell: float = 3.0, blocky: float = 0.4,
                 slice_px: float = 3.0, seg_px: float = 22.0, shift: float = 5.0,
                 density: float = 0.35, dropout: float = 0.10,
                 rate: float = 24.0,
                 split: float = 3.0, vsplit: float = 1.0, prism: float = 0.9,
                 noise: float = 0.3,
                 after: float = 0.35, after_rate: float = 0.08,
                 bar_w: float = 2.0, glow_px: float = 14.0,
                 spark: tuple[float, float, float] = (0.36, 0.90, 0.94),
                 cursor: tuple[float, float, float] = (1.0, 0.165, 0.427),
                 fps: int = 60):
        self.duration = duration
        self.scan = scan
        self.settle = settle
        self.cell = cell
        self.blocky = blocky
        self.slice_px = slice_px
        self.seg_px = seg_px
        self.shift = shift
        self.density = density
        self.dropout = dropout
        self.rate = rate
        self.split = split
        self.vsplit = vsplit
        self.prism = prism
        self.noise = noise
        self.after = after
        self.after_rate = after_rate
        self.bar_w = bar_w
        self.glow_px = glow_px
        self.spark = spark
        self.cursor = cursor
        self.fps = fps
        self._effect = compile_sksl(_GLITCH_WIPE)

    def shader(self, src: skia.Shader, w: float, h: float, *,
               t: float, appear: float, seed: float = 0.0) -> skia.Shader:
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("res", [float(w), float(h)])
        b.setUniform("t", float(t))
        b.setUniform("appear", float(appear))
        b.setUniform("seed", float(seed))
        b.setUniform("scan", float(self.scan))
        b.setUniform("settle", float(self.settle))
        b.setUniform("inset", float(self.bleed(w, h)))
        b.setUniform("cell", float(self.cell))
        b.setUniform("blocky", float(self.blocky))
        b.setUniform("slice_px", float(self.slice_px))
        b.setUniform("seg_px", float(self.seg_px))
        b.setUniform("shift", float(self.shift))
        b.setUniform("density", float(self.density))
        b.setUniform("dropout", float(self.dropout))
        b.setUniform("rate", float(self.rate))
        b.setUniform("split", float(self.split))
        b.setUniform("vsplit", float(self.vsplit))
        b.setUniform("prism", float(self.prism))
        b.setUniform("noise", float(self.noise))
        b.setUniform("after", float(self.after))
        b.setUniform("after_rate", float(self.after_rate))
        b.setUniform("bar_w", float(self.bar_w))
        b.setUniform("glow_px", float(self.glow_px))
        b.setUniform("spark", list(self.spark))
        b.setUniform("cursor", list(self.cursor))
        return b.makeShader()

    def bleed(self, w: float, h: float) -> float:
        return max(self.shift + self.split, self.vsplit + self.cell) + 2.0
