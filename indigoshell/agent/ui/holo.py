"""The call as a hologram: every plate and chip is one projection.

**The raster** is the projection's grain: faint horizontal lines every
few pixels across each surface, under its content (`style.raster`, drawn
by `paint_plate` and the chips). The lines sit on the window's own pixel
grid, so they run on unbroken from a chip into the plate beside it — two
pieces of one image, not two textured boxes.

**The scan** is the projector refreshing that image, and the image
glitching as it does. Every few seconds a thick glowing band runs slowly
down the whole call, crossing your plate and chip, then the agent's, at
the same height at the same moment. Where it is, the hologram loses its
shape: rows tear sideways in chunks — so the plates' outlines notch and
jog, not just their text — colours split apart, a few slices drop out;
above it the raster stays lit for a while and fades. In the gaps between
surfaces there is nothing to project onto, so nothing shows.

It distorts what is really there: `Projection` paints after the plates
and chips, snapshots the band they just drew, and redraws it through one
shader (`_SCAN`). It is a widget of its own whose dirty area is just the
strip the band moved through, so a pass repaints a band of the window,
not every plate it crosses.
"""

import time

import skia

from ...effects import compile_sksl
from ...widgets.base import Size, Widget
from .style import RASTER_PITCH

SCAN_EVERY_S = 7.0          # one pass starts this often
SCAN_SPEED = 170.0          # px/s down the call
SCAN_HEAD_PX = 11.0         # the band's half-thickness
SCAN_WAKE_PX = 100.0        # the lit raster above it
SCAN_SHIFT_PX = 9.0         # sideways tear at the band, at most
SCAN_SPLIT_PX = 2.5         # colour separation at the band
SCAN_RATE = 18.0            # how often the tears re-roll, per second
SCAN_FPS = 30
_BELOW = 2.5                # band half-thicknesses it reaches ahead

_SCAN = """
uniform shader src;
uniform float head;      // the band's centre, canvas y
uniform float t;
uniform float seed;
uniform float head_px;
uniform float wake;
uniform float shift;
uniform float split;
uniform float rate;
uniform float pitch;

float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }

half4 main(float2 xy) {
    float d = head - xy.y;                       // > 0: already scanned
    float band = exp(-(d * d) / (head_px * head_px));
    float lit = d >= 0.0 ? exp(-d / (wake * 0.35)) : band;
    float roll = floor(t * rate) + seed;

    // Tears, only at the band, at two scales. 9px bands of rows shift
    // sideways whole, so the plates' and chips' outlines jog with them;
    // 3px slices also break into chunks of random length that jitter on
    // their own. Every tear is a clear jump: a 1-2px offset between the
    // top and bottom of a line, the same for every glyph across it,
    // reads as italic type rather than as a tear. Strength is taken at
    // each piece's centre so no piece shears.
    float g = floor(xy.y / 9.0);
    float s = floor(xy.y / 3.0);
    float dg = head - (g + 0.5) * 9.0;
    float ds = head - (s + 0.5) * 3.0;
    float bg = exp(-(dg * dg) / (head_px * head_px));
    float bs = exp(-(ds * ds) / (head_px * head_px));
    float hs = max(bs, (ds >= 0.0 ? exp(-ds / (wake * 0.35)) : bs) * 0.45);
    float seg_len = 24.0 + 96.0 * h1(s * 2.3 + roll * 0.9);
    float seg = floor((xy.x + h1(s * 4.1 + roll) * seg_len) / seg_len);
    float piece = s * 131.0 + seg;
    float on_g = step(1.0 - 0.7 * bg, h1(g * 3.7 + roll));
    float on_p = step(1.0 - 0.6 * bs, h1(piece * 1.9 + roll * 0.5));
    float sg = h1(g * 7.1 + roll * 1.3) < 0.5 ? -1.0 : 1.0;
    float sp_ = h1(piece * 5.3 + roll * 0.7) < 0.5 ? -1.0 : 1.0;
    float dx = sg * mix(4.0, shift, h1(g * 2.9 + roll)) * on_g
             + sp_ * mix(2.0, shift * 0.6, h1(piece * 6.7 + roll)) * on_p;
    float2 p = float2(xy.x + floor(dx), xy.y);

    // Whole pixels, per slice, for the same reason: a split that grew
    // row by row sheared the red and blue channels into a slant.
    float sp = floor(split * hs + 0.5);
    half4 c = src.eval(p);
    half4 r = src.eval(p + float2(sp, 0.0));
    half4 b = src.eval(p - float2(sp, 0.0));
    half a = max(c.a, max(r.a, b.a));
    half3 rgb = half3(r.r, c.g, b.b);

    // The band glows; behind it the raster rows stay lit, fading.
    float row = step(mod(xy.y, pitch), 1.0);
    float k = band * 0.9 + lit * (0.10 + 0.45 * row);
    rgb = rgb * half(1.0 + k) + half3(a) * half(0.16 * band);

    float drop = step(0.9, h1(piece * 9.1 + roll * 0.3)) * step(0.4, bs);
    rgb *= half(1.0 - 0.75 * drop);
    a *= half(1.0 - 0.75 * drop);
    return half4(min(rgb, half3(a)), a);
}
"""


