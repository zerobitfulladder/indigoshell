"""Default configuration.

A user config at `~/.config/indigoshell/config.py` replaces this by
exporting its own `WINDOWS`.
"""

from . import theme
from .api import toggle
from .effects import Decode, ScanLock, GlitchWipe
from .core import registry
from .services import music, sysinfo
from .services.text_effects import Scramble
from .widgets.base import Insets
from .agent import (CALL_WINDOW, STATUS_WINDOW, Agent, CallHost,
                    CallRouter, CallStatus, Firefox, ShellTools)
from .agent.ui.status import AgentChip
from .applink.context import AppContext
from .applink.service import AppService
from .browser.context import JackWindow
from .widgets.clock import Clock
from .widgets.fastfetch import Fastfetch
from .widgets.hardware_panel import HardwarePanel
from .widgets.launcher import LAUNCHER_WINDOW, Launcher
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
LAUNCHER = LAUNCHER_WINDOW       # also where Jack's Ctrl+L/T/M open it
TOP_BAR = "topbar"

# Which MPRIS source the now-playing widgets follow. None watches every
# player, which also means a browser tab counts as "playing" and starts
# cava and aubio for it; naming one keeps the visualiser and the beat
# pulse tied to actual music.
#
# AUDIO // PLAYER (`indigoshell menu audio`) switches between the Spotify
# desktop app ("spotify") and the terminal client ("spotify_player"),
# remembers the pick, and reloads the shell to follow it; the default
# here is what is followed until a pick has been made. A fixed string
# instead — `MUSIC_PLAYER = "spotify_player"` — ignores the menu.
# sptlrx needs no switch: `player: spotify` in its config reads the
# account through Spotify's web API, whichever client is playing.
MUSIC_PLAYER = music.chosen_player(default="spotify")

# Chord menus, by name. Plugins load from `indigoshell/plugins/` and
# `~/.config/indigoshell/plugins/`; a user file with the same module
# name replaces the shipped one, and a menu of the same name replaces
# the shipped menu. `indigoshell menu <name>` opens one — bind that to a
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


# ── The call ───────────────────────────────────────────────────────────
# Claude on the line, through the Agent SDK — `indigoshell toggle call`
# from a keybinding. It signs in from its own Claude Code config
# directory, so it can use a different account from a `claude` in a
# terminal; sign that directory in once with
#
#     CLAUDE_CONFIG_DIR=~/.config/indigoshell/claude claude auth login
#
# `persona` tells it the tone you enjoy (agent/prompts.py has the default:
# a love of cyberpunk and its street slang). `model="opus"` follows the newest
# Opus; `effort` defaults to Claude Code's own. `permission_mode="auto"`
# (the default) lets a classifier approve routine tool calls; the ones in
# `agent/permissions.py` (rm, sudo, kill, ...) still ask on the call.
# `tools` gives it the desktop (agent/tools.py): screenshots, opening
# links and files (reports, images, videos) brought forward on their
# workspace, apps, workspaces, the clipboard, Spotify, the WiZ lights
# on the LAN (named in ~/.config/indigoshell/connections/wiz/config.json),
# and your everyday Firefox through the INDIGO bridge add-on — set up
# once with `python -m indigoshell.agent.firefox install` (agent/firefox.py).
# The agent works in ~/.config/indigoshell/assistant, whose CLAUDE.md is
# its notebook for this machine — how it shows you things included; ask
# it on a call to work differently and it edits the notebook — and whose
# .claude/commands/ holds any slash commands of your own (`/name` on the
# call runs one).
FIREFOX = Firefox()
AGENT = Agent(name="INDIGO", model="opus",
              tools=ShellTools(firefox=FIREFOX))


# ── Services ───────────────────────────────────────────────────────────
# Long-lived non-window services the daemon starts once its loop is up.
# Each toast is its own window, so the scan-lock spawn/despawn plays per
# notification rather than once for the stack. The router watches a
# hung-up call: the top bar's agent chip shows its work and blinks when
# it rings back, and its corner plate shows while it works; FIREFOX
# is the socket the browser add-on's host connects to. APPS is the
# socket the shell's own apps connect to, one line per window, and knows
# which of their windows has focus (applink/): today Jack, the browser
# (`indigoshell browser`), whose windows the top bar shows
# (browser/context.py) and whose Ctrl+L, Ctrl+T, Ctrl+M open the
# launcher. Another app kind is one more entry in `kinds`.
APPS = AppService(kinds={"jack": JackWindow})

