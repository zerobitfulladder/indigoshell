"""The shell on Wayland: the one canvas (canvas.py) and its backend
(backend/wayland.py, egl_wayland.py, wlproto.py).

    .venv/bin/python -B tests/wayland.py            1-3: pure and off screen,
                                                    a few seconds
    .venv/bin/python -B tests/wayland.py --nested   ... then 4: the shell itself
                                                    in a nested niri on qtile
                                                    group 8 (two minutes or so)
        --full     the nested niri on group 7 at the output's full size
                   instead (only when group 7 is empty, one run at a time)
        --shots DIR   keep the nested niri's screenshots there
        --raster   the nested shell without the GPU (INDIGOSHELL_GPU=0)

1. Pure: which display the shell picks ($INDIGOSHELL_BACKEND,
   $WAYLAND_DISPLAY); which kinds the canvas draws (every kind the shell
   ships: the bars, panels, chord menu, launcher, drop-down, toasts, the
   call and the systray's) and which it refuses (one of nobody's); the
   bars among them; a beveled plate as the whole-pixel stairs a blur
   region is made of; the edge a spec reserves and how far; what a back
   buffer of each age must repaint; the foreign-toplevel states that
   hide the canvas, a fullscreen window deactivated for the canvas's
   keyboard still one; buttons and wheel steps in X's numbers; the
   protocol bindings generated into a scratch cache.
2. Pure: the pointer's routing over stand-in regions: hover moving
   between regions and off the canvas, X's implicit grab (motion and
   the release to the region that had the press, wherever the pointer
   is; the hover settled after it), a second button, a grab broken by
   the compositor, the wheel, a region taken away under the pointer;
   outside clicks: told to who catches them and swallowed with their
   release, a press on a region still its own, the wheel.
2b. Pure: the keyboard: xkbcommon's keysym and text per key (us, tr's
   ı and İ, Caps Lock, a us,tr keymap's second group), the modifiers,
   no control characters and no text under Ctrl/Alt/Super, a keymap
   as the compositor sends it; our repeat (delay, rate, the newest key,
   none); who has the keyboard (the newest taker; asked for and let go
   of once); the input region while outside clicks are caught.
3. Off screen: the real bars (config_default's specs, real widgets; the
   window manager a stand-in) as regions of a 2560x1440 canvas on the
   CPU: where they sit, the input region and reservations they make,
   their pixels composed (the bars' background on their strips,
   transparent between), a clock tick damaging only its corner, a
   region hidden and back, the canvas resized and the bars following,
   the canvas taken away and back.
3b. Off screen: the launcher and the chord menu (config_default's,
   a stand-in daemon): centred / bottom right as on X, their spawn, the
   keyboard and outside clicks taken, keys typed into the query with
   the damage inside the launcher, Escape and an outside click closing
   (the keyboard given back at once), a digit run on its release.
3c. Off screen: the call (config_default's `call`, its hub a stand-in
   that plays the hub's events, tests/_live.py): top right, the
   keyboard and outside clicks taken, no blur during its spawn, then
   its plates and chips as the blur region (stair-stepped, nothing
   between or below), typed into, a line said and answered growing the
   plate and the blur region, text dragged across in the reply on
   PRIMARY and Ctrl+C's clipboard, Escape (blur off at once), Super+A
   and an outside click hanging up; then a fullscreen window: the bars
   stopped and cleared, no surface wanted until the call is called up
   over it (shown, blurred, the bars' strips empty) and none after; the
   top bar shown while its open input catches the outside clicks.
4. `--nested`: a nested niri (its config the user's own, read at run
   time, minus what it starts; a private D-Bus; its window on qtile
   group 8, put there by a runtime rule that is removed again), and the
   shell in it — DISPLAY unset, its own runtime, config, state, data and
   cache directories, a config.py of the user's shape plus a probe
   widget and a stand-in app — checked: it chose Wayland and the GPU
   (not llvmpipe), its layer surfaces, a kitty tiled between the
   strips, both bars in a screenshot, hover and clicks reaching the
   workspaces (niri follows), a press-drag-release on the probe, the
   stand-in app's tab dragged (its `move` arrives), the wheel, a click
   between the bars reaching kitty; then (phase 2a) niri's Mod+D
   opening the launcher, which takes the keyboard (exclusive), filters
   as typed (nothing leaking to the window), Ctrl+A, a held key's
   repeat, Turkish ı/İ and a second layout group, Escape giving the
   keyboard back to the window, an outside click closing it swallowed,
   Return launching into the nested niri; the system panel (Escape),
   the hardware panel (opened from the bar, closed by an outside
   click), the chord menu (a digit, Escape, outside), the stand-in
   app's menu in the top bar's drop-down (filtering, Return, Escape,
   outside), a toast from the shell's own bus; what each open costs;
   then (phase 2b) the call from niri's Mod+A over a busy window on a
   workspace of its own: placed, the keyboard taken, the blur region
   niri gets (the plates, stair-stepped) and the blur seen in niri's
   screenshot against the same with the canvas's blur off, typing, a
   line said and the reply growing the plate and the blur region, text
   selected (wl-paste --primary) and Ctrl+C (wl-paste), Escape, Mod+A
   again, Super+A and an outside click hanging up; the top bar's agent
   chip idle and dim, turning while a turn runs with the call hung up,
   blinking when it rings, a click answering; a tray item on the
   shell's bus, the tray's panel bottom right on the output, its menu
   beside its row, a row heard by the item; then a fullscreen window
   taking the canvas away, the launcher, a toast, the app's menu (the
   top bar with it) and the call each over it with nothing else, the
   canvas gone again after each; and giving it back, the frame times,
   and idle.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# The session's own, for qtile's client and the nested niri (section 4).
ORIG_ENV = dict(os.environ)

# Before anything of the shell's is imported: scratch directories, and no
# way to the session's D-Bus or sound server, so nothing here can touch
# the running shell or the desktop.
SCRATCH = tempfile.mkdtemp(prefix="wayland-test-")
for _var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[_var] = os.path.join(SCRATCH, _var.lower())
os.environ["XDG_RUNTIME_DIR"] = os.path.join(SCRATCH, "run")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={SCRATCH}/no-bus"
os.environ["PULSE_SERVER"] = f"unix:{SCRATCH}/no-pulse"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail if not ok else ''}".rstrip(),
          flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── 1. pure ─────────────────────────────────────────────────────────────
def test_pure() -> None:
    import skia

    from indigoshell import config_default as cd, theme
    from indigoshell.backend import wayland as wb, wlproto
    from indigoshell.canvas import (AxisSteps, DamageHistory, button_of, edge_of,
                                    on_canvas, reserved, scroll_buttons)
    from indigoshell.window import Anchor

    section("1. pure: the display, the kinds on the canvas, damage, states, input")
    order = wb.backend_order
    check("no $WAYLAND_DISPLAY: X11", order({"DISPLAY": ":0"}) == ["x11"])
    check("$WAYLAND_DISPLAY set: Wayland first (if it has layer-shell), then X11",
          order({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}) == ["wayland", "x11"])
    check("$INDIGOSHELL_BACKEND forces one either way",
          order({"WAYLAND_DISPLAY": "w", "INDIGOSHELL_BACKEND": "x11"}) == ["x11"]
          and order({"INDIGOSHELL_BACKEND": "Wayland"}) == ["wayland"])
    check("an empty $WAYLAND_DISPLAY is no Wayland", order({"WAYLAND_DISPLAY": ""}) == ["x11"])
    check("connect() on X: None, nothing opened", wb.connect({"DISPLAY": ":0"}) is None)

    from indigoshell.agent import CALL_WINDOW, STATUS_WINDOW
    from indigoshell.applink.drop import DROP_WINDOW
    from indigoshell.applink.launcher import LAUNCHER_WINDOW
    from indigoshell.canvas import CANVAS_KINDS, is_bar, pixel_rects
    from indigoshell.widgets import notification, systray
    from indigoshell.widgets.menu import MENU_WINDOW
    from indigoshell.window import WindowSpec

    drawn = [s.name for s in cd.WINDOWS if on_canvas(s)]
    check("the canvas draws every kind config_default ships: the bars, the three panels, the "
          "chord menu, the launcher, the drop-down",
          sorted(drawn) == sorted(["bar", "topbar", "system", "hardware", "network", "menu",
                                   "launcher", "dropdown"]), str(drawn))
    check("... named as their modules name them, the call's, its corner plate's and the "
          "systray's panel and menu with them",
          CANVAS_KINDS == {cd.SYSTEM_PANEL, cd.HARDWARE_PANEL, cd.NETWORK_PANEL, MENU_WINDOW,
                           LAUNCHER_WINDOW, DROP_WINDOW, CALL_WINDOW, STATUS_WINDOW,
                           systray.PANEL_WINDOW, systray.MENU_WINDOW}, str(CANVAS_KINDS))
    call_spec = WindowSpec(name=CALL_WINDOW, layer=cd.Layer.OVERLAY,
                           anchor=cd.Anchor.TOP | cd.Anchor.RIGHT, blur=True)
    check("toasts (notification.py's names) and the call are on it; a kind of nobody's is not",
          on_canvas(WindowSpec(name=notification._win_name(7))) and on_canvas(call_spec)
          and not on_canvas(WindowSpec(name="somebodys-own")))
    check("the bars are the kinds that reserve one edge; the call, a panel, a toast are not",
          [is_bar(s) for s in cd.WINDOWS if s.name in ("bar", "topbar", "system", "launcher")]
          .count(True) == 2 and not is_bar(call_spec)
          and not is_bar(WindowSpec(name=notification._win_name(7))))
    bevel = skia.Path()
    bevel.moveTo(12, 0)
    bevel.lineTo(100, 0)
    bevel.lineTo(100, 50)
    bevel.lineTo(0, 50)
    bevel.lineTo(0, 12)
    bevel.close()
    px = pixel_rects(bevel, skia.IRect.MakeWH(200, 200), 1000, 10)
    rows = [r for r in px if r[1] < 22]
    check("a plate as whole pixels: a 45° bevel of 12 stair-stepped, one rect per row each "
          "a pixel wider, the rest one rect; moved onto the canvas",
          len(rows) == 12 and all(r[3] == 1 for r in rows)
          and [r[0] for r in rows] == list(range(1012, 1000, -1))
          and px[-1] == (1000, 22, 100, 38) and all(r[0] + r[2] == 1100 for r in px), str(px))
    check("edges: top, bottom, left, right; a corner or the centre is no edge",
          [edge_of(a) for a in (Anchor.TOP, Anchor.BOTTOM | Anchor.LEFT | Anchor.RIGHT,
                                Anchor.LEFT, Anchor.RIGHT | Anchor.TOP | Anchor.BOTTOM,
                                Anchor.BOTTOM | Anchor.RIGHT, Anchor.NONE,
                                Anchor.TOP | Anchor.BOTTOM)]
          == ["top", "bottom", "left", "right", None, None, None])
    bh, th = theme.BAR_HEIGHT, theme.TOP_BAR_HEIGHT
    check("reservations as X's struts: the bottom bar 42 px, the top bar its height, a "
          "margin included",
          reserved("bottom", (0, 1440 - bh, 2560, bh), (2560, 1440)) == bh
          and reserved("top", (0, 0, 2560, th), (2560, 1440)) == th
          and reserved("top", (0, 6, 2560, th), (2560, 1440)) == th + 6
          and reserved("left", (4, 0, 30, 1440), (2560, 1440)) == 34
          and reserved("right", (2500, 0, 60, 1440), (2560, 1440)) == 60)

    full = skia.IRect.MakeWH(100, 100)
    h = DamageHistory()
    a = skia.Region(skia.IRect.MakeXYWH(0, 0, 10, 10))
    b = skia.Region(skia.IRect.MakeXYWH(50, 50, 10, 10))
    c = skia.Region(skia.IRect.MakeXYWH(90, 0, 10, 10))
    check("buffer age 0 (unknown): the whole canvas",
          h.repaint(a, 0, full).getBounds() == full)
    check("age 1 before any frame: only the new damage",
          h.repaint(a, 1, full).getBounds() == skia.IRect.MakeXYWH(0, 0, 10, 10))
    check("age 2 with no history: the whole canvas", h.repaint(a, 2, full).getBounds() == full)
    h.push(a)
    h.push(b)
    r3 = h.repaint(c, 3, full)
    check("age 3: this frame's damage and the two frames' since",
          r3.contains(skia.IRect.MakeXYWH(0, 0, 10, 10)) and r3.contains(
              skia.IRect.MakeXYWH(50, 50, 10, 10)) and r3.contains(
              skia.IRect.MakeXYWH(90, 0, 10, 10)) and not r3.contains(5, 50))
    r2 = h.repaint(c, 2, full)
    check("age 2: the last frame's only", r2.contains(55, 55) and not r2.contains(5, 5))
    for _ in range(6):
        h.push(c)
    check("history kept to four frames: age 6 repaints all",
          len(h.frames) == 4 and h.repaint(a, 6, full).getBounds() == full)

    import struct
    raw = struct.pack("=3I", 0, 2, 3)
    check("toplevel states from bytes or a list",
          wb.parse_states(raw) == {0, 2, 3} and wb.parse_states([3]) == {3}
          and wb.parse_states(None) == set())
    check("fullscreen = fullscreen and activated; either alone is not",
          wb.is_fullscreen({2, 3}) and not wb.is_fullscreen({3})
          and not wb.is_fullscreen({2}) and wb.is_fullscreen({0, 2, 3}))

    # Toplevels as the compositor reports them, the canvas's keyboard held
    # or not.
    class Handle:
        def __init__(self):
            self.dispatcher = {}

        def destroy(self):
            pass

    class Manager:
        def __init__(self):
            self.dispatcher = {}

    saved_ptr, wb._ptr = wb._ptr, id
    try:
        held = [False]
        told = []
        mgr = Manager()
        wb.Toplevels(None, mgr, told.append, held=lambda: held[0])
        tl_recheck = None
        h1, h2 = Handle(), Handle()
        mgr.dispatcher["toplevel"](mgr, h1)
        mgr.dispatcher["toplevel"](mgr, h2)

        def state(h, *states):
            h.dispatcher["state"](h, list(states))
            h.dispatcher["done"](h)

        state(h1, 2, 3)
        state(h2)
        fs_on = list(told)
        held[0] = True
        state(h1, 3)                        # deactivated: the canvas took the keyboard
        kept = list(told)
        held[0] = False
        tl_recheck = h1.dispatcher["done"]
        tl_recheck(h1)                      # as Toplevels.recheck: the states as they are
        let_go = list(told)
        state(h1, 2, 3)
        held[0] = True
        state(h1, 3)
        state(h2, 2)                        # another window activated meanwhile
        other = list(told)
        state(h2)
        state(h1, 2, 3)
        state(h1, 2)                        # left fullscreen while held
        left = list(told)
    finally:
        wb._ptr = saved_ptr
    check("a fullscreen window deactivated for the canvas's keyboard (nothing else "
          "activated) stays fullscreen while it is held; let go, as the states say",
          fs_on == [True] and kept == [True] and let_go == [True, False], f"{told}")
    check("... but not when another window is activated meanwhile, or the window leaves "
          "fullscreen", other == [True, False, True, False] and left[-1] is False
          and left.count(True) == 3, f"{told}")
    check("buttons: left 1, middle 2, right 3, side 8/9, others none",
          [button_of(c) for c in (0x110, 0x112, 0x111, 0x113, 0x114, 0x115)]
          == [1, 2, 3, 8, 9, None])
    check("wheel: up 4, down 5, left 6, right 7, one per step",
          scroll_buttons(0, -2) == [4, 4] and scroll_buttons(0, 1) == [5]
          and scroll_buttons(1, -1) == [6] and scroll_buttons(1, 3) == [7, 7, 7])
    s = AxisSteps()
    got = s.discrete(0, 1) + s.value(0, 15.0)
    s.frame()
    got2 = s.value(0, 7.0) + s.value(0, 7.0) + s.value(0, 7.0)
    check("a wheel's discrete step counts once; a touchpad's values summed in 15s",
          got == [5] and got2 == [5], f"{got} {got2}")
    s.stop(0)
    check("axis_stop drops what was left over", s.value(0, 7.0) == [])

    files = [p for p in wlproto.XML if os.path.exists(p)]
    if not all(os.path.exists(p) for p in wlproto.REQUIRED):
        print("  (no wayland.xml / wlr-layer-shell XML here: bindings skipped)")
        return
    check("the bindings' cache key: stable for the same inputs",
          wlproto.key(files) == wlproto.key(files) and len(wlproto.key(files)) == 16)
    t0 = time.monotonic()
    wl = wlproto.load()
    took = time.monotonic() - t0
    root = wlproto.cache_root()
    check("generated into the (scratch) cache, never the repository, and loaded",
          root.startswith(SCRATCH) and hasattr(wl, "wlr_layer_shell_unstable_v1")
          and hasattr(wl.wayland, "WlCompositor") and not os.path.exists(
              os.path.join(REPO, "wlproto")), f"{root} ({took:.1f}s)")
    LS = wl.wlr_layer_shell_unstable_v1
    check("layer-shell's enums as the canvas uses them",
          int(LS.ZwlrLayerShellV1.layer.overlay) == 3
          and int(LS.ZwlrLayerSurfaceV1.anchor.top | LS.ZwlrLayerSurfaceV1.anchor.bottom) == 3)


# ── 2. pure: the pointer ────────────────────────────────────────────────
class StandIn:
    """A region as PointerRouter sees one, recording what it is told."""

    def __init__(self, name, x, y, w, h):
        self.name, self.x, self.y, self.w, self.h = name, x, y, w, h
        self.log: list[tuple] = []

    def takes_input(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def pointer_motion(self, x, y):
        self.log.append(("motion", round(x), round(y)))

    def pointer_leave(self):
        self.log.append(("leave",))

    def pointer_button(self, b, pressed, x, y):
        self.log.append(("press" if pressed else "release", b, round(x), round(y)))

    def pointer_lost(self):
        self.log.append(("lost",))


def test_pointer() -> None:
    from indigoshell.canvas import PointerRouter

    section("2. pure: the pointer's routing")

    class Scene:
        def __init__(self, regions):
            self.regions = regions
            self.catchers = []

        def region_at(self, x, y):
            return next((r for r in reversed(self.regions) if r.takes_input(x, y)), None)

        def outside_click(self):
            for c in self.catchers:
                c()
            return bool(self.catchers)

    top = StandIn("top", 0, 0, 1000, 40)
    bottom = StandIn("bottom", 0, 960, 1000, 40)
    p = PointerRouter(Scene([top, bottom]))
    p.enter(10, 10)
    p.motion(20, 15)
    check("motion over a region: in its own coordinates",
          top.log == [("motion", 10, 10), ("motion", 20, 15)], str(top.log))
    p.motion(500, 965)
    check("moving to another region: the first hears leave, the second the motion",
          top.log[-1] == ("leave",) and bottom.log == [("motion", 500, 5)],
          f"{top.log} {bottom.log}")
    top.log.clear()
    bottom.log.clear()
    p.button(1, True)
    p.motion(500, 500)            # between the bars: nothing is drawn there
    p.motion(30, 20)              # over the top bar
    check("from a press until the release, motion goes to the region that had it, "
          "wherever the pointer is (off it, over another region)",
          bottom.log == [("press", 1, 500, 5), ("motion", 500, -460), ("motion", 30, -940)]
          and top.log == [], f"{bottom.log} {top.log}")
    p.button(3, True)
    p.button(3, False)
    check("a second button meanwhile: to the same region",
          bottom.log[-2:] == [("press", 3, 30, -940), ("release", 3, 30, -940)])
    p.button(1, False)
    check("the release too; then the region under the pointer is hovered again",
          bottom.log[-2:] == [("release", 1, 30, -940), ("leave",)]
          and top.log == [("motion", 30, 20)], f"{bottom.log} {top.log}")
    p.button(1, False)
    check("a release with nothing pressed: dropped",
          top.log == [("motion", 30, 20)] and bottom.log[-1] == ("leave",))
    top.log.clear()
    p.button(1, True)
    p.leave()
    check("the pointer leaving mid-press (the compositor broke the grab): the press is "
          "lost, then the hover", top.log == [("press", 1, 30, 20), ("lost",), ("leave",)],
          str(top.log))
    top.log.clear()
    p.button(1, True)
    check("no pointer on the canvas: a button goes nowhere", top.log == [])
    p.enter(100, 970)
    bottom.log.clear()
    p.scroll([4, 4])
    check("the wheel: a press and a release per step, to the region under it",
          bottom.log == [("press", 4, 100, 10), ("release", 4, 100, 10)] * 2, str(bottom.log))
    p.button(1, True)
    p.forget(bottom)
    bottom.log.clear()
    p.motion(110, 970)
    check("a region taken off the canvas mid-press: forgotten, not told",
          bottom.log == [("motion", 110, 10)] and p.grab is None, str(bottom.log))
    p.reset()
    check("reset (the canvas went away): nothing hovered or held",
          p.over is None and p.grab is None and p.xy is None)

    # Outside clicks: a press on no region while something catches them.
    scene = Scene([top, bottom])
    p = PointerRouter(scene)
    p.enter(500, 500)                       # between the bars
    p.button(1, True)
    p.button(1, False)
    check("a press on no region, nobody catching: nothing (it was never ours)",
          p.buttons == set() and p.grab is None)
    closed = []
    scene.catchers.append(lambda: closed.append(1))
    top.log.clear()
    bottom.log.clear()
    p.button(1, True)
    p.motion(30, 20)                        # over the top bar, still pressed
    p.button(3, True)
    p.button(3, False)
    p.button(1, False)
    check("caught: the catchers told once, and the press swallowed with its release, a second "
          "button and motion over a region meanwhile",
          closed == [1] and not any(e[0] in ("press", "release") for e in top.log + bottom.log)
          and p.buttons == set(), f"{closed} {top.log} {bottom.log}")
    p.button(1, True)
    p.button(1, False)
    check("... while a press on a region (the bar) still reaches it, as X's owner_events did",
          closed == [1] and [e for e in top.log if e[0] in ("press", "release")]
          == [("press", 1, 30, 20), ("release", 1, 30, 20)], str(top.log))
    p.motion(500, 500)
    p.scroll([5])
    check("the wheel on no region is an outside click too (X's buttons 4-7 under the grab)",
          closed == [1, 1])


# ── 2b. pure: the keyboard ──────────────────────────────────────────────
async def test_keyboard() -> None:
    from indigoshell.backend.wlkeys import Keys, Repeat, printable
    from indigoshell.canvas import KeyboardRouter, Scene
    from indigoshell.widgets.base import KeyEvent

    section("2b. pure: the keyboard (xkbcommon, our repeat, who has it)")
    try:
        keys = Keys()
    except ImportError:
        print("  (no python-xkbcommon here: skipped)")
        return
    check("printable: control characters out (Return, Tab, Escape, BackSpace, Delete, "
          "Ctrl+A's \\x01), the rest kept",
          printable("\r\t\x1b\x08\x7f\x01") == "" and printable("aı İ€") == "aı İ€")
    check("no keymap yet: no event", keys.event(30) is None and not keys.ready)
    keys.load_names("us")

    def ev(code, *mods, group=0):
        keys.modifiers(keys.mask(*mods), 0, 0, group)
        return keys.event(code)

    a, A, ret, esc = ev(30), ev(30, "shift"), ev(28), ev(1)
    check("us: a, Shift+A (its keysym the upper one, as X's), Return and Escape typing nothing",
          (a.keysym, a.text, a.shift) == (0x61, "a", False) and (A.keysym, A.text, A.shift)
          == (0x41, "A", True) and (ret.keysym, ret.text) == (0xFF0D, "")
          and (esc.keysym, esc.text) == (0xFF1B, ""), f"{a} {A} {ret} {esc}")
    ca, aa, sa, alt_gr = ev(30, "ctrl"), ev(30, "alt"), ev(30, "super"), None
    check("Ctrl, Alt, Super: the modifier set, the keysym the key's, no text (never \\x01)",
          (ca.keysym, ca.text, ca.ctrl) == (0x61, "", True) and (aa.text, aa.alt) == ("", True)
          and (sa.text, sa.super) == ("", True) and not ca.alt and not aa.ctrl, f"{ca} {aa} {sa}")
    keys.load_names("tr")
    i, I, dot_i = ev(23), ev(40, "shift"), ev(40)
    check("tr: the I key types ı (keysym idotless), Shift on the i key İ",
          (i.keysym, i.text) == (0x2B9, "ı") and I.text == "İ" and dot_i.text == "i",
          f"{i} {I} {dot_i}")
    keys.modifiers(0, 0, keys.keymap.mod_get_index("Lock") and 1 << keys.keymap.mod_get_index(
        "Lock"), 0)
    caps = keys.event(40)
    check("Caps Lock on the i key: İ (xkb's own capitalisation, not str.upper's)",
          caps.text == "İ" and not caps.shift, str(caps))
    keys.load_names("us,tr")
    check("a us,tr keymap: the first group types i, the second (wl_keyboard.modifiers' group) ı",
          ev(23).text == "i" and ev(23, group=1).text == "ı")
    text = keys.keymap.get_as_bytes()
    k2 = Keys()
    check("a keymap as the compositor sends it (XKB text, NUL terminated) compiles; a broken "
          "one is refused and the old kept",
          k2.load((text if isinstance(text, bytes) else str(text).encode()) + b"\0")
          and not k2.load(b"xkb_keymap { broken") and k2.ready)
    check("repeats: letters yes, Shift no", keys.repeats(30) and not keys.repeats(42))
    check("code_for: the key and level that type a character (the test's virtual keyboard)",
          keys.code_for("a") == (30, False) and keys.code_for("A") == (30, True))

    loop = asyncio.get_running_loop()
    fired = []
    r = Repeat(loop, fired.append)
    r.info(50, 100)                         # 100 ms, then every 20 ms
    r.press(30)
    await asyncio.sleep(0.05)
    early = len(fired)
    await asyncio.sleep(0.25)
    r.release(30)
    n = len(fired)
    await asyncio.sleep(0.1)
    check("repeat: nothing before the delay, then the rate (about 10 in 0.2 s), none after "
          "the release", early == 0 and 7 <= n <= 13 and len(fired) == n and set(fired) == {30},
          f"{early} {n} {len(fired)}")
    fired.clear()
    r.press(30)
    await asyncio.sleep(0.06)
    r.press(31)
    await asyncio.sleep(0.2)
    r.release(30)                           # the old key's release stops nothing
    await asyncio.sleep(0.05)
    r.stop()
    check("a second key down: the first stops, the newest repeats (as X's)",
          fired and set(fired) == {31}, str(fired))
    fired.clear()
    r.press(42, repeats=False)
    await asyncio.sleep(0.15)
    r.info(0, 100)
    r.press(30)
    await asyncio.sleep(0.15)
    check("a key that does not repeat, and a rate of 0: nothing", fired == [], str(fired))

    changes = []
    k = KeyboardRouter(lambda: changes.append(k.wanted))

    class Taker:
        def __init__(self, name):
            self.name, self.got = name, []

        def key_press(self, key):
            self.got.append(("press", key.keysym))

        def key_release(self, key):
            self.got.append(("release", key.keysym))

    one, two = Taker("one"), Taker("two")
    k.take("one", one)
    check("no keymap library: taking it asks for nothing (it would only take the windows' "
          "keys)", not k.wanted and changes == [])
    k.give_back("one")
    k.available = True
    k.take("one", one)
    k.take("two", two)
    k.press(KeyEvent(keysym=0x61, text="a"))
    k.release(KeyEvent(keysym=0x61))
    check("the newest taker gets the keys; asked for once",
          two.got == [("press", 0x61), ("release", 0x61)] and one.got == [] and changes == [True])
    k.take("one", one)
    k.press(KeyEvent(keysym=0x62))
    k.give_back("one")
    k.press(KeyEvent(keysym=0x63))
    check("taken again: on top; given back: the one before has them again",
          one.got == [("press", 0x62)] and two.got[-1] == ("press", 0x63))
    k.give_back("two")
    k.give_back("two")
    check("the last one gone: let go of, once", changes == [True, False] and not k.wanted
          and not k.press(KeyEvent(keysym=0x64)))

    class Bad:
        def key_press(self, key):
            raise RuntimeError("bad handler")

    k.take("bad", Bad())
    check("a handler that raises: logged, not raised", k.press(KeyEvent(keysym=0x61)))
    k.give_back("bad")

    sc = Scene(loop)
    sc.resize(800, 600)
    layouts = []
    sc.on_layout = lambda: layouts.append(1)
    sc.catch_outside("p", lambda: None)
    whole = sc.input_rects()
    sc.release_outside("p")
    check("while outside clicks are caught the input region is the whole canvas, and back after",
          [(r.left(), r.top(), r.width(), r.height()) for r in whole] == [(0, 0, 800, 600)]
          and sc.input_rects() == [] and len(layouts) == 2)


# ── 3. off screen: the real bars on a canvas ────────────────────────────
async def test_offscreen() -> None:
    import skia

    from indigoshell import config_default as cd, theme, wm
    from indigoshell.canvas import Region, Scene

    section("3. off screen: the real bars as regions of a 2560x1440 canvas (CPU)")

    class StandInWM(wm.WM):
        name = "stand-in"

        def follow_workspaces(self, window, on_change):
            on_change(wm.WorkspaceState(count=5, current=1, windows=(1, 2, 0, 0, 0),
                                        urgent=(False,) * 5))
            return wm.Follower()

    saved = wm._current
    wm._current = StandInWM()
    loop = asyncio.get_running_loop()
    scene = Scene(loop)
    presents = []
    layouts = []
    scene.on_damage = lambda: presents.append(1)
    scene.on_layout = lambda: layouts.append(1)
    scene.resize(2560, 1440)
    scene.take_damage()                 # the resize's: all of it
    specs = {s.name: s for s in cd.WINDOWS}
    regions = {}
    try:
        for name in ("bar", "topbar"):
            r = Region(scene, specs[name], loop)
            r.create()
            r.show()
            regions[name] = r
        bar, top = regions["bar"], regions["topbar"]
        bh, th = theme.BAR_HEIGHT, theme.TOP_BAR_HEIGHT
        check("where they sit: as their X windows on a 2560x1440 monitor",
              (bar.x, bar.y, bar.width, bar.height) == (0, 1440 - bh, 2560, bh)
              and (top.x, top.y, top.width, top.height) == (0, 0, 2560, th),
              f"{(bar.x, bar.y, bar.width, bar.height)} {(top.x, top.y, top.width, top.height)}")
        check("z-order: both docks, in the order shown", scene.regions == [bar, top])
        check("the input region: exactly the two strips",
              [(r.left(), r.top(), r.width(), r.height()) for r in scene.input_rects()]
              == [(0, 0, 2560, th), (0, 1440 - bh, 2560, bh)], str(scene.input_rects()))
        check("the reservations: the top and the bottom, each bar's height",
              scene.reservations() == {"top": th, "bottom": bh}, str(scene.reservations()))
        await asyncio.sleep(0.4)
        surface = skia.Surface(2560, 1440)
        canvas = surface.getCanvas()
        damage = scene.take_damage()
        scene.compose(canvas, damage)
        img = surface.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType,
                                                  alphaType=skia.kUnpremul_AlphaType)
        bg = tuple(int(theme.BAR_BG[i:i + 2], 16) for i in (1, 3, 5))

        def share(rows, cols, rgb, alpha=255):
            px = img[rows, cols]
            ok = ((abs(px[..., :3].astype(int) - rgb).max(axis=-1) <= 2)
                  & (px[..., 3] == alpha))
            return ok.mean()

        check("the first frame damaged both strips and nothing between",
              damage.contains(skia.IRect.MakeXYWH(0, 0, 2560, th))
              and damage.contains(skia.IRect.MakeXYWH(0, 1440 - bh, 2560, bh))
              and not damage.intersects(skia.IRect.MakeXYWH(0, th, 2560, 1440 - th - bh)))
        check("composed: the bars' background on their strips (mostly; widgets on it), "
              "opaque",
              share(slice(1440 - bh, 1440), slice(0, 2560), bg) > 0.6
              and share(slice(0, th), slice(0, 2560), bg) > 0.8,
              f"{share(slice(1440 - bh, 1440), slice(0, 2560), bg):.2f} "
              f"{share(slice(0, th), slice(0, 2560), bg):.2f}")
        check("between the bars: transparent, every pixel",
              (img[th:1440 - bh, :, 3] == 0).all())
        check("something drawn on the bottom bar (its widgets)",
              share(slice(1440 - bh, 1440), slice(0, 2560), bg) < 0.995)

        presents.clear()
        await asyncio.sleep(1.3)
        tick = scene.take_damage()
        tb = tick.getBounds()
        area = sum(r.width() * r.height() for r in __import__(
            "indigoshell.canvas", fromlist=["rects"]).rects(tick))
        # The meters' labels relayout their bar when their text changes
        # (Label.set_value: invalidate(layout=True)), on X11 as here, so
        # the bottom bar may be repainted whole; never more than it.
        check("a second later: the bottom bar damaged (the clock, the meters), within it",
              not tick.isEmpty() and tb.top() >= 1440 - bh and area <= 2560 * bh,
              f"bounds {tb}, {area} px")
        clock = cd.clock
        cb = skia.IRect.MakeXYWH(int(clock.rect.left()) + bar.x, int(clock.rect.top()) + bar.y,
                                 int(clock.rect.width()), int(clock.rect.height()))
        check("... the clock's own rect among it", tick.intersects(cb), f"{cb}")
        check("... and the top bar (nothing on it moves) not at all",
              not tick.intersects(skia.IRect.MakeXYWH(0, 0, 2560, th)))
        stats = scene.stats.paints
        check("the regions' paints timed",
              "bar" in stats and len(stats["bar"]) >= 2, str({k: len(v) for k, v in stats.items()}))

        top.hide()
        gone = scene.take_damage()
        check("a region hidden: off the canvas, its strip damaged, the input region and "
              "the reservation without it",
              top not in scene.regions and gone.contains(skia.IRect.MakeXYWH(0, 0, 2560, th))
              and scene.reservations() == {"bottom": bh}
              and len(scene.input_rects()) == 1)
        top.show()
        check("... and shown again", top in scene.regions
              and scene.reservations() == {"top": th, "bottom": bh})

        scene.resize(1920, 1080)
        await asyncio.sleep(0.2)
        check("the canvas resized: the bars follow at once, all of it damaged",
              (bar.x, bar.y, bar.width) == (0, 1080 - bh, 1920) and top.width == 1920
              and scene.take_damage().getBounds() == skia.IRect.MakeWH(1920, 1080),
              f"{(bar.x, bar.y, bar.width)} {top.width}")

        scene.suspend()
        await asyncio.sleep(1.2)
        quiet = scene.take_damage()
        check("the canvas taken away (a fullscreen window): the regions' clocks stop, "
              "nothing is damaged, no input region",
              quiet.isEmpty() and not bar.mapped and not bar._clock_fps
              and scene.input_rects() == [], f"{quiet.getBounds()}")
        scene.resume()
        await asyncio.sleep(0.2)
        check("... and back: all of it damaged, the regions painting again",
              scene.take_damage().getBounds() == skia.IRect.MakeWH(1920, 1080)
              and bar.mapped and bar._clock_fps > 0)
    finally:
        for r in regions.values():
            r.destroy()
        wm._current = saved
    check("destroyed: off the canvas", scene.regions == [])


# ── 3b. off screen: the launcher and the chord menu ─────────────────────
async def test_offscreen_overlays() -> None:
    import skia

    from indigoshell import config_default as cd
    from indigoshell.canvas import Region, Scene, rects
    from indigoshell.core import daemon as core_daemon
    from indigoshell.plugin import Item, Menu
    from indigoshell.widgets.base import KeyEvent
    from indigoshell.widgets.menu import MENU_WINDOW

    section("3b. off screen: the launcher and the chord menu as regions (CPU)")
    loop = asyncio.get_running_loop()
    scene = Scene(loop)
    scene.resize(2560, 1440)
    scene.keyboard.available = True
    asks = []
    scene.on_keyboard = lambda: asks.append(scene.keyboard.wanted)
    specs = {s.name: s for s in cd.WINDOWS}

    class StandInDaemon:
        """What a region asks of the daemon: close it, dismiss the
        others."""

        def __init__(self):
            self.instances, self.closed = {}, []

        def open(self, name):
            r = Region(scene, specs[name], loop)
            r.create()
            self.instances[name] = r
            r.show()
            return r

        def close(self, name):
            win = self.instances.pop(name, None)
            if win is None:
                return False
            self.closed.append(name)
            win.dismiss(win.destroy)
            return True

        def dismiss_others(self, except_name=None):
            pass

    daemon = StandInDaemon()
    saved = core_daemon._DAEMON
    core_daemon._DAEMON = daemon
    try:
        launcher = daemon.open("launcher")
        cx, cy, cw, ch = launcher.content_root
        check("the launcher: centred (no anchor), the keyboard and the outside clicks taken, "
              "the input region the whole canvas",
              (cx + cw // 2, cy + ch // 2) in ((1280, 720), (1279, 719), (1280, 719), (1279, 720))
              and scene.keyboard.holds(launcher) and launcher in scene.catchers
              and asks == [True] and [(r.width(), r.height()) for r in scene.input_rects()]
              == [(2560, 1440)], f"{launcher.content_root} {asks} {scene.input_rects()}")
        await asyncio.sleep(0.4)
        check("its spawn played (ScanLock, frames of its own) and it is still",
              launcher._phase == "idle" and len(scene.stats.paints.get("launcher", [])) >= 5,
              f"{launcher._phase} {len(scene.stats.paints.get('launcher', []))}")
        scene.take_damage()
        field = specs["launcher"].content.query.field
        for ch_ in "kit":
            scene.keyboard.press(KeyEvent(keysym=ord(ch_), text=ch_))
            scene.keyboard.release(KeyEvent(keysym=ord(ch_)))
        await asyncio.sleep(0.1)
        damage = scene.take_damage()
        box = launcher.box()
        check("keys from the canvas's keyboard reach its query; the damage is inside the "
              "launcher, a part of it",
              field.text == "kit" and not damage.isEmpty()
              and all(box.contains(r) for r in rects(damage))
              and sum(r.width() * r.height() for r in rects(damage)) < box.width() * box.height(),
              f"{field.text!r} {damage.getBounds()} in {box}")
        scene.keyboard.press(KeyEvent(keysym=0xFF1B))
        check("Escape: closed, the keyboard given back at once (the despawn needs none) and the "
              "outside clicks too", daemon.closed == ["launcher"] and asks == [True, False]
              and not scene.catchers and launcher in scene.regions,
              f"{daemon.closed} {asks} {scene.catchers}")
        await asyncio.sleep(0.4)
        check("... and gone from the canvas once its despawn has played",
              launcher not in scene.regions and scene.input_rects() == [])

        launcher = daemon.open("launcher")
        scene.pointer.enter(20, 20)
        scene.pointer.button(1, True)
        scene.pointer.button(1, False)
        check("an outside click on the canvas closes it", daemon.closed[-1] == "launcher"
              and "launcher" not in daemon.instances)
        await asyncio.sleep(0.4)

        ran = []
        host = specs[MENU_WINDOW].content
        await host.start(Menu("t", "TEST", items=[Item("ONE", run=lambda: ran.append(1)),
                                                  Item("TWO", run=lambda: ran.append(2))]))
        menu = daemon.open(MENU_WINDOW)
        mx, my, mw, mh = menu.content_root
        check("the chord menu: bottom right above the bar as on X, the keyboard taken",
              mx + mw == 2560 - specs[MENU_WINDOW].margin.right
              and my + mh == 1440 - specs[MENU_WINDOW].margin.bottom
              and scene.keyboard.holds(menu), f"{menu.content_root}")
        await asyncio.sleep(0.35)
        scene.keyboard.press(KeyEvent(keysym=ord("2"), text="2"))
        armed = ran == []
        scene.keyboard.release(KeyEvent(keysym=ord("2")))
        await asyncio.sleep(0.05)
        check("a digit arms its row on the press and runs it on the release (key releases "
              "reach the region too), then the menu closes",
              armed and ran == [2] and daemon.closed[-1] == MENU_WINDOW, f"{ran} {daemon.closed}")
        await asyncio.sleep(0.4)
        surface = skia.Surface(2560, 1440)
        scene.compose(surface.getCanvas(), skia.Region(scene.bounds))
        img = surface.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)
        check("everything closed: the canvas transparent, no input region",
              (img[..., 3] == 0).all() and scene.input_rects() == [] and not scene.keyboard.wanted)
    finally:
        for r in list(daemon.instances.values()):
            r.destroy()
        core_daemon._DAEMON = saved


# ── 3c. off screen: the call, and what shows over a fullscreen window ───
async def test_offscreen_call() -> None:
    import skia

    # The call's real bookkeeping with no hub behind it (tests/_live.py);
    # _live points XDG_STATE_HOME at a directory of its own: ours again.
    state_home = os.environ["XDG_STATE_HOME"]
    from _live import ScriptedAgent
    shutil.rmtree(os.environ["XDG_STATE_HOME"], ignore_errors=True)
    os.environ["XDG_STATE_HOME"] = state_home

    from indigoshell import config_default as cd, theme, wm
    from indigoshell.agent.ui.call import BOTTOM_CLEAR
    from indigoshell.canvas import Region, Scene, rects
    from indigoshell.core import daemon as core_daemon
    from indigoshell.widgets.base import KeyEvent

    section("3c. off screen: the call as a region, its blur; what shows over a fullscreen "
            "window (CPU)")
    copied: list[tuple[str, str]] = []

    class StandInWM(wm.WM):
        name = "stand-in"

        def follow_workspaces(self, window, on_change):
            on_change(wm.WorkspaceState(count=5, current=1, windows=(1, 0, 0, 0, 0),
                                        urgent=(False,) * 5))
            return wm.Follower()

        def copy(self, value, selection="clipboard"):
            copied.append((selection, value))
            return True

    saved_wm, wm._current = wm._current, StandInWM()
    loop = asyncio.get_running_loop()
    scene = Scene(loop)
    scene.resize(2560, 1440)
    scene.keyboard.available = True
    layouts = []
    scene.on_layout = lambda: layouts.append(1)
    agent = ScriptedAgent(voice="indigo")
    call_spec, _services = cd.call(agent)
    specs = {s.name: s for s in cd.WINDOWS}
    specs["call"] = call_spec

    class StandInDaemon:
        def __init__(self):
            self.instances, self.closed = {}, []
            self.config = {"services": []}

        def open(self, name, *_a, **_k):
            if name in self.instances:
                return self.instances[name]
            r = Region(scene, specs[name], loop)
            r.create()
            self.instances[name] = r
            r.show()
            return r

        def close(self, name):
            win = self.instances.pop(name, None)
            if win is None:
                return False
            self.closed.append(name)
            win.dismiss(win.destroy)
            return True

        def dismiss_others(self, except_name=None):
            pass

    def blur_box():
        region = skia.Region()
        for r in scene.blur_rects():
            region.op(r, skia.Region.kUnion_Op)
        return region

    def key(sym, text="", **mods):
        scene.keyboard.press(KeyEvent(keysym=sym, text=text, **mods))
        scene.keyboard.release(KeyEvent(keysym=sym, **mods))

    daemon = StandInDaemon()
    saved = core_daemon._DAEMON
    core_daemon._DAEMON = daemon
    bars = []
    try:
        call = daemon.open("call")
        host = call_spec.content
        cx, cy, cw, ch = call.content_root
        m = call_spec.margin
        entering = scene.blur_rects()
        check("the call: top right as on X (its margins), as tall as the canvas allows less "
              "the bar's clearance, the keyboard and the outside clicks taken",
              cx + cw == 2560 - m.right and cy == m.top and ch == 1440 - m.top - BOTTOM_CLEAR
              and scene.keyboard.holds(call) and call in scene.catchers,
              f"{call.content_root}")
        check("... no blur while its spawn plays (it tears past the plates)",
              call._phase == "enter" and entering == [], f"{call._phase} {entering}")
        await asyncio.sleep(0.45)
        blur = blur_box()
        plates = [host.you_plate, host.agent_plate, host.you_chip, host.agent_chip]
        inside = all(blur.contains(int(call.x + w.rect.centerX()), int(call.y + w.rect.centerY()))
                     for w in plates)
        gap = host.you_plate.rect.right() + 4       # between your plate and your chip
        steps = [r for r in scene.blur_rects() if r.height() == 1]
        check("spawned: the blur region is the plates and chips, stair-stepped at their "
              "bevels (one-pixel rows), nothing between them or below the call",
              call._phase == "idle" and inside and len(steps) >= 8
              and not blur.contains(int(call.x + gap), int(call.y + host.you_plate.rect.centerY()))
              and blur.getBounds().bottom() <= call.y + int(host.agent_plate.rect.bottom()) + 1
              and call.box().contains(blur.getBounds()),
              f"{blur.getBounds()} {len(scene.blur_rects())} rects, {len(steps)} steps")

        scene.take_damage()
        for ch_ in "hello":
            key(ord(ch_), ch_)
        await asyncio.sleep(0.1)
        damage = scene.take_damage()
        check("typed into the call through the canvas's keyboard: the line in your plate, the "
              "damage inside the call",
              host.you_plate.input.text == "hello" and not damage.isEmpty()
              and all(call.box().contains(r) for r in rects(damage)),
              f"{host.you_plate.input.text!r} {damage.getBounds()}")
        before = blur.getBounds()
        n_layouts = len(layouts)
        key(0xFF0D)                                     # Return: said
        await asyncio.sleep(0.05)
        agent.text_start()
        agent.text("Three resets since boot, all from the wifi driver.\n\n"
                   + "\n\n".join(f"Line {i} of the answer, long enough to read." for i in range(8)))
        await asyncio.sleep(0.3)
        agent.end()
        await asyncio.sleep(0.3)
        after = blur_box().getBounds()
        check("a line said and answered: the agent's plate grew, and the blur region with it "
              "(sent again: a layout change)",
              any(s.get("type") == "say" and s.get("text") == "hello" for s in agent.sent)
              and after.bottom() > before.bottom() + 40 and len(layouts) > n_layouts,
              f"{before} -> {after}")

        plate = host.agent_plate
        laid = plate._layout()
        row = next(((r, top) for r, top in laid if r.selectable and r.kind == "line"
                    and "Line 3" in r.src[r.start:r.end]), None)
        if row is not None:
            r, top = row
            y = call.y + top + r.h / 2
            x0 = call.x + plate.rect.left() + 40
            scene.pointer.enter(x0, y)
            scene.pointer.button(1, True)
            for i in range(1, 9):
                scene.pointer.motion(x0 + 30 * i, y)
            scene.pointer.button(1, False)
            await asyncio.sleep(0.05)
            primary = [v for s, v in copied if s == "primary"]
            key(ord("c"), ctrl=True)
            clip = [v for s, v in copied if s == "clipboard"]
            check("text dragged across in the agent's reply: selected and put on PRIMARY; "
                  "Ctrl+C copies it to the clipboard",
                  primary and len(primary[-1]) >= 10 and primary[-1] in r.src
                  and clip and clip[-1] == primary[-1],
                  f"{copied}")
        else:
            check("a line of the reply in the agent's plate", False, str([r.src for r, _ in laid])[:300])

        key(0xFF1B)                                     # Escape: hang up
        check("Escape hangs up: closed, the keyboard and the outside clicks given back, the "
              "blur off at once (the despawn runs unblurred)",
              daemon.closed[-1:] == ["call"] and not scene.keyboard.wanted and not scene.catchers
              and scene.blur_rects() == [], f"{daemon.closed} {scene.blur_rects()}")
        await asyncio.sleep(0.45)
        check("... gone once it has despawned", call not in scene.regions)

        call = daemon.open("call")
        await asyncio.sleep(0.4)
        key(ord("a"), super=True)
        check("Super+A (its close key, for when the compositor's binding does not take it) "
              "closes it too", daemon.closed[-1:] == ["call"] and "call" not in daemon.instances)
        await asyncio.sleep(0.45)
        call = daemon.open("call")
        await asyncio.sleep(0.4)
        scene.pointer.enter(300, 700)
        scene.pointer.button(1, True)
        scene.pointer.button(1, False)
        check("an outside click hangs up", daemon.closed[-1:] == ["call"]
              and "call" not in daemon.instances)
        await asyncio.sleep(0.45)

        # ── a fullscreen window: what shows ──────────────────────────────
        for name in ("bar", "topbar"):
            r = Region(scene, specs[name], loop)
            r.create()
            r.show()
            bars.append(r)
        bar, top = bars
        await asyncio.sleep(0.2)
        scene.take_damage()
        scene.set_fullscreen(True)
        damage = scene.take_damage()
        check("a fullscreen window: the bars stop (clocks, input), their strips cleared, and "
              "the canvas wants no surface",
              not scene.shows(bar) and not bar.mapped and not bar._clock_fps
              and damage.contains(skia.IRect.MakeXYWH(0, 1440 - theme.BAR_HEIGHT, 2560,
                                                      theme.BAR_HEIGHT))
              and not scene.wants_surface() and scene.input_rects() == [],
              f"{damage.getBounds()} {scene.wants_surface()}")
        call = daemon.open("call")
        await asyncio.sleep(0.45)
        surface = skia.Surface(2560, 1440)
        scene.compose(surface.getCanvas(), skia.Region(scene.bounds))
        img = surface.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)
        check("the call called up over it: the canvas wants its surface, the call shows, "
              "blurred, with the keyboard; the bars' strips stay empty",
              scene.wants_surface() and scene.shows(call) and call._clock_fps >= 0
              and scene.blur_rects() and scene.keyboard.holds(call)
              and (img[1440 - theme.BAR_HEIGHT:, :, 3] == 0).all()
              and (img[:theme.TOP_BAR_HEIGHT, :1500, 3] == 0).all()
              and img[call.y + int(host.agent_plate.rect.centerY()),
                      call.x + int(host.agent_plate.rect.centerX()), 3] > 0,
              f"{scene.wants_surface()} {len(scene.blur_rects())}")
        daemon.close("call")
        await asyncio.sleep(0.45)
        check("... hung up: once it has despawned the canvas wants no surface again",
              not scene.wants_surface() and call not in scene.regions)

        token = object()
        scene.catch_outside(token, lambda: None, top)
        shown_top = scene.shows(top) and top.mapped and not scene.shows(bar)
        scene.take_damage()
        scene.release_outside(token)
        gone = scene.take_damage()
        check("the top bar's open input (its outside clicks caught for it) shows the top bar "
              "over the window, the bottom one not; closed, the top bar goes and is cleared",
              shown_top and not scene.shows(top) and not top.mapped
              and gone.contains(skia.IRect.MakeXYWH(0, 0, 2560, theme.TOP_BAR_HEIGHT)))
        scene.set_fullscreen(False)
        await asyncio.sleep(0.1)
        check("the fullscreen window gone: both bars back, painting",
              scene.shows(bar) and bar.mapped and top.mapped and scene.wants_surface()
              and len(scene.input_rects()) == 2)
    finally:
        for r in list(daemon.instances.values()) + bars:
            r.destroy()
        core_daemon._DAEMON = saved
        wm._current = saved_wm


# ── 4. a nested niri ────────────────────────────────────────────────────
def test_nested(args) -> None:
    import nested_niri
    nested_niri.run(args, check, section, ORIG_ENV)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--nested", action="store_true",
                        help="also run the shell in a nested niri (qtile group 8)")
    parser.add_argument("--full", action="store_true",
                        help="the nested niri on group 7 at full size")
    parser.add_argument("--raster", action="store_true",
                        help="the nested shell without the GPU")
    parser.add_argument("--shots", metavar="DIR", help="keep the nested screenshots here")
    args = parser.parse_args()
    try:
        test_pure()
        test_pointer()
        asyncio.run(test_keyboard())
        asyncio.run(test_offscreen())
        asyncio.run(test_offscreen_overlays())
        asyncio.run(test_offscreen_call())
        if args.nested:
            test_nested(args)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n{len(FAILED)} failed" + (": " + ", ".join(FAILED) if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
