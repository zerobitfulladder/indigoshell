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
none runs and can start one (browser/control.py, `ensure_running`). A
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

Every request is logged at info: its type, the tab and the host, never
what the page holds, never what the agent types.

Qt: nothing here is newer than 6.8 (the Pi's Debian). A selection in
an iframe is read with QWebEngineFrame (6.8); with an older Qt that
part is skipped.
"""

import logging
import os
import re
import time
from urllib.parse import urlsplit

import shiboken6
from PySide6.QtCore import QObject, QPoint, QPointF, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from indigoshell.browser import protocol

from . import act, reader
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
           quiet: bool = False) -> _Await:
        """`code` run in the agent's world of `target` (a page or a frame);
        its answer (reader.answer). `quiet`: no answer in time is None,
        not an error."""
        def start(done) -> None:
            target.runJavaScript(code, reader.WORLD, done)

        def expire():
            if quiet:
                return None, None
            return None, RequestError(f"the page did not answer within {timeout:g} s")
        return self._await(start, timeout, expire)

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
        job.start(getattr(requests, kind)(job, msg))

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


def _voice(msg: dict) -> str | None:
    value = msg.get("voice")
    return value if isinstance(value, str) and value.strip() else None


def _strings(value, key: str) -> list[str]:
    """A string, or a list of strings, as a list."""
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(v, str) for v in values):
        raise RequestError(f"`{key}` must be a string or a list of strings")
    return values


REFUSALS = {
    "upload": lambda what: protocol.UPLOADS,
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

    # ── finding windows and tabs ────────────────────────────────────────
    def windows(self) -> list:
        return [w for w in self.browser.windows if shiboken6.isValid(w) and w.tabs]

    def window(self, wid: int):
        for w in self.windows():
            if w.id == wid:
                return w
        raise RequestError(f"no window {wid}")

    def focused(self):
        active = QApplication.activeWindow()
        return active if active is not None and active in self.windows() else None

    def find(self, tab_id: int) -> tuple:
        for w in self.windows():
            for tab in w.tabs:
                if tab.id == tab_id:
                    return w, tab
        raise RequestError(f"no tab {tab_id}")

    def showing(self, tab_id: int) -> tuple:
        """The tab, which must be the current one of its window and not
        taken over by the user."""
        window, tab = self.find(tab_id)
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
        return {"tab": tab.id, "window": window.id, "index": window.tabs.index(tab),
                "title": tab.page.title(), "url": tab.url().toString(),
                "loading": tab.busy(), "current": window.current_tab() is tab,
                **self.watch.about(tab.id)}

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
        if kind == "read" and msg.get("what") in protocol.READS:
            parts.append(msg["what"])
        log.info("agent: %s%s", " ".join(parts), f" ({host})" if host else "")

    # ── the requests ────────────────────────────────────────────────────
    async def tabs(self, job: Job, msg: dict) -> dict:
        focused = self.focused()
        out = []
        for w in self.windows():
            out.append({"window": w.id, "xid": int(w.internalWinId()) or None,
                        "single": w.single, "focused": w is focused,
                        "tabs": [self.tab(w, t) for t in w.tabs]})
        return {"windows": out, "focused": focused.id if focused is not None else None}

    async def open(self, job: Job, msg: dict) -> dict:
        url = msg.get("url")
        if not isinstance(url, str) or not url.strip():
            raise RequestError("`url` must be an address (or a search)")
        if msg.get("window") is not None:
            window = self.window(_int(msg, "window"))
            if window.single:
                raise RequestError(f"window {window.id} is a page's popup; it takes no tabs")
        else:
            window = self.browser.main_window()
        if not msg.get("background"):
            self.not_hidden(window)
        tab = window.add_tab(to_url(url.strip()), background=bool(msg.get("background")))
        tab.expect_load()
        return self.tab(window, tab)

    async def switch(self, job: Job, msg: dict) -> dict:
        window, tab = self.find(_int(msg, "tab"))
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
                        "outline": (protocol.OUTLINE_LENGTH, protocol.OUTLINE_MAX)}[what]
        offset = _int(msg, "offset", 0)
        length = _int(msg, "length", default)
        if offset < 0 or length < 1:
            raise RequestError("`offset` must be 0 or more and `length` 1 or more")
        value = await job.read(tab.page, reader.script(
            what, {"offset": offset, "length": min(length, top),
                   "hidden": bool(msg.get("hidden"))}))
        return {**self.tab(window, tab), "what": what, **value}

    async def screenshot(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        if self.watch.dialog_of(tab.id) is None:    # else the page paints nothing new
            await self._fresh_frame(job, tab)
        window, tab = self.showing(tab.id)
        view = tab.view
        zoom = tab.page.zoomFactor() or 1.0
        return {**self.tab(window, tab), **reader.png(reader.grab(view)),
                "viewport_width": round(view.width() / zoom),
                "viewport_height": round(view.height() / zoom)}

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
            painted = await job.read(tab.page, reader.expression("window.__jack.painted"),
                                     timeout=FRAME_TIMEOUT_S, quiet=True)
            if painted is None or painted >= n:
                break
            await job.sleep(FRAME_POLL_S)
        await job.sleep(FRAME_POLL_S)   # the frame from the renderer to the view

    async def look(self, job: Job, msg: dict) -> dict:
        window = self._look_window(msg)
        tab = window.current_tab()
        focused = self.focused() is window
        out = {**self.tab(window, tab), "focused": focused,
               "unfocused_for": None if focused or window.unfocused_at is None
               else round(time.monotonic() - window.unfocused_at, 3)}
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
        if msg.get("window") is not None:
            return self.window(_int(msg, "window"))
        if msg.get("xid") is not None:
            xid = _int(msg, "xid")
            for w in self.windows():
                if int(w.internalWinId()) == xid:
                    return w
            raise RequestError(f"no browser window is X window {xid}")
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
    def acting(self, job: Job, msg: dict, *, dialog_ok: bool = False) -> tuple:
        """The tab an acting request is for, made the agent's (watch.py)."""
        window, tab = self.showing(_int(msg, "tab"))
        if not dialog_ok:
            self.no_dialog(tab)
        self.watch.begin(job, window, tab, _voice(msg))
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

    async def spot(self, job: Job, tab: Tab, msg: dict, *, refuse: bool, cover: bool,
                   need: bool = True) -> dict | None:
        """Where the request points: its `ref`, or `x` and `y` (reader.js
        `point`). None when it names neither and `need` is false."""
        ref, x, y = msg.get("ref"), msg.get("x"), msg.get("y")
        if ref is not None and (x is not None or y is not None):
            raise RequestError("give `ref`, or `x` and `y`, not both")
        if ref is not None:
            where = {"ref": _int(msg, "ref")}
        elif x is not None or y is not None:
            where = {"x": _number(msg, "x"), "y": _number(msg, "y")}
            if where["x"] is None or where["y"] is None:
                raise RequestError("give both `x` and `y` (CSS px of the visible page)")
        elif need:
            raise RequestError("give `ref` (from read) or `x` and `y`")
        else:
            return None
        spot = await job.read(tab.page, reader.script("point", {**where, "refuse": refuse,
                                                                "cover": cover}))
        if spot.get("refused"):
            raise RequestError(REFUSALS[spot["refused"]](spot.get("what") or "it"))
        if spot.get("covered"):
            named = f"[{where['ref']}]" if "ref" in where else "the point"
            raise RequestError(f"{named} is covered at ({spot['x']}, {spot['y']}) by another "
                               f"element; nothing done. What covers it: {spot['covered']}")
        return spot

    def before(self, tab: Tab) -> dict:
        return {"loads": tab.loads, "navigations": tab.navigations,
                "tabs": {t.id for w in self.windows() for t in w.tabs}}

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
        return out

    async def click(self, job: Job, msg: dict) -> dict:
        window, tab = self.acting(job, msg)
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
        window, tab = self.acting(job, msg)
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

    async def _focus(self, job: Job, tab: Tab, ref: int) -> dict:
        done = await job.read(tab.page, reader.script("focusField", ref))
        if done.get("refused"):
            raise RequestError(REFUSALS[done["refused"]](done.get("what") or "it"))
        return done

    async def type(self, job: Job, msg: dict) -> dict:
        text = msg.get("text")
        if not isinstance(text, str):
            raise RequestError("`text` must be a string")
        window, tab = self.acting(job, msg)
        before = self.before(tab)
        what = None
        if msg.get("ref") is not None and (msg.get("x") is None and msg.get("y") is None):
            what = (await self._focus(job, tab, _int(msg, "ref"))).get("what")
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
        window, tab = self.acting(job, msg)
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
        ref = _int(msg, "ref")
        value, label = msg.get("value"), msg.get("label")
        if (value is None) == (label is None):
            raise RequestError("give `value` or `label`")
        by, wanted = ("value", value) if value is not None else ("label", label)
        values = _strings(wanted, by)
        window, tab = self.acting(job, msg)
        before = self.before(tab)
        chosen = await job.read(tab.page, reader.script("choose", ref,
                                                        {"by": by, "values": values}))
        return await self.settled(job, tab, before, value=chosen["value"], label=chosen["label"])

    async def scroll(self, job: Job, msg: dict) -> dict:
        window, tab = self.acting(job, msg)
        before = self.before(tab)
        extra = {}
        if msg.get("to") is not None:
            if any(msg.get(k) is not None for k in ("dx", "dy", "ref", "x", "y")):
                raise RequestError("give `to` alone, or `dy`/`dx` with where to turn the wheel")
            extra["what"] = (await job.read(tab.page, reader.script(
                "scrollTo", _int(msg, "to")))).get("what") or ""
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
                isinstance(f, dict) and protocol.is_id(f.get("ref")) and "value" in f
                for f in fields):
            raise RequestError(f"`fields` must be a list of 1 to {FILL_MAX} {{ref, value}}")
        window, tab = self.acting(job, msg)
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

    async def _fill_one(self, job: Job, tab: Tab, ref: int, value) -> None:
        self.going_on(tab)
        field = await job.read(tab.page, reader.script("fieldKind", ref))
        kind = field["kind"]
        if field["disabled"]:
            raise RequestError(f"[{ref}] is disabled")
        if kind == "file":
            raise RequestError(protocol.UPLOADS)
        if kind == "text":
            if not isinstance(value, str):
                raise RequestError(f"[{ref}] is a text field: give a string")
            await self._focus(job, tab, ref)
            self._clear(tab)
            await self._type(job, tab, value)
        elif kind in ("checkbox", "radio"):
            if not isinstance(value, bool):
                raise RequestError(f"[{ref}] is a {kind}: give true or false")
            if kind == "radio" and not value:
                raise RequestError(f"[{ref}] is a radio button: choose another one of its "
                                   "group instead of unchecking it")
            if field["checked"] != value:
                spot = await self.spot(job, tab, {"ref": ref}, refuse=False, cover=True)
                self.watch.inject.click(self.proxy(tab), self.at(tab, spot))
                await job.sleep(0.05)
                now = await job.read(tab.page, reader.script("fieldKind", ref))
                if now["checked"] != value:
                    raise RequestError(f"[{ref}] did not change when clicked")
        elif kind == "select":
            values = _strings(value, "value")
            try:
                await job.read(tab.page, reader.script("choose", ref,
                                                       {"by": "value", "values": values}))
            except reader.ScriptError:
                await job.read(tab.page, reader.script("choose", ref,
                                                       {"by": "label", "values": values}))
        elif kind == "value":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise RequestError(f"[{ref}] takes a value as text (2024-05-01, 13:30, #ff0000)")
            await job.read(tab.page, reader.script("setValue", ref, str(value)))
        else:
            raise RequestError(f"[{ref}] is no field to fill ({field['what']})")

    async def dialog(self, job: Job, msg: dict) -> dict:
        accept, text = msg.get("accept"), msg.get("text")
        if not isinstance(accept, bool):
            raise RequestError("`accept` must be true or false")
        if text is not None and not isinstance(text, str):
            raise RequestError("`text` must be a string")
        window, tab = self.acting(job, msg, dialog_ok=True)
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
