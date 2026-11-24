"""What Jack (the browser) and the shell say to each other.

Pure Python: imported by the browser (Qt) and by the daemon's side of
Jack (`browser/context.py`, xcffib and skia), so it imports neither.

The line itself is every indigoshell app's (applink/protocol.py): one
unix socket connection per browser window, the shell listening, one
JSON object per line both ways, each with its `type`; a type a side
does not know is ignored. This module is what Jack says on it.

Browser -> shell
    announce   {"type": "announce", "app": "jack", "wid": <X window>, "single": bool}
               First on every connection, then the current state.
               `single`: a window of one page that takes no tabs (a page's
               sign-in popup, the inspector). The shell takes KEYS from
               Jack's other windows, the main ones; a single window keeps
               them (its own handling, below).
    state      {"type": "state",
                "tabs": [{"title": str, "host": str, "loading": bool}, ...],
                "current": <index into tabs>,
                "progress": <0..100, the current tab's load>,
                "secure": <bool, the current tab's connection>,
                "url": str,                      the current tab's address
                "bookmarked": bool,              ... and whether it counts as a
                                                 bookmark: its address is one,
                                                 or it was reached from one
                                                 through redirects
                "find": null | {"query": str, "active": int | null,
                                "total": int | null, "typing": bool}}
               Always whole, every key there, and only when it differs
               from the last one sent on this connection. `find` is the
               find-in-page line while it is open: `typing` while the
               query is being typed (a caret is drawn), false while
               stepping through the matches with n / N; `active`
               (1-based) and `total` are null until the page has counted.
    download   {"type": "download", "name": str, "state": one of DOWNLOAD_STATES,
                "percent": <0..100, or null when unknown>}
    launcher   {"type": "launcher", "action": "address"}
               Open the launcher for this window as for that action's key
               (ACTIONS), with no key pressed: what a new window opened
               with no address sends, once the shell has it.

Shell -> browser
    back, forward, reload
               {"type": "back"} ...                the current tab
    open       {"type": "open", "url": str}         a new tab in this window
    tab        {"type": "tab", "index": int}        switch to that tab
    close      {"type": "close"}                    close the current tab
               {"type": "close", "index": int}      close that tab
    load       {"type": "load", "url": str, "where": "here" | "tab"}
               in the current tab, or a new one at the end
    bookmark   {"type": "bookmark", "op": "add" | "remove"}
               the current page: "remove" takes out the bookmark it
               counts as (the one it was reached from first, see
               `bookmarked`)
               {"type": "bookmark", "op": "add" | "remove", "url": str}
               that address's bookmark instead (the address list's
               Shift+Delete)
    prompt     {"type": "prompt", "action": one of ACTIONS}
               The shell took that action's key (or a `launcher`) but
               cannot show its launcher: do what the browser does for the
               key with no shell, its own dialog.

The keys (KEYS). Ctrl+L (an address for this tab), Ctrl+T (an address
for a new tab) and Ctrl+M (the window's actions) open the shell's
launcher, dropped from the top bar over the window. The shell grabs them
on every main window it is told of (applink/service.py): the X server
then gives the press, and the keyboard with it, to the shell and not to
the browser, so every key typed after the chord goes to the launcher's
query in order and none reaches the page. With no shell, a key the
shell could not grab, or a single window, they reach the browser: Ctrl+L
and Ctrl+T ask with a Qt dialog of the browser's own, Ctrl+M does
nothing.

Single instance: the running browser also listens at
`instance_socket_path()`, guarded by an flock on `instance_lock_path()`.
A second invocation sends {"type": "open", "urls": [url, ...]} (an
empty list asks for a new window) and waits for {"type": "ok"}; a
request it cannot take is answered {"type": "error", "error": str}.
A line with no `id` is such a hand-off: answered once, then the line is
closed (also, without an answer, for a type that is not `open`).

Control: the same socket, one per browser process, is how the agent
drives the browser (browser/control.py is the client, browser/qt/
control.py the browser's side). A connection stays open for as many
requests as it likes. Every request carries an `id`, an int the client
picks, unique among its requests in flight on that connection; several
may be in flight, and each is answered once, in any order:

    {"type": <request>, "id": N, ...}
    {"type": "result", "id": N, "ok": true, ...}
    {"type": "result", "id": N, "ok": false, "error": str}

    cancel     {"type": "cancel", "id": N}
               Stops request N. If it was still running it is answered
               ok:false, error "cancelled"; otherwise nothing is.

A type the browser does not know is answered ok:false. Times are
seconds. Tabs and windows are named by ids the browser gives them, ints
that are never reused within one browser process; a tab's id is its
page's, so it stays the same when the page moves to another window.
Windows are listed most recently used first.

TAB, as several results give it:
    {"tab": id, "window": id, "index": <0-based, in its window>,
     "title": str, "url": str, "loading": bool, "current": bool}
    `current`: the tab its window shows.

**Showing.** The agent works only on what the user could see: every
request that reads, shows or acts on a tab (close, go, wait, read,
screenshot) refuses a tab that is not the current one of its window,
ok:false with the error NOT_SHOWING ("tab N is not showing in its
window; switch to it first"); a wait whose tab stops showing ends so.
A window on another workspace still counts as showing: its pages stay
live. `switch` makes a tab current without raising its window, taking
focus or changing the workspace; `open` and `tabs` work on any.

    tabs       {}
               -> {"windows": [{"window": id, "xid": <X window, or null>,
                                "single": bool, "focused": bool,
                                "tabs": [TAB, ...]}, ...],
                   "focused": <the focused window's id, or null>}
               `single`: a page's popup or the inspector (one page, takes
               no tabs). `xid` is the window manager's id for the window.
    open       {"url": str, "window"?: id, "background"?: bool}
               A new tab at the end of that window (a main one), or of
               the most recently used main window (one is made when there
               is none). It becomes the current tab unless `background`.
               -> TAB. Answered once the load is asked for; `wait` for it.
               `url` is anything the address line takes (a search too).
    switch     {"tab": id}                         -> TAB
    close      {"tab": id}                         -> {"tab": id, "window_closed": bool}
               The window closes with its last tab, and the browser
               with its last window.
    go         {"tab": id, "url": str}
               {"tab": id, "action": "back" | "forward" | "reload"}
               -> TAB, once the navigation is asked for.
    wait       {"tab": id, "timeout"?: s}                 until it has loaded
               {"tab": id, "selector": str, "timeout"?: s}  ... until the CSS
                                                          selector matches
               {"tab": id, "text": str, "timeout"?: s}    ... until the text
                                                          is on the page (as
                                                          rendered, case kept)
               -> TAB + {"waited": s}; ok:false "timed out after N s ..."
               when not by `timeout` (default WAIT_DEFAULT_S, at most
               WAIT_MAX_S). A load asked for a moment ago and not yet
               begun counts as loading.
    read       {"tab": id, "what": one of READS, "offset"?: int,
                "length"?: int, "hidden"?: bool}
               -> TAB + {"what", "offset", "total", "more": bool,
                         "next": <offset of the rest, or null>, ...}
               The page's main frame (iframes are not read), visible
               elements only unless `hidden` (links, forms, outline).
               text      {"text": str}: the text as rendered, characters
                         [offset, offset + length) of `total`
                         (length: TEXT_LENGTH, at most TEXT_MAX)
               links     {"links": [{"ref", "text", "url" (absolute)}]},
                         links [offset, offset + length) of `total`
                         (LINKS_LENGTH, at most LINKS_MAX)
               forms     {"forms": [{"ref" (null for the fields outside
                         any form), "name", "action", "method",
                         "fields": [{"ref", "label", "type", "name",
                         "value", "required": bool, "disabled": bool,
                         "checked"?: bool, "options"?: [{"value", "text",
                         "selected"}]}]}]}, forms [offset, ...) of `total`
               outline   {"outline": [str, ...]}: the page's meaningful
                         elements, one line each, indented under the
                         landmark that holds them:
                             navigation "Main"
                               [14] link "Orders" -> /orders
                             heading 2 "Your account"
                             [12] button "Sign in"
                             [13] textbox "Email" = "a@b.example"
                         lines [offset, offset + length) of `total`
                         (OUTLINE_LENGTH, at most OUTLINE_MAX)
               A password field's value is never read: it reads
               PASSWORD_VALUE when it holds one. `[N]` and "ref" are
               numbers for elements, the same for an element in every
               read until the page loads another document; they are kept
               where the page cannot see them (an isolated script world).
    screenshot {"tab": id}
               -> TAB + {"png": <base64>, "width", "height" (as sent),
                         "source_width", "source_height" (as rendered)}
               The visible part of the page, after a frame painted since
               the request came; its long side scaled down to at most
               SHOT_MAX_SIDE.
    look       {"window"?: id, "xid"?: int, "screenshot"?: bool,
                "image"?: bool}
               What the user is looking at in the browser, changing
               nothing they see: the focused window, or with none the one
               that had focus last (or the window named).
               -> TAB + {"focused": bool, "unfocused_for": <s, or null
                         while focused>, "selection": str,
                         "selection_in": "page" | "field" | "frame" | null,
                         "screenshot"?: {"png", "width", "height",
                                         "source_width", "source_height"},
                         "image"?: null | {"png", "width", "height",
                                   "source_width", "source_height",
                                   "under": "selection" | "pointer",
                                   "url": str, "alt": str,
                                   "from": "page" | "screen"}}
               `selection`: the text the user selected (at most 200,000
               characters), in the page, a text field (never a password
               field) or a frame (where Qt has frames, 6.8 on); "" for
               none. `image`: the image in the selection, else the one
               under the mouse pointer: its own pixels where the page may
               read them ("page"; a cross-origin image without CORS it
               may not), else the part of the screen it covers ("screen";
               a selected image is drawn tinted there).
"""

