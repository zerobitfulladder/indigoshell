"""The shell on Wayland: the one canvas (canvas.py) and its backend
(backend/wayland.py, egl_wayland.py, wlproto.py).

    .venv/bin/python -B tests/wayland.py            1-3: pure and off screen,
                                                    a few seconds
    .venv/bin/python -B tests/wayland.py --nested   ... then 4: the shell itself
                                                    in a nested niri on qtile
                                                    group 8 (about a minute)
        --full     the nested niri on group 7 at the output's full size
                   instead (only when group 7 is empty, one run at a time)
        --shots DIR   keep the nested niri's screenshots there
        --raster   the nested shell without the GPU (INDIGOSHELL_GPU=0)

1. Pure: which display the shell picks ($INDIGOSHELL_BACKEND,
   $WAYLAND_DISPLAY); which kinds the canvas draws (the bars) and which
   it refuses; the edge a spec reserves and how far; what a back buffer
   of each age must repaint; the foreign-toplevel states that hide the
   canvas; buttons and wheel steps in X's numbers; the protocol bindings
   generated into a scratch cache.
2. Pure: the pointer's routing over stand-in regions: hover moving
   between regions and off the canvas, X's implicit grab (motion and
   the release to the region that had the press, wherever the pointer
   is; the hover settled after it), a second button, a grab broken by
   the compositor, the wheel, a region taken away under the pointer.
3. Off screen: the real bars (config_default's specs, real widgets; the
   window manager a stand-in) as regions of a 2560x1440 canvas on the
   CPU: where they sit, the input region and reservations they make,
   their pixels composed (the bars' background on their strips,
   transparent between), a clock tick damaging only its corner, a
   region hidden and back, the canvas resized and the bars following,
   the canvas taken away and back.
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
   between the bars reaching kitty, a fullscreen window taking the
   canvas away and giving it back, the frame times, and idle.
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

    drawn = [s.name for s in cd.WINDOWS if on_canvas(s)]
    check("the canvas draws the two bars and nothing else (phase 1)",
          sorted(drawn) == ["bar", "topbar"], str(drawn))
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

        def region_at(self, x, y):
            return next((r for r in reversed(self.regions) if r.takes_input(x, y)), None)

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
        asyncio.run(test_offscreen())
        if args.nested:
            test_nested(args)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n{len(FAILED)} failed" + (": " + ", ".join(FAILED) if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
