"""A tab's image as the bar draws it: a page's icon (or, while it has
none, its letter) glowing in the tab's colour (the user's look: "inverted
the brightness so the background turns black and the icons glow cyan,
then added a soft bloom").

Each pixel's brightness (Rec. 709 luma of its colour) is turned round,
so a white or light ground goes black and dark marks go bright; the
result is the tint (cyan, or the agent's colour on a tab she holds) at
that brightness, the pixel's own alpha kept. Over it goes a blurred
copy of the light alone (BLOOM: its blur and strength), the lighter of
the two kept at each pixel: it lights the dark round the marks and
between them, over a black ground too, and bleeds BLOOM.pad px past the
icon's square on each side, but never brightens a mark past the tint.
Added instead, it would push a light tint to white where the marks are
(the agent's lilac reads as white then, not as hers).

Turned round only when the icon is drawn for a light ground. Favicons
mostly are (a browser's light tab strip), but a page in the dark scheme
Flux asks for may give one made for a dark strip: light marks on
nothing (GitHub's), or a dark tile; turning those round would leave
black marks on the bar's near-black. So its shape decides (`_turned`):
an icon that is a solid shape (a tile, a disk: each row filled between
its ends, SOLID) has a ground, and is turned round when the shape's
edge is light (Wikipedia's white tile, Google's white disk); one that
is marks on nothing (holes in it: GitHub's cat, Arch's A) when its
marks are dark. Either way the ground ends dark and the marks glow.

Made once per image, colour and bloom and kept (`CACHE_MAX`, the least
recently drawn let go first): a frame only draws the finished image, at
whole pixels so it stays crisp and leaves no anti-aliased fringe for a
partial repaint to keep (see window.py on the GPU's clip). An image is
known by skia's unique id for it, which a process never gives twice.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass

import skia

from ... import text

ICON_PX = 16                        # an image drawn this size, in its tab's spot
LETTER_SIZE = 13                    # a tab with no image: its letter, bold, this size
LUMA = (0.2126, 0.7152, 0.0722)     # Rec. 709: what "brightness" is here
SOLID = 0.85                        # an icon's rows this filled between their ends: a ground
LIGHT = 0.5                         # brighter than this: a light ground, light marks
CACHE_MAX = 256                     # finished images kept


@dataclass(frozen=True)
class Bloom:
    """The blurred copy over an icon: its blur (sigma, px) and its
    strength (0..1)."""
    sigma: float
    gain: float

    @property
    def pad(self) -> int:
        """How far it reaches past the icon's square, px."""
        return math.ceil(3 * self.sigma)


# Wide and whole: at 1x a tighter one (1.6 px at 70%) all but vanishes
# into the icon (prototypes/visuals/render_flux_icons.py has both).
BLOOM = Bloom(3.0, 1.0)

_RGBA = skia.ImageInfo.Make(ICON_PX, ICON_PX, skia.kRGBA_8888_ColorType,
                            skia.kUnpremul_AlphaType)
_cache: OrderedDict = OrderedDict()
made = 0                            # finished images made, for the tests


@dataclass(frozen=True)
class Glow:
    """A finished image: the icon's ICON_PX square sits `pad` px in from
    its top left corner."""
    image: skia.Image
    pad: int


def decode(png: bytes) -> skia.Image | None:
    """A PNG as an image decoded now (not at its first paint); None when
    it does not decode."""
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    return image.makeRasterImage() if image is not None else None


def of_image(image: skia.Image, color: str, bloom: Bloom | None = None) -> Glow:
    """`image` (any size: fitted in ICON_PX, its shape kept) glowing in
    `color`, with `bloom` (BLOOM by default)."""
    bloom = bloom or BLOOM
    key = ("image", image.uniqueID(), color, bloom)
    return _cached(key, lambda: _finish(*_icon_pixels(image), color, bloom))


def of_letter(letter: str, color: str, bloom: Bloom | None = None) -> Glow:
    """A tab with no image: its letter (a capital) glowing in `color`,
    as an icon of light marks on nothing would."""
    bloom = bloom or BLOOM
    key = ("letter", letter, color, bloom)
    return _cached(key, lambda: _finish(_letter_pixels(letter), False, color, bloom))


def paint(canvas: skia.Canvas, glow: Glow, spot: skia.Rect, alpha: float = 1.0) -> None:
    """`glow` centred in `spot`, at whole pixels."""
    x = round(spot.centerX() - ICON_PX / 2) - glow.pad
    y = round(spot.centerY() - ICON_PX / 2) - glow.pad
    p = skia.Paint()
    if alpha < 1.0:
        p.setAlphaf(max(0.0, alpha))
    canvas.drawImage(glow.image, x, y, skia.SamplingOptions(), p)


