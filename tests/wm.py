"""The window-manager layer (indigoshell/wm) and what sits on it.

    .venv/bin/python tests/wm.py            pure, then X11 + qtile read-only
    .venv/bin/python tests/wm.py --niri     ... then a nested niri (half a
                                            minute; --shots DIR keeps two
                                            of its screenshots)

1. Pure, X11 + qtile: the qtile commands each call sends and what comes
   of their answers (workspaces, windows, focus, screenshots, the
   clipboard, xrandr's outputs and the one-call "use alone", RandR
   monitors, logging out), against a stand-in for the subprocesses; the
   laptop hands' replies built from them.
2. Pure, niri: requests in the shapes `niri msg` 26.04 sends (recorded
   from it), against a stand-in niri socket answering in niri's shapes;
   workspace names, the bar's readout of one monitor (the spare empty
   workspace left out), the state kept from the event stream, outputs
   and "use alone" as output changes, window screenshots (cut out,
   whole monitor, not shown); the bar widget on a follower.
3. Pure, Wayland: the wire's argument packing, frames turned upright.
4. Pure, tiling: qtile's two columns as decisions on niri's windows.
5. Live, X11 + qtile, read-only: the layer reads the running session as
   qtile, EWMH and xrandr say it is (each read here too, directly). No
   focus, window, workspace or clipboard changes, no screenshots.
6. `--niri`: a nested niri of its own (a window on qtile group 8, put
   there by a runtime qtile rule this removes again), with kitty windows
   in it and the tiling helper on a bare python3 (-S: the standard
   library only): workspaces and windows, focus, screenshots as PNG
   (one compared with niri's own), the clipboard inside it, outputs,
   the bar's readout following events, the laptop hands, the tiling
   (1-4 windows, closing them, a resize by hand that stays), and quit.
   Everything it starts is stopped; nothing touches the X session's
   focus, clipboard or screen.
"""

import argparse
import asyncio
import contextlib
import inspect
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from indigoshell import wm  # noqa: E402
from indigoshell.wm import niri as nirimod, tiling, wayland, x11  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail if not ok else ''}".rstrip(),
          flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def call(tool: str, **args):
    from indigoshell.core.hands import Call
    return Call(id="t", tool=tool, args=args, timeout=60)


def texts(blocks) -> str:
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text")


def use(backend) -> None:
    """Make `backend` this process's window manager."""
    wm._current = backend


# ── 1. X11 + qtile, pure ────────────────────────────────────────────────
GROUPS = {"1": {"name": "1", "focus": "term", "windows": ["term", "notes"],
                "screen": 0},
          "2": {"name": "2", "focus": "web", "windows": ["web"],
                "screen": None},
          "3": {"name": "3", "focus": None, "windows": [], "screen": None}}
WINDOWS = [{"id": 11, "name": "term", "group": "1",
            "wm_class": ["kitty", "kitty"]},
           {"id": 12, "name": "notes", "group": "1",
            "wm_class": ["gedit", "Gedit"]},
           {"id": 21, "name": "web", "group": "2",
            "wm_class": ["Navigator", "firefox"]}]
SCREENS = [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080},
           {"index": 1, "x": 1920, "y": 0, "width": 2560, "height": 1440}]
XRANDR = """\
Screen 0: minimum 8 x 8, current 2560 x 1440, maximum 32767 x 32767
eDP-1 connected (normal left inverted right x axis y axis)
   1920x1080     60.02 +  59.93    48.00
   1680x1050     59.88
HDMI-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   2560x1440     59.95 + 143.97*
   1920x1080i    60.00    50.00
DP-1 disconnected (normal left inverted right x axis y axis)
DP-2 disconnected 1920x1080+2560+0 (normal left inverted right x axis y axis) 0mm x 0mm
"""
MONITORS = """Monitors: 2
 0: +*HDMI-1 2560/597x1440/336+0+0  HDMI-1
 1: braindance 1920/309x1080/174+2560+0  DP-2
"""


