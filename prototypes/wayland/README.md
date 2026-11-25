# One canvas on niri (wlr-layer-shell prototype)

Checks the Wayland layout for indigoshell. The shell draws everything
(both bars, dropdowns, panels) on one overlay surface above the windows.
Two invisible surfaces reserve the bars' strips, so niri tiles windows
between them.

| surface | layer | anchors | exclusive zone | buffer | input |
|---|---|---|---|---|---|
| `indigoshell-reserve-top` | bottom | top+left+right, h 42 | 42 | 1x1 transparent wl_shm + wp_viewporter | empty |
| `indigoshell-reserve-bottom` | bottom | bottom+left+right, h 42 | 42 | same | empty |
| `indigoshell` | overlay | all four, size from configure | -1 | the whole canvas | what is drawn |

Files:
- `overlay.py`: the client (wl_shm + Skia raster, or `--gpu`).
- `egl.py`: EGL on the Wayland platform + `wl_egl_window`.
- `protocols.py`: generates pywayland bindings from the system XML into
  `~/.cache/indigoshell/wayland-protocols/` at run time.
- `poke.py`: clicks and keys through the compositor's own virtual-input
  protocols, for tests in a nested niri.

## Run it in a nested niri (from an X session)

```sh
cp /usr/share/doc/niri/default-config.kdl niri-test.kdl
# edit: no spawn-at-startup, hotkey-overlay { skip-at-startup }, screenshot-path,
#       xwayland-satellite { off }
dbus-run-session -- niri -c niri-test.kdl &     # private bus: niri's screenshot
                                                # notifications stay off the host's
export WAYLAND_DISPLAY=wayland-1                # niri logs "listening on Wayland socket: …"
export NIRI_SOCKET=$(ls $XDG_RUNTIME_DIR/niri.$WAYLAND_DISPLAY.*.sock)
env -u DISPLAY kitty &
env -u DISPLAY PYTHONPATH=. python -B prototypes/wayland/overlay.py [--gpu] [--blur]
```

Type `toggle`, `blur on`, `bench 100`, `stats` or `quit` on stdin. SIGUSR1
toggles the dropdown. A click on the top bar opens it, Escape closes it.
`niri msg action screenshot-screen --path …` captures the nested output.

For a real blur of the windows under the dropdown, add this layer rule
(see "Blur" below):

```kdl
layer-rule {
    match namespace="^indigoshell$"
    background-effect { xray false; }
}
```

## What it showed (niri 26.04, nested, NVIDIA RTX 3060, 2026-11)

**The layout works.** The strips reserve 42 px at the top and bottom. kitty
tiles between them (2560x1356 output: window 1240 high = 1356 - 2x42 -
2x16 gaps). The overlay covers both strips and draws the bars there,
with a dropdown above kitty. The input region is exactly the drawn rects:
a click on kitty reaches kitty, and a click on a bar or the dropdown
reaches the overlay. Changes to the output size arrive as a new configure
and are handled live.

**Keyboard.** With `exclusive` while the dropdown is open, the overlay gets
`wl_keyboard.enter`, Escape closes it, and `none` gives the focus back to
the window. The catch is that a click on a window while the dropdown is
open never reaches the shell: the dropdown stays open and keeps the
keyboard. There are two working fixes:
- `--catch-outside`: the input region is the whole surface while the
  dropdown is open, so an outside click closes it. That click is
  swallowed, like an X11 grab.
- `--kb on_demand`: close the dropdown on `wl_keyboard.leave`. But niri
  gives an `on_demand` surface focus only when it is clicked, so the
  dropdown has no keyboard until the user clicks inside it.

Switching `exclusive` to `on_demand` once focus has arrived does not work:
niri drops the surface's focus at once.

**Fullscreen.** The overlay layer stays above fullscreen windows: the bars
cover the window's top and bottom 42 px. On layer `top`, niri draws
fullscreen windows above the canvas, which hides the bars for free, but it
also hides the dropdown, which then still holds exclusive keyboard focus.
So keep `overlay` and clear the canvas yourself:
- `zwlr_foreign_toplevel_manager_v1` reports a `fullscreen` (+ `activated`)
  state per toplevel; `--hide-on-fullscreen` uses it.
- niri's IPC has no fullscreen flag in 26.04. `windows` and
  `WindowLayoutsChanged` only show `window_size` equal to the output size.

In niri's Overview the overlay bars stay drawn on top.

**Blur.** niri offers `ext_background_effect_manager_v1` (capability: blur).
`set_blur_region` blurs only the given region of the surface (here the
dropdown's rect), not the whole surface. For layer surfaces niri
defaults to *xray*: the blur shows the blurred backdrop or wallpaper and
hides the windows under it. The `xray false` layer rule above gives a
real blur of the windows under the region. The region is a `wl_region`,
so a beveled plate gets a rectangular blur unless the region is
stair-stepped.

**Rendering.**

Medians at the real 2560x1440 (nested niri fullscreen), dropdown closed / open.
`bench` repaints a canvas of that size 100-200 times. GPU times include `glFinish`.

| path | full redraw | clock only (bench) | clock frame, live |
|---|---|---|---|
| SHM: Skia raster into a memfd `wl_shm` buffer | 2.7 / 3.6 ms | 0.12 ms | 0.37 ms |
| GPU: EGL Wayland, `GL_RENDERER` = NVIDIA GeForce RTX 3060 Laptop GPU (not llvmpipe) | 0.34 / 0.68 ms | 0.13 ms | 0.44 ms (paint + flush) |

- SHM: niri keeps up to two buffers busy, so three are mapped (3 x 14 MiB
  at 2560x1440). Each new buffer needs one full repaint. The clock-only
  cost does not grow with the output size.
- GPU: `EGL_EXT_buffer_age` and `EGL_KHR_swap_buffers_with_damage` are
  both there, so partial redraws work. The first frame that drew the
  dropdown cost ~70 ms (shader compiles); after that, ~1-2 ms.
- Both paths: the very first full frame costs ~10-12 ms (fonts, glyphs,
  shaders).
- `eglSwapInterval(0)` is set on the surface. On Wayland, interval 1
  blocks the swap on the previous frame's callback.

**Globals niri 26.04 offers** (the ones that matter):

| global | version |
|---|---|
| `zwlr_layer_shell_v1` | v5 |
| `ext_background_effect_manager_v1` | v1 |
| `wp_fractional_scale_manager_v1` | v1 |
| `wp_viewporter` | v1 |
| `xdg_activation_v1` | v1 |
| `zwlr_foreign_toplevel_manager_v1` | v3 |
| `ext_foreign_toplevel_list_v1` | v1 |
| `ext_workspace_manager_v1` | v1 |
| `wp_cursor_shape_manager_v1` | v2 |
| `zwlr_screencopy_manager_v1` | v3 |
| `zwlr_virtual_pointer_manager_v1` | v2 |
| `zwp_virtual_keyboard_manager_v1` | v1 |
| `zwp_linux_dmabuf_v1` | v5 |
| `ext_idle_notifier_v1` | v2 |
| `ext_session_lock_manager_v1` | v1 |
| `zwlr_data_control_manager_v1` | v2 |
| `ext_data_control_manager_v1` | v1 |

Not offered: `wp_single_pixel_buffer_manager_v1` (hence the 1x1 wl_shm
pixel for the strips). Fractional scale was not exercised: the nested
output is scale 1.
