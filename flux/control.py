"""The agent's line to Flux: the control socket's client.

Pure Python on asyncio (no PySide6, no xcffib, no skia), so the daemon
imports it. Flux listens at `protocol.control_socket_path()`, one socket
per browser process; protocol.py documents every request and result.

    flux = await control.ensure_running()       # or connect(), which never starts it
    windows = await flux.tabs()
    tab = await flux.open("https://example.org")
    await flux.wait(tab.tab)
    page = await flux.read(tab.tab, "outline")
    await flux.close()

A `Control` is one connection; requests on it run side by side. A
request that Flux refuses raises `ControlError` with Flux's words (the
agent can read them: "tab 7 is not showing in its window; switch to it
first"). One that runs past its `timeout`, or whose task is cancelled,
is cancelled in Flux too. `NotRunning`: no browser listens.

`ensure_running()` starts Flux when none runs, the way the launcher
starts apps (services/proc.py `launch`: detached with `setsid`, in a
systemd scope of its own where a user manager runs, plainly where none
does), as `<the CLI> browser --no-window` (desktop.py's command: this
Python and the repository's main.py, found at run time, unless the CLI
on PATH is what runs): no window until a request opens one. A Flux that
is starting meanwhile, the user's or another start's, is the one it then
connects to. Where there is no display (no $DISPLAY, no
$WAYLAND_DISPLAY) it starts it `--headless`, unless told otherwise. A
caller whose own Python has no PySide6 (a hub's virtual environment)
names one that has, `python=` or `$<APP>_BROWSER_PYTHON`
(start_command). `quit()` ends the Flux it is connected to, for a
process done with a Flux it started (protocol.py, "Quit").

Profiles (profiles.py): every function that finds Flux takes `profile`,
the name of the browser to talk to (its own socket), None for the
default one; `ensure_running` starts that profile's.

The acting requests (`click`, `type_text`, `key`, `select`, `scroll`,
`fill`, `hover`, `dialog`, `upload`, `js`, `release`) put the agent's
hold on the tab, which Flux draws in her colour (core/persona.py). On a
tab the user took over they raise ControlError with protocol.TOOK_OVER's
words. A ref is an int, or "f2:14" for an element in frame f2
(protocol.py, "Frames"). `js` runs the agent's own JavaScript behind
Flux's guard (protocol.py, "Scripts"); `screenshot(..., full=True)` gives
the page as tiles.

The agent's own window (protocol.py, "Alone"): `open(..., window="agent")`,
`borrow`, `return_tab`; `show` puts a local file in the
user's window; `downloads` and `wait_download`.

`look()` is for the call: what the user is looking at in Flux, as
content blocks to send with their words, or nothing when Flux is not
running or no Flux window has had focus. It never starts Flux and
changes nothing on screen.

`shared()` keeps one connection open for the hands (hands.py), per
profile, made again when it dropped.

Pure means more than no Qt here: this module and hands.py import on a
machine with PySide6 and none of the shell's own dependencies (xcffib,
skia, dbus-fast, psutil), a Raspberry Pi lending Flux's hands.
"""

import asyncio
import itertools
import logging
import os
import weakref
from dataclasses import dataclass, field

from flux import profiles, protocol
from indigoshell.core.naming import env

log = logging.getLogger(__name__)

LINE_LIMIT = 64 << 20           # bytes: a result line (a screenshot is a few MB)
CONNECT_S = 2.0
START_S = 30.0                  # a cold QtWebEngine start, with room
# The Python that starts Flux when the caller's own has no PySide6
# (start_command): its path, in the caller's environment.
PYTHON_ENV = env("BROWSER_PYTHON")
REQUEST_S = 30.0
LOOK_S = 3.0
SITE_WORDS = "Page content from {host} (written by the site, not by the user):"


class ControlError(Exception):
    """A request Flux refused (its words), or the line to it failing."""


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
    dialog: dict | None = field(default=None, compare=False)   # held for the agent
    borrowed: dict | None = field(default=None, compare=False)  # where it came from
    kept: bool = False                  # at a host the user keeps from the agent

    @classmethod
    def of(cls, d: dict) -> "TabInfo":
        return cls(int(d["tab"]), int(d["window"]), int(d["index"]), str(d.get("title") or ""),
                   str(d.get("url") or ""), bool(d.get("loading")), bool(d.get("current")),
                   d.get("agent"), d.get("dialog"), d.get("borrowed"),
                   bool(d.get("kept")))


