# indigoshell

A widget-engine desktop shell. A configurable bottom bar, popup windows,
toast command-runners with dialog-tree pipelines, an interactive floating
terminal, a StatusNotifierItem system tray, and a built-in
`org.freedesktop.Notifications` daemon — all driven by one Python
process on top of GTK 3 + PyGObject.

Styled out of the box with the INDIGO Cyberpunk palette: hot magenta,
electric cyan, neon yellow, violet accent, deep blue-violet base.

## Features

- **Bar** — a transparent dock-strut at the screen edge with declarative
  widget composition (`Box([…])`, `Spacer()`).
- **Widgets** — workspaces (urgent ring blink), system stat meters
  (CPU / RAM / temp), volume, network, media (cava equalizer background +
  beat-pulse), clock with battery underline, scrambling/beat-syncing
  lyrics, identity tag with pulsing corner brackets, SNI system tray.
- **Popups** — terminal-hosted popups (`fastfetch`, `sptlrx`,
  `spotify-player`, `nmtui`), a network panel, hardware panel
  (CPU/RAM history, GPU live readouts), calendar, systray panel.
- **Interactive terminal** — `indigoshell open terminal` opens a
  top-right floating `$SHELL` popup with the bar's theme; close it
  with `exit` / Ctrl-D / mod+q. Single keybind to summon a
  full terminal that's *always-on-top* and tracks you across workspaces.
- **Chord menus** — modal, seat-grabbed popups bound to keyboard chords
  (`power-menu`, `display-menu`, `layout-menu`, `profile-menu`,
  `envy-menu`). Press-to-arm, release-to-fire; unmapped keys flash red.
- **Toasts** — `Daemon.toast(argv)` spawns a one-shot top-right TermToast
  that runs `argv`, auto-grows its VTE to fit the output, then runs a
  perimeter-trace countdown animation and auto-closes (paused on hover).
  Used from menu actions (`from .api import toast`) for visible feedback
  on system changes (`envycontrol -s …`, etc).
- **Dialog pipelines** — multi-stage cascades stacked top-right. Each
  stage is a registered script that prints output (visible in a
  TermToast) and writes a JSON manifest declaring the next-stage
  options. The orchestrator routes 0-options → leaf-with-linger,
  1-option → auto-advance, N-options → chord menu. Picked rows stay
  lit as visual history; escape pulls a cancel branch if the script
  declared one. See `helpers/flows/display/` for the included example.
- **Notifications** — full D-Bus notification daemon. Replaces dunst.
  Renders toasts in the bar's visual language (corner brackets,
  segmented progress meters when `value` hints arrive), supports
  actions, images, urgency styling, replace-by-id.
- **Theme** — every color, spacing, font size, and per-widget preset
  lives in one [`theme.py`](indigoshell/theme.py). Includes 16-color
  ANSI palettes for the embedded VTE terminals and a `NEWT_COLORS`
  block for `nmtui` so they all match the bar.
- **Hot reload** — `--watch` re-execs the process on `.py` change; or
  call `indigoshell reload` over IPC.

## Architecture

