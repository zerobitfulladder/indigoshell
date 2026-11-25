"""X11 with qtile: qtile's command interface (`qtile cmd-obj`), the EWMH
properties on the root window, xclip, ImageMagick's `import` and xrandr.

The workspace readout reads the EWMH root-window properties on the
shell's own xcffib connection (the bar window's display) rather than
opening one, so there is no second event queue that could buffer events
during a reply round-trip, and no polling tick to drain it.
"""

import json
import logging
import re
import shutil
from typing import Any, Callable, Sequence

from . import (WM, Follower, Mode, NotFound, Output, Shot, Window,
               WMError, Workspace, WorkspaceState, fire, run, serve)

log = logging.getLogger(__name__)


async def qtile(*args: str, run=run) -> Any:
    """`qtile cmd-obj ARGS`, its answer as JSON where it is."""
    rc, out, err = await run(["qtile", "cmd-obj", *args])
    if rc != 0:
        raise RuntimeError(err or f"qtile cmd-obj {' '.join(args)} failed")
    text = out.decode(errors="replace").strip()
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


# ── xrandr ──────────────────────────────────────────────────────────────
_HEADER = re.compile(r"^(\S+) (connected|disconnected)( primary)?")
_GEOMETRY = re.compile(r"\d+x\d+\+\d+\+\d+")
_MODE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")
_RATE = re.compile(r"(\d+(?:\.\d+)?)([*+]*)")
# ` 0: braindance 1920/309x1080/174+0+0  DP-0` (name may carry + and *)
_MONITOR = re.compile(r"^\s*\d+: [+*]*(\S+) ", re.MULTILINE)


def parse_xrandr(text: str) -> list[Output]:
    """Outputs from `xrandr --query`, in order: connected, primary,
    active (has a current geometry), modes as (width, height, rate), and
    current — the mode xrandr marks with `*`, or None."""
    rows: list[dict] = []
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            rows.append({"name": m.group(1),
                         "connected": m.group(2) == "connected",
                         "primary": bool(m.group(3)),
                         "active": bool(_GEOMETRY.search(line)),
                         "modes": [], "current": None})
            continue
        mm = _MODE.match(line)
        if mm and rows:
            w, h = int(mm.group(1)), int(mm.group(2))
            for rate, flags in _RATE.findall(mm.group(3)):
                mode = Mode(w, h, float(rate))
                rows[-1]["modes"].append(mode)
                if "*" in flags:
                    rows[-1]["current"] = mode
    return [Output(r["name"], r["connected"], r["active"], r["primary"],
                   tuple(r["modes"]), r["current"]) for r in rows]


def use_only_argv(name: str, mode: Mode | None,
                  off: Sequence[str]) -> list[str]:
    """One xrandr call, not one per output, or there is a frame with no
    output enabled and X collapses the screen to 640x480."""
    argv = ["xrandr", "--output", name, "--primary"]
    if mode is not None:
        w, h, rate = mode
        argv += ["--mode", f"{w}x{h}", "--rate", f"{rate:g}"]
    else:
        argv += ["--auto"]
    for other in off:
        argv += ["--output", other, "--off"]
    return argv


def parse_monitors(text: str) -> list[str]:
    """Monitor names from `xrandr --listmonitors`."""
    return [m.group(1) for m in _MONITOR.finditer(text)]


