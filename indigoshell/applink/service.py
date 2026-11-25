"""The daemon's end of the apps' line: who is connected, who has focus,
the bars they send, and the keys the shell takes from them.

`AppService` is a SERVICES entry. It listens at `protocol.socket_path()`
on the daemon's loop; each connection is one app window (`Link`), which
announces itself first (the app, its own id for the window, its X
window: PROTOCOL.md) and is then an `AppWindow`, the same class for
every app: nothing of any app's is here. The window's `bar` is checked
(bar.py `bar_problems`: a broken one is ignored whole, the last good one
kept, a warning once per problem) and shown in its `BarView`
(applink/view.py), which the top bar's context area shows while the
window has focus (applink/context.py); the user's doings go back to the
app as the bar's events.

Focus. The service follows the root window's `_NET_ACTIVE_WINDOW`, the
window manager's word for which client window has the keyboard, and
knows the app window that has it (`focused`): the one whose announced
X window it is, or, for a dialog of an app's, the one the dialog is
transient for. Anyone that wants to hear about it (the top bar's
context area, applink/context.py) calls `follow`. A window manager that
reparents would name its frame here instead of the app's window; qtile
does not, and none other is supported.

Keys. The chords of a window's inputs and menus (bar.py `chords`:
Flux's Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M) are the keys the shell takes
from it. Whenever they change the service grabs each on that window,
passively (Display.grab_keys), once per combination of the lock
modifiers so Caps Lock and Num Lock change nothing, by the keycodes the
current keymap gives the key. The X server activates such a grab when
the chord is pressed while the window is the focus window or holds it,
or while the focus is on an ancestor of it (the root, when nothing has
the focus) and the pointer is inside it; it then sends the press here
instead of to the app, and every key after it until that key is let
go. So there is no moment where the app has seen the chord and the
shell has not: the shell has the keyboard from the press on. The press
reaches the window's `on_key`, which opens that input or menu
(`open_prompt`): the keyboard held past the chord's release at once
(`hold_keyboard`, an active grab on the app's window, kept for as long
as it is open), its box in the window's section, its list dropping
under the bar (applink/prompt.py, drop.py). Every key typed while it is
open arrives on the app's window (`on_typed`) in the order typed and
goes to it: none reaches the app. The pointer is held too (on the bar's
window, owner_events on: the bar and the list get their clicks), so a
click anywhere else closes it; so does the window losing focus or going.
An app may ask for one with no key pressed (`open`): honoured once its
window has focus (OPEN_WAIT_S). With no top bar over the window, or no
keyboard to be had, the app is told `fallback` and the keyboard goes
back.

The grabs go when the connection closes or the service stops; the X
server drops them itself with the window. They are made again when the
keymap changes (MappingNotify) and when the app announces again. A grab
another client holds already on the window (BadAccess) is said once as a
warning, and that chord is left to the app.

Placing. An app may ask for a window it has made but not yet shown to
go to a workspace before it maps (protocol `place`): Flux's window for
the agent, which works on a workspace of its own, never the one the user
is looking at. The service sets the window's `_NET_WM_DESKTOP` (EWMH's
"desktop", qtile's group by index) and reads it back before it answers
`placed`, so the X server has it when the app's map request comes:
qtile then manages the window on that group from its first map, keeps
it unmapped while the group is not on a screen, and neither switches to
it nor gives it focus (checked against qtile's `Window.set_group` and
`handle_MapRequest`, and by experiment: tests/browser_alone.py).

Which workspace (`pick_desktop`): the highest-numbered one with no
client window on it that no screen shows. Shown: `_NET_CURRENT_DESKTOP`,
any whose `_NET_DESKTOP_VIEWPORT` is not (0, 0) (another monitor's), and
any with a client window mapped on it. Client windows: those in
`_NET_CLIENT_LIST`, by their `_NET_WM_DESKTOP`, and the windows this
service placed (qtile lists a window it keeps unmapped only at its next
update of the list). When every hidden workspace has windows, the
highest-numbered hidden one; when every workspace is shown, none (the
app maps where the window manager puts it). With several monitors an
empty workspace shown on the one at (0, 0) while another has the focus
cannot be told from a hidden one by EWMH; that case is not handled.
`place` naming another window of the same app process (`with`, by the
app's own id for it) puts the new one on that window's workspace: a
popup of a placed window goes where it is.

Lifecycle. A connection that closes (the app window closed, the app
quit, or it replaced its X window and announced again on a new
connection) drops its `AppWindow`. `stop()` (the daemon stopping or
reloading) closes the socket and every connection; each app shows its
own state if the shell stays away (Flux after a few seconds, so that a
reload, which is back sooner, never makes its page jump; a reload says
so first, `restarting`, and the app waits longer still) and connects
again to the next daemon.

One listener per socket. Finding the socket live (another daemon), this
service leaves it alone, says so once, and tries again every few
seconds, taking over once it is free.
"""

import asyncio
import logging
import os
import socket
import struct
from dataclasses import dataclass

import xcffib.xproto as xp

from ..core.voices import voice_names
from ..window import Placement, key_event
from . import bar as B
from . import protocol
from .drop import DROP_WINDOW
from .prompt import Prompt
from .view import BarView

log = logging.getLogger(__name__)

