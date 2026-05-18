"""User-facing config — applies the INDIGO Cyberpunk theme to all widgets.

Theme tokens live in `theme.py` (palette, colors, spacing, StatMeter
presets). To change colors/sizes globally, edit there. To customize one
widget, override its `style`, `hover_style`, `active_style`, or
`child_styles` here.
"""

from . import theme
from .api import toggle
from .services import proc, sysinfo
from .services.text_effects import Scramble
from .widgets import (
    BatteryMeter,
    Box,
    Calendar,
    Clock,
    Media,
    Network,
    NetworkPanel,
    Spacer,
    StatMeter,
    StdoutText,
    Style,
    SystagBlock,
    Terminal,
    Volume,
    Workspaces,
)
from .windows.notification import NotificationKind
from .windows.popup import PopupKind

# ── Reusable interaction styles ───────────────────────────────────────
hover  = Style(bg=theme.BG_SHADOW)
active = Style(bg=theme.MAGENTA_DIM, fg=theme.MAGENTA_BRIGHT)


def spawn(*argv):
    return lambda _w: proc.fire(argv, detach=True)


# ── Identity ──────────────────────────────────────────────────────────
widget_systag = SystagBlock(
    on_left_click=toggle("fastfetch"),
)

widget_workspaces = Workspaces(
    label="◆",
    style=Style(font_size=theme.FONT_SIZE_XL),
    child_styles={
        "current":  Style(fg=theme.WORKSPACE_CURRENT_FG, bold=True),
        "occupied": Style(fg=theme.WORKSPACE_OCCUPIED_FG),
        "empty":    Style(fg=theme.WORKSPACE_EMPTY_FG),
    },
)

# ── Now playing ───────────────────────────────────────────────────────
widget_lyrics = StdoutText(
    ["sptlrx", "pipe"],
    placeholder="          ",
    min_width_chars=8,
    max_width_chars=100,
    scroll_interval_ms=90,
    loop_scroll=False,
    effect=Scramble(interval_ms=40, frames_per_char=1, scramble_window=8),
    pulse_colors=(theme.CYAN_MID, theme.CYAN_BRIGHT),
    pulse_period_ms=500,
    beat_sync=True,
    clear_when_idle=True,
    style=Style(fg=theme.MUSIC_FG, italic=True),
    hover_style=hover,
    active_style=active,
    on_left_click=toggle("sptlrx"),
)

widget_media = Media(
    player="spotify_player",
    show_cava_bg=True,
    beat_pulse=True,
    max_chars=26,
    style=Style(fg=theme.MUSIC_FG, bold=True),
    on_left_click=toggle("spotify-player"),
)

# ── System readouts ───────────────────────────────────────────────────
widget_cpu_stat    = StatMeter(label="CPU", source=sysinfo.cpu_percent,         **theme.STAT_CPU)
widget_memory_stat = StatMeter(label="RAM", source=sysinfo.memory_percent,      **theme.STAT_RAM)
widget_temp_stat   = StatMeter(label="TMP", source=sysinfo.temperature_package, **theme.STAT_TEMP)

# ── Hardware ──────────────────────────────────────────────────────────
widget_network = Network(
    style=Style(fg=theme.HARDWARE_FG),
    on_left_click=toggle("network"),
    on_middle_click=spawn("nm-connection-editor"),
)
widget_volume = Volume(
    on_middle_click=spawn("pavucontrol"),
)

# ── Clock ─────────────────────────────────────────────────────────────
widget_clock_battery = BatteryMeter(
    # 43 cells × 2px + 42 × 2px gap = 170 — matches Clock.width so the
    # bar fills edge to edge underneath the date/time row.
    cells=43,
    cell_thick=2,
    gap=2,
    corner_arm=0,
    pad_x=0,
    pad_y=0,
    height=4,
)
widget_clock = Clock(
    on_left_click=toggle("calendar"),
    extra_widget=widget_clock_battery,
)

# ── Sections ──────────────────────────────────────────────────────────
left = Box(
    [widget_systag, widget_workspaces, widget_lyrics],
    spacing=theme.SPACING_SM,
)

sensors_section = Box(
    [widget_cpu_stat, widget_memory_stat, widget_temp_stat],
    spacing=theme.SPACING_LG,
)

right = Box(
    [sensors_section, widget_network, widget_media, widget_volume, widget_clock],
    spacing=theme.SPACING_MD,
)

# ── Popup windows (registered with the daemon by name) ────────────────
WINDOWS = {
    "fastfetch": PopupKind(
        name="fastfetch",
        content=Terminal(["fastfetch"], cols=110, rows=28, transparent=True),
        transparent=True, blur=True, bg=theme.POPUP_BG,
    ),
    "sptlrx": PopupKind(
        name="sptlrx",
        content=Terminal(["sptlrx"], cols=50, rows=30, transparent=True, respawn=True),
        persistent=True, transparent=True, blur=True, bg=theme.POPUP_BG,
    ),
    "spotify-player": PopupKind(
        name="spotify-player",
        content=Terminal(["spotify_player"], cols=120, rows=40, transparent=True, respawn=True),
        persistent=True, transparent=True, blur=True, bg=theme.POPUP_BG,
    ),
    "network": PopupKind(
        name="network",
        content=NetworkPanel(),
        transparent=True, blur=True, bg=theme.POPUP_BG, padding=12,
    ),
    "calendar": PopupKind(
        name="calendar",
        content=Calendar(),
    ),
    "notifications": NotificationKind(),
}

# ── Bar layout ────────────────────────────────────────────────────────
BAR = {
    "widgets": [left, Spacer(), right],
    "windows": WINDOWS,
}