import os

from indigoshell.applink import protocol as _link
from indigoshell.applink.protocol import (  # noqa: F401  (part of this module's API)
    ANNOUNCE, ATTACHED, RESTARTING, SOCKET_ENV, TYPE, WID, decode, encode, message,
    socket_path)
from indigoshell.core.naming import APP, env
from indigoshell.core.paths import runtime_dir

KIND = "jack"                   # the app kind Jack announces

# ── where ───────────────────────────────────────────────────────────────
INSTANCE_SOCKET_ENV = env("BROWSER_SOCKET")


def instance_socket_path() -> str:
    """Where the running browser takes URLs from later invocations."""
    return os.environ.get(INSTANCE_SOCKET_ENV) or os.path.join(
        runtime_dir(), f"{APP}-browser-{os.getuid()}.sock")


def instance_lock_path() -> str:
    return instance_socket_path() + ".lock"


def control_socket_path() -> str:
    """Where the running browser takes the agent's requests: the same
    socket (one per browser process)."""
    return instance_socket_path()


# ── names ───────────────────────────────────────────────────────────────
SINGLE = "single"
STATE = "state"
TABS = "tabs"
CURRENT = "current"
PROGRESS = "progress"
SECURE = "secure"
URL = "url"
BOOKMARKED = "bookmarked"
FIND = "find"
TITLE = "title"
HOST = "host"
LOADING = "loading"
DOWNLOAD = "download"
DOWNLOAD_STATES = ("started", "progress", "done", "failed", "cancelled")
LAUNCHER = "launcher"
PROMPT = "prompt"

