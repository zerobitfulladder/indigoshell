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
BAR_BG          = BASE_BLACK            # solid — matches kitty bg w/o a wallpaper

# ── Popup window ────────────────────────────────────────────────────────
POPUP_BG        = "#17062022"           # tinted violet-black, ~28% alpha
POPUP_BORDER    = HIGHLIGHT              # cyan-bright beveled stroke
POPUP_BEVEL     = 16
POPUP_BEVEL_CORNERS = ("top-right", "bottom-left")

# ── Per-domain widget tokens ────────────────────────────────────────────
SYSTAG_FG               = VIOLET_BRIGHT

CLOCK_FG                = YELLOW_BRIGHT
CLOCK_BRACKET_FG        = CYAN_BRIGHT

SEPARATOR_FG            = MAGENTA_DIM

WORKSPACE_CURRENT_FG    = MAGENTA_BRIGHT
WORKSPACE_OCCUPIED_FG   = YELLOW_DIM
WORKSPACE_EMPTY_FG      = MAGENTA_DIM
WORKSPACE_URGENT_FG     = ERROR  # window set _NET_WM_STATE_DEMANDS_ATTENTION
# Ring pattern: list of (ms, visible) frames. Two short on-pulses with a
# small gap between, then a longer pause before repeating — like a phone
# ring. Set to None or [] to disable blinking (steady red instead).
WORKSPACE_URGENT_RING   = [
    (150, True),
    (100, False),
    (150, True),
    (700, False),
]
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
NOTIF_BORDER_THICK      = 2.0   # frame stroke width
NOTIF_BEVEL             = 12    # 45° corner cut depth
NOTIF_BEVEL_CORNERS     = ("top-right", "bottom-left")
# Progress meter — only painted when a `value` hint is set.
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
# Progress trace drawn over the urgency border. When it completes the
# full perimeter (clockwise from top-left), the toast expires.
NOTIF_TIMER_BORDER_FG   = BASE_MUTED
NOTIF_TIMER_TICK_MS     = 33    # ~30fps redraw cadence for the trace

# ── Terminal (VTE) ──────────────────────────────────────────────────────
# Applied uniformly to every embedded Terminal popup (spotify_player,
# sptlrx, fastfetch, nmtui, …) so they all match the bar's palette
# without each TUI app needing its own theme config.

TERMINAL_FG       = "#a0a8c8"
TERMINAL_BG       = BASE_BLACK
TERMINAL_CURSOR   = MAGENTA_BRIGHT
TERMINAL_FONT     = f"{FONT.split(',')[0]} 11"

# Standard ANSI 16-color palette, mapped to INDIGO tokens.
# 0..7 are the dim/normal slots, 8..15 are the bright slots.
TERMINAL_PALETTE: list[str] = [
    BASE_SHADOW,     # 0  black
    MAGENTA_MID,     # 1  red    (pinks read as "red" in a neon palette)
    LIME_MID,        # 2  green
    YELLOW_MID,      # 3  yellow
    VIOLET,          # 4  blue
    MAGENTA_BRIGHT,  # 5  magenta
    CYAN_MID,        # 6  cyan
    "#a0a8c8",       # 7  white  (soft lavender — matches TERMINAL_FG)
    BASE_SURFACE,    # 8  bright black (dim grey)
    ERROR,           # 9  bright red — true red for error contexts
    LIME_BRIGHT,     # 10 bright green
    YELLOW_BRIGHT,   # 11 bright yellow
    VIOLET_BRIGHT,   # 12 bright blue
    MAGENTA_BLOOM,   # 13 bright magenta
    CYAN_BRIGHT,     # 14 bright cyan
    "#c8d0e8",       # 15 bright white
]

# ── newt (nmtui, whiptail) ──────────────────────────────────────────────
# libnewt only supports the 16 ANSI color names, not truecolor — but
# Terminal popups already map slots 0..15 to the INDIGO palette via
# TERMINAL_PALETTE, so naming a color here picks the matching INDIGO
# hex automatically. Slot reference:
#   https://pagure.io/newt/raw/...newt.c — `colorsets` array
# Format is `slot=fg,bg`; pass to a child process as `NEWT_COLORS`.
#   • bg stays `black` on most slots so the popup's blurred chrome shows
#     through unchanged — picking any other bg would tile a solid block.
#   • fg cycles INDIGO accents: brightcyan = data, yellow = labels +
#     title, brightmagenta = focus highlights, white = body text, gray
#     = disabled. Selected/focused states swap to a magenta or yellow
#     background so the active row pops without filling the dialog.
NEWT_COLORS = "\n".join((
    "root=brightcyan,black",
    "window=brightcyan,black",
    "border=brightmagenta,black",
    "shadow=black,black",
    "title=yellow,black",
    "button=brightcyan,black",
    "actbutton=black,brightmagenta",
    "compactbutton=yellow,black",
    "checkbox=brightcyan,black",
    "actcheckbox=black,yellow",
    "entry=brightcyan,black",
    "disentry=gray,black",
    "label=yellow,black",
    "listbox=white,black",
    "actlistbox=brightcyan,black",
    "sellistbox=black,brightcyan",
    "actsellistbox=black,brightmagenta",
    "textbox=white,black",
    "acttextbox=white,black",
    "helpline=gray,black",
    "roottext=brightcyan,black",
    "emptyscale=brightmagenta,black",
    "fullscale=black,brightmagenta",
))


# ── Toast popup (one-shot command runners) ─────────────────────────────
# Rendered via PopupKind anchored top-right with the notification chrome
# (NOTIF_BG / NOTIF_BEVEL / NOTIF_FRAME_NORMAL / NOTIF_PADDING_Y) so a
# toast reads as a single notification with the command's VTE output as
# its body. On child exit the popup's border is overdrawn clockwise from
# top-left until full perimeter → auto-close.
TOAST_LINGER_MS         = 5000   # post-exit countdown animation duration
TOAST_TICK_MS           = 33     # ~30fps redraw cadence


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
