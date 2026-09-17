"""INDIGO Cyberpunk theme.

Ported from v1's theme.py, which was already pure data — the only change
is the color helper: hex strings are parsed once into Skia's packed ARGB
ints and cached, so paint code never parses a string per frame.

Palette and semantic tokens are verbatim; the widget-domain tokens will
come across as their widgets are ported.
"""

import skia

# ── Palette ─────────────────────────────────────────────────────────────
# Night City neon: hot magenta primary, electric cyan data, neon yellow
# time, violet accent, hot crimson error. Backgrounds are deep blue-violet
# black — cool enough to throw the neons forward without crushing the glow.

BASE_BLACK      = "#050310"
BASE_SHADOW     = "#0d0820"
BASE_GUTTER     = "#15102a"
BASE_SURFACE    = "#1e1838"
BASE_MUTED      = "#5a4a78"

MAGENTA_DIM     = "#3a0a2a"
MAGENTA_MID     = "#d1004f"
MAGENTA_BRIGHT  = "#ff2a6d"
MAGENTA_BLOOM   = "#ff80b0"

YELLOW_FAINT    = "#3a3010"
YELLOW_DIM      = "#a89020"
YELLOW_MID      = "#e0c020"
YELLOW_BRIGHT   = "#fcee0c"

CYAN_FAINT      = "#0a2030"
CYAN_DIM        = "#0d4a5e"
CYAN_MID        = "#05a9c4"
CYAN_BRIGHT     = "#05d9e8"
# Top of the ramp, for a flash that has to read against CYAN_BRIGHT's own
# neighbours. Same construction as MAGENTA_BLOOM: the bright lifted ~35%
# toward white, which is the step that buys luminance without leaving the
# hue. CYAN_MID -> CYAN_BRIGHT is only +27% luminance; -> BLOOM is +48%.
CYAN_BLOOM      = "#5ce6f0"

LIME_DIM        = "#3a4a0a"
LIME_MID        = "#99cc00"
LIME_BRIGHT     = "#ccff00"

VIOLET_DIM      = "#2a0a3a"
VIOLET          = "#7700a6"
VIOLET_BRIGHT   = "#b967ff"

ERROR           = "#ff003c"

# ── Semantic roles ──────────────────────────────────────────────────────
BG              = BASE_BLACK
BG_SHADOW       = BASE_SHADOW

FG              = MAGENTA_MID
FG_MUTED        = MAGENTA_DIM
FG_STRONG       = MAGENTA_BRIGHT
FG_ACCENT       = YELLOW_BRIGHT
ICON            = VIOLET_BRIGHT
HIGHLIGHT       = CYAN_BRIGHT

# HUD body text — the braindance overlay's `fg`/`fg_hi`/`muted` triad.
# Long rows of neon are unreadable, so its panels set labels in a cool
# blue-grey and spend the neon on values and accents. BASE_MUTED already
# matches its `muted` exactly.
HUD_FG          = "#a0a8c8"
HUD_FG_BRIGHT   = "#c8d0e8"
HUD_PLATE       = "#241840"            # selected-row / active-tab fill
HUD_SECTION     = "#1a1030"            # section header fill

# ── Typography ──────────────────────────────────────────────────────────
FONT            = "FiraCode Nerd Font Mono"
FONT_FALLBACKS  = ("FiraCode Nerd Font", "JetBrainsMono Nerd Font",
                   "DejaVu Sans Mono", "monospace")
FONT_SIZE       = 16
FONT_SIZE_LG    = 18
FONT_SIZE_ICON  = 22
FONT_SIZE_XL    = 28

# ── Spacing ─────────────────────────────────────────────────────────────
SPACING_SM      = 10
SPACING_MD      = 12
SPACING_LG      = 14
SPACING_XL      = 20
BAR_SPACING     = 20   # between top-level bar widgets

# ── Bar window ──────────────────────────────────────────────────────────
BAR_POSITION    = "bottom"
BAR_HEIGHT      = 42
BAR_MARGIN      = 4     # horizontal gutter inside the bar window
BAR_BG          = BASE_BLACK
# Accent rule along the bar's outer edge. Pure decoration — it exists so
# an empty bar is visibly *rendered by us* rather than just a black
# rectangle, and it exercises the Skia blur path from frame one.
BAR_RULE        = MAGENTA_BRIGHT
BAR_RULE_THICK  = 2
BAR_RULE_GLOW   = 7.0   # blur sigma

MUSIC_FG                = CYAN_MID
STATUS_FG               = MAGENTA_DIM

