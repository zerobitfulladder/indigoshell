"""User-facing config — applies the INDIGO Cyberpunk theme to all widgets.

Theme tokens live in `theme.py` (palette, colors, spacing, StatMeter
presets). To change colors/sizes globally, edit there. To customize one
widget, override its `style`, `hover_style`, `active_style`, or
`child_styles` here.
"""

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk

from . import theme
from .helpers.flows import display as flow_display
from .api import open_window, toast, toggle
from .helpers import layout, power, profile
from .services import proc, sysinfo
from .services.text_effects import Scramble
from .helpers import command_panel
from .widgets import (
    BatteryMeter,
    Box,
    Calendar,
    Clock,
    HardwarePanel,
    Media,
    Menu,
    MenuItem,
    Network,
    NetworkPanel,
    Spacer,
    StatMeter,
    StdoutText,
    Style,
    SystagBlock,
    Systray,
    SystrayPanel,
    Terminal,
    TermToast,
    Volume,
    Workspaces,
)
from .windows.color_picker import ColorPickerKind
from .windows.notification import NotificationKind
from .windows.popup import PopupKind

# ── Reusable interaction styles ───────────────────────────────────────
hover  = Style(bg=theme.BG_SHADOW)
active = Style(bg=theme.MAGENTA_DIM, fg=theme.MAGENTA_BRIGHT)


def spawn(*argv):
    return lambda _w: proc.fire(argv, detach=True)


def _menu_popup(name: str, items: list[MenuItem]) -> PopupKind:
    """Standard chord menu: bottom-left, above the bar, grabbed seat.
    Opts out of the default panel chrome — each row paints its own
    beveled frame, so a window-level bg + border would just sit behind
    them and look heavy."""
    return PopupKind(
        name=name,
        content=Menu(popup_name=name, items=items),
        corner="bottom-right",
        corner_margin=(theme.BAR_MARGIN + 8, theme.BAR_HEIGHT + theme.BAR_MARGIN + 6),
        bg=None,
        border=None,
        bevel=0,
        bevel_corners=(),
        blur=False,
        grab=True,
    )


