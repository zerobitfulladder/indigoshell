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
| `launcher.png` | the real launcher at 1x on this machine's apps: empty, matches lit, selection moved, no match |
| `call.png` | the real call overlay at 1x over a blurred stand-in desktop: answered, working, thinking, speaking, asking, not signed in; the status chip working, ringing, asking, missed |
| `call_looks*.png`, `gif/call_look_*.gif`, `gif/call_reveal_*.gif` | looks proposed for the call (brackets, tags, trace, sections, signal, open line) and ways for a reply to arrive; brackets + trace + the decode reveal were built |
| `call_holo.png`, `gif/call_holo*.gif` | the built hologram: the raster, and the scan glitching down the call and the status chip |
| `call_progress.png` | progress bars from the real widgets: a download, a skill's pipeline, a busy stage, a step's summary, a background job, the status chip |
| `call_chat.png` | the call's one chat from the real widgets: how full it is (the meter in her heading, the user's pick C of A under the heading / B at the foot / C in the heading); paging back an exchange at a time on both plates, a hub turn, the edge of what she holds, a line said into a running turn |

Animated versions of the panel transitions are in `gif/`: one looping
30fps GIF per effect (open, hold, close, pause) and `panel_all.gif`
with all six on one timeline.

Regenerate from this folder:

    PYTHONPATH=../.. ../../.venv/bin/python render_widgets.py
    PYTHONPATH=../.. ../../.venv/bin/python render_panels.py
    PYTHONPATH=../.. ../../.venv/bin/python render_gifs.py    # needs ffmpeg
    PYTHONPATH=../.. ../../.venv/bin/python render_launcher.py
    PYTHONPATH=../.. ../../.venv/bin/python render_call.py
    PYTHONPATH=../.. ../../.venv/bin/python render_call_looks.py   # needs ffmpeg
    PYTHONPATH=../.. ../../.venv/bin/python render_call_holo.py    # needs ffmpeg
    PYTHONPATH=../.. ../../.venv/bin/python render_call_progress.py
    PYTHONPATH=../.. ../../.venv/bin/python render_call_chat.py
    PYTHONPATH=../.. ../../.venv/bin/python readme_call.py         # the README's call media, into docs/media

The panel effects are prototype SkSL inside `render_panels.py`; a chosen
one moves into `indigoshell/effects.py` as a proper `Effect` class.