CLAIM_RETRY_S = 5.0                 # while another shell holds the socket
PREPARE_S = 2.0                     # after the start: the drop-down made ahead
LINE_LIMIT = 1 << 20                # bytes in one message: a state with many tabs
_KEY_PRESS = 2
_BUTTON_PRESS = 4

_CHORD_MODIFIERS = {"ctrl": xp.ModMask.Control, "shift": xp.ModMask.Shift}


class AppWindow:
    """One window of an indigoshell app, as the shell sees it.

    `view` is its section of the top bar (applink/view.py), which the
    context area shows while this window has focus (`content`; empty
    until the window's bar comes). `bar` is the last good bar shown; the
    chords of its inputs and menus are the keys the shell takes from the
    window (`keys`: chord -> ("input" | "menu", id)), grabbed again
    whenever they change.
    """

    def __init__(self, link: "Link", wid: int, announce: dict) -> None:
        self.link = link
        self.service = link.service
        self.wid = wid
        self.kind = str(announce.get(B.APP) or "app")   # the app's name, for log lines
        self.app_id = announce.get(B.WINDOW)            # the app's own id for the window
        self._said: set[str] = set()                    # problems warned of, once each
        self.view = BarView(emit=self.send_event, on_open=self._item_opens)
        self.content = self.view
        self.bar: dict | None = None
        self.keys: dict[str, tuple[str, object]] = {}
        self.closed = False
        self.placed: int | None = None      # the workspace `place` put it on
        self._seq = 0

    def on_message(self, msg: dict) -> None:
        """Every message after the announce (`place` is the service's)."""
        kind = msg[protocol.TYPE]
        if kind == B.BAR:
            problems = B.bar_problems(msg)
            if problems:
                self._warn("a bar ignored, the last good one kept", problems)
                return
            self.show_bar(msg)
        elif kind == B.ANSWER:
            problems = B.answer_problems(msg)
            if problems:
                log.debug("%s window 0x%x: answer seq %s for %r dropped: %s", self.kind,
                          self.wid, msg.get("seq"), msg.get("input"), "; ".join(problems))
                self._warn("an answer ignored", problems)
                return
            p = self.service.prompt_of(self)
            if p is not None and p.what == "input" and p.name == msg["input"]:
                p.answer(msg)
            else:
                log.debug("%s window 0x%x: answer seq %s for %r dropped: that input is not "
                          "open", self.kind, self.wid, msg.get("seq"), msg["input"])
        elif kind == B.OPEN:
            what = "input" if "input" in msg else "menu"
            self.service.open_asked(self, what, msg.get(what))
        else:
            log.debug("%s window 0x%x: message %r not understood", self.kind, self.wid, kind)

    def _warn(self, what: str, problems: list[str]) -> None:
        new = [p for p in problems if p not in self._said]
        if not new:
            log.debug("%s window 0x%x: %s again", self.kind, self.wid, what)
            return
        self._said.update(new)
        log.warning("%s window 0x%x: %s: %s", self.kind, self.wid, what, "; ".join(new))

    # ── its bar ─────────────────────────────────────────────────────────
    def show_bar(self, bar: dict) -> None:
        """A good bar (bar.bar_problems found nothing): shown, its chords
        grabbed, an open input or menu of it kept up to date."""
        self.bar = bar
        self.view.set_bar(bar)
        keys = B.chords(bar)
        if keys != self.keys:
            self.keys = keys
            self.service.grab_keys(self)
        prompt = self.service.prompt_of(self)
        if prompt is not None:
            spec = (bar.get(B.INPUTS if prompt.what == "input" else B.MENUS) or {}).get(
                prompt.name)
            if isinstance(spec, dict):
                prompt.refresh_spec(spec)
            else:
                prompt.close("shell")       # the app took it away
        wait = self.service._open_wait
        if wait is not None and wait[0] is self:
            self.service._try_open()        # an open asked before this bar came

    def _item_opens(self, what_id, item) -> None:
        """An item of the bar opens one of its inputs or menus (a click)."""
        what = "input" if what_id in ((self.bar or {}).get(B.INPUTS) or {}) else "menu"
        self.service.open_prompt(self, what, what_id, pressed=False, anchor=item)

    # ── keys ────────────────────────────────────────────────────────────
    def on_key(self, chord: str, key) -> None:
        """One of `keys` was pressed in the window: the shell has the
        keyboard, until that key is let go (`service.hold_keyboard` keeps
        it longer). `key` is the press as a widgets.base.KeyEvent. While
        one of the window's inputs or menus is open it is a key for it."""
        prompt = self.service.prompt_of(self)
        if prompt is not None:
            prompt.key(key)
            return
        what, name = self.keys.get(chord, (None, None))
        if what is not None:
            self.service.open_prompt(self, what, name, pressed=True, chord=chord)

    def on_typed(self, key) -> None:
        """Another key pressed while the shell holds the keyboard for this
        window: reported on the app's window, in the order typed, for the
        input or menu open."""
        prompt = self.service.prompt_of(self)
        if prompt is None:
            log.debug("a key typed into %s window 0x%x with nothing open for it; dropped",
                      self.kind or "app", self.wid)
            return
        prompt.key(key)

    # ── to the app ──────────────────────────────────────────────────────
    def send_event(self, event: dict) -> None:
        """An event of the bar's for the app (PROTOCOL.md, section 3)."""
        self.send(event)

    def next_seq(self) -> int:
        """The next query's seq: rising per connection."""
        self._seq += 1
        return self._seq

    def prompt_closed(self, prompt) -> None:
        """An input or menu of this window's left the screen."""

    def send(self, msg: dict) -> None:
        if not self.closed:
            self.link.send(msg)

    def close(self) -> None:
        """The connection is gone: let go of anything held for the app."""
        self.closed = True


