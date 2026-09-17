# Visual proposals

Offline-rendered sheets: the current look of each bar widget next to
sketched alternatives, and frame strips of panel open/close transitions.
Nothing here touches the running shell; the sketches are throwaway paint
code that reuses the theme, font and shapes so they are honest previews.

| Sheet | What |
|---|---|
| `workspaces.png` | current + A chamfered tabs, B hex readout, C signal ticks, D segmented rail |
| `gauges.png` | current + A arc dials, B bracketed bars with peak marker, C sparklines, D mixer columns |
| `media.png` | current + A mirrored spectrum, B oscilloscope, C HUD plate, D LED matrix |
| `volume.png` | current + A wedge + icon, B ring, C rail + readout, D stack + icon |
| `network.png` | current + A signal bars, B live rates, C NET tag, D icon + stack |
| `after.png` | the chosen configuration rendered from the real widget classes |
| `panel_open.png` / `panel_close.png` | current ScanLock + A CRT power, B boot wipe, C hologram, D blinds, E decode cells |

Animated versions of the panel transitions are in `gif/`: one looping
30fps GIF per effect (open, hold, close, pause) and `panel_all.gif`
with all six on one timeline.

Regenerate from this folder:

    PYTHONPATH=../.. ../../.venv/bin/python render_widgets.py
    PYTHONPATH=../.. ../../.venv/bin/python render_panels.py
    PYTHONPATH=../.. ../../.venv/bin/python render_gifs.py    # needs ffmpeg

The panel effects are prototype SkSL inside `render_panels.py`; a chosen
one moves into `indigoshell2/effects.py` as a proper `Effect` class.
