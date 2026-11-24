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

Every request is logged at info: its type, the tab and the host, never
what the page holds.

Qt: nothing here is newer than 6.8 (the Pi's Debian). A selection in
an iframe is read with QWebEngineFrame (6.8); with an older Qt that
part is skipped.
"""

import logging
import os
import time
from urllib.parse import urlsplit

import shiboken6
from PySide6.QtCore import QObject, QPoint, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from indigoshell.browser import protocol

from . import reader
from .tabs import Tab, to_url

log = logging.getLogger(__name__)

LINE_MAX = 1 << 20              # bytes: a longer request line ends the connection
JS_TIMEOUT_S = 10.0             # a script in the page that never answers
POLL_S = 0.15                   # wait's checks
FRAME_POLL_S = 0.016
FRAME_TIMEOUT_S = 1.0           # a page that paints no frame: grabbed anyway
FRAMES_MAX = 50                 # frames asked for a selection, at most


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
        window, tab = self.find(tab_id)
        if window.current_tab() is not tab:
            raise RequestError(protocol.not_showing(tab_id))
        return window, tab

    def tab(self, window, tab: Tab) -> dict:
        return {"tab": tab.id, "window": window.id, "index": window.tabs.index(tab),
                "title": tab.page.title(), "url": tab.url().toString(),
                "loading": tab.busy(), "current": window.current_tab() is tab}

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
        tab = window.add_tab(to_url(url.strip()), background=bool(msg.get("background")))
        tab.expect_load()
        return self.tab(window, tab)

    async def switch(self, job: Job, msg: dict) -> dict:
        window, tab = self.find(_int(msg, "tab"))
        if window.current_tab() is not tab:
            window.select(window.tabs.index(tab))     # never raised, never focused
        return self.tab(window, tab)

    async def close(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
        last = len(window.tabs) == 1
        window.close_tab(tab)
        return {"tab": tab.id, "window_closed": last}

    async def go(self, job: Job, msg: dict) -> dict:
        window, tab = self.showing(_int(msg, "tab"))
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
                raise RequestError(f"tab {tab_id} stopped showing while waiting ({e})") from None
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
        await self._fresh_frame(job, tab)
        window, tab = self.showing(tab.id)
        return {**self.tab(window, tab), **reader.png(reader.grab(tab.view))}

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
        sel = await job.read(tab.page, reader.script("selection",
                                                     {"image": bool(msg.get("image"))}),
                             timeout=3.0, quiet=True) or {}
        text, where = sel.get("text") or "", sel.get("in")
        if where == "frame":
            text = await self._frame_selection(job, tab)
            where = "frame" if text else None
        out["selection"], out["selection_in"] = text, (where if text else None)
        if msg.get("screenshot") or msg.get("image"):
            await self._fresh_frame(job, tab)
            if not shiboken6.isValid(tab.view):
                raise RequestError("the tab went away")
            image = reader.grab(tab.view)
            if msg.get("screenshot"):
                out["screenshot"] = reader.png(image)
            if msg.get("image"):
                out["image"] = await self._image(job, tab, image, sel.get("image"))
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
