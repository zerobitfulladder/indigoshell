"""The take: the showcase choreography, performed and recorded.

    take.py              perform and record to ~/Videos/indigoshell-showcase/
    take.py --no-record  perform only

Needs `stage.py setup` first, and `stage.py reset` between takes. Every
step writes a marker — seconds from the recorder's start — to a JSON file
beside the video, along with each lyric line as sptlrx reports it; that
file is what edit.py cuts on.

The acts: Neovim from the launcher, quickly — into the project, its file
tree, a fuzzy-found file opened; the music player fetched from workspace
7, where setup left it cued and paused, onto this one beside Neovim;
Neovim quit and the player floated into the top-left corner; the song
resumed, and the bar coming alive; each panel opened, held and closed;
notifications, worked with — paused by hovering, a meter driven in
place, an action clicked, each dismissed; the chord menus; the power
menu as the last frame. qtile's layout is never switched.

Safety, since this drives a live session:

  * Before anything is typed, the focused window is checked against the
    one the step means; a mismatch aborts the take. Hover-to-focus is
    why: a window tiling in under a parked pointer takes focus, and typed
    text would land in whatever that is. Before typing, the pointer parks
    on the bar, which takes no focus.
  * Chord menus are only hovered, never given a digit — a digit fires on
    release, and the power menu's first row suspends the machine.
  * However it ends, every synthetic key and button is released and the
    recorder is stopped, so the file is finalised.
"""

import argparse
import json
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import xcffib
import xcffib.xproto as xp

import bar
import qtilectl as q
from indigoshell import text, theme
from indigoshell.widgets.notification import (_ACTION_PAD_X, _ACTION_PAD_Y,
                                              _ACTION_SIZE)
from stage import PLAYER_GROUP, STATE, TAKE_GROUP
from synth import CTRL, ESCAPE, RETURN, RIGHT_BUTTON, SHIFT, SUPER, Synth

