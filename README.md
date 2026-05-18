# indigoshell

A widget-engine desktop shell. A configurable bottom bar, popup windows, and
a built-in `org.freedesktop.Notifications` daemon — all driven by one Python
process on top of GTK 3 + PyGObject.

Styled out of the box with the INDIGO Cyberpunk palette: hot magenta, electric
cyan, neon yellow, violet accent, deep blue-violet base.

## Features

- **Bar** — a transparent dock-strut at the screen edge with declarative widget
  composition (`Box([…])`, `Spacer()`).
- **Widgets** — workspaces, system stat meters (CPU / RAM / temp), volume,
  network, media (with cava equalizer background + beat-pulse), clock with
  battery underline, scrambling/beat-syncing lyrics, identity tag with
  pulsing corner brackets.
- **Popups** — terminal-hosted popups (`fastfetch`, `sptlrx`, `spotify-player`),
  a network panel, calendar.
- **Notifications** — full D-Bus notification daemon. Replaces dunst. Renders
  toasts in the bar's visual language (corner brackets, segmented progress
  meters when `value` hints arrive), supports actions, images, urgency
  styling, replace-by-id.
- **Theme** — every color, spacing, font size, and per-widget preset lives in
  one [`theme.py`](indigoshell/theme.py).
- **Hot reload** — `--watch` re-execs the process on `.py` change; or call
  `indigoshell reload` over IPC.

## Architecture

```text
indigoshell/
  app.py                  ─ entry point (daemon vs client mode)
  api.py                  ─ public helpers used in user config
  theme.py                ─ palette, semantic tokens, per-widget presets
  style.py                ─ Style dataclass, CSS builder, hex helpers
  config_default.py       ─ default bar+windows config (user can override)
  core/
    daemon.py             ─ GTK main loop, window registry, store, IPC
    ipc.py                ─ unix-socket command server
    registry.py           ─ merges built-in + user WindowKinds
    client.py             ─ CLI side of IPC (open/close/toggle/reload)
    paths.py, store.py, singleton.py
  services/
    proc.py               ─ unified subprocess (run / fire / popen / subscribe)
    sysinfo.py            ─ cached cpu / memory / temperature
    beat.py               ─ aubio beat detector + cava bands broker
    music.py              ─ playerctl status broker
    notifications.py      ─ org.freedesktop.Notifications D-Bus server
    text_effects.py       ─ scramble + other arrival animations
  widgets/                ─ bar widgets (each ≤ ~300 LOC)
    base.py               ─ Widget + paint() Cairo helper
    layout.py             ─ Box, Spacer
    workspaces.py, systag.py, clock.py, volume.py, network.py, media.py,
    stat_meter.py, battery_meter.py, stdout_text.py, network_panel.py,
    calendar.py, terminal.py, notification.py
  windows/                ─ top-level window kinds
    base.py               ─ WindowKind abstract class
    bar.py                ─ the bar itself
    popup.py              ─ click-anchored transient popups
    notification.py       ─ floating notification stack (bottom-left)
```

### Key conventions

- All Cairo color painting goes through `paint(cr, hex_color, alpha=None)`
  ([widgets/base.py](indigoshell/widgets/base.py)).
- All subprocess work goes through [`services/proc.py`](indigoshell/services/proc.py):
  - `proc.run(cmd)` — capture stdout, swallow missing-binary / timeout
  - `proc.fire(cmd, *, detach=False)` — fire-and-forget
  - `proc.popen(cmd, *, text=False, bufsize=-1)` — raw spawn for custom readers
  - `proc.subscribe(cmd, on_line, ...)` — line-streaming with reader thread
- Widgets center vertically by default in the bar; override `valign`/`vexpand`
  if you need a draw widget to fill the bar height (Media does this for its
  cava background).
- Widget styling: every widget accepts `style=`, `hover_style=`, `active_style=`,
  and `child_styles={}` — overrides are CSS rules scoped to the widget's
  generated id. The theme file ships sensible defaults.

## Configuration

`indigoshell` looks for a user config in:

1. `~/.config/indigoshell/config.py`
2. `./config.py` (cwd)

A user config exports `BAR = {"widgets": [...], "windows": {...}}`. See
[`indigoshell/config_default.py`](indigoshell/config_default.py) for the
shape and the available widgets.

## Running

```bash
# Install as a uv tool (editable)
uv tool install -e .

# Run the daemon
indigoshell                  # foreground
indigoshell --watch          # re-exec on .py change

# Client commands
indigoshell open <window-name>
indigoshell close <window-name>
indigoshell toggle <window-name>
indigoshell reload
```

## Notifications

`indigoshell` claims `org.freedesktop.Notifications` on the session bus on
startup. To use it, stop and mask any existing notification daemon first:

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

Toasts mirror the bar's visual language: corner brackets in the urgency color,
a CPU-style segmented progress meter when a `value` hint is present.

## Dependencies

System:

- GTK 3, PyGObject (3.56+)
- `pactl`, `pulseaudio` (volume)
- `nmcli` (network)
- `cava` (visualizer background)
- `parec` + Python `aubio` (beat detection)
- `playerctl` (media status)
- `psutil`, `python-xlib`, `watchdog` (Python deps; declared in `pyproject.toml`)

Optional:

- `sptlrx` (synced lyrics)
- `spotify_player` (terminal music player)
- `fastfetch` (system info popup)
- A Nerd Font for the glyphs (`FiraCode Nerd Font Mono` ships in the default
  theme).

## Status

Personal project, single-author. Stable for daily-driver use; the API may
still change.
