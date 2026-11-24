"""What every indigoshell app and the shell say to each other.

Pure Python: imported by the apps (Qt processes) and by the daemon, so
it imports neither toolkit.

One unix stream socket connection per app window, the shell listening
at `socket_path()` (`<APP>-apps-<uid>.sock` in the runtime directory),
the app connecting. One JSON object per line, both ways, each with its
`type`: a side dispatches on it, ignores a type it does not know (with
one debug line), and ignores keys it does not use. What a kind of app
says after its announce is its own (Flux's: browser/protocol.py).

App -> shell
    announce    {"type": "announce", "app": <kind>, "wid": <X window>, ...}
                First on every connection, before anything else. `app`
                names the kind, whose class on the shell's side decides
                how the top bar draws the window and which keys the shell
                takes from it (applink/service.py); a kind may say more
                here (Flux: browser/protocol.py).

App -> shell, any time after the announce
    place       {"type": "place", "with"?: <X window>}
                The window, made but not yet shown (mapped), wants a
                workspace of the window manager's before it maps, so that
                it never appears on the one the user is looking at: an
                empty one no screen shows (applink/service.py has the
                rule), or with `with` the one that other window is on (a
                popup of a window placed so). The app maps the window once
                `placed` answers, or after PLACE_WAIT_S with no answer (an
                older shell that does not know `place`), or at once when
                no shell listens: then wherever the window manager puts it.

Shell -> app
    attached    {"type": "attached"}
                The answer to the announce: the shell has the window. It
                shows the window's state in its top bar while the window
                has focus, so the app draws none of its own.
    restarting  {"type": "restarting"}
                Sent as the shell stops for a reload: it will be back in
                a moment (a second or two), so an app can wait longer than
                usual before it shows its state itself.
    placed      {"type": "placed", "desktop": <index> | null, "why"?: str}
                The answer to `place`: the window's `_NET_WM_DESKTOP` is
                set (and read back, so the X server has it before the app
                maps), or null with why not (no workspace to put it on,
                the window already shown, no X display).
"""

import json
import os

from indigoshell.core.naming import APP, env
from indigoshell.core.paths import runtime_dir

SOCKET_ENV = env("APPS_SOCKET")

TYPE = "type"
APP_KEY = "app"
WID = "wid"

ANNOUNCE = "announce"
ATTACHED = "attached"
RESTARTING = "restarting"
PLACE = "place"
PLACED = "placed"
WITH = "with"
DESKTOP = "desktop"
PLACE_WAIT_S = 1.5              # an app waits this long for `placed`, then maps anyway


def socket_path() -> str:
    """Where the shell listens for its apps."""
    return os.environ.get(SOCKET_ENV) or os.path.join(
        runtime_dir(), f"{APP}-apps-{os.getuid()}.sock")


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes | str) -> dict | None:
    """One line as a message: a JSON object with a string `type`; None
    for anything else."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get(TYPE), str) else None


def message(type_: str, **fields) -> dict:
    return {TYPE: type_, **fields}


def announce(app: str, wid: int, **fields) -> dict:
    return message(ANNOUNCE, **{APP_KEY: app, WID: int(wid)}, **fields)


def valid_wid(wid) -> bool:
    """Whether an announced window id can be an X window at all (X
    resource ids are 29 bits)."""
    return isinstance(wid, int) and not isinstance(wid, bool) and 0 < wid < 1 << 29


def place(with_wid: int | None = None) -> dict:
    return message(PLACE, **({WITH: int(with_wid)} if with_wid else {}))


def placed(desktop: int | None, why: str | None = None) -> dict:
    return message(PLACED, **{DESKTOP: desktop}, **({"why": why} if why else {}))
