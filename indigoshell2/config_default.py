"""Default configuration.

A user config at `~/.config/indigoshell2/config.py` replaces this by
exporting its own `WINDOWS`.
"""

from . import theme
from .api import toggle
from .effects import Decode, ScanLock, GlitchWipe
from .core import registry
from .services import sysinfo
from .services.text_effects import Scramble
from .widgets.base import Insets
from .widgets.clock import Clock
from .widgets.fastfetch import Fastfetch
from .widgets.hardware_panel import HardwarePanel
from .widgets.layout import Align, Box, Brackets, Row, Spacer
from .widgets.media import Media
from .widgets.menu import MENU_WINDOW, MenuHost
from .widgets.meters import BatteryMeter, StatMeter
from .widgets.network import Network
from .widgets.network_panel import NetworkPanel
from .widgets.notification import NotificationManager
from .widgets.stdout_text import StdoutText
from .widgets.systag import Systag
from .widgets.systray import Systray
from .widgets.volume import Volume
from .widgets.workspaces import Workspaces
from .window import Anchor, Layer, WindowSpec

SYSTEM_PANEL = "system"
HARDWARE_PANEL = "hardware"
NETWORK_PANEL = "network"

# Which MPRIS source the now-playing widgets follow. None watches every
# player, which also means a browser tab counts as "playing" and starts
# cava and aubio for it; naming one keeps the visualiser and the beat
# pulse tied to actual music.
MUSIC_PLAYER = "spotify_player"

# Chord menus, by name. Plugins load from `indigoshell2/plugins/` and
# `~/.config/indigoshell2/plugins/`; a user file with the same module
# name replaces the shipped one, and a menu of the same name replaces
# the shipped menu. `main2.py menu <name>` opens one — bind that to a
# key in the window manager.
_plugins = registry.discover()
MENUS = registry.by_name(_plugins.menus)
# Async hooks the daemon awaits before creating any window — a plugin
# restoring the remembered display runs here, so the bar lands on it.
STARTUP = _plugins.startup
# Async hooks the daemon awaits after the screen or an output's
# connection changes, before re-placing its windows.
SCREEN = _plugins.screen

# ── Spawn animation ────────────────────────────────────────────────────
# Every panel opens the same way: braindance's scan-lock reveal, where
# bands snap in out of order with a sideways tear and an RGB split and
# resolve row by row. One shared instance — an Effect keeps no per-window
# state (`shader()` builds a fresh RuntimeEffectBuilder each call and the
# window supplies t/appear/seed), so sharing it is safe and compiles the
# SkSL once instead of once per panel.
#
# `tear` is the parameter that costs: it sets `bleed`, which grows the X
# window on every side. At 0.10 it is smaller than the ColorSplit it
# replaced, so the panels' windows shrank. On the GPU the pass itself is
# ~0.08ms, so nothing else here is a performance trade.
#
# Braindance's always-on half — `Aberration`, a chromatic base with an
# occasional burst and a breathing edge glow — is still off. It was
# unaffordable on CPU raster (~20ms/frame, continuous, for as long as a
# panel stayed open); on the GPU it is not, so it is now a taste call
# rather than a cost one. Add it after PANEL_SPAWN to try it.
#
# Sized in pixels rather than as fractions of the window, so every panel
# plays the same animation whatever its size. Tall bands: 36px, around
# fifteen per panel, so each one reads as a slab that snaps into place
# rather than a flicker of scanlines. Tear and split are the toast's
# old numbers, the lock flash is at 35% of braindance's, static is off.
PANEL_SPAWN = ScanLock(duration=0.22, band_px=36.0, tear_px=29.0, split_px=17.0,
                       spark=(0.035, 0.33, 0.35))


# ── Services ───────────────────────────────────────────────────────────
# Long-lived non-window services the daemon starts once its loop is up.
# Each toast is its own window, so the scan-lock spawn/despawn plays per
# notification rather than once for the stack.
SERVICES = [
    NotificationManager(effects=(PANEL_SPAWN,), effect_scale=0.35),
]


# ── Sensors ────────────────────────────────────────────────────────────
stat_cpu = StatMeter("CPU", sysinfo.cpu_percent,
                     bright_color=theme.CYAN_BRIGHT, dim_color=theme.CYAN_DIM,
                     label_color=theme.CYAN_DIM, value_color=theme.CYAN_BRIGHT)
