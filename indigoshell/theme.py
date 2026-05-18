"""INDIGO Cyberpunk theme.

Single source of truth for all bar/widget styling:

  - Palette (raw hex)            — `BASE_*`, `MAGENTA_*`, `CYAN_*`, ...
  - Semantic tokens              — `FG`, `ICON`, `HIGHLIGHT`, ...
  - Layout primitives            — `SPACING_*`, `FONT_SIZE_*`
  - Window chrome                — `BAR_*`, `POPUP_*`
  - Per-domain widget tokens     — `WORKSPACE_*`, `SYS_*`, `HARDWARE_*`, ...
  - StatMeter presets            — `STAT_CPU`, `STAT_RAM`, `STAT_TEMP`

To change colors globally, edit the palette block. To restyle one widget,
override its `style` / `hover_style` / `active_style` / `child_styles` in
config_default.py.
"""

from typing import Any

# ── Palette ─────────────────────────────────────────────────────────────
# Night City neon: hot magenta primary, electric cyan data, neon yellow
# time, violet accent, hot crimson error. Backgrounds are deep blue-violet
# black — cool enough to throw the neons forward without crushing the glow.

BASE_BLACK      = "#0a0418"
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

LIME_DIM        = "#3a4a0a"
LIME_MID        = "#99cc00"
LIME_BRIGHT     = "#ccff00"

VIOLET_DIM      = "#2a0a3a"
VIOLET          = "#7700a6"
VIOLET_BRIGHT   = "#b967ff"

ERROR           = "#ff003c"

# ── Semantic foreground roles ───────────────────────────────────────────
BG              = BASE_BLACK
BG_SHADOW       = BASE_SHADOW

FG              = MAGENTA_MID
FG_MUTED        = MAGENTA_DIM
FG_STRONG       = MAGENTA_BRIGHT
FG_ACCENT       = YELLOW_BRIGHT
ICON            = VIOLET_BRIGHT
HIGHLIGHT       = CYAN_BRIGHT

# ── Typography ──────────────────────────────────────────────────────────
FONT            = "FiraCode Nerd Font Mono, monospace"
FONT_SIZE       = 16
FONT_SIZE_LG    = 18
FONT_SIZE_ICON  = 22
FONT_SIZE_XL    = 28

# ── Spacing ─────────────────────────────────────────────────────────────
BOX_SPACING     = 0
SPACING_SM      = 10
SPACING_MD      = 12
SPACING_LG      = 14

# ── Bar window ──────────────────────────────────────────────────────────
BAR_POSITION    = "bottom"
BAR_HEIGHT      = 42
BAR_MARGIN      = 4     # horizontal gutter inside the bar window
BAR_RADIUS      = 0
BAR_TRANSPARENT = True
BAR_BG          = BASE_BLACK + "0c"     # near-transparent base black

# ── Popup window ────────────────────────────────────────────────────────
POPUP_BG        = "#17062050"           # tinted violet-black, ~31% alpha

# ── Per-domain widget tokens ────────────────────────────────────────────
SYSTAG_FG               = VIOLET_BRIGHT

CLOCK_FG                = YELLOW_BRIGHT
CLOCK_BRACKET_FG        = CYAN_BRIGHT

SEPARATOR_FG            = MAGENTA_DIM

WORKSPACE_CURRENT_FG    = MAGENTA_BRIGHT
WORKSPACE_OCCUPIED_FG   = YELLOW_DIM
WORKSPACE_EMPTY_FG      = MAGENTA_DIM
WORKSPACE_STACK_FG      = [CYAN_BRIGHT, VIOLET_BRIGHT]  # [middle, innermost]

SYS_ICON_FG             = CYAN_BRIGHT
SYS_VALUE_FG            = MAGENTA_MID

NET_ICON_FG             = VIOLET_BRIGHT

HARDWARE_FG             = CYAN_MID
HARDWARE_ICON_FG        = VIOLET_BRIGHT

MUSIC_FG                = CYAN_MID
STATUS_FG               = MAGENTA_DIM

# Notifications — mirrors the prior dunst look.
NOTIF_WIDTH             = 500
NOTIF_GAP               = 10            # vertical px between stacked toasts
NOTIF_PADDING_X         = 24
NOTIF_PADDING_Y         = 14
NOTIF_CORNER_ARM        = 10    # length of each corner-bracket arm
NOTIF_CORNER_THICK      = 2     # thickness of bracket strokes
# Progress meter — only painted when a `value` hint is set.
NOTIF_METER_SEGMENTS    = 28
NOTIF_METER_GAP         = 2
NOTIF_METER_THICK       = 3
NOTIF_METER_DIM         = BASE_GUTTER
NOTIF_OFFSET_X          = 10            # screen-edge offset
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

# ── StatMeter presets ───────────────────────────────────────────────────
# Drop into a StatMeter as `**theme.STAT_CPU` etc. Keeps per-domain color
# choices here so widgets in config_default.py stay declarative.

STAT_CPU: dict[str, Any] = {
    "bright_color": CYAN_BRIGHT,
    "dim_color":    CYAN_DIM,
    "label_color":  CYAN_DIM,
    "value_color":  CYAN_BRIGHT,
}

STAT_RAM: dict[str, Any] = {
    "bright_color": VIOLET_BRIGHT,
    "dim_color":    VIOLET_DIM,
    "label_color":  VIOLET,
    "value_color":  VIOLET_BRIGHT,
}

STAT_TEMP: dict[str, Any] = {
    "value_format": "{:.0f}°",
    "dim_color":    CYAN_DIM,
    "label_color":  CYAN_DIM,
    # Map 30°C → 0% (cool) and 90°C → 100% (hot).
    "to_pct":       lambda t: (t - 30) * (100 / 60),
    "gradient": (
        (0.0, CYAN_BRIGHT),
        (0.5, YELLOW_BRIGHT),
        (0.8, ERROR),
    ),
}