class Projection(Widget):
    """The scan down the whole hologram; place it last in its host, over
    the surfaces it distorts.

    A pass starts every SCAN_EVERY_S, counted from `epoch`, and runs at
    a constant speed until its wake has left the bottom of the call —
    so where the band is follows from the time alone, whatever the call
    grows or shrinks to meanwhile. It asks for frames only while a pass
    is on the call; a timer wakes it for the next one, so an idle call's
    clock is not kept running in between.
    """

    def __init__(self) -> None:
        super().__init__()
        self.epoch = time.monotonic()
        self._effect = compile_sksl(_SCAN)
        self._wake_handle = None
        self._seed = 0.0
        self._t = 0.0
        # The band as of the last `animate()` — what `paint()` draws.
        self._y: float | None = None
        self._shown = skia.Rect.MakeEmpty()     # the strip on screen now
        self._dirty = skia.Rect.MakeEmpty()     # ...and where it just was

    # ── timing ──────────────────────────────────────────────────────────
    def _into_pass(self) -> float:
        """Seconds since the current pass started."""
        return (time.monotonic() - self.epoch) % SCAN_EVERY_S

    def _band_y(self) -> float | None:
        """Where the band's centre is now; None between passes."""
        y = (self.rect.top() - _BELOW * SCAN_HEAD_PX
             + self._into_pass() * SCAN_SPEED)
        over = self.rect.bottom() + SCAN_WAKE_PX + _BELOW * SCAN_HEAD_PX
        return y if y < over else None

    @property
    def animation_fps(self) -> int:
        return SCAN_FPS if self._band_y() is not None else 0

    def attach(self, window) -> None:
        self._forget()
        super().attach(window)
        self._arm()

    def detach(self) -> None:
        if self._wake_handle is not None:
            self._wake_handle.cancel()
            self._wake_handle = None
        self._forget()
        super().detach()

    def _arm(self) -> None:
        """A wake-up for the start of the next pass. Without a loop —
        an offline render, which steps the time itself — there is
        nothing to wake."""
        if self._wake_handle is not None:
            self._wake_handle.cancel()
            self._wake_handle = None
        loop = getattr(self.window, "loop", None)
        if loop is not None:
            self._wake_handle = loop.call_later(
                SCAN_EVERY_S - self._into_pass(), self._wake)

    def _wake(self) -> None:
        self._wake_handle = None
        if self.window is None:
            return
        self._arm()
        self.invalidate()       # nothing to repaint yet: starts the clock

    def animate(self, t: float) -> None:
        was = self._y
        self._y = self._band_y()
        self._t = time.monotonic()
        if self._y is not None and (was is None or self._y < was):
            self._seed = (self._t * 7.31) % 97.0     # a new pass, new tears
        strip = (self._strip(self._y) if self._y is not None
                 else skia.Rect.MakeEmpty())
        # The frame clock repaints `paint_bounds()` after this: the strip
        # the band is in now, and the one it left, which must be cleared.
        # When the pass is over that is its last strip alone — the frame
        # the window gives a widget that has stopped asking for them.
        self._dirty = skia.Rect.MakeLTRB(self._shown.left(), self._shown.top(),
                                         self._shown.right(), self._shown.bottom())
        self._dirty.join(strip)
        self._shown = strip

    def _strip(self, y: float) -> skia.Rect:
        pad = SCAN_SHIFT_PX * 1.4 + SCAN_SPLIT_PX + 2
        return skia.Rect.MakeLTRB(self.rect.left() - pad,
                                  y - SCAN_WAKE_PX - 2,
                                  self.rect.right() + pad,
                                  y + _BELOW * SCAN_HEAD_PX + 2)

    def measure(self, avail: Size) -> Size:
        return Size(0, 0)

    def paint_bounds(self) -> skia.Rect:
        return self._dirty

    def _forget(self) -> None:
        """No band: a call closed mid-pass reopens clean."""
        self._y = None
        self._shown = skia.Rect.MakeEmpty()
        self._dirty = skia.Rect.MakeEmpty()

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        # From `animate()`'s reading, not the time now: the band drawn is
        # the one whose strip was marked dirty.
        y = self._y
        surface = canvas.getSurface()
        m = canvas.getTotalMatrix()
        if y is None or surface is None or not m.isTranslate():
            return
        tx, ty = m.getTranslateX(), m.getTranslateY()
        # Only what was painted fresh this pass: the band within the
        # current clip. Pixels outside it still hold last frame's tears.
        strip = self._strip(y)
        dev = skia.Rect.MakeLTRB(strip.left() + tx, strip.top() + ty,
                                 strip.right() + tx, strip.bottom() + ty).roundOut()
        if not dev.intersect(canvas.getDeviceClipBounds()):
            return
        image = surface.makeImageSnapshot(dev)
        if image is None:
            return
        local = skia.Rect.MakeLTRB(dev.left() - tx, dev.top() - ty,
                                   dev.right() - tx, dev.bottom() - ty)
        src = image.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal,
                               skia.SamplingOptions(),
                               skia.Matrix.Translate(local.left(), local.top()))
        b = skia.RuntimeEffectBuilder(self._effect)
        b.setChild("src", src)
        b.setUniform("head", float(y))
        b.setUniform("t", float(self._t))
        b.setUniform("seed", float(self._seed))
        b.setUniform("head_px", SCAN_HEAD_PX)
        b.setUniform("wake", SCAN_WAKE_PX)
        b.setUniform("shift", SCAN_SHIFT_PX)
        b.setUniform("split", SCAN_SPLIT_PX)
        b.setUniform("rate", SCAN_RATE)
        b.setUniform("pitch", float(RASTER_PITCH))
        paint = skia.Paint(Shader=b.makeShader())
        paint.setBlendMode(skia.BlendMode.kSrc)     # replace, tears and all
        canvas.drawRect(local, paint)