class FakeRun:
    """Stands in for wm.run: records argv, answers from a table."""

    def __init__(self, answers=None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    async def __call__(self, argv, *, data=None, timeout=15.0):
        argv = list(argv)
        self.calls.append(argv)
        key = " ".join(argv)
        for prefix, answer in self.answers.items():
            if key.startswith(prefix):
                rc, out, err = answer if isinstance(answer, tuple) \
                    else (0, answer, "")
                if not isinstance(out, bytes):
                    out = (out if isinstance(out, str)
                           else json.dumps(out)).encode()
                return rc, out, err
        return 1, b"", f"unexpected {key}"


def qtile_answers(**extra):
    q = "qtile cmd-obj"
    answers = {f"{q} -o root -f get_groups": GROUPS,
               f"{q} -o root -f windows": WINDOWS,
               f"{q} -o group -f info": GROUPS["1"],
               f"{q} -o root -f get_screens": SCREENS,
               f"{q} -o screen -f info": {"index": 1},
               f"{q} -o window -f info": {**WINDOWS[0], "x": 10, "y": 20,
                                          "width": 800, "height": 600},
               f"{q} -o window 21 -f info": {**WINDOWS[2], "x": 1920,
                                             "y": 0, "width": 1280,
                                             "height": 1440},
               f"{q} -o window 99 -f info": (1, b"", "No object window 99"),
               f"{q} -o group 2 -f toscreen": "",
               f"{q} -o window 21 -f focus": "",
               f"{q} -o cmd -f shutdown": "",
               "import ": b"\x89PNG fake",
               "xclip -selection primary -o": "highlighted",
               "xclip -selection clipboard -o": (1, b"", ""),
               "xrandr --query": XRANDR,
               "xrandr --listmonitors": MONITORS,
               "xrandr --output": ""}
    answers.update(extra)
    return answers


async def test_x11_pure() -> None:
    from indigoshell.agent import laptop
    section("1. X11 + qtile, pure")
    run = FakeRun(qtile_answers())
    x = x11.X11(run=run)
    use(x)

    spaces = await x.workspaces()
    check("workspaces: qtile's groups in order, the current one shown",
          [(s.name, s.shown) for s in spaces]
          == [("1", True), ("2", False), ("3", False)], str(spaces))
    wins = await x.windows()
    check("windows: id, class (the last WM_CLASS), title, group, the group's "
          "focus as `active`",
          [(w.id, w.app, w.title, w.workspace, w.active) for w in wins]
          == [(11, "kitty", "term", "1", True), (12, "Gedit", "notes", "1", False),
              (21, "firefox", "web", "2", True)], str(wins))
    reply = texts(await laptop._windows(call("windows")))
    check("laptop_windows: the reply as before",
          reply == "workspace 1 (shown)\n  [11] kitty: term\n  [12] Gedit: notes\n"
                   "workspace 2\n  [21] firefox: web\nworkspace 3", repr(reply))
    check("... its description names no qtile", "qtile" not in laptop.WINDOWS.description)

    run.calls.clear()
    reply = texts(await laptop._focus(call("focus", window_id=21)))
    check("laptop_focus a window: info, its group to the screen, focus",
          run.calls == [["qtile", "cmd-obj", "-o", "window", "21", "-f", "info"],
                        ["qtile", "cmd-obj", "-o", "group", "2", "-f", "toscreen"],
                        ["qtile", "cmd-obj", "-o", "window", "21", "-f", "focus"]]
          and reply == "Focused web, on workspace 2.", f"{run.calls} {reply!r}")
    run.calls.clear()
    reply = texts(await laptop._focus(call("focus", workspace="2")))
    check("laptop_focus a workspace: toscreen",
          run.calls == [["qtile", "cmd-obj", "-o", "group", "2", "-f", "toscreen"]]
          and reply == "Workspace 2 is shown.", f"{run.calls} {reply!r}")
    from indigoshell.core.hands import HandError
    try:
        await laptop._focus(call("focus", window_id=99))
        said = None
    except HandError as e:
        said = str(e)
    check("... a window that isn't there: qtile's words", said
          == "can't focus it: No object window 99", repr(said))

    async def shot(**kw):
        run.calls.clear()
        try:
            s = await x.screenshot(**kw)
            return s.what, run.calls[-1]
        except wm.WMError as e:
            return str(e), None

    imp = ["import", "-silent", "-window", "root"]
    check("screenshot, screen: the root window as PNG",
          await shot() == ("the whole screen", imp + ["png:-"]))
    check("screenshot, the monitor in use (qtile's screen info): cropped",
          await shot(target="monitor") == (
              "monitor 1", imp + ["-crop", "2560x1440+1920+0", "+repage", "png:-"]))
    check("screenshot, monitor 0 by number",
          (await shot(target="monitor", monitor=0))[1]
          == imp + ["-crop", "1920x1080+0+0", "+repage", "png:-"])
    check("screenshot, monitor 5: there is none, said as before",
          (await shot(target="monitor", monitor=5))[0]
          == "no monitor 5; there are 2 (0 to 1)")
    check("screenshot, window 21: cropped to it, named",
          await shot(target="window", window=21) == (
              "window 21 (firefox: web)",
              imp + ["-crop", "1280x1440+1920+0", "+repage", "png:-"]))
    check("screenshot, the focused window (no id)",
          (await shot(target="window"))[0] == "window 11 (kitty: term)")
    check("screenshot, window 99: can't find what to capture",
          (await shot(target="window", window=99))[0]
          == "can't find what to capture: No object window 99")
    bad = x11.X11(run=FakeRun(qtile_answers(**{"import ": (1, b"", "no display")})))
    try:
        await bad.screenshot()
        said = None
    except wm.WMError as e:
        said = str(e)
    check("screenshot failing: the screenshot failed, and why",
          said == "the screenshot failed: no display", repr(said))

    served = []
    real = x11.serve
    x11.serve = lambda argv, value, what: served.append((argv, value))
    try:
        ok = x.copy("hello", "primary") and x.copy("there")
        empty = x.copy("")
    finally:
        x11.serve = real
    check("copy: xclip -in on the selection, served in the background",
          ok and not empty and served == [
              (["xclip", "-selection", "primary", "-in"], "hello"),
              (["xclip", "-selection", "clipboard", "-in"], "there")], str(served))
    check("paste: xclip -o", await x.paste("primary") == "highlighted")
    try:
        await x.paste()
        said = None
    except wm.WMError as e:
        said = str(e)
    check("paste of nothing: the clipboard is empty or not text",
          said == "the clipboard is empty or not text", repr(said))
    reply = texts(await laptop._read_clipboard(call("read_clipboard",
                                                    selection="primary")))
    check("laptop_read_clipboard through it", reply == "highlighted", repr(reply))

    outs = await x.outputs()
    check("xrandr: outputs, connection, primary, lit, modes, the current one",
          [(o.name, o.connected, o.primary, o.active, o.current) for o in outs] == [
              ("eDP-1", True, False, False, None),
              ("HDMI-1", True, True, True, (2560, 1440, 143.97)),
              ("DP-1", False, False, False, None),
              ("DP-2", False, False, True, None)]
          and outs[0].modes == ((1920, 1080, 60.02), (1920, 1080, 59.93),
                                (1920, 1080, 48.0), (1680, 1050, 59.88))
          and (1920, 1080, 50.0) in outs[1].modes, str(outs))
    check("use alone: one xrandr call, primary at the mode, the rest off",
          x11.use_only_argv("eDP-1", wm.Mode(1920, 1080, 60.02), ["HDMI-1", "DP-2"])
          == ["xrandr", "--output", "eDP-1", "--primary", "--mode", "1920x1080",
              "--rate", "60.02", "--output", "HDMI-1", "--off",
              "--output", "DP-2", "--off"])
    check("... with no mode table: --auto",
          x11.use_only_argv("eDP-1", None, [])
          == ["xrandr", "--output", "eDP-1", "--primary", "--auto"])
    check("RandR monitors by name (braindance marks its session)",
          await x.virtual_monitors() == ["HDMI-1", "braindance"])

    from indigoshell.plugins import display
    held = [o for o in outs if o.name != "DP-2"]
    check("display: HDMI-1 alone, primary, at its best mode: in sole use",
          display._is_sole(held, "HDMI-1") and not display._is_sole(held, "eDP-1"))
    check("... not while an unplugged DP-2 still holds a place",
          not display._is_sole(outs, "HDMI-1"))
    state = {}
    real_set = display.state.set
    display.state.set = lambda k, v: state.update({k: v})
    run.calls.clear()
    try:
        await display._use_only("eDP-1")
    finally:
        display.state.set = real_set
    check("display: use eDP-1 alone: the xrandr call as before, remembered",
          run.calls[-1] == ["xrandr", "--output", "eDP-1", "--primary", "--mode",
                            "1920x1080", "--rate", "60.02", "--output", "HDMI-1",
                            "--off", "--output", "DP-2", "--off"]
          and state == {"display.output": "eDP-1"}, str(run.calls[-1:]))
    check("display: braindance running (its RandR monitor)",
          await display._braindance_running())

    from indigoshell.plugins import system
    real_active = system._active_layout

    async def active():
        return "tr"
    system._active_layout = active
    try:
        items = await system._layout_items()
    finally:
        system._active_layout = real_active
    check("layout menu on X: setxkbmap's, as before",
          [(i.label, list(i.run), i.active) for i in items]
          == [("US", ["setxkbmap", "us", "-option", "caps:escape"], False),
              ("TR", ["setxkbmap", "tr", "-option", "caps:escape"], True)], str(items))

    run.calls.clear()
    x.logout()
    await asyncio.sleep(0.05)
    check("logout: qtile's shutdown",
          run.calls == [["qtile", "cmd-obj", "-o", "cmd", "-f", "shutdown"]],
          str(run.calls))


# ── 2. niri, pure ───────────────────────────────────────────────────────
# What `niri msg` 26.04 writes to the socket for these commands, recorded
# from it (a stand-in socket printing what it was sent).
NIRI_MSG = {
    "action focus-workspace 2": {"Action": {"FocusWorkspace": {"reference": {"Index": 2}}}},
    "action focus-window --id 7": {"Action": {"FocusWindow": {"id": 7}}},
    "action quit --skip-confirmation": {"Action": {"Quit": {"skip_confirmation": True}}},
    "output winit on": {"Output": {"output": "winit", "action": "On"}},
    "output winit off": {"Output": {"output": "winit", "action": "Off"}},
    "output winit mode 1920x1080@59.951": {"Output": {"output": "winit", "action": {
        "Mode": {"mode": {"Specific": {"width": 1920, "height": 1080, "refresh": 59.951}}}}}},
    "output winit mode auto": {"Output": {"output": "winit", "action": {
        "Mode": {"mode": "Automatic"}}}},
    "action set-window-width --id 5 50%": {"Action": {"SetWindowWidth": {
        "id": 5, "change": {"SetProportion": 50.0}}}},
    "action set-window-height --id 5 55%": {"Action": {"SetWindowHeight": {
        "id": 5, "change": {"SetProportion": 55.0}}}},
    "action reset-window-height --id 5": {"Action": {"ResetWindowHeight": {"id": 5}}},
    "action consume-or-expel-window-left --id 5": {"Action": {
        "ConsumeOrExpelWindowLeft": {"id": 5}}},
    "action switch-layout 1": {"Action": {"SwitchLayout": {"layout": {"Index": 1}}}},
}


def layout(col=None, row=1, floating_at=None, size=(600, 400)):
    return {"pos_in_scrolling_layout": None if col is None else [col, row],
            "tile_size": [float(size[0]), float(size[1])],
            "window_size": list(size),
            "tile_pos_in_workspace_view": floating_at,
            "window_offset_in_tile": [0.0, 0.0]}


def ws(id, idx, output="eDP-1", name=None, active=False, focused=False,
       urgent=False, window=None):
    return {"id": id, "idx": idx, "name": name, "output": output,
            "is_urgent": urgent, "is_active": active, "is_focused": focused,
            "active_window_id": window}


def win(id, ws_id, app="kitty", title="", focused=False, col=None, row=1,
        floating_at=None, urgent=False, size=(600, 400)):
    return {"id": id, "title": title, "app_id": app, "pid": 100 + id,
            "workspace_id": ws_id, "is_focused": focused,
            "is_floating": floating_at is not None, "is_urgent": urgent,
            "layout": layout(col, row, floating_at, size),
            "focus_timestamp": None}


def output(name, x, y, w, h, scale=1.0, on=True):
    return {"name": name, "make": "", "model": "", "serial": None,
            "physical_size": None,
            "modes": [{"width": w, "height": h, "refresh_rate": 60000,
                       "is_preferred": True},
                      {"width": 1280, "height": 720, "refresh_rate": 59940,
                       "is_preferred": False}],
            "current_mode": 0 if on else None, "is_custom_mode": False,
            "vrr_supported": False, "vrr_enabled": False,
            "logical": {"x": x, "y": y, "width": w, "height": h, "scale": scale,
                        "transform": "Normal"} if on else None}


ONE = {"Workspaces": [ws(1, 1, name="1", active=True, focused=True, window=3),
                      ws(2, 2, name="2", window=5), ws(3, 3, name="3"),
                      ws(4, 4)],
       "Windows": [win(3, 1, "kitty", "term", True, col=1),
                   win(4, 1, "firefox", "web", col=2),
                   win(5, 2, "gedit", "notes", floating_at=[100.0, 50.0])],
       "Outputs": {"eDP-1": output("eDP-1", 0, 0, 1920, 1080)},
       "FocusedOutput": output("eDP-1", 0, 0, 1920, 1080),
       "FocusedWindow": win(3, 1, "kitty", "term", True, col=1),
       "KeyboardLayouts": {"names": ["English (US)", "Turkish"], "current_idx": 0}}


class FakeNiri:
    """A stand-in niri socket (an abstract unix socket): answers from
    `state` as niri does, records every request, streams `events`."""

    def __init__(self, state, events=()) -> None:
        self.state = state
        self.events = list(events)
        self.requests: list = []
        self.path = f"\0indigoshell-wm-test-{os.getpid()}-{id(self)}"
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self._serve, self.path)
        return self

    async def __aexit__(self, *_):
        self.server.close()

    async def _serve(self, reader, writer):
        line = await reader.readline()
        req = json.loads(line)
        self.requests.append(req)
        if req == "EventStream":
            writer.write(b'{"Ok":"Handled"}\n')
            for e in self.events:
                writer.write(json.dumps(e).encode() + b"\n")
            await writer.drain()
            with contextlib.suppress(Exception):
                await reader.read()            # until the client hangs up
        elif isinstance(req, str):
            ok = {req: self.state.get(req)} if req in self.state else None
            writer.write(json.dumps({"Ok": ok} if ok else
                                    {"Err": "error parsing request"}).encode() + b"\n")
        elif "Output" in req:
            known = req["Output"]["output"] in self.state["Outputs"]
            writer.write(json.dumps({"Ok": {"OutputConfigChanged":
                                            "Applied" if known else "OutputWasMissing"}}
                                    ).encode() + b"\n")
        else:
            writer.write(b'{"Ok":"Handled"}\n')
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()


