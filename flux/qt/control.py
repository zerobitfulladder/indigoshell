"""The control socket, the browser's side: hand-offs and the agent's requests.

One unix socket per browser process (protocol.control_socket_path(),
the instance socket grown; mode 0600 in $XDG_RUNTIME_DIR). It takes two
kinds of line, told apart by `id`:

- with none, the single-instance hand-off: a later invocation's
  {"type": "open", "urls": [...]}, answered {"type": "ok"} and the line
  closed, exactly as before (instance.py; a browser started from newer
  code still hands off to one running older code, and back).
- with one, a request of the agent's (protocol.py documents every one).
  The connection stays open; requests run side by side and are answered
  as they finish; `cancel` stops one.

One socket rather than two: the process already has one place it is
found at, guarded by the instance lock, listening from the moment the
browser can take anything; a client that finds no browser there knows
none runs and can start one (flux/control.py, `ensure_running`). A
second socket would be a second name to agree on, a second thing to
clean up, and a moment when one listens and the other does not.

A request is a coroutine (`async def` on `Requests`) driven by Qt, not
asyncio: what it awaits is a `Job`'s, a script in the page or a pause,
and the job resumes it from the Qt callback that answers. Cancelling
closes the coroutine; an answer arriving for it later is dropped.

**What the agent may touch.** The tab it reads, shows or acts on must
be the current one of its window (protocol.py, "Showing"); `switch`
makes it so without raising the window, taking focus or changing the
workspace. A window on another workspace keeps its pages live (measured:
visible, timers and animation frames unthrottled, `grab()` working), so
its current tab counts as showing. Page content is read in an isolated
script world (reader.py): the refs the agent gets are invisible to the
page. A password field's value is never read; cookies and storage are
not exported; a site's permission prompts stay the user's (window.py).

**Acting.** The acting requests (protocol.py, "Acting") send real
input to the page's view (act.py), find what a ref or a point is in the
agent's world (reader.js `point`: scrolled into view, the middle of its
visible part, refused when something covers it), and answer once the
page settled. They are the agent's session on the tab (watch.py): they
start or go on with it, and every request naming a tab the agent drives
keeps it from running out while it runs. A paused tab refuses them all
(protocol.TOOK_OVER), and a dialog held for the agent refuses all but
`dialog` and the ones that only look (`tabs`, `screenshot`, `wait`,
which ends at once, saying the dialog). A page popup (a datalist's, an
autofill list) that opened anyway is closed when the action settles:
it would hold the user's pointer and keyboard.

**Alone.** The agent's own window (protocol.py, "Alone"; app.py has
the moves): `open` in it, `borrow` a user's tab into it, `return` a tab
from it, each answered once the window is placed and shown (window.py
`show_placed`). `show` opens a local file in the user's window;
`downloads` lists this run's downloads and `wait` waits for one;
`upload` arms the answer to the page's next file chooser (watch.py
`arm_files`) with paths flux/uploads.py let through, then clicks what
opens it with real input (a hidden field: the key F24, which lets a page
open a chooser and does nothing else, then a click in the agent's world;
measured: a modifier alone does not let it, F24 does), and answers once
the chooser took them. A host sites.json keeps from the agent (identity.py)
refuses every request on its tabs (`allowed`).

**Frames** (frames.py). Every read is the page's and its frames', each
document read in its own world, laid out as one; a ref "f2:14" names an
element in frame f2, and acting on it finds it there and carries its
point up through the frames above it to the page's viewport (`spot`);
a point over a frame is asked of the frame. `ask` runs a ref's reader.js
call in the document the ref is in.

**Scripts** (`js`, script.py). The agent's own code, in a world of its
own behind guard.js, the guard run first in every frame and again just
before the code; its answer polled for, scrubbed of password values in
the reader's world (frames.py `scrub`), cut. An acting request: it is
the agent's session, and its answer says what the page did after.

**Full-page screenshots** (`_full`). Viewport-high tiles, top down, at
most protocol.SHOT_TILES_MAX: each scrolled to (reader.js `tile`, at once,
not smoothly), a fresh frame waited for, the view grabbed; the fixed and
sticky hidden from the second tile on; scroll and sheets put back after,
however the request ends.

**Headless** (protocol.py "Headless"): no window has focus, none has an
X window (`tabs` says `xid` null and `headless`), `look` looks at the
window named or the most recently used, and its image is only ever the
selection's: there is no mouse pointer to be under.

Every request is logged at info: its type, the tab and the host, never
what the page holds, never what the agent types or runs, never a file's
name.

Qt: nothing here is newer than 6.8 (the Pi's Debian). Frames are reached
with QWebEngineFrame and QWebEnginePage.mainFrame (6.8, frames.py); with
an older Qt, frames are not read nor acted in, a frame's selection is
not read, and the reads say so.
"""

import itertools
import json
import logging
import os
import re
import time
from functools import partial
from urllib.parse import urlsplit

import shiboken6
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from indigoshell.applink.protocol import PLACE_WAIT_S
from flux import protocol, uploads

from . import act, frames, reader, script
from .tabs import Tab, to_url

log = logging.getLogger(__name__)

LINE_MAX = 1 << 20              # bytes: a longer request line ends the connection
JS_TIMEOUT_S = 10.0             # a script in the page that never answers
POLL_S = 0.15                   # wait's checks
FRAME_POLL_S = 0.016
FRAME_TIMEOUT_S = 1.0           # a page that paints no frame: grabbed anyway
FRAMES_MAX = 50                 # frames asked for a selection, at most
SETTLE_POLL_S = 0.03            # an action's settling, checked this often
TYPE_BATCH = 20                 # characters typed between turns of the event loop
SCROLL_POLL_S = 0.06
SCROLL_MAX_S = 1.5              # a smooth scroll's end, waited for at most
FILL_MAX = 100                  # fields in one fill
READY_POLL_S = 0.03             # the agent's new window, checked this often until shown
HOLD_TRIES = 8                  # a running glitch (watch.py hold_s) waited out this often at most
# Requests whose name is no Python method name.
METHODS = {"return": "return_"}


class RequestError(Exception):
    """A request that cannot be done: answered ok:false with the message."""


class _Await:
    """What a request awaits: `begin(job)` starts it, and the job resumes
    the request with its outcome."""

    def __init__(self, begin) -> None:
        self.begin = begin

    def __await__(self):
        return (yield self)


