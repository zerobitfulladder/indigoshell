"""The shell in a nested niri: tests/wayland.py's section 4 (`--nested`).

Everything runs inside a niri of its own, a window of the X session on
qtile group 8 (or 7 with `--full`), put there by a runtime qtile rule
that is removed at the end:

- niri: its config the user's own, read at run time, minus what it
  starts (spawn-at-startup), its screenshots into the test's scratch
  directory, no Xwayland; under dbus-run-session (its notifications and
  portals stay off the session bus).
- the shell: DISPLAY unset, the nested niri's socket by absolute path,
  its own runtime, config, state, data and cache directories, a D-Bus
  of its own (dbus-run-session), no sound server, and a config.py of
  the user's shape (config_default's, plus a probe widget on the top
  bar, a chord menu whose items only log, timing probes, and the
  stand-in app, this process on the apps socket with no window in
  niri, taken as focused while no real app's window is); a desktop
  entry of the test's for its launcher to find (a kitty). niri's config
  gets docs/niri.kdl's window rule for the agent's Flux window.
- Flux (phase 2c): `main.py browser` with the nested shell's whole
  environment (its scratch XDG directories: never the user's profile,
  history or bookmarks; Widevine off), on Qt's Wayland platform, its
  pages from a server of the test's on 127.0.0.1, its control socket
  reached the same way; every Flux killed at the end of its section.
- niri's Mod+D: the user's (fuzzel) replaced by a script running the
  nested shell's own client with the nested shell's environment and
  nothing else (env -i), so it reaches that daemon, never the live one;
  Mod+A the same for `toggle call`. The call's hub is a stand-in
  (tests/_live.py's ScriptedAgent) that connects nowhere: the test plays
  the hub's events through a file the config reads.
  niri's virtual keyboard goes to the focused client and not through
  niri's bindings, so the test runs the binding's command as niri
  does for the key (`niri msg action spawn`).
- input only through niri's own virtual pointer and keyboard
  (zwlr_virtual_pointer, zwp_virtual_keyboard: prototypes/wayland/
  poke.py's way, in this process), so nothing reaches the X session;
  screenshots only by niri's own action, into the scratch directory.
"""

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

QTILE = r"""
import json, sys
from libqtile.command.client import InteractiveCommandClient as C
c = C()
cmd = sys.argv[1]
if cmd == "add":
    print(c.add_rule({"wm_class": "niri"}, {"group": sys.argv[2]}))
elif cmd == "remove":
    print(c.remove_rule(int(sys.argv[2])))
elif cmd == "group":
    print(json.dumps(c.group[sys.argv[2]].info()["windows"]))
elif cmd == "fullscreen":
    c.window[int(sys.argv[2])].enable_fullscreen()
    print("ok")
"""

# The probe and the focus hook, in a config of the user's shape.
TEST_CONFIG = r'''
"""The nested test's config: this machine's shape (config_default's
windows and services), a probe widget on the top bar, and the stand-in
app (the test's own process, with no window in niri) taken as focused
while no real app's window is."""

import asyncio
import json
import logging
import os

import skia

from indigoshell import text, theme
from indigoshell.config_default import (APPS, MENUS, SCREEN, SERVICES,  # noqa: F401
                                        STARTUP, TOP_BAR, WINDOWS, top_bar)
from indigoshell.widgets.base import Size, Widget

log = logging.getLogger("indigoshell.probe")
DUMP = os.environ["INDIGOSHELL_TEST_DUMP"]


class Probe(Widget):
    """Says what reaches it, and writes where everything on the bars is."""

    def __init__(self):
        super().__init__()
        self.moves = 0
        self.held = False

    def measure(self, avail):
        return Size(90, 26)

    def hit(self, x, y):
        return self if self.rect.contains(x, y) else None

    def paint(self, canvas):
        r = self.rect
        p = skia.Paint(Color=theme.color(theme.MAGENTA_MID if self.held else theme.CYAN_DIM))
        canvas.drawRect(r, p)
        text.draw(canvas, "PROBE", r.left() + 8, text.baseline_in(r, theme.FONT_SIZE - 2),
                  theme.FONT_SIZE - 2, theme.color(theme.FG_STRONG), bold=True)

    def set_hovered(self, value):
        if value != self.hovered:
            log.info("probe: hovered %s", value)
        super().set_hovered(value)

    def press(self, button, x, y):
        log.info("probe: press %d at %.0f,%.0f", button, x, y)
        self.moves, self.held = 0, True
        self.invalidate()
        return True

    def motion(self, x, y):
        if self.held:
            self.moves += 1
            self.last = (x, y)

    def release(self, button, x, y):
        log.info("probe: release %d at %.0f,%.0f after %d motions", button, x, y, self.moves)
        self.held = False
        self.invalidate()

    def cancel(self):
        log.info("probe: cancel")
        self.held = False

    def click(self, button, x=0.0, y=0.0):
        log.info("probe: click %d", button)
        return True



class Dump:
    """Where everything on the canvas is, and what an open input or
    menu holds, written for the test five times a second (only when it
    changed); and the canvas's state, the call's, its agent's."""

    last = None

    @classmethod
    def write(cls):
        from indigoshell.core.daemon import get_daemon
        d = get_daemon()
        scene = d.canvas.scene
        out = {}
        for name, win in d.instances.items():
            widgets = []
            for w in win.content.walk():
                r = w.rect
                item = {"type": type(w).__name__, "x": win.x + r.left(), "y": win.y + r.top(),
                        "w": r.width(), "h": r.height()}
                if type(w).__name__ == "Workspaces":
                    item.update(stride=w._stride(), count=w._count)
                if type(w).__name__ == "TabsItem":
                    item.update(pitch=w.pitch, first=w.first, ids=list(w.ids))
                if type(w).__name__ == "_TrayRow":
                    item.update(bus=w.item.bus_name, title=w.item.title)
                if type(w).__name__ == "AgentChip":
                    item.update(state=w.state, strength=round(w.strength(), 2))
                widgets.append(item)
            out[name] = {"x": win.x, "y": win.y, "w": win.width, "h": win.height,
                         "cx": win.content_root[0], "cy": win.content_root[1],
                         "cw": win.content_root[2], "ch": win.content_root[3],
                         "phase": win._phase, "widgets": widgets,
                         "shows": scene.shows(win), "blur": list(win.blur_rects())}
            prompt = getattr(win.content, "prompt", None)
            if prompt is not None and getattr(prompt, "open", False):
                out[name]["prompt"] = _prompt(prompt)
            you = getattr(win.content, "you_plate", None)
            if you is not None:
                out[name]["typed"] = you.input.text
        p = APPS.prompt
        if p is not None and p.open:
            out["_apps_prompt"] = _prompt(p)
        f = APPS.focused
        out["_apps"] = {"focused": [f.kind, f.app_id, f.link.pid] if f else None,
                        "windows": [[w.kind, w.app_id, w.link.pid, w.active_seq, w.wm_id]
                                    for w in APPS.windows]}
        c = d.canvas
        out["_canvas"] = {"fullscreen": c.fullscreen, "hidden": scene.hidden,
                          "surface": c.surface is not None, "kb": c._kb_sent,
                          "blur": [list(r) for r in (c._blur_sent or ())],
                          "can_blur": c.can_blur, "frames_late": c.frames_late}
        from indigoshell.services.systray import get_broker
        out["_tray"] = [i.title for i in get_broker().items()]
        out["_agent"] = {"busy": HUB.busy, "ringing": HUB.ringing, "call_open": HUB.call_open,
                         "phase": HUB.phase, "said": [m.get("text") for m in HUB.sent
                                                      if m.get("type") == "say"],
                         "attached": [((m.get("attach") or [{}])[0].get("text") or "")[:300]
                                      for m in HUB.sent if m.get("type") == "say"]}
        key = json.dumps(out, sort_keys=True)
        if key != cls.last:
            cls.last = key
            with open(DUMP + ".tmp", "w") as f:
                f.write(key)
            os.replace(DUMP + ".tmp", DUMP)


def _prompt(p):
    """An open input's or menu's text and rows, for the test."""
    box = getattr(p, "box", None)
    text = box.field.text if box is not None else getattr(p.level, "filter", None)
    selected = p.level.selected if p.what == "menu" and p.level is not None else p.selected
    return {"what": p.what, "text": text, "selected": selected,
            "rows": [str(r.get("title")) for r in p.list_rows()][:20]}


def _script():
    """What the test asks of the shell's insides, a JSON line each in
    DUMP + ".do": the stand-in hub's events (`do`: a ScriptedAgent
    method and its `args`), or `blur` (whether the canvas asks for its
    blur: the test's A/B of what the compositor's blur does)."""
    path = DUMP + ".do"
    try:
        with open(path) as f:
            f.seek(_script.at)
            lines = f.read()
    except OSError:
        return
    _script.at += len(lines.encode())
    for line in lines.splitlines():
        if not line.strip():
            continue
        step = json.loads(line)
        log.info("probe: script %s", step)
        if "do" in step:
            getattr(HUB, step["do"])(*step.get("args", ()))
        elif step.get("tray") == "toggle":
            from indigoshell.core.daemon import get_daemon
            bar = get_daemon().instances["bar"]
            next(w for w in bar.content.walk() if type(w).__name__ == "Systray")._toggle_panel()
        elif "blur" in step:
            from indigoshell.core.daemon import get_daemon
            c = get_daemon().canvas
            c.can_blur = bool(step["blur"])
            c._blur_sent = None
            c.pending_state = True
            c.scene.damage_all()        # a frame to carry it


_script.at = 0


async def _dumping():
    try:
        _script.at = os.path.getsize(DUMP + ".do")     # a shell started again: what is new
    except OSError:
        pass

    async def loop():
        while True:
            await asyncio.sleep(0.2)
            try:
                _script()
            except Exception:
                log.exception("probe: the script failed")
            try:
                Dump.write()
            except Exception:
                log.exception("probe: the layout dump failed")
    asyncio.get_running_loop().create_task(loop())


WINDOWS = [top_bar(Probe()) if spec.name == TOP_BAR else spec for spec in WINDOWS]
STARTUP = [*STARTUP, _dumping]


# The call as this machine's config has it (config_default's `call`),
# its hub stood in for: tests/_live.py's ScriptedAgent, the hub's events
# as the test scripts them; it connects nowhere (no line is started).
import shutil as _shutil  # noqa: E402
import sys as _sys  # noqa: E402

import indigoshell as _pkg  # noqa: E402

_sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_pkg.__file__)), "tests"))
_state_home = os.environ["XDG_STATE_HOME"]
from _live import ScriptedAgent  # noqa: E402

_shutil.rmtree(os.environ["XDG_STATE_HOME"], ignore_errors=True)   # _live's own
os.environ["XDG_STATE_HOME"] = _state_home

from indigoshell.config_default import call  # noqa: E402


class _Hub(ScriptedAgent):
    def start(self):
        pass                            # no line: the test plays the hub


HUB = _Hub()
CALL, CALL_SERVICES = call(HUB)
WINDOWS = [*WINDOWS, CALL]
SERVICES = [*SERVICES, *CALL_SERVICES]


# A chord menu of the test's own: its items only say they ran.
from indigoshell.plugin import Item, Menu  # noqa: E402


def _said(n):
    def run():
        log.info("probe: menu item %d ran", n)
    return run


MENUS = {**MENUS, "wltest": Menu("wltest", "WLTEST", items=[
    Item("PROBE ONE", run=_said(1)), Item("PROBE TWO", run=_said(2))])}


# Timings: when a window is asked for and each canvas present, on one clock.
import time as _time  # noqa: E402

from indigoshell.backend import wayland as _wayland  # noqa: E402
from indigoshell.core.daemon import Daemon as _Daemon  # noqa: E402

_open = _Daemon.open


def _timed_open(self, name, *args, **kw):
    t0 = _time.monotonic()
    try:
        return _open(self, name, *args, **kw)
    finally:
        log.debug("probe: t=%.4f open %s took %.2f ms", t0, name,
                 (_time.monotonic() - t0) * 1000)


_present = _wayland.Canvas._present


def _timed_present(self):
    t0 = _time.monotonic()
    before = len(self.scene.stats.presents)
    _present(self)
    if len(self.scene.stats.presents) > before:
        ms, painted, damaged = self.scene.stats.presents[-1]
        log.debug("probe: t=%.4f present %.2f ms, %d px", t0, ms, damaged)


_Daemon.open = _timed_open
_wayland.Canvas._present = _timed_present

if os.environ.get("INDIGOSHELL_TEST_UNPACED"):
    # The other side of an A/B: presents not paced on frame callbacks.
    _wayland.Canvas.ask_frame = lambda self: None

# The stand-in app (the test's own process on the apps socket) has no
# window in niri for the shell to follow: it is taken as focused whenever
# no real app's window is (niri's focus decides for Flux, section 4f).
import types as _types  # noqa: E402

_STAND_IN = int(os.environ.get("INDIGOSHELL_TEST_STAND_IN_PID") or 0)
_focused_app = type(APPS)._focused_app


def _focused_or_stand_in(self):
    window = _focused_app(self)
    if window is None:
        window = next((w for w in reversed(self.windows)
                       if w.link.pid == _STAND_IN and w.active_seq), None)
    return window


APPS._focused_app = _types.MethodType(_focused_or_stand_in, APPS)
'''


# What the launcher finds and starts: a kitty in the nested niri, its
# title the test's scratch name (so it can be found and stopped).
LAUNCHED_ENTRY = """[Desktop Entry]
Type=Application
Name=Zqxwv Nested Probe
Exec=kitty --class wltest.launched -T {tag} -o linux_display_server=wayland -o confirm_os_window_close=0 sleep 900
"""


# A screen of blocks in hard-edged colours, the same every time: the
# terminal's size read once its window has settled (maximised).
BUSY = r"""
import random, shutil, time
time.sleep(1.5)
cols, rows = shutil.get_terminal_size()
random.seed(7)
for _ in range(rows - 1):
    print("".join(f"\033[3{random.randint(1, 7)}m" + random.choice("\u2588\u2588 \u2580")
                  for _ in range(cols)) + "\033[0m")
"""


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


