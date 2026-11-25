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
   $WAYLAND_DISPLAY); which kinds the canvas draws (the bars, panels,
   chord menu, launcher, drop-down, toasts) and which it refuses (the
   call, the systray's); the edge a spec reserves and how far; what a back buffer
   of each age must repaint; the foreign-toplevel states that hide the
   canvas; buttons and wheel steps in X's numbers; the protocol bindings
   generated into a scratch cache.
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
   then a fullscreen window taking the canvas away and giving it back,
   the frame times, and idle.
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

    from indigoshell.agent import CALL_WINDOW
    from indigoshell.applink.drop import DROP_WINDOW
    from indigoshell.applink.launcher import LAUNCHER_WINDOW
    from indigoshell.canvas import CANVAS_KINDS
    from indigoshell.widgets import notification, systray
    from indigoshell.widgets.menu import MENU_WINDOW
    from indigoshell.window import WindowSpec

    drawn = [s.name for s in cd.WINDOWS if on_canvas(s)]
    check("the canvas draws every kind config_default ships: the bars, the three panels, the "
          "chord menu, the launcher, the drop-down",
          sorted(drawn) == sorted(["bar", "topbar", "system", "hardware", "network", "menu",
                                   "launcher", "dropdown"]), str(drawn))
    check("... named as their modules name them",
          CANVAS_KINDS == {cd.SYSTEM_PANEL, cd.HARDWARE_PANEL, cd.NETWORK_PANEL, MENU_WINDOW,
                           LAUNCHER_WINDOW, DROP_WINDOW}, str(CANVAS_KINDS))
    check("toasts (notification.py's names) are on it; the call and the systray's panel and "
          "menu are not yet",
          on_canvas(WindowSpec(name=notification._win_name(7)))
          and not on_canvas(WindowSpec(name=CALL_WINDOW, blur=True))
          and not on_canvas(WindowSpec(name=systray.PANEL_WINDOW))
          and not on_canvas(WindowSpec(name=systray.MENU_WINDOW)))
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
        if args.nested:
            test_nested(args)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n{len(FAILED)} failed" + (": " + ", ".join(FAILED) if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
