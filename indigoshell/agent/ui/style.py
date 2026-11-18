"""The call overlay's look, in one place: sizes, colours, motion, and the
drawing every plate shares — the beveled frame, the toast-style heading,
the fonts. Tune the overlay here."""

import math
import time

import skia

from ... import shapes, text, theme
from ...widgets.base import Size


# ── geometry ────────────────────────────────────────────────────────────
PLATE_W = 680
CHIP = 64
CHIP_SM = 44
GAP = 8                 # plate ↔ chip
ROW_GAP = 10            # agent row ↔ your row
PAD_X = 16
PAD_Y = 12
BEVEL = 12
CORNERS = ("top-right", "bottom-left")
# The toasts' and the launcher's frame: 2px and fully opaque.
FRAME_W = 2.0
# Corner brackets inside every plate's frame (the bar's `Brackets`, the
# one piece of HUD chrome the bar kept), and a hairline inside each chip.
BRACKET_INSET = 6
BRACKET_ARM = 12
BRACKET_W = 1.5
BRACKET_ALPHA = 0.85
CHIP_INSET = 5.5
CHIP_INSET_ALPHA = 0.4
STATUS_W = 380

NAME_SIZE = 15          # the toast heading's size
TAG_SIZE = theme.FONT_SIZE - 4
BODY_SIZE = theme.FONT_SIZE - 1
ACT_SIZE = theme.FONT_SIZE - 3
MAX_BODY_ROWS = 30      # the turn's height before it scrolls, in reply rows
HISTORY_ROWS = 12       # past conversations listed at once
MAX_HEARD_LINES = 3
ACT_ROWS_BUSY = 3       # steps shown of the run still going
ACT_ROWS_IDLE = 2       # ... and of each finished run
ACT_ROWS_ALL = 30       # the most "every step" shows
OUT_ROWS = 12           # output rows of an expanded step
OUT_ROWS_FULL = 40      # ... after "more lines" is clicked

# ── colour ──────────────────────────────────────────────────────────────
# Each speaker owns one colour everywhere — frame, chip, name, and a tint
# in the plate itself: magenta for you, violet for the agent. The agent's
# frame and chip turn yellow while it asks and red when the line is down;
# those are signals, so its plate keeps the violet tint through them.
AGENT_ACCENT = theme.VIOLET_BRIGHT
YOU_ACCENT = theme.MAGENTA_BRIGHT
# The plate is the panels' violet-black pulled PLATE_TINT toward the
# speaker's colour, at 88%: the compositor's blur still shows faintly
# through, but it holds its contrast over a bright page too. At 62% a white
# window behind turned it a washed-out grey and the muted text vanished
# (prototypes/visuals/call_opacity.png).
PLATE_BASE = "#170620"
PLATE_ALPHA = 0.88
PLATE_TINT = 0.12


def plate_fill(accent: str) -> tuple[str, float]:
    return theme.lerp(PLATE_BASE, accent, PLATE_TINT), PLATE_ALPHA


AGENT_FILL = plate_fill(AGENT_ACCENT)
YOU_FILL = plate_fill(YOU_ACCENT)
SLASH_FG = theme.NOTIF_SEPARATOR_FG     # the toasts' cyan `//`
ASK_ACCENT = theme.YELLOW_BRIGHT
FAIL_ACCENT = theme.ERROR
# A hung-up call ringing back, and missed until answered: the bar's
# urgent-workspace red, which blinks in the same pattern. Violet was too
# easy to miss in the corner (user's call).
RING_ACCENT = theme.ERROR
TOOL_FG = theme.CYAN_BRIGHT
CODE_FG = theme.YELLOW_BRIGHT
LINK_FG = theme.CYAN_BRIGHT
SELECT_BG = (theme.VIOLET_BRIGHT, 0.38)

# ── motion ──────────────────────────────────────────────────────────────
# Arc speeds in degrees per second, by what the agent is doing.
SPIN = {"dialing": 360.0, "thinking": 110.0, "working": 200.0,
        "speaking": 260.0}