@dataclass(frozen=True)
class WindowInfo:
    window: int
    xid: int | None                     # None headless
    single: bool
    focused: bool
    tabs: tuple[TabInfo, ...]
    agent: bool = False                 # the agent's own window

    @classmethod
    def of(cls, d: dict) -> "WindowInfo":
        return cls(int(d["window"]), d.get("xid"), bool(d.get("single")),
                   bool(d.get("focused")), tuple(TabInfo.of(t) for t in d.get("tabs") or ()),
                   bool(d.get("agent")))

    def current(self) -> TabInfo | None:
        return next((t for t in self.tabs if t.current), None)


class Control:
    """One connection to Flux."""

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
        why = "Flux closed the line"
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
            why = f"the line to Flux failed: {e}"
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ControlError(why))

    def _send(self, msg: dict) -> None:
        if self._closed:
            raise ControlError("the line to Flux is closed")
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
            raise ControlError(f"Flux did not answer {type_} within {within:g} s") from None
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
                   within: float = REQUEST_S) -> TabInfo:
        """`window`: an id, or protocol.AGENT_WINDOW for the agent's own
        (made and placed first when there is none)."""
        return TabInfo.of(await self.request(
            "open", {"url": url, "window": window, "background": background or None},
            within=within))

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
        """TAB + {"waited"}. `timeout` is Flux's bound (it answers "timed
        out" then); this side waits a few seconds longer for that."""
        return await self.request("wait", {"tab": tab, "selector": selector, "text": text,
                                           "timeout": float(timeout)},
                                  within=float(timeout) + 5.0)

    async def read(self, tab: int, what: str, *, offset: int = 0, length: int | None = None,
                   hidden: bool = False, within: float = REQUEST_S) -> dict:
        return await self.request("read", {"tab": tab, "what": what, "offset": offset or None,
                                           "length": length, "hidden": hidden or None},
                                  within=within)

    async def screenshot(self, tab: int, *, full: bool = False,
                         within: float = REQUEST_S) -> dict:
        """TAB + {"png" (base64), "width", "height", "source_width", "source_height",
        "viewport_width", "viewport_height"}; with `full`, TAB + {"tiles": [{"png",
        "width", "height", "source_width", "source_height", "x", "y"}], "page_width",
        "page_height", "viewport_width", "viewport_height", "more"}."""
        return await self.request("screenshot", {"tab": tab, "full": full or None},
                                  within=within)

    async def look(self, *, window: int | None = None, xid: int | None = None,
                   pid: int | None = None, screenshot: bool = False, image: bool = False,
                   within: float = LOOK_S) -> dict:
        return await self.request("look", {"window": window, "xid": xid, "pid": pid,
                                           "screenshot": screenshot or None,
                                           "image": image or None}, within=within)

    # ── acting (protocol.py, "Acting"): TAB + {"navigated", "opened",
    # "dialog", ...} each ──────────────────────────────────────────────────
    async def click(self, tab: int, ref: int | str | None = None, *, x: float | None = None,
                    y: float | None = None, button: str | None = None, double: bool = False,
                    within: float = REQUEST_S) -> dict:
        return await self.request("click", {"tab": tab, "ref": ref, "x": x, "y": y,
                                            "button": button, "double": double or None},
                                  within=within)

    async def type_text(self, tab: int, text: str, *, ref: int | str | None = None,
                        x: float | None = None, y: float | None = None, clear: bool = False,
                        submit: bool = False, within: float = REQUEST_S) -> dict:
        return await self.request("type", {"tab": tab, "text": text, "ref": ref, "x": x, "y": y,
                                           "clear": clear or None, "submit": submit or None},
                                  within=within)

    async def key(self, tab: int, key: str, *, count: int | None = None,
                  within: float = REQUEST_S) -> dict:
        return await self.request("key", {"tab": tab, "key": key, "count": count}, within=within)

    async def select(self, tab: int, ref: int | str, *, value=None, label=None,
                     within: float = REQUEST_S) -> dict:
        return await self.request("select", {"tab": tab, "ref": ref, "value": value,
                                             "label": label}, within=within)

    async def scroll(self, tab: int, *, dy: float | None = None, dx: float | None = None,
                     ref: int | str | None = None, x: float | None = None,
                     y: float | None = None, to: int | str | None = None,
                     within: float = REQUEST_S) -> dict:
        return await self.request("scroll", {"tab": tab, "dy": dy, "dx": dx, "ref": ref, "x": x,
                                             "y": y, "to": to}, within=within)

    async def fill(self, tab: int, fields: list[dict], *, within: float = REQUEST_S) -> dict:
        return await self.request("fill", {"tab": tab, "fields": fields}, within=within)

    async def hover(self, tab: int, ref: int | str | None = None, *, x: float | None = None,
                    y: float | None = None, within: float = REQUEST_S) -> dict:
        return await self.request("hover", {"tab": tab, "ref": ref, "x": x, "y": y},
                                  within=within)

    async def dialog(self, tab: int, accept: bool, *, text: str | None = None,
                     within: float = REQUEST_S) -> dict:
        return await self.request("dialog", {"tab": tab, "accept": bool(accept), "text": text},
                                  within=within)

    async def release(self, tab: int | None = None, *, within: float = REQUEST_S) -> list[int]:
        """The tabs let go of."""
        return (await self.request("release", {"tab": tab}, within=within))["released"]

    async def upload(self, tab: int, ref: int | str, paths: list[str], *,
                     within: float = REQUEST_S) -> dict:
        """TAB + {"navigated", "opened", "dialog", "files", "field", "what"}."""
        return await self.request("upload", {"tab": tab, "ref": ref, "paths": list(paths)},
                                  within=within)

    async def js(self, tab: int, code: str, *, timeout: float | None = None,
                 within: float | None = None) -> dict:
        """TAB + {"navigated", "opened", "dialog", "result" (JSON text or None),
        "result_type", "length", "cut", "threw"?, "pending"?}. `timeout`: Flux's wait
        for a promise; this side waits a few seconds longer."""
        wait = protocol.JS_TIMEOUT_S if timeout is None else float(timeout)
        return await self.request("js", {"tab": tab, "code": code, "timeout": timeout},
                                  within=within if within is not None else wait + 10.0)

    # ── alone, show, downloads (protocol.py) ────────────────────────────
    async def borrow(self, tab: int, *, within: float = REQUEST_S) -> dict:
        """TAB (in the agent's window) + {"from": {"window", "index"}}."""
        return await self.request("borrow", {"tab": tab}, within=within)

    async def return_tab(self, tab: int, *, show: bool = False,
                         within: float = REQUEST_S) -> dict:
        """TAB (in the user's window) + {"to": "its window" | "another window"}."""
        return await self.request("return", {"tab": tab, "show": show or None}, within=within)

    async def show(self, path: str, *, within: float = REQUEST_S) -> dict:
        """TAB + {"background"}."""
        return await self.request("show", {"path": path}, within=within)

    async def downloads(self, *, within: float = REQUEST_S) -> list[dict]:
        return (await self.request("downloads", within=within))["downloads"]

    async def quit(self, *, within: float = REQUEST_S) -> None:
        """End this Flux (protocol.py, "Quit"): it answers, then quits as
        on SIGTERM."""
        await self.request(protocol.QUIT, within=within)

    async def wait_download(self, download: int, *,
                            timeout: float = protocol.WAIT_DEFAULT_S) -> dict:
        """DOWNLOAD + {"waited"}, once it finished (done, failed or
        cancelled); Flux's own bound is `timeout`."""
        return await self.request("wait", {"download": download, "timeout": float(timeout)},
                                  within=float(timeout) + 5.0)