class Link:
    """One connection: an app window before and after it announced."""

    def __init__(self, service: "AppService", writer: asyncio.StreamWriter) -> None:
        self.service = service
        self.writer = writer
        self.window: AppWindow | None = None
        self.closed = False

    def on_message(self, msg: dict) -> None:
        if self.closed:
            return
        announce = msg[protocol.TYPE] == protocol.ANNOUNCE
        if self.window is not None:
            if announce:
                log.debug("app window 0x%x announced again on the same connection; ignored",
                          self.window.wid)
            elif msg[protocol.TYPE] == protocol.PLACE:
                self.service.place(self.window, msg)
            else:
                self.window.on_message(msg)
            return
        if not announce:
            log.debug("message before the announce, ignored: %r", msg[protocol.TYPE])
            return
        handle = msg.get(B.HANDLE)
        wid = handle.get(B.X11) if isinstance(handle, dict) else None
        app, window_id = msg.get(B.APP), msg.get(B.WINDOW)
        d = self.service.display
        if not protocol.valid_wid(wid):
            log.warning("an app announced %r: not an X window; ignored", wid)
            return
        if d is not None and (wid == d.root or d._ours(wid)):
            log.warning("an app announced 0x%x, which is not its own window; ignored", wid)
            return
        if not isinstance(app, str) or not app or not B.is_id(window_id):
            log.warning("an app announced window 0x%x as no app (%r) or with no id of its "
                        "own (%r); ignored", wid, app, window_id)
            return
        self.window = AppWindow(self, wid, msg)
        log.info("%s window %s (0x%x) connected", app, window_id, wid)
        self.service.added(self.window)
        self.send(protocol.message(protocol.ATTACHED, voices=voice_names()))

    @property
    def pid(self) -> int | None:
        """The app's process, as the socket says (SO_PEERCRED)."""
        sock = self.writer.get_extra_info("socket")
        try:
            return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                       struct.calcsize("3i")))[0]
        except (AttributeError, OSError):
            return None

    def send(self, msg: dict) -> None:
        if not self.closed and not self.writer.is_closing():
            self.writer.write(protocol.encode(msg))

    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        window, self.window = self.window, None
        if window is not None:
            try:
                window.close()
            except Exception:
                log.exception("closing app window 0x%x failed", window.wid)
            log.info("%s window 0x%x gone: %s", window.kind or "app", window.wid, reason)
        if not self.writer.is_closing():
            self.writer.close()
        self.service.forget(self, window)


UNSET = 0xFFFFFFFF                  # _NET_WM_DESKTOP of a window on every workspace


def pick_desktop(count: int, current: int | None, viewports: list[int],
                 clients: list[tuple[int | None, bool]],
                 taken: set[int] = frozenset()) -> tuple[int | None, str]:
    """The workspace for a window that must not appear where the user
    looks (the module docstring, "Placing"): (index, how it was chosen),
    or (None, why none). `clients`: each client window's workspace and
    whether it is mapped; `taken`: workspaces of windows placed here."""
    if not count:
        return None, "the window manager names no workspaces"
    shown = {current} if current is not None else set()
    for d in range(count):
        xy = tuple(viewports[2 * d:2 * d + 2])
        if len(xy) == 2 and xy != (0, 0):
            shown.add(d)
    used = {d for d in taken if 0 <= d < count}
    for desktop, mapped in clients:
        if desktop is None or not 0 <= desktop < count:
            continue                    # on every workspace, or on none
        used.add(desktop)
        if mapped:
            shown.add(desktop)
    hidden = [d for d in range(count) if d not in shown]
    if not hidden:
        return None, "every workspace is shown"
    empty = [d for d in hidden if d not in used]
    if empty:
        return max(empty), "the highest-numbered empty workspace no screen shows"
    return max(hidden), "no workspace is empty: the highest-numbered one no screen shows"


@dataclass
class Grabs:
    """The keys grabbed on one app window."""

    window: AppWindow
    chords: dict[tuple[int, int], str]      # (keycode, modifiers) -> chord
    keys: list[tuple[int, int]]             # what X holds: (keycode, modifiers with locks)