class X11(WM):
    name = "x11"
    has_primary = True
    clipboard_tool = "xclip"

    def __init__(self, run=run) -> None:
        self._run = run

    async def _qtile(self, *args: str) -> Any:
        return await qtile(*args, run=self._run)

    # ── workspaces and windows ──────────────────────────────────────────
    async def workspaces(self) -> list[Workspace]:
        groups = await self._qtile("-o", "root", "-f", "get_groups")
        current = await self._qtile("-o", "group", "-f", "info")
        return [Workspace(name, shown=bool(current)
                          and current.get("name") == name)
                for name in groups or {}]

    async def windows(self) -> list[Window]:
        wins = await self._qtile("-o", "root", "-f", "windows") or []
        groups = await self._qtile("-o", "root", "-f", "get_groups") or {}
        return [self._window(w, groups) for w in wins]

    @staticmethod
    def _window(w: dict, groups: dict) -> Window:
        classes = tuple(w.get("wm_class") or ())
        group = str(w.get("group"))
        return Window(id=w["id"], app=(classes or ("?",))[-1],
                      title=w.get("name", ""), workspace=group,
                      classes=classes,
                      active=(groups.get(group) or {}).get("focus")
                      == w.get("name"))

    async def focus_workspace(self, name: str) -> None:
        await self._qtile("-o", "group", name, "-f", "toscreen")

    async def focus_window(self, wid: int) -> Window:
        wid_s = str(int(wid))
        info = await self._qtile("-o", "window", wid_s, "-f", "info")
        try:
            await self._qtile("-o", "group", str(info["group"]),
                              "-f", "toscreen")
            await self._qtile("-o", "window", wid_s, "-f", "focus")
            info.setdefault("id", int(wid))
            return self._window(info, {})
        except (KeyError, TypeError, AttributeError) as e:
            raise WMError(str(e)) from None

    def follow_workspaces(self, window, on_change) -> Follower:
        return EwmhFollower(window.display, on_change)

    # ── screenshots ─────────────────────────────────────────────────────
    async def screenshot(self, target: str = "screen", *,
                         monitor: int | None = None,
                         window: int | None = None) -> Shot:
        crop = None
        what = "the whole screen"
        try:
            if target == "monitor":
                screens = await self._qtile("-o", "root", "-f",
                                            "get_screens") or []
                if monitor is not None:
                    index = int(monitor)
                else:
                    index = int((await self._qtile("-o", "screen", "-f",
                                                   "info")
                                 or {}).get("index", 0))
                screen = next((s for s in screens
                               if s.get("index") == index), None)
                if screen is None:
                    raise NotFound(f"no monitor {index}; there are "
                                   f"{len(screens)} (0 to {len(screens) - 1})")
                crop = (screen["x"], screen["y"], screen["width"],
                        screen["height"])
                what = f"monitor {index}"
            elif target == "window":
                info = await (self._qtile("-o", "window", str(int(window)),
                                          "-f", "info")
                              if window is not None
                              else self._qtile("-o", "window", "-f", "info"))
                crop = (info["x"], info["y"], info["width"], info["height"])
                cls = (info.get("wm_class") or ["?"])[-1]
                what = (f"window {info.get('id', window)} "
                        f"({cls}: {info.get('name', '')})")
        except NotFound:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError) as e:
            raise NotFound(f"can't find what to capture: {e}") from None
        argv = ["import", "-silent", "-window", "root"]
        if crop is not None:
            x, y, w, h = crop
            argv += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
        rc, data, err = await self._run(argv + ["png:-"], timeout=20)
        if rc != 0:
            raise WMError(f"the screenshot failed: {err or rc}")
        return Shot(data, what)

    # ── clipboard ───────────────────────────────────────────────────────
    def copy(self, value: str, selection: str = "clipboard") -> bool:
        """Owning a selection means answering every SelectionRequest for
        as long as we hold it, which a window that parks and unparks is
        badly placed to do — so the text is handed to `xclip`, which
        forks and serves it until something else takes the selection."""
        if not value or shutil.which("xclip") is None:
            if value:
                log.warning("cannot copy: xclip is not installed")
            return False
        serve(["xclip", "-selection", selection, "-in"], value, "xclip")
        return True

    async def paste(self, selection: str = "clipboard") -> str:
        rc, out, err = await self._run(
            ["xclip", "-selection", selection, "-o"], timeout=5)
        if rc != 0:
            raise WMError(err or f"the {selection} is empty or not text")
        return out.decode(errors="replace")

    # ── outputs ─────────────────────────────────────────────────────────
    async def outputs(self) -> list[Output]:
        _rc, out, _err = await self._run(["xrandr", "--query"], timeout=5.0)
        return parse_xrandr(out.decode(errors="replace"))

    async def use_only(self, name: str, mode: Mode | None,
                       off: Sequence[str]) -> None:
        rc, _out, err = await self._run(use_only_argv(name, mode, off),
                                        timeout=15.0)
        if rc != 0:
            log.warning("display: xrandr: %s", err or rc)

    async def virtual_monitors(self) -> list[str]:
        _rc, out, _err = await self._run(["xrandr", "--listmonitors"],
                                         timeout=5.0)
        return parse_monitors(out.decode(errors="replace"))

    # ── session ─────────────────────────────────────────────────────────
    def logout(self) -> None:
        # qtile's IPC shutdown.
        fire(self._run(["qtile", "cmd-obj", "-o", "cmd", "-f", "shutdown"]))