def frame(w, h, rgb=(10, 20, 30), name="eDP-1", transform=0, y_invert=False,
          paint=None):
    """A wl_shm XRGB8888 frame (bytes B, G, R, X)."""
    px = bytearray(bytes((rgb[2], rgb[1], rgb[0], 255)) * (w * h))
    for (x, y), c in (paint or {}).items():
        px[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes((c[2], c[1], c[0], 255))
    return wayland.Frame(name, w, h, w * 4, wayland.XRGB8888, bytes(px),
                         y_invert, transform)


def pixel(image, x, y):
    import skia
    c = image.getColor(x, y) if hasattr(image, "getColor") else None
    if c is None:
        bm = skia.Bitmap()
        bm.allocPixels(skia.ImageInfo.MakeN32Premul(image.width(), image.height()))
        image.readPixels(bm.info(), bm.getPixels(), bm.rowBytes(), 0, 0)
        c = bm.getColor(x, y)
    return (skia.ColorGetR(c), skia.ColorGetG(c), skia.ColorGetB(c))


def decoded(png: bytes):
    import skia
    return skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))


async def test_niri_pure() -> None:
    from indigoshell.agent import laptop
    section("2. niri, pure")
    sent = {}
    async with FakeNiri(ONE) as fake:
        n = nirimod.Niri(fake.path)
        use(n)
        await n.focus_window(4)
        await n.focus_workspace("2")
        await n.use_only("eDP-1", wm.Mode(1920, 1080, 59.951), ["HDMI-A-1"])
        await n.use_only("eDP-1", None, [])
        n.logout()
        await asyncio.sleep(0.05)
        sent = [r for r in fake.requests if not isinstance(r, str)]
    check("focus a window: FocusWindow by id, as niri msg sends it",
          sent[0] == {"Action": {"FocusWindow": {"id": 4}}}
          and NIRI_MSG["action focus-window --id 7"]["Action"].keys() == sent[0]["Action"].keys())
    check("focus workspace \"2\": FocusWorkspace by its id",
          sent[1] == {"Action": {"FocusWorkspace": {"reference": {"Id": 2}}}}
          and NIRI_MSG["action focus-workspace 2"]["Action"]["FocusWorkspace"].keys()
          == sent[1]["Action"]["FocusWorkspace"].keys(), str(sent[1]))
    mode_shape = NIRI_MSG["output winit mode 1920x1080@59.951"]["Output"]["action"]
    check("use alone: on, the mode (niri msg's shape), then the others off",
          sent[2:5] == [{"Output": {"output": "eDP-1", "action": "On"}},
                        {"Output": {"output": "eDP-1", "action": mode_shape}},
                        {"Output": {"output": "HDMI-A-1", "action": "Off"}}], str(sent[2:5]))
    check("... with no mode: Automatic",
          sent[6] == {"Output": {"output": "eDP-1", "action":
                                 NIRI_MSG["output winit mode auto"]["Output"]["action"]}},
          str(sent[5:7]))
    check("log out: Quit, skipping niri's confirmation (niri msg's shape)",
          sent[-1] == NIRI_MSG["action quit --skip-confirmation"], str(sent[-1]))

    # X's pick (its HDMI-0 is niri's HDMI-A-1) is not niri's: niri's
    # screen is left as it is, with niri's own pick remembered apart.
    from indigoshell.plugins import display
    big, panel = wm.Mode(2560, 1440, 59.951), wm.Mode(1920, 1080, 300.009)
    outs = [wm.Output("HDMI-A-1", connected=True, active=True, primary=False,
                      modes=(big,), current=big),
            wm.Output("eDP-1", connected=True, active=True, primary=False,
                      modes=(panel,), current=panel)]
    saved = {"display.output": "HDMI-0"}
    used = []

    class Quiet(nirimod.Niri):
        async def outputs(self):
            return outs

        async def use_only(self, name, mode, off):
            used.append((name, off))
    real_get, real_set = display.state.get, display.state.set
    display.state.get = lambda k, default=None: saved.get(k, default)
    display.state.set = lambda k, v: saved.update({k: v})
    use(Quiet("/nonexistent"))
    try:
        await display.restore()
        left_alone = not used
        await display._use_only("HDMI-A-1")
    finally:
        display.state.get, display.state.set = real_get, real_set
    check("display on niri: X's remembered HDMI-0 leaves both outputs on",
          left_alone, str(used))
    check("... a pick from the menu is niri's own, X's kept",
          used == [("HDMI-A-1", ["eDP-1"])]
          and saved == {"display.output": "HDMI-0", "display.output.niri": "HDMI-A-1"},
          str((used, saved)))

    async with FakeNiri(ONE) as fake:
        n = nirimod.Niri(fake.path)
        use(n)
        spaces = await n.workspaces()
        wins = await n.windows()
        reply = texts(await laptop._windows(call("windows")))
        try:
            await n.focus_workspace("7")
            said = None
        except wm.NotFound as e:
            said = str(e)
        try:
            await n.use_only("DP-9", None, [])
            missing = None
        except wm.NotFound as e:
            missing = str(e)
    check("workspaces: names (\"4\": the unnamed spare by index), the active one shown",
          [(s.name, s.shown) for s in spaces]
          == [("1", True), ("2", False), ("3", False), ("4", False)], str(spaces))
    check("windows: app id, title, workspace name, the workspace's active one",
          [(w.id, w.app, w.title, w.workspace, w.active, w.classes) for w in wins]
          == [(3, "kitty", "term", "1", True, ("kitty",)),
              (4, "firefox", "web", "1", False, ("firefox",)),
              (5, "gedit", "notes", "2", True, ("gedit",))], str(wins))
    check("laptop_windows on niri",
          reply == "workspace 1 (shown)\n  [3] kitty: term\n  [4] firefox: web\n"
                   "workspace 2\n  [5] gedit: notes\nworkspace 3\nworkspace 4", repr(reply))
    check("a workspace that isn't there: NotFound, naming those that are",
          said == "no workspace '7'; there are 1, 2, 3, 4", repr(said))
    check("an output that isn't there: NotFound", missing == "no output DP-9", repr(missing))

    two = [ws(1, 1, "eDP-1", active=True, focused=True), ws(2, 2, "eDP-1"),
           ws(7, 1, "HDMI-A-1", active=True), ws(8, 2, "HDMI-A-1", name="mail")]
    from indigoshell.plugins import system
    async with FakeNiri(ONE) as fake:
        use(nirimod.Niri(fake.path))
        items = await system._layout_items()
        await items[1].run()
        switched = fake.requests[-1]
    check("layout menu on niri: niri's own layouts, the one in use lit",
          [(i.label, i.active) for i in items]
          == [("ENGLISH (US)", True), ("TURKISH", False)], str(items))
    check("... picking one: SwitchLayout by index (niri msg's shape)",
          switched == NIRI_MSG["action switch-layout 1"], str(switched))

    check("two monitors: unnamed workspaces named monitor:index",
          [s.name for s in nirimod.workspace_list(two, ["HDMI-A-1", "eDP-1"])]
          == ["HDMI-A-1:1", "mail", "eDP-1:1", "eDP-1:2"])

    named = ([ws(i, i, name=str(i), active=i == 3) for i in range(1, 10)]
             + [ws(10, 10)])
    wins9 = [win(20, 3), win(21, 3), win(22, 5, urgent=True)]
    named[4]["is_urgent"] = True
    st = nirimod.bar_state(named, wins9, "eDP-1")
    check("bar: named 1-9, the spare empty one left out, current, windows, urgent",
          (st.count, st.current, st.windows[2], st.windows[4], st.urgent[4], sum(st.urgent))
          == (9, 2, 2, 1, True, 1), str(st))
    check("bar: the spare stays while it is shown or holds a window",
          nirimod.bar_state(named[:9] + [ws(10, 10, active=True)], [], "eDP-1").count == 10
          and nirimod.bar_state(named, wins9 + [win(23, 10)], "eDP-1").count == 10)
    check("bar: only the bar's own monitor",
          nirimod.bar_state(two, [], "HDMI-A-1").count == 2
          and nirimod.bar_state(two, [], "eDP-1") == wm.WorkspaceState(1, 0, (0,), (False,)))

    # The state kept from events, against what the requests answer.
    events = [{"WorkspacesChanged": {"workspaces": [dict(w) for w in ONE["Workspaces"]]}},
              {"WindowsChanged": {"windows": [json.loads(json.dumps(w)) for w in ONE["Windows"]]}},
              {"KeyboardLayoutsChanged": {"keyboard_layouts": {"names": ["English (US)"],
                                                               "current_idx": 0}}},
              {"WindowOpenedOrChanged": {"window": win(6, 1, "foot", "new", True, col=3)}},
              {"WorkspaceActiveWindowChanged": {"workspace_id": 1, "active_window_id": 6}},
              {"WindowLayoutsChanged": {"changes": [[4, layout(2, 2)]]}},
              {"WorkspaceActivated": {"id": 3, "focused": True}},
              {"WindowFocusChanged": {"id": None}},
              {"WindowClosed": {"id": 4}},
              {"WorkspaceUrgencyChanged": {"id": 2, "urgent": True}},
              {"WindowUrgencyChanged": {"id": 5, "urgent": True}}]
    state = nirimod.State()
    applied = [state.apply(e) for e in events]
    check("events: what each says, kept (unknown kinds ignored)",
          applied == [True, True, False] + [True] * 8
          and sorted(state.windows) == [3, 5, 6]
          and state.workspaces[1]["active_window_id"] == 6
          and [w["id"] for w in state.workspaces.values() if w["is_focused"]] == [3]
          and [w["id"] for w in state.workspaces.values() if w["is_active"]] == [3]
          and not any(w["is_focused"] for w in state.windows.values())
          and state.workspaces[2]["is_urgent"] and state.windows[5]["is_urgent"],
          json.dumps(state.workspaces)[:300])

    outs = nirimod.output_list({"HDMI-A-1": output("HDMI-A-1", 1920, 0, 2560, 1440),
                                "eDP-1": output("eDP-1", 0, 0, 1920, 1080),
                                "DP-3": output("DP-3", 0, 0, 1920, 1080, on=False)})
    check("outputs: left to right, the dark one last; no primary; Hz from mHz",
          [(o.name, o.active, o.primary, o.current) for o in outs]
          == [("eDP-1", True, False, (1920, 1080, 60.0)),
              ("HDMI-A-1", True, False, (2560, 1440, 60.0)), ("DP-3", False, False, None)]
          and outs[0].modes[1] == (1280, 720, 59.94), str(outs))
    from indigoshell.plugins import display
    alone = nirimod.output_list({"eDP-1": output("eDP-1", 0, 0, 1920, 1080),
                                 "DP-3": output("DP-3", 0, 0, 1920, 1080, on=False)})
    check("display: in sole use without a primary (niri has none)",
          display._is_sole(alone, "eDP-1") and not display._is_sole(alone, "DP-3"))

    # Screenshots, with frames standing in for screencopy's.
    shots = {"eDP-1": frame(1920, 1080, paint={(100, 50): (255, 0, 0),
                                               (699, 449): (0, 255, 0)}),
             "HDMI-A-1": frame(2560, 1440, (200, 200, 200), "HDMI-A-1")}
    asked = []

    async def capture(names=None, timeout=10.0):
        asked.append(names)
        return [shots[n] for n in names]

    layouts = dict(ONE, Outputs={"eDP-1": output("eDP-1", 0, 0, 1920, 1080),
                                 "HDMI-A-1": output("HDMI-A-1", 1920, 0, 2560, 1440)},
                   Windows=[win(3, 1, "kitty", "term", True, col=1),
                            win(5, 1, "gedit", "notes", floating_at=[100.0, 50.0]),
                            win(8, 2, "mpv", "film", col=1)])
    async with FakeNiri(layouts) as fake:
        n = nirimod.Niri(fake.path, capture=capture)
        cut = await n.screenshot("window", window=5)
        tiled = await n.screenshot("window")
        try:
            await n.screenshot("window", window=8)
            hidden = None
        except wm.NotFound as e:
            hidden = str(e)
        mon = await n.screenshot("monitor")
        mon1 = await n.screenshot("monitor", monitor=1)
        try:
            await n.screenshot("monitor", monitor=4)
            nomon = None
        except wm.NotFound as e:
            nomon = str(e)
        screen = await n.screenshot()
    img = decoded(cut.png)
    check("a floating window: cut out of its monitor where niri says it is",
          (img.width(), img.height()) == (600, 400) and pixel(img, 0, 0) == (255, 0, 0)
          and pixel(img, 599, 399) == (0, 255, 0) and cut.what == "window 5 (gedit: notes)",
          f"{img.width()}x{img.height()} {pixel(img, 0, 0)} {cut.what}")
    check("a tiled window (the focused one): its whole monitor, and the reply says why",
          decoded(tiled.png).width() == 1920
          and tiled.what.startswith("monitor 0 (eDP-1), the one window 3 (kitty: term) is on")
          and "doesn't say where a tiled window sits" in tiled.what, tiled.what)
    check("a window on a workspace not shown: NotFound, saying so",
          hidden == "window 8 (mpv: film) is on workspace 2, which isn't shown; "
                    "bring it forward first", repr(hidden))
    check("monitors: the focused one by default, by number, none past the last",
          mon.what == "monitor 0 (eDP-1)" and decoded(mon1.png).width() == 2560
          and nomon == "no monitor 4; there are 2 (0 to 1)", f"{mon.what} {nomon}")
    sc = decoded(screen.png)
    check("the whole screen: both monitors where they sit",
          (sc.width(), sc.height()) == (4480, 1440) and pixel(sc, 100, 50) == (255, 0, 0)
          and pixel(sc, 3000, 100) == (200, 200, 200) and pixel(sc, 100, 1300) == (0, 0, 0)
          and asked[-1] == ["eDP-1", "HDMI-A-1"], f"{sc.width()}x{sc.height()}")

    # The bar widget on niri's event stream.
    from indigoshell.widgets.workspaces import Workspaces

    class Bar:
        x, y, width, height = 0, 0, 1920, 30

        def damage(self, *_a, **_k):
            pass

    stream = [{"WorkspacesChanged": {"workspaces": named}},
              {"WindowsChanged": {"windows": wins9}},
              {"WorkspaceActivated": {"id": 5, "focused": True}}]
    async with FakeNiri(dict(ONE, Outputs={"eDP-1": output("eDP-1", 0, 0, 1920, 1080)}),
                        stream) as fake:
        use(nirimod.Niri(fake.path))
        bar = Workspaces()
        bar.attach(Bar())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if bar._current == 4:
                break
        seen = (bar._count, bar._current, bar._per_desktop[:5], bar._urgent[4])
        bar._switch(7)
        await asyncio.sleep(0.05)
        bar.detach()
        follower_gone = bar._follower is None
        asked_focus = [r for r in fake.requests if isinstance(r, dict)]
    check("the bar on niri: nine workspaces, the fifth shown, windows, urgency",
          seen == (9, 4, [0, 0, 2, 0, 1], True), str(seen))
    check("... a click on the eighth: FocusWorkspace by its id; detach stops it",
          asked_focus == [{"Action": {"FocusWorkspace": {"reference": {"Id": 8}}}}]
          and follower_gone, str(asked_focus))