```text
indigoshell/
  app.py                  ─ entry point (daemon vs client mode)
  api.py                  ─ public helpers used in user config
                            (toggle, open_window, close_window, toast)
  theme.py                ─ palette, semantic tokens, per-widget presets,
                            terminal + newt + toast tokens
  style.py                ─ Style dataclass, CSS builder, hex helpers
  config_default.py       ─ default bar+windows+scripts+pipelines config
  core/
    daemon.py             ─ GTK main loop, window registry, store, IPC,
                            toast(), start_pipeline(), pipelines routing
    pipeline.py           ─ dialog-tree orchestrator (cascade lifecycle,
                            manifest parsing, auto-grow reflow)
    ipc.py                ─ unix-socket command server
    registry.py           ─ merges built-in + user WindowKinds
    client.py             ─ CLI side of IPC
    paths.py, store.py, singleton.py
  helpers/
    layout.py, profile.py, power.py
    flows/                ─ dialog-tree pipeline definitions
      display/            ─ pick-one-monitor pipeline
        __init__.py       ─ exports SCRIPTS + PIPELINES
        query.py          ─ list connected outputs
        set.py            ─ apply xrandr (or "cancel")
  services/
    proc.py               ─ unified subprocess (run/fire/popen/subscribe)
    sysinfo.py            ─ 1Hz CPU/RAM sampler + rolling history,
                            direct-sysfs temperature
    beat.py               ─ aubio beat detector + cava bands broker
                            (lazy: only spawns when subscribed)
    music.py              ─ per-player playerctl status broker
    notifications.py      ─ org.freedesktop.Notifications D-Bus server
    systray.py            ─ org.kde.StatusNotifierWatcher + Host
    dbusmenu.py           ─ com.canonical.dbusmenu client
    text_effects.py       ─ scramble + other arrival animations
  widgets/                ─ bar widgets
    base.py               ─ Widget + paint() / beveled_path() /
                            beveled_polyline() / stroke_partial()
    layout.py             ─ Box, Spacer
    workspaces.py, systag.py, clock.py, volume.py, network.py, media.py,
    stat_meter.py, battery_meter.py, stdout_text.py, network_panel.py,
    hardware_panel.py, calendar.py, terminal.py, notification.py,
    menu.py, systray.py, hud.py, line_graph.py, bar_meter.py,
    term_toast.py         ─ Toast / interactive shell (auto-grow VTE)
  windows/                ─ top-level window kinds
    base.py               ─ WindowKind abstract class
    bar.py                ─ the bar itself
    popup.py              ─ click-anchored transient popups + PopupKind
                            (grab, glow, blur, bevel, type_hint)
    notification.py       ─ floating notification stack (bottom-right)
```

### Key conventions

- All Cairo color painting goes through `paint(cr, hex_color, alpha=None)`
  ([widgets/base.py](indigoshell/widgets/base.py)).
- Perimeter geometry helpers `beveled_path`, `beveled_polyline`,
  `stroke_partial` are shared by notifications, toasts, and menus.
- All subprocess work goes through
  [`services/proc.py`](indigoshell/services/proc.py):
  - `proc.run(cmd)` — capture stdout, swallow missing-binary / timeout
  - `proc.fire(cmd, *, detach=False)` — fire-and-forget
  - `proc.popen(cmd, *, text=False, bufsize=-1)` — raw spawn
  - `proc.subscribe(cmd, on_line, ...)` — line-streaming with reader
- Widgets center vertically by default in the bar; override
  `valign`/`vexpand` if a draw widget should fill bar height (Media
  does this for its cava background).
- Widget styling: every widget accepts `style=`, `hover_style=`,
  `active_style=`, and `child_styles={}` — overrides are CSS rules
  scoped to the widget's generated id. The theme file ships defaults.

## Configuration

`indigoshell` looks for a user config in:

1. `~/.config/indigoshell/config.py`
2. `./config.py` (cwd)

A user config exports `BAR = {...}` with these keys:

```python
BAR = {
    "widgets":   [...],            # bar layout
    "windows":   WINDOWS,          # name → WindowKind (PopupKind etc.)
    "scripts":   SCRIPTS,          # name → script path (dialog flows)
    "pipelines": PIPELINES,        # name → initial command argv
}
```

See [`indigoshell/config_default.py`](indigoshell/config_default.py)
for the shape and the available widgets.

### Dialog flows

A *flow* is a directory under `helpers/flows/` with an `__init__.py`
that exports two dicts:

```python
# helpers/flows/myflow/__init__.py
from pathlib import Path
_HERE = Path(__file__).parent

SCRIPTS = {
    "myflow_step_a": str(_HERE / "step_a.py"),
    "myflow_step_b": str(_HERE / "step_b.py"),
}
PIPELINES = {
    "myflow-menu": ["myflow_step_a"],   # entry-point
}
```

In `config_default.py`, add the flow to `_FLOWS`. Then bind your WM to
`indigoshell open myflow-menu` and the first script runs.