CHIP_FPS = 30
BLINK_FPS = 20
KICK_S = 0.45           # the half-turn your chip makes on a send
SETTLE_RATE = 9.0       # how fast stopped arcs ease back to rest (1/s)
RING_FOR_S = 40.0       # a hung-up ring blinks this long, then "missed"
# An answer landing in an open call: two blinks, ending inside the second
# flash (the ring pattern is on 0-150ms and 250-400ms).
NUDGE_S = 0.4
HANGUP_CLOSE_S = 1.8    # the agent's hang-up line stays this long
COPIED_S = 1.2          # the "copied" tag in the agent's heading
COPIED_FPS = 4          # ...and how promptly it comes down again
DOTS_PER_S = 4.0        # "thinking" gains a dot this often, up to three
CLICK_S = 0.4           # double/triple-click window


UNBOUNDED = Size(10_000.0, 10_000.0)

STATUS_WORDS = {
    "offline": "off the line", "dialing": "dialing", "idle": "on the line",
    "thinking": "thinking", "working": "working", "speaking": "speaking",
    "asking": "needs your ok", "error": "no line",
}


# ── shared drawing ──────────────────────────────────────────────────────
def plate_path(r: skia.Rect, inset: float = 0.0) -> skia.Path:
    return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                          bevel=BEVEL, corners=CORNERS, inset=inset)


def frame_inset() -> float:
    # Half the width in, as the toasts do, plus half a pixel: the
    # window's SHAPE is aliased, and a stroke riding the plate's very
    # edge would lose its outer antialiasing to it.
    return FRAME_W / 2 + 0.5


def bevel_at(d: float, bevel: float = BEVEL) -> float:
    """The bevel of a path `d` px inside a beveled one, so the two 45°
    cuts stay parallel (an inset rect with the same bevel would not)."""
    return max(0.0, bevel - d * (2 - math.sqrt(2)))


def inner_path(r: skia.Rect, d: float, bevel: float = BEVEL) -> skia.Path:
    """The plate's outline, `d` px inside it."""
    return shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                          bevel=bevel_at(d, bevel), corners=CORNERS, inset=d)


def corner_marks(r: skia.Rect, d: float = BRACKET_INSET,
                 arm: float = BRACKET_ARM, bevel: float = BEVEL) -> skia.Path:
    """Corner brackets `d` px inside a beveled rect — the bar's
    `Brackets`, fitted to the plate: an L on a square corner, a short
    arm-cut-arm along a 45° one."""
    x0, y0, x1, y1 = r.left() + d, r.top() + d, r.right() - d, r.bottom() - d
    b = bevel_at(d, bevel)
    path = skia.Path()
    for name, cx, cy, sx, sy in (("top-left", x0, y0, 1, 1),
                                 ("top-right", x1, y0, -1, 1),
                                 ("bottom-right", x1, y1, -1, -1),
                                 ("bottom-left", x0, y1, 1, -1)):
        if name in CORNERS:
            h = arm * 0.5
            path.moveTo(cx + sx * (b + h), cy)
            path.lineTo(cx + sx * b, cy)
            path.lineTo(cx, cy + sy * b)
            path.lineTo(cx, cy + sy * (b + h))
        else:
            path.moveTo(cx + sx * arm, cy)
            path.lineTo(cx, cy)
            path.lineTo(cx, cy + sy * arm)
    return path


# The hologram's raster (see holo.py): a faint line every few pixels,
# on the window's own grid so it runs on from one surface to the next.
RASTER_PITCH = 3
RASTER_ALPHA = 0.07
RASTER_LIFT = 0.35          # how far the raster's colour leans to white

_rasters: dict[tuple[str, float], skia.Shader] = {}