class Poker:
    """niri's virtual pointer, in this process (zwlr_virtual_pointer_v1):
    events that exist only inside the nested compositor."""

    BTN_LEFT = 0x110

    def __init__(self, wayland_display: str) -> None:
        from pywayland.client import Display

        from indigoshell.backend import wlproto
        self.wl = wl = wlproto.load()
        self.display = Display(wayland_display)
        self.display.connect()
        reg = self.display.get_registry()
        found: dict[str, tuple[int, int]] = {}
        reg.dispatcher["global"] = lambda _r, n, i, v: found.__setitem__(i, (n, v))
        self.display.roundtrip()
        self.seat = reg.bind(found["wl_seat"][0], wl.wayland.WlSeat, 1)
        out = reg.bind(found["wl_output"][0], wl.wayland.WlOutput, 1)
        self.size = [0, 0]

        def mode(_o, flags, w, h, _refresh):
            if flags & 1:
                self.size[:] = [w, h]

        out.dispatcher["mode"] = mode       # again when the output is resized
        out.dispatcher["geometry"] = lambda *a: None
        self.output = out
        mgr = reg.bind(found["zwlr_virtual_pointer_manager_v1"][0],
                       wl.wlr_virtual_pointer_unstable_v1.ZwlrVirtualPointerManagerV1, 1)
        self.display.roundtrip()
        self.vp = mgr.create_virtual_pointer(self.seat)
        self.display.roundtrip()
        self.xy = (0, 0)

    @staticmethod
    def _ms() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def move(self, x: float, y: float, settle: float = 0.08) -> None:
        self.xy = (x, y)
        self.vp.motion_absolute(self._ms(), int(x), int(y), self.size[0], self.size[1])
        self.vp.frame()
        self.display.roundtrip()
        time.sleep(settle)

    def button(self, pressed: bool, button: int = BTN_LEFT, settle: float = 0.08) -> None:
        self.vp.button(self._ms(), button, 1 if pressed else 0)
        self.vp.frame()
        self.display.roundtrip()
        time.sleep(settle)

    def click(self, x: float, y: float, button: int = BTN_LEFT) -> None:
        self.move(x, y)
        self.button(True, button)
        self.button(False, button)

    def drag(self, x0, y0, x1, y1, steps: int = 12) -> None:
        self.move(x0, y0)
        self.button(True)
        for i in range(1, steps + 1):
            self.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, settle=0.03)
        time.sleep(0.1)
        self.button(False)

    def wheel(self, steps: int) -> None:
        """Vertical wheel clicks: negative up, positive down."""
        self.vp.axis_source(0)                  # wheel
        for _ in range(abs(steps)):
            d = 1 if steps > 0 else -1
            self.vp.axis_discrete(self._ms(), 0, 15.0 * d, d)
            self.vp.frame()
        self.display.roundtrip()
        time.sleep(0.15)

    # ── the keyboard: niri's virtual keyboard (zwp_virtual_keyboard_v1) ──
    def keyboard(self, layout: str = "us") -> None:
        """A virtual keyboard with `layout`'s keymap (compiled here by
        xkbcommon); niri hands that keymap to the focused client before
        its keys."""
        from indigoshell.backend.wlkeys import Keys
        wl = self.wl
        if getattr(self, "kb", None) is None:
            reg = self.display.get_registry()
            found: dict[str, tuple[int, int]] = {}
            reg.dispatcher["global"] = lambda _r, n, i, v: found.__setitem__(i, (n, v))
            self.display.roundtrip()
            mgr = reg.bind(found["zwp_virtual_keyboard_manager_v1"][0],
                           wl.virtual_keyboard_unstable_v1.ZwpVirtualKeyboardManagerV1, 1)
            self.kb = mgr.create_virtual_keyboard(self.seat)
        self.keys = Keys()
        self.keys.load_names(layout)
        text = self.keys.keymap.get_as_bytes()
        text = (text if isinstance(text, bytes) else str(text).encode()) + b"\0"
        fd = os.memfd_create("keymap", os.MFD_CLOEXEC)
        try:
            os.write(fd, text)
            self.kb.keymap(1, fd, len(text))       # WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1
        finally:
            os.close(fd)
        self.mods = 0
        self.kb.modifiers(0, 0, 0, 0)
        self.display.roundtrip()

    def key(self, code: int, pressed: bool, settle: float = 0.02) -> None:
        self.kb.key(self._ms(), code, 1 if pressed else 0)
        self.display.roundtrip()
        time.sleep(settle)

    def tap(self, code: int, *mods: str, settle: float = 0.05) -> None:
        """Press and release evdev `code` with `mods` ("shift", "ctrl",
        "alt", "super") held: their keys down first, the modifier state
        sent, as a real keyboard's would be."""
        mod_codes = {"shift": 42, "ctrl": 29, "alt": 56, "super": 125}
        for m in mods:
            self.key(mod_codes[m], True, 0.0)
        mask = self.keys.mask(*mods)
        if mask:
            self.kb.modifiers(mask, 0, 0, 0)
        self.key(code, True, 0.03)
        self.key(code, False, 0.0)
        if mask:
            self.kb.modifiers(0, 0, 0, 0)
        for m in reversed(mods):
            self.key(mod_codes[m], False, 0.0)
        self.display.roundtrip()
        time.sleep(settle)

    def burst(self, chord: tuple, text: str) -> None:
        """`chord` (evdev code, modifiers...) and then `text`, every key
        sent at once with one round trip at the end: niri hands them all
        to the focused client before anyone can move the keyboard (the
        type-ahead after an app's chord)."""
        mod_codes = {"shift": 42, "ctrl": 29, "alt": 56, "super": 125}
        code, *mods = chord
        for m in mods:
            self.kb.key(self._ms(), mod_codes[m], 1)
        mask = self.keys.mask(*mods)
        self.kb.modifiers(mask, 0, 0, 0)
        self.kb.key(self._ms(), code, 1)
        self.kb.key(self._ms(), code, 0)
        self.kb.modifiers(0, 0, 0, 0)
        for m in reversed(mods):
            self.kb.key(self._ms(), mod_codes[m], 0)
        for ch in text:
            found = self.keys.code_for(ch)
            if found is None:
                raise ValueError(f"no key types {ch!r} in this layout")
            key, shift = found
            if shift:
                self.kb.modifiers(self.keys.mask("shift"), 0, 0, 0)
            self.kb.key(self._ms(), key, 1)
            self.kb.key(self._ms(), key, 0)
            if shift:
                self.kb.modifiers(0, 0, 0, 0)
        self.display.roundtrip()
        time.sleep(0.05)

    def type(self, text: str, settle: float = 0.04) -> None:
        for ch in text:
            found = self.keys.code_for(ch)
            if found is None:
                raise ValueError(f"no key types {ch!r} in this layout")
            code, shift = found
            self.tap(code, *(("shift",) if shift else ()), settle=settle)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if getattr(self, "kb", None) is not None:
                self.kb.destroy()
            self.vp.destroy()
            self.display.roundtrip()
            self.display.disconnect()


class Nested:
    def __init__(self, user_env: dict, group: str) -> None:
        self.user_env = user_env
        self.group = group
        # Short: unix socket paths are, and the shell's sockets live here.
        self.tmp = tempfile.mkdtemp(prefix="wl-niri-", dir="/tmp")
        self.run = os.path.join(self.tmp, "run")
        self.shots = os.path.join(self.tmp, "shots")
        os.makedirs(self.run, mode=0o700)
        os.makedirs(self.shots)
        self.launcher_key = os.path.join(self.tmp, "launcher-key.sh")
        self.call_key = os.path.join(self.tmp, "call-key.sh")
        self.rule = None
        self.wrapper = None             # dbus-run-session
        self.niri_pid = None
        self.env: dict = {}
        self.procs: list[subprocess.Popen] = []

    # qtile, through its own client, on the user's session (read-mostly:
    # a rule of ours, added and removed)
    def qtile(self, *args) -> str:
        return subprocess.run([shutil.which("python3") or "python3", "-c", QTILE, *args],
                              env=self.user_env, capture_output=True, text=True,
                              timeout=10).stdout.strip()

    def where(self) -> tuple[str | None, int | None]:
        """qtile's group of our niri's X window, and the window."""
        out = subprocess.run(["qtile", "cmd-obj", "-o", "root", "-f", "windows"],
                             env=self.user_env, capture_output=True, text=True, timeout=10)
        try:
            windows = json.loads(out.stdout)
        except ValueError:
            return None, None
        for w in windows:
            prop = subprocess.run(["xprop", "-id", str(w["id"]), "_NET_WM_PID"],
                                  env=self.user_env, capture_output=True, text=True).stdout.split()
            if prop and prop[-1] == str(self.niri_pid):
                return str(w.get("group")), w["id"]
        return None, None

    def config(self) -> str:
        """The user's niri config as niri reads it, minus what it starts;
        a plain one when there is none."""
        path = os.path.join(self.user_env.get("XDG_CONFIG_HOME")
                            or os.path.expanduser("~/.config"), "niri", "config.kdl")
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            lines = ["prefer-no-csd", "animations { off; }"]
        out = [line for line in lines
               if not re.match(r"\s*(spawn-at-startup|spawn-sh-at-startup|screenshot-path)\b",
                               line)]
        # The launcher's key: the user's Mod+D (fuzzel) replaced by the
        # nested shell's own `toggle launcher` (a script of the test's,
        # written once the shell's environment is known).
        out = [line for line in out if not re.match(r"\s*Mod\+[DA]\s*(\{|\S+=)", line)]
        binds = [f'    Mod+D {{ spawn "{self.launcher_key}"; }}',
                 f'    Mod+A {{ spawn "{self.call_key}"; }}']
        at = next((i for i, line in enumerate(out) if re.match(r"\s*binds\s*\{", line)), None)
        if at is None:
            out += ["binds {", *binds, "}"]
        else:
            out[at + 1:at + 1] = binds
        # The agent's window rule of docs/niri.kdl, as the user applies it.
        with open(os.path.join(REPO, "docs", "niri.kdl")) as f:
            rule = re.search(r"^window-rule \{\n.*?^\}", f.read(), re.S | re.M)
        if rule:
            out += rule[0].splitlines()
        out += [f'screenshot-path "{self.shots}/shot-%H%M%S.png"',
                "xwayland-satellite {", "    off", "}",
                "hotkey-overlay {", "    skip-at-startup", "}"]
        if any(re.match(r"\s*hotkey-overlay\b", line) for line in lines):
            out = out[:-3]
        cfg = os.path.join(self.tmp, "niri.kdl")
        with open(cfg, "w") as f:
            f.write("\n".join(out) + "\n")
        return cfg

    def start(self, check) -> bool:
        cfg = self.config()
        ok = subprocess.run(["niri", "validate", "-c", cfg], capture_output=True, text=True)
        if not check("the nested niri's config (the user's, minus what it starts) is valid",
                     ok.returncode == 0, ok.stderr[-400:]):
            return False
        self.rule = self.qtile("add", self.group)
        env = {k: v for k, v in self.user_env.items()
               if k not in ("WAYLAND_DISPLAY", "NIRI_SOCKET", "WAYLAND_SOCKET",
                            "DBUS_SESSION_BUS_ADDRESS")}
        env.update(XDG_RUNTIME_DIR="/proc/self/cwd/run",
                   XDG_DATA_HOME=os.path.join(self.tmp, "niri-data"),
                   XDG_CACHE_HOME=os.path.join(self.tmp, "niri-cache"),
                   XDG_STATE_HOME=os.path.join(self.tmp, "niri-state"),
                   TMPDIR=self.tmp)
        self.wrapper = subprocess.Popen(
            ["dbus-run-session", "--", "niri", "-c", cfg], cwd=self.tmp, env=env,
            stdout=open(os.path.join(self.tmp, "niri.log"), "w"), stderr=subprocess.STDOUT,
            start_new_session=True)
        group = wid = None
        for _ in range(150):
            time.sleep(0.1)
            if self.niri_pid is None:
                kids = subprocess.run(["pgrep", "-P", str(self.wrapper.pid), "-x", "niri"],
                                      capture_output=True, text=True).stdout.split()
                self.niri_pid = int(kids[0]) if kids else None
            if self.niri_pid is not None:
                group, wid = self.where()
            if group is not None or self.wrapper.poll() is not None:
                break
        if not check(f"the nested niri's window is on qtile group {self.group} "
                     f"(rule {self.rule})", group == self.group, f"group {group}"):
            self.kill_niri()
            return False
        self.wid = wid
        base = f"/proc/{self.niri_pid}/cwd/run"
        sock = None
        for _ in range(50):
            names = os.listdir(self.run)
            sock = next((n for n in names if n.startswith("niri.") and n.endswith(".sock")),
                        None)
            wl = next((n for n in names if n.startswith("wayland-")
                       and not n.endswith(".lock")), None)
            if sock and wl:
                break
            time.sleep(0.1)
        self.env = dict(env, XDG_RUNTIME_DIR=base, WAYLAND_DISPLAY=f"{base}/{wl}",
                        NIRI_SOCKET=f"{base}/{sock}")
        self.env.pop("DISPLAY", None)
        return True

    def output_size(self) -> tuple[int, int]:
        """The nested output's logical size now: it follows the niri
        window, which qtile lays out anew when its group is shown."""
        out = next(iter((self.msg("outputs") or {}).values()), {})
        return (out.get("logical", {}).get("width", 0),
                out.get("logical", {}).get("height", 0))

    def fullscreen_in_qtile(self) -> None:
        self.qtile("fullscreen", str(self.wid))

    def msg(self, *args):
        out = subprocess.run(["niri", "msg", "--json", *args], env=self.env,
                             capture_output=True, text=True, timeout=10)
        try:
            return json.loads(out.stdout) if out.stdout.strip() else None
        except ValueError:
            return None

    def action(self, *args) -> None:
        subprocess.run(["niri", "msg", "action", *args], env=self.env,
                       capture_output=True, timeout=10)

    def spawn(self, argv, log, env=None) -> subprocess.Popen:
        p = subprocess.Popen(argv, env=env or self.env, cwd=self.tmp, start_new_session=True,
                             stdout=open(os.path.join(self.tmp, log), "w"),
                             stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def kitty(self, name, command: str | None = None) -> subprocess.Popen:
        command = command or f"printf 'nested kitty {name}\\n'; sleep 900"
        return self.spawn(["kitty", "--class", f"wltest.{name}", "-T", f"wl test {name}",
                           "-o", "linux_display_server=wayland", "-o", "confirm_os_window_close=0",
                           "sh", "-c", command], f"kitty-{name}.log")

    def recorder(self, name, busy: bool = False) -> str:
        """A kitty whose terminal reports clicks (xterm's mode 1000) and
        writes them and every key typed into it, raw, to a file: what
        reached that window. `busy`: its screen full of hard-edged
        coloured blocks first (BUSY), something a blur is seen on."""
        path = os.path.join(self.tmp, f"typed-{name}")
        open(path, "wb").close()
        first = f"python3 -c {shlex_quote(BUSY)}; " if busy else ""
        self.kitty(name, f"{first}stty raw -echo; printf '\\033[?1000h'; exec cat > {path}")
        return path

    def screenshot(self, name: str, keep: str | None):
        """niri's own screenshot of its output, as an RGBA array."""
        import skia
        path = os.path.join(self.shots, f"{name}.png")
        self.action("screenshot-screen", "--show-pointer", "false", "--path", path)
        for _ in range(50):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.1)
        time.sleep(0.1)
        img = skia.Image.open(path)
        if keep:
            shutil.copy(path, os.path.join(keep, f"{name}.png"))
        return img.toarray(colorType=skia.kRGBA_8888_ColorType,
                           alphaType=skia.kUnpremul_AlphaType), path

    def reap_launched(self) -> None:
        """Stop what the launcher started (LAUNCHED_ENTRY: its title is
        this run's scratch name), should it outlive the nested niri."""
        tag = os.path.basename(self.tmp).encode()
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if b"wltest.launched" in argv and tag in argv:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(int(pid), signal.SIGTERM)

    def kill_niri(self) -> None:
        if self.wrapper is not None and self.wrapper.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.wrapper.pid, signal.SIGTERM)
            try:
                self.wrapper.wait(5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.wrapper.pid, signal.SIGKILL)
                self.wrapper.wait(5)

    def stop(self, check) -> None:
        for p in self.procs:
            if p.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGTERM)
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
        self.kill_niri()
        if self.rule:
            removed = self.qtile("remove", self.rule)
            check(f"our qtile rule {self.rule} removed", removed in ("True", "None", ""),
                  removed)
        left = self.qtile("group", self.group)
        check(f"qtile group {self.group} empty again", left in ("[]", ""), left)