# ── making one ──────────────────────────────────────────────────────────
def _cached(key, make) -> Glow:
    glow = _cache.get(key)
    if glow is not None:
        _cache.move_to_end(key)
        return glow
    global made
    glow = make()
    made += 1
    _cache[key] = glow
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return glow


def _read(surface: skia.Surface) -> bytearray:
    out = bytearray(ICON_PX * ICON_PX * 4)
    surface.makeImageSnapshot().readPixels(_RGBA, out, ICON_PX * 4)
    return out


def _icon_pixels(image: skia.Image) -> tuple[bytearray, bool]:
    """The image fitted in the ICON_PX square (centred, its shape kept,
    filtered down), as unpremultiplied RGBA; and whether it is turned
    round (`_turned`, on the part the image covers)."""
    w, h = image.width(), image.height()
    k = ICON_PX / max(w, h)
    dw, dh = max(1, round(w * k)), max(1, round(h * k))
    left, top = (ICON_PX - dw) // 2, (ICON_PX - dh) // 2
    surface = skia.Surface.MakeRasterN32Premul(ICON_PX, ICON_PX)
    c = surface.getCanvas()
    c.clear(skia.ColorTRANSPARENT)
    c.drawImageRect(image, skia.Rect.MakeXYWH(left, top, dw, dh),
                    skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kNone))
    px = _read(surface)
    return px, _turned(px, left, top, dw, dh)


def _letter_pixels(letter: str) -> bytearray:
    surface = skia.Surface.MakeRasterN32Premul(ICON_PX, ICON_PX)
    c = surface.getCanvas()
    c.clear(skia.ColorTRANSPARENT)
    ch = letter.upper()
    m = text.font(LETTER_SIZE, True).getMetrics()
    x = (ICON_PX - text.measure(ch, LETTER_SIZE, True)) / 2
    base = round(ICON_PX / 2 - (m.fAscent + m.fDescent) / 2)
    text.draw(c, ch, round(x), base, LETTER_SIZE, skia.ColorWHITE, True)
    return _read(surface)


def _luma(px, i: int) -> float:
    return (LUMA[0] * px[i] + LUMA[1] * px[i + 1] + LUMA[2] * px[i + 2]) / 255


def _turned(px, left: int, top: int, w: int, h: int) -> bool:
    """Whether an icon is drawn for a light ground (the module
    docstring), from the rect it covers: a solid shape whose edge (each
    row's first and last covered pixel) is light, or marks on nothing
    that are dark on the whole."""
    filled = spans = 0
    edge = edge_a = marks = marks_a = 0.0
    for y in range(top, top + h):
        row = [x for x in range(left, left + w) if px[(y * ICON_PX + x) * 4 + 3] >= 128]
        if row:
            spans += row[-1] - row[0] + 1
            filled += len(row)
            for x in {row[0], row[-1]}:
                i = (y * ICON_PX + x) * 4
                edge += px[i + 3] * _luma(px, i)
                edge_a += px[i + 3]
        for x in range(left, left + w):
            i = (y * ICON_PX + x) * 4
            marks += px[i + 3] * _luma(px, i)
            marks_a += px[i + 3]
    if not marks_a:
        return False
    if spans and filled / spans >= SOLID:
        return edge / edge_a > LIGHT
    return marks / marks_a <= LIGHT


def _finish(px: bytearray, turned: bool, color: str, bloom: Bloom) -> Glow:
    """The icon in `color` by its brightness (turned round or not), its
    alpha kept, and the bloom over it (the lighter kept), on a square
    `bloom.pad` px wider each side."""
    tint = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    icon, light = bytearray(len(px)), bytearray(len(px))
    for i in range(0, len(px), 4):
        a = px[i + 3]
        if not a:
            continue
        m = _luma(px, i)
        if turned:
            m = 1.0 - m
        for j in range(3):
            icon[i + j] = round(tint[j] * m)
            light[i + j] = tint[j]
        icon[i + 3] = a
        light[i + 3] = round(a * m)
    size = (ICON_PX, ICON_PX)
    icon_img = skia.Image.frombytes(icon, size, skia.kRGBA_8888_ColorType)
    light_img = skia.Image.frombytes(light, size, skia.kRGBA_8888_ColorType)
    pad = bloom.pad
    side = ICON_PX + 2 * pad
    surface = skia.Surface.MakeRasterN32Premul(side, side)
    c = surface.getCanvas()
    c.clear(skia.ColorTRANSPARENT)
    c.drawImage(icon_img, pad, pad)
    p = skia.Paint(ImageFilter=skia.ImageFilters.Blur(bloom.sigma, bloom.sigma,
                                                      skia.TileMode.kDecal, None),
                   BlendMode=skia.BlendMode.kLighten)
    p.setAlphaf(bloom.gain)
    c.drawImage(light_img, pad, pad, skia.SamplingOptions(), p)
    return Glow(surface.makeImageSnapshot().makeRasterImage(), pad)
