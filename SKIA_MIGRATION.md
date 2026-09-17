# Skia migration — decision record & plan

**Status:** decided 2026-08-05, not started. This file is the handoff for the
next working session.

## The decision

indigoshell will be refactored off GTK3 entirely. Target stack:

| Concern       | Today                    | Target                                   |
|---------------|--------------------------|------------------------------------------|
| Windowing/input | GTK3 / GDK             | **xcffib** (XCB: windows, events, seat grabs, struts, ARGB visuals) |
| Rendering     | GTK widgets + CSS + cairo| **skia-python** (CPU raster; SkSL shaders; text via Skia FontMgr — no Pango needed) |
| D-Bus (notifications daemon, SNI tray, dbusmenu) | GLib loop | **dbus-fast** on asyncio (one loop for D-Bus, timers, subprocs, IPC) |
| Terminals (VTE popups/toasts) | embedded VTE | dropped — spawn external terminals for interactive TUIs; render captured stdout natively for toasts |

Cairo was evaluated (cairocffi + pangocffi) and rejected in favor of Skia:
Skia adds real Gaussian blur/glow, SkSL per-pixel shader effects that run on
the **CPU raster backend** (no GL context), its own text shaping (dissolves
the pangocffi bus-factor concern), and the same SkSL moves to a GPU surface
unchanged if ever wanted. Cairo can't blur at all and has no per-pixel
programmability. GPU is explicitly deferred — everything below was measured
CPU-only.

## Verified during prototyping (2026-08-04/05)

Working prototypes live in [`prototypes/skia/`](prototypes/skia/). Run with:

```bash
uv run --no-project --with skia-python python prototypes/skia/demo.py 3 frames3x
uv run --no-project --with skia-python --with xcffib python prototypes/skia/panel_x11.py           # interactive
uv run --no-project --with skia-python --with xcffib python prototypes/skia/panel_x11.py --smoke 40 # no grabs, self-verifies
```

- **`demo.py`** — BatteryMeter ported to Skia; state machine ported verbatim,
  only `_draw` changed. Neon glow = 2 extra `drawRect` passes with
  `MaskFilter.MakeBlur`. Renders a flat-vs-glow GIF sheet (assemble with the
  ffmpeg palettegen line in the script header comments).
- **`shader_test.py`** — SkSL runtime shaders on CPU raster: ~5 ms/frame at
  624×160 (1 sample/px).
- **`panel_glitch.py`** — braindance-style panel (mirrors
  `~/Projects/VitureBD/wreath/braindance` `render.py set_panel()` design +
  palette) with full-frame glitch post-shader: ~17.5 ms/frame at 620×420
  (3 samples/px). Fine for bursts/transitions; continuous 60 fps full-panel
  shading would want the GPU backend.
- **`panel_x11.py`** — **the architecture proof, seed of the future
  `backend/x11.py`.** Zero GTK: centered override-redirect ARGB window,
  keyboard+pointer seat grab (chord-menu style), 20 fps interactive menu
  (mini port of braindance `menu.py`), SkSL glitch driven by input, beveled
  top-right corner, frosted-glass backdrop (root captured pre-map →
  Skia-blurred → clipped to the panel `skia.Path`, so blur **cannot** leak
  past the bevel — fixes the picom rectangular-blur leak we have today).

### API gotchas (each cost real debugging time)

- skia-python uniforms/children **must** go through `skia.RuntimeEffectBuilder`
  (`setUniform`/`setChild`); hand-packed `skia.Data` binds silently wrong →
  NaN-white output.
- Shader-to-window output must draw with `BlendMode.kSrc` — default SrcOver
  composites onto the previous frame: ghost accumulation + alpha saturating
  to opaque.
- `skia.Surface(w, h)` N32 on this platform = **BGRA** premul — matches X11
  ZPixmap on little-endian for both depth 24 and 32 (verified by pixel
  readback).
- xcffib `PutImage` needs an explicit `data_len` arg; chunk uploads (~60 KB)
  to stay under request limits. 620×420 uploads are a non-issue without SHM.
- ARGB window: pick the depth-32 visual from `screen.allowed_depths`, create
  a matching colormap, and pass BackPixel **and** BorderPixel (BadMatch
  otherwise).
- Root `GetImage` pad bytes are 0 — force alpha bytes to 0xff before wrapping
  in a Skia image.
- `skia.ImageFilters.Blur(sx, sy, skia.TileMode.kClamp)` — kClamp avoids dark
  edge halos without oversizing the capture.

### Dependency due diligence (checked live 2026-08-04)

- `skia-python` v144 (2026-03), active, cp314 wheels, ~14 MB.
- `dbus-fast` 5.x, very active (Home Assistant ecosystem), asyncio-first,
  serves interfaces — covers notifications daemon + SNI + dbusmenu.
- `xcffib` 1.12 builds/works fine (verified in the prototypes).
- pyproject after migration: drop `pygobject` + `python-xlib`, add
  `skia-python`, `xcffib`, `dbus-fast`; keep `psutil`, `watchdog`.

## Staged plan (daily driver must keep working)

1. **Skia widget model inside GTK hosts.** New `Widget` contract:
   `measure()` / `paint(canvas, w, h)` / hit-test + service subscriptions;
   one surface per window; own layout (Box/Spacer become rect math) and one
   event-dispatch walk. Bridge: blit Skia raster into the GTK draw callback.
   Port widgets incrementally (draw-heavy ones are mechanical; hardware/
   network panels are the real rewrites). Kill the Style→CSS pipeline as
   widgets move over. Pure-logic extraction pattern proven in `demo.py`
   (`_cell_color`-style separation).
2. **Services to asyncio + dbus-fast** (own thread while GTK still hosts UI,
   queue to the UI side). Survives step 3 untouched.
3. **XCB backend replaces GTK per window kind** (bar → popups → chord menus →
   notifications). `panel_x11.py` already demonstrates the host contract:
   make window, hand me a surface, deliver input, grab seat.
4. **Delete GTK/VTE/CSS.** Interactive TUI popups → external terminal spawns;
   TermToasts → captured stdout rendered natively (keeps pipeline visibility).

The plugin system (registry merging widgets/services/flows from a user dir)
comes **after** step 1 — the plugin API is the widget contract, and it must
not be GTK-shaped. Flow typing (in-process `Stage` objects, subprocess
manifest as one adapter) is independent and can happen anytime.

## Open questions for next session

- Migration order within step 1: which widget first (battery meter is the
  proven guinea pig)?
- Theme port: `theme.py` tokens map cleanly; decide Color4f pre-parse layer.
- Live frosted blur (snapshot goes stale behind long-lived panels) — periodic
  recapture strategy, or accept-as-modal.
- Effects vocabulary as first-class theme tokens (glow sigma, burst envelope,
  materialize) — the SkSL from `panel_x11.py` is the starting library.
- X11-only is accepted (struts/xrandr/grabs already assume it); revisit
  Wayland only if life changes.