# ── one request ─────────────────────────────────────────────────────────
class Job:
    def __init__(self, conn: "Connection", rid: int, kind: str) -> None:
        self.conn, self.id, self.kind = conn, rid, kind
        self.coro = None
        self.finished = False
        self._seq = 0                   # which await is current; older answers are dropped
        self._expire = None             # what the current await's timer does
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timed_out)
        self.cleanups: list = []        # run once it is over, however it ends

    def start(self, coro) -> None:
        self.coro = coro
        self._step(None, None)

    def _step(self, value, error) -> None:
        if self.finished:
            return
        try:
            waiting = self.coro.throw(error) if error is not None else self.coro.send(value)
        except StopIteration as done:
            self._finish(protocol.result(self.id, **(done.value or {})))
        except (RequestError, reader.ScriptError) as e:
            self._finish(protocol.failure(self.id, str(e)))
        except Exception as e:          # noqa: BLE001  (a bug: said, and the browser goes on)
            log.exception("request %s %d failed", self.kind, self.id)
            self._finish(protocol.failure(self.id, f"{type(e).__name__}: {e}"))
        else:
            waiting.begin(self)

    def _finish(self, msg: dict) -> None:
        if self.finished:
            return
        self._close()
        self.conn.send(msg)

    def _close(self) -> None:
        self.finished = True
        self.timer.stop()
        self.timer.deleteLater()
        self.conn.jobs.pop(self.id, None)
        cleanups, self.cleanups = self.cleanups, []
        for fn in cleanups:
            try:
                fn()
            except Exception:           # noqa: BLE001
                log.exception("request %s %d: a cleanup failed", self.kind, self.id)

    def cancel(self, *, answer: bool = True) -> None:
        if self.finished:
            return
        self._close()
        if self.coro is not None:
            try:
                self.coro.close()
            except (ValueError, RuntimeError):  # cancelled from inside its own step
                pass
        if answer:
            self.conn.send(protocol.failure(self.id, protocol.CANCELLED))

    # ── awaiting ────────────────────────────────────────────────────────
    def _await(self, start, seconds: float, expire) -> _Await:
        def begin(job: "Job") -> None:
            self._seq += 1
            seq = self._seq
            self._expire = lambda: self._resume(seq, *expire())
            self.timer.start(max(0, int(seconds * 1000)))
            if start is not None:
                start(lambda value: self._resume(seq, value, None))
        return _Await(begin)

    def _resume(self, seq: int, value, error) -> None:
        if self.finished or seq != self._seq:
            return
        self._seq += 1                  # this await is over
        self.timer.stop()
        self._step(value, error)

    def _timed_out(self) -> None:
        if self._expire is not None and not self.finished:
            self._expire()

    def sleep(self, seconds: float) -> _Await:
        return self._await(None, seconds, lambda: (None, None))

    def js(self, target, code: str, *, timeout: float = JS_TIMEOUT_S,
           quiet: bool = False, world: int = reader.WORLD) -> _Await:
        """`code` run in the agent's world of `target` (a page or a frame);
        its answer (reader.answer). `quiet`: no answer in time is None,
        not an error. `world`: another of the agent's worlds (script.py)."""
        def start(done) -> None:
            target.runJavaScript(code, world, done)

        def expire():
            if quiet:
                return None, None
            return None, RequestError(f"the page did not answer within {timeout:g} s")
        return self._await(start, timeout, expire)

    def many(self, calls: list, timeout: float, *, world: int = reader.WORLD) -> _Await:
        """Several scripts at once, each `(target, code)` in its target's
        world: their raw answers in order, None for one that gave none
        within `timeout` (frames.py asks every frame together)."""
        answers: list = [None] * len(calls)
        got = [False] * len(calls)

        def start(done) -> None:
            left = [len(calls)]

            def answered(i: int, value) -> None:
                if got[i]:
                    return
                got[i], answers[i] = True, value
                left[0] -= 1
                if not left[0]:
                    done(list(answers))
            if not calls:
                QTimer.singleShot(0, lambda: done([]))
                return
            for i, (target, code) in enumerate(calls):
                callback = partial(answered, i)
                hold.append(callback)       # alive while the answers come
                target.runJavaScript(code, world, callback)
        hold: list = []
        return self._await(start, timeout, lambda: (list(answers), None))

    async def read(self, target, code: str, **kw):
        """`js`, its answer unpacked (reader.answer); None for a quiet
        call that got none."""
        raw = await self.js(target, code, **kw)
        if not raw and kw.get("quiet"):
            return None                 # no answer: the document went away meanwhile
        return reader.answer(raw)


# ── one client ──────────────────────────────────────────────────────────
class Connection(QObject):
    def __init__(self, server: "ControlServer", sock: QLocalSocket) -> None:
        super().__init__(server)
        self.server, self.sock = server, sock
        self.jobs: dict[int, Job] = {}
        self.control = False            # it sent a request (it is no hand-off)
        self.closed = False             # no more lines taken, nothing more sent
        self._released = False
        sock.readyRead.connect(self._read)
        sock.disconnected.connect(self._gone)

    def _read(self) -> None:
        while not self.closed and self.sock.canReadLine():
            raw = bytes(self.sock.readLine()).decode(errors="replace")
            self._handle(protocol.decode(raw))
        if not self.closed and self.sock.bytesAvailable() > LINE_MAX:
            log.warning("control: a line of more than %d bytes; connection dropped", LINE_MAX)
            self.close()

    def _handle(self, msg: dict | None) -> None:
        if msg is None:
            if self.control:
                log.debug("control: a line that is no message, ignored")
            else:
                self.close()
            return
        if protocol.ID not in msg:
            if self.control:
                log.debug("control: a %r with no id, ignored", msg[protocol.TYPE])
            else:
                self.server.hand_off(self, msg)
            return
        self.control = True
        rid, kind = msg[protocol.ID], msg[protocol.TYPE]
        if kind == protocol.CANCEL:
            job = self.jobs.get(rid) if protocol.is_id(rid) else None
            if job is not None:
                log.info("agent: cancel %s %d", job.kind, rid)
                job.cancel()
            return
        if not protocol.is_id(rid):
            self.send(protocol.failure(rid, "`id` must be an integer"))
            return
        if rid in self.jobs:
            self.send(protocol.failure(rid, f"request {rid} is still running"))
            return
        if kind not in protocol.REQUESTS:
            self.send(protocol.failure(rid, f"no request named {kind!r}"))
            return
        requests = self.server.requests
        requests.note(kind, msg)
        job = Job(self, rid, kind)
        self.jobs[rid] = job
        requests.watch.touch(job, msg.get("tab"))   # a session it names lasts while it runs
        job.start(getattr(requests, METHODS.get(kind, kind))(job, msg))

    def send(self, msg: dict) -> None:
        if self.closed or not shiboken6.isValid(self.sock):
            return
        if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self.sock.write(protocol.encode(msg))
            self.sock.flush()

    def close(self) -> None:
        """Stop taking lines and end the line once what was sent is
        written (its jobs go with it, unanswered)."""
        if self.closed:
            return
        self._stop()
        S = QLocalSocket.LocalSocketState
        if shiboken6.isValid(self.sock) and self.sock.state() != S.UnconnectedState:
            self.sock.disconnectFromServer()        # `disconnected` follows: _gone
            if self.sock.state() != S.UnconnectedState:
                return
        self._release()

    def _gone(self) -> None:
        self._stop()
        self._release()

    def _stop(self) -> None:
        self.closed = True
        for job in list(self.jobs.values()):
            job.cancel(answer=False)

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self.server.connections.discard(self)
        if shiboken6.isValid(self.sock):
            self.sock.deleteLater()
        self.deleteLater()


# ── the listening end ───────────────────────────────────────────────────
class ControlServer(QObject):
    """Listens at the control socket for hand-offs and requests."""

    def __init__(self, browser, path: str | None = None) -> None:
        super().__init__(browser)
        self.browser = browser
        self.requests = Requests(browser)
        self.path = path or protocol.control_socket_path()
        self.connections: set[Connection] = set()
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        # We hold the instance lock, so whatever is at the path is a dead browser's.
        QLocalServer.removeServer(self.path)
        if self.server.listen(self.path):
            try:
                os.chmod(self.path, 0o600)
            except OSError as e:
                log.warning("cannot make %s private: %s", self.path, e)
            log.debug("listening on %s", self.path)
        else:
            log.error("cannot listen on %s: %s", self.path, self.server.errorString())

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            self.connections.add(Connection(self, self.server.nextPendingConnection()))

    def hand_off(self, conn: Connection, msg: dict) -> None:
        """A later invocation's URLs (instance.py), as it always was."""
        if msg[protocol.TYPE] != protocol.OPEN:
            log.debug("instance socket: %r not understood", msg[protocol.TYPE])
            conn.close()
            return
        urls = msg.get(protocol.URLS)
        if isinstance(urls, list) and all(isinstance(u, str) for u in urls):
            log.info("handed %s", f"{len(urls)} URL(s)" if urls else "a new window")
            self.browser.open_urls(urls)
            conn.send(protocol.message(protocol.OK))
        else:
            conn.send(protocol.message(protocol.ERROR, error="`urls` is not a list of strings"))
        conn.close()

    def close(self) -> None:
        for conn in list(self.connections):
            conn.close()
        self.server.close()


# ── the requests ────────────────────────────────────────────────────────
def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _int(msg: dict, key: str, default: int | None = None) -> int:
    value = msg.get(key, default)
    if not protocol.is_id(value):
        raise RequestError(f"`{key}` must be an integer")
    return value


def _number(msg: dict, key: str, default: float | None = None) -> float | None:
    value = msg.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"`{key}` must be a number")
    return float(value)


