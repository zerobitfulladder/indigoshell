"""A thin client for qtile's command interface (`qtile cmd-obj`).

Used for what the camera should not see — staging workspaces before the
take, closing what it opened afterwards — and for the checks the take
makes before anything irreversible, like confirming which window has
focus before a mod+q.
"""

import ast
import json
import subprocess
import time


def call(*args: str):
    out = subprocess.run(["qtile", "cmd-obj", *args], capture_output=True,
                         text=True, timeout=10, check=True).stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return ast.literal_eval(out)


def windows() -> list[dict]:
    return call("-o", "cmd", "-f", "windows") or []


def window(win_id: int) -> dict | None:
    return next((w for w in windows() if w["id"] == win_id), None)


def focused() -> dict:
    return call("-o", "window", "-f", "info")


def current_group() -> str:
    return call("-o", "group", "-f", "info")["name"]


def show_group(name: str) -> None:
    call("-o", "group", name, "-f", "toscreen")


def spawn(command: str) -> None:
    call("-o", "cmd", "-f", "spawn", "-a", command)


def kill(win_id: int) -> None:
    """Close the way a user would — WM_DELETE_WINDOW, so the app saves."""
    call("-o", "window", str(win_id), "-f", "kill")


def wait_for_window(match, *, known: set[int] = frozenset(),
                    timeout: float = 15.0) -> dict:
    """The first window not in `known` that `match(info)` accepts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in windows():
            if w["id"] not in known and match(w):
                return w
        time.sleep(0.15)
    raise TimeoutError("window did not appear")


def wait_gone(win_id: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if window(win_id) is None:
            return
        time.sleep(0.15)
    raise TimeoutError(f"window {win_id} did not close")