# ── 3. Wayland, pure ────────────────────────────────────────────────────
def test_wayland_pure() -> None:
    section("3. Wayland, pure")
    body, fds = wayland._pack("usuh", [7, "wl_shm", 1, 42])
    check("pack: uint, a string NUL-ended and padded to 4, an fd beside",
          body == struct.pack("<I", 7) + struct.pack("<I", 7) + b"wl_shm\0\0"
          + struct.pack("<I", 1) and fds == [42], repr(body))
    check("unpack: back again", wayland._unpack("usu", body) == [7, "wl_shm", 1])
    check("unpack: an int, negative", wayland._unpack("i", struct.pack("<i", -3)) == [-3])
    red = {(0, 0): (255, 0, 0)}                 # top-left of a 4x2 buffer
    cases = {0: ((4, 2), (0, 0)), 1: ((2, 4), (1, 0)), 2: ((4, 2), (3, 1)),
             3: ((2, 4), (0, 3)), 4: ((4, 2), (3, 0)), 6: ((4, 2), (0, 1))}
    for t, (size, at) in cases.items():
        img = wayland.upright(frame(4, 2, (0, 0, 0), transform=t, paint=red))
        check(f"upright, transform {t}: {size[0]}x{size[1]}, the corner at {at}",
              (img.width(), img.height()) == size and pixel(img, *at) == (255, 0, 0),
              f"{img.width()}x{img.height()}")
    img = wayland.upright(frame(4, 2, (0, 0, 0), y_invert=True, paint=red))
    check("upright, y-inverted: flipped top to bottom", pixel(img, 0, 1) == (255, 0, 0))


