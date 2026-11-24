"""The agent's line to Jack: the control socket's client.

Pure Python on asyncio (no PySide6, no xcffib, no skia), so the daemon
imports it. Jack listens at `protocol.control_socket_path()`, one socket
per browser process; protocol.py documents every request and result.

    jack = await control.ensure_running()       # or connect(), which never starts it
    windows = await jack.tabs()
    tab = await jack.open("https://example.org")
    await jack.wait(tab.tab)
    page = await jack.read(tab.tab, "outline")
    await jack.close()

A `Control` is one connection; requests on it run side by side. A
request that Jack refuses raises `ControlError` with Jack's words (the
agent can read them: "tab 7 is not showing in its window; switch to it
first"). One that runs past its `timeout`, or whose task is cancelled,
is cancelled in Jack too. `NotRunning`: no browser listens.

`ensure_running()` starts Jack when none runs, the way the launcher
starts apps (services/proc.py `launch`: detached, in a scope of its
own), as `<the CLI> browser --no-window` (desktop.py's command): no
window until a request opens one. A Jack that is starting meanwhile, the
user's or another start's, is the one it then connects to.

The acting requests (`click`, `type_text`, `key`, `select`, `scroll`,
`fill`, `hover`, `dialog`, `upload`, `release`) carry the agent's
`voice`, which Jack draws its hold on the tab in (protocol.VOICES). On a
tab the user took over they raise ControlError with protocol.TOOK_OVER's
words.

The agent's own window (protocol.py, "Alone"): `open(..., window="agent",
voice=...)`, `borrow`, `return_tab`; `show` puts a local file in the
user's window; `downloads` and `wait_download`.

`look()` is for the call: what the user is looking at in Jack, as
content blocks to send with their words, or nothing when Jack is not
running or no Jack window has had focus. It never starts Jack and
changes nothing on screen.

`shared()` keeps one connection open for the hands (hands.py), made
again when it dropped.
"""

import asyncio
import itertools
import logging
import weakref
from dataclasses import dataclass, field

from indigoshell.browser import protocol

log = logging.getLogger(__name__)

LINE_LIMIT = 64 << 20           # bytes: a result line (a screenshot is a few MB)
CONNECT_S = 2.0
START_S = 30.0                  # a cold QtWebEngine start, with room
REQUEST_S = 30.0
LOOK_S = 3.0
SITE_WORDS = "Page content from {host} (written by the site, not by the user):"


class ControlError(Exception):
    """A request Jack refused (its words), or the line to it failing."""


class NotRunning(ControlError):
    """No browser listens at the control socket."""


@dataclass(frozen=True)
class TabInfo:
    tab: int
    window: int
    index: int
    title: str
    url: str
    loading: bool
    current: bool
    agent: str | None = None            # None, "driving", "paused" (the user took over)
    voice: str | None = None
    dialog: dict | None = field(default=None, compare=False)   # held for the agent
    borrowed: dict | None = field(default=None, compare=False)  # where it came from
    kept: bool = False                  # at a host the user keeps from the agent

    @classmethod
    def of(cls, d: dict) -> "TabInfo":
        return cls(int(d["tab"]), int(d["window"]), int(d["index"]), str(d.get("title") or ""),
                   str(d.get("url") or ""), bool(d.get("loading")), bool(d.get("current")),
                   d.get("agent"), d.get("voice"), d.get("dialog"), d.get("borrowed"),
                   bool(d.get("kept")))


@dataclass(frozen=True)
class WindowInfo:
    window: int
    xid: int | None
    single: bool
    focused: bool
    tabs: tuple[TabInfo, ...]
    agent: str | None = None            # the voice whose own window it is

    @classmethod
    def of(cls, d: dict) -> "WindowInfo":
        return cls(int(d["window"]), d.get("xid"), bool(d.get("single")),
                   bool(d.get("focused")), tuple(TabInfo.of(t) for t in d.get("tabs") or ()),
                   d.get("agent"))

    def current(self) -> TabInfo | None:
        return next((t for t in self.tabs if t.current), None)