SERVICES = [
    NotificationManager(effects=(PANEL_SPAWN,), effect_scale=0.35, holo=True),
    FIREFOX,
    AGENT,
    CallRouter(AGENT),
    APPS,
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
    # then clean text. Nothing moves vertically. `Decode(...)` is a
    # column-scramble reveal and `effect=Scramble(...)` a character-swap
    # one; both still work, and the two kinds compose.
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
# MAGENTA_MID on each beat. Click plays/pauses, scroll changes track.
media = Media(
    player=MUSIC_PLAYER,
    max_chars=26,
    # 18px bold: 14pt is 18.67px at 96 DPI, which brings the widget out
    # at 286px wide — 11px per character at `max_chars`.
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
    [clock, Systray(width=7, panel_effects=(PANEL_SPAWN,), holo=True)],
    spacing=4,
)


# ── The top bar ────────────────────────────────────────────────────────
# Always there, like the bottom bar and in its look, a little thinner,
# its space reserved the same way (and, like it, under a fullscreen
# window: the window manager stacks a focused fullscreen window over
# docks). Its left is the focused indigoshell app's: Jack's tabs, host
# and load line while a Jack window has focus, nothing while any other
# window does. `widgets` go after it, at the right; the right end itself
# is the agent's: `AgentChip` (agent/ui/status.py), always there, shows
# the name of the voice on the task and a blinking light while the agent
# works, blinks whole when it calls back (in place of the old corner
# plate's ring), and rests dimmed in between. It finds the agent through
# the `CallRouter` in SERVICES; with none it takes no room at all.
#
# A config.py adds its own without copying the spec, by building the
# bar again with them in place of the shipped one:
#
#     from indigoshell.config_default import TOP_BAR, WINDOWS, top_bar
#     WINDOWS = [top_bar(MyWidget()) if spec.name == TOP_BAR else spec
#                for spec in WINDOWS]
def top_bar(*widgets, apps: AppService = APPS) -> WindowSpec:
    return WindowSpec(
        name=TOP_BAR,
        layer=Layer.DOCK,
        anchor=Anchor.TOP,
        size=(None, theme.TOP_BAR_HEIGHT),
        exclusive=True,
        focusable=False,
        background=theme.BAR_BG,
        padding=Insets.xy(theme.BAR_MARGIN, 0),
        autostart=True,
        content=Row(
            [AppContext(apps), *widgets, AgentChip()],
            spacing=theme.BAR_SPACING,
            align=Align.CENTER,
        ),
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

    top_bar(),

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
        holo=True,                  # the call's hologram look
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
        holo=True,                  # the call's hologram look
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
        holo=True,                  # the call's hologram look
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
        margin=Insets(right=theme.BAR_MARGIN + 8,
                      bottom=theme.BAR_HEIGHT + theme.BAR_MARGIN + 6),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        grab_keyboard=True,
        keep_alive=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        holo=True,                  # the call's hologram look
        effect_scale=0.35,
        content=MenuHost(),
    ),

    # The app launcher — `indigoshell toggle launcher` from a keybinding.
    # No anchor, so it opens centred, where rofi did. Content-sized on
    # attach like the menu, but its size never changes after: the list
    # has a fixed number of rows, so typing repaints and never resizes.
    # `terminal` wraps the apps whose desktop entry says Terminal=true.
    WindowSpec(
        name=LAUNCHER,
        layer=Layer.OVERLAY,
        size=(1, 1),                 # placeholder; content-sized on attach
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        grab_keyboard=True,
        keep_alive=True,
        background="#00000000",
        effects=(PANEL_SPAWN,),
        holo=True,                  # the call's hologram look
        effect_scale=0.35,           # raster fallback only
        content=Launcher(rows=12, width=760, terminal=("kitty",)),
    ),

    # The call — top right, clear of the bar. Frosted rather than solid:
    # translucent plates with `blur`, which has the compositor blur what
    # is behind them and cuts the window to the plates' outline. Grabs
    # the keyboard and pointer like the launcher; Escape, an outside
    # click or the keybinding again hangs up, and the work carries on.
    WindowSpec(
        name=CALL_WINDOW,
        layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.RIGHT,
        size=(1, 1),                 # placeholder; content-sized
        margin=Insets(top=16, right=theme.BAR_MARGIN + 12),
        override_redirect=True,
        focusable=False,
        dismiss_on_outside_click=True,
        grab_keyboard=True,
        close_keys=("super+a",),     # qtile's MOD+A, hidden from it by the grab
        keep_alive=True,
        background="#00000000",
        blur=True,
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,           # raster fallback only
        content=CallHost(AGENT),
    ),

    # Where the call goes when it is hung up but still working (the
    # router's `show_working`); a ring is the top bar's chip now. No
    # grab: it sits in the corner while you carry on. Click to answer.
    WindowSpec(
        name=STATUS_WINDOW,
        layer=Layer.OVERLAY,
        anchor=Anchor.TOP | Anchor.RIGHT,
        size=(1, 1),                 # placeholder; content-sized
        margin=Insets(top=16, right=theme.BAR_MARGIN + 12),
        override_redirect=True,
        focusable=False,
        keep_alive=True,
        background="#00000000",
        blur=True,
        effects=(PANEL_SPAWN,),
        effect_scale=0.35,           # raster fallback only
        content=CallStatus(AGENT),
    ),
]