class EwmhFollower(Follower):
    """The workspace readout from EWMH root-window properties.

    Event-driven, no polling: PropertyNotify on the root covers
    _NET_CURRENT_DESKTOP / _NET_NUMBER_OF_DESKTOPS / _NET_CLIENT_LIST,
    and each client is watched for _NET_WM_DESKTOP and _NET_WM_STATE
    (_NET_WM_STATE_DEMANDS_ATTENTION is urgency)."""

    def __init__(self, display,
                 on_change: Callable[[WorkspaceState], None]) -> None:
        self.display = display
        self.on_change = on_change
        self._closed = False
        self._clients_dirty = True
        self._desktop_cache: dict[int, int] = {}
        self._urgent_cache: dict[int, bool] = {}
        self._watched: set[int] = set()
        display.add_root_listener(self._on_root_property)
        self._refresh()

    def _on_root_property(self, ev) -> None:
        if self._closed:
            return
        try:
            name = self.display.conn.core.GetAtomName(
                ev.atom).reply().name.to_string()
        except Exception:
            return
        if name in ("_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS"):
            self._refresh()
        elif name == "_NET_CLIENT_LIST":
            self._clients_dirty = True
            self._refresh()

    def _on_client_property(self, ev) -> None:
        """A watched client changed desktop or urgency."""
        import xcffib.xproto as xp
        if self._closed or not isinstance(ev, xp.PropertyNotifyEvent):
            # Someone else here may select more on this window (the
            # display merges masks per window): not ours.
            return
        self._desktop_cache.pop(ev.window, None)
        self._urgent_cache.pop(ev.window, None)
        self._clients_dirty = True
        self._refresh()

    def _refresh(self) -> None:
        d = self.display
        root = d.root
        count = d.get_int(root, "_NET_NUMBER_OF_DESKTOPS") or 0
        current = d.get_int(root, "_NET_CURRENT_DESKTOP") or 0
        clients = d.get_ints(root, "_NET_CLIENT_LIST", 512)

        if self._clients_dirty:
            desktops: dict[int, int] = {}
            urgents: dict[int, bool] = {}
            demands = d.atom("_NET_WM_STATE_DEMANDS_ATTENTION")
            for xid in clients:
                if xid not in self._watched:
                    d.watch_window_properties(xid)
                    d.on(xid, self._on_client_property)
                    self._watched.add(xid)
                desk = self._desktop_cache.get(xid)
                if desk is None:
                    desk = d.get_int(xid, "_NET_WM_DESKTOP")
                if desk is not None:
                    desktops[xid] = desk
                urgent = self._urgent_cache.get(xid)
                if urgent is None:
                    urgent = demands in d.get_ints(xid, "_NET_WM_STATE", 32)
                urgents[xid] = urgent
            self._desktop_cache, self._urgent_cache = desktops, urgents
            for stale in self._watched - set(clients):
                d.off(stale, self._on_client_property)
            self._watched &= set(clients)
            self._clients_dirty = False

        per = [0] * count
        urgent = [False] * count
        for xid, desk in self._desktop_cache.items():
            if 0 <= desk < count:
                per[desk] += 1
                if self._urgent_cache.get(xid):
                    urgent[desk] = True
        self.on_change(WorkspaceState(count, current, tuple(per),
                                      tuple(urgent)))

    def show(self, index: int) -> None:
        self.display.send_client_message(
            self.display.root, "_NET_CURRENT_DESKTOP", [index])

    def close(self) -> None:
        self._closed = True
        for xid in self._watched:
            self.display.off(xid, self._on_client_property)
        self._watched.clear()
