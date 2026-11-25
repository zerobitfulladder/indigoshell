"""The desktop, for what the shell does there on the agent's behalf and
the user's: running a program, asking the window manager (indigoshell/wm:
qtile on X, niri on Wayland), opening a link or a file in its usual app
and bringing that window forward on its workspace.

laptop.py's hands use these (the hub's agent acting on the laptop), and
the call opens a link clicked in a reply with `open_target`.
"""

import asyncio
import os
import re

import skia

from .. import wm
from ..services import proc
# A subprocess's (exit code, stdout, stderr), and `qtile cmd-obj` — kept
# here under their old names for the callers that use them.
from ..wm import run
from ..wm.x11 import qtile

__all__ = ["run", "qtile", "decode", "bring_forward", "open_target"]


def decode(data: bytes) -> skia.Image | None:
    """Decode now, from a copy. `MakeFromEncoded` on a Python `bytes`
    wraps the buffer without copying and decodes lazily, so an image kept
    past the call decodes freed memory — it came out blank."""
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    return image.makeRasterImage() if image is not None else None


async def bring_forward(desktop_id: str) -> str:
    """Show the window of the app behind `desktop_id` (what xdg-open
    just handed something to) on its workspace, and focus it.

    Matched by class (X) or app id (Wayland) against the desktop id's
    name ("firefox" from firefox.desktop, "celluloid" from
    io.github…Celluloid.desktop). A browser opens a link as a tab in its
    last window, which is the focused window of its workspace; an app
    that was not running yet maps its window on the current workspace,
    so finding none is fine.
    """
    if not desktop_id:
        return ""
    stem = desktop_id.removesuffix(".desktop").lower()
    names = {stem, stem.rsplit(".", 1)[-1]}
    matches: list[wm.Window] = []
    for _ in range(12):
        await asyncio.sleep(0.25)
        try:
            wins = await wm.get().windows()
        except RuntimeError:
            return ""
        matches = [w for w in wins
                   if names & {c.lower() for c in w.classes}]
        if matches:
            break
    if not matches:
        return ""
    pick = next((w for w in matches if w.active), matches[0])
    try:
        await wm.get().focus_window(pick.id)
    except RuntimeError:
        return ""
    return f", on workspace {pick.workspace}"


async def open_target(target: str, cwd: str) -> tuple[bool, str]:
    """Open a link or a file in its usual program and bring that window
    forward on its workspace. Relative paths are from `cwd`. Returns
    (ok, what happened) — the `open` tool's reply, or why it failed.
    Shared with the call overlay, where clicking a link does the same."""
    target = target.strip()
    if re.match(r"(https?|mailto):", target):
        kind, arg = "x-scheme-handler/" + target.split(":")[0], target
    else:
        path = os.path.expanduser(target.removeprefix("file://"))
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        if not os.path.exists(path):
            return False, f"no such file: {path}"
        _, out, _ = await run(["xdg-mime", "query", "filetype", path])
        kind, arg = out.decode().strip(), path
    _, out, _ = await run(["xdg-mime", "query", "default", kind])
    app = out.decode().strip()
    proc.launch(["xdg-open", arg], app_id="xdg-open")
    where = await bring_forward(app)
    return True, f"Opened {target} with {app or 'its default app'}{where}."