#### Script protocol

Every dialog script:

1. **Prints** anything to stdout — visible in its TermToast.
2. **Writes** a JSON manifest to `$INDIGOSHELL_MANIFEST`:

   ```json
   {
     "options": [
       {"label": "First choice",  "command": ["myflow_step_b", "arg1"]},
       {"label": "Second choice", "command": ["myflow_step_b", "arg2"]}
     ],
     "cancel": {"command": ["myflow_step_b", "cancel"]}
   }
   ```

3. Exits.

The orchestrator reads the manifest on child-exit and:

- **0 options** → leaf node, linger trace fires, cascade closes
- **1 option**  → auto-advance, no menu, new toast stacked below
- **N options** → chord Menu below the cascade; user picks → next stage
- **escape on menu** → runs the `cancel.command` as the next stage if
  declared, otherwise the keypress is swallowed (flow menus never
  silently dismiss themselves)

Scripts can be any executable. `SCRIPTS` values may be a `str` path
(run under the daemon's Python) or a `list[str]` argv prefix (treated
as-is); the orchestrator appends the rest of the manifest `command`
list as argv.

## Running

```bash
# Install as a uv tool (editable)
uv tool install -e .

# Run the daemon
indigoshell                  # foreground
indigoshell --watch          # re-exec on .py change

# Client commands
indigoshell open <window-name>     # also triggers a pipeline if `name`
                                   # is a registered pipeline entry
indigoshell close <window-name>
indigoshell toggle <window-name>
indigoshell list                   # registered kinds + open instances
indigoshell ping                   # health check
indigoshell reload                 # re-exec daemon in place
indigoshell kill                   # tear down + exit the daemon
```

## Notifications

`indigoshell` claims `org.freedesktop.Notifications` on the session bus
on startup. To use it, stop and mask any existing notification daemon:

```bash
systemctl --user stop dunst.service
systemctl --user mask dunst.service
```

Test:

```bash
notify-send "Hello" "from indigoshell"
notify-send -u critical "Critical" "stays until clicked"
notify-send -h int:value:65 "Download" "stable.iso — 65%"
notify-send --action="reply=Reply" --action="archive=Archive" "Message"
```

## Toasts (programmatic command popups)

```python
from indigoshell.api import toast

# Use as a menu/click handler:
MenuItem("1", "PERF", toast(["pkexec", "envycontrol", "-s", "nvidia"]))

# Or imperatively from anywhere with a daemon handle:
get_daemon().toast(["ping", "-c", "10", "1.1.1.1"], cols=80, rows=4,
                   linger_ms=5000)
```

The TermToast starts compact and grows its VTE rows as output writes
past the visible area (capped at `max_rows`). When the child exits, a
cyan perimeter trace runs around the popup border (paused on hover);
trace completion closes the popup.

## System tray

`indigoshell` registers as `org.kde.StatusNotifierWatcher` + Host on
startup. Compatible with `nm-applet --indicator`, `blueman-applet`,
`udiskie --tray`, Discord/Spotify/Steam, etc. Pair the bar `Systray`
widget for the tray icon row; the `systray-panel` popup expands into
a per-item list with hover tooltips and right-click context menus
(via `com.canonical.dbusmenu`).

## Dependencies

System:

- GTK 3, PyGObject (3.56+)
- VTE 2.91 (embedded terminals)
- `pactl`, `pulseaudio` (volume)
- `nmcli` (network)
- `cava` (visualizer background)
- `parec` + Python `aubio` (beat detection)
- `playerctl` (media status)
- `psutil`, `python-xlib`, `watchdog` (Python deps; declared in
  `pyproject.toml`)

Optional:

- `sptlrx` (synced lyrics)
- `spotify_player` (terminal music player)
- `fastfetch` (system info popup)
- `nmtui`, `envycontrol`, `pkexec` (display / GPU flows)
- A Nerd Font for the glyphs (`FiraCode Nerd Font Mono` ships in the
  default theme).

## Status

Personal project, single-author. Stable for daily-driver use; the API
may still change.