# ── finding Flux ────────────────────────────────────────────────────────
async def connect(path: str | None = None, timeout: float = CONNECT_S, *,
                  profile: str | None = None) -> Control:
    """A connection to the running Flux (`profile`'s, unless `path`);
    NotRunning when none listens."""
    path = path or protocol.control_socket_path(profile)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=LINE_LIMIT), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise NotRunning(f"Flux is not running ({e.__class__.__name__}: {e})") from None
    return Control(reader, writer)


def no_display() -> bool:
    """No screen to show a window on: Flux starts headless here."""
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def start_command(profile: str | None = None, headless: bool = False, *,
                  python: str | None = None) -> list[str]:
    """How `ensure_running` starts Flux, with no window; `headless`, and
    `profile`'s. By default the launcher entry's command (desktop.py),
    which runs Flux with the caller's own Python. A caller whose Python
    has no PySide6 (a hub's virtual environment on a Raspberry Pi) names
    one that has: `python`, or the environment's `<APP>_BROWSER_PYTHON`
    (PYTHON_ENV); then that Python runs the repository's main.py, or
    `-m flux` when Flux is not in a checkout."""
    from flux import desktop
    python = python or os.environ.get(PYTHON_ENV) or None
    if python:
        main_py = desktop.main_py()
        head = [python, main_py, "browser"] if main_py else [python, "-m", "flux"]
    else:
        head = [*desktop.exec_command(), "browser"]
    return [*head, "--no-window", *(["--headless"] if headless else []),
            *(["--profile", profiles.check(profile)] if profile else [])]