W, H = 2560, 1440
MONITOR = "HDMI-0"
BAR_Y = H - 21
PARK = (1180, BAR_Y)                # the bar's empty middle
CENTRE = (W // 2, H // 2)
OUT = Path.home() / "Videos" / "indigoshell-showcase"

# The player, floated: a little over a quarter of the screen, and short
# enough to clear the system panel that opens bottom-left.
FLOAT_SIZE = (1400, 780)
GAP = 8                             # qtile's window margin

# Typing speeds, characters a second. The Neovim stretch is meant to be
# quick, near-instant; the launcher query a touch slower so its matching
# can still be seen narrowing.
QUERY_CPS, COMMAND_CPS = 24, 45

# The chord menu's rows, from the shell's config: right edge 12px in,
# bottom 52px up, 38px rows 4px apart.
MENU_X = W - 12 - 70
MENU_BOTTOM_ROW = H - 52 - 19
MENU_ROW_STEP = 42
MENUS = "sopg"                      # display, layout, profile, graphics

# After the last frame of the desktop, the recording runs on this long
# with the music playing: the widget reel (reel.py) is cut in over it,
# so the song carries on under the cards and no audio has to be sourced
# from anywhere else.
REEL_SECONDS = 18.0

BRIGHTNESS = ["-a", "brightness", "Brightness"]
BUILD = ["-a", "build", "-A", "open=Open", "-A", "log=Log",
         "Build finished", "indigoshell · 0 errors"]
INFO = ["-a", "indigoshell", "INDIGOSHELL", "toasts are drawn by the shell itself"]
CRITICAL = ["-a", "thermal", "-u", "critical", "Critical", "stays until clicked"]


class Abort(RuntimeError):
    pass


def _sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


class Take:
    def __init__(self, record: bool) -> None:
        self.record = record
        self.s = Synth()
        self.t0 = time.monotonic()
        self.markers: list[dict] = []
        # The player setup cued on workspace 7.
        self.ids: dict[str, int] = {
            "player": json.loads(STATE.read_text())["player_window"]}
        self.recorder: subprocess.Popen | None = None
        self.side: list[subprocess.Popen] = []
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.video = OUT / f"take-{stamp}.mp4"
        self.meta = OUT / f"take-{stamp}.json"

    # ── bookkeeping ─────────────────────────────────────────────────────
    def mark(self, name: str, **extra) -> None:
        t = round(time.monotonic() - self.t0, 3)
        self.markers.append({"name": name, "t": t, **extra})
        print(f"[{t:7.2f}] {name} {extra if extra else ''}", flush=True)

    def hold(self, seconds: float) -> None:
        time.sleep(seconds)

    def expect_focus(self, key: str) -> None:
        got = q.focused()
        if got["id"] != self.ids[key]:
            raise Abort(f"expected {key} focused, found {got['name']!r} — aborting")

    def new_window(self, known: set[int], kind: str = "kitty",
                   timeout: float = 20.0) -> int:
        return q.wait_for_window(
            lambda w: w["group"] == TAKE_GROUP and w["wm_class"][-1].lower() == kind,
            known=known, timeout=timeout)["id"]

    def park(self) -> None:
        self.s.move(*PARK)

    def wait_process(self, name: str, *, running: bool, timeout=10.0) -> None:
        deadline = time.monotonic() + timeout
        while bool(_sh("pgrep", "-x", name)) != running:
            if time.monotonic() > deadline:
                raise Abort(f"{name} did not {'start' if running else 'exit'}")
            self.hold(0.1)

    # ── recorder and lyrics ─────────────────────────────────────────────
    def start(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        if self.record:
            log = open(self.video.with_suffix(".log"), "w")
            self.recorder = subprocess.Popen(
                ["gpu-screen-recorder", "-w", MONITOR, "-f", "60", "-fm", "cfr",
                 "-k", "h264", "-bm", "qp", "-q", "ultra",
                 "-a", "default_output", "-ac", "aac", "-cursor", "yes",
                 "-o", str(self.video)],
                stdout=log, stderr=log)
        self.t0 = time.monotonic()
        lyrics = subprocess.Popen(["sptlrx", "pipe"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.side.append(lyrics)

        def follow() -> None:
            assert lyrics.stdout is not None
            for line in lyrics.stdout:
                if line.strip():
                    self.mark("lyric", text=line.strip())
        threading.Thread(target=follow, daemon=True).start()
        self.hold(2.0)      # encoder warm-up; trimmed in the edit

    def stop(self) -> None:
        if self.recorder is not None and self.recorder.poll() is None:
            # The recording's last frame is this moment, so this marker
            # minus the video's length is the recorder's start-up delay —
            # what edit.py shifts every marker by.
            self.mark("stop")
            self.recorder.send_signal(signal.SIGINT)
            self.recorder.wait(timeout=60)
        for p in self.side:
            if p.poll() is None:
                p.terminate()
        _sh("playerctl", "-p", "spotify_player", "pause")
        self.meta.write_text(json.dumps({
            "video": str(self.video) if self.record else None,
            "markers": self.markers,
        }, indent=2, ensure_ascii=False))
        print(f"markers: {self.meta}" + (f"\nvideo:   {self.video}" if self.record else ""))

    # ── moves ───────────────────────────────────────────────────────────
    def launch(self, query: str) -> int:
        """Open an app from the launcher; returns its window."""
        self.park()
        self.s.chord(SUPER, ord("d"))
        self.hold(0.35)
        self.s.type(query, cps=QUERY_CPS)
        self.hold(0.35)
        known = {w["id"] for w in q.windows()}
        self.s.tap(RETURN)
        return self.new_window(known)

    def float_top_left(self, key: str) -> None:
        """Float the focused window, resize it, carry it to the corner."""
        self.s.chord(SUPER, ord("t"))
        self.hold(0.5)
        # Resize from inside the window: mod+right-drag grows or shrinks
        # it by the pointer's travel, top-left anchored.
        w = q.window(self.ids[key])
        px, py = w["x"] + w["width"] * 2 // 3, w["y"] + w["height"] * 2 // 3
        self.s.move(px, py, duration=0.3)
        fw, fh = FLOAT_SIZE
        self.s.drag(px + fw - w["width"], py + fh - w["height"],
                    button=RIGHT_BUTTON, duration=0.55)
        self.hold(0.25)
        w = q.window(self.ids[key])
        px, py = w["x"] + w["width"] // 2, w["y"] + w["height"] // 2
        self.s.move(px, py, duration=0.3)
        self.s.drag(px + GAP - w["x"], py + GAP - w["y"], duration=0.7)

    def toast(self, nid: str) -> tuple[int, int, int, int] | None:
        return _window_geometry(f"notif-{nid}")

    def click_toast(self, nid: str, at=None) -> None:
        geo = self.toast(nid)
        if geo is None:
            return
        x, y, w, h = geo
        self.s.move(*(at(geo) if at else (x + w // 2, y + h // 2)), duration=0.4)
        self.hold(0.5)
        self.s.click()
        self.hold(0.7)

    def hover(self, key: str) -> None:
        """Pointer onto a window — which, with hover-to-focus, focuses it."""
        w = q.window(self.ids[key])
        self.s.move(w["x"] + w["width"] // 2, w["y"] + w["height"] // 2,
                    duration=0.35)
        self.hold(0.2)

    # ── acts ────────────────────────────────────────────────────────────
    def cold_open(self) -> None:
        self.s.move(*CENTRE, duration=0.4)
        self.mark("open")
        self.hold(2.0)

    def nvim(self) -> None:
        self.mark("nvim")
        self.ids["nvim"] = self.launch("nvim")
        self.hold(0.6)                  # nvim and its plugins load
        self.expect_focus("nvim")
        self.s.type(":cd ~/Projects/indigoshell\n", cps=COMMAND_CPS)
        self.hold(0.15)
        self.mark("tree")
        self.s.type("\\e", cps=COMMAND_CPS)
        self.hold(0.6)
        self.s.chord(CTRL, ord("w"))    # out of the tree, into the editor
        self.s.tap(ord("l"))
        self.hold(0.1)
        self.mark("find")
        self.s.type("\\ff", cps=COMMAND_CPS)
        self.hold(0.35)
        self.s.type("launcher", cps=30)
        self.hold(0.35)
        self.s.tap(RETURN)
        self.hold(1.4)

    def fetch_player(self) -> None:
        """Over to workspace 7, send the cued player to 6, come back."""
        self.mark("fetch")
        self.s.chord(SUPER, ord(PLAYER_GROUP))
        self.hold(0.9)
        self.expect_focus("player")
        self.s.chord(SUPER, SHIFT, ord(TAKE_GROUP))
        self.hold(0.5)
        self.s.chord(SUPER, ord(TAKE_GROUP))
        self.hold(1.0)                  # Neovim and the player, side by side

        self.mark("close_nvim")
        self.hover("nvim")
        self.expect_focus("nvim")
        self.s.type(":qa\n", cps=COMMAND_CPS)
        q.wait_gone(self.ids["nvim"])
        self.hold(0.6)

        self.hover("player")
        self.expect_focus("player")
        self.mark("float")
        self.float_top_left("player")
        self.mark("floated")
        self.hold(0.6)

    def drop(self) -> None:
        """Resume the cued song, from inside the player."""
        self.hover("player")
        self.expect_focus("player")
        self.mark("play")
        self.s.tap(ord(" "))            # spotify_player: ResumePause
        deadline = time.monotonic() + 1.5
        while _sh("playerctl", "-p", "spotify_player", "status") != "Playing":
            if time.monotonic() > deadline:
                _sh("playerctl", "-p", "spotify_player", "play")
                self.mark("play_fallback")
                break
            self.hold(0.05)
        meta = _sh("playerctl", "-p", "spotify_player", "metadata", "--format",
                   "{{ artist }} — {{ title }}")
        self.mark("music", track=meta)
        self.park()
        self.hold(7.5)

    def panel(self, target: tuple[int, int], name: str, hold: float) -> None:
        """Open a panel from its bar widget, hold, and close it with a
        second click on the same widget — one panel on screen at a time,
        never one opening over another."""
        self.s.move(*target)
        self.hold(0.3)
        self.mark(name)
        self.s.click()
        self.hold(hold)
        self.s.click()
        self.mark(f"{name}_closed")
        self.hold(0.7)

    def panels(self) -> None:
        t = bar.targets(W, H, playing=True)
        self.panel(t["meters"], "hardware", 3.2)
        self.panel(t["network"], "network", 2.8)
        self.s.move(*t["volume"])
        self.hold(0.3)
        self.mark("volume")
        self.s.scroll(up=True, notches=3, interval=0.22)
        self.hold(0.5)
        self.s.scroll(up=False, notches=3, interval=0.22)
        self.hold(0.8)
        self.panel(t["indigo"], "system", 3.2)
        self.panel(t["tray"], "tray", 2.6)

    def notifications(self) -> None:
        self.mark("notifications")
        self.park()
        info = _sh("notify-send", "-p", *INFO)
        self.hold(0.9)
        bright = _sh("notify-send", "-p", "-h", "int:value:70", *BRIGHTNESS, "70%")
        self.hold(0.9)
        # A notify-send with actions waits for the answer, so it runs in
        # the background; stdbuf makes it print its id before it waits.
        build_proc = subprocess.Popen(["stdbuf", "-oL", "notify-send", "-p", *BUILD],
                                      stdout=subprocess.PIPE, text=True)
        self.side.append(build_proc)
        assert build_proc.stdout is not None
        ready, _, _ = select.select([build_proc.stdout], [], [], 1.5)
        build = build_proc.stdout.readline().strip() if ready else ""
        self.hold(0.9)
        critical = _sh("notify-send", "-p", *CRITICAL)
        self.hold(1.4)

        # Hovering any toast pauses every timer: the stack holds still
        # while it is worked through.
        geo = self.toast(info)
        if geo is not None:
            x, y, w, h = geo
            self.s.move(x + w // 2, y + h // 2, duration=0.5)
        self.mark("hover")
        self.hold(1.0)

        # The meter, driven in place by replace-by-id.
        self.mark("meter")
        geo = self.toast(bright)
        if geo is not None:
            x, y, w, h = geo
            self.s.move(x + w // 2, y + h // 2, duration=0.35)
        for value in (55, 40, 25, 45, 70, 90):
            _sh("notify-send", "-r", bright, "-h", f"int:value:{value}",
                *BRIGHTNESS, f"{value}%")
            self.hold(0.32)
        self.hold(0.8)

        if build:
            self.mark("action")
            self.click_toast(build, at=_first_action)
        self.mark("dismiss")
        self.click_toast(critical)
        self.click_toast(bright)
        self.click_toast(info)
        self.park()
        self.hold(0.8)

    def menus(self) -> None:
        away = (W // 2 + 300, H // 2)
        self.s.move(*away)
        for key in MENUS:
            self.mark(f"menu_{key}")
            self.s.chord(SUPER, SHIFT, ord(key))
            self.hold(0.6)
            self.s.move(MENU_X, MENU_BOTTOM_ROW, duration=0.4)
            self.hold(0.45)
            self.s.move(MENU_X, MENU_BOTTOM_ROW - MENU_ROW_STEP, duration=0.2)
            self.hold(0.4)
            self.s.move(*away, duration=0.35)   # leaving disarms the row
            self.s.tap(ESCAPE)
            self.hold(0.35)

        self.mark("power")
        self.s.chord(SUPER, ESCAPE)
        self.hold(0.6)
        self.s.move(MENU_X, MENU_BOTTOM_ROW, duration=0.45)
        for step in (1, 2):
            self.hold(0.5)
            self.s.move(MENU_X, MENU_BOTTOM_ROW - step * MENU_ROW_STEP,
                        duration=0.2)
        self.hold(1.3)
        self.s.move(*away, duration=0.5)
        self.s.tap(ESCAPE)
        self.mark("end")
        self.hold(1.2)
        self.mark("reel")
        self.park()
        self.hold(REEL_SECONDS)

    def perform(self) -> None:
        self.start()
        for act in (self.cold_open, self.nvim, self.fetch_player, self.drop,
                    self.panels, self.notifications, self.menus):
            act()


def _first_action(geo: tuple[int, int, int, int]) -> tuple[int, int]:
    """Centre of a toast's first action chip — "Open". Chips sit at the
    bottom-left of the toast, inside the bleed every effect window has."""
    x, y, w, h = geo
    bleed = (w - theme.NOTIF_WIDTH) // 2
    chip_w = text.measure("Open", _ACTION_SIZE) + 2 * _ACTION_PAD_X
    chip_h = text.line_height(_ACTION_SIZE) + 2 * _ACTION_PAD_Y
    return (round(x + bleed + theme.NOTIF_PADDING_X + chip_w / 2),
            round(y + h - bleed - theme.NOTIF_PADDING_Y - chip_h / 2))


def _window_geometry(instance: str) -> tuple[int, int, int, int] | None:
    """Root-relative geometry of the top-level window whose WM_CLASS
    instance is `instance` — the shell names a toast's window by its id."""
    conn = xcffib.connect()
    try:
        root = conn.get_setup().roots[0].root
        for wid in conn.core.QueryTree(root).reply().children:
            prop = conn.core.GetProperty(False, wid, xp.Atom.WM_CLASS,
                                         xp.Atom.STRING, 0, 64).reply()
            name = b"".join(prop.value).split(b"\0")[0].decode(errors="replace")
            if name == instance:
                g = conn.core.GetGeometry(wid).reply()
                return g.x, g.y, g.width, g.height
        return None
    finally:
        conn.disconnect()


def preflight() -> None:
    if not STATE.exists():
        sys.exit("take: run `stage.py setup` first")
    if q.current_group() != TAKE_GROUP or any(
            w["group"] == TAKE_GROUP for w in q.windows()):
        sys.exit("take: workspace 6 must be showing and empty — `stage.py reset`")
    player = json.loads(STATE.read_text()).get("player_window")
    info = q.window(player) if player else None
    if info is None or info["group"] != PLAYER_GROUP:
        sys.exit("take: no cued player on workspace 7 — `stage.py reset`")
    if _sh("playerctl", "-p", "spotify_player", "status") != "Paused":
        sys.exit("take: the player is not paused at its cue — `stage.py reset`")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()
    preflight()

    def interrupted(*_):
        raise KeyboardInterrupt     # `kill <pid>` aborts like Ctrl+C
    signal.signal(signal.SIGTERM, interrupted)
    take = Take(record=not args.no_record)
    try:
        take.perform()
    except (Abort, KeyboardInterrupt, TimeoutError) as e:
        take.mark("aborted", reason=str(e) or type(e).__name__)
    finally:
        take.s.close()
        take.stop()


if __name__ == "__main__":
    main()
