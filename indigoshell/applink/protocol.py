"""The apps' line itself: where it is, how a message is written, and the
messages every app window says to the shell however it fills its bar.

Pure Python: imported by the apps (Qt processes) and by the daemon, so
it imports neither toolkit.

One unix stream socket connection per app window, the shell listening
at `socket_path()` (`<APP>-apps-<uid>.sock` in the runtime directory),
the app connecting. One JSON object per line, both ways, each with its
`type`: a side dispatches on it, ignores a type it does not know (with
one debug line), and ignores keys it does not use. What goes on the line
about the top bar (`announce`, `bar`, `answer`, `open`, the bar's
events) is the app bar protocol: PROTOCOL.md beside this file, its
vocabulary in bar.py. This module has the rest:

App -> shell, any time after the announce
    place       {"type": "place", "with"?: <the app's id for another of its windows>}
                The window, made but not yet shown, wants a workspace of
                the window manager's, so that it never appears on the one
                the user is looking at: an empty one no monitor shows
                (applink/service.py has the rule), or with `with` the one
                that other window (of the same app process) is on: a
                popup of a window placed so.
                The app shows the window once `placed` answers, or after
                PLACE_WAIT_S with no answer, or at once when no shell
                listens: then wherever the window manager puts it. The
                shell answers at once and moves the window there as soon
                as the compositor lists it (PROTOCOL.md, "Placing").

Shell -> app
    attached    {"type": "attached", "agent": {"name", "color"}}
                The answer to the announce: the shell has the window. It
                shows the window's bar while the window has focus, so the
                app draws none of its own. `agent`: the agent's name and
                colour, as the shell knows them (core/persona.py: the
                hub's welcome names her), for an app's labels and marks.
    restarting  {"type": "restarting"}
                Sent as the shell stops for a reload: it will be back in
                a moment (a second or two), so an app can wait longer than
                usual before it shows its state itself.
    placed      {"type": "placed", "desktop": <index> | null, "why"?: str}
                The answer to `place`: the workspace the window will be
                moved to once it maps (its index among the bar's
                workspaces), or null with why not (no window manager to
                place it with, no workspace to put it on).
"""

import json
import os

from indigoshell.core.naming import APP, env
from indigoshell.core.paths import runtime_dir

SOCKET_ENV = env("APPS_SOCKET")

TYPE = "type"

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


def place(with_window=None) -> dict:
    """`with_window`: the app's own id for the window to go beside."""
    return message(PLACE, **({WITH: with_window} if with_window is not None else {}))


def placed(desktop: int | None, why: str | None = None) -> dict:
    return message(PLACED, **{DESKTOP: desktop}, **({"why": why} if why else {}))