class AppService:
    """A SERVICES entry: the socket indigoshell apps announce their
    windows on, which of those windows has focus, and the keys the shell
    takes from them.

    `path` and `display` are for tests, which run it on a socket and an
    X connection of their own; the daemon's are used otherwise.
    """

    def __init__(self, *, path: str | None = None, display=None) -> None:
        self._path = path
        self._own_display = display
        self.display = display
        self.links: list[Link] = []
        self.windows: list[AppWindow] = []          # announced, oldest first
        self.active = 0                 # _NET_ACTIVE_WINDOW as last read
        self.focused: AppWindow | None = None
        self.listening = False
        self.running = False
        # Who hears focus move: callables taking the focused AppWindow or
        # None. The top bar's context area, chiefly.
        self._followers: list = []
        # What shows the focused window: the context area tells the
        # service where it is, for things placed against the top bar
        # (Flux's launcher). See `bar_bottom`.
        self.context = None
        self.grabs: dict[int, Grabs] = {}           # by app X window
        self._refused: set[tuple[str, str]] = set()     # (kind, chord) said once
        self._active_atom: int | None = None
        self._transient: dict[int, int] = {}        # active wid -> what it is transient for
        self._open_wait: tuple | None = None        # (window, what, name, timer): `open`
        self._sock: socket.socket | None = None
        self._ino: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._task: asyncio.Task | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._warned = False
        # The input or menu open (prompt.py): at most one, the shell's.
        self.prompt: Prompt | None = None
        self._pointer_on: int | None = None         # the bar window the pointer is held on

    @property
    def path(self) -> str:
        return self._path or protocol.socket_path()

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._own_display is None:
            from ..core.daemon import get_daemon
            self.display = get_daemon().display
        self.running = True
        self._warned = False
        if self.display is not None:
            self.display.add_keymap_listener(self._keymap_changed)
        self._watch_focus()
        self._try_claim()
        if self._own_display is None:
            # The drop-down made and parked ahead of the first input opened.
            asyncio.get_running_loop().call_later(PREPARE_S, self._prepare)

    def _prepare(self) -> None:
        if not self.running:
            return
        try:
            from ..core.daemon import get_daemon
            get_daemon().prepare(DROP_WINDOW)
        except Exception:
            log.exception("making the drop-down ahead failed")

    def stop(self) -> None:
        self.running = False
        if self.prompt is not None:
            self.prompt.close("shell")
        if self._restarting():
            # Back in a moment: the apps may wait for it a little longer.
            for link in self.links:
                link.send(protocol.message(protocol.RESTARTING))
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self.listening:
            self.listening = False
            self._unlink_ours()
        for link in list(self.links):
            link.close("shell stopping")
        if self.display is not None:
            self.display.remove_keymap_listener(self._keymap_changed)
            self.display.flush()

    @staticmethod
    def _restarting() -> bool:
        """Whether the daemon is stopping to re-execute itself."""
        from ..core.daemon import get_daemon
        try:
            return bool(getattr(get_daemon(), "restarting", False))
        except RuntimeError:            # no daemon: a test's own service
            return False

    def _try_claim(self) -> None:
        self._retry = None
        try:
            sock = self._claim()
        except OSError:
            log.exception("apps: cannot listen on %s", self.path)
            return
        if sock is None:
            if not self._warned:
                self._warned = True
                log.warning("apps: another shell is listening on %s; leaving it be, "
                            "and taking over once it stops", self.path)
            self._retry = asyncio.get_running_loop().call_later(
                CLAIM_RETRY_S, self._try_claim)
            return
        self._sock = sock
        self._ino = os.stat(self.path).st_ino
        self.listening = True
        self._task = asyncio.get_running_loop().create_task(self._serve(sock))
        log.info("apps: listening on %s", self.path)

    def _claim(self) -> socket.socket | None:
        """The socket, bound and listening; None while another listener
        holds it. A socket file nobody answers on is a stale one."""
        path = self.path
        if os.path.lexists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(path)
            except FileNotFoundError:
                pass
            except OSError:
                os.unlink(path)         # stale: its owner is gone
            else:
                return None
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)           # the socket is ours alone
        try:
            sock.bind(path)
        except OSError as e:
            sock.close()
            if e.errno == 98:           # EADDRINUSE: someone was quicker
                return None
            raise
        finally:
            os.umask(old)
        sock.listen(16)
        sock.setblocking(False)
        return sock

    def _unlink_ours(self) -> None:
        """Remove the socket file, unless it is not ours any more."""
        try:
            if os.stat(self.path).st_ino == self._ino:
                os.unlink(self.path)
        except OSError:
            pass

    async def _serve(self, sock: socket.socket) -> None:
        self._server = await asyncio.start_unix_server(
            self._client, sock=sock, limit=LINE_LIMIT)
        self._sock = None               # the server owns it now

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        link = Link(self, writer)
        self.links.append(link)
        try:
            while line := await reader.readline():
                msg = protocol.decode(line)
                if msg is None:
                    log.debug("apps: a line that is no message, ignored")
                    continue
                try:
                    link.on_message(msg)
                except Exception:
                    log.exception("apps: message %r failed", msg.get(protocol.TYPE))
                if self.display is not None:
                    self.display.flush()
        except (ConnectionError, OSError, ValueError):
            pass                        # a reset, or a line past LINE_LIMIT
        finally:
            link.close("app hung up")

    # ── app windows ─────────────────────────────────────────────────────
    def added(self, window: AppWindow) -> None:
        self.windows.append(window)
        self.grab_keys(window)
        self._refocus()

    def forget(self, link: Link, window: AppWindow | None) -> None:
        if link in self.links:
            self.links.remove(link)
        if window is not None:
            if self.prompt is not None and self.prompt.owner is window:
                self.prompt.close("focus")
            if self._open_wait is not None and self._open_wait[0] is window:
                if self._open_wait[3] is not None:
                    self._open_wait[3].cancel()
                self._open_wait = None
            self.release_keys(window)
            if window in self.windows:
                self.windows.remove(window)
                self._refocus()

    def window_for(self, wid: int) -> AppWindow | None:
        """The app window announced as X window `wid`; the newest when
        two said the same (the older one's connection is on its way
        out)."""
        return next((w for w in reversed(self.windows) if w.wid == wid), None)

    # ── placing ─────────────────────────────────────────────────────────
    def place(self, window: AppWindow, msg: dict) -> None:
        """An app window not yet shown asks for a workspace (protocol
        `place`): its `_NET_WM_DESKTOP` set and read back, then `placed`."""
        d = self.display
        if d is None:
            window.send(protocol.placed(None, "no X display"))
            return
        near = self._sibling(window, msg.get(protocol.WITH))
        try:
            desktop, how = self._placement(window, near)
            if desktop is not None:
                d.set_prop(window.wid, "_NET_WM_DESKTOP", xp.Atom.CARDINAL, [desktop])
                # The round trip: once this answers, the server has the
                # property, before the app (told next) can map.
                if d.get_int(window.wid, "_NET_WM_DESKTOP") != desktop:
                    desktop, how = None, "the window is gone"
        except Exception as e:          # noqa: BLE001  (a window gone meanwhile, mostly)
            log.debug("placing 0x%x failed", window.wid, exc_info=True)
            desktop, how = None, f"placing failed ({type(e).__name__})"
        finally:
            d.wake()
        if desktop is not None:
            window.placed = desktop
            log.info("%s window 0x%x placed on workspace %d (%s)", window.kind or "app",
                     window.wid, desktop, how)
        else:
            log.info("%s window 0x%x not placed: %s", window.kind or "app", window.wid, how)
        window.send(protocol.placed(desktop, None if desktop is not None else how))

    def _sibling(self, window: AppWindow, app_id) -> int | None:
        """The X window of another window of the same app (the same
        process) that the app names by its own id; None for none."""
        if app_id is None or not B.is_id(app_id):
            return None
        pid = window.link.pid
        return next((w.wid for w in reversed(self.windows)
                     if w is not window and w.kind == window.kind and w.app_id == app_id
                     and w.link.pid == pid), None)

    def _placement(self, window: AppWindow, near: int | None) -> tuple[int | None, str]:
        d = self.display
        core = d.conn.core
        if core.GetWindowAttributes(window.wid).reply().map_state != xp.MapState.Unmapped:
            return None, "the window is shown already"
        if near is not None:
            desktop = d.get_int(near, "_NET_WM_DESKTOP")
            if desktop is None or desktop == UNSET:
                return None, f"window 0x{near:x} is on no one workspace"
            return desktop, f"where window 0x{near:x} is"
        count = d.get_int(d.root, "_NET_NUMBER_OF_DESKTOPS") or 0
        current = d.get_int(d.root, "_NET_CURRENT_DESKTOP")
        viewports = d.get_ints(d.root, "_NET_DESKTOP_VIEWPORT", 2 * max(1, count))
        atom = d.atom("_NET_WM_DESKTOP")
        asked = [(core.GetProperty(False, c, atom, xp.Atom.CARDINAL, 0, 1),
                  core.GetWindowAttributes(c))
                 for c in d.get_ints(d.root, "_NET_CLIENT_LIST", 4096)]
        clients = []
        for prop, attrs in asked:
            try:
                p, a = prop.reply(), attrs.reply()
            except Exception:           # noqa: BLE001  (gone since the list was read)
                continue
            raw = bytes(p.value.buf())
            desktop = struct.unpack("<I", raw[:4])[0] if p.format == 32 and len(raw) >= 4 \
                else None
            clients.append((desktop, a.map_state == xp.MapState.Viewable))
        taken = {w.placed for w in self.windows if w is not window and w.placed is not None}
        return pick_desktop(count, current, viewports, clients, taken)

    # ── keys ────────────────────────────────────────────────────────────
    def grab_keys(self, window: AppWindow) -> None:
        """Grab `window.keys` on its X window, in place of what was
        grabbed there before (by it, or by an older connection that
        announced the same window)."""
        d = self.display
        if d is None or window.closed:
            return
        old = self.grabs.pop(window.wid, None)
        if old is not None:
            d.ungrab_keys(window.wid, old.keys)
        if not window.keys:
            if old is not None:
                d.off(window.wid, self._on_app_event)
            return
        locks = d.lock_combinations()
        chords: dict[tuple[int, int], str] = {}
        held: list[tuple[int, int]] = []
        for chord in window.keys:
            parsed = self._parse(chord)
            codes = d.keycodes(parsed[0]) if parsed else []
            if not codes:
                self._refuse(window.kind, chord, "no such key in this keymap" if parsed
                             else "not a chord")
                continue
            keysym, mods = parsed
            keys = [(code, mods | lock) for code in codes for lock in locks]
            why = d.grab_keys(window.wid, keys)
            if why == "gone":
                log.debug("app window 0x%x gone before its keys were grabbed", window.wid)
                break
            if why is not None:
                self._refuse(window.kind, chord, "another client has it on that window"
                             if why == "taken" else why)
                continue
            chords.update({(code, mods): chord for code in codes})
            held += keys
        if held:
            self.grabs[window.wid] = Grabs(window, chords, held)
            d.on(window.wid, self._on_app_event)
            log.debug("%s window 0x%x: %d key grab(s) for %s", window.kind, window.wid,
                      len(held), ", ".join(sorted(set(chords.values()))))
        else:
            d.off(window.wid, self._on_app_event)

    def release_keys(self, window: AppWindow) -> None:
        """Let go of the keys grabbed for `window` (not of a newer window
        announced on the same X window since)."""
        g = self.grabs.get(window.wid)
        d = self.display
        if g is None or g.window is not window or d is None:
            return
        del self.grabs[window.wid]
        d.ungrab_keys(window.wid, g.keys)
        d.off(window.wid, self._on_app_event)

    def _parse(self, chord: str) -> tuple[int, int] | None:
        """(keysym, modifiers) of a chord like "ctrl+l"; None if it is
        not one this can grab."""
        parsed = B.parse_chord(chord, grab=True)
        if parsed is None:
            return None
        mods, key = parsed
        mask = 0
        for m in mods:
            bit = _CHORD_MODIFIERS.get(m) or (self.display.mod_mask(m)
                                              if m in ("alt", "super") else 0)
            if not bit:
                return None
            mask |= bit
        return ord(key), mask

    def _refuse(self, kind: str, chord: str, why: str) -> None:
        if (kind, chord) in self._refused:
            log.debug("apps: %s's %s not grabbed: %s", kind, chord, why)
            return
        self._refused.add((kind, chord))
        log.warning("apps: %s's %s not grabbed (%s); that key is left to the app",
                    kind, chord, why)

    def _keymap_changed(self) -> None:
        if not self.running:
            return
        for g in list(self.grabs.values()):
            self.grab_keys(g.window)

    def _on_app_event(self, ev) -> None:
        """An event on an app window the service grabbed keys on: a key
        pressed while the shell holds the keyboard for it. Releases, and
        whatever else someone here selected on that window, are not
        ours."""
        if (ev.response_type & 0x7F) != _KEY_PRESS:
            return
        g = self.grabs.get(ev.event)
        if g is None or g.window.closed:
            return
        d = self.display
        locks = xp.ModMask.Lock | d.mod_mask("numlock")
        name = g.chords.get((ev.detail, ev.state & 0xFF & ~locks))
        key = key_event(d, ev)
        try:
            if name is not None:
                log.debug("%s window 0x%x: %s", g.window.kind, g.window.wid, name)
                g.window.on_key(name, key)
            else:
                g.window.on_typed(key)
        except Exception:
            log.exception("apps: a key for %s window 0x%x failed", g.window.kind, g.window.wid)
        d.flush()

    def hold_keyboard(self, window: AppWindow) -> bool:
        """Keep the keyboard the X server handed over with one of
        `window`'s keys past that key's release (which ends a passive
        grab), its keys still reported on the app's window: until
        whatever answers the key takes the keyboard with a grab of its
        own (this connection has one keyboard grab; a new one replaces
        it), or `release_keyboard`."""
        d = self.display
        return d is not None and d.grab_keyboard(window.wid)

    def release_keyboard(self) -> None:
        """Give the keyboard back: keys go to the focused window again."""
        if self.display is not None:
            self.display.ungrab_keyboard()

    # ── focus ───────────────────────────────────────────────────────────
    def follow(self, callback) -> None:
        """Call `callback(window)` whenever the focused app window changes
        (None: the window with focus is no app's)."""
        if callback not in self._followers:
            self._followers.append(callback)

    def unfollow(self, callback) -> None:
        if callback in self._followers:
            self._followers.remove(callback)

    def _watch_focus(self) -> None:
        d = self.display
        if d is None or self._active_atom is not None:
            return
        self._active_atom = d.atom("_NET_ACTIVE_WINDOW")
        d.add_root_listener(self._on_root_property)
        self.read_active()

    def _on_root_property(self, ev) -> None:
        # The root listener stays after stop() (the display has no way to
        # remove one); a stopped service only stops listening.
        if self.running and getattr(ev, "atom", None) == self._active_atom:
            self.read_active()

    def read_active(self) -> None:
        """_NET_ACTIVE_WINDOW as the root has it now."""
        d = self.display
        self.set_active(d.get_int(d.root, "_NET_ACTIVE_WINDOW") or 0)

    def set_active(self, wid: int) -> None:
        """The window manager says `wid` has focus now (0: none)."""
        if wid != self.active:
            self.active = wid
            self._refocus()

    def _refocus(self) -> None:
        window = self._app_of(self.active) if self.active else None
        if window is self.focused:
            return
        self.focused = window
        p = self.prompt
        if p is not None and p.owner is not None and p.owner is not window:
            p.close("focus")
        if self._open_wait is not None and self._open_wait[0] is window:
            self._try_open()
        log.debug("apps: focus on %s", f"{window.kind} 0x{window.wid:x}" if window else
                  f"0x{self.active:x}, no app's")
        for callback in list(self._followers):
            try:
                callback(window)
            except Exception:
                log.exception("apps: a focus follower failed")

    def _app_of(self, wid: int) -> AppWindow | None:
        window = self.window_for(wid)
        if window is not None or self.display is None:
            return window
        # A dialog of an app's (Flux's address prompt, a permission
        # question) is the app's too: one round trip, once per window.
        parent = self._transient.get(wid)
        if parent is None:
            parent = self.display.get_int(wid, "WM_TRANSIENT_FOR") or 0
            if len(self._transient) > 64:
                self._transient.clear()
            self._transient[wid] = parent
        return self.window_for(parent) if parent else None

    # ── the top bar ─────────────────────────────────────────────────────
    def bar_bottom(self, x: int) -> int | None:
        """The y (root coordinates) of the top bar's bottom edge, when the
        bar showing the context area spans root x `x`; None when there is
        no such bar, or it lies elsewhere (another monitor)."""
        context = self.context
        window = getattr(context, "window", None)
        root = getattr(window, "content_root", None)
        if root is None or getattr(window, "wid", None) is None:
            return None
        cx, cy, cw, ch = root
        return cy + ch if cx <= x < cx + cw else None

    def section(self) -> tuple[int, int, int] | None:
        """The app section of the top bar: its left edge and the bar's
        bottom edge (root coordinates), and its width; None with no top
        bar made."""
        context = self.context
        window = getattr(context, "window", None)
        root = getattr(window, "content_root", None)
        if root is None or getattr(window, "wid", None) is None:
            return None
        r = context.rect
        return int(window.x + r.left()), root[1] + root[3], int(r.width())

    # ── the open input or menu ──────────────────────────────────────────
    def prompt_of(self, window: AppWindow):
        """The input or menu open for `window`, or None."""
        p = self.prompt
        return p if p is not None and p.open and p.owner is window else None

    def open_prompt(self, window: AppWindow, what: str, name, *, pressed: bool,
                    chord: str | None = None, anchor=None) -> bool:
        """Open `window`'s input or menu `name` (what: "input" or "menu"):
        its box in the bar, its list under it, the keyboard the shell's
        while it is open. `pressed`: for its chord, the keyboard the
        shell's since the press. False, and `fallback` to the app, when
        it cannot show (no top bar over the window, no keyboard)."""
        held = False
        if pressed:
            # First: a passive grab ends with the chord's key, and every
            # key typed after it until the prompt is up would go to the
            # window.
            held = self.hold_keyboard(window)
        if self.prompt is not None:
            self.prompt.close("shell")
        spec = ((window.bar or {}).get(B.INPUTS if what == "input" else B.MENUS) or {}).get(name)
        if not isinstance(spec, dict):
            log.info("%s window 0x%x: no %s %r in its bar; the app does without",
                     window.kind or "app", window.wid, what, name)
            if pressed:
                self.release_keyboard()
            window.send_event(B.event(B.FALLBACK, **{what: name}))
            return False
        # From here on every way out must give the keyboard back unless the
        # prompt holds it: an exception left unhandled here would leave the
        # whole desktop's keyboard with the shell.
        why = self._cannot_show(window)
        if why is None and not pressed:
            held = self.hold_keyboard(window)
        if why is None and not held and self.display is not None:
            why = "the keyboard is not to be had"
        prompt = None
        if why is None:
            try:
                prompt = Prompt(self, window, what, name, spec, chord=chord, anchor=anchor)
            except Exception:
                log.exception("the %s %r failed to open for %s window 0x%x", what, name,
                              window.kind or "app", window.wid)
                why = "it failed to open"
        if prompt is None:
            log.info("%s window 0x%x: its %s %r cannot show (%s); the app does without",
                     window.kind or "app", window.wid, what, name, why)
            if pressed or held:
                self.release_keyboard()
            window.send_event(B.event(B.FALLBACK, **{what: name}))
            return False
        self.prompt = prompt
        self._dismiss_panels()
        if prompt.box is not None:
            window.view.show_box(prompt.box)
        if anchor is not None:
            window.view.set_lit({anchor.id})
        self._hold_pointer()
        prompt.start()
        self.changed(prompt)
        return True

    def open_asked(self, window: AppWindow, what: str, name) -> None:
        """An app asks for one of its inputs or menus open with no key
        pressed (`open`): honoured once the window has focus and its bar
        has that input or menu, waiting for both up to OPEN_WAIT_S (a new
        window asks as soon as it is shown: the window manager may focus
        it a moment later, and its first bar may come after the ask);
        else `fallback`."""
        old = self._open_wait
        if old is not None:
            if old[3] is not None:
                old[3].cancel()
            if old[0] is not window:
                log.info("%s window 0x%x: its %s %r gave way to another window's; the app "
                         "does without", old[0].kind, old[0].wid, old[1], old[2])
                old[0].send_event(B.event(B.FALLBACK, **{old[1]: old[2]}))
        self._open_wait = (window, what, name, None)
        if self._try_open():
            return
        log.debug("%s window 0x%x asks for its %s %r: waiting for %s", window.kind, window.wid,
                  what, name, self._open_missing(window, what, name))
        self._open_wait = (window, what, name, self.later(B.OPEN_WAIT_S, self._open_late))

    def _open_missing(self, window: AppWindow, what: str, name) -> str | None:
        """What an asked-for open still waits for; None when nothing."""
        if self.focused is not window:
            return "its window's focus"
        if not isinstance(((window.bar or {}).get(B.INPUTS if what == "input" else B.MENUS)
                           or {}).get(name), dict):
            return f"that {what} in its bar"
        return None

    def _try_open(self) -> bool:
        """The open asked for, once nothing is missing (focus moved, or a
        bar came). True when it went ahead (shown, or refused and said)."""
        wait = self._open_wait
        if wait is None:
            return False
        window, what, name, timer = wait
        if window.closed or self._open_missing(window, what, name) is not None:
            return False
        self._open_wait = None
        if timer is not None:
            timer.cancel()
        self.open_prompt(window, what, name, pressed=False)
        return True

    def _open_late(self) -> None:
        wait, self._open_wait = self._open_wait, None
        if wait is None:
            return
        window, what, name, _timer = wait
        log.info("%s window 0x%x: its %s %r not opened: still waiting for %s after %.1f s; "
                 "the app does without", window.kind, window.wid, what, name,
                 self._open_missing(window, what, name) or "nothing", B.OPEN_WAIT_S)
        window.send_event(B.event(B.FALLBACK, **{what: name}))

    def _cannot_show(self, window: AppWindow) -> str | None:
        """Why `window`'s input or menu cannot show in the top bar; None
        when it can."""
        if self.section() is None:
            return "no top bar"
        d = self.display
        if d is None:
            return None
        bx, _by, bw, _bh = self.context.window.content_root
        if bx <= 0 and bx + bw >= d.geometry()[0]:
            return None                 # the bar spans the whole screen: over every window
        try:
            at = d.conn.core.TranslateCoordinates(window.wid, d.root, 0, 0).reply()
            width = d.conn.core.GetGeometry(window.wid).reply().width
        except (xp.WindowError, xp.DrawableError):
            return "the window is gone"
        finally:
            d.wake()
        if self.bar_bottom(at.dst_x + width // 2) is None:
            return "the top bar is on another monitor"
        return None

    def changed(self, prompt) -> None:
        """The prompt's list changed: the drop-down follows."""
        if prompt is not self.prompt or not prompt.open:
            return
        try:
            if prompt.wants_list():
                self._drop_show(prompt)
            else:
                self._drop_hide()
        except Exception:
            log.exception("the drop-down failed")

    def closed(self, prompt) -> None:
        """The prompt left the screen: everything given back."""
        if prompt is not self.prompt:
            return
        self.prompt = None
        try:
            self._drop_hide()
        except Exception:
            log.exception("hiding the drop-down failed")
        self._release_pointer()
        self.release_keyboard()
        owner = prompt.owner
        if owner is not None:
            owner.view.show_box(None)
            owner.view.set_lit(())
            owner.prompt_closed(prompt)

    def next_seq(self, prompt) -> int:
        return prompt.owner.next_seq() if prompt.owner is not None else 0

    def later(self, delay: float, callback):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.call_later(delay, callback)

    # ── the drop-down window ────────────────────────────────────────────
    def _drop_place(self, prompt) -> tuple[int, int, int]:
        """Where the list drops: (left, top, width) in root coordinates."""
        left, top, width = self.section() or (0, 0, 400)
        anchor = prompt.anchor
        window = getattr(self.context, "window", None)
        if anchor is not None and window is not None and not anchor.rect.isEmpty():
            left = int(window.x + anchor.rect.left())
        return left, top, width

    def _drop_show(self, prompt) -> None:
        show = getattr(self, "show_drop", None)     # tests put a recorder there
        if show is not None:
            show(prompt, self._drop_place(prompt))
            return
        from ..core.daemon import get_daemon
        daemon = get_daemon()
        spec = daemon.kinds.get(DROP_WINDOW)
        drop = getattr(spec, "content", None)
        if drop is None:
            return
        left, top, width = self._drop_place(prompt)
        drop.show(prompt, width)
        w, h = drop.relayout()
        win = daemon.instances.get(DROP_WINDOW)
        if win is None:
            daemon.open(DROP_WINDOW, placement=Placement(left + w // 2, top))
            return
        win.placement = Placement(left + w // 2, top)
        win.resize_content(w, h)
        drop.invalidate()

    def _drop_hide(self) -> None:
        hide = getattr(self, "hide_drop", None)
        if hide is not None:
            hide()
            return
        try:
            from ..core.daemon import get_daemon
            daemon = get_daemon()
        except RuntimeError:            # no daemon: a test's own service
            return
        if DROP_WINDOW in daemon.instances:
            daemon.close(DROP_WINDOW)

    def _dismiss_panels(self) -> None:
        """Open panels close, as for a click elsewhere: the prompt's
        pointer grab takes their outside clicks from them."""
        try:
            from ..core.daemon import get_daemon
            get_daemon().dismiss_others(DROP_WINDOW)
        except RuntimeError:
            pass

    # ── the pointer, while a prompt is open ─────────────────────────────
    def _hold_pointer(self) -> None:
        """A press outside the bar and its list closes the prompt: the
        pointer held on the bar's window (owner_events: the bar and the
        list still get theirs), presses anywhere else reported there."""
        d, window = self.display, getattr(self.context, "window", None)
        wid = getattr(window, "wid", None)
        if d is None or wid is None or self._pointer_on is not None:
            return
        if d.grab_pointer(wid):
            self._pointer_on = wid
            d.on(wid, self._on_bar_event)
        else:
            log.debug("the pointer is not to be had: a click elsewhere will not close it")

    def _release_pointer(self) -> None:
        wid, self._pointer_on = self._pointer_on, None
        if wid is None or self.display is None:
            return
        self.display.off(wid, self._on_bar_event)
        self.display.ungrab_pointer()

    def _on_bar_event(self, ev) -> None:
        if (ev.response_type & 0x7F) != _BUTTON_PRESS or self.prompt is None:
            return
        window = getattr(self.context, "window", None)
        if window is None:
            return
        if not (0 <= ev.event_x < window.width and 0 <= ev.event_y < window.height):
            log.debug("a click outside the bar and its list: the prompt closes")
            self.prompt.close("click")