stat_ram = StatMeter("RAM", sysinfo.memory_percent,
                     bright_color=theme.VIOLET_BRIGHT, dim_color=theme.VIOLET_DIM,
                     label_color=theme.VIOLET, value_color=theme.VIOLET_BRIGHT)
stat_temp = StatMeter("TEMP", sysinfo.temperature_package,
                      value_format="{:.0f}°",
                      dim_color=theme.CYAN_DIM, label_color=theme.CYAN_DIM,
                      # 30°C -> 0%, 90°C -> 100%
                      to_pct=lambda t: (t - 30) * (100 / 60),
                      gradient=((0.0, theme.CYAN_BRIGHT),
                                (0.5, theme.YELLOW_BRIGHT),
                                (0.8, theme.ERROR)))

# Now playing — sptlrx pipes the current lyric line; the scramble effect
# reveals each new line instead of snapping to it, and the line breathes
# between the two pulse colours on every beat aubio reports.
#
# `clear_when_idle` is what keeps the helpers honest: it subscribes to
# the music status, and the beat listener is added and dropped with it,
# so parec and aubio only exist between a Playing and the next Paused.
lyrics = StdoutText(
    ["sptlrx", "pipe"],
    placeholder="          ",
    min_width_chars=8,
    max_width_chars=100,
    scroll_interval_ms=90,
    loop_scroll=False,
    # A GPU pass over the widget's own pixels, replayed per line: a
    # cursor bar sweeps the line left to right and the text decodes
    # behind it — solid cells first, then torn, colour-fringed glyphs,
    # then clean text. Nothing moves vertically. `Decode(...)` is the
    # earlier column-scramble reveal and still works, as does the v1
    # character-swap `effect=Scramble(...)`; the two kinds compose.
    #
    # The knobs that change its character most, all in px unless noted:
    #   duration   whole reveal; the sweep takes `scan` (0.55) of it
    #   cell       block size in the decode phase; blocky = share of a
    #              pixel's settle time spent as blocks
    #   shift/seg_px/density   how far, how long and how many of the
    #              glitch chunks tear sideways
    #   cursor/spark   bar colour and hot-pixel/cell tint (rgb 0..1)
    reveal_effect=GlitchWipe(duration=0.8),
    # Rest colour then flash colour. This pair *is* the pulse
    # amplitude — widen it to hit harder, narrow it to calm it down.
    pulse_colors=(theme.CYAN_MID, theme.CYAN_BLOOM),
    beat_sync=True,
    clear_when_idle=True,
    idle_player=MUSIC_PLAYER,
    italic=True,
    size=theme.FONT_SIZE - 1,
)

# The track title, marquee'd over a cava visualiser that tints towards
# MAGENTA_MID on each beat. Click plays/pauses, scroll changes track —
# v1 also opened a terminal player, which went with TUI support.
media = Media(
    player=MUSIC_PLAYER,
    max_chars=26,
    # 18px bold puts the title back at v1's scale: v1 asked Pango for
    # 14pt, which is 18.67px at 96 DPI, and the widget comes out 286px
    # wide — exactly v1's `max_chars * 11`.
    size=theme.FONT_SIZE_LG,
    # Positive nudges the title down. The default centres the font's ink
    # band rather than its em box; this is the taste correction on top.
    baseline_shift=1.0,
    # No plate. A HUD chip and then an open-bottomed tab were both
    # tried here and both taken out: any enclosure made this the one
    # boxed thing on a bar of open widgets. `plate=True` brings it back.
    show_cava_bg=True,
    beat_pulse=True,
)

clock = Clock(extra=BatteryMeter(cell_thick=2, gap=2, height=4,
                                 pad_y=0, brackets=False, fill=True))

# Tight cluster so the systray indicator sits right beside the clock,
# not at the full BAR_SPACING gap used between other sections. The
# panel and menu windows aren't in WINDOWS below — their size depends
# on what's registered, so the widget builds their specs at open time.
clock_cluster = Row(
    [clock, Systray(width=7, panel_effects=(PANEL_SPAWN,))],
    spacing=4,
)