# ── Workspaces ──────────────────────────────────────────────────────────
WORKSPACE_CURRENT_FG    = MAGENTA_BRIGHT
WORKSPACE_OCCUPIED_FG   = YELLOW_DIM
WORKSPACE_EMPTY_FG      = MAGENTA_DIM
WORKSPACE_URGENT_FG     = ERROR   # window set _NET_WM_STATE_DEMANDS_ATTENTION
# Ring pattern: (ms, visible) frames. Two short pulses with a gap, then a
# longer pause — like a phone ring. None/[] disables blinking.
WORKSPACE_URGENT_RING   = [
    (150, True),
    (100, False),
    (150, True),
    (700, False),
]

# ── Notifications ───────────────────────────────────────────────────────
# Verbatim from v1 — the toast recipe predates the Skia port.
NOTIF_WIDTH             = 500
NOTIF_GAP               = 10            # vertical px between stacked toasts
NOTIF_PADDING_X         = 24
NOTIF_PADDING_Y         = 14
NOTIF_BORDER_THICK      = 2.0   # frame stroke width
NOTIF_BEVEL             = 12    # 45° corner cut depth
NOTIF_BEVEL_CORNERS     = ("top-right", "bottom-left")

NOTIF_METER_SEGMENTS    = 28
NOTIF_METER_GAP         = 2
NOTIF_METER_THICK       = 6
NOTIF_METER_INSET_Y     = 10    # gap between bottom border and meter
NOTIF_METER_DIM         = BASE_GUTTER

NOTIF_OFFSET_X          = 10            # from screen right edge
NOTIF_OFFSET_Y          = 50            # from screen bottom (above the bar)
NOTIF_BG                = BASE_BLACK
NOTIF_FRAME_LOW         = BASE_GUTTER
NOTIF_FRAME_NORMAL      = CYAN_BRIGHT
NOTIF_FRAME_CRITICAL    = ERROR
NOTIF_BODY_FG_NORMAL    = VIOLET_BRIGHT
NOTIF_BODY_FG_CRITICAL  = ERROR
NOTIF_SEPARATOR_FG      = CYAN_BRIGHT   # the "//" marker
NOTIF_SUMMARY_FG        = MAGENTA_BRIGHT
NOTIF_APPNAME_FG        = BASE_MUTED
NOTIF_ACTION_FG         = CYAN_BRIGHT
NOTIF_ACTION_BG         = BASE_GUTTER
NOTIF_TIMEOUT_LOW_MS    = 5000
NOTIF_TIMEOUT_NORMAL_MS = 5000
NOTIF_TIMEOUT_CRITICAL  = 0     # 0 = never auto-dismiss; click to close
NOTIF_TIMER_BORDER_FG   = BASE_MUTED

# ── Popup window ────────────────────────────────────────────────────────
POPUP_BG        = "#170620F2"           # tinted violet-black, ~95% alpha
POPUP_BORDER    = HIGHLIGHT             # cyan-bright beveled stroke
POPUP_BEVEL     = 16
POPUP_BEVEL_CORNERS = ("top-right", "bottom-left")
POPUP_GAP       = 10                    # clearance from the bar


# ── color helpers ───────────────────────────────────────────────────────
_CACHE: dict[str, int] = {}


def color(spec: str, alpha: float | None = None) -> int:
    """Packed ARGB int for a #RGB / #RRGGBB / #RRGGBBAA string.

    Cached, because paint code asks for the same handful of colors on
    every frame and hex parsing in a render loop is pure waste. `alpha`
    (0..1) overrides the string's own alpha channel.
    """
    key = spec if alpha is None else f"{spec}@{alpha:.3f}"
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    r, g, b, a = _parse(spec)
    if alpha is not None:
        a = max(0, min(255, round(alpha * 255)))
    packed = skia.ColorSetARGB(a, r, g, b)
    _CACHE[key] = packed
    return packed


def color4f(spec: str, alpha: float | None = None) -> "skia.Color4f":
    """Float form, for shader uniforms and gradients."""
    return skia.Color4f(color(spec, alpha))


def lerp(a: str, b: str, t: float) -> str:
    """Blend two #RRGGBB colors. Used by pulses, shimmers and gradients."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab, _ = _parse(a)
    br, bg, bb, _ = _parse(b)
    return (f"#{round(ar + (br - ar) * t):02x}"
            f"{round(ag + (bg - ag) * t):02x}"
            f"{round(ab + (bb - ab) * t):02x}")


def _parse(spec: str) -> tuple[int, int, int, int]:
    s = spec.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        s += "ff"
    if len(s) != 8:
        raise ValueError(f"bad color: {spec!r}")
    return (int(s[0:2], 16), int(s[2:4], 16),
            int(s[4:6], 16), int(s[6:8], 16))
