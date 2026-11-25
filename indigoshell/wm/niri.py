"""niri, over its IPC socket ($NIRI_SOCKET), and Wayland for the rest
(wayland.py: wl-clipboard, screencopy).

The socket takes one JSON request per line and answers one line,
`{"Ok": …}` or `{"Err": "…"}`: a bare string for a request without
fields (`"Workspaces"` → `{"Ok": {"Workspaces": [...]}}`), an object
for the rest (`{"Action": {"FocusWindow": {"id": 7}}}`,
`{"Output": {"output": "eDP-1", "action": "Off"}}`). `"EventStream"`
answers `{"Ok": "Handled"}` and then streams events, one per line, the
whole current state first (WorkspacesChanged, WindowsChanged, …) and
then each change. Nothing here leans on more than the shapes niri
documents as stable; fields it adds later are ignored.

How niri differs from qtile, as the interface sees it:

- Workspaces are per monitor, dynamic, stacked vertically; each
  monitor keeps one empty one at its end. A workspace's name (what
  the laptop's focus hand takes) is its configured name, else its index on its
  monitor ("2"), with the monitor's name in front ("eDP-1:2") when
  there are several monitors. The bar shows the workspaces of its own
  monitor, without that spare empty one at the end.
- There is no primary monitor (`has_primary` is False): "use alone"
  lights the output at its mode and turns the others off, and niri
  forgets that at its next config reload of the outputs. No RandR
  monitors either, so nothing marks a braindance session.
- Keyboard layouts are niri's own (its config's xkb layouts): the
  layout menu lists those and switches among them.
- Screenshots go over wlr-screencopy, never through niri's own
  screenshot actions: those put the picture on the clipboard, every
  time. A floating window is cut out of its monitor's picture; niri
  doesn't say where a tiled window sits on screen, so asking for one
  gives its whole monitor, and the reply says so.

Standard library only at load time (tiling.py runs on a bare python3).
"""

import asyncio
import contextlib
import json
import logging
from typing import Any, Callable, Sequence

from . import (WM, Follower, Mode, NotFound, Output, Shot, Window, WMError,
               Workspace, WorkspaceState, fire)
from . import wayland

log = logging.getLogger(__name__)

STREAM_LIMIT = 1 << 24          # one event line: the whole window list


# ── the socket ──────────────────────────────────────────────────────────
def unwrap(line: bytes) -> Any:
    """A reply line's `Ok`, or WMError with niri's `Err`."""
    if not line:
        raise WMError("niri closed the connection")
    try:
        reply = json.loads(line)
    except ValueError:
        raise WMError(f"niri answered something that isn't JSON: "
                      f"{line[:80]!r}") from None
    if isinstance(reply, dict) and "Err" in reply:
        raise WMError(f"niri: {reply['Err']}")
    if not isinstance(reply, dict) or "Ok" not in reply:
        raise WMError(f"niri answered {line[:80]!r}")
    return reply["Ok"]


class Ipc:
    """niri's socket: one connection per request, and event streams."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def request(self, req: Any) -> Any:
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.path, limit=STREAM_LIMIT)
        except OSError as e:
            raise WMError(f"can't reach niri: {e}") from None
        try:
            writer.write(json.dumps(req).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        except OSError as e:
            raise WMError(f"niri: {e}") from None
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return unwrap(line)

    async def query(self, name: str) -> Any:
        """A request without fields: its reply's payload."""
        reply = await self.request(name)
        return reply.get(name) if isinstance(reply, dict) else reply

    async def action(self, name: str, **fields) -> Any:
        return await self.request({"Action": {name: fields}})

    async def output(self, output: str, action: Any) -> str:
        """An output change: "Applied" or "OutputWasMissing"."""
        reply = await self.request({"Output": {"output": output,
                                               "action": action}})
        return (reply or {}).get("OutputConfigChanged", "")

    async def events(self):
        """The event stream, as dicts, until niri closes it."""
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.path, limit=STREAM_LIMIT)
        except OSError as e:
            raise WMError(f"can't reach niri: {e}") from None
        try:
            writer.write(b'"EventStream"\n')
            await writer.drain()
            unwrap(await reader.readline())
            while line := await reader.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