def raster(color: str, alpha: float = RASTER_ALPHA) -> skia.Shader:
    """Horizontal lines one px tall, every `RASTER_PITCH`, aligned to the
    canvas's own y — so every surface in a window shares the grid."""
    key = (color, alpha)
    shader = _rasters.get(key)
    if shader is None:
        s = skia.Surface(1, RASTER_PITCH)
        c = s.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        p = skia.Paint()
        p.setColor(theme.color(theme.lerp(color, theme.HUD_FG_BRIGHT,
                                          RASTER_LIFT), alpha))
        c.drawRect(skia.Rect.MakeXYWH(0, 0, 1, 1), p)
        shader = s.makeImageSnapshot().makeShader(
            skia.TileMode.kRepeat, skia.TileMode.kRepeat,
            skia.SamplingOptions())
        _rasters[key] = shader
    return shader


def paint_raster(canvas: skia.Canvas, shape: skia.Path, color: str,
                 alpha: float = RASTER_ALPHA) -> None:
    canvas.drawPath(shape, skia.Paint(AntiAlias=True,
                                      Shader=raster(color, alpha)))


def paint_plate(canvas: skia.Canvas, r: skia.Rect, frame: str,
                fill_color: tuple[str, float]) -> None:
    """A plate: the tinted fill and its raster, the 2px frame, and
    corner brackets inside it in the frame's colour."""
    fill = skia.Paint(AntiAlias=True)
    fill.setColor(theme.color(*fill_color))
    canvas.drawPath(plate_path(r), fill)
    paint_raster(canvas, plate_path(r), frame)
    stroke = skia.Paint(AntiAlias=True)
    stroke.setStyle(skia.Paint.kStroke_Style)
    stroke.setStrokeWidth(FRAME_W)
    stroke.setColor(theme.color(frame))
    canvas.drawPath(plate_path(r, inset=frame_inset()), stroke)
    marks = skia.Paint(AntiAlias=True)
    marks.setStyle(skia.Paint.kStroke_Style)
    marks.setStrokeWidth(BRACKET_W)
    marks.setColor(theme.color(frame, BRACKET_ALPHA))
    canvas.drawPath(corner_marks(r), marks)


def paint_heading(canvas: skia.Canvas, box: skia.Rect, size: float,
                  parts: tuple[tuple[str, str, bool], ...]) -> float:
    """The toasts' `// SUMMARY app` heading: runs of (text, colour,
    bold) after a cyan `//`, a space apart. Returns where it ends."""
    base = text.baseline_in(box, size, bold=True)
    x = box.left()
    for value, color, bold in (("//", SLASH_FG, True), *parts):
        if not value:
            continue
        text.draw(canvas, value, x, base, size, theme.color(color), bold)
        x += text.measure(value, size, bold) + text.measure(" ", size, bold)
    return x


_italics: dict[tuple[float, bool], skia.Font] = {}


def font_for(size: float, bold: bool, italic: bool) -> skia.Font:
    if not italic:
        return text.font(size, bold)
    key = (size, bold)
    hit = _italics.get(key)
    if hit is None:
        # The face has no italic; a skew reads as one on a mono grid.
        hit = skia.Font(text.typeface(bold), size)
        hit.setSubpixel(True)
        hit.setEdging(skia.Font.Edging.kAntiAlias)
        hit.setSkewX(-0.2)
        _italics[key] = hit
    return hit


def clock_text(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def ago(epoch: float) -> str:
    s = max(0, time.time() - epoch)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    if s < 7 * 86400:
        return f"{int(s // 86400)}d ago"
    return time.strftime("%b %d", time.localtime(epoch))


def accent_of(agent) -> str:
    if agent.approval is not None or agent.ringing == "ask":
        return ASK_ACCENT
    if agent.ringing is not None:       # only a closed call rings
        return RING_ACCENT
    if agent.phase == "error":
        return FAIL_ACCENT
    return AGENT_ACCENT


def blink_on(elapsed: float) -> bool:
    """Phone-ring blink: the workspace urgent pattern, looped."""
    pattern = theme.WORKSPACE_URGENT_RING
    period = sum(ms for ms, _ in pattern) / 1000.0
    t = (elapsed % period) * 1000.0
    for ms, visible in pattern:
        if t < ms:
            return visible
        t -= ms
    return True


def line_h(size: float, bold: bool = False) -> float:
    return math.ceil(text.line_height(size, bold))