# ── 4. tiling, pure ─────────────────────────────────────────────────────
def test_tiling_pure() -> None:
    section("4. tiling, pure")
    lay = tiling.Layout()
    W = 50.0

    def widths(reqs):
        return sorted((r["Action"]["SetWindowWidth"]["id"],
                       r["Action"]["SetWindowWidth"]["change"]["SetProportion"])
                      for r in reqs if "SetWindowWidth" in r["Action"])

    check("what is there at the start is left as it is",
          lay.decide([win(1, 1, col=1), win(2, 1, col=2), win(3, 1, col=3)]) == ([], True))
    lay = tiling.Layout()
    lay.decide([])
    reqs, done = lay.decide([win(1, 1, col=1)])
    check("one window: its column the whole screen", widths(reqs) == [(1, 100.0)] and done)
    reqs, done = lay.decide([win(1, 1, col=1), win(2, 1, col=2)])
    check("two: half and half", widths(reqs) == [(1, W), (2, W)] and done)
    reqs, done = lay.decide([win(1, 1, col=1), win(3, 1, col=2), win(2, 1, col=3)])
    check("a third, as a column right of the focused one: into that column",
          reqs == [{"Action": {"ConsumeOrExpelWindowLeft": {"id": 3}}}] and not done,
          str(reqs))
    reqs, done = lay.decide([win(1, 1, col=1), win(3, 1, col=1, row=2), win(2, 1, col=2)])
    check("... then two columns again: no widths touched", reqs == [] and done, str(reqs))
    reqs, done = lay.decide([win(4, 1, col=1), win(1, 1, col=2), win(3, 1, col=2, row=2),
                             win(2, 1, col=3)])
    check("a new window at the far left: into the column on its right",
          reqs == [{"Action": {"ConsumeOrExpelWindowRight": {"id": 4}}}], str(reqs))
    lay.decide([win(1, 1, col=1), win(4, 1, col=1, row=2), win(3, 1, col=1, row=3),
                win(2, 1, col=2)])
    reqs, _ = lay.decide([win(1, 1, col=1), win(4, 1, col=1, row=2), win(3, 1, col=1, row=3),
                          win(2, 1, col=2), win(9, 1, floating_at=[0.0, 0.0])])
    check("a floating window: left alone", reqs == [], str(reqs))
    reqs, _ = lay.decide([win(1, 1, col=1), win(2, 1, col=2)])
    check("windows closing in a column: still two columns, widths kept", reqs == [])
    reqs, _ = lay.decide([win(1, 1, col=1)])
    check("its column gone: the other back to the whole screen",
          widths(reqs) == [(1, 100.0)], str(reqs))
    reqs, _ = lay.decide([win(8, 1, col=1)])
    check("one column gone and a new one in its place between two looks: "
          "the whole screen", widths(reqs) == [(8, 100.0)], str(reqs))
    reqs, _ = lay.decide([win(1, 1, col=1)])
    reqs, _ = lay.decide([win(1, 1, col=1), win(5, 2, col=1)])
    check("another workspace's first window: its own count",
          widths(reqs) == [(5, 100.0)], str(reqs))
    reqs, done = lay.decide([win(1, 1, col=1), win(6, 1, col=2), win(7, 1, col=3),
                             win(5, 2, col=1)])
    check("two at once on one column: the right-most goes in first",
          reqs == [{"Action": {"ConsumeOrExpelWindowLeft": {"id": 7}}}] and not done)
    reqs, done = lay.decide([win(1, 1, col=1), win(6, 1, col=2), win(7, 1, col=2, row=2),
                             win(5, 2, col=1)])
    check("... and then the columns are halved once", widths(reqs) == [(1, W), (6, W)]
          and done, str(reqs))
    reqs, _ = lay.decide([win(1, 1, col=1), win(6, 1, col=2), win(7, 1, col=3),
                          win(5, 2, col=1)])
    check("three columns made by hand (no new window): thirds",
          widths(reqs) == [(1, 100 / 3), (6, 100 / 3), (7, 100 / 3)], str(reqs))
    check("the requests are niri msg's shapes",
          tiling.width_requests({1: [win(5, 1, col=1)], 2: [win(6, 1, col=2)]})[0]
          == NIRI_MSG["action set-window-width --id 5 50%"])

    # grow: qtile's grow_left/right/up/down on Mod+Ctrl+H/L/K/J.
    def sets(reqs):
        out = []
        for r in reqs:
            (name, f), = r["Action"].items()
            out.append((name, f["id"], (f.get("change") or {}).get("SetProportion")))
        return out

    def grow(wins, wid, direction):
        return sets(tiling.grow_requests(wins, wid, direction))
    two = [win(1, 1, col=1, size=(1268, 1340)), win(2, 1, col=2, size=(1268, 1340))]
    right = [("SetWindowWidth", 1, 55.0), ("SetWindowWidth", 2, 45.0)]
    left = [("SetWindowWidth", 1, 45.0), ("SetWindowWidth", 2, 55.0)]
    check("grow right on the left column: the split moves right", grow(two, 1, "right") == right,
          str(grow(two, 1, "right")))
    check("... and on the right column too: right always moves it right",
          grow(two, 2, "right") == right, str(grow(two, 2, "right")))
    check("grow left on either column: the split moves left",
          grow(two, 1, "left") == left and grow(two, 2, "left") == left)
    off_grid = [win(1, 1, col=1, size=(1523, 1340)), win(2, 1, col=2, size=(1013, 1340))]
    check("a split off the 5% steps lands on one",
          grow(off_grid, 1, "right") == [("SetWindowWidth", 1, 65.0), ("SetWindowWidth", 2, 35.0)],
          str(grow(off_grid, 1, "right")))
    edge = [win(1, 1, col=1, size=(2700, 1340)), win(2, 1, col=2, size=(300, 1340))]
    check("the split stops with 10% left", grow(edge, 1, "right") == [], str(grow(edge, 1, "right")))
    three = [win(1, 1, col=1), win(2, 1, col=2), win(3, 1, col=3)]
    got = grow(three, 2, "right")
    check("three columns: only the focused one and its right neighbour change",
          [(n, i, round(v, 2)) for n, i, v in got]
          == [("SetWindowWidth", 2, 40.0), ("SetWindowWidth", 3, 26.67)], str(got))
    check("a lone column, a floating window: nothing",
          grow([win(1, 1, col=1)], 1, "left") == []
          and grow([win(9, 1, floating_at=[0.0, 0.0])], 9, "right") == [])
    stack = [win(6, 1, col=1, size=(1268, 666)), win(5, 1, col=1, row=2, size=(1268, 666)),
             win(2, 1, col=2, size=(1268, 1340))]
    check("grow down: the focused window taller, the others sharing the rest",
          grow(stack, 5, "down") == [("ResetWindowHeight", 6, None), ("SetWindowHeight", 5, 55.0)],
          str(grow(stack, 5, "down")))
    check("grow up: shorter", grow(stack, 5, "up")[-1] == ("SetWindowHeight", 5, 45.0))
    check("a window alone in its column: no height change",
          grow(stack, 2, "down") == [] and grow(stack, 2, "up") == [])
    reqs = tiling.grow_requests(stack, 5, "down")
    check("... in niri msg's shapes",
          reqs[-1] == NIRI_MSG["action set-window-height --id 5 55%"]
          and reqs[0]["Action"].keys() == NIRI_MSG["action reset-window-height --id 5"]["Action"].keys(),
          str(reqs))


