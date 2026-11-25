"""A niri of a test's own, out of sight.

niri runs on its winit backend as the client of cage, the kiosk Wayland
compositor, on wlroots' headless backend (as tests/_cage.py runs Flux):
no output reaches any screen, no input comes from any device, and the
session the test was started in is never its display — $DISPLAY and
$WAYLAND_DISPLAY are not passed on.

cage's one output is sized first (wlr-output-management, which cage
speaks; its headless output starts at 1280x720), and niri's window
fills it, so niri's output is about that size (2560x1440 gives
2552x1408).

    hidden = Hidden(tmp, env, ["niri", "-c", config], log, size=(2560, 1440))
    if hidden.start():
        hidden.env      # niri's: WAYLAND_DISPLAY, NIRI_SOCKET, XDG_RUNTIME_DIR
        hidden.pid      # niri's process
    hidden.stop()

`tmp` must be short (unix socket paths are): cage and niri both run in
it, their runtime directory its `run`, reached through /proc/<pid>/cwd.
`argv` may wrap niri (dbus-run-session -- niri ...); niri is found by
its IPC socket, which names its pid.
"""

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time

WLR_XML = "/usr/share/wlr-protocols/unstable/wlr-output-management-unstable-v1.xml"
WAYLAND_XML = "/usr/share/wayland/wayland.xml"


def missing() -> list[str]:
    """What this needs and the machine does not have."""
    return [t for t in ("cage", "niri") if shutil.which(t) is None] + [
        p for p in (WLR_XML, WAYLAND_XML) if not os.path.exists(p)]


class Hidden:
    def __init__(self, tmp: str, env: dict, argv: list[str], log: str,
                 size: tuple[int, int] = (2560, 1440)) -> None:
        self.tmp = tmp
        self.run = os.path.join(tmp, "run")
        os.makedirs(self.run, mode=0o700, exist_ok=True)
        self.argv = argv
        self.log = log
        self.size = size
        self.go = os.path.join(tmp, "niri-go")
        self.cage: subprocess.Popen | None = None
        self.pid: int | None = None
        self.env: dict = {}
        self.note = ""                  # why start() failed
        base = {k: v for k, v in env.items()
                if k not in ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET",
                             "NIRI_SOCKET", "XAUTHORITY")}
        base.update(XDG_RUNTIME_DIR="/proc/self/cwd/run", TMPDIR=tmp,
                    WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1")
        self.base = base

    def start(self, timeout: float = 15.0) -> bool:
        # niri waits for the output to be sized: cage's client is a shell
        # that execs it once `go` exists. cage's own Xwayland is not for it.
        wait = (f'while [ ! -e "{self.go}" ]; do sleep 0.05; done; '
                'unset DISPLAY; exec "$@"')
        self.cage = subprocess.Popen(
            ["cage", "--", "sh", "-c", wait, "sh", *self.argv], cwd=self.tmp,
            env=self.base, stdout=open(self.log, "w"), stderr=subprocess.STDOUT,
            start_new_session=True)
        end = time.monotonic() + timeout
        cage_wl = os.path.join(self.run, "wayland-0")
        while not os.path.exists(cage_wl):
            if self.cage.poll() is not None or time.monotonic() > end:
                self.note = "cage never made its socket"
                return False
            time.sleep(0.05)
        try:
            size_output(cage_wl, self.size, self.tmp)
        except Exception as e:          # noqa: BLE001 — said, and the run stops
            self.note = f"sizing cage's output failed: {e!r}"
            return False
        open(self.go, "w").close()
        sock = None
        while time.monotonic() < end:
            sock = next((n for n in os.listdir(self.run)
                         if re.fullmatch(r"niri\.wayland-\d+\.\d+\.sock", n)), None)
            if sock or self.cage.poll() is not None:
                break
            time.sleep(0.05)
        if sock is None:
            self.note = "niri never made its IPC socket"
            return False
        _niri, wl, pid, _sock = sock.split(".")
        self.pid = int(pid)
        base = f"/proc/{self.pid}/cwd/run"
        while not os.path.exists(os.path.join(self.run, wl)) and time.monotonic() < end:
            time.sleep(0.05)
        self.env = dict(self.base, XDG_RUNTIME_DIR=base, WAYLAND_DISPLAY=f"{base}/{wl}",
                        NIRI_SOCKET=f"{base}/{sock}")
        for var in ("WLR_BACKENDS", "WLR_LIBINPUT_NO_DEVICES"):
            self.env.pop(var)
        return True

    def alive(self) -> bool:
        return self.pid is not None and os.path.exists(f"/proc/{self.pid}")

    def stop(self) -> None:
        if self.cage is None or self.cage.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.cage.pid, signal.SIGTERM)
        try:
            self.cage.wait(5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.cage.pid, signal.SIGKILL)
            self.cage.wait(5)


def _bindings(tmp: str):
    """pywayland's modules for wl_output and wlr-output-management,
    generated into `tmp` (never the shell's cache)."""
    from pywayland.scanner.protocol import Protocol
    root = os.path.join(tmp, "outproto")
    pkg = os.path.join(root, "outproto")
    if not os.path.isdir(pkg):
        protocols = [Protocol.parse_file(p) for p in (WAYLAND_XML, WLR_XML)]
        names = {i.name: p.name for p in reversed(protocols) for i in p.interface}
        os.makedirs(pkg)
        for p in protocols:
            imports = dict(names)
            imports.update({i.name: p.name for i in p.interface})
            p.output(pkg, imports)
        open(os.path.join(pkg, "__init__.py"), "w").close()
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib
    return importlib.import_module("outproto.wlr_output_management_unstable_v1")


def size_output(display: str, size: tuple[int, int], tmp: str) -> None:
    """Give the compositor at `display` (its first head) a custom mode
    of `size`. Raises when it is refused."""
    from pywayland.client import Display
    wom = _bindings(tmp)
    d = Display(display)
    d.connect()
    try:
        reg = d.get_registry()
        found: dict = {}
        reg.dispatcher["global"] = lambda _r, n, i, v: found.__setitem__(i, (n, v))
        d.roundtrip()
        name, version = found["zwlr_output_manager_v1"]
        mgr = reg.bind(name, wom.ZwlrOutputManagerV1, min(version, 2))
        heads: list = []
        serial: list = []
        mgr.dispatcher["head"] = lambda _m, head: heads.append(head)
        mgr.dispatcher["done"] = lambda _m, s: serial.append(s)
        d.roundtrip()
        d.roundtrip()
        if not heads or not serial:
            raise RuntimeError("no head to size")
        config = mgr.create_configuration(serial[-1])
        result: list = []
        for event in ("succeeded", "failed", "cancelled"):
            config.dispatcher[event] = lambda _c, e=event: result.append(e)
        config.enable_head(heads[0]).set_custom_mode(size[0], size[1], 0)
        config.apply()
        for _ in range(20):
            d.roundtrip()
            if result:
                break
        config.destroy()
        if result != ["succeeded"]:
            raise RuntimeError(f"the configuration {result[0] if result else 'unanswered'}")
    finally:
        d.disconnect()