class Shell:
    """The shell under test, in the nested niri."""

    def __init__(self, nested: Nested, raster: bool) -> None:
        self.nested = nested
        self.home = os.path.join(nested.tmp, "shell")
        self.dump = os.path.join(self.home, "layout.json")
        self.log_path = os.path.join(nested.tmp, "shell.log")
        env = dict(nested.env)
        for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            env[var] = os.path.join(self.home, var.lower())
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "indigoshell"))
        with open(os.path.join(env["XDG_CONFIG_HOME"], "indigoshell", "config.py"), "w") as f:
            f.write(TEST_CONFIG)
        apps = os.path.join(env["XDG_DATA_HOME"], "applications")
        os.makedirs(apps)
        with open(os.path.join(apps, "wltest-launched.desktop"), "w") as f:
            f.write(LAUNCHED_ENTRY.format(tag=os.path.basename(nested.tmp)))
        env.update(PYTHONPATH=REPO, PULSE_SERVER=f"unix:{nested.tmp}/no-pulse",
                   INDIGOSHELL_TEST_DUMP=self.dump, PYTHONDONTWRITEBYTECODE="1",
                   INDIGOSHELL_TEST_STAND_IN_PID=str(os.getpid()))
        env.pop("DISPLAY", None)
        if raster:
            env["INDIGOSHELL_GPU"] = "0"
        self.env = env
        self.proc = None
        self.pid = None

    def paths(self) -> dict:
        code = ("import json; from indigoshell.core.paths import socket_path, lock_path, "
                "config_dir, state_dir; from indigoshell.applink.protocol import socket_path as a;"
                " print(json.dumps([socket_path(), lock_path(), a(), config_dir(), state_dir()]))")
        out = subprocess.run([sys.executable, "-B", "-c", code], env=self.env,
                             capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)

    def start(self, level: str = "debug") -> None:
        self.proc = subprocess.Popen(
            ["dbus-run-session", "--", sys.executable, "-B", os.path.join(REPO, "main.py"),
             "--log-level", level],
            env=self.env, cwd=self.nested.tmp, start_new_session=True,
            stdout=open(self.log_path, "w"), stderr=subprocess.STDOUT)
        for _ in range(100):
            kids = subprocess.run(["pgrep", "-P", str(self.proc.pid)], capture_output=True,
                                  text=True).stdout.split()
            py = [int(k) for k in kids if "python" in _comm(int(k))]
            if py:
                self.pid = py[0]
                break
            time.sleep(0.05)

    def log(self) -> str:
        with open(self.log_path, errors="replace") as f:
            return f.read()

    def wait_log(self, pattern: str, timeout: float = 20.0, since: int = 0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.search(pattern, self.log()[since:], re.M)
            if m:
                return m
            if self.proc.poll() is not None:
                return None
            time.sleep(0.1)
        return None

    def layout(self) -> dict:
        try:
            with open(self.dump) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def widget(self, bar: str, kind: str) -> dict | None:
        return next((w for w in self.layout().get(bar, {}).get("widgets", [])
                     if w["type"] == kind), None)

    def client(self, *argv) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", os.path.join(REPO, "main.py"), *argv],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def cpu_ticks(self) -> int:
        with open(f"/proc/{self.pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])        # utime + stime

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.client("kill")
        try:
            self.proc.wait(8)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)


def write_launcher_key(nested: "Nested", shell: "Shell",
                       verb: tuple[str, ...] = ("toggle", "launcher"),
                       path: str | None = None) -> None:
    """The nested niri's Mod+D (or, at `path`, Mod+A's `toggle call`):
    the nested shell's own client, with the nested shell's whole
    environment and nothing else (env -i), so it reaches that daemon and
    never the live one."""
    import shlex
    path = path or nested.launcher_key
    env = " ".join(shlex.quote(f"{k}={v}") for k, v in shell.env.items())
    main = shlex.quote(os.path.join(REPO, "main.py"))
    out = shlex.quote(os.path.join(nested.tmp, "launcher-key.log"))
    with open(path, "w") as f:
        f.write(f"#!/bin/sh\nexec env -i {env} {shlex.quote(sys.executable)} -B {main} "
                f"{' '.join(verb)} >> {out} 2>&1\n")
    os.chmod(path, 0o700)


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


class TrayItem:
    """A StatusNotifierItem of the test's on the nested shell's own bus:
    a title, a menu of two rows (com.canonical.dbusmenu), and what the
    shell calls on it recorded in `calls`. dbus-fast, on a loop in a
    thread of its own; registered with the shell's watcher."""

    TITLE = "Zqx Tray Probe"
    ROWS = ((1, "Probe Alpha"), (2, "Probe Beta"))

    def __init__(self, bus_address: str) -> None:
        import asyncio
        import threading
        self.address = bus_address
        self.calls: list[tuple] = []
        self.error = None
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(10)

    def _run(self) -> None:
        import asyncio
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as e:
            self.error = e
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()

    async def _start(self) -> None:
        from dbus_fast import Message, PropertyAccess, Variant
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, dbus_property, method

        calls = self.calls
        title, rows = self.TITLE, self.ROWS

        class Item(ServiceInterface):
            def __init__(self):
                super().__init__("org.kde.StatusNotifierItem")

            @dbus_property(access=PropertyAccess.READ)
            def Id(self) -> "s":
                return "zqx-tray-probe"

            @dbus_property(access=PropertyAccess.READ)
            def Title(self) -> "s":
                return title

            @dbus_property(access=PropertyAccess.READ)
            def Status(self) -> "s":
                return "Active"

            @dbus_property(access=PropertyAccess.READ)
            def Menu(self) -> "o":
                return "/MenuBar"

            @dbus_property(access=PropertyAccess.READ)
            def ItemIsMenu(self) -> "b":
                return True

            @method()
            def Activate(self, x: "i", y: "i"):
                calls.append(("Activate", x, y))

            @method()
            def ContextMenu(self, x: "i", y: "i"):
                calls.append(("ContextMenu", x, y))

        class Menu(ServiceInterface):
            def __init__(self):
                super().__init__("com.canonical.dbusmenu")

            @dbus_property(access=PropertyAccess.READ)
            def Version(self) -> "u":
                return 3

            @method()
            def AboutToShow(self, _id: "i") -> "b":
                return False

            @method()
            def GetLayout(self, _parent: "i", _depth: "i", _names: "as") -> "u(ia{sv}av)":
                kids = [Variant("(ia{sv}av)", [i, {"label": Variant("s", label)}, []])
                        for i, label in rows]
                return [1, [0, {"children-display": Variant("s", "submenu")}, kids]]

            @method()
            def Event(self, item: "i", event: "s", _data: "v", _ts: "u"):
                calls.append(("Event", item, event))

        bus = await MessageBus(bus_address=self.address).connect()
        self.bus = bus
        bus.export("/StatusNotifierItem", Item())
        bus.export("/MenuBar", Menu())
        await bus.call(Message(destination="org.kde.StatusNotifierWatcher",
                               path="/StatusNotifierWatcher",
                               interface="org.kde.StatusNotifierWatcher",
                               member="RegisterStatusNotifierItem", signature="s",
                               body=[bus.unique_name]))

    def close(self) -> None:
        def stop():
            bus = getattr(self, "bus", None)
            if bus is not None:
                bus.disconnect()
            self.loop.stop()
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(stop)
        self.thread.join(5)


def shell_bus(shell: "Shell", user_env: dict) -> str | None:
    """The nested shell's own D-Bus (from its environment), never the
    session's."""
    try:
        with open(f"/proc/{shell.pid}/environ", "rb") as f:
            env = dict(v.split(b"=", 1) for v in f.read().split(b"\0") if b"=" in v)
    except OSError:
        return None
    bus = env.get(b"DBUS_SESSION_BUS_ADDRESS", b"").decode()
    return bus if bus and bus != user_env.get("DBUS_SESSION_BUS_ADDRESS") else None


class App:
    """A stand-in app on the shell's apps socket: one window with tabs."""

    def __init__(self, path: str) -> None:
        from indigoshell.applink import bar as B, protocol
        self.B, self.protocol = B, protocol
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.settimeout(0.1)
        self.buf = b""
        self.got: list[dict] = []

    def send(self, msg: dict) -> None:
        self.sock.sendall(self.protocol.encode(msg))

    def announce(self, wid: int, titles) -> None:
        """As a Wayland app announces: no handle, then `active` (the
        test config takes it as focused while no real app is)."""
        B = self.B
        self.send(B.announce("standin", wid))
        self.send(B.active(True))
        tabs = [B.tab(101 + i, t) for i, t in enumerate(titles)]
        self.send(B.bar([B.tabs("tabs", tabs, current=101, can=("close", "move", "detach"))],
                        [B.text("host", spans=[B.span("example.org")], opens="pick")],
                        menus={"pick": B.menu([B.row(1, "Alpha row"), B.row(2, "Beta row"),
                                               B.row(3, "Gamma row")])}))

    def read(self, wait: float = 0.5) -> list[dict]:
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                msg = self.protocol.decode(line)
                if msg is not None:
                    self.got.append(msg)
        return self.got

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def _frames(log: str) -> list[tuple[float, int, int]]:
    """(ms, damaged px, painted px) of each canvas frame in a log."""
    return [(float(m[1]), int(m[2]), int(m[3])) for m in re.finditer(
        r"canvas frame: ([\d.]+) ms, damage (\d+) px in \d+ rect\(s\) \([^)]*\), "
        r"painted (\d+) px", log)]


def _paints(log: str, name: str) -> list[tuple[float, int]]:
    return [(float(m[1]), int(m[2])) for m in re.finditer(
        rf"region {re.escape(name)} painted in ([\d.]+) ms \((\d+) px\)", log)]


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else float("nan")


def run(args, check, section, user_env) -> None:
    from indigoshell import theme

    group = "7" if args.full else "8"
    section(f"4. the shell in a nested niri (qtile group {group})")
    missing = [t for t in ("niri", "kitty", "dbus-run-session", "qtile", "xprop", "pgrep")
               if shutil.which(t) is None]
    if missing or not user_env.get("DISPLAY"):
        print(f"  (skipped: needs an X session with qtile, and {missing or 'DISPLAY'})")
        return
    nested = Nested(user_env, group)
    if group == "7":
        left = nested.qtile("group", "7")
        if left not in ("[]", ""):
            print(f"  (skipped: qtile group 7 is not empty: {left})")
            return
    shell = poker = app = None
    keep = args.shots
    if keep:
        os.makedirs(keep, exist_ok=True)
    try:
        if not nested.start(check):
            return
        if args.full:
            nested.fullscreen_in_qtile()
        time.sleep(1.0)
        W, H = nested.output_size()
        print(f"  (the nested output: {W}x{H})", flush=True)

        shell = Shell(nested, args.raster)
        live_runtime = user_env.get("XDG_RUNTIME_DIR") or "/tmp"
        paths = shell.paths()
        uid = os.getuid()
        live = {os.path.join(live_runtime, f"indigoshell-{uid}.sock"),
                os.path.join(live_runtime, f"indigoshell-{uid}.lock"),
                os.path.join(live_runtime, f"indigoshell-apps-{uid}.sock")}
        check("the nested shell's socket, lock, apps socket, config and state are its own "
              "(the nested runtime and scratch directories), none of the live shell's",
              all(p.startswith(nested.env["XDG_RUNTIME_DIR"]) for p in paths[:3])
              and all(p.startswith(shell.home) for p in paths[3:])
              and not live & set(paths), str(paths))
        check("... no DISPLAY, the nested compositor by absolute path, NIRI_SOCKET the "
              "nested one", "DISPLAY" not in shell.env
              and shell.env["WAYLAND_DISPLAY"].startswith("/proc/")
              and shell.env["NIRI_SOCKET"].startswith(nested.env["XDG_RUNTIME_DIR"]))

        kitty_a = nested.kitty("a")
        shell.start()
        ready = shell.wait_log(r"ready \(pid \d+\)", 40)
        log = shell.log()
        check("the shell starts on the nested niri", ready is not None, log[-2000:])
        if ready is None:
            return
        gpu = re.search(r"canvas: (\d+)x(\d+), (.*)$", log, re.M)
        check("it chose Wayland (layer-shell offered) and sized its canvas to the output",
              "Wayland display" in log and gpu is not None
              and (int(gpu[1]), int(gpu[2])) == (W, H), gpu[0] if gpu else log[-1500:])
        renderer = gpu[3] if gpu else ""
        if args.raster:
            check("--raster: drawn on the CPU into wl_shm", renderer == "raster", renderer)
        else:
            check("drawn by the GPU, not a software rasteriser", renderer.startswith("GPU")
                  and not re.search(r"llvmpipe|softpipe|swrast", renderer, re.I), renderer)
            print(f"  ({renderer})", flush=True)
        line = re.search(r"canvas draws (.*); not on Wayland yet: (.*)$", log, re.M)
        check("one line names what the canvas draws: the bars, the panels, the chord menu, "
              "the launcher, the drop-down, the call; nothing left out",
              line is not None and set(line[1].split(", ")) == {
                  "bar", "topbar", "system", "hardware", "network", "menu", "launcher",
                  "dropdown", "call"} and line[2] == "none", line[0] if line else "")
        warmed = re.search(r"canvas: (\d+) effect chain\(s\) and the hologram warmed on the "
                           r"GPU in (\d+) ms", log)
        if not args.raster:
            check("the effects' and the hologram's shaders warmed at start",
                  warmed is not None and int(warmed[1]) >= 1, warmed[0] if warmed else "")
            if warmed:
                print(f"  (shaders warmed in {warmed[2]} ms)", flush=True)
        blur = re.search(r"canvas: the compositor can blur behind the canvas", log)
        check("niri offers ext-background-effect's blur, and the canvas binds it",
              blur is not None and "no background blur" not in log)
        keymap = re.search(r"keyboard: keymap with (.*)$", log, re.M)
        check("the keymap niri sent read by xkbcommon (the user's layouts)",
              keymap is not None, keymap[0] if keymap else "")
        write_launcher_key(nested, shell)
        write_launcher_key(nested, shell, ("toggle", "call"), nested.call_key)
        check("no errors or tracebacks in its log so far",
              "Traceback" not in log and " ERROR " not in log,
              "\n".join(ln for ln in log.splitlines() if "ERROR" in ln or "Trace" in ln)[:800])

        layers = nested.msg("layers") or []
        names = {(ly.get("namespace"), ly.get("layer")) for ly in layers}
        check("its layer surfaces: the canvas on the overlay layer, the two reservations "
              "on the bottom layer",
              ("indigoshell", "Overlay") in names
              and ("indigoshell-reserve-top", "Bottom") in names
              and ("indigoshell-reserve-bottom", "Bottom") in names, str(names))

        def kitty_window(cls):
            return next((w for w in nested.msg("windows") or []
                         if w.get("app_id") == cls), None)

        for _ in range(100):
            if kitty_window("wltest.a"):
                break
            time.sleep(0.1)
        time.sleep(0.8)
        ka = kitty_window("wltest.a")
        bh, th = theme.BAR_HEIGHT, theme.TOP_BAR_HEIGHT
        if ka is not None:
            lay = ka["layout"]
            tile_h = lay["tile_size"][1]
            gaps = H - bh - th - tile_h
            check("a kitty tiles between the reservations: its tile the output's height less "
                  "both bars and niri's gaps", 0 <= gaps <= 64,
                  f"output {H}, tile {lay['tile_size']}, bars {th}+{bh}")
        else:
            check("a kitty window in the nested niri", False, "none came")

        for _ in range(50):
            if shell.widget("bar", "Workspaces") and shell.widget("topbar", "Probe"):
                break
            time.sleep(0.1)
        img, path = nested.screenshot("bars", keep)
        bg = [int(theme.BAR_BG[i:i + 2], 16) for i in (1, 3, 5)]
        probe_rgb = [int(theme.CYAN_DIM[i:i + 2], 16) for i in (1, 3, 5)]

        def drawn(img):
            """(the probe's colour over its rect, the bottom strip's ink):
            what only the canvas draws (kitty's background is the bars')."""
            p = shell.widget("topbar", "Probe")
            if p is None:
                return 0.0, 0.0
            r = img[int(p["y"]) + 2:int(p["y"] + p["h"]) - 2,
                    int(p["x"]) + 2:int(p["x"] + p["w"]) - 2, :3].astype(int)
            share_probe = float((abs(r - probe_rgb).max(axis=-1) <= 12).mean())
            strip = img[H - bh + 2:H - 2, :, :3].astype(int)
            ink = float((abs(strip - bg).max(axis=-1) > 24).mean())
            return share_probe, ink

        probe_share, ink = drawn(img)
        check("niri's screenshot: both bars drawn on their strips (the probe on the top "
              "one, the widgets' ink on the bottom one)", probe_share > 0.4 and ink > 0.02,
              f"probe {probe_share:.2f} ink {ink:.3f} ({path})")
        shots = {"bars": path}

        poker = Poker(nested.env["WAYLAND_DISPLAY"])
        probe = shell.widget("topbar", "Probe")
        mark = len(shell.log())
        if probe:
            px, py = probe["x"] + probe["w"] / 2, probe["y"] + probe["h"] / 2
            poker.move(px, py)
            poker.move(px + 2, py + 1)
            hovered = shell.wait_log(r"probe: hovered True", 3, mark)
            poker.move(px, H / 2)
            unhovered = shell.wait_log(r"probe: hovered False", 3, mark)
            check("hover: the probe lit when the pointer comes over it, and let go when it "
                  "leaves the bar for a window", hovered is not None and unhovered is not None,
                  shell.log()[mark:][-600:])
            mark = len(shell.log())
            poker.drag(px, py, px - 300, py + 300)
            pressed = shell.wait_log(r"probe: press 1 at", 3, mark)
            released = shell.wait_log(r"probe: release 1 at (-?\d+),(-?\d+) after (\d+) motions",
                                      3, mark)
            check("press, motion, release: the probe holds the press, its motions follow the "
                  "pointer past the bar's edge (over a window), and the release comes there",
                  pressed is not None and released is not None and int(released[3]) >= 8
                  and int(released[2]) > th + 100,
                  shell.log()[mark:][-800:])
            mark = len(shell.log())
            poker.move(px, py)
            poker.wheel(-1)
            check("the wheel on the probe: a click of button 4 (X's scroll up)",
                  shell.wait_log(r"probe: click 4", 3, mark) is not None,
                  shell.log()[mark:][-400:])
        else:
            check("the probe's place on the top bar written by the test config", False,
                  str(shell.layout())[:400])

        ws = shell.widget("bar", "Workspaces")
        if ws:
            def focused_ws():
                return next((w["idx"] for w in nested.msg("workspaces") or []
                             if w.get("is_focused")), None)
            before = focused_ws()
            wx = ws["x"] + ws["stride"] * 1.5
            wy = ws["y"] + ws["h"] / 2
            poker.click(wx, wy)
            after = None
            for _ in range(30):
                after = focused_ws()
                if after == 2:
                    break
                time.sleep(0.1)
            check("a click on the bottom bar's second workspace: niri shows workspace 2 "
                  "(through indigoshell.wm's niri backend)", after == 2, f"{before} -> {after}")
            img2, path2 = nested.screenshot("workspace-2", keep)
            shots["workspace-2"] = path2
            poker.move(wx, wy)
            poker.wheel(-1)
            back = None
            for _ in range(30):
                back = focused_ws()
                if back == 1:
                    break
                time.sleep(0.1)
            check("the wheel up over the workspaces: back to workspace 1", back == 1, str(back))
        else:
            check("the workspaces' place on the bottom bar", False, str(shell.layout())[:400])

        # A second kitty: the pointer between the bars belongs to the windows.
        # It records every key and click that reaches it (section 2a).
        recorder = nested.recorder("b")
        for _ in range(100):
            if kitty_window("wltest.b"):
                break
            time.sleep(0.1)
        time.sleep(0.8)

        def focused_app():
            return (nested.msg("focused-window") or {}).get("app_id")

        poker.click(W * 0.25, H / 2)
        first = focused_app()
        poker.click(W * 0.75, H / 2)
        second = focused_app()
        check("clicks between the bars reach the windows (kitty a, then kitty b focused)",
              (first, second) == ("wltest.a", "wltest.b"), f"{first} {second}")
        poker.click(W * 0.25, th / 2)
        on_bar = focused_app()
        poker.click(W * 0.25, H - bh / 2)
        on_bottom = focused_app()
        check("a click on either bar over kitty a is the shell's: kitty b stays focused",
              on_bar == "wltest.b" and on_bottom == "wltest.b", f"{on_bar} {on_bottom}")

        # The stand-in app's tabs on the top bar, and one dragged.
        app = App(os.path.join(nested.env["XDG_RUNTIME_DIR"], f"indigoshell-apps-{uid}.sock"))
        app.announce(0x400001, ["Alpha", "Beta", "Gamma"])
        app.read(1.0)
        tabs = None
        for _ in range(40):
            tabs = shell.widget("topbar", "TabsItem")
            if tabs and tabs.get("ids"):
                break
            time.sleep(0.1)
        check("the stand-in app attached and its tabs shown on the top bar",
              any(m.get("type") == "attached" for m in app.got) and tabs is not None
              and tabs.get("ids") == [101, 102, 103], f"{app.got[:2]} {tabs}")
        if tabs and tabs.get("ids"):
            pitch, left = tabs["pitch"], tabs["x"]
            ty = tabs["y"] + tabs["h"] / 2
            img3, path3 = nested.screenshot("tabs", keep)
            shots["tabs"] = path3
            poker.drag(left + pitch * 0.5, ty, left + pitch * 1.8, ty, steps=16)
            got = app.read(1.0)
            moves = [m for m in got if m.get("type") == "move"]
            check("the first tab dragged past the second: the app is told to move it before "
                  "the third (press, motion and release routed on the canvas)",
                  moves and moves[-1].get("tab") == 101 and moves[-1].get("before") == 103,
                  str(got[-4:]))

        ctx = {"nested": nested, "shell": shell, "poker": poker, "app": app,
               "check": check, "W": W, "H": H, "keep": keep, "shots": shots,
               "recorder": recorder, "drawn": drawn}
        if getattr(args, "only", None) == "flux":
            ctx["timings"] = {}
            flux(ctx)
            app = ctx["app"]
            log = shell.log()
            check("no errors or tracebacks in its log", "Traceback" not in log
                  and " ERROR " not in log,
                  "\n".join(ln for ln in log.splitlines() if "ERROR" in ln or "Trace" in ln)[:800])
            print("  screenshots: " + ", ".join(f"{k} {v}" for k, v in shots.items()), flush=True)
            return
        overlays(ctx)
        calls(ctx)
        chip(ctx)
        tray(ctx)
        nested.action("focus-workspace", "1")
        time.sleep(0.5)
        log = shell.log()
        check("no errors or tracebacks after the call, the chip and the tray",
              "Traceback" not in log and " ERROR " not in log,
              "\n".join(ln for ln in log.splitlines() if "ERROR" in ln or "Trace" in ln)[:800])
        if nested.output_size() != (W, H):
            W, H = nested.output_size()
            print(f"  (the nested output is {W}x{H} now: qtile laid its window out anew)",
                  flush=True)
            time.sleep(1.0)

        # A fullscreen window takes the canvas away; it comes back after.
        poker.click(W * 0.75, H / 2)
        mark = len(shell.log())
        nested.action("fullscreen-window")
        gone = shell.wait_log(r"the canvas goes away", 5, mark)
        time.sleep(0.6)
        layers = {(ly.get("namespace"), ly.get("layer")) for ly in nested.msg("layers") or []}
        img4, path4 = nested.screenshot("fullscreen", keep)
        shots["fullscreen"] = path4
        fs_probe, fs_ink = drawn(img4)
        check("a fullscreen kitty: the canvas surface is gone (niri can scan out the "
              "window), the reservations stay, no bar on screen",
              gone is not None and ("indigoshell", "Overlay") not in layers
              and ("indigoshell-reserve-top", "Bottom") in layers
              and fs_probe < 0.05 and fs_ink < 0.005,
              f"{layers} probe {fs_probe:.2f} ink {fs_ink:.3f}")
        ctx.update(W=W, H=H)
        over_fullscreen(ctx)
        mark = len(shell.log())
        nested.action("fullscreen-window")
        back = shell.wait_log(r"the canvas is back", 5, mark)
        frame_back = shell.wait_log(r"canvas frame: ([\d.]+) ms, damage (\d+) px", 5,
                                    back.end() + mark if back else mark)
        time.sleep(0.6)
        layers = {(ly.get("namespace"), ly.get("layer")) for ly in nested.msg("layers") or []}
        img5, path5 = nested.screenshot("after-fullscreen", keep)
        shots["after-fullscreen"] = path5
        back_probe, back_ink = drawn(img5)
        check("out of fullscreen: the canvas made again and drawn whole, both bars back",
              back is not None and frame_back is not None
              and int(frame_back[2]) == W * H and ("indigoshell", "Overlay") in layers
              and back_probe > 0.4 and back_ink > 0.02,
              f"{layers} {frame_back[0] if frame_back else None} probe {back_probe:.2f} "
              f"ink {back_ink:.3f}")

        # Flux itself (the stand-in app goes first).
        flux(ctx)
        app = ctx["app"]
        nested.action("focus-workspace", "1")
        time.sleep(0.5)

        # Timings and idle.
        poker.move(W * 0.75, H / 2)
        time.sleep(1.0)
        t0, c0, m0 = time.monotonic(), shell.cpu_ticks(), len(shell.log())
        time.sleep(10.0)
        t1, c1 = time.monotonic(), shell.cpu_ticks()
        idle_log = shell.log()[m0:]
        hz = os.sysconf("SC_CLK_TCK")
        cpu = (c1 - c0) / hz / (t1 - t0) * 100
        idle_frames = _frames(idle_log)
        print(f"  (idle 10 s: {cpu:.1f}% of a core, {len(idle_frames)} canvas frames, "
              f"{len(idle_frames) / (t1 - t0):.1f}/s)", flush=True)
        idle_top = _paints(idle_log, "topbar")
        check("idle: the top bar (nothing on it moves) not repainted at all; the bottom bar's "
              "frames only its moving parts (INDIGO's 12 fps colour pulse, the clock, the "
              "meters): at most ~16 a second", not idle_top
              and len(idle_frames) / (t1 - t0) <= 16 and cpu < 15,
              f"{cpu:.1f}% {len(idle_frames)} frames, top bar {len(idle_top)}")
        log = shell.log()
        frames = _frames(log)
        full = [f for f in frames if f[1] == W * H]
        bar_paints = _paints(log, "bar")
        top_paints = _paints(log, "topbar")
        bar_area = W * bh
        partial_bar = [f for f in idle_frames if f[1] < bar_area]
        whole_bar = [f for f in idle_frames if f[1] == bar_area]
        small = [p for p in bar_paints if p[1] < bar_area]
        print(f"  (canvas frames: {len(frames)}; whole-canvas ones median "
              f"{_median(f[0] for f in full):.2f} ms; idle frames partial {len(partial_bar)} "
              f"median {_median(f[0] for f in partial_bar):.2f} ms, whole bottom bar "
              f"{len(whole_bar)} median {_median(f[0] for f in whole_bar):.2f} ms)", flush=True)
        print(f"  (region paints: bar {len(bar_paints)}, small (clock-like) ones median "
              f"{_median(p[0] for p in small):.2f} ms, whole-bar ones median "
              f"{_median(p[0] for p in bar_paints if p[1] == bar_area):.2f} ms; topbar "
              f"{len(top_paints)})", flush=True)
        check("partial redraws: idle frames damage only parts of the bottom bar, never the "
              "whole canvas", idle_frames and all(f[1] <= bar_area for f in idle_frames)
              and not any(f[1] == W * H for f in idle_frames), str(idle_frames[:6]))
        log = shell.log()
        check("no errors or tracebacks in its log", "Traceback" not in log
              and " ERROR " not in log,
              "\n".join(ln for ln in log.splitlines() if "ERROR" in ln or "Trace" in ln)[:800])
        print("  screenshots: " + ", ".join(f"{k} {v}" for k, v in shots.items()), flush=True)

        # Idle again, logging at info: debug logs two lines a frame.
        shell.stop()
        summary = re.search(r"canvas: (\d+ presents in .*)$", shell.log(), re.M)
        if summary:
            print(f"  ({summary[1]})", flush=True)
        if keep:
            shutil.copy(shell.log_path, os.path.join(keep, "shell-debug.log"))
        shell.start("info")
        if shell.wait_log(r"ready \(pid \d+\)", 40) is not None:
            time.sleep(3.0)
            t0, c0 = time.monotonic(), shell.cpu_ticks()
            time.sleep(10.0)
            t1, c1 = time.monotonic(), shell.cpu_ticks()
            cpu = (c1 - c0) / hz / (t1 - t0) * 100
            print(f"  (idle 10 s at log level info: {cpu:.1f}% of a core)", flush=True)
            # Most of it is the widgets' own: the frame clock's tick and
            # their paint at INDIGO's 12 fps pulse, as on X11.
            check("idle at log level info: well under a fifth of a core", cpu < 15, f"{cpu:.1f}%")
    finally:
        if app is not None:
            app.close()
        if poker is not None:
            poker.close()
        if shell is not None:
            shell.stop()
            log = shell.log() if os.path.exists(shell.log_path) else ""
            summary = re.search(r"canvas: (\d+ presents in .*)$", log, re.M)
            if summary:
                print(f"  ({summary[1]})", flush=True)
            if keep:
                shutil.copy(shell.log_path, os.path.join(keep, "shell.log"))
        nested.reap_launched()
        nested.stop(check)
        if keep:
            with contextlib.suppress(OSError):
                shutil.copy(os.path.join(nested.tmp, "niri.log"), os.path.join(keep, "niri.log"))
        shutil.rmtree(nested.tmp, ignore_errors=True)


# ── phase 2a: the panels, the menu, the launcher, the drop-down, toasts ──
def _wait(pred, timeout: float = 5.0, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


def _timings(log: str, name: str, after: int = 0) -> dict | None:
    """The open of `name` after log offset `after`: how long open()
    took, when the first present came after it, and the presents of
    the spawn (its 0.22 s and a little more)."""
    m = re.search(rf"probe: t=([\d.]+) open {re.escape(name)} took ([\d.]+) ms", log[after:])
    if m is None:
        return None
    t0, took = float(m[1]), float(m[2])
    presents = [(float(p[1]), float(p[2])) for p in re.finditer(
        r"probe: t=([\d.]+) present ([\d.]+) ms", log[after:])]
    spawn = [(t, ms) for t, ms in presents if t0 <= t <= t0 + 0.35]
    if not spawn:
        return {"took": took, "first": None, "frames": 0, "median": 0.0, "max": 0.0}
    return {"took": took, "first": (spawn[0][0] - t0) * 1000, "frames": len(spawn),
            "median": _median(ms for _t, ms in spawn), "max": max(ms for _t, ms in spawn)}


def _say_timing(what: str, t: dict | None) -> str:
    if not t:
        return f"{what}: no timing"
    return (f"{what}: open() {t['took']:.1f} ms, first frame on the canvas "
            f"{t['first']:.1f} ms after the ask, {t['frames']} frames in the spawn's "
            f"0.35 s, present median {t['median']:.2f} ms, max {t['max']:.2f} ms"
            if t["first"] is not None else f"{what}: open() {t['took']:.1f} ms, no frame")


def overlays(ctx) -> None:
    """The kinds phase 2a puts on the canvas, in the nested niri: the
    launcher from niri's Mod+D taking the keyboard (typing, the
    keymap's layouts, repeat, Escape, an outside click swallowed, a
    launch), the system panel (Escape) and the hardware panel (an
    outside click), the chord menu, the top bar's drop-down for an
    app's menu, a toast; what each costs to open."""
    from indigoshell import theme
    nested, shell, poker, app = ctx["nested"], ctx["shell"], ctx["poker"], ctx["app"]
    check, W, H, keep, shots = ctx["check"], ctx["W"], ctx["H"], ctx["keep"], ctx["shots"]
    rec = ctx["recorder"]                   # kitty b's: what reached it
    bh, th = theme.BAR_HEIGHT, theme.TOP_BAR_HEIGHT
    timings = {}

    def lay():
        return shell.layout()

    def idle(name):
        return _wait(lambda: (lay().get(name) or {}).get("phase") == "idle" and lay()[name])

    def gone(name):
        return _wait(lambda: name not in lay())

    def kb():
        return next((ly.get("keyboard_interactivity") for ly in nested.msg("layers") or []
                     if ly.get("namespace") == "indigoshell"), None)

    def kb_is(mode):
        return _wait(lambda: kb() == mode, 3.0) and kb()

    def got():
        with open(rec, "rb") as f:
            return f.read()

    def prompt(name):
        return (lay().get(name) or {}).get("prompt") or {}

    def text_is(name, want):
        return _wait(lambda: prompt(name).get("text") == want, 3.0)

    def outside_of(box):
        """A point on kitty b's half of the output, clear of `box`."""
        x = max(box["cx"] + box["cw"] + 20, W * 0.8)
        return min(x, W - 10), H / 2

    if nested.output_size() != (W, H):
        W, H = nested.output_size()         # qtile laid the niri window out anew
        print(f"  (the nested output is {W}x{H} now)", flush=True)
        time.sleep(1.0)
    ctx["W"], ctx["H"] = W, H
    poker.keyboard("us")
    # Kitty b (the recorder) has the focus: everything it gets is kept.
    poker.click(W * 0.75, H / 2)
    time.sleep(0.3)
    start = len(got())

    # ── the launcher, from niri's Mod+D ────────────────────────────────
    # niri's virtual keyboard goes to the focused client and never
    # through niri's bindings, so the binding's command is run as niri
    # runs it for the key: `spawn`.
    def mod_d():
        nested.action("spawn", "--", nested.launcher_key)

    mark = len(shell.log())
    mod_d()
    win = idle("launcher")
    focus = shell.wait_log(r"keyboard: focus on the canvas", 3, mark)
    check("Mod+D (niri's binding, the nested shell's `toggle launcher`): the launcher opens "
          "centred, the canvas asks for the keyboard (exclusive) and has the focus",
          bool(win) and abs(win["cx"] + win["cw"] / 2 - W / 2) <= 1
          and abs(win["cy"] + win["ch"] / 2 - H / 2) <= 1 and kb() == "Exclusive"
          and focus is not None, f"{win and {k: win[k] for k in ('cx', 'cy', 'cw', 'ch')}} "
          f"kb {kb()} focus {focus is not None}")
    timings["launcher (first open)"] = _timings(shell.log(), "launcher", mark)
    img, path = nested.screenshot("launcher", keep)
    shots["launcher"] = path
    if win:
        bg = [int(theme.POPUP_BG[i:i + 2], 16) for i in (1, 3, 5)]
        box = img[int(win["cy"]) + 30:int(win["cy"] + win["ch"]) - 30,
                  int(win["cx"]) + 30:int(win["cx"] + win["cw"]) - 30, :3].astype(int)
        share = float((abs(box - bg).max(axis=-1) <= 40).mean())
        check("niri's screenshot: the launcher's plate drawn on the canvas", share > 0.5,
              f"{share:.2f} ({path})")

    mark = len(shell.log())
    poker.type("zqxw")
    typed = text_is("launcher", "zqxw")
    rows = prompt("launcher").get("rows") or []
    check("typing filters the list: the query is what was typed, the test's app first",
          bool(typed) and rows[:1] == ["Zqxwv Nested Probe"], str(prompt("launcher")))
    check("... and none of it reached the window that had the focus", got()[start:] == b"",
          repr(got()[start:]))
    paints = _paints(shell.log()[mark:], "launcher")
    full = (win["w"] * win["h"]) if win else 0
    check("each key repainted only part of the launcher (its query and list), never the "
          "whole canvas", paints and all(px < full for _ms, px in paints)
          and all(f[1] < W * H for f in _frames(shell.log()[mark:])),
          f"{paints[:6]} of {full}")
    poker.tap(30, "ctrl")                   # Ctrl+A: to the start
    poker.type("x")
    check("Ctrl+A is a command, not text: no \\x01, the caret at the start",
          bool(text_is("launcher", "xzqxw")), str(prompt("launcher")))
    def clear():
        poker.tap(18, "ctrl")               # Ctrl+E: to the end
        poker.tap(22, "ctrl")               # Ctrl+U: all of it before the caret

    clear()
    text_is("launcher", "")
    poker.key(30, True)                     # `a` held for 1.2 s
    time.sleep(1.2)
    poker.key(30, False)
    time.sleep(0.4)
    held = prompt("launcher").get("text") or ""
    check("a held key repeats by our own timer at niri's repeat_info (600 ms, then 25/s)",
          set(held) == {"a"} and 10 <= len(held) <= 30, repr(held))
    clear()
    text_is("launcher", "")
    poker.keyboard("tr")
    poker.type("ıİ")
    poker.keyboard("us,tr")
    poker.kb.modifiers(0, 0, 0, 1)          # the second group: Turkish
    poker.key(23, True)
    poker.key(23, False)
    poker.kb.modifiers(0, 0, 0, 0)
    check("the keymap niri sends is the one read: Turkish ı and İ, and ı again in a "
          "us,tr keymap's second group", bool(text_is("launcher", "ıİı")),
          str(prompt("launcher")))
    poker.keyboard("us")
    mark = len(shell.log())
    poker.tap(1)                            # Escape
    closed = gone("launcher")
    mode = kb_is("None")
    left = shell.wait_log(r"keyboard: focus left the canvas", 3, mark)
    poker.type("ok")
    after = _wait(lambda: got()[start:].endswith(b"ok"), 3.0)
    check("Escape closes it, the keyboard is given back (none) and the window has it again: "
          "what is typed next reaches it", bool(closed) and mode == "None"
          and left is not None and bool(after), f"kb {kb()} {got()[start:]!r}")

    # An outside click: closes it, and goes no further.
    mod_d()
    win = idle("launcher")
    rects = [ly for ly in nested.msg("layers") or [] if ly.get("namespace") == "indigoshell"]
    before = len(got())
    if win:
        poker.click(*outside_of(win))
    closed = gone("launcher")
    time.sleep(0.4)
    swallowed = got()[before:] == b""
    poker.click(*outside_of(win or {"cx": 0, "cw": 0}))
    reached = _wait(lambda: b"\x1b[M" in got()[before:], 3.0)
    check("a click outside the open launcher closes it and is swallowed (the window under "
          "it saw nothing); the next click reaches the window", bool(win) and bool(closed)
          and swallowed and bool(reached), f"{rects} {got()[before:]!r}")

    # A launch: Return starts the selected app, in the nested session.
    mod_d()
    idle("launcher")
    poker.type("zqxw")
    text_is("launcher", "zqxw")
    mark = len(shell.log())
    poker.tap(28)                           # Return
    launched = _wait(lambda: next((w for w in nested.msg("windows") or []
                                   if w.get("app_id") == "wltest.launched"), None), 10.0)
    check("Return launches the selected app into the nested niri and the launcher closes",
          launched is not None and bool(gone("launcher"))
          and shell.wait_log(r"launching wltest-launched", 2, mark) is not None,
          shell.log()[mark:][-600:])
    if launched:
        nested.action("close-window", "--id", str(launched["id"]))
        _wait(lambda: not any(w.get("app_id") == "wltest.launched"
                              for w in nested.msg("windows") or []), 5.0)
    mark = len(shell.log())
    mod_d()
    idle("launcher")
    timings["launcher (reopen)"] = _timings(shell.log(), "launcher", mark)
    mod_d()
    check("Mod+D's command again (`toggle`) closes it and lets the keyboard go",
          bool(gone("launcher")) and kb_is("None") == "None", f"kb {kb()}")

    # ── the system panel: Escape ───────────────────────────────────────
    poker.move(W * 0.75, H / 2)
    for n in (1, 2):
        mark = len(shell.log())
        shell.client("toggle", "system")
        win = idle("system")
        timings[f"system panel ({'first open' if n == 1 else 'reopen'})"] = _timings(
            shell.log(), "system", mark)
        if n == 1:
            img, path = nested.screenshot("system-panel", keep)
            shots["system-panel"] = path
            check("the system panel opens bottom left over the bar, as on X (margin: the "
                  "bar's height and the popup gap), and takes the keyboard",
                  bool(win) and win["cx"] == theme.BAR_MARGIN
                  and win["cy"] + win["ch"] == H - bh - theme.POPUP_GAP
                  and kb_is("Exclusive") == "Exclusive",
                  f"{win and (win['cx'], win['cy'], win['cw'], win['ch'])} kb {kb()}")
        poker.tap(1)
        check(f"Escape closes the system panel ({n})", bool(gone("system"))
              and kb_is("None") == "None", f"kb {kb()}")

    # ── the hardware panel: a click on the bar opens it, outside closes ─
    brackets = shell.widget("bar", "Brackets")
    if brackets:
        poker.click(brackets["x"] + brackets["w"] / 2, brackets["y"] + brackets["h"] / 2)
        win = idle("hardware")
        layers = nested.msg("layers") or []
        check("a click on the bar's meters opens the hardware panel bottom right; it takes no "
              "keyboard (none on X either)", bool(win) and kb() == "None"
              and win["cx"] + win["cw"] == W - theme.BAR_MARGIN,
              f"{win and (win['cx'], win['cw'])} kb {kb()}")
        before = len(got())
        poker.click(W * 0.75, th + 40)      # on kitty b, above the panel
        closed = gone("hardware")
        time.sleep(0.3)
        check("a click outside it closes it, swallowed", bool(closed) and got()[before:] == b"",
              repr(got()[before:]))
    else:
        check("the bar's meters (Brackets) in the layout", False, str(lay().get("bar"))[:300])

    # ── the chord menu ─────────────────────────────────────────────────
    mark = len(shell.log())
    shell.client("menu", "wltest")
    win = idle("menu")
    timings["chord menu (first open)"] = _timings(shell.log(), "menu", mark)
    check("`menu wltest`: the chord menu bottom right above the bar, keyboard taken",
          bool(win) and kb_is("Exclusive") == "Exclusive", f"{win} kb {kb()}")
    poker.tap(3)                            # "2": armed on the press, run on the release
    ran = shell.wait_log(r"probe: menu item 2 ran", 3, mark)
    check("a digit picks its row (armed on the press, run on the release) and the menu "
          "closes", ran is not None and bool(gone("menu")) and kb_is("None") == "None",
          shell.log()[mark:][-500:])
    shell.client("menu", "wltest")
    idle("menu")
    poker.tap(1)
    check("Escape closes the menu", bool(gone("menu")) and kb_is("None") == "None")
    shell.client("menu", "wltest")
    win = idle("menu")
    before = len(got())
    poker.click(W * 0.75, th + 40)
    check("an outside click closes the menu, swallowed", bool(gone("menu"))
          and got()[before:] == b"", repr(got()[before:]))

    # ── the top bar's drop-down: the stand-in app's menu ───────────────
    host = next((w for w in lay().get("topbar", {}).get("widgets", [])
                 if w["type"] == "TextItem"), None)
    if host is None:
        host = next((w for w in lay().get("topbar", {}).get("widgets", [])
                     if "Text" in w["type"]), None)
    if host:
        hx, hy = host["x"] + host["w"] / 2, host["y"] + host["h"] / 2
        mark = len(shell.log())
        poker.click(hx, hy)
        drop = idle("dropdown")
        menu = _wait(lambda: lay().get("_apps_prompt"), 3.0) or {}
        timings["drop-down (first open)"] = _timings(shell.log(), "dropdown", mark)
        img, path = nested.screenshot("dropdown", keep)
        shots["dropdown"] = path
        check("a click on the app's host opens its menu: the list drops flush under the top "
              "bar, the keyboard taken", bool(drop) and drop["cy"] == th
              and menu.get("rows") == ["Alpha row", "Beta row", "Gamma row"]
              and kb_is("Exclusive") == "Exclusive",
              f"{drop and (drop['cx'], drop['cy'], drop['cw'], drop['ch'])} {menu} kb {kb()}")
        poker.type("gam")
        filtered = _wait(lambda: (lay().get("_apps_prompt") or {}).get("rows") == ["Gamma row"],
                         3.0)
        check("typing filters the drop-down's rows", bool(filtered),
              str(lay().get("_apps_prompt")))
        app.read(0.1)
        n0 = len(app.got)
        poker.tap(28)                       # Return: picked
        picked = _wait(lambda: [m for m in app.read(0.2)[n0:] if m.get("type") != "attached"],
                       3.0)
        check("Return picks the row: the app is told, the drop-down goes, the keyboard is "
              "given back", bool(picked) and bool(gone("dropdown"))
              and kb_is("None") == "None", f"{app.got[n0:][-3:]} kb {kb()}")
        poker.click(hx, hy)
        idle("dropdown")
        poker.tap(1)
        check("Escape closes the drop-down", bool(gone("dropdown")) and kb_is("None") == "None")
        poker.click(hx, hy)
        idle("dropdown")
        before = len(got())
        poker.click(W * 0.75, H / 2)
        check("an outside click closes it, swallowed", bool(gone("dropdown"))
              and not lay().get("_apps_prompt") and got()[before:] == b"",
              repr(got()[before:]))
    else:
        check("the stand-in app's host on the top bar", False,
              str([w["type"] for w in lay().get("topbar", {}).get("widgets", [])]))

    # ── a toast ────────────────────────────────────────────────────────
    bus = None
    with contextlib.suppress(OSError):
        with open(f"/proc/{shell.pid}/environ", "rb") as f:
            env = dict(v.split(b"=", 1) for v in f.read().split(b"\0") if b"=" in v)
        bus = env.get(b"DBUS_SESSION_BUS_ADDRESS", b"").decode()
    if bus and bus != nested.user_env.get("DBUS_SESSION_BUS_ADDRESS"):
        mark = len(shell.log())
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications", "--method",
             "org.freedesktop.Notifications.Notify", "wltest", "0", "", "Nested toast",
             "from the Wayland test", "[]", "{}", "2500"],
            env={"PATH": os.environ.get("PATH", "/usr/bin"), "DBUS_SESSION_BUS_ADDRESS": bus},
            capture_output=True, text=True, timeout=10)
        name = None
        m = re.search(r"\(uint32 (\d+),\)", out.stdout)
        if m:
            name = f"notif-{m[1]}"
        toast = idle(name) if name else None
        timings["toast"] = _timings(shell.log(), name or "?", mark)
        if toast:
            img, path = nested.screenshot("toast", keep)
            shots["toast"] = path
        check("a notification on the shell's own bus: its toast bottom right (the "
              "notification offsets), above the windows",
              bool(toast) and toast["cx"] + toast["cw"] == W - theme.NOTIF_OFFSET_X
              and toast["cy"] + toast["ch"] == H - theme.NOTIF_OFFSET_Y,
              f"{out.stdout.strip()} {out.stderr.strip()[-200:]} "
              f"{toast and (toast['cx'], toast['cy'], toast['cw'], toast['ch'])}")
        check("... and it goes by itself when its time is up",
              bool(name) and bool(_wait(lambda: name not in lay(), 6.0)))
    else:
        check("the shell's own D-Bus, for a toast", False, str(bus))

    log = shell.log()
    check("no errors or tracebacks after the panels, menus and toasts",
          "Traceback" not in log and " ERROR " not in log,
          "\n".join(ln for ln in log.splitlines() if "ERROR" in ln or "Trace" in ln)[:800])
    for what, t in timings.items():
        print(f"  ({_say_timing(what, t)})", flush=True)
    ctx["timings"] = {}