# ── 5. X11 + qtile, live and read-only ──────────────────────────────────
def qtile_raw(*args):
    out = subprocess.run(["qtile", "cmd-obj", *args], capture_output=True,
                         text=True, timeout=10)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


async def test_x11_live() -> None:
    from indigoshell.agent import laptop
    section("5. X11 + qtile, live and read-only")
    if os.environ.get("NIRI_SOCKET") or not os.environ.get("DISPLAY") \
            or qtile_raw("-o", "root", "-f", "get_groups") is None:
        print("  (skipped: no X session with qtile here)")
        return
    x = x11.X11()
    use(x)
    groups = qtile_raw("-o", "root", "-f", "get_groups")
    wins = qtile_raw("-o", "root", "-f", "windows")
    current = qtile_raw("-o", "group", "-f", "info")
    # The reply as the hand always built it, from qtile's own answers.
    by = {}
    for w in wins:
        by.setdefault(str(w.get("group")), []).append(w)
    lines = []
    for name in groups:
        lines.append(f"workspace {name}" + (" (shown)" if current.get("name") == name else ""))
        for w in by.get(name, []):
            lines.append(f"  [{w['id']}] {(w.get('wm_class') or ['?'])[-1]}: {w.get('name', '')}")
    reply = texts(await laptop._windows(call("windows")))
    check(f"laptop_windows: qtile's {len(groups)} groups and {len(wins)} windows, "
          "as the hand put them before", reply == "\n".join(lines))
    mine = await x.windows()
    check("windows: each group's focus is its active window",
          [w.active for w in mine]
          == [(groups.get(str(w.get("group"))) or {}).get("focus") == w.get("name")
              for w in wins])

    # The bar's readout, against EWMH read on a connection of this test's.
    import xcffib
    import xcffib.xproto as xp
    from indigoshell.backend.x11 import Display
    from indigoshell.widgets.workspaces import Workspaces
    conn = xcffib.connect()
    root = conn.get_setup().roots[0].root

    def atom(name):
        return conn.core.InternAtom(False, len(name), name).reply().atom

    def ints(wid, name, n=512):
        r = conn.core.GetProperty(False, wid, atom(name), xp.GetPropertyType.Any,
                                  0, n).reply()
        raw = bytes(r.value.buf())
        return list(struct.unpack(f"<{len(raw) // 4}I", raw)) if r.format == 32 else []

    count = (ints(root, b"_NET_NUMBER_OF_DESKTOPS") or [0])[0]
    cur = (ints(root, b"_NET_CURRENT_DESKTOP") or [0])[0]
    per, urgent = [0] * count, [False] * count
    demands = atom(b"_NET_WM_STATE_DEMANDS_ATTENTION")
    for xid in ints(root, b"_NET_CLIENT_LIST"):
        desk = ints(xid, b"_NET_WM_DESKTOP", 1)
        if desk and 0 <= desk[0] < count:
            per[desk[0]] += 1
            urgent[desk[0]] |= demands in ints(xid, b"_NET_WM_STATE", 32)
    conn.disconnect()
    d = Display()

    class Bar:
        display = d

        def damage(self, *_a, **_k):
            pass

    bar = Workspaces()
    bar.attach(Bar())
    seen = (bar._count, bar._current, bar._per_desktop, bar._urgent)
    bar.detach()
    d.close()
    check(f"the bar's readout: {count} workspaces, the current one, windows on "
          "each, urgency, as EWMH says", seen == (count, cur, per, urgent),
          f"{seen} vs {(count, cur, per, urgent)}")

    raw = subprocess.run(["xrandr", "--query"], capture_output=True, text=True).stdout
    outs = await x.outputs()
    names = re.findall(r"^(\S+) (?:connected|disconnected)", raw, re.M)
    primary = re.findall(r"^(\S+) connected primary", raw, re.M)
    stars = len(re.findall(r"\d\*", raw))
    check(f"outputs: xrandr's {len(names)}, the primary, the modes in use",
          [o.name for o in outs] == names
          and [o.name for o in outs if o.primary] == primary
          and sum(o.current is not None for o in outs) == stars)
    raw = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True).stdout
    check("RandR monitors: xrandr's", len(await x.virtual_monitors())
          == len(re.findall(r"^\s*\d+: ", raw, re.M)))


# ── 6. a nested niri ────────────────────────────────────────────────────
QTILE_RULE = ("from libqtile.command.client import InteractiveCommandClient as C; "
              "import sys; c = C(); "
              "print(c.add_rule({'wm_class': 'niri'}, {'group': '8'}) "
              "if sys.argv[1] == 'add' else c.remove_rule(int(sys.argv[1])))")

NIRI_CONFIG = """\
// The user's niri settings that bear on the layout, and nothing to start.
input {{
    keyboard {{
        xkb {{
            layout "us,tr"
        }}
    }}
    focus-follows-mouse max-scroll-amount="0%"
}}
layout {{
    gaps 8
    center-focused-column "never"
    default-column-width {{ proportion 0.5; }}
    focus-ring {{
        off
    }}
    border {{
        width 3
    }}
}}
animations {{
    off
}}
prefer-no-csd
hotkey-overlay {{
    skip-at-startup
}}
screenshot-path "{shots}/niri-%Y%m%d-%H%M%S.png"
{workspaces}
xwayland-satellite {{
    off
}}
"""