# ── the state, from events ──────────────────────────────────────────────
class State:
    """Workspaces and windows by id, kept current from the event stream:
    the stream starts with the whole state and then sends each change."""

    def __init__(self) -> None:
        self.workspaces: dict[int, dict] = {}
        self.windows: dict[int, dict] = {}

    def apply(self, event: dict) -> bool:
        """Take one event in; whether it was about workspaces or
        windows."""
        if len(event) != 1:
            return False
        (kind, e), = event.items()
        if kind == "WorkspacesChanged":
            self.workspaces = {w["id"]: w for w in e["workspaces"]}
        elif kind == "WorkspaceUrgencyChanged":
            if e["id"] in self.workspaces:
                self.workspaces[e["id"]]["is_urgent"] = e["urgent"]
        elif kind == "WorkspaceActivated":
            ws = self.workspaces.get(e["id"])
            if ws is None:
                return False
            for other in self.workspaces.values():
                if other["output"] == ws["output"]:
                    other["is_active"] = other is ws
                if e["focused"]:
                    other["is_focused"] = other is ws
        elif kind == "WorkspaceActiveWindowChanged":
            if e["workspace_id"] in self.workspaces:
                self.workspaces[e["workspace_id"]]["active_window_id"] = \
                    e["active_window_id"]
        elif kind == "WindowsChanged":
            self.windows = {w["id"]: w for w in e["windows"]}
        elif kind == "WindowOpenedOrChanged":
            w = e["window"]
            if w.get("is_focused"):
                for other in self.windows.values():
                    other["is_focused"] = False
            self.windows[w["id"]] = w
        elif kind == "WindowClosed":
            self.windows.pop(e["id"], None)
        elif kind == "WindowFocusChanged":
            for w in self.windows.values():
                w["is_focused"] = w["id"] == e["id"]
        elif kind == "WindowUrgencyChanged":
            if e["id"] in self.windows:
                self.windows[e["id"]]["is_urgent"] = e["urgent"]
        elif kind == "WindowLayoutsChanged":
            for wid, layout in e["changes"]:
                if wid in self.windows:
                    self.windows[wid]["layout"] = layout
        else:
            return False
        return True


# ── from niri's shapes to the interface's ───────────────────────────────
def output_order(outputs: dict) -> list[str]:
    """Output names in screen order: the lit ones left to right, top to
    bottom, then the dark ones by name. Monitor numbers count these."""
    def key(name):
        lg = outputs[name].get("logical")
        return (lg is None, (lg or {}).get("x", 0), (lg or {}).get("y", 0),
                name)
    return sorted(outputs, key=key)


def _sorted_workspaces(workspaces, order: Sequence[str] = ()) -> list[dict]:
    rank = {name: i for i, name in enumerate(order)}
    return sorted(workspaces, key=lambda w: (
        rank.get(w.get("output"), len(rank)), w.get("output") or "",
        w.get("idx", 0)))


def labels(workspaces) -> dict[int, str]:
    """Each workspace's name for the interface: its own name, else its
    index on its monitor, with the monitor in front when there are
    several."""
    several = len({w.get("output") for w in workspaces}) > 1
    return {w["id"]: (w.get("name") or
                      (f"{w.get('output')}:{w.get('idx')}" if several
                       else str(w.get("idx"))))
            for w in workspaces}


def workspace_list(workspaces, order: Sequence[str] = ()) -> list[Workspace]:
    names = labels(workspaces)
    return [Workspace(names[w["id"]], bool(w.get("is_active")),
                      w.get("output") or "")
            for w in _sorted_workspaces(workspaces, order)]


def window_list(windows, workspaces, order: Sequence[str] = ()
                ) -> list[Window]:
    """Windows in workspace order, then by place in the layout."""
    names = labels(workspaces)
    rank = {w["id"]: i for i, w in
            enumerate(_sorted_workspaces(workspaces, order))}
    active = {w["id"]: w.get("active_window_id") for w in workspaces}

    def key(w):
        layout = w.get("layout") or {}
        return (rank.get(w.get("workspace_id"), len(rank)),
                bool(w.get("is_floating")),
                tuple(layout.get("pos_in_scrolling_layout") or ()), w["id"])
    out = []
    for w in sorted(windows, key=key):
        app = w.get("app_id") or ""
        out.append(Window(
            id=w["id"], app=app or "?", title=w.get("title") or "",
            workspace=names.get(w.get("workspace_id"), ""),
            classes=(app,) if app else (),
            active=active.get(w.get("workspace_id")) == w["id"]))
    return out