# ── phase 2b: the call, its blur, the agent chip, the tray ─────────────
class _Look:
    """What the phase 2b checks read of the nested shell and niri."""

    def __init__(self, ctx) -> None:
        self.nested, self.shell = ctx["nested"], ctx["shell"]

    def lay(self) -> dict:
        return self.shell.layout()

    def win(self, name) -> dict | None:
        return self.lay().get(name)

    def idle(self, name, timeout: float = 5.0):
        return _wait(lambda: (self.win(name) or {}).get("phase") == "idle" and self.win(name),
                     timeout)

    def gone(self, name, timeout: float = 5.0) -> bool:
        return bool(_wait(lambda: name not in self.lay(), timeout))

    def widget(self, name, kind) -> dict | None:
        return next((w for w in (self.win(name) or {}).get("widgets", [])
                     if w["type"] == kind), None)

    def widgets(self, name, kind) -> list[dict]:
        return [w for w in (self.win(name) or {}).get("widgets", []) if w["type"] == kind]

    def kb(self):
        return next((ly.get("keyboard_interactivity") for ly in self.nested.msg("layers") or []
                     if ly.get("namespace") == "indigoshell"), None)

    def kb_is(self, mode):
        return _wait(lambda: self.kb() == mode, 3.0) and self.kb()

    def overlay(self) -> bool:
        return any(ly.get("namespace") == "indigoshell" and ly.get("layer") == "Overlay"
                   for ly in self.nested.msg("layers") or [])

    def canvas(self) -> dict:
        return self.lay().get("_canvas") or {}

    def agent(self) -> dict:
        return self.lay().get("_agent") or {}

    def do(self, **step) -> None:
        with open(self.shell.dump + ".do", "a") as f:
            f.write(json.dumps(step) + "\n")

    def paste(self, primary: bool = False) -> str:
        out = subprocess.run(["wl-paste", "-n", *(["--primary"] if primary else [])],
                             env=self.nested.env, capture_output=True, text=True, timeout=10)
        return out.stdout


