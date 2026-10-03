"""Set the stage for a take, reset it between takes, and put everything back.

    stage.py setup      before the first take
    stage.py reset      between takes: close what the take opened, re-stage
    stage.py teardown   after the last: restore every change

The take happens on workspace 6, and fetches its music player from
workspace 7; both must be empty, so nothing of the user's is moved.

The player is made ready before the recorder runs: spotify_player is
opened on workspace 7, the song searched for and started, moved to
SONG_START and paused — so the take never searches or scrolls in it, it
only brings the window over and resumes. Its now-playing toast is left
to expire first, so the opening frame is clean.

What setup changes, and teardown restores:

  * spotify_player: a running instance is quit — two players would fight
    over playback — and `notify_timeout_in_secs` is set to 6 so a
    now-playing toast expires on its own.
  * The default sink's volume is recorded and restored.

Windows are closed by stopping what runs in them, not through the window
manager: kitty asks for confirmation before closing a window with a
program running, and a dialog mid-cleanup would hang it. A kitty started
for one window (the launcher's, the player's) is terminated outright;
the shared `kitty -1` instance, which may hold the user's own terminals,
is only asked to close the one window, once its program has been
stopped. Only windows that did not exist in the snapshot taken before
setup are ever touched.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import xcffib
import xcffib.xproto as xp

import qtilectl as q

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "indigoshell-showcase"
STATE = CACHE / "stage.json"
SPOTIFY_TOML = Path.home() / ".config/spotify-player/app.toml"

TAKE_GROUP, PLAYER_GROUP = "6", "7"
STAGE_GROUPS = (TAKE_GROUP, PLAYER_GROUP)
PROGRAMS = ("spotify_player", "nvim")           # what the take runs in kitty

SONG_QUERY = "resist and disorder rezodrone"
# 0.8s before the chorus's first line, "Rise up! Resist and disorder"
# (1:09.67 in LRCLIB's synced lyrics): the music drops in on the chorus,
# and ~72s later, as the closing reel runs, the second chorus arrives.
SONG_START = 68.9


def _sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def _log(msg: str) -> None:
    print(f"stage: {msg}", flush=True)


def _volume() -> str:
    m = re.search(r"(\d+)%", _sh("pactl", "get-sink-volume", "@DEFAULT_SINK@"))
    return f"{m.group(1)}%" if m else ""


def _player(*args: str) -> str:
    return _sh("playerctl", "-p", "spotify_player", *args)


def _load() -> dict:
    return json.loads(STATE.read_text())


def _save(state: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _wait(test, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not test():
        if time.monotonic() > deadline:
            sys.exit(f"stage: {what}")
        time.sleep(0.1)


# ── windows ─────────────────────────────────────────────────────────────
def _top_level_names() -> list[str]:
    conn = xcffib.connect()
    try:
        root = conn.get_setup().roots[0].root
        names = []
        for wid in conn.core.QueryTree(root).reply().children:
            prop = conn.core.GetProperty(False, wid, xp.Atom.WM_CLASS,
                                         xp.Atom.STRING, 0, 64).reply()
            names.append(b"".join(prop.value).split(b"\0")[0].decode(errors="replace"))
        return names
    finally:
        conn.disconnect()


def _window_pid(win_id: int) -> int | None:
    conn = xcffib.connect()
    try:
        atom = conn.core.InternAtom(True, len(b"_NET_WM_PID"),
                                    b"_NET_WM_PID").reply().atom
        prop = conn.core.GetProperty(False, win_id, atom, xp.Atom.CARDINAL,
                                     0, 1).reply()
        values = prop.value.to_atoms() if prop.value_len else ()
        return int(values[0]) if values else None
    except Exception:
        return None
    finally:
        conn.disconnect()


def _children(pid: int) -> list[int]:
    out = _sh("pgrep", "-P", str(pid))
    return [int(p) for p in out.split()] if out else []


def _stop_programs_under(pid: int) -> None:
    """SIGTERM every PROGRAMS process below `pid`, and wait for them."""
    targets, frontier = [], [pid]
    while frontier:
        for child in _children(frontier.pop()):
            frontier.append(child)
            try:
                if Path(f"/proc/{child}/comm").read_text().strip() in PROGRAMS:
                    targets.append(child)
            except OSError:
                pass
    for t in targets:
        try:
            os.kill(t, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while any(Path(f"/proc/{t}").exists() for t in targets):
        if time.monotonic() > deadline:
            break
        time.sleep(0.1)


def _close(win: dict) -> None:
    pid = _window_pid(win["id"])
    shared = False
    if pid is not None:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            shared = b"-1" in argv or b"--single-instance" in argv
        except OSError:
            pid = None
    _log(f"closing {win['name'][:40]!r} on {win['group']}")
    if pid is not None and not shared:
        os.kill(pid, signal.SIGTERM)         # a kitty of its own: all of it
    else:
        if pid is not None:
            _stop_programs_under(pid)        # leaves a prompt: no dialog
        q.kill(win["id"])
    try:
        q.wait_gone(win["id"])
    except TimeoutError:
        _log(f"  {win['id']} did not close")


def _close_new_windows(state: dict, groups) -> None:
    before = set(state["windows_before"])
    for w in q.windows():
        if w["id"] not in before and w["group"] in groups:
            _close(w)


# ── the player ──────────────────────────────────────────────────────────
def _stage_player(state: dict) -> None:
    """spotify_player on workspace 7 with the song cued at SONG_START,
    paused, and its toast gone."""
    from synth import RETURN, TAB, Synth

    _log("opening spotify_player on 7 and cueing the song")
    q.show_group(PLAYER_GROUP)
    known = {w["id"] for w in q.windows()}
    q.spawn("kitty spotify_player")
    win = q.wait_for_window(lambda w: w["group"] == PLAYER_GROUP, known=known)
    _wait(lambda: bool(_sh("pgrep", "-x", "spotify_player")), 10,
          "spotify_player did not start")
    time.sleep(3.0)                         # it connects and draws

    s = Synth()
    try:
        info = q.window(win["id"]) or win
        s.move(info["x"] + info["width"] // 2, info["y"] + info["height"] // 2)
        time.sleep(0.3)
        if q.focused()["id"] != win["id"]:
            sys.exit("stage: the player window does not have focus")
        s.tap(ord("g"))
        s.tap(ord("s"))
        time.sleep(0.7)
        s.type(SONG_QUERY, cps=25)
        s.tap(RETURN)
        time.sleep(2.0)                     # results arrive
        s.tap(TAB)                          # input -> track results
        time.sleep(0.5)
        s.tap(RETURN)
    finally:
        s.close()
    _wait(lambda: _player("status") == "Playing", 12, "the song did not start")
    track = _player("metadata", "--format", "{{ artist }} — {{ title }}")
    _player("position", str(SONG_START))
    time.sleep(0.8)
    _player("pause")
    _wait(lambda: _player("status") == "Paused", 5, "the song did not pause")
    time.sleep(0.5)
    pos = float(_player("position") or 0)
    if pos < SONG_START - 3:
        sys.exit(f"stage: the seek did not take (at {pos:.1f}s)")
    _log(f"  cued {track!r} at {pos:.1f}s, paused")

    _log("waiting for its toast to expire")
    _wait(lambda: not any(n.startswith("notif-") for n in _top_level_names()),
          15, "a toast is still showing")
    state["player_window"] = win["id"]
    _save(state)
    q.show_group(TAKE_GROUP)


# ── commands ────────────────────────────────────────────────────────────
def preflight() -> None:
    problems = []
    if subprocess.run([str(PROJECT / ".venv/bin/python"), str(PROJECT / "main.py"),
                       "ping"], capture_output=True).returncode != 0:
        problems.append("indigoshell is not running")
    if "us" not in _sh("setxkbmap", "-query"):
        problems.append("keyboard layout is not US (the take types on it)")
    busy = {w["group"] for w in q.windows()} & set(STAGE_GROUPS)
    if busy:
        problems.append(f"workspaces {sorted(busy)} are not empty")
    if not shutil.which("gpu-screen-recorder"):
        problems.append("gpu-screen-recorder is missing")
    if problems:
        sys.exit("stage: cannot set up:\n  " + "\n  ".join(problems))


def setup() -> None:
    if STATE.exists():
        sys.exit("stage: already set up — run `stage.py teardown` first")
    preflight()
    state = {
        "windows_before": [w["id"] for w in q.windows()],
        "group_before": q.current_group(),
        "volume": _volume(),
        "spotify_toml": SPOTIFY_TOML.read_text(),
    }
    _save(state)                    # from here on, teardown can undo it

    _log("pausing players, quitting spotify_player")
    _sh("playerctl", "-a", "pause")
    pid = _sh("pgrep", "-x", "spotify_player")
    if pid:
        os.kill(int(pid.split()[0]), signal.SIGTERM)
        # Wait for the process, not a window: started from a shell, it
        # leaves its terminal open behind it.
        _wait(lambda: not _sh("pgrep", "-x", "spotify_player"), 10,
              "spotify_player did not quit")
    SPOTIFY_TOML.write_text(re.sub(r"(?m)^notify_timeout_in_secs\s*=.*$",
                                   "notify_timeout_in_secs = 6",
                                   state["spotify_toml"]))
    _stage_player(state)
    _log("ready: workspace 6 is empty and showing, the player waits on 7")


def reset() -> None:
    """Back to the opening frame: the take's windows closed — the player
    among them — and a fresh player cued on 7."""
    state = _load()
    _sh("playerctl", "-a", "pause")
    _close_new_windows(state, STAGE_GROUPS)
    if state["volume"]:
        _sh("pactl", "set-sink-volume", "@DEFAULT_SINK@", state["volume"])
    _stage_player(state)
    _log("reset: workspace 6 is empty and showing, the player waits on 7")


def teardown() -> None:
    state = _load()
    _sh("playerctl", "-a", "pause")
    _close_new_windows(state, STAGE_GROUPS)
    SPOTIFY_TOML.write_text(state["spotify_toml"])
    if state["volume"]:
        _sh("pactl", "set-sink-volume", "@DEFAULT_SINK@", state["volume"])
    q.show_group(state["group_before"])
    STATE.unlink()
    _log("teardown complete — spotify_player is not restarted")


if __name__ == "__main__":
    commands = {"setup": setup, "reset": reset, "teardown": teardown}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(commands)}")
    commands[sys.argv[1]]()
