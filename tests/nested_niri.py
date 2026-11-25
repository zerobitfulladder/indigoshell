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
  bar, a chord menu whose items only log, timing probes, and the window
  a stand-in app announces taken as focused); a desktop entry of the
  test's for its launcher to find (a kitty).
- niri's Mod+D: the user's (fuzzel) replaced by a script running the
  nested shell's own client with the nested shell's environment and
  nothing else (env -i), so it reaches that daemon, never the live one.
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
windows and services), a probe widget on the top bar, and the window a
stand-in app announces taken as focused (applink follows X's focus
only, so far)."""

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
    changed)."""

    last = None

    @classmethod
    def write(cls):
        from indigoshell.core.daemon import get_daemon
        out = {}
        for name, win in get_daemon().instances.items():
            widgets = []
            for w in win.content.walk():
                r = w.rect
                item = {"type": type(w).__name__, "x": win.x + r.left(), "y": win.y + r.top(),
                        "w": r.width(), "h": r.height()}
                if type(w).__name__ == "Workspaces":
                    item.update(stride=w._stride(), count=w._count)
                if type(w).__name__ == "TabsItem":
                    item.update(pitch=w.pitch, first=w.first, ids=list(w.ids))
                widgets.append(item)
            out[name] = {"x": win.x, "y": win.y, "w": win.width, "h": win.height,
                         "cx": win.content_root[0], "cy": win.content_root[1],
                         "cw": win.content_root[2], "ch": win.content_root[3],
                         "phase": win._phase, "widgets": widgets}
            prompt = getattr(win.content, "prompt", None)
            if prompt is not None and getattr(prompt, "open", False):
                out[name]["prompt"] = _prompt(prompt)
        p = APPS.prompt
        if p is not None and p.open:
            out["_apps_prompt"] = _prompt(p)
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


async def _dumping():
    async def loop():
        while True:
            await asyncio.sleep(0.2)
            try:
                Dump.write()
            except Exception:
                log.exception("probe: the layout dump failed")
    asyncio.get_running_loop().create_task(loop())


WINDOWS = [top_bar(Probe()) if spec.name == TOP_BAR else spec for spec in WINDOWS]
STARTUP = [*STARTUP, _dumping]


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

_added = APPS.added


def _added_and_focused(window):
    _added(window)
    APPS.set_active(window.wid)


APPS.added = _added_and_focused
'''


# What the launcher finds and starts: a kitty in the nested niri, its
# title the test's scratch name (so it can be found and stopped).
LAUNCHED_ENTRY = """[Desktop Entry]
Type=Application
Name=Zqxwv Nested Probe
Exec=kitty --class wltest.launched -T {tag} -o linux_display_server=wayland -o confirm_os_window_close=0 sleep 900
"""


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
        out = [line for line in out if not re.match(r"\s*Mod\+D\s*(\{|\S+=)", line)]
        binds = [f'    Mod+D {{ spawn "{self.launcher_key}"; }}']
        at = next((i for i, line in enumerate(out) if re.match(r"\s*binds\s*\{", line)), None)
        if at is None:
            out += ["binds {", *binds, "}"]
        else:
            out[at + 1:at + 1] = binds
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

    def recorder(self, name) -> str:
        """A kitty whose terminal reports clicks (xterm's mode 1000) and
        writes them and every key typed into it, raw, to a file: what
        reached that window."""
        path = os.path.join(self.tmp, f"typed-{name}")
        open(path, "wb").close()
        self.kitty(name, f"stty raw -echo; printf '\\033[?1000h'; exec cat > {path}")
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
                   INDIGOSHELL_TEST_DUMP=self.dump, PYTHONDONTWRITEBYTECODE="1")
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
                       verb: tuple[str, ...] = ("toggle", "launcher")) -> None:
    """The nested niri's Mod+D: the nested shell's own client, with the
    nested shell's whole environment and nothing else (env -i), so it
    reaches that daemon and never the live one."""
    import shlex
    env = " ".join(shlex.quote(f"{k}={v}") for k, v in shell.env.items())
    main = shlex.quote(os.path.join(REPO, "main.py"))
    out = shlex.quote(os.path.join(nested.tmp, "launcher-key.log"))
    with open(nested.launcher_key, "w") as f:
        f.write(f"#!/bin/sh\nexec env -i {env} {shlex.quote(sys.executable)} -B {main} "
                f"{' '.join(verb)} >> {out} 2>&1\n")
    os.chmod(nested.launcher_key, 0o700)


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


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
        B = self.B
        self.send(B.announce("flux", wid, wid))
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
              "the launcher, the drop-down (this config has no call)",
              line is not None and set(line[1].split(", ")) == {
                  "bar", "topbar", "system", "hardware", "network", "menu", "launcher",
                  "dropdown"} and line[2] == "none", line[0] if line else "")
        keymap = re.search(r"keyboard: keymap with (.*)$", log, re.M)
        check("the keymap niri sent read by xkbcommon (the user's layouts)",
              keymap is not None, keymap[0] if keymap else "")
        write_launcher_key(nested, shell)
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

        overlays({"nested": nested, "shell": shell, "poker": poker, "app": app,
                  "check": check, "W": W, "H": H, "keep": keep, "shots": shots,
                  "recorder": recorder})
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
    ctx["timings"] = timings