def _union(rects) -> tuple[int, int, int, int] | None:
    """The bounds (x0, y0, x1, y1) of (x, y, w, h) rects."""
    rects = [r for r in rects if r]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects))


def _edges(img, box) -> float:
    """How sharp a box of a screenshot is: the mean step between
    neighbouring pixels along its rows (the plates' raster runs along
    them, so it adds nothing)."""
    import numpy as np
    x0, y0, x1, y1 = (int(v) for v in box)
    px = img[y0:y1, x0:x1, :3].astype(int)
    return float(np.abs(np.diff(px, axis=1)).mean()) if px.size else 0.0


def calls(ctx) -> None:
    """The call on the canvas, over a busy window on a workspace of its
    own: Mod+A (niri's binding, the nested shell's `toggle call`), its
    place, the keyboard, typing and a line said, the hub's reply growing
    the agent's plate and the blur region with it, the blur seen in
    niri's screenshot (against the same with the canvas's blur off),
    text selected and copied, and every way to hang up."""
    from indigoshell import theme
    nested, shell, poker = ctx["nested"], ctx["shell"], ctx["poker"]
    check, keep, shots, timings = ctx["check"], ctx["keep"], ctx["shots"], ctx["timings"]
    look = _Look(ctx)
    W, H = nested.output_size()
    ctx["W"], ctx["H"] = W, H

    def mod_a():
        nested.action("spawn", "--", nested.call_key)

    nested.action("focus-workspace", "3")
    busy = nested.recorder("busy", busy=True)
    win = _wait(lambda: next((w for w in nested.msg("windows") or []
                              if w.get("app_id") == "wltest.busy"), None), 10.0)
    nested.action("maximize-column")
    time.sleep(2.8)                         # BUSY prints once the window has settled
    poker.click(W * 0.3, H / 2)

    def got():
        with open(busy, "rb") as f:
            return f.read()

    under, _ = nested.screenshot("call-under", None)
    start = len(got())
    mark = len(shell.log())
    mod_a()
    call = look.idle("call")
    timings["call (first open)"] = _timings(shell.log(), "call", mark)
    m_right = theme.BAR_MARGIN + 12
    check("Mod+A (niri's binding, the nested shell's `toggle call`): the call opens top right, "
          "16 px under the top bar (its margins), takes the keyboard (exclusive) and the focus",
          bool(win) and bool(call) and call["cx"] + call["cw"] == W - m_right
          and call["cy"] == theme.TOP_BAR_HEIGHT + 16 and look.kb() == "Exclusive"
          and shell.wait_log(r"keyboard: focus on the canvas", 3, mark) is not None,
          f"{call and (call['cx'], call['cy'], call['cw'], call['ch'])} kb {look.kb()}")
    blur = look.canvas().get("blur") or []
    own = (call or {}).get("blur") or []
    steps = [r for r in blur if r[3] == 1]
    check("the blur region sent to niri: the call's plates and chips, stair-stepped at their "
          "bevels, inside the call; nothing else blurred",
          bool(blur) and len(steps) >= 8 and sorted(map(tuple, blur)) == sorted(map(tuple, own))
          and _union(blur)[2] <= call["cx"] + call["cw"]
          and _union(blur)[1] >= call["cy"], f"{len(blur)} rects, {len(steps)} steps")
    img, path = nested.screenshot("call", keep)
    shots["call"] = path
    plate = look.widget("call", "AgentPlate")
    sharp = blurred = flat = 0.0
    if plate:
        box = (plate["x"] + plate["w"] * 0.42, plate["y"] + 10,
               plate["x"] + plate["w"] * 0.8, plate["y"] + plate["h"] - 10)
        behind = _edges(under, box)
        blurred = _edges(img, box)
        look.do(blur=False)
        _wait(lambda: not look.canvas().get("blur"), 3.0)
        time.sleep(0.4)
        img2, path2 = nested.screenshot("call-unblurred", keep)
        shots["call-unblurred"] = path2
        sharp = _edges(img2, box)
        look.do(blur=True)
        _wait(lambda: look.canvas().get("blur"), 3.0)
        flat = behind
        print(f"  (the agent's plate over the busy window: edges {behind:.1f} behind it, "
              f"{sharp:.2f} through the plate unblurred, {blurred:.2f} blurred)", flush=True)
        check("niri's screenshot: behind the plates the busy window is blurred (frosted, as "
              "picom does), not merely showing through: its edges through the plate at most "
              "half what they are with the canvas's blur off",
              flat > 3 and sharp > 0.4 and blurred < 0.5 * sharp,
              f"behind {flat:.1f}, unblurred {sharp:.2f}, blurred {blurred:.2f} ({path})")
    else:
        check("the agent's plate in the layout", False, str(look.win("call"))[:300])

    poker.type("hello")
    typed = _wait(lambda: (look.win("call") or {}).get("typed") == "hello", 3.0)
    check("typed into the call (niri's virtual keyboard, the canvas's keyboard): your line in "
          "your plate; nothing reached the window", bool(typed) and got()[start:] == b"",
          f"{(look.win('call') or {}).get('typed')!r} {got()[start:]!r}")
    before = _union((look.win("call") or {}).get("blur") or [])
    poker.tap(28)                           # Return
    said = _wait(lambda: "hello" in (look.agent().get("said") or []), 3.0)
    look.do(do="text_start")
    look.do(do="text", args=["Three resets since boot, all from the wifi driver.\n\n" + "\n\n".join(
        f"Line {i} of the answer, long enough to be read across the plate." for i in range(8))])
    look.do(do="end")
    grown = _wait(lambda: (_union((look.win("call") or {}).get("blur") or []) or (0,) * 4)[3]
                  > (before or (0,) * 4)[3] + 60, 5.0)
    after = _union((look.win("call") or {}).get("blur") or [])
    followed = _wait(lambda: _union(look.canvas().get("blur") or []) == after, 3.0)
    check("Return says it (the stand-in hub hears it); the reply grows the agent's plate, and "
          "the blur region sent follows it",
          bool(said) and bool(grown) and bool(followed),
          f"{before} -> {after}, said {look.agent().get('said')}")
    img3, path3 = nested.screenshot("call-reply", keep)
    shots["call-reply"] = path3

    plate = look.widget("call", "AgentPlate")
    if plate:
        y = plate["y"] + plate["h"] * 0.62
        x0 = plate["x"] + 30
        poker.drag(x0, y, x0 + 360, y, steps=12)
        primary = _wait(lambda: look.paste(primary=True).strip(), 3.0) or ""
        poker.tap(46, "ctrl")               # Ctrl+C
        clip = _wait(lambda: look.paste().strip(), 3.0) or ""
        check("text dragged across in the reply: on PRIMARY (wl-paste --primary in the nested "
              "niri); Ctrl+C puts it on the clipboard", len(primary) >= 10
              and "of the answer" in primary
              and clip == primary, f"{primary!r} {clip!r}")
    mark = len(shell.log())
    poker.tap(1)                            # Escape
    closed = look.gone("call")
    poker.type("ok")
    back = _wait(lambda: got()[start:].endswith(b"ok"), 3.0)
    check("Escape hangs up: the call goes, the blur region is cleared, the keyboard goes back "
          "to the window (what is typed next reaches it)",
          closed and look.canvas().get("blur") == [] and look.kb_is("None") == "None"
          and bool(back), f"blur {look.canvas().get('blur')} kb {look.kb()} {got()[start:]!r}")

    mark = len(shell.log())
    mod_a()
    look.idle("call")
    timings["call (reopen)"] = _timings(shell.log(), "call", mark)
    mod_a()
    check("Mod+A again (`toggle`) hangs up", look.gone("call") and look.kb_is("None") == "None")
    mod_a()
    look.idle("call")
    poker.tap(30, "super")                  # Super+A typed to the canvas
    check("Super+A reaching the call (its close key, for when no binding takes it) hangs up",
          look.gone("call"))
    mod_a()
    look.idle("call")
    before = len(got())
    poker.click(W * 0.15, H * 0.6)
    closed = look.gone("call")
    time.sleep(0.4)
    check("an outside click hangs up, swallowed (the window under it saw nothing)",
          closed and got()[before:] == b"", repr(got()[before:]))
    ctx["busy"] = busy