def _ref(msg: dict, key: str = "ref") -> tuple[int | None, int]:
    """A REF (protocol.parse_ref): (frame number or None, its number)."""
    parsed = protocol.parse_ref(msg.get(key))
    if parsed is None:
        raise RequestError(f"`{key}` must be a ref from read: a number, or \"f2:14\" for an "
                           "element in frame f2")
    return parsed


def _strings(value, key: str) -> list[str]:
    """A string, or a list of strings, as a list."""
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(v, str) for v in values):
        raise RequestError(f"`{key}` must be a string or a list of strings")
    return values


REFUSALS = {
    "upload": lambda what: protocol.FILE_FIELD.format(what=what),
    "select": lambda what: f"{what} is a <select>: choose its option with select (its popup "
                           "would take the user's pointer)",
    "picker": lambda what: f"{what} opens a picker that would take the user's pointer: set it "
                           "with fill",
}


def _seconds(msg: dict, key: str, default: float, top: float) -> float:
    value = msg.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RequestError(f"`{key}` must be a number of seconds")
    return min(float(value), top)


class Requests:
    """What each request does (protocol.py has their shapes)."""

    def __init__(self, browser) -> None:
        self.browser = browser
        self.watch = browser.watch
        self.runs = itertools.count(1)  # the agent's scripts (js), in this browser

    # ── finding windows and tabs ────────────────────────────────────────
    def windows(self) -> list:
        return [w for w in self.browser.windows if shiboken6.isValid(w) and w.tabs]

    def window(self, wid: int):
        for w in self.windows():
            if w.id == wid:
                return w
        raise RequestError(f"no window {wid}")

    def focused(self):
        if self.browser.headless:
            return None                 # Qt's offscreen platform "activates" what it shows
        active = QApplication.activeWindow()
        return active if active is not None and active in self.windows() else None

    def find(self, tab_id: int) -> tuple:
        for w in self.windows():
            for tab in w.tabs:
                if tab.id == tab_id:
                    return w, tab
        raise RequestError(f"no tab {tab_id}")

    # ── the sites the user keeps from the agent (identity.py) ────────────
    def kept(self, host: str) -> bool:
        return bool(host) and self.browser.identity.agent_policy(host) == "never"

    def allowed(self, tab: Tab) -> None:
        """Refused: a tab at a host the user keeps from the agent."""
        host = tab.host()
        if self.kept(host):
            raise RequestError(protocol.kept(host))

    def allowed_url(self, url: QUrl) -> None:
        if self.kept(url.host()):
            raise RequestError(protocol.kept(url.host()))

    def showing(self, tab_id: int) -> tuple:
        """The tab, which must be the current one of its window, not
        taken over by the user, and at no host they keep from the agent."""
        window, tab = self.find(tab_id)
        self.allowed(tab)
        if self.watch.paused(tab_id):
            raise RequestError(protocol.took_over(tab_id))
        if window.current_tab() is not tab:
            raise RequestError(protocol.not_showing(tab_id))
        return window, tab

    def not_hidden(self, window) -> None:
        """What would change what `window` shows: refused while its tab is
        the user's (taken over)."""
        current = window.current_tab() if window is not None else None
        if current is not None and self.watch.paused(current.id):
            raise RequestError(protocol.took_over(current.id))

    def no_dialog(self, tab: Tab) -> None:
        held = self.watch.dialog_of(tab.id)
        if held is not None:
            article = "an" if held.kind[0] in "aeiou" else "a"
            raise RequestError(f"tab {tab.id} has {article} {held.kind} dialog open; answer it "
                               "first (dialog: accept or dismiss)")

    def tab(self, window, tab: Tab) -> dict:
        origin = window.borrowed.get(tab.id)
        out = {"tab": tab.id, "window": window.id, "index": window.tabs.index(tab),
               "title": tab.page.title(), "url": tab.url().toString(),
               "loading": tab.busy(), "current": window.current_tab() is tab,
               **self.watch.about(tab.id),
               "borrowed": {"window": origin["window_id"], "index": origin["index"]}
               if origin else None}
        host = tab.host()
        if self.kept(host):
            out.update(title="", url=f"{tab.url().scheme()}://{host}/", kept=True)
        return out

    async def until_ready(self, job: Job, window) -> None:
        """Until the agent's new window is placed and shown (window.py
        `show_placed`: at most applink's PLACE_WAIT_S)."""
        deadline = time.monotonic() + PLACE_WAIT_S + 1.0
        while not window.ready and time.monotonic() < deadline:
            await job.sleep(READY_POLL_S)

    def note(self, kind: str, msg: dict) -> None:
        """The request in the log: its type, its tab and host, no content.
        (`look` says its own, once it knows the tab.)"""
        if kind == "look":
            return
        parts = [kind]
        tab_id = msg.get("tab")
        if protocol.is_id(tab_id):
            parts.append(f"tab {tab_id}")
            try:
                host = self.find(tab_id)[1].host()
            except RequestError:
                host = ""
        else:
            host = ""
        if kind in ("open", "go") and isinstance(msg.get("url"), str):
            host = _host(to_url(msg["url"]).toString()) or host
            parts.append("to")
        if kind == "go" and msg.get("action") in protocol.GO_ACTIONS:
            parts.append(msg["action"])
        if kind == "open" and msg.get("window") == protocol.AGENT_WINDOW:
            parts.append("in the agent's window")
        if kind == "show":
            parts.append("a local file")
        if kind == "upload" and isinstance(msg.get("paths"), list):
            parts.append(f"{len(msg['paths'])} file(s)")
        if kind == "wait" and protocol.is_id(msg.get("download")):
            parts.append(f"for download {msg['download']}")
        if kind == "read" and msg.get("what") in protocol.READS:
            parts.append(msg["what"])
        if kind == "screenshot" and msg.get("full"):
            parts.append("full")
        log.info("agent: %s%s", " ".join(parts), f" ({host})" if host else "")

    # ── the requests ────────────────────────────────────────────────────
    def xid(self, window) -> int | None:
        """The window's X window; None headless (offscreen numbers are no
        window manager's) and on Wayland (Qt's numbers there are its own)."""
        if self.browser.headless or QGuiApplication.platformName().startswith("wayland"):
            return None
        return int(window.internalWinId()) or None

    async def quit(self, job: Job, msg: dict) -> dict:
        """Flux ends right after this answer (protocol.py, "Quit")."""
        self.browser.quit("a quit request")
        return {"quitting": True}

    async def tabs(self, job: Job, msg: dict) -> dict:
        focused = self.focused()
        out = []
        for w in self.windows():
            out.append({"window": w.id, "xid": self.xid(w),
                        "single": w.single, "focused": w is focused, "agent": w.agent,
                        "tabs": [self.tab(w, t) for t in w.tabs]})
        return {"windows": out, "focused": focused.id if focused is not None else None,
                "headless": self.browser.headless, "profile": self.browser.profile_name}

    async def open(self, job: Job, msg: dict) -> dict:
        url = msg.get("url")
        if not isinstance(url, str) or not url.strip():
            raise RequestError("`url` must be an address (or a search)")
        target = to_url(url.strip())
        self.allowed_url(target)
        where = msg.get("window")
        if where == protocol.AGENT_WINDOW:
            window = self.browser.agent_window()
        elif where is not None:
            window = self.window(_int(msg, "window"))
            if window.single:
                raise RequestError(f"window {window.id} is a page's popup; it takes no tabs")
        else:
            window = self.browser.main_window()
        if not msg.get("background"):
            self.not_hidden(window)
        tab = window.add_tab(target, background=bool(msg.get("background")))
        tab.expect_load()
        if window.agent:
            window.show_placed()        # a window just made: placed, then shown
            await self.until_ready(job, window)
        window, tab = self.find(tab.id)
        return self.tab(window, tab)

    async def switch(self, job: Job, msg: dict) -> dict:
        window, tab = self.find(_int(msg, "tab"))
        self.allowed(tab)
        if self.watch.paused(tab.id):
            raise RequestError(protocol.took_over(tab.id))
        if window.current_tab() is not tab:
            self.not_hidden(window)
            window.select(window.tabs.index(tab))     # never raised, never focused
        return self.tab(window, tab)

    async def close(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        last = len(window.tabs) == 1
        window.close_tab(tab)
        return {"tab": tab.id, "window_closed": last}

    async def go(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        self.no_dialog(tab)
        url, action = msg.get("url"), msg.get("action")
        if (url is None) == (action is None):
            raise RequestError("give `url` or `action`")
        WA = QWebEnginePage.WebAction
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                raise RequestError("`url` must be an address (or a search)")
            self.allowed_url(to_url(url.strip()))
            window.load(url.strip(), "here", tab)
        elif action in ("back", "forward"):
            history = tab.page.history()
            if not (history.canGoBack() if action == "back" else history.canGoForward()):
                raise RequestError(f"tab {tab.id} has no page to go {action} to")
            tab.note_input()            # as the user's Back: a new beginning (tabs.py)
            tab.page.triggerAction(WA.Back if action == "back" else WA.Forward)
        elif action == "reload":
            tab.page.triggerAction(WA.Reload)
        else:
            raise RequestError(f"`action` must be one of {', '.join(protocol.GO_ACTIONS)}")
        tab.expect_load()
        return self.tab(window, tab)

    async def wait(self, job: Job, msg: dict) -> dict:
        if msg.get("download") is not None:
            return await self._wait_download(job, msg)
        tab_id = _int(msg, "tab")
        self.showing(tab_id)
        timeout = _seconds(msg, "timeout", protocol.WAIT_DEFAULT_S, protocol.WAIT_MAX_S)
        selector, text = msg.get("selector"), msg.get("text")
        if selector is not None and text is not None:
            raise RequestError("give `selector` or `text`, not both")
        for key, value in (("selector", selector), ("text", text)):
            if value is not None and (not isinstance(value, str) or not value):
                raise RequestError(f"`{key}` must be a non-empty string")
        what = (f"the selector {selector!r}" if selector is not None
                else f"the text {text!r}" if text is not None else "its load to finish")
        began = time.monotonic()
        deadline = began + timeout
        while True:
            try:
                window, tab = self.showing(tab_id)
            except RequestError as e:
                if self.watch.paused(tab_id):
                    raise
                raise RequestError(f"tab {tab_id} stopped showing while waiting ({e})") from None
            if self.watch.dialog_of(tab_id) is not None:
                break                   # the page waits for its dialog: said in the answer
            if selector is None and text is None:
                if not tab.busy():
                    break
            elif not tab.asked():
                # Not while a load asked for has yet to end: the page
                # still shown is the one being left.
                found = await job.read(tab.page, reader.script(
                    "has", {"selector": selector, "text": text}), timeout=2.0, quiet=True)
                if found and found.get("found"):
                    break
            left = deadline - time.monotonic()
            if left <= 0:
                raise RequestError(f"timed out after {timeout:g} s waiting for {what}")
            await job.sleep(min(POLL_S, left))
        window, tab = self.showing(tab_id)
        return {**self.tab(window, tab), "waited": round(time.monotonic() - began, 3)}

    async def read(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        self.no_dialog(tab)             # the page cannot answer until it is
        what = msg.get("what")
        if what not in protocol.READS:
            raise RequestError(f"`what` must be one of {', '.join(protocol.READS)}")
        default, top = {"text": (protocol.TEXT_LENGTH, protocol.TEXT_MAX),
                        "links": (protocol.LINKS_LENGTH, protocol.LINKS_MAX),
                        "forms": (protocol.FORMS_LENGTH, protocol.FORMS_MAX),
                        "outline": (protocol.OUTLINE_LENGTH, protocol.OUTLINE_MAX),
                        "article": (protocol.ARTICLE_LENGTH, protocol.ARTICLE_MAX)}[what]
        offset = _int(msg, "offset", 0)
        length = _int(msg, "length", default)
        if offset < 0 or length < 1:
            raise RequestError("`offset` must be 0 or more and `length` 1 or more")
        value = await frames.read(job, tab, what, {"offset": offset, "length": min(length, top),
                                                   "hidden": bool(msg.get("hidden"))}, self.kept)
        return {**self.tab(window, tab), "what": what, **value}

    async def screenshot(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        if msg.get("full"):
            self.no_dialog(tab)         # the page must answer to be scrolled
            return await self._full(job, tab)
        if self.watch.dialog_of(tab.id) is None:    # else the page paints nothing new
            await self._fresh_frame(job, tab)
        window, tab = self.showing(tab.id)
        view = tab.view
        zoom = tab.page.zoomFactor() or 1.0
        return {**self.tab(window, tab), **reader.png(reader.grab(view)),
                "viewport_width": round(view.width() / zoom),
                "viewport_height": round(view.height() / zoom)}

    async def _full(self, job: Job, tab: Tab) -> dict:
        """The page from its top in viewport-high tiles (protocol.py
        `screenshot`, full): scrolled to and grabbed in turn, the fixed and
        sticky hidden from the second on, all put back after (also when
        the request ends otherwise: `job.cleanups`)."""
        size = await job.read(tab.page, reader.script("pageSize"))
        home = {"x": size["x"], "y": size["y"]}
        ended = [False]
        job.cleanups.append(partial(self._end_tiles, tab, home, ended))
        vh = size["vh"]
        tiles, want, height = [], 0, size["height"]
        while len(tiles) < protocol.SHOT_TILES_MAX:
            at = await job.read(tab.page, reader.script(
                "tile", {"x": home["x"], "y": want, "hide": bool(tiles)}))
            height = at["height"]
            if tiles and at["y"] <= tiles[-1]["y"]:
                break                   # the page scrolls no further
            self.showing(tab.id)
            await self._fresh_frame(job, tab)
            window, tab = self.showing(tab.id)
            tiles.append({**reader.png(reader.grab(tab.view)), "x": at["x"], "y": at["y"]})
            if at["y"] + vh >= height - 1:
                break                   # the bottom is in this one
            want = at["y"] + vh
        ended[0] = True
        await job.read(tab.page, reader.script("endTiles", home))
        window, tab = self.showing(tab.id)
        last = tiles[-1]["y"] if tiles else 0
        return {**self.tab(window, tab), "tiles": tiles, "page_width": size["width"],
                "page_height": height, "viewport_width": size["vw"], "viewport_height": vh,
                "more": last + vh < height - 1}

    def _end_tiles(self, tab: Tab, home: dict, ended: list) -> None:
        if not ended[0] and shiboken6.isValid(tab) and shiboken6.isValid(tab.page):
            tab.page.runJavaScript(reader.script("endTiles", home), reader.WORLD)

    async def _fresh_frame(self, job: Job, tab: Tab) -> None:
        """Until the page has painted a frame begun after this request
        came, so the grab shows what it does now. A load the agent asked
        for a moment ago gets up to FRAME_TIMEOUT_S to arrive first: a
        script sent straight after a same-document move (an anchor) can
        run before the move does (measured)."""
        deadline = time.monotonic() + FRAME_TIMEOUT_S
        while tab.asked() and time.monotonic() < deadline:
            await job.sleep(FRAME_POLL_S)
        n = await job.read(tab.page, reader.script("frame"), timeout=2.0, quiet=True)
        if not n:
            return
        deadline = time.monotonic() + FRAME_TIMEOUT_S
        while time.monotonic() < deadline:
            painted = await job.read(tab.page, reader.expression("window.__flux.painted"),
                                     timeout=FRAME_TIMEOUT_S, quiet=True)
            if painted is None or painted >= n:
                break
            await job.sleep(FRAME_POLL_S)
        await job.sleep(FRAME_POLL_S)   # the frame from the renderer to the view

    async def look(self, job: Job, msg: dict) -> dict:
        window = self._look_window(msg)
        tab = window.current_tab()
        self.allowed(tab)
        focused = self.focused() is window
        out = {**self.tab(window, tab), "focused": focused,
               "unfocused_for": None if focused or window.unfocused_at is None
               or self.browser.headless
               else round(time.monotonic() - window.unfocused_at, 3)}
        if self.browser.headless:
            out["headless"] = True
        log.info("agent: look at tab %d%s", tab.id, f" ({tab.host()})" if tab.host() else "")
        # A page waiting on a dialog held for the agent answers no script.
        blocked = self.watch.dialog_of(tab.id) is not None
        sel = {} if blocked else await job.read(tab.page, reader.script(
            "selection", {"image": bool(msg.get("image"))}), timeout=3.0, quiet=True) or {}
        text, where = sel.get("text") or "", sel.get("in")
        if where == "frame":
            text = await self._frame_selection(job, tab)
            where = "frame" if text else None
        out["selection"], out["selection_in"] = text, (where if text else None)
        if msg.get("screenshot") or msg.get("image"):
            if not blocked:
                await self._fresh_frame(job, tab)
            if not shiboken6.isValid(tab.view):
                raise RequestError("the tab went away")
            image = reader.grab(tab.view)
            if msg.get("screenshot"):
                out["screenshot"] = reader.png(image)
            if msg.get("image"):
                out["image"] = None if blocked else await self._image(job, tab, image,
                                                                      sel.get("image"))
        return out

    def _look_window(self, msg: dict):
        pid = msg.get("pid")
        theirs = pid is not None and _int(msg, "pid") != os.getpid()
        if msg.get("window") is not None and not theirs:
            return self.window(_int(msg, "window"))
        if msg.get("xid") is not None:
            xid = _int(msg, "xid")
            if self.browser.headless:
                raise RequestError("Flux is headless: its windows have no X window")
            if QGuiApplication.platformName().startswith("wayland"):
                raise RequestError("Flux is on Wayland: its windows have no X window")
            for w in self.windows():
                if int(w.internalWinId()) == xid:
                    return w
            raise RequestError(f"no browser window is X window {xid}")
        if self.browser.headless:
            # Nobody looks: the most recently used window is the one in sight.
            windows = self.windows()
            if not windows:
                raise RequestError("Flux (headless) has no window open")
            return windows[0]
        window = self.focused()
        if window is None:
            last = [w for w in self.windows() if w.unfocused_at is not None]
            window = max(last, key=lambda w: w.unfocused_at) if last else None
        if window is None:
            raise RequestError("no browser window has had focus")
        return window

    async def _frame_selection(self, job: Job, tab: Tab) -> str:
        """The selection in the frame the page's focus is in
        (QWebEngineFrame, Qt 6.8 on): the one whose document has focus,
        else (the window no longer has it) the first holding a
        selection; "" for none."""
        main = getattr(tab.page, "mainFrame", None)
        if main is None:
            return ""
        frames, todo = [], list(main().children())
        while todo and len(frames) < FRAMES_MAX:
            frame = todo.pop(0)
            if frame.isValid():
                frames.append(frame)
                todo.extend(frame.children())
        first = ""
        for frame in frames:
            sel = await job.read(frame, reader.script("selection", {}), timeout=1.0,
                                 quiet=True)
            if not sel or sel.get("in") not in ("page", "field"):
                continue
            if sel.get("focused"):
                return sel.get("text") or ""
            first = first or sel.get("text") or ""
        return first

    async def _image(self, job: Job, tab: Tab, image, chosen: dict | None) -> dict | None:
        """The image in the selection, else under the mouse pointer, and
        what the page says of it: its own pixels where the page may read
        them ("from": "page"), else the part of the grab it covers
        ("screen": a selected image is drawn tinted there)."""
        if chosen is None:
            if self.browser.headless:
                return None             # no mouse pointer for an image to be under
            view = tab.view
            at = view.mapFromGlobal(QCursor.pos())
            if not view.rect().contains(QPoint(at.x(), at.y())):
                return None
            zoom = tab.page.zoomFactor() or 1.0
            chosen = await job.read(tab.page, reader.script(
                "imageAt", at.x() / zoom, at.y() / zoom), timeout=2.0, quiet=True)
            if not chosen:
                return None
        own = reader.from_data_url(chosen.get("pixels") or "")
        part = own if own is not None else reader.crop(image, chosen["rect"], chosen["width"],
                                                       chosen["height"])
        if part is None:
            return None
        return {**reader.png(part), "under": chosen["under"], "url": chosen.get("url") or "",
                "alt": chosen.get("alt") or "", "from": "page" if own is not None else "screen"}

    # ── acting ──────────────────────────────────────────────────────────
    async def acting(self, job: Job, msg: dict, *, dialog_ok: bool = False) -> tuple:
        """The tab an acting request is for, made the agent's (watch.py),
        once a glitch playing over it has ended (watch.py `hold_s`: the
        lock's own, about a quarter of a second, or a repeat's)."""
        window, tab = self.showing(_int(msg, "tab"))
        if not dialog_ok:
            self.no_dialog(tab)
        self.watch.begin(job, window, tab)
        for _ in range(HOLD_TRIES):
            hold = self.watch.hold_s(tab)
            if hold <= 0:
                break
            await job.sleep(hold)
            self.going_on(tab)          # the user may have taken it over meanwhile
        return window, tab

    def proxy(self, tab: Tab):
        """The widget the page takes input through."""
        proxy = tab.view.focusProxy()
        if proxy is None:
            raise RequestError(f"tab {tab.id} has no page to act on yet")
        self.watch.guard(tab)
        return proxy

    @staticmethod
    def at(tab: Tab, spot: dict) -> QPointF:
        """A point in CSS px of the viewport, in the view's own pixels."""
        zoom = tab.page.zoomFactor() or 1.0
        return QPointF(spot["x"] * zoom, spot["y"] * zoom)

    def going_on(self, tab: Tab) -> None:
        """Between the steps of an action: still the agent's to act on."""
        if self.watch.paused(tab.id):
            raise RequestError(protocol.took_over(tab.id))
        if not self.alive(tab):
            raise RequestError(f"tab {tab.id} went away")

    def alive(self, tab: Tab) -> bool:
        return shiboken6.isValid(tab) and any(tab in w.tabs for w in self.windows())

    async def in_frame(self, job: Job, tab: Tab, frame_no: int | None) -> tuple:
        """Frame f<frame_no> of the tab (frames.py): (what runs its scripts,
        its name, the frames down to it); the page itself for None."""
        if frame_no is None:
            return tab.page, "", []
        try:
            nodes = await frames.path(job, tab, frame_no, self.kept)
        except frames.FrameError as e:
            raise RequestError(str(e)) from None
        return nodes[-1].target, nodes[-1].name, nodes

    async def ask(self, job: Job, tab: Tab, ref: tuple, fn: str, *args) -> tuple:
        """reader.js `fn(ref's number, *args)` in the document the ref is
        in: (its answer, the frames down to it)."""
        target, name, path = await self.in_frame(job, tab, ref[0])
        return await job.read(target, reader.script(fn, ref[1], *args, frame=name)), path

    async def into_sight(self, job: Job, tab: Tab, path: list, done: dict) -> None:
        """An element in a frame, acted on there: the frame brought into
        sight in the frames above it, as far as it goes."""
        if path and "x" in done:
            try:
                await frames.to_page(job, tab, path, done["x"], done["y"], False)
            except reader.ScriptError:
                pass

    async def spot(self, job: Job, tab: Tab, msg: dict, *, refuse: bool, cover: bool,
                   need: bool = True) -> dict | None:
        """Where the request points: its `ref`, or `x` and `y` (reader.js
        `point`), in the page's viewport. A ref in a frame is found there
        and its point carried up through the frames above (frames.py
        `to_page`); a point over a frame is asked of the frame. None when
        it names neither and `need` is false."""
        ref, x, y = msg.get("ref"), msg.get("x"), msg.get("y")
        if ref is not None and (x is not None or y is not None):
            raise RequestError("give `ref`, or `x` and `y`, not both")
        path: list = []
        if ref is not None:
            frame_no, n = _ref(msg)
            named = f"[{protocol.ref_name(frame_no, n)}]"
            target, name, path = await self.in_frame(job, tab, frame_no)
            spot = await job.read(target, reader.script(
                "point", {"ref": n, "refuse": refuse, "cover": cover}, frame=name))
        elif x is not None or y is not None:
            where = {"x": _number(msg, "x"), "y": _number(msg, "y")}
            if where["x"] is None or where["y"] is None:
                raise RequestError("give both `x` and `y` (CSS px of the visible page)")
            named = "the point"
            spot = await job.read(tab.page, reader.script("point", {**where, "refuse": refuse,
                                                                    "cover": cover}))
            spot = await self._into_frames(job, tab, spot, refuse, cover)
        elif need:
            raise RequestError("give `ref` (from read) or `x` and `y`")
        else:
            return None
        if spot.get("refused"):
            raise RequestError(REFUSALS[spot["refused"]](spot.get("what") or "it"))
        if spot.get("covered"):
            raise RequestError(f"{named} is covered at ({spot['x']}, {spot['y']}) by another "
                               f"element; nothing done. What covers it: {spot['covered']}")
        if path:
            mapped = await frames.to_page(job, tab, path, spot["x"], spot["y"], cover)
            if mapped.get("covered"):
                raise RequestError(f"{named} is in frame {mapped['frame']}, whose element is "
                                   f"covered at ({mapped['x']}, {mapped['y']}) by another "
                                   f"element; nothing done. What covers it: {mapped['covered']}")
            spot = {**spot, "x": mapped["x"], "y": mapped["y"]}
        return spot

    async def _into_frames(self, job: Job, tab: Tab, spot: dict, refuse: bool,
                           cover: bool) -> dict:
        """A point over a frame's page: what is there, asked of the frame
        (and of a frame in that), the point staying the page's."""
        parent = None
        while spot.get("frame"):
            inner = spot["frame"]
            try:
                node = await frames.child_at(job, tab, parent, inner["index"], self.kept)
            except frames.FrameError as e:
                raise RequestError(str(e)) from None
            got = await job.read(node.target, reader.script(
                "point", {"x": inner["x"], "y": inner["y"], "refuse": refuse, "cover": cover},
                frame=node.name))
            spot = {**got, "x": spot["x"], "y": spot["y"]}
            parent = node
        return spot

    def before(self, tab: Tab) -> dict:
        return {"loads": tab.loads, "navigations": tab.navigations,
                "tabs": {t.id for w in self.windows() for t in w.tabs},
                "downloads": {d["download"] for d in self.browser.download_list}}

    async def settled(self, job: Job, tab: Tab, before: dict, **extra) -> dict:
        """The answer to an action, once the page settled: a navigation it
        caused began, a dialog opened, or SETTLE_S passed quietly."""
        began = time.monotonic()
        while True:
            if not self.alive(tab):
                raise RequestError(f"tab {tab.id} closed (the page closed itself)")
            if (tab.navigations != before["navigations"]
                    or self.watch.dialog_of(tab.id) is not None
                    or time.monotonic() - began >= protocol.SETTLE_S):
                break
            await job.sleep(SETTLE_POLL_S)
        if tab.loads != before["loads"]:
            tab.expect_load()           # a `wait` straight after counts it as loading
        notes = self.watch.notes(tab.id)
        popup = QApplication.activePopupWidget()
        if isinstance(popup, QQuickWidget):
            popup.close()               # a page's popup: it would hold the user's pointer
            notes.append("a list the page popped up (suggestions, a picker) was closed: it "
                         "would take the user's pointer")
            log.info("agent: a page popup closed after acting on tab %d", tab.id)
        if self.watch.paused(tab.id):
            raise RequestError(protocol.took_over(tab.id))
        window, tab = self.find(tab.id)
        opened = [self.tab(w, t) for w in self.windows() for t in w.tabs
                  if t.id not in before["tabs"]]
        out = {**self.tab(window, tab), "navigated": tab.navigations != before["navigations"],
               "opened": opened, **extra}
        if notes:
            out["notes"] = notes
        started = [self.browser.download(d["download"]) for d in self.browser.download_list
                   if d["download"] not in before.get("downloads", ())]
        if started:
            out["downloads"] = started
        return out

    async def click(self, job: Job, msg: dict) -> dict:
        window, tab = await self.acting(job, msg)
        button = msg.get("button") or "left"
        if button == "right":
            raise RequestError("no right clicks: a context menu would take the user's pointer")
        if button not in protocol.BUTTONS:
            raise RequestError(f"`button` must be one of {', '.join(protocol.BUTTONS)}")
        spot = await self.spot(job, tab, msg, refuse=True, cover=True)
        self.going_on(tab)
        before = self.before(tab)
        tab.note_input()
        self.watch.inject.click(self.proxy(tab), self.at(tab, spot), button,
                                bool(msg.get("double")))
        return await self.settled(job, tab, before, x=spot["x"], y=spot["y"],
                                  what=spot.get("what") or "")

    async def hover(self, job: Job, msg: dict) -> dict:
        window, tab = await self.acting(job, msg)
        spot = await self.spot(job, tab, msg, refuse=False, cover=True)
        self.going_on(tab)
        before = self.before(tab)
        self.watch.inject.move(self.proxy(tab), self.at(tab, spot))
        return await self.settled(job, tab, before, x=spot["x"], y=spot["y"],
                                  what=spot.get("what") or "")

    async def _type(self, job: Job, tab: Tab, text: str) -> tuple[int, str]:
        """`text` typed into what has focus: (characters typed, how). It
        stops early when the page opens a dialog."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        via = "keys" if len(text) <= protocol.TYPE_KEYS_MAX else "input method"
        inject, typed = self.watch.inject, 0
        for piece in re.split(r"([\n\t])", text):
            if not piece:
                continue
            if piece in ("\n", "\t") or via == "keys":
                for ch in piece:
                    inject.char(self.proxy(tab), ch)
                    typed += 1
                    if typed % TYPE_BATCH == 0:
                        await job.sleep(0)
                        self.going_on(tab)
                        if self.watch.dialog_of(tab.id) is not None:
                            return typed, via
            else:
                inject.commit(self.proxy(tab), piece)
                typed += len(piece)
            await job.sleep(0)
            self.going_on(tab)
            if self.watch.dialog_of(tab.id) is not None:
                break
        return typed, via

    def _clear(self, tab: Tab) -> None:
        """The focused field emptied as the user would: all of it
        selected, then Backspace."""
        key, mods, text = act.parse_key("ctrl+a")
        self.watch.inject.key(self.proxy(tab), key, mods, text)
        key, mods, text = act.parse_key("Backspace")
        self.watch.inject.key(self.proxy(tab), key, mods, text)

    async def _focus(self, job: Job, tab: Tab, ref: tuple) -> dict:
        done, path = await self.ask(job, tab, ref, "focusField")
        if done.get("refused"):
            raise RequestError(REFUSALS[done["refused"]](done.get("what") or "it"))
        await self.into_sight(job, tab, path, done)
        return done

    async def type(self, job: Job, msg: dict) -> dict:
        text = msg.get("text")
        if not isinstance(text, str):
            raise RequestError("`text` must be a string")
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        what = None
        if msg.get("ref") is not None and (msg.get("x") is None and msg.get("y") is None):
            what = (await self._focus(job, tab, _ref(msg))).get("what")
        else:
            spot = await self.spot(job, tab, msg, refuse=True, cover=True, need=False)
            if spot is not None:
                self.going_on(tab)
                tab.note_input()
                self.watch.inject.click(self.proxy(tab), self.at(tab, spot))
                await job.sleep(0.05)
        self.going_on(tab)
        if what is None:
            what = (await job.read(tab.page, reader.script("focused"))).get("what")
        tab.note_input()
        if msg.get("clear"):
            self._clear(tab)
        typed, via = await self._type(job, tab, text)
        if msg.get("submit") and self.watch.dialog_of(tab.id) is None:
            key, mods, ch = act.parse_key("Enter")
            self.watch.inject.key(self.proxy(tab), key, mods, ch)
        return await self.settled(job, tab, before, typed=typed, via=via, what=what or "")

    async def key(self, job: Job, msg: dict) -> dict:
        try:
            key, mods, text = act.parse_key(msg.get("key"))
        except ValueError as e:
            raise RequestError(f"`key`: {e}") from None
        count = _int(msg, "count", 1)
        if not 1 <= count <= protocol.KEY_COUNT_MAX:
            raise RequestError(f"`count` must be 1 to {protocol.KEY_COUNT_MAX}")
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        tab.note_input()
        for i in range(count):
            self.watch.inject.key(self.proxy(tab), key, mods, text)
            if i % 10 == 9:
                await job.sleep(0)
                self.going_on(tab)
                if self.watch.dialog_of(tab.id) is not None:
                    break
        return await self.settled(job, tab, before)

    async def select(self, job: Job, msg: dict) -> dict:
        ref = _ref(msg)
        value, label = msg.get("value"), msg.get("label")
        if (value is None) == (label is None):
            raise RequestError("give `value` or `label`")
        by, wanted = ("value", value) if value is not None else ("label", label)
        values = _strings(wanted, by)
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        chosen, _path = await self.ask(job, tab, ref, "choose", {"by": by, "values": values})
        return await self.settled(job, tab, before, value=chosen["value"], label=chosen["label"])

    async def scroll(self, job: Job, msg: dict) -> dict:
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        extra = {}
        if msg.get("to") is not None:
            if any(msg.get(k) is not None for k in ("dx", "dy", "ref", "x", "y")):
                raise RequestError("give `to` alone, or `dy`/`dx` with where to turn the wheel")
            done, path = await self.ask(job, tab, _ref(msg, "to"), "scrollTo")
            await self.into_sight(job, tab, path, done)
            extra["what"] = done.get("what") or ""
        else:
            dx, dy = _number(msg, "dx", 0.0), _number(msg, "dy", 0.0)
            if not dx and not dy:
                raise RequestError("give `dy` (down, px; negative is up) or `dx`, or `to`")
            spot = await self.spot(job, tab, msg, refuse=False, cover=False, need=False)
            if spot is None:
                view = await job.read(tab.page, reader.script("info"))
                spot = {"x": view["width"] / 2, "y": view["height"] / 2}
            self.going_on(tab)
            self.watch.inject.wheel(self.proxy(tab), self.at(tab, spot), dx, dy)
            # A smooth scroll: until the page stops moving.
            began, last = time.monotonic(), None
            while time.monotonic() - began < SCROLL_MAX_S:
                await job.sleep(SCROLL_POLL_S)
                now = await job.read(tab.page, reader.script("scrolled"), timeout=2.0,
                                     quiet=True)
                if now is not None and now == last and time.monotonic() - began > 0.2:
                    break
                last = now
        pos = await job.read(tab.page, reader.script("scrolled"), timeout=2.0, quiet=True) or {}
        return await self.settled(job, tab, before, scroll_x=pos.get("x"), scroll_y=pos.get("y"),
                                  **extra)

    async def fill(self, job: Job, msg: dict) -> dict:
        fields = msg.get("fields")
        if not isinstance(fields, list) or not fields or len(fields) > FILL_MAX or not all(
                isinstance(f, dict) and protocol.is_ref(f.get("ref")) and "value" in f
                for f in fields):
            raise RequestError(f"`fields` must be a list of 1 to {FILL_MAX} {{ref, value}}")
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        tab.note_input()
        done = []
        for i, field in enumerate(fields):
            ref, value = field["ref"], field["value"]
            try:
                await self._fill_one(job, tab, ref, value)
                done.append({"ref": ref, "ok": True})
            except (RequestError, reader.ScriptError) as e:
                if self.watch.paused(tab.id) or not self.alive(tab):
                    raise
                done.append({"ref": ref, "ok": False, "error": str(e)})
            if self.watch.dialog_of(tab.id) is not None:
                done += [{"ref": f["ref"], "ok": False, "error": "not filled: the page opened "
                          "a dialog"} for f in fields[i + 1:]]
                break
        return await self.settled(job, tab, before, fields=done)

    async def _fill_one(self, job: Job, tab: Tab, raw_ref, value) -> None:
        self.going_on(tab)
        parsed = protocol.parse_ref(raw_ref)
        ref = protocol.ref_name(*parsed)
        field, _path = await self.ask(job, tab, parsed, "fieldKind")
        kind = field["kind"]
        if field["disabled"]:
            raise RequestError(f"[{ref}] is disabled")
        if kind == "file":
            raise RequestError(protocol.FILE_FIELD.format(what=f"[{ref}]"))
        if kind == "text":
            if not isinstance(value, str):
                raise RequestError(f"[{ref}] is a text field: give a string")
            await self._focus(job, tab, parsed)
            self._clear(tab)
            await self._type(job, tab, value)
        elif kind in ("checkbox", "radio"):
            if not isinstance(value, bool):
                raise RequestError(f"[{ref}] is a {kind}: give true or false")
            if kind == "radio" and not value:
                raise RequestError(f"[{ref}] is a radio button: choose another one of its "
                                   "group instead of unchecking it")
            if field["checked"] != value:
                spot = await self.spot(job, tab, {"ref": raw_ref}, refuse=False, cover=True)
                self.watch.inject.click(self.proxy(tab), self.at(tab, spot))
                await job.sleep(0.05)
                now, _path = await self.ask(job, tab, parsed, "fieldKind")
                if now["checked"] != value:
                    raise RequestError(f"[{ref}] did not change when clicked")
        elif kind == "select":
            values = _strings(value, "value")
            try:
                await self.ask(job, tab, parsed, "choose", {"by": "value", "values": values})
            except reader.ScriptError:
                await self.ask(job, tab, parsed, "choose", {"by": "label", "values": values})
        elif kind == "value":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise RequestError(f"[{ref}] takes a value as text (2024-05-01, 13:30, #ff0000)")
            await self.ask(job, tab, parsed, "setValue", str(value))
        else:
            raise RequestError(f"[{ref}] is no field to fill ({field['what']})")

    async def dialog(self, job: Job, msg: dict) -> dict:
        accept, text = msg.get("accept"), msg.get("text")
        if not isinstance(accept, bool):
            raise RequestError("`accept` must be true or false")
        if text is not None and not isinstance(text, str):
            raise RequestError("`text` must be a string")
        window, tab = await self.acting(job, msg, dialog_ok=True)
        if self.watch.dialog_of(tab.id) is None:
            raise RequestError(f"no dialog is open on tab {tab.id}")
        before = self.before(tab)
        self.watch.answer(tab.id, accept, text)
        return await self.settled(job, tab, before)

    async def release(self, job: Job, msg: dict) -> dict:
        tab_id = None
        if msg.get("tab") is not None:
            tab_id = _int(msg, "tab")
            self.find(tab_id)
        return {"released": self.watch.release(tab_id)}

    # ── alone: the agent's own window (protocol.py, "Alone") ────────────
    async def borrow(self, job: Job, msg: dict) -> dict:
        tab_id = _int(msg, "tab")
        window, tab = self.find(tab_id)
        self.allowed(tab)
        if self.watch.paused(tab_id):
            raise RequestError(protocol.took_over(tab_id))
        if window.agent:
            raise RequestError(f"tab {tab_id} is in your own window already")
        if window.single:
            raise RequestError(f"tab {tab_id} is a page's popup (window {window.id}); it moves to "
                               "no other window")
        if tab.page.in_dialog:
            raise RequestError(f"tab {tab_id} has a dialog open; it moves once that is answered")
        origin = self.browser.borrow(tab)
        window, tab = self.find(tab_id)
        await self.until_ready(job, window)
        window, tab = self.find(tab_id)
        log.info("agent: tab %d borrowed from window %d into window %d", tab_id,
                 origin["window_id"], window.id)
        return {**self.tab(window, tab),
                "from": {"window": origin["window_id"], "index": origin["index"]}}

    async def return_(self, job: Job, msg: dict) -> dict:
        tab_id = _int(msg, "tab")
        window, tab = self.find(tab_id)
        if not window.agent:
            raise RequestError(f"tab {tab_id} is not in your own window: only a tab there goes "
                               "back to the user")
        if tab.page.in_dialog:
            raise RequestError(f"tab {tab_id} has a dialog open; it moves once that is answered")
        try:
            home, to = self.browser.hand_back(tab, show=bool(msg.get("show")))
        except LookupError as e:
            raise RequestError(f"{e}; tab {tab_id} stays in your window") from None
        return {**self.tab(home, tab), "to": to}

    async def show(self, job: Job, msg: dict) -> dict:
        raw = msg.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise RequestError("`path` must be a file's absolute path, or a file:// URL")
        raw = raw.strip()
        if raw.lower().startswith("file:"):
            local = QUrl(raw).toLocalFile()
        else:
            local = os.path.expanduser(raw) if raw.startswith("~/") else raw
        if not local or not os.path.isabs(local):
            raise RequestError(f"{raw!r} is not an absolute path or a file:// URL")
        if not os.path.exists(local):
            raise RequestError(f"{local} does not exist")
        if not os.path.isfile(local):
            raise RequestError(f"{local} is not a file")
        window = self.browser.main_window()
        current = window.current_tab()
        background = current is not None and self.watch.paused(current.id)
        tab = window.add_tab(QUrl.fromLocalFile(os.path.abspath(local)), background=background)
        tab.expect_load()
        return {**self.tab(window, tab), "background": background}

    # ── downloads ───────────────────────────────────────────────────────
    async def downloads(self, job: Job, msg: dict) -> dict:
        return {"downloads": [self.browser.download(d["download"])
                              for d in self.browser.download_list]}

    async def _wait_download(self, job: Job, msg: dict) -> dict:
        did = _int(msg, "download")
        timeout = _seconds(msg, "timeout", protocol.WAIT_DEFAULT_S, protocol.WAIT_MAX_S)
        if self.browser.download(did) is None:
            raise RequestError(f"no download {did} in this run of Flux (downloads lists them)")
        began = time.monotonic()
        while True:
            now = self.browser.download(did)
            if now["state"] != "running":
                return {**now, "waited": round(time.monotonic() - began, 3)}
            left = began + timeout - time.monotonic()
            if left <= 0:
                raise RequestError(f"timed out after {timeout:g} s waiting for download {did} "
                                   f"({now['percent'] or 0}% so far)")
            await job.sleep(min(POLL_S, left))

    # ── uploads ─────────────────────────────────────────────────────────
    async def upload(self, job: Job, msg: dict) -> dict:
        try:
            paths = uploads.check(msg.get("paths"))
        except ValueError as e:
            raise RequestError(str(e)) from None
        if len(paths) > protocol.UPLOAD_PATHS_MAX:
            raise RequestError(f"at most {protocol.UPLOAD_PATHS_MAX} files at once")
        ref = _ref(msg)
        window, tab = await self.acting(job, msg)
        aim, _path = await self.ask(job, tab, ref, "uploadTarget")
        if aim.get("folder"):
            raise RequestError(f"{aim['field']} takes a folder; upload gives a page files")
        if aim.get("multiple") is False and len(paths) > 1:
            raise RequestError(f"{aim['field']} takes one file; {len(paths)} were given")
        self.going_on(tab)
        before = self.before(tab)
        self.watch.arm_files(tab.id, paths)
        job.cleanups.append(partial(self.watch.disarm_files, tab.id))
        tab.note_input()
        if aim["aim"] == "script":
            # A hidden field with no label shown: a key the page may open
            # a chooser after (a modifier alone is not one), then its click.
            self.watch.inject.key(self.proxy(tab), int(Qt.Key.Key_F24))
            await job.sleep(0.05)
            self.going_on(tab)
            await self.ask(job, tab, ref, "fileClick")
        else:
            spot = await self.spot(job, tab, {"ref": aim["label"] if aim["aim"] == "label"
                                              else msg["ref"]}, refuse=False, cover=True)
            self.going_on(tab)
            self.watch.inject.click(self.proxy(tab), self.at(tab, spot))
        deadline = time.monotonic() + protocol.UPLOAD_WAIT_S
        while self.watch.took_files(tab.id) is None and time.monotonic() < deadline:
            await job.sleep(SETTLE_POLL_S)
            self.going_on(tab)
        took = self.watch.disarm_files(tab.id)
        if took is None:
            raise RequestError(protocol.NO_CHOOSER.format(what=aim["what"]))
        if took.get("error"):
            raise RequestError(took["error"])
        files = [{"name": os.path.basename(p), "size": os.path.getsize(p)} for p in took["paths"]]
        return await self.settled(job, tab, before, files=files, field=aim.get("field"),
                                  what=aim["what"])

    # ── the agent's own scripts (protocol.py "Scripts", script.py) ──────
    async def js(self, job: Job, msg: dict) -> dict:
        code = msg.get("code")
        if not isinstance(code, str) or not code.strip():
            raise RequestError("`code` must be JavaScript: one expression, or the statements of "
                               "an async function's body (`return` its answer)")
        if len(code) > protocol.JS_CODE_MAX:
            raise RequestError(f"`code` is longer than {protocol.JS_CODE_MAX} characters")
        timeout = _seconds(msg, "timeout", protocol.JS_TIMEOUT_S, protocol.JS_TIMEOUT_MAX_S)
        window, tab = await self.acting(job, msg)
        before = self.before(tab)
        tab.note_input()                # what it makes the page do is the agent's doing
        await frames.guard_all(job, tab, script.GUARD, script.WORLD)
        run = next(self.runs)
        began = time.monotonic()
        # One line is tried as an expression first; more, as statements
        # only (a line starting with "(" or "[" would join the one above).
        ways = (True, False) if "\n" not in code.strip() else (False,)
        started = None
        for expression in ways:
            guarded = await job.js(tab.page, script.GUARD, world=script.WORLD, timeout=2.0,
                                   quiet=True)
            if guarded is None:
                raise RequestError("the page did not answer (it may be busy)")
            started = await job.js(tab.page, script.source(run, code, expression=expression),
                                   world=script.WORLD, timeout=max(1.0, timeout), quiet=True)
            if started is None:
                raise RequestError(f"the script did not return within {timeout:g} s: it may loop "
                                   "without end, and the page stays stuck while it does")
            if started:
                break
        if not started:
            raise RequestError("the script did not run: it does not compile (code is one "
                               "expression, or the statements of an async function's body, "
                               "`return` giving the answer)")
        job.cleanups.append(partial(self._drop_run, tab, run))
        answer, pending = None, False
        while True:
            self.going_on(tab)
            if self.watch.dialog_of(tab.id) is not None:
                pending = True          # the code waits for the dialog as the page does
                break
            raw = await job.js(tab.page, script.poll(run), world=script.WORLD, timeout=2.0,
                               quiet=True)
            state = None
            if raw:
                try:
                    state = json.loads(raw)
                except ValueError:
                    state = None
            if isinstance(state, dict) and state.get("gone"):
                pending = True          # the page went on to another document first
                break
            if isinstance(state, dict) and state.get("done"):
                answer = state
                break
            if time.monotonic() - began >= timeout:
                pending = True
                break
            await job.sleep(script.POLL_S)
        out: dict = {"result": None, "result_type": None, "length": 0, "cut": False}
        if pending:
            out["pending"] = True
        elif answer.get("ok"):
            text = await frames.scrub(job, tab, str(answer.get("json") or "null"), self.kept)
            if text is None:
                out["withheld"] = True  # not checked for passwords: not given
            else:
                length = int(answer.get("length") or len(text))
                out.update(result=text[:protocol.JS_RESULT_MAX], result_type=answer.get("type"),
                           length=length, cut=length > protocol.JS_RESULT_MAX
                           or len(text) > protocol.JS_RESULT_MAX)
        else:
            said = await frames.scrub(job, tab, str(answer.get("error") or "an error"), self.kept)
            if said is None:
                out["withheld"] = True
            out["threw"] = said[:2000] if said is not None else "(its words withheld)"
        log.info("agent: script %d on tab %d %s", run, tab.id,
                 "still running" if pending else "threw" if "threw" in out else
                 "answered, withheld (not checked for passwords)" if out.get("withheld") else
                 f"answered ({out['length']} characters)")
        return await self.settled(job, tab, before, **out)

    def _drop_run(self, tab: Tab, run: int) -> None:
        if shiboken6.isValid(tab) and shiboken6.isValid(tab.page):
            tab.page.runJavaScript(script.drop(run), script.WORLD)