WINDOWS = [
    WindowSpec(
        name="bar",
        layer=Layer.DOCK,
        anchor=Anchor.BOTTOM,
        size=(None, theme.BAR_HEIGHT),
        exclusive=True,
        focusable=False,
        background=theme.BAR_BG,
        padding=Insets.xy(theme.BAR_MARGIN, 0),
        autostart=True,
        content=Row(
            [
                Systag("INDIGO", on_left_click=toggle(SYSTEM_PANEL)),
                Workspaces(),
                lyrics,
                Spacer(),
                # The three meters read as one instrument: corner brackets
                # around the cluster, not around each.
                Brackets(Row([stat_cpu, stat_ram, stat_temp],
                             spacing=theme.SPACING_XL),
                         on_left_click=toggle(HARDWARE_PANEL)),
                Network(on_left_click=toggle(NETWORK_PANEL)),
                media,
                Volume(cap=True, cap_color=theme.YELLOW_BRIGHT),
                clock_cluster,
            ],
            spacing=theme.BAR_SPACING,
            align=Align.CENTER,
        ),
    ),

    WindowSpec(
        name=SYSTEM_PANEL,
        layer=Layer.OVERLAY,
        anchor=Anchor.BOTTOM | Anchor.LEFT,
        # Sized to this machine's fastfetch output. The panel has no
        # auto-size, so a host reporting more GPUs or interfaces would
        # need this raised.
        size=(600, 580),
        margin=Insets(left=theme.BAR_MARGIN,
                      bottom=theme.BAR_HEIGHT + theme.POPUP_GAP),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        keep_alive=True,
        # Nothing in here binds a key, but the grab is what makes Escape
        # close the panel.
        grab_keyboard=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,          # raster fallback only
        content=Box(
            Fastfetch(width=540),
            padding=Insets.all(20),
            background=theme.POPUP_BG,
            border=theme.POPUP_BORDER,
            border_width=2.0,
            bevel=theme.POPUP_BEVEL,
            bevel_corners=theme.POPUP_BEVEL_CORNERS,
        ),
    ),

    WindowSpec(
        name=HARDWARE_PANEL,
        layer=Layer.OVERLAY,
        anchor=Anchor.BOTTOM | Anchor.RIGHT,
        size=(600, 520),
        margin=Insets(right=theme.BAR_MARGIN,
                      bottom=theme.BAR_HEIGHT + theme.POPUP_GAP),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        keep_alive=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,          # raster fallback only
        content=Box(
            HardwarePanel(width=560),
            padding=Insets.all(20),
            background=theme.POPUP_BG,
            border=theme.POPUP_BORDER,
            border_width=2.0,
            bevel=theme.POPUP_BEVEL,
            bevel_corners=theme.POPUP_BEVEL_CORNERS,
        ),
    ),

    WindowSpec(
        name=NETWORK_PANEL,
        layer=Layer.OVERLAY,
        anchor=Anchor.BOTTOM | Anchor.RIGHT,
        # Sized for two or three UP interfaces plus the speedtest card.
        # Like the other panels this has no auto-size, so a host with
        # more interfaces than that would need it raised.
        size=(600, 470),
        margin=Insets(right=theme.BAR_MARGIN,
                      bottom=theme.BAR_HEIGHT + theme.POPUP_GAP),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        keep_alive=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,          # raster fallback only
        content=Box(
            NetworkPanel(width=560),
            padding=Insets.all(20),
            background=theme.POPUP_BG,
            border=theme.POPUP_BORDER,
            border_width=2.0,
            bevel=theme.POPUP_BEVEL,
            bevel_corners=theme.POPUP_BEVEL_CORNERS,
        ),
    ),

    # The one window every chord menu uses: bottom right above the bar,
    # transparent around the chips, sized to whatever stage is showing
    # (MenuHost resizes it), kept alive so a keybinding opens it in the
    # time a close takes. Digits pick, Escape backs out.
    WindowSpec(
        name=MENU_WINDOW,
        layer=Layer.OVERLAY,
        anchor=Anchor.BOTTOM | Anchor.RIGHT,
        size=(1, 1),                 # placeholder; content-sized on attach
        # v1's chord-menu corner: `corner_margin=(BAR_MARGIN + 8,
        # BAR_HEIGHT + BAR_MARGIN + 6)`.
        margin=Insets(right=theme.BAR_MARGIN + 8,
                      bottom=theme.BAR_HEIGHT + theme.BAR_MARGIN + 6),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        grab_keyboard=True,
        keep_alive=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,
        content=MenuHost(),
    ),
]