class Control:
    """One connection to Jack."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> "Control":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._writer.close()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):     # noqa: BLE001
            pass

    # ── the line ────────────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        why = "Jack closed the line"
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                msg = protocol.decode(line)
                if msg is None or msg[protocol.TYPE] != protocol.RESULT:
                    continue
                future = self._pending.get(msg.get(protocol.ID))
                if future is not None and not future.done():
                    future.set_result(msg)
        except (OSError, ValueError, asyncio.LimitOverrunError) as e:
            why = f"the line to Jack failed: {e}"
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ControlError(why))

    def _send(self, msg: dict) -> None:
        if self._closed:
            raise ControlError("the line to Jack is closed")
        self._writer.write(protocol.encode(msg))

    def _cancel(self, rid: int) -> None:
        if not self._closed:
            try:
                self._writer.write(protocol.encode(protocol.message(protocol.CANCEL,
                                                                    **{protocol.ID: rid})))
            except (OSError, RuntimeError):
                pass

    async def request(self, type_: str, args: dict | None = None, *,
                      within: float = REQUEST_S) -> dict:
        """One request (arguments that are None left out), answered
        within `within` seconds or cancelled; the result's fields."""
        rid = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            self._send(protocol.request(type_, rid, **{k: v for k, v in (args or {}).items()
                                                      if v is not None}))
            msg = await asyncio.wait_for(future, within)
        except asyncio.TimeoutError:
            self._cancel(rid)
            raise ControlError(f"Jack did not answer {type_} within {within:g} s") from None
        except asyncio.CancelledError:
            self._cancel(rid)
            raise
        finally:
            self._pending.pop(rid, None)
        if not msg.get("ok"):
            raise ControlError(str(msg.get("error") or f"{type_} failed"))
        return {k: v for k, v in msg.items() if k not in (protocol.TYPE, protocol.ID, "ok")}

    # ── the requests ────────────────────────────────────────────────────
    # `within`: how long this side waits for the answer.
    async def tabs(self, *, within: float = REQUEST_S) -> list[WindowInfo]:
        result = await self.request("tabs", within=within)
        return [WindowInfo.of(w) for w in result.get("windows") or ()]

    async def open(self, url: str, *, window: int | str | None = None, background: bool = False,
                   voice: str | None = None, within: float = REQUEST_S) -> TabInfo:
        """`window`: an id, or protocol.AGENT_WINDOW for the agent's own
        (made and placed first when there is none: `voice`'s)."""
        return TabInfo.of(await self.request(
            "open", {"url": url, "window": window, "background": background or None,
                     "voice": voice}, within=within))

    async def switch(self, tab: int, *, within: float = REQUEST_S) -> TabInfo:
        return TabInfo.of(await self.request("switch", {"tab": tab}, within=within))

    async def close_tab(self, tab: int, *, within: float = REQUEST_S) -> dict:
        """{"tab", "window_closed"}."""
        return await self.request("close", {"tab": tab}, within=within)

    async def go(self, tab: int, url: str | None = None, *, action: str | None = None,
                 within: float = REQUEST_S) -> TabInfo:
        return TabInfo.of(await self.request("go", {"tab": tab, "url": url, "action": action},
                                             within=within))

    async def wait(self, tab: int, *, selector: str | None = None, text: str | None = None,
                   timeout: float = protocol.WAIT_DEFAULT_S) -> dict:
        """TAB + {"waited"}. `timeout` is Jack's bound (it answers "timed
        out" then); this side waits a few seconds longer for that."""
        return await self.request("wait", {"tab": tab, "selector": selector, "text": text,
                                           "timeout": float(timeout)},
                                  within=float(timeout) + 5.0)

    async def read(self, tab: int, what: str, *, offset: int = 0, length: int | None = None,
                   hidden: bool = False, within: float = REQUEST_S) -> dict:
        return await self.request("read", {"tab": tab, "what": what, "offset": offset or None,
                                           "length": length, "hidden": hidden or None},
                                  within=within)

    async def screenshot(self, tab: int, *, within: float = REQUEST_S) -> dict:
        """TAB + {"png" (base64), "width", "height", "source_width", "source_height"}."""
        return await self.request("screenshot", {"tab": tab}, within=within)

    async def look(self, *, window: int | None = None, xid: int | None = None,
                   screenshot: bool = False, image: bool = False,
                   within: float = LOOK_S) -> dict:
        return await self.request("look", {"window": window, "xid": xid,
                                           "screenshot": screenshot or None,
                                           "image": image or None}, within=within)

    # ── acting (protocol.py, "Acting"): TAB + {"navigated", "opened",
    # "dialog", ...} each; `voice` is the agent's ──────────────────────────
    async def click(self, tab: int, ref: int | None = None, *, x: float | None = None,
                    y: float | None = None, button: str | None = None, double: bool = False,
                    voice: str | None = None, within: float = REQUEST_S) -> dict:
        return await self.request("click", {"tab": tab, "ref": ref, "x": x, "y": y,
                                            "button": button, "double": double or None,
                                            "voice": voice}, within=within)

    async def type_text(self, tab: int, text: str, *, ref: int | None = None,
                        x: float | None = None, y: float | None = None, clear: bool = False,
                        submit: bool = False, voice: str | None = None,
                        within: float = REQUEST_S) -> dict:
        return await self.request("type", {"tab": tab, "text": text, "ref": ref, "x": x, "y": y,
                                           "clear": clear or None, "submit": submit or None,
                                           "voice": voice}, within=within)

    async def key(self, tab: int, key: str, *, count: int | None = None,
                  voice: str | None = None, within: float = REQUEST_S) -> dict:
        return await self.request("key", {"tab": tab, "key": key, "count": count,
                                          "voice": voice}, within=within)

    async def select(self, tab: int, ref: int, *, value=None, label=None,
                     voice: str | None = None, within: float = REQUEST_S) -> dict:
        return await self.request("select", {"tab": tab, "ref": ref, "value": value,
                                             "label": label, "voice": voice}, within=within)

    async def scroll(self, tab: int, *, dy: float | None = None, dx: float | None = None,
                     ref: int | None = None, x: float | None = None, y: float | None = None,
                     to: int | None = None, voice: str | None = None,
                     within: float = REQUEST_S) -> dict:
        return await self.request("scroll", {"tab": tab, "dy": dy, "dx": dx, "ref": ref, "x": x,
                                             "y": y, "to": to, "voice": voice}, within=within)

    async def fill(self, tab: int, fields: list[dict], *, voice: str | None = None,
                   within: float = REQUEST_S) -> dict:
        return await self.request("fill", {"tab": tab, "fields": fields, "voice": voice},
                                  within=within)

    async def hover(self, tab: int, ref: int | None = None, *, x: float | None = None,
                    y: float | None = None, voice: str | None = None,
                    within: float = REQUEST_S) -> dict:
        return await self.request("hover", {"tab": tab, "ref": ref, "x": x, "y": y,
                                            "voice": voice}, within=within)

    async def dialog(self, tab: int, accept: bool, *, text: str | None = None,
                     voice: str | None = None, within: float = REQUEST_S) -> dict:
        return await self.request("dialog", {"tab": tab, "accept": bool(accept), "text": text,
                                             "voice": voice}, within=within)

    async def release(self, tab: int | None = None, *, within: float = REQUEST_S) -> list[int]:
        """The tabs let go of."""
        return (await self.request("release", {"tab": tab}, within=within))["released"]

    async def upload(self, tab: int, ref: int, paths: list[str], *, voice: str | None = None,
                     within: float = REQUEST_S) -> dict:
        """TAB + {"navigated", "opened", "dialog", "files", "field", "what"}."""
        return await self.request("upload", {"tab": tab, "ref": ref, "paths": list(paths),
                                             "voice": voice}, within=within)

    # ── alone, show, downloads (protocol.py) ────────────────────────────
    async def borrow(self, tab: int, *, voice: str | None = None,
                     within: float = REQUEST_S) -> dict:
        """TAB (in the agent's window) + {"from": {"window", "index"}}."""
        return await self.request("borrow", {"tab": tab, "voice": voice}, within=within)

    async def return_tab(self, tab: int, *, show: bool = False,
                         within: float = REQUEST_S) -> dict:
        """TAB (in the user's window) + {"to": "its window" | "another window"}."""
        return await self.request("return", {"tab": tab, "show": show or None}, within=within)

    async def show(self, path: str, *, within: float = REQUEST_S) -> dict:
        """TAB + {"background"}."""
        return await self.request("show", {"path": path}, within=within)

    async def downloads(self, *, within: float = REQUEST_S) -> list[dict]:
        return (await self.request("downloads", within=within))["downloads"]

    async def wait_download(self, download: int, *,
                            timeout: float = protocol.WAIT_DEFAULT_S) -> dict:
        """DOWNLOAD + {"waited"}, once it finished (done, failed or
        cancelled); Jack's own bound is `timeout`."""
        return await self.request("wait", {"download": download, "timeout": float(timeout)},
                                  within=float(timeout) + 5.0)


