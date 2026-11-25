"""The window manager, as the shell uses it outside its core: the bar's
workspace readout, the windows and the focus for the apps' line, the
clipboard, the hands the laptop lends the hub's agent (agent/laptop.py,
agent/desk.py), the display menu, the keyboard layout menu and logging
out. `WM` says what the shell asks; niri is what answers, found by
$NIRI_SOCKET (niri exports it to everything it starts).

    niri.py      niri over its IPC socket, and Wayland for the rest
                 (wayland.py)
    wayland.py   what any Wayland compositor with the common protocols
                 gives: wl-clipboard, and screenshots over wlr-screencopy
    tiling.py    for niri: a column layout like qtile's `Columns`,
                 driven over niri's IPC (`python -m indigoshell.wm.tiling`)

This module and niri.py, wayland.py and tiling.py import only the
standard library at load time (skia only when a screenshot is
composed), so the tiling helper runs from niri's config with a bare
python3.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, NamedTuple, Sequence

log = logging.getLogger(__name__)

__all__ = ["WM", "Workspace", "Window", "Mode", "Output", "Shot",
           "WorkspaceState", "Follower", "WindowFollower", "WMError",
           "NotFound", "Unsupported", "get", "run"]


class WMError(RuntimeError):
    """The window manager couldn't do it; the message says why, in words
    fit for the agent or the log."""


class NotFound(WMError):
    """No such window, workspace or monitor."""


class Unsupported(WMError):
    """The compositor has no such thing (no wlr-screencopy)."""


@dataclass(frozen=True)
class Workspace:
    name: str                   # what `focus_workspace` takes
    shown: bool                 # on screen
    output: str = ""            # the monitor it is on, where that is fixed


@dataclass(frozen=True)
class Window:
    id: int                     # what `focus_window` and screenshots take
    app: str                    # its app id
    title: str
    workspace: str              # the Workspace.name it is on, or ""
    active: bool = False        # the focused one of its workspace
    pid: int | None = None      # its client's process


class Mode(NamedTuple):
    width: int
    height: int
    rate: float                 # Hz


@dataclass(frozen=True)
class Output:
    name: str
    connected: bool
    active: bool                # lit, with a place on the screen
    modes: tuple[Mode, ...]
    current: Mode | None


@dataclass(frozen=True)
class Shot:
    png: bytes
    what: str                   # "monitor 0", "window 12 (kitty: ~)"


@dataclass(frozen=True)
class WorkspaceState:
    """The bar's readout: the workspaces of the bar's monitor, which one
    is shown, how many windows each holds, and which want attention."""
    count: int
    current: int
    windows: tuple[int, ...]
    urgent: tuple[bool, ...]


class Follower:
    """Follows the workspaces for the bar (`WM.follow_workspaces`)."""

    def show(self, index: int) -> None:
        """Show the `index`th workspace of the readout."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop following."""


class WindowFollower:
    """Follows the windows and the keyboard focus, for the apps' line
    (`WM.follow_windows`; applink/service.py): which window has
    the focus and whose process it is, which windows a process has, and
    the workspaces a window can be put on without the user seeing it."""

    def focused(self) -> Window | None:
        """The window with the keyboard focus; None while none has it
        (a layer surface took the keyboard, or nothing is open)."""
        raise NotImplementedError

    def ids_of(self, pid: int) -> set[int]:
        """The ids of the windows process `pid` has open now."""
        raise NotImplementedError

    def spare_workspace(self, taken: set = frozenset()) -> tuple[object, int | None, str]:
        """Where a window the user must not see appearing goes: (the
        workspace's reference for `move`, its index among the bar's
        workspaces, how it was chosen), or (None, None, why none).
        `taken`: references of workspaces already given out so."""
        raise NotImplementedError

    def workspace_of(self, window_id: int) -> tuple[object, int | None] | None:
        """The reference and the bar's index of the workspace a window is
        on; None for no such window."""
        raise NotImplementedError

    def move(self, window_id: int, workspace) -> None:
        """Move a window to a workspace (a reference from above) without
        focusing it or showing the workspace; fire and forget."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop following."""