def bar_workspaces(workspaces, windows, output: str | None) -> list[dict]:
    """The workspaces the bar shows for `output`, in order: all of that
    monitor's but the spare empty one niri keeps at the end (unnamed,
    windowless, not shown)."""
    mine = sorted((w for w in workspaces if w.get("output") == output),
                  key=lambda w: w.get("idx", 0))
    used = {w.get("workspace_id") for w in windows}
    if mine and not mine[-1].get("name") and not mine[-1].get("is_active") \
            and mine[-1]["id"] not in used:
        mine.pop()
    return mine


def bar_state(workspaces, windows, output: str | None) -> WorkspaceState:
    shown = bar_workspaces(workspaces, windows, output)
    per = {w["id"]: 0 for w in shown}
    for win in windows:
        if win.get("workspace_id") in per:
            per[win["workspace_id"]] += 1
    current = next((i for i, w in enumerate(shown) if w.get("is_active")), 0)
    return WorkspaceState(len(shown), current,
                          tuple(per[w["id"]] for w in shown),
                          tuple(bool(w.get("is_urgent")) for w in shown))


def output_list(outputs: dict) -> list[Output]:
    out = []
    for name in output_order(outputs):
        o = outputs[name]
        modes = tuple(Mode(m["width"], m["height"],
                           m["refresh_rate"] / 1000.0)
                      for m in o.get("modes") or ())
        cur = o.get("current_mode")
        current = modes[cur] if cur is not None and 0 <= cur < len(modes) \
            else None
        # niri lists connected outputs only; lit ones have a place.
        out.append(Output(name, True, o.get("logical") is not None, False,
                          modes, current))
    return out


def use_only_requests(name: str, mode: Mode | None,
                      off: Sequence[str]) -> list[tuple[str, Any]]:
    """(output, action) pairs: the one lit and set first, so there is
    never a moment with nothing on."""
    if mode is None:
        set_mode = {"Mode": {"mode": "Automatic"}}
    else:
        set_mode = {"Mode": {"mode": {"Specific": {
            "width": mode.width, "height": mode.height,
            "refresh": mode.rate}}}}
    return ([(name, "On"), (name, set_mode)]
            + [(other, "Off") for other in off if other != name])


def output_at(outputs: dict, x: float, y: float) -> str | None:
    for name, o in outputs.items():
        lg = o.get("logical")
        if lg and lg["x"] <= x < lg["x"] + lg["width"] \
                and lg["y"] <= y < lg["y"] + lg["height"]:
            return name
    return None