# What the shell's launcher does for the browser, and the keys for it.
ACTIONS = ("address", "new_tab", "actions")
KEYS = {"ctrl+l": "address", "ctrl+t": "new_tab", "ctrl+m": "actions"}

COMMANDS = ("back", "forward", "reload", "open", "tab", "close", "load", "bookmark",
            PROMPT)

OPEN = "open"
URLS = "urls"
OK = "ok"
ERROR = "error"

STATE_KEYS = (TABS, CURRENT, PROGRESS, SECURE, URL, BOOKMARKED, FIND)
TAB_KEYS = (TITLE, HOST, LOADING)
FIND_KEYS = ("query", "active", "total", "typing")


# ── messages ────────────────────────────────────────────────────────────
def announce(wid: int, single: bool) -> dict:
    return _link.announce(KIND, wid, **{SINGLE: bool(single)})


def tab(title: str, host: str, loading: bool) -> dict:
    return {TITLE: title, HOST: host, LOADING: bool(loading)}


def find(query: str, active: int | None, total: int | None, typing: bool) -> dict:
    return {"query": query, "active": active, "total": total, "typing": bool(typing)}


FIND_LABEL = "find"
FIND_CARET = "▏"           # ▏, after the query while it is typed
NO_MATCHES = "no matches"


def find_count(f: dict) -> str:
    """The count after the query, as the top bar and the browser's own
    line say it:
    "3/12", "no matches", or nothing before the page has counted."""
    total, active = f.get("total"), f.get("active")
    if total is None or not f.get("query"):
        return ""
    if total == 0:
        return NO_MATCHES
    return f"{active or 0}/{total}"


