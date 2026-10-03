# Showcase

A scripted, recorded tour of the shell: the launcher opening Neovim,
the music player fetched from another workspace and floated, the song
resumed and the bar coming alive, the panels, the tray, notifications
worked through, the chord menus — then a reel of the bar's widgets, cut
to the same song. The take is performed by
synthetic input over XTEST, so it is the same every time and can be
re-shot until it is right.

Run from this folder with the project's venv:

    PY=/home/lavender/Projects/indigoshell/.venv/bin/python
    $PY stage.py setup                  # once
    PYTHONPATH=../.. $PY take.py        # ~1:45, hands off
    $PY stage.py reset                  # between takes
    PYTHONPATH=../.. $PY reel.py render ~/Videos/indigoshell-showcase/take-*.json \
        [--at SECONDS --length SECONDS]  # where the desktop cuts to the reel
    $PY edit.py ~/Videos/indigoshell-showcase/take-*.json
    $PY stage.py teardown               # puts everything back

| File | What |
|---|---|
| `synth.py` | keyboard and mouse over XTEST: eased pointer paths, uneven typing, everything released on exit |
| `qtilectl.py` | `qtile cmd-obj` client: windows, focus, staging, closing |
| `bar.py` | click targets from the real bar laid out at the screen's width, idle or playing |
| `stage.py` | setup / reset / teardown — what it changes and restores is in its docstring |
| `take.py` | the choreography and the recorder; writes markers beside the video |
| `reel.py` | the closing widget reel: each widget alone and large, scripted values, cut to the take's music |
| `audio.py` | visualiser bands and beats from a recording, for the reel |
| `edit.py` | the cut from those markers: trims, zooms on the bar, the reel appended over the continuing music, fades |
| `readme_media.py` | the README's pictures in `docs/media/`: footage cut from a take, and widget loops from the real classes |

The take plays on workspace 6 and fetches the music player, which setup
cues and pauses there beforehand, from workspace 7; it refuses to start
unless both were empty.
Before typing anything it checks which window has focus and aborts on a
mismatch. It never presses a digit in a chord menu,
since digits fire on release. `kill <pid>` aborts it cleanly, releasing
every synthetic key and finalising the recording.

Recordings go to `~/Videos/indigoshell-showcase/`, staging state to
`~/.cache/indigoshell-showcase/`.
