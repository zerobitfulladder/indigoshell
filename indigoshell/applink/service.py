"""The daemon's end of the apps' line: who is connected, who has focus,
the bars they send, and the inputs and menus they open in the top bar.

`AppService` is a SERVICES entry. It listens at `protocol.socket_path()`
on the daemon's loop; each connection is one app window (`Link`), which
announces itself first (the app, its own id for the window: PROTOCOL.md)
and is then an `AppWindow`, the same class for every app: nothing of any
app's is here. The connection is the window: the app's process is the
socket's peer (SO_PEERCRED), and the app says whether the window has the
keyboard focus (`active`). The window's `bar` is checked (bar.py
`bar_problems`: a broken one is ignored whole, the last good one kept, a
warning once per problem) and shown in its `BarView` (applink/view.py),
which the top bar's context area shows while the window has focus
(applink/context.py); the user's doings go back to the app as the bar's
events. The images its tabs name (a page's icon) come as `image`
messages, decoded once and held for the connection while a bar names
them (PROTOCOL.md, "Images"): each good bar lets go of those it does not
name.

Focus. The service follows the compositor's focused window
(indigoshell.wm's `follow_windows`: niri's event stream), which names
its client's process: the focused app window (`focused`) is the one of
that process's connections that said it is `active` last, or its only
one when it never said. A focused window of no app's process is no
app's. While the canvas has the keyboard (and for HOLD_GRACE_S after it
lets go, while the compositor focuses the window again) niri names no
focused window and the app says its window is not active: neither moves
the focus. With no window manager to follow, the apps' own `active`
decides. Anyone that wants to hear about it (the top bar's context
area, applink/context.py) calls `follow`.

Inputs and menus. The app catches the chords of its bar's inputs and
menus (bar.py `chords`: Flux's Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M) itself
and sends `open` with the chord, which opens that input or menu
(`open_prompt`): the canvas's keyboard taken at once (`exclusive`), its
box in the window's section, its list dropping under the bar
(applink/prompt.py, drop.py). Until the compositor has moved the
keyboard the app forwards what its window gets (`keys`), typed into the
open input in order; keys the canvas gets meanwhile wait for the app's
`last` (at most bar.FORWARD_WAIT_S), so nothing is lost or reordered
between the lines. While it is open the canvas catches clicks outside
the bar and its list, which close it; so does the window losing focus or
going. An app may ask for one with no key pressed (`open` without a
chord): honoured once its window has focus and its bar has it (within
bar.OPEN_WAIT_S). With no top bar, the app is told `fallback`.

Placing. A window exists for the compositor only once it maps, so an
app asks for a workspace for a window it is about to show (protocol
`place`): Flux's window for the agent, which works on a workspace of its
own, never the one the user is looking at. `place` is answered at once
with the workspace chosen (wm `spare_workspace`: on the focused monitor,
the highest-numbered empty one of the bar's workspaces that no monitor
shows), the app maps the window, and the first new window of the app's
process the compositor lists after the `place` (within PLACE_FOLLOW_S)
is moved there, not focused; its compositor id is kept, so a `place`
naming it `with` (the app's own id for it, a window of the same
process) goes to its workspace: a popup of a placed window goes where
it is.

Lifecycle. A connection that closes (the app window closed, or the app
quit) drops its `AppWindow`. `stop()` (the daemon stopping or
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
import itertools
import logging
import os
import socket
import struct
import time

from ..core import persona
from ..widgets.base import KeyEvent
from ..window import Placement
from . import bar as B
from . import protocol
from .drop import DROP_WINDOW
from .kinds import glow
from .prompt import Prompt
from .view import BarView

log = logging.getLogger(__name__)

CLAIM_RETRY_S = 5.0                 # while another shell holds the socket
PREPARE_S = 2.0                     # after the start: the drop-down made ahead
LINE_LIMIT = 1 << 20                # bytes in one message: a state with many tabs
HOLD_GRACE_S = 0.5                  # after the canvas lets the keyboard go (the focus)
PLACE_FOLLOW_S = 5.0                # a placed window that maps later is not looked for


class AppWindow:
    """One window of an indigoshell app, as the shell sees it.

    `view` is its section of the top bar (applink/view.py), which the
    context area shows while this window has focus (`content`; empty
    until the window's bar comes). `bar` is the last good bar shown.
    `images` are the app's images it holds (id -> skia image), which the
    view's tabs draw. The window is known by its connection: the app's
    process (`link.pid`) and its own word on its focus (`active_seq`).
    """

    _actives = itertools.count(1)

    def __init__(self, link: "Link", announce: dict) -> None:
        self.link = link
        self.service = link.service
        self.kind = str(announce.get(B.APP) or "app")   # the app's name, for log lines
        self.app_id = announce.get(B.WINDOW)            # the app's own id for the window
        self._said: set[str] = set()                    # problems warned of, once each
        self.images: dict = {}
        self.view = BarView(emit=self.send_event, on_open=self._item_opens, images=self.images)
        self.content = self.view
        self.bar: dict | None = None
        self.closed = False
        self._seq = 0
        # When it last said it is `active` (rising; 0 since it said it is
        # not; None: it never said), and the compositor's id for it once
        # known (placing).
        self.active_seq: int | None = None
        self.wm_id: int | None = None

    @property
    def label(self) -> str:
        """The window in log lines: its app id and process."""
        return f"{self.app_id!r} (pid {self.link.pid})"

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
                log.debug("%s window %s: answer seq %s for %r dropped: %s", self.kind,
                          self.label, msg.get("seq"), msg.get("input"), "; ".join(problems))
                self._warn("an answer ignored", problems)
                return
            p = self.service.prompt_of(self)
            if p is not None and p.what == "input" and p.name == msg["input"]:
                p.answer(msg)
            else:
                log.debug("%s window %s: answer seq %s for %r dropped: that input is not "
                          "open", self.kind, self.label, msg.get("seq"), msg["input"])
        elif kind == B.OPEN:
            what = "input" if "input" in msg else "menu"
            chord = msg.get(B.CHORD)
            self.service.open_asked(self, what, msg.get(what),
                                    chord=chord if isinstance(chord, str) else None)
        elif kind == B.IMAGE:
            self.on_image(msg)
        elif kind == B.ACTIVE:
            self.set_active(msg.get("active") is True)
        elif kind == B.KEYS:
            self.on_keys(msg)
        else:
            log.debug("%s window %s: message %r not understood", self.kind, self.label, kind)

    def set_active(self, active: bool) -> None:
        """The app says whether its window has the keyboard focus
        (PROTOCOL.md, "Focus")."""
        if active:
            self.active_seq = next(AppWindow._actives)
        elif self.service.holds_focus(self):
            # The canvas took the keyboard from it: no change of focus.
            log.debug("%s window %s: not active while the canvas has the keyboard; still "
                      "focused", self.kind, self.label)
            return
        else:
            self.active_seq = 0
        self.service.activity()

    def on_keys(self, msg: dict) -> None:
        """Keys the app's window got after a chord it caught, before the
        canvas had the keyboard: into the open input, in order."""
        problems = B.keys_problems(msg)
        if problems:
            self._warn("keys ignored", problems)
        else:
            if msg["keys"]:
                log.debug("%s window %s: %d key(s) forwarded (typed before the canvas had "
                          "the keyboard)", self.kind, self.label, len(msg["keys"]))
            for k in msg["keys"]:
                mods = set(k.get("mods") or ())
                key = KeyEvent(keysym=k["keysym"], text=str(k.get("text") or ""),
                               shift="shift" in mods, ctrl="ctrl" in mods, alt="alt" in mods,
                               super="super" in mods)
                prompt = self.service.prompt_of(self)
                if prompt is None:
                    log.debug("%s window %s: a key forwarded with nothing open for it; "
                              "dropped", self.kind, self.label)
                    continue
                prompt.key(key)
        if msg.get(B.LAST) is True:
            self.service.forwarded(self)

    def on_image(self, msg: dict) -> None:
        """An image for the window's tabs to name: checked, decoded once,
        held until a bar goes without it (one sent again under its id
        replaces it). A tab of the bar showing it repaints."""
        problems = B.image_problems(msg)
        if not problems and msg["id"] not in self.images and len(self.images) >= B.IMAGES_MAX:
            problems = [f"{B.IMAGES_MAX} held already"]
        image = None if problems else glow.decode(B.image_png(msg))
        if image is None:
            self._warn("an image ignored", problems or ["its PNG does not decode"])
            return
        self.images[msg["id"]] = image
        if self.bar is not None and msg["id"] in B.image_ids(self.bar):
            self.view.invalidate()

    def _warn(self, what: str, problems: list[str]) -> None:
        new = [p for p in problems if p not in self._said]
        if not new:
            log.debug("%s window %s: %s again", self.kind, self.label, what)
            return
        self._said.update(new)
        log.warning("%s window %s: %s: %s", self.kind, self.label, what, "; ".join(new))

    # ── its bar ─────────────────────────────────────────────────────────
    def show_bar(self, bar: dict) -> None:
        """A good bar (bar.bar_problems found nothing): shown, an open
        input or menu of it kept up to date; the images it does not name
        let go."""
        self.bar = bar
        named = B.image_ids(bar)
        for image_id in [i for i in self.images if i not in named]:
            del self.images[image_id]
        self.view.set_bar(bar)
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
                log.debug("app window %s announced again on the same connection; ignored",
                          self.window.label)
            elif msg[protocol.TYPE] == protocol.PLACE:
                self.service.place(self.window, msg)
            else:
                self.window.on_message(msg)
            return
        if not announce:
            log.debug("message before the announce, ignored: %r", msg[protocol.TYPE])
            return
        app, window_id = msg.get(B.APP), msg.get(B.WINDOW)
        if not isinstance(app, str) or not app or not B.is_id(window_id):
            log.warning("an app announced a window as no app (%r) or with no id of its "
                        "own (%r); ignored", app, window_id)
            return
        self.window = AppWindow(self, msg)
        log.info("%s window %s connected (pid %s)", app, window_id, self.pid)
        self.service.added(self.window)
        self.send(protocol.message(protocol.ATTACHED, agent=persona.look()))

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
                log.exception("closing app window %s failed", window.label)
            log.info("%s window %s gone: %s", window.kind or "app", window.label, reason)
        if not self.writer.is_closing():
            self.writer.close()
        self.service.forget(self, window)


class AppService:
    """A SERVICES entry: the socket indigoshell apps announce their
    windows on, which of those windows has focus, and the inputs and
    menus they open in the top bar.

    `path` and `windows` are for tests, which run it on a socket of their
    own and with a stand-in for the window manager's WindowFollower
    (False for none: the apps' `active` alone decides); the daemon's
    socket and the window manager's are used otherwise.
    """

    current: "AppService | None" = None     # the running one, for the call's Look

    def __init__(self, *, path: str | None = None, windows=None) -> None:
        self._path = path
        self._own_windows = windows
        self.wm_windows = None          # the window manager's WindowFollower
        self.wm_focus = None            # ... and the window it says has the focus
        # The window whose app forwards keys typed after its chord until
        # its `last`; the canvas's own keys held meanwhile.
        self._forwarding: AppWindow | None = None
        self._held: list = []
        self._held_timer: asyncio.TimerHandle | None = None
        self._placing: dict = {}        # window -> (workspace, index, ids before, until)
        self._grace = None              # the focus looked at again once HOLD_GRACE_S is over
        self.links: list[Link] = []
        self.windows: list[AppWindow] = []          # announced, oldest first
        self.focused: AppWindow | None = None
        self.listening = False
        self.running = False
        # Who hears focus move: callables taking the focused AppWindow or
        # None. The top bar's context area, chiefly.
        self._followers: list = []
        # What shows the focused window: the context area tells the
        # service where it is, for the input's box and the list dropping
        # under the bar (`section`).
        self.context = None
        self._open_wait: tuple | None = None        # (window, what, name, timer, chord)
        self._sock: socket.socket | None = None
        self._ino: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._task: asyncio.Task | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._warned = False
        # The input or menu open (prompt.py): at most one, the shell's.
        self.prompt: Prompt | None = None

    @property
    def path(self) -> str:
        return self._path or protocol.socket_path()

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        self.running = True
        AppService.current = self
        self._warned = False
        self._watch_windows()
        self._try_claim()
        if self._own_windows is None:
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
        if AppService.current is self:
            AppService.current = None
        if self.prompt is not None:
            self.prompt.close("shell")
        if self.wm_windows is not None and self._own_windows is None:
            self.wm_windows.close()
        self.wm_windows = None
        self._placing.clear()
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
        except (ConnectionError, OSError, ValueError):
            pass                        # a reset, or a line past LINE_LIMIT
        finally:
            link.close("app hung up")

    # ── app windows ─────────────────────────────────────────────────────
    def added(self, window: AppWindow) -> None:
        self.windows.append(window)
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
            self._placing.pop(window, None)
            if self._forwarding is window:
                self.forwarded(window)
            if window in self.windows:
                self.windows.remove(window)
                self._refocus()

    # ── placing ─────────────────────────────────────────────────────────
    def _sibling(self, window: AppWindow, app_id) -> int | None:
        """The compositor's id of another window of the same app (the same
        process) that the app names by its own id; None for none (or not
        known yet)."""
        if app_id is None or not B.is_id(app_id):
            return None
        pid = window.link.pid
        return next((w.wm_id for w in reversed(self.windows)
                     if w is not window and w.kind == window.kind and w.app_id == app_id
                     and w.link.pid == pid), None)

    def place(self, window: AppWindow, msg: dict) -> None:
        """An app window about to show asks for a workspace (protocol
        `place`; the module docstring, "Placing"): answered at once with
        the workspace chosen; the window moved there once the compositor
        lists it."""
        f, pid = self.wm_windows, window.link.pid
        why = None
        if not f:
            why = "no window manager to place it with"
        elif not getattr(f, "ready", True):
            why = "the window manager's windows are not known yet"
        elif pid is None:
            why = "the app's process is unknown"
        ws = index = None
        if why is None:
            near = self._sibling(window, msg.get(protocol.WITH))
            if msg.get(protocol.WITH) is not None and near is not None:
                at = f.workspace_of(near)
                if at is not None:
                    (ws, index), how = at, f"where window {near} is"
                else:
                    why = f"window {near} is on no workspace"
            else:
                taken = {p[0] for p in self._placing.values()}
                ws, index, how = f.spare_workspace(taken)
                if ws is None:
                    why = how
        if why is not None:
            log.info("%s window %s not placed: %s", window.kind or "app", window.label, why)
            window.send(protocol.placed(None, why))
            return
        self._placing[window] = (ws, index, f.ids_of(pid), time.monotonic() + PLACE_FOLLOW_S)
        log.info("%s window %s goes to workspace %s (%s) once it maps", window.kind or "app",
                 window.label, index if index is not None else ws, how)
        window.send(protocol.placed(index if index is not None else ws))

    def _follow_placing(self) -> None:
        """Windows that `place` waits for: moved once the compositor lists
        them (the first new window of the app's process)."""
        f = self.wm_windows
        now = time.monotonic()
        for window, (ws, index, before, until) in list(self._placing.items()):
            if now > until or window.closed:
                del self._placing[window]
                log.info("%s window %s not placed: it did not map within %.0f s",
                         window.kind or "app", window.label, PLACE_FOLLOW_S)
                continue
            new = sorted(f.ids_of(window.link.pid) - before - {
                w.wm_id for w in self.windows if w.wm_id is not None})
            if not new:
                continue
            del self._placing[window]
            window.wm_id = new[0]
            f.move(new[0], ws)
            log.info("%s window %s is window %d of the compositor's: moved to workspace %s",
                     window.kind or "app", window.label, new[0],
                     index if index is not None else ws)

    # ── the keyboard, while a prompt is open ────────────────────────────
    def hold_keyboard(self) -> bool:
        """Take the canvas's keyboard (canvas.py KeyboardRouter) for the
        open prompt: its keys come to `key_press`, until
        `release_keyboard`. False when there is none to take."""
        scene = self._scene()
        if scene is None or not scene.keyboard.available:
            return False
        scene.keyboard.take(self, self)
        return True

    def release_keyboard(self) -> None:
        """Give the keyboard back: keys go to the focused window again."""
        scene = self._scene()
        if scene is not None:
            scene.keyboard.give_back(self)

    def key_press(self, key) -> None:
        """A key on the canvas while the keyboard is ours: for the open
        input or menu; held while the app still forwards keys its window
        got before (`forwarded`)."""
        p = self.prompt
        if p is None or not p.open:
            return
        if self._forwarding is not None:
            self._held.append(key)
            if self._held_timer is None:
                self._held_timer = self.later(B.FORWARD_WAIT_S, self._forward_late)
            return
        p.key(key)

    def forwarded(self, window: AppWindow) -> None:
        """The app's `last`: what its window got is all in; the canvas's
        own keys held meanwhile go in after it."""
        if self._forwarding is not window:
            return
        self._forwarding = None
        if self._held_timer is not None:
            self._held_timer.cancel()
            self._held_timer = None
        held, self._held = self._held, []
        for key in held:
            p = self.prompt
            if p is None or not p.open:
                break
            p.key(key)

    def _forward_late(self) -> None:
        self._held_timer = None
        window = self._forwarding
        if window is not None:
            log.debug("%s window %s: no `last` within %.2f s; the canvas's keys go in",
                      window.kind, window.label, B.FORWARD_WAIT_S)
            self.forwarded(window)

    def key_release(self, key) -> None:
        pass

    @staticmethod
    def _scene():
        """The canvas's scene, or None (no daemon: a test's own service)."""
        try:
            from ..core.daemon import get_daemon
            daemon = get_daemon()
        except RuntimeError:
            return None
        return getattr(getattr(daemon, "canvas", None), "scene", None)

    # ── focus ───────────────────────────────────────────────────────────
    def follow(self, callback) -> None:
        """Call `callback(window)` whenever the focused app window changes
        (None: the window with focus is no app's)."""
        if callback not in self._followers:
            self._followers.append(callback)

    def unfollow(self, callback) -> None:
        if callback in self._followers:
            self._followers.remove(callback)

    def _watch_windows(self) -> None:
        """The compositor's windows and focus, as the window manager says
        them (indigoshell.wm `follow_windows`)."""
        if self.wm_windows is not None:
            return
        if self._own_windows is not None:
            self.wm_windows = self._own_windows or None
            return
        try:
            from .. import wm
            self.wm_windows = wm.get().follow_windows(self._on_windows)
        except Exception:
            log.exception("apps: the window manager's windows cannot be followed")
            self.wm_windows = None
        if self.wm_windows is None:
            log.info("apps: no window manager says which window has the focus; the apps' "
                     "own word decides")

    def _on_windows(self, follower) -> None:
        if not self.running:
            return
        if self._placing:
            self._follow_placing()
        self.set_focus(follower.focused())

    def set_focus(self, focused) -> None:
        """The window manager says `focused` (a wm.Window, with its
        client's `pid`; None: no window) has the keyboard focus."""
        old = self.wm_focus
        if (old is None) != (focused is None) or (
                old is not None and (old.id, old.pid) != (focused.id, focused.pid)):
            self.wm_focus = focused
            self._refocus()

    def activity(self) -> None:
        """An app said which of its windows has the focus (`active`)."""
        self._refocus()

    def _keyboard_held(self) -> bool:
        """The canvas has the keyboard (or let it go a moment ago, and the
        compositor is giving it back to the window: the focus is looked
        at again when that moment is over)."""
        scene = self._scene()
        kb = getattr(scene, "keyboard", None)
        if kb is None:
            return False
        if kb.wanted:
            return True
        left = getattr(kb, "released_at", -1e9) + HOLD_GRACE_S - time.monotonic()
        if left <= 0:
            return False
        if self._grace is None:
            def over():
                self._grace = None
                if self.running:
                    self._refocus()
            self._grace = self.later(left + 0.01, over)
        return True

    def holds_focus(self, window: AppWindow) -> bool:
        """Whether `window` keeps the focus whatever it and the compositor
        say now: it has it and the canvas took the keyboard from it."""
        return window is self.focused and self._keyboard_held()

    def _focused_app(self) -> AppWindow | None:
        """The focused app window (the module docstring, "Focus")."""
        held = self._keyboard_held()
        focus = self.wm_focus
        if self.wm_windows is None:
            candidates = self.windows
        elif focus is None:
            return self.focused if held else None
        else:
            candidates = [w for w in self.windows if w.link.pid == focus.pid]
        said = [w for w in candidates if w.active_seq]
        if said:
            return max(said, key=lambda w: w.active_seq)
        if len(candidates) == 1 and candidates[0].active_seq is None and focus is not None:
            return candidates[0]
        return None

    def _refocus(self) -> None:
        window = self._focused_app()
        if window is self.focused:
            return
        self.focused = window
        p = self.prompt
        if p is not None and p.owner is not None and p.owner is not window:
            p.close("focus")
        if self._open_wait is not None and self._open_wait[0] is window:
            self._try_open()
        f = self.wm_focus
        log.debug("apps: focus on %s", f"{window.kind} {window.label}" if window else
                  f"window {f.id} ({f.app}, pid {f.pid}), no app's" if f else "no window")
        for callback in list(self._followers):
            try:
                callback(window)
            except Exception:
                log.exception("apps: a focus follower failed")

    # ── the top bar ─────────────────────────────────────────────────────
    def section(self) -> tuple[int, int, int] | None:
        """The app section of the top bar: its left edge and the bar's
        bottom edge (canvas coordinates), and its width; None with no top
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
        while it is open. `pressed`: for its chord, the keyboard taken at
        once, whether or not it can show. False, and `fallback` to the
        app, when it cannot show (no top bar)."""
        if self.prompt is not None:
            self.prompt.close("shell")      # giving its keyboard back: before ours is taken
        held = self.hold_keyboard() if pressed else False
        spec = ((window.bar or {}).get(B.INPUTS if what == "input" else B.MENUS) or {}).get(name)
        if not isinstance(spec, dict):
            log.info("%s window %s: no %s %r in its bar; the app does without",
                     window.kind or "app", window.label, what, name)
            if pressed:
                self.release_keyboard()
            window.send_event(B.event(B.FALLBACK, **{what: name}))
            return False
        # From here on every way out must give the keyboard back unless the
        # prompt holds it: an exception left unhandled here would leave the
        # whole desktop's keyboard with the shell.
        why = "no top bar" if self.section() is None else None
        if why is None and not pressed:
            held = self.hold_keyboard()
        prompt = None
        if why is None:
            try:
                prompt = Prompt(self, window, what, name, spec, chord=chord, anchor=anchor)
            except Exception:
                log.exception("the %s %r failed to open for %s window %s", what, name,
                              window.kind or "app", window.label)
                why = "it failed to open"
        if prompt is None:
            log.info("%s window %s: its %s %r cannot show (%s); the app does without",
                     window.kind or "app", window.label, what, name, why)
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
        self._catch_outside()
        prompt.start()
        self.changed(prompt)
        return True

    def open_asked(self, window: AppWindow, what: str, name, *, chord: str | None = None) -> None:
        """An app asks for one of its inputs or menus open with no key
        pressed (`open`): honoured once the window has focus and its bar
        has that input or menu, waiting for both up to OPEN_WAIT_S (a new
        window asks as soon as it is shown: the window manager may focus
        it a moment later, and its first bar may come after the ask);
        else `fallback`. With `chord` (the app caught a chord of its bar,
        PROTOCOL.md section 4) it opens as for a press: the keyboard taken
        at once, and the app's forwarded keys awaited."""
        old = self._open_wait
        if old is not None:
            if old[3] is not None:
                old[3].cancel()
            if old[0] is not window:
                log.info("%s window %s: its %s %r gave way to another window's; the app "
                         "does without", old[0].kind, old[0].label, old[1], old[2])
                old[0].send_event(B.event(B.FALLBACK, **{old[1]: old[2]}))
        if chord is not None:
            # The key reached the app's window: it has the focus, whatever
            # the compositor and the app have said about it so far.
            if self.focused is not window and window in self.windows:
                if window.active_seq is not None:
                    window.active_seq = next(AppWindow._actives)
                self._refocus()
        self._open_wait = (window, what, name, None, chord)
        if self._try_open():
            return
        log.debug("%s window %s asks for its %s %r: waiting for %s", window.kind,
                  window.label, what, name, self._open_missing(window, what, name))
        self._open_wait = (window, what, name, self.later(B.OPEN_WAIT_S, self._open_late),
                           chord)

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
        window, what, name, timer, chord = wait
        if window.closed or self._open_missing(window, what, name) is not None:
            return False
        self._open_wait = None
        if timer is not None:
            timer.cancel()
        if chord is not None:
            if self.open_prompt(window, what, name, pressed=True, chord=chord):
                self._forward_from(window)
        else:
            self.open_prompt(window, what, name, pressed=False)
        return True

    def _forward_from(self, window: AppWindow) -> None:
        """The app's window will forward the keys it gets until its `last`."""
        if self._forwarding is not None and self._forwarding is not window:
            self.forwarded(self._forwarding)
        self._forwarding = window
        self._held = []

    def _open_late(self) -> None:
        wait, self._open_wait = self._open_wait, None
        if wait is None:
            return
        window, what, name, _timer, _chord = wait
        log.info("%s window %s: its %s %r not opened: still waiting for %s after %.1f s; "
                 "the app does without", window.kind, window.label, what, name,
                 self._open_missing(window, what, name) or "nothing", B.OPEN_WAIT_S)
        window.send_event(B.event(B.FALLBACK, **{what: name}))

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
        if self._forwarding is not None:
            self._forwarding = None         # the app stops at `closed`; nothing to wait for
            self._held = []
            if self._held_timer is not None:
                self._held_timer.cancel()
                self._held_timer = None
        try:
            self._drop_hide()
        except Exception:
            log.exception("hiding the drop-down failed")
        self._release_outside()
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
        """Where the list drops: (left, top, width) in canvas coordinates."""
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
        """Open panels close, as for a click elsewhere: the prompt catches
        the outside clicks from now on (`_catch_outside`)."""
        try:
            from ..core.daemon import get_daemon
            get_daemon().dismiss_others(DROP_WINDOW)
        except RuntimeError:
            pass

    # ── the pointer, while a prompt is open ─────────────────────────────
    def _catch_outside(self) -> None:
        """A press outside the bar and its list closes the prompt: the
        canvas's outside clicks caught (presses on any region of it still
        reach that region), for the bar's region, which so shows over a
        fullscreen window while the prompt is open."""
        window = getattr(self.context, "window", None)
        scene = self._scene()
        if scene is not None and getattr(window, "wid", None) is not None:
            scene.catch_outside(self, self._outside_click, window)

    def _release_outside(self) -> None:
        scene = self._scene()
        if scene is not None:
            scene.release_outside(self)

    def _outside_click(self) -> None:
        if self.prompt is not None:
            log.debug("a click outside the bar and its list: the prompt closes")
            self.prompt.close("click")