def chip(ctx) -> None:
    """The top bar's agent chip with the call hung up: idle and dim; its
    arcs turning (frames) while a turn runs; the ring blinking when it
    ends; a click answers (the call opens)."""
    nested, shell, poker = ctx["nested"], ctx["shell"], ctx["poker"]
    check, keep, shots = ctx["check"], ctx["keep"], ctx["shots"]
    look = _Look(ctx)
    ac = look.widget("topbar", "AgentChip")
    check("the agent chip on the top bar finds its router (CallRouter in SERVICES): idle, "
          "dimmed", bool(ac) and ac.get("state") == "idle" and ac.get("strength") == 0.4,
          str(ac))
    if not ac:
        return
    look.do(do="say", args=["status check"])
    look.do(do="tool", args=["t1", "uptime"])
    working = _wait(lambda: (look.widget("topbar", "AgentChip") or {}).get("state")
                    == "working", 3.0)
    time.sleep(0.3)
    m0 = len(shell.log())
    time.sleep(1.5)
    paints = _paints(shell.log()[m0:], "topbar")
    check("a turn with the call hung up: the chip says working, its arcs turn (the top bar "
          "repainted ~30 times a second, only the chip's square)",
          bool(working) and 30 <= len(paints) <= 60
          and all(px <= 40 * 40 for _ms, px in paints),
          f"{look.widget('topbar', 'AgentChip')} {len(paints)} paints, {paints[:3]}")
    img, path = nested.screenshot("chip-working", keep)
    shots["chip-working"] = path
    look.do(do="result", args=["t1", "up 3 days"])
    look.do(do="text", args=["All good."])
    look.do(do="end")
    ringing = _wait(lambda: (look.widget("topbar", "AgentChip") or {}).get("state")
                    == "calling", 3.0)
    strengths = set()
    m0 = len(shell.log())
    for _ in range(12):
        strengths.add((look.widget("topbar", "AgentChip") or {}).get("strength"))
        time.sleep(0.1)
    paints = _paints(shell.log()[m0:], "topbar")
    check("the turn ends: it rings, both pieces blinking (lit and unlit, the top bar repainted "
          "for it)", bool(ringing) and {1.0, 0.3} <= strengths and len(paints) >= 8
          and look.agent().get("ringing") is not None,
          f"{strengths} {len(paints)} paints {look.agent()}")
    img, path = nested.screenshot("chip-ringing", keep)
    shots["chip-ringing"] = path
    ac = look.widget("topbar", "AgentChip")
    poker.click(ac["x"] + ac["w"] - 16, ac["y"] + ac["h"] / 2)
    call = look.idle("call")
    check("a click on the chip answers: the call opens, the ring is over, the chip idle",
          bool(call) and look.agent().get("ringing") is None and look.agent().get("call_open")
          and _wait(lambda: (look.widget("topbar", "AgentChip") or {}).get("state")
                    == "idle", 3.0), f"{look.agent()} {look.widget('topbar', 'AgentChip')}")
    poker.tap(1)
    look.gone("call")
    for what, t in ctx["timings"].items():
        print(f"  ({_say_timing(what, t)})", flush=True)