def state(tabs: list[dict], current: int, progress: int, secure: bool, *,
          url: str, bookmarked: bool, find: dict | None) -> dict:
    return message(STATE, **{TABS: tabs, CURRENT: int(current), PROGRESS: int(progress),
                             SECURE: bool(secure), URL: url, BOOKMARKED: bool(bookmarked),
                             FIND: find})


def _count(value) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool)
                             and value >= 0)


def state_problems(msg: dict) -> list[str]:
    """What is wrong with a state message; empty when it is well formed."""
    problems = []
    missing = [k for k in STATE_KEYS if k not in msg]
    if missing:
        problems.append(f"missing {missing}")
        return problems
    tabs = msg[TABS]
    if not isinstance(tabs, list) or not tabs:
        problems.append("tabs is not a non-empty list")
        return problems
    for i, t in enumerate(tabs):
        if not isinstance(t, dict) or set(t) != set(TAB_KEYS):
            problems.append(f"tab {i} is {t!r}")
        elif not (isinstance(t[TITLE], str) and isinstance(t[HOST], str)
                  and isinstance(t[LOADING], bool)):
            problems.append(f"tab {i} field types: {t!r}")
    current = msg[CURRENT]
    if not isinstance(current, int) or not 0 <= current < len(tabs):
        problems.append(f"current {current!r} outside 0..{len(tabs) - 1}")
    progress = msg[PROGRESS]
    if not isinstance(progress, int) or not 0 <= progress <= 100:
        problems.append(f"progress {progress!r}")
    if not isinstance(msg[SECURE], bool):
        problems.append(f"secure {msg[SECURE]!r}")
    if not isinstance(msg[URL], str):
        problems.append(f"url {msg[URL]!r}")
    if not isinstance(msg[BOOKMARKED], bool):
        problems.append(f"bookmarked {msg[BOOKMARKED]!r}")
    f = msg[FIND]
    if f is not None:
        if not isinstance(f, dict) or not all(k in f for k in FIND_KEYS):
            problems.append(f"find {f!r}")
        elif not (isinstance(f["query"], str) and _count(f["active"])
                  and _count(f["total"]) and isinstance(f["typing"], bool)):
            problems.append(f"find field types: {f!r}")
    return problems


def download(name: str, state_: str, percent: int | None) -> dict:
    return message(DOWNLOAD, name=name, state=state_, percent=percent)


def launcher(action: str) -> dict:
    return message(LAUNCHER, action=action)


def command(name: str, **args) -> dict:
    """A message from the shell to the browser: one of COMMANDS."""
    if name not in COMMANDS:
        raise ValueError(f"not a browser command: {name!r}")
    return message(name, **args)


def open_urls(urls: list[str]) -> dict:
    return message(OPEN, **{URLS: list(urls)})


# ── control (the agent's requests) ──────────────────────────────────────
ID = "id"
RESULT = "result"
CANCEL = "cancel"
CANCELLED = "cancelled"
REQUESTS = ("tabs", "open", "switch", "close", "go", "wait", "read", "screenshot", "look")
GO_ACTIONS = ("back", "forward", "reload")
READS = ("text", "links", "forms", "outline")

NOT_SHOWING = "tab {tab} is not showing in its window; switch to it first"
PASSWORD_VALUE = "(password)"

WAIT_DEFAULT_S = 30.0
WAIT_MAX_S = 600.0
TEXT_LENGTH, TEXT_MAX = 20000, 100000          # characters
LINKS_LENGTH, LINKS_MAX = 200, 1000
FORMS_LENGTH, FORMS_MAX = 20, 100
OUTLINE_LENGTH, OUTLINE_MAX = 400, 2000        # lines
SHOT_MAX_SIDE = 1568                           # px, the long side


def is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def request(type_: str, id_: int, **args) -> dict:
    return message(type_, **{ID: id_}, **args)


def result(id_, **fields) -> dict:
    return message(RESULT, **{ID: id_, "ok": True}, **fields)


def failure(id_, error: str) -> dict:
    return message(RESULT, **{ID: id_, "ok": False, "error": str(error)})


def not_showing(tab: int) -> str:
    return NOT_SHOWING.format(tab=tab)


def insecure_scheme(scheme: str) -> bool:
    """Whether a page reached over this scheme travelled in the clear.
    Local schemes (about:, file:, chrome:, data:) count as secure: there
    is no connection to doubt."""
    return scheme.lower() in ("http", "ws", "ftp")
