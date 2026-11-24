"""The desktop, for what the shell does there on the agent's behalf and
the user's: running a program, asking qtile, opening a link or a file in
its usual app and bringing that window forward on its workspace.

laptop.py's hands use these (the hub's agent acting on the laptop), and
the call opens a link clicked in a reply with `open_target`.
"""

import asyncio
import json
import os
import re
from typing import Any

import skia

from ..services import proc


async def run(argv: list[str], *, data: bytes | None = None,
               timeout: float = 15.0) -> tuple[int, bytes, str]:
    """A subprocess's exit code, stdout bytes and stderr text."""
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return 127, b"", str(e)
    try:
        out, err = await asyncio.wait_for(p.communicate(data), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, b"", f"{argv[0]} timed out"
    return p.returncode or 0, out, err.decode(errors="replace").strip()


async def qtile(*args: str) -> Any:
    """`qtile cmd-obj ARGS`, its answer as JSON where it is."""
    rc, out, err = await run(["qtile", "cmd-obj", *args])
    if rc != 0:
        raise RuntimeError(err or f"qtile cmd-obj {' '.join(args)} failed")
    text = out.decode(errors="replace").strip()
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def decode(data: bytes) -> skia.Image | None:
    """Decode now, from a copy. `MakeFromEncoded` on a Python `bytes`
    wraps the buffer without copying and decodes lazily, so an image kept
    past the call decodes freed memory — it came out blank."""
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    return image.makeRasterImage() if image is not None else None


async def bring_forward(desktop_id: str) -> str:
    """Show the window of the app behind `desktop_id` (what xdg-open
    just handed something to) on its workspace, and focus it.

    Matched by WM_CLASS against the desktop id's name ("firefox" from
    firefox.desktop, "celluloid" from io.github…Celluloid.desktop). A
    browser opens a link as a tab in its last window, which is the
    focused window of its workspace; an app that was not running yet
    maps its window on the current workspace, so finding none is fine.
    """
    if not desktop_id:
        return ""
    stem = desktop_id.removesuffix(".desktop").lower()
    names = {stem, stem.rsplit(".", 1)[-1]}
    matches: list = []
    for _ in range(12):
        await asyncio.sleep(0.25)
        try:
            wins = await qtile("-o", "root", "-f", "windows") or []
        except RuntimeError:
            return ""
        matches = [w for w in wins
                   if names & {c.lower() for c in w.get("wm_class") or []}]
        if matches:
            break
    if not matches:
        return ""
    try:
        groups = await qtile("-o", "root", "-f", "get_groups") or {}
        pick = next((w for w in matches
                     if (groups.get(str(w.get("group"))) or {}).get("focus")
                     == w.get("name")), matches[0])
        await qtile("-o", "group", str(pick["group"]), "-f", "toscreen")
        await qtile("-o", "window", str(pick["id"]), "-f", "focus")
    except (RuntimeError, KeyError, TypeError):
        return ""
    return f", on workspace {pick['group']}"


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