def tray(ctx) -> None:
    """The systray's panel and menu on the canvas, placed on its output:
    an item of the test's on the shell's bus, its row in the panel, its
    menu beside the row, a row of the menu clicked (the item hears it)."""
    from indigoshell import theme
    from indigoshell.widgets.systray import TrayPanel
    nested, shell, poker = ctx["nested"], ctx["shell"], ctx["poker"]
    check, keep, shots = ctx["check"], ctx["keep"], ctx["shots"]
    look = _Look(ctx)
    W, H = ctx["W"], ctx["H"]
    bus = shell_bus(shell, nested.user_env)
    if not bus:
        check("the shell's own D-Bus, for a tray item", False, str(bus))
        return
    item = TrayItem(bus)
    try:
        listed = _wait(lambda: TrayItem.TITLE in (look.lay().get("_tray") or []), 5.0)
        check("a StatusNotifierItem on the shell's bus registers with its watcher",
              item.error is None and bool(listed), f"{item.error} {look.lay().get('_tray')}")
        st = look.widget("bar", "Systray")
        if not st:
            check("the systray indicator on the bottom bar", False)
            return
        if st["x"] + st["w"] <= W:
            how = "a click on the indicator"
            poker.click(st["x"] + st["w"] / 2, st["y"] + st["h"] / 2)
        else:
            # The bottom bar's row is wider than a small output: the
            # indicator is past its edge (as on X at that width).
            how = "the indicator's toggle (it is past the edge of this small output)"
            look.do(tray="toggle")
        panel = look.idle("systray-panel")
        check(f"{how}: the tray's panel opens bottom right on the canvas's output, above the "
              "bar, as on X (anchored, its margins), keyboard taken",
              bool(panel) and panel["cx"] + panel["cw"] == W - theme.BAR_MARGIN
              and panel["cy"] + panel["ch"] == H - theme.BAR_HEIGHT - theme.POPUP_GAP
              and look.kb_is("Exclusive") == "Exclusive",
              f"{panel and (panel['cx'], panel['cy'], panel['cw'], panel['ch'])} kb {look.kb()}")
        row = look.widget("systray-panel", "_TrayRow")
        if not row:
            check("the item's row in the panel", False, str(panel)[:300])
            return
        img, path = nested.screenshot("tray-panel", keep)
        shots["tray-panel"] = path
        poker.click(row["x"] + row["w"] / 2, row["y"] + row["h"] / 2)
        menu = look.idle("systray-menu")
        rows = look.widgets("systray-menu", "_MenuItemRow")
        gap = TrayPanel.MENU_GAP
        check("a click on the row: its menu (the item's dbusmenu, two rows) beside the row, its "
              "right edge MENU_GAP left of the row, its top at the row's, inside the output",
              bool(menu) and len(rows) == 2 and menu["cx"] + menu["cw"] == int(row["x"]) - gap
              and menu["cy"] == int(row["y"]) and 0 <= menu["cx"]
              and menu["cy"] + menu["ch"] <= H,
              f"{menu and (menu['cx'], menu['cy'], menu['cw'], menu['ch'])} row "
              f"{(row['x'], row['y'])} {len(rows)} rows")
        img, path = nested.screenshot("tray-menu", keep)
        shots["tray-menu"] = path
        if len(rows) == 2:
            r = rows[1]
            poker.click(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            heard = _wait(lambda: ("Event", 2, "clicked") in item.calls, 3.0)
            check("a click on its second row: the item hears `clicked` for it, the menu closes, "
                  "the panel keeps the keyboard", bool(heard) and look.gone("systray-menu")
                  and "systray-panel" in look.lay() and look.kb() == "Exclusive",
                  f"{item.calls} kb {look.kb()}")
        poker.tap(1)
        check("Escape closes the panel", look.gone("systray-panel")
              and look.kb_is("None") == "None")
    finally:
        item.close()


def over_fullscreen(ctx) -> None:
    """With a fullscreen window (the canvas gone): what the user calls up
    shows over it with nothing else — the launcher, a toast, the app's
    menu in the top bar, the call — and the canvas goes again when the
    last of it closes, the window still fullscreen."""
    from indigoshell import theme
    from indigoshell.applink import bar as B
    nested, shell, poker, app = ctx["nested"], ctx["shell"], ctx["poker"], ctx["app"]
    check, keep, shots, drawn = ctx["check"], ctx["keep"], ctx["shots"], ctx["drawn"]
    rec = ctx["recorder"]
    look = _Look(ctx)
    W, H = ctx["W"], ctx["H"]

    def got():
        with open(rec, "rb") as f:
            return f.read()

    def still_fullscreen(mark):
        return "no fullscreen window" not in shell.log()[mark:]

    mark = len(shell.log())
    nested.action("spawn", "--", nested.launcher_key)
    win = look.idle("launcher")
    time.sleep(0.3)
    img, path = nested.screenshot("launcher-over-fullscreen", keep)
    shots["launcher-over-fullscreen"] = path
    probe, ink = drawn(img)
    bg = [int(theme.POPUP_BG[i:i + 2], 16) for i in (1, 3, 5)]
    share = 0.0
    if win:
        import numpy as np
        box = img[int(win["cy"]) + 30:int(win["cy"] + win["ch"]) - 30,
                  int(win["cx"]) + 30:int(win["cx"] + win["cw"]) - 30, :3].astype(int)
        share = float((np.abs(box - bg).max(axis=-1) <= 40).mean())
    check("Mod+D over the fullscreen window: the canvas is back with only the launcher on it "
          "(drawn; no bar), the keyboard exclusive, the window still fullscreen",
          bool(win) and look.overlay() and look.kb() == "Exclusive" and share > 0.5
          and probe < 0.05 and ink < 0.005 and still_fullscreen(mark),
          f"launcher {share:.2f} probe {probe:.2f} ink {ink:.3f} kb {look.kb()} ({path})")
    start = len(got())
    poker.tap(1)
    gone = look.gone("launcher")
    away = _wait(lambda: not look.overlay(), 3.0)
    poker.type("fs")
    reached = _wait(lambda: got()[start:].endswith(b"fs"), 3.0)
    time.sleep(0.8)                         # past the keyboard's way back
    check("Escape: once it has despawned the canvas goes again, the window still fullscreen "
          "with the keyboard (typed text reaches it), no bar flashed back",
          gone and bool(away) and bool(reached) and still_fullscreen(mark)
          and not look.overlay(), f"{look.canvas()} {got()[start:]!r}")

    bus = shell_bus(shell, nested.user_env)
    if bus:
        mark = len(shell.log())
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications", "--method",
             "org.freedesktop.Notifications.Notify", "wltest", "0", "", "Over fullscreen",
             "a toast over a fullscreen window", "[]", "{}", "2000"],
            env={"PATH": os.environ.get("PATH", "/usr/bin"), "DBUS_SESSION_BUS_ADDRESS": bus},
            capture_output=True, text=True, timeout=10)
        m = re.search(r"\(uint32 (\d+),\)", out.stdout)
        name = f"notif-{m[1]}" if m else None
        toast = look.idle(name) if name else None
        img, path = nested.screenshot("toast-over-fullscreen", keep)
        shots["toast-over-fullscreen"] = path
        probe, ink = drawn(img)
        check("a toast over the fullscreen window (notifications not missed): the canvas with "
              "the toast alone, no keyboard taken", bool(toast) and look.overlay()
              and look.kb() == "None" and ink < 0.005 and probe < 0.05,
              f"{toast and (toast['cx'], toast['cy'])} kb {look.kb()} ink {ink:.3f}")
        away = _wait(lambda: name not in look.lay() and not look.overlay(), 8.0)
        check("... its time up, the canvas goes again", bool(away) and still_fullscreen(mark))

    mark = len(shell.log())
    app.send(B.open_menu("pick"))
    drop = look.idle("dropdown")
    menu = _wait(lambda: look.lay().get("_apps_prompt"), 3.0) or {}
    top = look.win("topbar") or {}
    img, path = nested.screenshot("menu-over-fullscreen", keep)
    shots["menu-over-fullscreen"] = path
    probe, ink = drawn(img)
    check("the app asks for its menu: the top bar (its open menu) and the drop-down over the "
          "fullscreen window, the bottom bar not", bool(drop) and top.get("shows") is True
          and (look.win("bar") or {}).get("shows") is False and probe > 0.4 and ink < 0.005
          and menu.get("rows") == ["Alpha row", "Beta row", "Gamma row"],
          f"top {top.get('shows')} probe {probe:.2f} ink {ink:.3f} {menu}")
    poker.tap(1)
    away = _wait(lambda: look.gone("dropdown") and not look.overlay(), 4.0)
    check("Escape: the menu closes, the top bar goes with it and so does the canvas",
          bool(away) and still_fullscreen(mark), str(look.canvas()))

    mark = len(shell.log())
    nested.action("spawn", "--", nested.call_key)
    call = look.idle("call")
    img, path = nested.screenshot("call-over-fullscreen", keep)
    shots["call-over-fullscreen"] = path
    check("Mod+A: the call over the fullscreen window, blurred behind its plates, the keyboard "
          "taken", bool(call) and look.overlay() and look.kb() == "Exclusive"
          and bool(look.canvas().get("blur")), f"{look.canvas()}")
    poker.tap(1)
    away = _wait(lambda: look.gone("call") and not look.overlay(), 4.0)
    check("... hung up: the canvas goes again", bool(away) and still_fullscreen(mark))


# ── phase 2c: Flux on Wayland, the app bar by niri's focus ─────────────
PAGE = """<!doctype html><title>{title}</title>
<body style="background:#123;color:#ccc;font:40px sans-serif"><h1>{title} page</h1>
<p>some text to find: needle {title} needle</p>
<script>document.addEventListener("keydown", e => {{ document.title += " KEY " + e.key; }});
</script></body>"""