class WM:
    """What the shell asks of the window manager outside its core. Every
    coroutine raises WMError (or NotFound, Unsupported) with a message
    fit for the agent."""

    name = ""
    # The program `copy` hands text to, for "… is missing".
    clipboard_tool = ""

    # ── workspaces and windows ──────────────────────────────────────────
    async def workspaces(self) -> list[Workspace]:
        raise NotImplementedError

    async def windows(self) -> list[Window]:
        raise NotImplementedError

    async def focus_workspace(self, name: str) -> None:
        raise NotImplementedError

    async def focus_window(self, wid: int) -> Window:
        """Bring a window forward on its workspace and focus it; the
        window as it was found."""
        raise NotImplementedError

    def follow_workspaces(self, window,
                          on_change: Callable[[WorkspaceState], None]
                          ) -> Follower:
        """Call `on_change` with the readout now and whenever it may
        have changed, for the bar in `window` (the shell's Window: the
        monitor under it)."""
        raise NotImplementedError

    def follow_windows(self, on_change: Callable[["WindowFollower"], None]
                       ) -> "WindowFollower":
        """Call `on_change(follower)` whenever the windows or the focus
        may have changed."""
        raise NotImplementedError

    # ── screenshots ─────────────────────────────────────────────────────
    async def screenshot(self, target: str = "screen", *,
                         monitor: int | None = None,
                         window: int | None = None) -> Shot:
        """A PNG of every monitor ("screen"), one monitor (its number,
        default the one in use) or one window (default the focused
        one). Nothing is saved and the clipboard is left alone."""
        raise NotImplementedError

    # ── clipboard ───────────────────────────────────────────────────────
    def copy(self, value: str, selection: str = "clipboard") -> bool:
        """Put text on `selection` ("clipboard" or "primary"), served in
        the background; False when there is nothing to copy or the tool
        is missing. Needs a running loop."""
        raise NotImplementedError

    async def paste(self, selection: str = "clipboard") -> str:
        raise NotImplementedError

    # ── outputs ─────────────────────────────────────────────────────────
    async def outputs(self) -> list[Output]:
        raise NotImplementedError

    async def use_only(self, name: str, mode: Mode | None,
                       off: Sequence[str]) -> None:
        """Light `name` at `mode` (None: its own pick) as the one in
        use and turn `off` off."""
        raise NotImplementedError

    # ── keyboard ────────────────────────────────────────────────────────
    async def keyboard_layouts(self) -> list[tuple[str, bool]]:
        """The window manager's keyboard layouts as (name, in use)."""
        raise NotImplementedError

    async def switch_keyboard_layout(self, index: int) -> None:
        raise NotImplementedError

    # ── session ─────────────────────────────────────────────────────────
    def logout(self) -> None:
        """End the session, without asking; fire and forget."""
        raise NotImplementedError


_current: WM | None = None


def get() -> WM:
    """This process's window manager: niri, at $NIRI_SOCKET. Raises
    WMError without it."""
    global _current
    if _current is None:
        sock = os.environ.get("NIRI_SOCKET")
        if not sock:
            raise WMError("no niri here ($NIRI_SOCKET is not set)")
        from .niri import Niri
        _current = Niri(sock)
        log.debug("window manager: %s", _current.name)
    return _current


async def run(argv: Sequence[str], *, data: bytes | None = None,
              timeout: float = 15.0) -> tuple[int, bytes, str]:
    """A subprocess's exit code, stdout bytes and stderr text."""
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return 127, b"", str(e)
    try:
        out, err = await asyncio.wait_for(p.communicate(data), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, b"", f"{argv[0]} timed out"
    return p.returncode or 0, out, err.decode(errors="replace").strip()


def serve(argv: Sequence[str], value: str, what: str) -> None:
    """Hand `value` to a clipboard tool on its stdin, in its own session
    so the server it forks outlives a reload. Fire and forget."""
    async def _go():
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            await proc.communicate(value.encode())
        except OSError:
            log.debug("%s failed", what, exc_info=True)

    asyncio.get_running_loop().create_task(_go())


def fire(coro) -> None:
    """Run a coroutine on the running loop and forget it; failures go to
    the log."""
    async def _go():
        try:
            await coro
        except Exception:
            log.warning("window manager request failed", exc_info=True)

    asyncio.get_running_loop().create_task(_go())