# ── finding Jack ────────────────────────────────────────────────────────
async def connect(path: str | None = None, timeout: float = CONNECT_S) -> Control:
    """A connection to the running Jack; NotRunning when none listens."""
    path = path or protocol.control_socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=LINE_LIMIT), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise NotRunning(f"Jack is not running ({e.__class__.__name__}: {e})") from None
    return Control(reader, writer)


def start_command() -> list[str]:
    """How `ensure_running` starts Jack: the launcher entry's command
    (desktop.py), with no window."""
    from indigoshell.browser import desktop
    return [*desktop.exec_command(), "browser", "--no-window"]


def _launch() -> None:
    from indigoshell.browser import desktop
    from indigoshell.services import proc
    proc.launch(start_command(), app_id=desktop.DESKTOP_ID.removesuffix(".desktop"))


async def ensure_running(timeout: float = START_S) -> Control:
    """A connection to Jack, started first (with no window) when it is
    not running. Its socket is the default one (`<APP>_BROWSER_SOCKET`
    when set, which the started browser inherits)."""
    try:
        return await connect()
    except NotRunning:
        pass
    log.info("Jack is not running; starting it")
    _launch()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        await asyncio.sleep(0.1)
        try:
            return await connect()
        except NotRunning:
            if loop.time() >= deadline:
                from indigoshell.core.paths import state_dir
                raise NotRunning(f"Jack did not start within {timeout:g} s (its log is "
                                 f"browser.log in {state_dir()})") from None