def _flux_control(shell: "Shell", code: str, timeout: float = 30.0):
    """Run `code` (the body of an async function given `control`, Flux's
    control client connected in the nested shell's environment) and
    return what it prints as JSON."""
    script = ("import asyncio, json\nfrom flux import control as C\n"
              "async def main():\n    control = await C.connect()\n"
              "    try:\n" + "".join(f"        {line}\n" for line in code.splitlines())
              + "    finally:\n        await control.close()\n"
              "print(json.dumps(asyncio.run(main())))\n")
    out = subprocess.run([sys.executable, "-B", "-c", script], env=shell.env,
                         capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": out.stderr[-800:]}


def flux(ctx) -> None:
    """Flux itself in the nested niri, on Qt's Wayland platform: its
    window a Wayland client whose process is the app connection's, its
    tabs on the top bar by niri's focus (and none for a kitty), a tab
    clicked, Alt+digits by their place, Ctrl+L caught by Flux and the
    shell's box opened with the keyboard taken, what is typed at once
    in it in order and never in the page, a submit loading, find and the
    actions menu, a new window's address input, the agent's window
    placed on a spare workspace, the call's Look naming Flux's window,
    a screenshot of a tab, the Watch's effects; then the laptop's hands
    on niri, called directly."""
    import http.server
    import functools
    import threading
    nested, shell, poker = ctx["nested"], ctx["shell"], ctx["poker"]
    check, keep, shots = ctx["check"], ctx["keep"], ctx["shots"]
    look = _Look(ctx)
    W, H = nested.output_size()
    if ctx.get("app") is not None:
        ctx["app"].close()                  # the stand-in: no longer taken as focused
        ctx["app"] = None
    www = os.path.join(nested.tmp, "www")
    os.makedirs(www, exist_ok=True)
    for title in ("Alpha", "Beta", "Gamma"):
        with open(os.path.join(www, f"{title.lower()}.html"), "w") as f:
            f.write(PAGE.format(title=title))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=www)
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = dict(shell.env, INDIGOSHELL_BROWSER_WIDEVINE="off")
    procs = []

    def start_flux(*urls):
        p = subprocess.Popen([sys.executable, "-B", os.path.join(REPO, "main.py"), "browser",
                              *urls], env=env, cwd=nested.tmp, start_new_session=True,
                             stdout=open(os.path.join(nested.tmp, f"flux-{len(procs)}.log"),
                                         "w"), stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    def windows():
        return nested.msg("windows") or []

    def flux_windows():
        return [w for w in windows() if w.get("app_id") == "indigoshell-browser"]

    def apps():
        return look.lay().get("_apps") or {}

    def tab_ids():
        t = look.widget("topbar", "TabsItem")
        return (t or {}).get("ids")

    def focused_title():
        return (nested.msg("focused-window") or {}).get("title") or ""

    def flux_log():
        out = ""
        for path in sorted(os.listdir(nested.tmp)):
            if path.startswith("flux-") and path.endswith(".log"):
                with open(os.path.join(nested.tmp, path), errors="replace") as f:
                    out += f.read()
        state = os.path.join(shell.home, "xdg_state_home")
        for root, _dirs, files in os.walk(state):
            for name in files:
                if name.endswith(".log") and "browser" in name:
                    with open(os.path.join(root, name), errors="replace") as f:
                        out += f.read()
        return out

    try:
        nested.action("focus-workspace", "4")
        time.sleep(0.4)
        kitty = nested.recorder("flux-side")
        _wait(lambda: any(w.get("app_id") == "wltest.flux-side" for w in windows()), 10)
        mark = len(shell.log())
        start_flux(f"{base}/alpha.html", f"{base}/beta.html", f"{base}/gamma.html")
        m = shell.wait_log(r"flux window (\S+) connected \(pid (\d+)\)", 60, mark)
        win = _wait(lambda: next(iter(flux_windows()), None), 20)
        ids = _wait(lambda: tab_ids() == [1, 2, 3] and tab_ids(), 15)
        check("Flux on Qt's Wayland platform: its window a Wayland client of niri's (app id "
              "indigoshell-browser) whose pid is the app connection's (no X handle)",
              m is not None and win is not None and win.get("pid") == int(m[2]),
              f"{m and m[0]} {win}")
        check("the top bar follows niri's focus: Flux's window focused, its three tabs on the "
              "top bar (by niri's pid and Flux's `active`)",
              bool(ids) and (apps().get("focused") or [None])[0] == "flux"
              and (win or {}).get("is_focused"), f"{ids} {apps()}")
        time.sleep(0.6)
        img, path = nested.screenshot("flux-tabs", keep)
        shots["flux-tabs"] = path

        # Focus elsewhere and back.
        side = next((w for w in windows() if w.get("app_id") == "wltest.flux-side"), None)
        if side is not None:
            nested.action("focus-window", "--id", str(side["id"]))
        gone = _wait(lambda: tab_ids() is None and apps().get("focused") is None, 5)
        if win is not None:
            nested.action("focus-window", "--id", str(win["id"]))
        back = _wait(lambda: tab_ids() == [1, 2, 3], 5)
        check("a kitty focused: the app section empty; Flux focused again: its tabs back",
              bool(gone) and bool(back), f"{apps()} {tab_ids()}")

        # A tab clicked on the top bar.
        tabs = look.widget("topbar", "TabsItem")
        if tabs:
            ty = tabs["y"] + tabs["h"] / 2
            poker.click(tabs["x"] + tabs["pitch"] * 1.5, ty)
            beta = _wait(lambda: focused_title().startswith("Beta"), 5)
            check("a click on the second tab: Flux shows it (its window's title Beta)",
                  bool(beta), focused_title())
        # Alt+digits by their place (XKB keycodes under Wayland too).
        poker.keyboard("us")
        poker.tap(2, "alt")
        alpha = _wait(lambda: focused_title().startswith("Alpha"), 5)
        poker.tap(4, "alt", "shift")                # Alt+Shift+3: "#", keycode 12
        moved = _wait(lambda: tab_ids() == [2, 3, 1], 5)
        poker.tap(2, "alt", "shift")                # back to the first place
        home = _wait(lambda: tab_ids() == [1, 2, 3], 5)
        check("Alt+1 shows the first tab; Alt+Shift+3 (\"#\" by its symbol) moves it third "
              "and Alt+Shift+1 back, the digits read by their place",
              bool(alpha) and bool(moved) and bool(home), f"{focused_title()} {tab_ids()}")

        # The pointer parked on the top bar: niri's focus follows the mouse
        # (the user's config), and qtile may lay the nested niri out anew.
        def flux_focused():
            poker.move(W / 2, 3)
            if win is not None:
                nested.action("focus-window", "--id", str(win["id"]))
            return _wait(lambda: (nested.msg("focused-window") or {}).get("id")
                         == (win or {}).get("id"), 3)

        # Ctrl+L: caught by Flux, opened by the shell; type-ahead.
        flux_focused()
        mark = len(shell.log())
        poker.burst((38, "ctrl"), "yo")
        prompt = _wait(lambda: (look.lay().get("_apps_prompt") or {}).get("text") == "yo"
                       and look.lay()["_apps_prompt"], 5)
        kb = look.kb_is("Exclusive")
        fwd = re.findall(r"(\d+) key\(s\) forwarded", shell.log()[mark:])
        forwarded = sum(map(int, fwd))
        check("Ctrl+L on Flux's window: Flux catches it (`open` with the chord), the shell "
              "opens the address box and takes the keyboard (exclusive)",
              bool(prompt) and prompt.get("what") == "input" and kb == "Exclusive",
              f"{look.lay().get('_apps_prompt')} kb {kb}")
        check("type-ahead: \"yo\" sent in the same burst as Ctrl+L reaches Flux's window "
              "before the shell has the keyboard; Flux forwards it (`keys`) and it lands in "
              "the box in order, nothing in the page",
              bool(prompt) and forwarded == 2 and "KEY" not in focused_title(),
              f"forwarded {forwarded}, title {focused_title()!r}")
        drop = _wait(lambda: "dropdown" in look.lay(), 3)
        time.sleep(0.4)
        img, path = nested.screenshot("flux-address", keep)
        shots["flux-address"] = path
        check("its suggestions drop under the bar (Flux's answer: the typed search)",
              bool(drop) and "yo" in (look.lay().get("_apps_prompt") or {}).get("rows", []),
              str(look.lay().get("_apps_prompt")))
        # Return on a typed address loads it here.
        poker.tap(14)                               # BackSpace twice: "yo" gone
        poker.tap(14)
        poker.type(f"{base}/gamma.html".replace("http://", ""), settle=0.0)
        time.sleep(0.4)
        poker.tap(28)
        gamma = _wait(lambda: focused_title().startswith("Gamma"), 8)
        closed = _wait(lambda: "_apps_prompt" not in look.lay() and look.kb() == "None", 3)
        check("an address typed and Return: Flux loads it in the tab; the box closes and the "
              "keyboard goes back to Flux's window; no key reached the page",
              bool(gamma) and bool(closed) and "KEY" not in focused_title(),
              f"{focused_title()!r} kb {look.kb()}")
        poker.type("a")
        keyed = _wait(lambda: "KEY a" in focused_title(), 3)
        check("... and a key typed after it reaches the page again", bool(keyed),
              focused_title())

        # Find and the actions menu.
        flux_focused()
        poker.tap(33, "ctrl")
        poker.type("needle", settle=0.0)
        found = _wait(lambda: (look.lay().get("_apps_prompt") or {}).get("text") == "needle", 5)
        poker.tap(1)
        found_closed = _wait(lambda: "_apps_prompt" not in look.lay(), 3)
        poker.tap(50, "ctrl")
        menu = _wait(lambda: (look.lay().get("_apps_prompt") or {}).get("what") == "menu"
                     and look.lay()["_apps_prompt"], 5)
        poker.tap(1)
        menu_closed = _wait(lambda: "_apps_prompt" not in look.lay(), 3)
        check("Ctrl+F: find typed into the box, Escape; Ctrl+M: the actions menu drops "
              "(\"Add bookmark\"), Escape", bool(found) and bool(found_closed) and bool(menu)
              and "Add bookmark" in menu.get("rows", []) and bool(menu_closed),
              f"{menu}")

        # A second invocation with no address: a new window asking for one.
        flux_focused()
        mark = len(shell.log())
        start_flux()
        second = shell.wait_log(r"flux window (\S+) connected", 30, mark)
        asked = _wait(lambda: (look.lay().get("_apps_prompt") or {}).get("what") == "input"
                      and (apps().get("focused") or [None, None])[1] != 1, 10)
        check("a second `browser` with no address: a new Flux window, focused by niri, its "
              "address input opened by the shell (`open`)", second is not None and bool(asked),
              f"{second and second[0]} {apps()} {look.lay().get('_apps_prompt')}")
        poker.tap(1)
        _wait(lambda: "_apps_prompt" not in look.lay(), 3)
        poker.tap(17, "ctrl")                       # Ctrl+W: its one tab, and it
        _wait(lambda: len(flux_windows()) == 1, 8)
        flux_focused()
        _wait(lambda: tab_ids() == [1, 2, 3], 5)

        # The agent's own window, placed by the shell through niri.
        mark = len(shell.log())
        before = {w["id"] for w in windows()}
        res = _flux_control(shell, f'r = await control.open({base + "/beta.html"!r}, '
                            'window="agent")\nreturn r.tab')
        moved = shell.wait_log(r"is window (\d+) of the compositor's: moved to workspace (\S+)",
                               10, mark)
        time.sleep(1.0)
        agent = next((w for w in windows() if w["id"] not in before), None)
        wss = {w["id"]: w for w in nested.msg("workspaces") or []}
        ws = wss.get((agent or {}).get("workspace_id")) or {}
        check("the agent's window: Flux asks `place`, the shell answers at once and moves the "
              "window to a spare workspace no monitor shows once niri lists it; the user's "
              "Flux window keeps the focus",
              moved is not None and agent is not None and int(moved[1]) == agent["id"]
              and not ws.get("is_active") and " · Flux" in (agent.get("title") or "")
              and (nested.msg("focused-window") or {}).get("id") == (win or {}).get("id"),
              f"{res} {moved and moved[0]} {agent} ws {ws}")

        # The call's Look: by Flux's window id and pid.
        flux_focused()
        mark = len(shell.log())
        nested.action("spawn", "--", nested.call_key)
        call = look.idle("call")
        poker.tap(38, "ctrl")                       # Look
        time.sleep(1.5)
        poker.type("hi")
        poker.tap(28)
        said = _wait(lambda: any("In Flux" in a for a in look.agent().get("attached", [])), 8)
        attached = (look.agent().get("attached") or [""])[-1]
        check("the call over Flux, Ctrl+L (Look) and a line: what Flux shows goes with it, the "
              "window named by its app bar id and pid (no X window on Wayland)",
              bool(call) and bool(said) and "Gamma" in attached, attached[:300])
        look.do(do="end")                           # the stand-in hub's turn over, unrung
        time.sleep(0.5)
        poker.tap(1)
        look.gone("call")

        # A screenshot of a tab and the Watch's effects, under Wayland.
        res = _flux_control(shell, "t = await control.tabs()\n"
                            "tab = [x.tab for w in t if not w.agent for x in w.tabs if x.current][0]\n"
                            "s = await control.screenshot(tab)\n"
                            "c = await control.click(tab, x=200, y=200)\n"
                            "await asyncio.sleep(1.5)\n"
                            "return {'png': len(s.get('png') or ''), 'w': s.get('width'), "
                            "'clicked': bool(c), 'xid': [w.xid for w in t]}")
        log = flux_log()
        check("Flux's screenshot of a tab under Wayland (the view grabbed): a PNG; `tabs` gives "
              "no X window (xid null)", isinstance(res, dict) and res.get("png", 0) > 1000
              and all(x is None for x in res.get("xid") or [0]), str(res)[:400])
        check("the agent clicking in the tab: the Watch locks it and its effects run on Qt "
              "Quick under Wayland (none turned off, no traceback in Flux's log)",
              isinstance(res, dict) and res.get("clicked") and "effects are off" not in log
              and "Traceback" not in log, str(res)[:200] + " " + "\n".join(
                  ln for ln in log.splitlines() if "effects" in ln or "Trace" in ln)[:600])
        img, path = nested.screenshot("flux-watch", keep)
        shots["flux-watch"] = path
        laptop(ctx)
        toggle_bar(ctx)
    finally:
        for p in procs:
            if p.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGTERM)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
        server.shutdown()
        if keep:
            with contextlib.suppress(OSError):
                with open(os.path.join(keep, "flux.log"), "w") as f:
                    f.write(flux_log())


LAPTOP = r"""
import asyncio, base64, json
from indigoshell.agent import laptop
from indigoshell.core.hands import Call

async def main():
    hands = {h.name: h for h in laptop.desktop_hands()}
    out = {}
    async def call(name, args=None):
        try:
            blocks = await hands[name].run(Call(id="t", tool=name, args=args or {},
                                                timeout=60.0))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return [{"type": b.get("type"), "text": (b.get("text") or "")[:400],
                 "png": len(base64.b64decode(b["data"])) if b.get("type") == "image" else 0}
                for b in blocks]
    out["windows"] = await call("windows")
    out["shot"] = await call("screenshot", {"target": "monitor"})
    out["write"] = await call("write_clipboard", {"text": "nested laptop clip"})
    await asyncio.sleep(0.5)
    out["read"] = await call("read_clipboard")
    out["focus"] = await call("focus", {"window_id": int(__import__("sys").argv[1])})
    out["shot_window"] = await call("screenshot", {"target": "window"})
    return out

print(json.dumps(asyncio.run(main())))
"""


def laptop(ctx) -> None:
    """The laptop's hands (agent/laptop.py) on niri, called directly in
    the nested niri's environment: the windows, a monitor's screenshot,
    the clipboard written and read, a window focused, its screenshot."""
    nested, shell, check = ctx["nested"], ctx["shell"], ctx["check"]
    side = next((w for w in nested.msg("windows") or []
                 if w.get("app_id") == "wltest.flux-side"), None)
    target = (side or {}).get("id", 0)
    out = subprocess.run([sys.executable, "-B", "-c", LAPTOP, str(target)], env=shell.env,
                         capture_output=True, text=True, timeout=60)
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check("the laptop's hands ran in the nested niri", False, out.stderr[-1200:])
        return

    def text(name):
        r = res.get(name)
        return " ".join(b.get("text", "") for b in r) if isinstance(r, list) else str(r)

    def png(name):
        r = res.get(name)
        return max((b.get("png", 0) for b in r), default=0) if isinstance(r, list) else 0

    check("laptop_windows on niri: the workspaces and their windows (the kitty, Flux)",
          "wltest.flux-side" in text("windows") and "indigoshell-browser" in text("windows"),
          text("windows")[:400])
    check("laptop_screenshot of the monitor over wlr-screencopy: a PNG", png("shot") > 10000,
          text("shot")[:300])
    check("laptop_write_clipboard then laptop_read_clipboard through wl-clipboard: the same text",
          "nested laptop clip" in text("read"), f"{text('write')[:200]} / {text('read')[:200]}")
    focused = (nested.msg("focused-window") or {}).get("id")
    check("laptop_focus on a window by its niri id: niri focuses it; its screenshot (a tiled "
          "window: its monitor, the reply says so)",
          focused == target and png("shot_window") > 10000,
          f"focused {focused} vs {target}: {text('focus')[:200]} / {text('shot_window')[:200]}")


def toggle_bar(ctx) -> None:
    """docs/niri.kdl's Mod+M: `toggle bar` hides the bottom bar and gives
    its strip to the windows; again, both back."""
    nested, shell, check = ctx["nested"], ctx["shell"], ctx["check"]
    look = _Look(ctx)

    def reserved():
        return any(ly.get("namespace") == "indigoshell-reserve-bottom"
                   for ly in nested.msg("layers") or [])

    shell.client("toggle", "bar")
    hidden = _wait(lambda: "bar" not in look.lay() and not reserved(), 5)
    shell.client("toggle", "bar")
    back = _wait(lambda: (look.win("bar") or {}).get("phase") == "idle" and reserved(), 5)
    check("Mod+M's `toggle bar` (docs/niri.kdl): the bottom bar and its reservation go, then "
          "come back", bool(hidden) and bool(back), f"{list(look.lay())} {reserved()}")