class Nested:
    """A nested niri on qtile group 8 and what runs in it."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wm-niri-")
        self.run = os.path.join(self.tmp, "run")
        self.shots = os.path.join(self.tmp, "shots")
        os.makedirs(self.run, mode=0o700)
        os.makedirs(self.shots)
        self.rule = None
        self.niri = None
        self.procs: list[subprocess.Popen] = []
        self.env = None
        self.user_env = dict(os.environ)

    def qtile_rule(self, arg) -> str:
        return subprocess.run(["python3", "-c", QTILE_RULE, str(arg)], env=self.user_env,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def where(self) -> str | None:
        """qtile's group of our niri's X window, found by its pid."""
        for w in qtile_raw("-o", "root", "-f", "windows") or []:
            out = subprocess.run(["xprop", "-id", str(w["id"]), "_NET_WM_PID"],
                                 capture_output=True, text=True).stdout.split()
            if out and out[-1] == str(self.niri.pid):
                return str(w.get("group"))
        return None

    def start(self) -> bool:
        config = os.path.join(self.tmp, "niri.kdl")
        with open(config, "w") as f:
            f.write(NIRI_CONFIG.format(shots=self.shots, workspaces="\n".join(
                f'workspace "{i}"' for i in range(1, 10))))
        self.rule = self.qtile_rule("add")
        env = {k: v for k, v in os.environ.items()
               if k not in ("WAYLAND_DISPLAY", "NIRI_SOCKET", "WAYLAND_SOCKET")}
        # A unix socket's path must be short; the temp dir's may not be.
        # niri runs in it, and everyone reaches it through /proc/<pid>/cwd.
        env.update(XDG_RUNTIME_DIR="/proc/self/cwd/run",
                   DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.tmp}/no-bus",
                   XDG_DATA_HOME=os.path.join(self.tmp, "data"),
                   XDG_CACHE_HOME=os.path.join(self.tmp, "cache"),
                   XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                   TMPDIR=self.tmp)
        self.niri = subprocess.Popen(["niri", "-c", config], cwd=self.tmp, env=env,
                                     stdout=open(os.path.join(self.tmp, "niri.log"), "w"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        group = None
        for _ in range(100):
            time.sleep(0.1)
            group = self.where()
            if group is not None or self.niri.poll() is not None:
                break
        if not check(f"the nested niri's window is on qtile group 8 (rule {self.rule})",
                     group == "8", f"group {group}"):
            self.niri.kill()
            return False
        sock = None
        for _ in range(50):
            sock = next((n for n in os.listdir(self.run) if n.startswith("niri.")), None)
            if sock and os.path.exists(os.path.join(self.run, "wayland-1")):
                break
            time.sleep(0.1)
        base = f"/proc/{self.niri.pid}/cwd/run"
        wl = next(n for n in os.listdir(self.run)
                  if n.startswith("wayland-") and not n.endswith(".lock"))
        self.env = dict(env, XDG_RUNTIME_DIR=base, WAYLAND_DISPLAY=f"{base}/{wl}",
                        NIRI_SOCKET=f"{base}/{sock}")
        self.env.pop("DISPLAY", None)
        return True

    def spawn(self, argv, log) -> subprocess.Popen:
        p = subprocess.Popen(argv, env=self.env, cwd=self.tmp, start_new_session=True,
                             stdout=open(os.path.join(self.tmp, log), "w"),
                             stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def kitty(self, name) -> subprocess.Popen:
        return self.spawn(["kitty", "--class", f"wmtest.{name}", "-T", f"wm test {name}",
                           "-o", "linux_display_server=wayland", "sh", "-c",
                           f"printf 'nested kitty {name}\\n'; sleep 600"], f"kitty-{name}.log")

    def msg(self, *args):
        out = subprocess.run(["niri", "msg", "--json", *args], env=self.env,
                             capture_output=True, text=True, timeout=10)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGTERM)
        for p in self.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(3)
        if self.niri is not None and self.niri.poll() is None:
            self.niri.kill()
            self.niri.wait(5)
        if self.rule:
            removed = self.qtile_rule(self.rule)
            check(f"our qtile rule {self.rule} removed", removed in ("True", "None", ""),
                  removed)


async def wait_for(test, timeout=8.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = test()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(step)
    return None


def columns_of(nested, ws_id):
    cols = {}
    for w in nested.msg("windows"):
        pos = w["layout"]["pos_in_scrolling_layout"]
        if w["workspace_id"] == ws_id and pos and not w["is_floating"]:
            cols.setdefault(pos[0], []).append((pos[1], w["id"], w["layout"]["tile_size"][0]))
    return [sorted(c) for _, c in sorted(cols.items())]


async def test_niri_live(keep_shots: str | None) -> None:
    from indigoshell.agent import laptop
    section("6. a nested niri (qtile group 8)")
    if shutil.which("niri") is None or shutil.which("kitty") is None:
        print("  (skipped: needs niri and kitty)")
        return
    nested = Nested()
    saved = {k: os.environ.get(k) for k in ("WAYLAND_DISPLAY", "NIRI_SOCKET",
                                            "XDG_RUNTIME_DIR", "DISPLAY")}
    try:
        if not nested.start():
            return
        os.environ.update({k: nested.env[k] for k in
                           ("WAYLAND_DISPLAY", "NIRI_SOCKET", "XDG_RUNTIME_DIR")})
        os.environ.pop("DISPLAY", None)
        wm._current = None
        n = wm.get()
        check("$NIRI_SOCKET set: the niri backend", isinstance(n, nirimod.Niri))
        width = next(iter(nested.msg("outputs").values()))["logical"]["width"]

        # The tiling helper, on a python without site-packages.
        nested.env["PYTHONPATH"] = REPO
        tiler = nested.spawn([shutil.which("python3"), "-B", "-S", "-m",
                              "indigoshell.wm.tiling"], "tiling.log")
        await asyncio.sleep(0.5)
        check("the tiling helper runs on the standard library alone (python3 -S)",
              tiler.poll() is None, open(os.path.join(nested.tmp, "tiling.log")).read())

        # The bar, following niri's events.
        from indigoshell.widgets.workspaces import Workspaces

        class Bar:
            x, y, width, height = 0, 0, 200, 20

            def damage(self, *_a, **_k):
                pass

        bar = Workspaces()
        bar.attach(Bar())
        await wait_for(lambda: bar._count)
        check("the bar: workspaces 1-9 of this monitor, the first shown, none used",
              (bar._count, bar._current, sum(bar._per_desktop)) == (9, 0, 0),
              str((bar._count, bar._current, bar._per_desktop)))

        def count_windows(k):
            return len(nested.msg("windows")) >= k

        g = lambda: (w["id"] for w in nested.msg("windows"))  # noqa: E731
        expect = [(1, [[width - 16]]), (2, [[(width - 24) / 2]] * 2),
                  (3, None), (4, None)]
        ids = []
        ws1 = next(w["id"] for w in nested.msg("workspaces") if w["name"] == "1")
        for i, (k, _) in enumerate(expect, 1):
            nested.kitty(f"k{i}")
            await wait_for(lambda: count_windows(i), 15)
            await asyncio.sleep(0.6)
            ids = sorted(g())
            cols = columns_of(nested, ws1)
            shape = [[t[1] for t in c] for c in cols]
            ws_ = [c[0][2] for c in cols]
            if i == 1:
                ok = shape == [[ids[0]]] and abs(ws_[0] - (width - 16)) <= 1
            elif i == 2:
                ok = shape == [[ids[0]], [ids[1]]] and all(
                    abs(v - (width - 24) / 2) <= 1 for v in ws_)
            else:
                ok = shape == [[ids[0]], ids[1:]] and all(
                    abs(v - (width - 24) / 2) <= 1 for v in ws_)
            check(f"tiling, {i} window(s): " + {1: "one column, the whole screen",
                                               2: "two columns, half and half"}.get(
                i, "the new one stacked in the focused column, widths kept"),
                ok, f"{shape} widths {ws_}")
        focused = (nested.msg("focused-window") or {}).get("id")
        check("... focus stayed on the window niri focused (the newest)",
              focused == ids[-1], str(focused))
        if keep_shots:
            shot = await n.screenshot()
            with open(os.path.join(keep_shots, "niri-tiling-4.png"), "wb") as f:
                f.write(shot.png)
        await wait_for(lambda: bar._per_desktop[:1] == [4])
        check("the bar: four windows on workspace 1", bar._per_desktop[:2] == [4, 0],
              str(bar._per_desktop))

        # A resize by hand stays until the number of columns changes.
        subprocess.run(["niri", "msg", "action", "set-window-width", "--id",
                        str(ids[0]), "--", "-10%"], env=nested.env, check=True)
        await asyncio.sleep(0.4)
        before = columns_of(nested, ws1)[0][0][2]
        nested.kitty("k5")
        await wait_for(lambda: count_windows(5), 15)
        await asyncio.sleep(0.6)
        after = columns_of(nested, ws1)
        check("a column resized by hand keeps its width as a fifth window stacks",
              before < (width - 24) / 2 - 10 and abs(after[0][0][2] - before) <= 1
              and len(after) == 2, f"{before} -> {after}")

        # The interface, on this niri.
        spaces = await n.workspaces()
        wins = await n.windows()
        raw = nested.msg("windows")
        check("workspaces: 1-9 by name, 1 shown",
              [s.name for s in spaces][:9] == [str(i) for i in range(1, 10)]
              and [s.name for s in spaces if s.shown] == ["1"], str(spaces))
        check("windows: niri's, with app ids and titles",
              sorted((w.id, w.app) for w in wins)
              == sorted((w["id"], w["app_id"]) for w in raw)
              and all(w.workspace == "1" for w in wins), str(wins))
        reply = texts(await laptop._windows(call("windows")))
        check("laptop_windows on niri", reply.startswith("workspace 1 (shown)\n  [")
              and "wmtest.k1: wm test k1" in reply, reply[:200])

        await n.focus_workspace("3")
        ok = await wait_for(lambda: next(w for w in nested.msg("workspaces")
                                         if w["is_focused"])["name"] == "3")
        await wait_for(lambda: bar._current == 2)
        check("focus workspace 3: niri shows it; the bar follows", ok and bar._current == 2,
              str(bar._current))
        win = await n.focus_window(ids[1])
        ok = await wait_for(lambda: (nested.msg("focused-window") or {}).get("id") == ids[1])
        await wait_for(lambda: bar._current == 0)
        check("focus a window: it and its workspace come forward", ok and bar._current == 0
              and win.title == "wm test k2", f"{win}")
        reply = texts(await laptop._focus(call("focus", window_id=ids[0])))
        check("laptop_focus on niri", reply == "Focused wm test k1, on workspace 1.", reply)

        # Screenshots: PNG bytes, nothing saved, the clipboard untouched.
        await asyncio.sleep(0.3)
        n.copy("before the screenshots")
        await asyncio.sleep(0.5)
        shot = await n.screenshot()
        img = decoded(shot.png)
        out = next(iter(nested.msg("outputs").values()))["logical"]
        check("screenshot of the screen: a PNG the size of the output",
              shot.png[:8] == b"\x89PNG\r\n\x1a\n"
              and (img.width(), img.height()) == (out["width"], out["height"]),
              f"{img.width()}x{img.height()}")
        mon = await n.screenshot("monitor")
        check("screenshot of the monitor in use", mon.what == "monitor 0 (winit)"
              and decoded(mon.png).width() == out["width"], mon.what)
        tiled = await n.screenshot("window", window=ids[0])
        check("screenshot of a tiled window: its monitor, and why",
              "doesn't say where a tiled window sits" in tiled.what, tiled.what)
        subprocess.run(["niri", "msg", "action", "toggle-window-floating", "--id",
                        str(ids[0])], env=nested.env, check=True)
        await asyncio.sleep(0.5)
        fw = next(w for w in nested.msg("windows") if w["id"] == ids[0])
        cut = await n.screenshot("window", window=ids[0])
        ci = decoded(cut.png)
        check("screenshot of a floating window: cut to its size",
              (ci.width(), ci.height()) == tuple(fw["layout"]["window_size"]),
              f"{ci.width()}x{ci.height()} vs {fw['layout']['window_size']}")
        if keep_shots:
            with open(os.path.join(keep_shots, "niri-window.png"), "wb") as f:
                f.write(cut.png)
        files = os.listdir(nested.shots)
        clip = await n.paste()
        check("... nothing saved, the clipboard as it was",
              files == [] and clip == "before the screenshots", f"{files} {clip!r}")
        # niri's own picture of the screen, as an oracle (it does take the
        # clipboard; this is the nested one's).
        mine = decoded((await n.screenshot()).png)
        path = os.path.join(nested.shots, "oracle.png")
        subprocess.run(["niri", "msg", "action", "screenshot-screen", "--path", path],
                       env=nested.env, check=True)
        await wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0)
        await asyncio.sleep(0.3)
        theirs = decoded(open(path, "rb").read())
        same = (mine.width(), mine.height()) == (theirs.width(), theirs.height())
        diff = 0
        if same:
            for (px, py) in [(x, y) for x in range(5, mine.width(), 97)
                             for y in range(5, mine.height(), 89)]:
                a, b = pixel(mine, px, py), pixel(theirs, px, py)
                diff = max(diff, max(abs(i - j) for i, j in zip(a, b)))
        check("the screen as niri's own screenshot has it (upright, same pixels)",
              same and diff <= 8, f"size {same}, largest difference {diff}")

        # The clipboard, inside the nested session.
        reply = texts(await laptop._write_clipboard(call("write_clipboard",
                                                         text="über clip")))
        await asyncio.sleep(0.5)
        reply2 = texts(await laptop._read_clipboard(call("read_clipboard")))
        pasted = subprocess.run(["wl-paste", "--no-newline"], env=nested.env,
                                capture_output=True, text=True).stdout
        check("clipboard: laptop_write_clipboard, then read back (and by wl-paste)",
              reply == "Copied." and reply2 == "über clip" and pasted == "über clip",
              f"{reply!r} {reply2!r} {pasted!r}")
        n.copy("primary text", "primary")
        await asyncio.sleep(0.5)
        check("clipboard: the primary selection", await n.paste("primary") == "primary text")

        # Keyboard layouts (the nested niri's own; X's are untouched).
        layouts = await n.keyboard_layouts()
        await n.switch_keyboard_layout(1)
        switched = nested.msg("keyboard-layouts")
        await n.switch_keyboard_layout(0)
        check("keyboard layouts: niri's two, the first in use; switching to the second",
              [a for _, a in layouts] == [True, False] and len(layouts) == 2
              and switched["current_idx"] == 1
              and nested.msg("keyboard-layouts")["current_idx"] == 0,
              f"{layouts} {switched}")

        # Outputs.
        outs = await n.outputs()
        check("outputs: the nested one, lit, its mode current",
              [o.name for o in outs] == ["winit"] and outs[0].active
              and outs[0].current is not None and not outs[0].primary, str(outs))
        await n.use_only("winit", outs[0].current, [])
        check("use winit alone: applied, still lit",
              (await n.outputs())[0].active)

        # Windows closing: the columns closed up and widths back.
        for p in [p for p in nested.procs if p.args[0] == "kitty"][1:]:
            os.killpg(p.pid, signal.SIGTERM)
        await wait_for(lambda: len(nested.msg("windows")) == 1, 10)
        subprocess.run(["niri", "msg", "action", "toggle-window-floating", "--id",
                        str(ids[0])], env=nested.env, check=True)
        await asyncio.sleep(0.8)
        cols = columns_of(nested, ws1)
        check("the others closed, the first tiled again: one column, the whole screen",
              len(cols) == 1 and abs(cols[0][0][2] - (width - 16)) <= 1, str(cols))
        await wait_for(lambda: bar._per_desktop[:1] == [1])
        check("the bar: one window left", bar._per_desktop[:1] == [1], str(bar._per_desktop))
        bar.detach()

        # Last: log out (niri's quit, no confirmation).
        n.logout()
        gone = await wait_for(lambda: nested.niri.poll() is not None, 10)
        check("log out: niri quits", bool(gone))
        await asyncio.sleep(0.5)
        check("... and the tiling helper ends with its stream", tiler.poll() is not None)
        log = open(os.path.join(nested.tmp, "tiling.log")).read()
        print("  tiling helper said:\n" + "\n".join("    " + line
                                                     for line in log.splitlines()))
    finally:
        nested.stop()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        wm._current = None


async def main_async(args) -> None:
    saved = wm._current
    try:
        await test_x11_pure()
        await test_niri_pure()
        test_wayland_pure()
        test_tiling_pure()
        wm._current = None
        await test_x11_live()
        if args.niri:
            await test_niri_live(args.shots)
    finally:
        wm._current = saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--niri", action="store_true",
                        help="also run a nested niri on qtile group 8")
    parser.add_argument("--shots", metavar="DIR",
                        help="keep a couple of the nested niri's screenshots here")
    args = parser.parse_args()
    asyncio.run(main_async(args))
    print(f"\n{len(FAILED)} failed" + (": " + ", ".join(FAILED) if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