# ── Identity ──────────────────────────────────────────────────────────
widget_systag = SystagBlock(
    on_left_click=open_window("fastfetch"),
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
    idle_player="spotify_player",
    style=Style(fg=theme.MUSIC_FG, italic=True),
    hover_style=hover,
    active_style=active,
    vfill=True,
    on_left_click=open_window("sptlrx"),
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
widget_cpu_stat    = StatMeter(label="CPU", source=sysinfo.cpu_percent,
                               on_left_click=open_window("hardware"), **theme.STAT_CPU)
widget_memory_stat = StatMeter(label="RAM", source=sysinfo.memory_percent,
                               on_left_click=open_window("hardware"), **theme.STAT_RAM)
widget_temp_stat   = StatMeter(label="TMP", source=sysinfo.temperature_package,
                               **theme.STAT_TEMP)

# ── Hardware ──────────────────────────────────────────────────────────
widget_network = Network(
    style=Style(fg=theme.HARDWARE_FG),
    on_left_click=open_window("network"),
    on_middle_click=spawn("nm-connection-editor"),
    on_right_click=toggle("nmtui"),
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
    on_left_click=open_window("calendar"),
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

widget_systray = Systray(width=7)

# Tight cluster so the systray indicator sits right beside the clock,
# not at the full SPACING_MD gap used between other sections.
clock_cluster = Box([widget_clock, widget_systray], spacing=4)

right = Box(
    [sensors_section, widget_network, widget_media, widget_volume, clock_cluster],
    spacing=theme.SPACING_MD,
)

# ── Popup windows (registered with the daemon by name) ────────────────
WINDOWS = {
    "fastfetch": PopupKind(
        name="fastfetch",
        content=Terminal(["fastfetch"], cols=110, rows=28, transparent=True),
        padding=14,
        close_on_outside_click=True,
    ),
    "sptlrx": PopupKind(
        name="sptlrx",
        content=Terminal(["sptlrx"], cols=50, rows=30, transparent=True, respawn=True),
        persistent=True,
        padding=14,
        close_on_outside_click=True,
    ),
    "spotify-player": PopupKind(
        name="spotify-player",
        content=Terminal(["spotify_player"], cols=120, rows=40, transparent=True, respawn=True),
        persistent=True,
        padding=14,
    ),
    "nmtui": PopupKind(
        name="nmtui",
        content=Terminal(
            ["nmtui"],
            cols=80, rows=24,
            transparent=True,
            env={"NEWT_COLORS": theme.NEWT_COLORS},
        ),
        # padding=0 so the newt dialog fills the popup edge-to-edge —
        # no transparent gutter visible around the black-bg nmtui frame.
        padding=0,
    ),
    "network": PopupKind(
        name="network",
        content=NetworkPanel(),
        align="left",
        padding=12,
        close_on_outside_click=True,
    ),
    "hardware": PopupKind(
        name="hardware",
        content=HardwarePanel(),
        align="left",
        padding=12,
        # History accumulates in services.sysinfo, so the panel itself
        # can be torn down on close — no work happens while hidden.
        persistent=False,
        close_on_outside_click=True,
    ),
    "calendar": PopupKind(
        name="calendar",
        content=Calendar(),
        close_on_outside_click=True,
    ),
    "systray-panel": PopupKind(
        name="systray-panel",
        content=SystrayPanel(),
        persistent=True,
        padding=10,
    ),
    "power-menu": _menu_popup("power-menu", [
        MenuItem("1", "SUSPEND",  power.suspend),
        MenuItem("2", "POWEROFF", power.poweroff),
        MenuItem("3", "REBOOT",   power.reboot),
        MenuItem("4", "LOGOUT",   power.logout),
    ]),
    # `display-menu` is registered below as a dialog pipeline (not a
    # static popup kind) — see SCRIPTS + PIPELINES.
    "envy-menu": _menu_popup("envy-menu", [
        # pkexec triggers polkit's GUI auth dialog so the toast popup
        # doesn't need keyboard focus to type a sudo password — works
        # cleanly with the NOTIFICATION-typed (non-focusable) toast.
        MenuItem("1", "NVIDIA",     toast(["pkexec", "envycontrol", "-s", "nvidia"])),
        MenuItem("2", "INTEGRATED", toast(["pkexec", "envycontrol", "-s", "integrated"])),
        MenuItem("3", "HYBRID",     toast(["pkexec", "envycontrol", "-s", "hybrid"])),
    ]),
    "layout-menu": _menu_popup("layout-menu", [
        MenuItem("1", "US", layout.us),
        MenuItem("2", "TR", layout.tr),
    ]),
    "profile-menu": _menu_popup("profile-menu", [
        MenuItem("1", "PERFORMANCE", profile.performance),
        MenuItem("2", "BALANCED",    profile.balanced),
        MenuItem("3", "POWERSAVE",   profile.powersave),
    ]),
    "terminal": PopupKind(
        name="terminal",
        # No command → interactive $SHELL, no linger, persists until the
        # shell exits (Ctrl-D / `exit`) or the popup is closed.
        content=TermToast(cols=100, rows=28, popup_name="terminal"),
        corner="top-right",
        corner_margin=(theme.NOTIF_OFFSET_X, theme.NOTIF_OFFSET_X),
        bevel=theme.NOTIF_BEVEL,
        bevel_corners=theme.NOTIF_BEVEL_CORNERS,
        border=theme.NOTIF_FRAME_NORMAL,
        border_thick=theme.NOTIF_BORDER_THICK,
        padding=theme.NOTIF_PADDING_Y,
        # UTILITY is focusable (so VTE accepts input + click) and on
        # most WMs stays above when keep_above is set. Tiling WMs may
        # still demote it on focus change — the companion fix is a
        # qtile floating_layout Match on wm_class="indigoshell-popup".
        type_hint=Gdk.WindowTypeHint.UTILITY,
    ),
    "notifications": NotificationKind(),
    "color-picker": ColorPickerKind(),
}


WINDOWS[command_panel.POPUP_NAME] = command_panel.WINDOW


# ── Dialog flows ──────────────────────────────────────────────────────
# Each flow under `helpers/flows/` declares its own `SCRIPTS` (name →
# script path) and `PIPELINES` (entry-point name → initial command). We
# merge all flows into the daemon's two top-level registries here. To
# add a new flow: drop a folder under `helpers/flows/` with an
# __init__.py that exports `SCRIPTS` and `PIPELINES`, then import it
# alongside `flow_display` above and merge it in.
_FLOWS = (flow_display,)
SCRIPTS: dict[str, list[str] | str] = {
    k: v for flow in _FLOWS for k, v in flow.SCRIPTS.items()
}
PIPELINES: dict[str, list[str]] = {
    k: v for flow in _FLOWS for k, v in flow.PIPELINES.items()
}


# ── Bar layout ────────────────────────────────────────────────────────
BAR = {
    "widgets": [left, Spacer(), right],
    "windows": WINDOWS,
    "scripts": SCRIPTS,
    "pipelines": PIPELINES,
}