# ── the backend ─────────────────────────────────────────────────────────
class Niri(WM):
    name = "niri"
    has_primary = False
    clipboard_tool = "wl-copy"

    def __init__(self, socket_path: str, capture=None) -> None:
        self.ipc = Ipc(socket_path)
        # Stands in for wayland.capture in tests.
        self._capture = capture or wayland.capture

    async def _layout(self) -> tuple[list, list, list[str]]:
        workspaces = await self.ipc.query("Workspaces") or []
        windows = await self.ipc.query("Windows") or []
        outputs = await self.ipc.query("Outputs") or {}
        return workspaces, windows, output_order(outputs)

    # ── workspaces and windows ──────────────────────────────────────────
    async def workspaces(self) -> list[Workspace]:
        workspaces, _windows, order = await self._layout()
        return workspace_list(workspaces, order)

    async def windows(self) -> list[Window]:
        workspaces, windows, order = await self._layout()
        return window_list(windows, workspaces, order)

    async def focus_workspace(self, name: str) -> None:
        workspaces = await self.ipc.query("Workspaces") or []
        names = labels(workspaces)
        wid = next((i for i, n in names.items() if n == name), None)
        if wid is None:
            raise NotFound(f"no workspace {name!r}; there are "
                           + ", ".join(names[w["id"]] for w in
                                       _sorted_workspaces(workspaces)))
        await self.ipc.action("FocusWorkspace", reference={"Id": wid})

    async def focus_window(self, wid: int) -> Window:
        win = next((w for w in await self.windows() if w.id == int(wid)),
                   None)
        if win is None:
            raise NotFound(f"no window {wid}")
        await self.ipc.action("FocusWindow", id=win.id)
        return win

    def follow_workspaces(self, window, on_change) -> Follower:
        return NiriFollower(self.ipc, window, on_change)

    # ── screenshots ─────────────────────────────────────────────────────
    async def screenshot(self, target: str = "screen", *,
                         monitor: int | None = None,
                         window: int | None = None) -> Shot:
        outputs = await self.ipc.query("Outputs") or {}
        lit = [n for n in output_order(outputs) if outputs[n].get("logical")]
        if not lit:
            raise NotFound("no monitor is on")
        if target == "monitor":
            if monitor is None:
                focused = await self.ipc.query("FocusedOutput") or {}
                index = lit.index(focused["name"]) \
                    if focused.get("name") in lit else 0
            else:
                try:
                    index = int(monitor)
                except (TypeError, ValueError):
                    raise NotFound(f"can't find what to capture: monitor "
                                   f"{monitor!r}") from None
            if not 0 <= index < len(lit):
                raise NotFound(f"no monitor {index}; there are {len(lit)} "
                               f"(0 to {len(lit) - 1})")
            image = await self._monitor(lit[index])
            return Shot(wayland.png(image), f"monitor {index} ({lit[index]})")
        if target == "window":
            return await self._window_shot(window, outputs, lit)
        return Shot(wayland.png(await self._screen(outputs, lit)),
                    "the whole screen")

    async def _monitor(self, name: str):
        frames = await self._capture([name])
        return wayland.upright(frames[0])

    async def _screen(self, outputs: dict, lit: list[str]):
        """Every lit monitor where it sits, at the finest scale among
        them."""
        frames = {f.name: f for f in await self._capture(lit)}
        boxes = {n: outputs[n]["logical"] for n in lit}
        if len(lit) == 1:
            return wayland.upright(frames[lit[0]])
        scale = max(b.get("scale", 1.0) for b in boxes.values())
        x0 = min(b["x"] for b in boxes.values())
        y0 = min(b["y"] for b in boxes.values())
        x1 = max(b["x"] + b["width"] for b in boxes.values())
        y1 = max(b["y"] + b["height"] for b in boxes.values())
        parts = [(wayland.upright(frames[n]), (b["x"] - x0) * scale,
                  (b["y"] - y0) * scale, b["width"] * scale,
                  b["height"] * scale) for n, b in boxes.items()]
        return wayland.compose(parts, round((x1 - x0) * scale),
                               round((y1 - y0) * scale))

    async def _window_shot(self, wid, outputs: dict, lit: list[str]) -> Shot:
        windows = await self.ipc.query("Windows") or []
        workspaces = await self.ipc.query("Workspaces") or []
        try:
            win = next(w for w in windows
                       if (w["id"] == int(wid) if wid is not None
                           else w.get("is_focused")))
        except (StopIteration, TypeError, ValueError):
            raise NotFound("can't find what to capture: "
                           + (f"no window {wid}" if wid is not None
                              else "no window is focused")) from None
        names = labels(workspaces)
        ws = next((w for w in workspaces
                   if w["id"] == win.get("workspace_id")), None)
        about = (f"window {win['id']} ({win.get('app_id') or '?'}: "
                 f"{win.get('title') or ''})")
        if ws is None or not ws.get("is_active") or ws.get("output") not in lit:
            where = names.get(win.get("workspace_id"), "none")
            raise NotFound(f"{about} is on workspace {where}, which isn't "
                           "shown; bring it forward first")
        image = await self._monitor(ws["output"])
        layout = win.get("layout") or {}
        pos = layout.get("tile_pos_in_workspace_view")
        if pos is None:
            index = lit.index(ws["output"])
            return Shot(wayland.png(image),
                        f"monitor {index} ({ws['output']}), the one {about} "
                        "is on: niri doesn't say where a tiled window sits "
                        "on screen, so this is its whole monitor")
        lg = outputs[ws["output"]]["logical"]
        scale = image.width() / lg["width"]
        dx, dy = layout.get("window_offset_in_tile") or (0, 0)
        w, h = layout.get("window_size") or layout.get("tile_size")
        try:
            image = wayland.crop(image, (pos[0] + dx) * scale,
                                 (pos[1] + dy) * scale, w * scale, h * scale)
        except WMError:
            raise NotFound(f"{about} is off the screen") from None
        return Shot(wayland.png(image), about)

    # ── clipboard ───────────────────────────────────────────────────────
    def copy(self, value: str, selection: str = "clipboard") -> bool:
        return wayland.copy(value, selection)

    async def paste(self, selection: str = "clipboard") -> str:
        return await wayland.paste(selection)

    # ── outputs ─────────────────────────────────────────────────────────
    async def outputs(self) -> list[Output]:
        return output_list(await self.ipc.query("Outputs") or {})

    async def use_only(self, name: str, mode: Mode | None,
                       off: Sequence[str]) -> None:
        for output, action in use_only_requests(name, mode, off):
            said = await self.ipc.output(output, action)
            if said == "OutputWasMissing":
                if output == name:
                    raise NotFound(f"no output {name}")
                log.info("display: %s went away meanwhile", output)

    # ── keyboard ────────────────────────────────────────────────────────
    async def keyboard_layouts(self) -> list[tuple[str, bool]]:
        layouts = await self.ipc.query("KeyboardLayouts") or {}
        current = layouts.get("current_idx")
        return [(name, i == current)
                for i, name in enumerate(layouts.get("names") or ())]

    async def switch_keyboard_layout(self, index: int) -> None:
        await self.ipc.action("SwitchLayout", layout={"Index": int(index)})

    # ── session ─────────────────────────────────────────────────────────
    def logout(self) -> None:
        async def quit():
            # niri may go before it answers.
            with contextlib.suppress(WMError):
                await self.ipc.action("Quit", skip_confirmation=True)
        fire(quit())