# Per event loop: [a lock, the connection or None].
_shared: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, list]" = \
    weakref.WeakKeyDictionary()


async def shared(*, start: bool = True) -> Control:
    """One connection kept for this event loop's requests; made again
    (and Jack started, with `start`) when it dropped."""
    loop = asyncio.get_running_loop()
    entry = _shared.get(loop)
    if entry is None:
        entry = _shared[loop] = [asyncio.Lock(), None]
    async with entry[0]:
        if entry[1] is None or entry[1].closed:
            entry[1] = await (ensure_running() if start else connect())
        return entry[1]


# ── look, for the call ──────────────────────────────────────────────────
def site_words(url: str) -> str:
    """The line before anything a page wrote."""
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        host = url
    return SITE_WORDS.format(host=host or "the page")


def image_block(png_b64: str) -> dict:
    return {"type": "image", "data": png_b64, "mime": "image/png"}


def look_blocks(result: dict) -> list[dict]:
    """A `look` result as content blocks: a text block (the window, the
    tab, the selection marked as the site's words), then the screenshot
    and the image when there are."""
    focus = ("the window has focus" if result.get("focused") else
             f"the window last had focus {result['unfocused_for']:.0f} s ago"
             if result.get("unfocused_for") is not None else "the window does not have focus")
    lines = [f"In Jack, the user's browser ({focus}): tab {result.get('tab')} of window "
             f"{result.get('window')}, \"{result.get('title') or ''}\" at {result.get('url')}"]
    selection = result.get("selection") or ""
    if selection:
        where = {"field": " (in a text field)", "frame": " (in a frame)"}.get(
            result.get("selection_in") or "", "")
        lines += ["", f"The user has selected this text{where}. "
                  + site_words(result.get("url") or ""), selection]
    blocks = [{"type": "text", "text": "\n".join(lines)}]
    shot = result.get("screenshot")
    if shot:
        blocks.append({"type": "text", "text": f"The tab as shown ({shot['source_width']}x"
                       f"{shot['source_height']}, sent at {shot['width']}x{shot['height']}):"})
        blocks.append(image_block(shot["png"]))
    image = result.get("image")
    if image:
        what = "in the selection" if image.get("under") == "selection" else "under the mouse pointer"
        about = ", ".join(x for x in (f"alt \"{image['alt']}\"" if image.get("alt") else "",
                                      image.get("url") or "") if x)
        shown = (", as shown on screen" + (" (tinted: it is selected)"
                                            if image.get("under") == "selection" else "")
                 if image.get("from") == "screen" else "")
        blocks.append({"type": "text", "text": f"The image {what}"
                       + (f" ({about})" if about else "") + shown + ":"})
        blocks.append(image_block(image["png"]))
    return blocks


async def look(*, screenshot: bool = False, image: bool = False, xid: int | None = None,
               timeout: float = LOOK_S) -> list[dict]:
    """What the user is looking at in Jack, as content blocks for the
    call to send with their words; [] when Jack is not running, no Jack
    window has had focus, or it does not answer within `timeout`.
    Never starts Jack; changes nothing on screen.

    `xid`: the X window the call saw focused, to look at that Jack
    window (the focused one, else the one focused last, without it).
    `screenshot`: the tab as shown; `image`: the image in the user's
    selection, else under the mouse pointer."""
    try:
        control = await connect(timeout=min(timeout, CONNECT_S))
    except NotRunning:
        return []
    try:
        result = await control.look(xid=xid, screenshot=screenshot, image=image,
                                    within=timeout)
    except ControlError as e:
        log.debug("look: %s", e)
        return []
    finally:
        await control.close()
    return look_blocks(result)