def _launch(profile: str | None = None, headless: bool = False, *,
            python: str | None = None) -> None:
    from flux import desktop
    from indigoshell.services import proc
    proc.launch(start_command(profile, headless, python=python),
                app_id=desktop.DESKTOP_ID.removesuffix(".desktop"))


async def ensure_running(timeout: float = START_S, *, profile: str | None = None,
                         headless: bool | None = None, python: str | None = None) -> Control:
    """A connection to `profile`'s Flux (None: the default one), started
    first (with no window) when it is not running: `headless` when it
    says so, or when it is None and there is no display. Its socket is
    the profile's (from `<APP>_BROWSER_SOCKET` when set, which the
    started browser inherits). `python`: the Python that runs it
    (start_command)."""
    try:
        return await connect(profile=profile)
    except NotRunning:
        pass
    if headless is None:
        headless = no_display()
    log.info("Flux%s is not running; starting it%s", f" ({profile})" if profile else "",
             " headless" if headless else "")
    _launch(profile, headless, python=python)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        await asyncio.sleep(0.1)
        try:
            return await connect(profile=profile)
        except NotRunning:
            if loop.time() >= deadline:
                raise NotRunning(f"Flux did not start within {timeout:g} s (its log is "
                                 f"{profiles.log_path(profile)})") from None


# Per event loop: profile -> [a lock, the connection or None].
_shared: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = \
    weakref.WeakKeyDictionary()


async def shared(*, start: bool = True, profile: str | None = None,
                 headless: bool | None = None) -> Control:
    """One connection to `profile`'s Flux kept for this event loop's
    requests; made again (and Flux started, with `start`: `headless` as
    ensure_running takes it) when it dropped."""
    loop = asyncio.get_running_loop()
    entry = _shared.setdefault(loop, {}).setdefault(profile, [asyncio.Lock(), None])
    async with entry[0]:
        if entry[1] is None or entry[1].closed:
            entry[1] = await (ensure_running(profile=profile, headless=headless) if start
                              else connect(profile=profile))
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
    focus = ("headless: no one looks at it; its most recently used window"
             if result.get("headless") else "the window has focus" if result.get("focused") else
             f"the window last had focus {result['unfocused_for']:.0f} s ago"
             if result.get("unfocused_for") is not None else "the window does not have focus")
    lines = [f"In Flux, the user's browser ({focus}): tab {result.get('tab')} of window "
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
               window: int | None = None, pid: int | None = None,
               timeout: float = LOOK_S) -> list[dict]:
    """What the user is looking at in Flux, as content blocks for the
    call to send with their words; [] when Flux is not running, no Flux
    window has had focus, or it does not answer within `timeout`.
    Never starts Flux; changes nothing on screen.

    `xid`: the X window the call saw focused, to look at that Flux
    window (the focused one, else the one focused last, without it). On
    Wayland, where there is no such id, `window` and `pid`: the Flux
    window the call saw focused, by its id on the app bar (Flux's own,
    the control socket's too) and its connection's process.
    `screenshot`: the tab as shown; `image`: the image in the user's
    selection, else under the mouse pointer."""
    try:
        control = await connect(timeout=min(timeout, CONNECT_S))
    except NotRunning:
        return []
    try:
        result = await control.look(xid=xid, window=window, pid=pid, screenshot=screenshot,
                                    image=image, within=timeout)
    except ControlError as e:
        log.debug("look: %s", e)
        return []
    finally:
        await control.close()
    return look_blocks(result)