class NiriFollower(Follower):
    """The bar's readout from niri's event stream: the workspaces of the
    monitor the bar is on (the one under its window's middle, else the
    one focused when it started)."""

    RETRY_S = 2.0

    def __init__(self, ipc: Ipc, window,
                 on_change: Callable[[WorkspaceState], None]) -> None:
        self.ipc = ipc
        self.window = window
        self.on_change = on_change
        self.state = State()
        self.outputs: dict = {}
        self._fallback: str | None = None
        self._shown: list[dict] = []
        self._task = None
        try:
            self._task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:
            log.warning("workspaces: no running loop to follow niri on")

    def output(self) -> str | None:
        win = self.window
        try:
            x = win.x + win.width / 2
            y = win.y + win.height / 2
        except (AttributeError, TypeError):
            x = y = None
        if x is not None:
            hit = output_at(self.outputs, x, y)
            if hit is not None:
                return hit
        present = {w.get("output") for w in self.state.workspaces.values()}
        if self._fallback not in present:
            focused = next((w for w in self.state.workspaces.values()
                            if w.get("is_focused")), None)
            self._fallback = (focused or {}).get("output") or next(
                iter(sorted(p for p in present if p)), None)
        return self._fallback

    def refresh(self) -> None:
        workspaces = list(self.state.workspaces.values())
        windows = list(self.state.windows.values())
        output = self.output()
        self._shown = bar_workspaces(workspaces, windows, output)
        self.on_change(bar_state(workspaces, windows, output))

    async def _run(self) -> None:
        while True:
            try:
                self.outputs = await self.ipc.query("Outputs") or {}
                async for event in self.ipc.events():
                    if "WorkspacesChanged" in event:
                        # Workspaces move when monitors come and go.
                        self.outputs = await self.ipc.query("Outputs") or {}
                    if self.state.apply(event):
                        self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("workspaces: niri's event stream: %s", e)
            await asyncio.sleep(self.RETRY_S)

    def show(self, index: int) -> None:
        if 0 <= index < len(self._shown):
            fire(self.ipc.action("FocusWorkspace",
                                 reference={"Id": self._shown[index]["id"]}))

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
