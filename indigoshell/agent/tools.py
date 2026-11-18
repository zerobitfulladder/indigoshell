"""The desktop, as tools for the call's agent.

In-process MCP tools — the Agent SDK's `create_sdk_mcp_server`, like the
agent's own `hang_up` — so they run on the shell's loop with the shell's
windows and services at hand, and cost no extra process. The agent sees
them as `mcp__shell__<name>`. They are pre-approved: each is either
read-only or a small, visible desktop action (open a link, switch a
workspace, skip a song). Nothing here clicks or types for you.

    screenshot          look at the screen, a window or a region
    open                a link or a file (a report, an image, a video) in
                        its usual program, brought forward on its workspace
    notify              a toast
    launch_app          by name, from the launcher's catalogue
    list_windows        workspaces and the windows on them
    switch_workspace    go to one
    focus_window        bring one forward
    read_clipboard / write_clipboard
    spotify             the desktop app: what is playing, play, pause, skip
    spotify_play        a track, album or playlist in the app, by link or URI
    spotify_search      the catalogue
    spotify_login       sign in once, for the account tools below
    spotify_now / spotify_queue / spotify_playlists / spotify_library
                        playback state, the queue, playlists, liked songs,
                        top and recent tracks
    spotify_api         any other Web API call (volume, devices, ...)
    lights              the WiZ lights at home and what each shows
    lights_set          on/off, brightness, white temperature, colour or
                        a built-in scene, for one light, a room or all
    lights_identify     blink lights, so the user can say which is which
    lights_name         give a light a name and a room
    browser_*           the everyday Firefox, with the user's sign-ins:
                        tabs, reading pages, clicking, filling forms,
                        keys, uploading files, JavaScript, screenshots
    phone_files         folders on the user's phone, over Wi-Fi (MiXplorer's
                        SFTP server, when the user has it on)
    phone_pull          copy from the phone to this computer
    phone_push          copy from this computer to the phone

What each Spotify tool needs — nothing, the app's credentials, or the
user's sign-in — is set out in agent/spotify.py; how the lights are
found and named, in agent/wiz.py; how Firefox is reached, in
agent/firefox.py; how the phone is set up and found, in agent/phone.py.
"""

import asyncio
import base64
import json
import logging
import os
import re
from typing import Any

import skia

from ..services import clipboard, proc
from .spotify import Spotify, SpotifyError, track_line, uri as spotify_uri
from .firefox import Firefox, FirefoxError
from .phone import DEPTH_MAX, LIST_DEFAULT, LIST_MAX, Phone, PhoneError
from .wiz import COLORS, Wiz, WizError, parse_color, parse_scene

log = logging.getLogger(__name__)

SERVER = "shell"
SHOT_MAX_SIDE = 1600       # what the model gets; the screen is 2560 wide
SHOT_QUALITY = 85
PROMPT = """\
You can also use this desktop through the `shell` tools: take a \
screenshot to see what is on screen; `open` links and files (reports, \
images, videos) in their usual program, which brings that window \
forward; launch apps; list and switch workspaces and windows; use the \
clipboard; and use Spotify (the desktop app, the catalogue, and, once \
the user is signed in (spotify_login), their account: the queue, \
playlists, liked songs, playback on any device); and the WiZ lights at \
home (`lights` to see them, `lights_set` to change them: colours, \
white temperatures, brightness, built-in scenes). Lights have no names \
until you give them some with the user: `lights_identify` blinks one, \
they tell you what it is, `lights_name` remembers it. The user's own \
Firefox is yours too, signed in as them, through the `browser_*` tools: \
list and open tabs, read a page (links, fields and buttons come back \
numbered, {12 button "Sign in"}, and the other tools take those \
numbers), click, fill forms, press keys, upload a local file into a \
page (sending it on WhatsApp, attaching it to a mail), run JavaScript \
in a page, and screenshot a tab. Prefer them to fetching a page yourself when the user \
means something in their browser or behind their sign-in. Outside \
Firefox, you cannot click or type on the user's behalf. The user's \
phone is reachable over Wi-Fi while its SFTP server is on in \
MiXplorer: `phone_files` lists and searches its folders, `phone_pull` \
copies files from it to this computer, `phone_push` sends files to \
it. Screenshots include the call overlay in the top-right \
corner. How the user wants to be shown things is in your CLAUDE.md."""


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def _error(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}], "is_error": True}


async def _run(argv: list[str], *, data: bytes | None = None,
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


async def _qtile(*args: str) -> Any:
    rc, out, err = await _run(["qtile", "cmd-obj", *args])
    if rc != 0:
        raise RuntimeError(err or f"qtile cmd-obj {' '.join(args)} failed")
    text = out.decode(errors="replace").strip()
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def _encode(image: skia.Image, max_side: int) -> tuple[str, int, int]:
    """JPEG base64 of `image`, scaled so its long side is at most
    `max_side`. Returns (data, width, height)."""
    w, h = image.width(), image.height()
    scale = min(1.0, max_side / max(w, h))
    tw, th = max(1, round(w * scale)), max(1, round(h * scale))
    if scale < 1.0:
        surface = skia.Surface(tw, th)
        surface.getCanvas().drawImageRect(
            image, skia.Rect.MakeWH(tw, th),
            skia.SamplingOptions(skia.FilterMode.kLinear,
                                 skia.MipmapMode.kLinear))
        image = surface.makeImageSnapshot()
    data = image.encodeToData(skia.EncodedImageFormat.kJPEG, SHOT_QUALITY)
    return base64.b64encode(bytes(data)).decode(), tw, th


def decode(data: bytes) -> skia.Image | None:
    """Decode now, from a copy. `MakeFromEncoded` on a Python `bytes`
    wraps the buffer without copying and decodes lazily, so an image kept
    past the call decodes freed memory — it came out blank."""
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    return image.makeRasterImage() if image is not None else None


# ── windows ─────────────────────────────────────────────────────────────
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
            wins = await _qtile("-o", "root", "-f", "windows") or []
        except RuntimeError:
            return ""
        matches = [w for w in wins
                   if names & {c.lower() for c in w.get("wm_class") or []}]
        if matches:
            break
    if not matches:
        return ""
    try:
        groups = await _qtile("-o", "root", "-f", "get_groups") or {}
        pick = next((w for w in matches
                     if (groups.get(str(w.get("group"))) or {}).get("focus")
                     == w.get("name")), matches[0])
        await _qtile("-o", "group", str(pick["group"]), "-f", "toscreen")
        await _qtile("-o", "window", str(pick["id"]), "-f", "focus")
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
        _, out, _ = await _run(["xdg-mime", "query", "filetype", path])
        kind, arg = out.decode().strip(), path
    _, out, _ = await _run(["xdg-mime", "query", "default", kind])
    app = out.decode().strip()
    proc.launch(["xdg-open", arg], app_id="xdg-open")
    where = await bring_forward(app)
    return True, f"Opened {target} with {app or 'its default app'}{where}."


class ShellTools:
    """The tool suite, handed to `Agent(tools=...)`."""

    prompt = PROMPT

    def __init__(self, *, spotify: Spotify | None = None,
                 lights: Wiz | None = None,
                 firefox: Firefox | None = None,
                 phone: Phone | None = None) -> None:
        self.spotify = spotify or Spotify()
        self.lights = lights or Wiz()
        self.firefox = firefox or Firefox()
        self.phone = phone or Phone()

    # ── the tools ───────────────────────────────────────────────────────
    def build(self, agent) -> list:
        from claude_agent_sdk import tool

        @tool("screenshot",
              "Look at the screen. target: 'screen' (default, every "
              "monitor), 'window' (the focused window) or 'region' (give "
              "x, y, width, height in screen pixels). The image may be "
              "scaled down; the reply says by how much.",
              {"type": "object",
               "properties": {
                   "target": {"type": "string",
                              "enum": ["screen", "window", "region"]},
                   "x": {"type": "integer"}, "y": {"type": "integer"},
                   "width": {"type": "integer"},
                   "height": {"type": "integer"}},
               "additionalProperties": False})
        async def screenshot(args):
            target = args.get("target") or "screen"
            crop = None
            try:
                if target == "window":
                    info = await _qtile("-o", "window", "-f", "info")
                    crop = (info["x"], info["y"], info["width"],
                            info["height"])
                elif target == "region":
                    crop = tuple(int(args[k]) for k in
                                 ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError, RuntimeError) as e:
                return _error(f"cannot find what to capture: {e}")
            argv = ["import", "-silent", "-window", "root"]
            if crop is not None:
                x, y, w, h = crop
                argv += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
            rc, data, err = await _run(argv + ["png:-"], timeout=20)
            image = decode(data) if rc == 0 else None
            if image is None:
                return _error(f"screenshot failed: {err or rc}")
            b64, tw, th = _encode(image, SHOT_MAX_SIDE)
            scale = tw / image.width()
            where = (f"region {crop[2]}x{crop[3]} at {crop[0]},{crop[1]}"
                     if crop else f"whole screen {image.width()}x{image.height()}")
            note = (f"{where}, shown at {tw}x{th}"
                    + (f" (scale {scale:.2f})" if scale < 1 else "") + ".")
            return {"content": [{"type": "image", "data": b64,
                                 "mimeType": "image/jpeg"},
                                {"type": "text", "text": note}]}

        @tool("notify", "Pop a notification toast.",
              {"type": "object",
               "properties": {"summary": {"type": "string"},
                              "body": {"type": "string"}},
               "required": ["summary"], "additionalProperties": False})
        async def notify(args):
            rc, _, err = await _run(["notify-send", "-a", agent.name,
                                     str(args["summary"]),
                                     str(args.get("body") or "")])
            return _text("Shown.") if rc == 0 else _error(err or "failed")

        @tool("open",
              "Open something for the user to look at, in the program that "
              "normally opens it, and bring that window forward, switching "
              "to its workspace. target: a link (http, https, mailto) or a "
              "file (an HTML report, an image, a video, a PDF); relative "
              "paths are from your working directory. Pages, images and "
              "PDFs open in Firefox here, most videos in a video player.",
              {"target": str})
        async def open_(args):
            ok, said = await open_target(str(args.get("target", "")),
                                         agent.workspace)
            return _text(said) if ok else _error(said)

        @tool("launch_app",
              "Start an application by name (fuzzy, as the launcher "
              "matches it), e.g. 'firefox', 'files', 'spotify'.",
              {"name": str})
        async def launch_app(args):
            from ..services.apps import Catalog
            catalog = getattr(self, "_catalog", None) or Catalog()
            self._catalog = catalog
            catalog.refresh()
            results = catalog.search(str(args.get("name", "")))
            if not results:
                return _error("no installed application matches that")
            app = results[0].app
            catalog.launch(app)
            return _text(f"Started {app.entry.name}.")

        @tool("list_windows",
              "Workspaces (qtile groups) and the windows on each, with "
              "window ids for focus_window, and which workspace is shown.",
              {})
        async def list_windows(args):
            try:
                groups = await _qtile("-o", "root", "-f", "get_groups")
                wins = await _qtile("-o", "root", "-f", "windows")
                current = await _qtile("-o", "group", "-f", "info")
            except RuntimeError as e:
                return _error(str(e))
            by_group: dict[str, list] = {}
            for w in wins or []:
                by_group.setdefault(str(w.get("group")), []).append(w)
            lines = []
            for name in groups or {}:
                mark = " (shown)" if current and current.get("name") == name else ""
                lines.append(f"workspace {name}{mark}")
                for w in by_group.get(name, []):
                    cls = (w.get("wm_class") or ["?"])[-1]
                    lines.append(f"  [{w['id']}] {cls}: {w.get('name', '')}")
            return _text("\n".join(lines) or "no workspaces")

        @tool("switch_workspace", "Show a workspace (qtile group) by name.",
              {"name": str})
        async def switch_workspace(args):
            name = str(args.get("name", "")).strip()
            try:
                await _qtile("-o", "group", name, "-f", "toscreen")
            except RuntimeError as e:
                return _error(str(e))
            return _text(f"Workspace {name} is shown.")

        @tool("focus_window",
              "Bring a window forward by its id from list_windows, "
              "switching to its workspace.", {"window_id": int})
        async def focus_window(args):
            wid = str(int(args.get("window_id", 0)))
            try:
                info = await _qtile("-o", "window", wid, "-f", "info")
                await _qtile("-o", "group", str(info["group"]),
                             "-f", "toscreen")
                await _qtile("-o", "window", wid, "-f", "focus")
            except (RuntimeError, KeyError, TypeError) as e:
                return _error(f"cannot focus it: {e}")
            return _text(f"Focused {info.get('name', wid)}.")

        @tool("read_clipboard", "The clipboard's text.", {})
        async def read_clipboard(args):
            rc, out, err = await _run(["xclip", "-selection", "clipboard",
                                       "-o"], timeout=5)
            if rc != 0:
                return _error(err or "the clipboard is empty or not text")
            return _text(out.decode(errors="replace")[:20000])

        @tool("write_clipboard", "Put text on the clipboard.", {"text": str})
        async def write_clipboard(args):
            ok = clipboard.copy(str(args.get("text", "")))
            return _text("Copied.") if ok else _error("xclip is missing")

        tools = [screenshot, notify, open_, launch_app, list_windows,
                 switch_workspace, focus_window, read_clipboard,
                 write_clipboard]
        return (tools + self._spotify_tools(agent) + self._light_tools()
                + self._browser_tools() + self._phone_tools())

    # ── phone ───────────────────────────────────────────────────────────
    def _phone_tools(self) -> list:
        """Files on the user's phone, over SFTP — agent/phone.py."""
        from claude_agent_sdk import tool
        phone = self.phone

        def paths(value) -> list[str]:
            if isinstance(value, str):
                value = [value]
            return [str(v) for v in value or [] if str(v).strip()]

        @tool("phone_files",
              "List a folder on the user's phone, or search it. path: a "
              "folder, relative to where the phone's server opens (usually "
              "internal storage: DCIM/Camera, Download, Pictures/"
              "Screenshots, Documents, ...) or absolute; empty for that "
              "root. pattern: match file names (*.pdf, *invoice*). depth: "
              f"how many folders down to look (0 = just this one, max "
              f"{DEPTH_MAX}). newest: newest first; 'the photo I just "
              f"took' is DCIM/Camera, newest, limit 1. limit: entries "
              f"(default {LIST_DEFAULT}, max {LIST_MAX}). Works only while "
              "the user has MiXplorer's SFTP server on.",
              {"type": "object", "properties": {
                  "path": {"type": "string"},
                  "pattern": {"type": "string"},
                  "depth": {"type": "integer", "minimum": 0,
                            "maximum": DEPTH_MAX},
                  "newest": {"type": "boolean"},
                  "limit": {"type": "integer", "minimum": 1,
                            "maximum": LIST_MAX}},
               "additionalProperties": False})
        async def phone_files(args):
            try:
                return _text(await phone.list(
                    str(args.get("path") or ""),
                    pattern=str(args.get("pattern") or ""),
                    depth=int(args.get("depth") or 0),
                    newest=bool(args.get("newest")),
                    limit=int(args.get("limit") or LIST_DEFAULT)))
            except PhoneError as e:
                return _error(str(e))

        @tool("phone_pull",
              "Copy files or folders from the phone to this computer. "
              "paths: phone paths as phone_files gives them; wildcards "
              "work (DCIM/Camera/IMG_20261118_*). to: a local folder "
              "(default ~/Downloads, created if missing). An existing "
              "name is never replaced unless overwrite is true: the copy "
              "gets ' (1)' instead. Answers with the local paths.",
              {"type": "object", "properties": {
                  "paths": {"type": "array", "items": {"type": "string"},
                            "minItems": 1},
                  "to": {"type": "string"},
                  "overwrite": {"type": "boolean"}},
               "required": ["paths"], "additionalProperties": False})
        async def phone_pull(args):
            try:
                return _text(await phone.pull(
                    paths(args.get("paths")),
                    str(args.get("to") or "~/Downloads"),
                    overwrite=bool(args.get("overwrite"))))
            except PhoneError as e:
                return _error(str(e))

        @tool("phone_push",
              "Send files or folders from this computer to the phone. "
              "paths: absolute local paths (or ~/...); wildcards work. "
              "to: a folder on the phone (default its Download folder), "
              "created if missing. A name already there is skipped unless "
              "overwrite is true.",
              {"type": "object", "properties": {
                  "paths": {"type": "array", "items": {"type": "string"},
                            "minItems": 1},
                  "to": {"type": "string"},
                  "overwrite": {"type": "boolean"}},
               "required": ["paths"], "additionalProperties": False})
        async def phone_push(args):
            try:
                return _text(await phone.push(
                    paths(args.get("paths")), str(args.get("to") or ""),
                    overwrite=bool(args.get("overwrite"))))
            except PhoneError as e:
                return _error(str(e))

        return [phone_files, phone_pull, phone_push]

    # ── firefox ─────────────────────────────────────────────────────────
    def _browser_tools(self) -> list:
        """The everyday Firefox, through the indigoshell bridge add-on —
        agent/firefox.py."""
        from claude_agent_sdk import tool
        ff = self.firefox
        TAB = {"type": "string",
               "description": "'active' (default: the tab the user is "
                              "looking at), a number from browser_tabs, "
                              "or words from its title or address"}

        def guard(fn):
            async def run(args):
                try:
                    return await fn(args)
                except FirefoxError as e:
                    return _error(str(e))
            run.__name__ = fn.__name__
            return run

        @tool("browser_tabs",
              "List the tabs open in the user's Firefox: number, title, "
              "address, and which one they are looking at.",
              {"type": "object", "properties": {},
               "additionalProperties": False})
        @guard
        async def browser_tabs(args):
            tabs = await ff.tabs()
            lines = [f"{t['n']}. {t['title'] or '(untitled)'} | {t['url']}"
                     + ("   ← looking at it" if t["focused"]
                        else "   (showing in its window)" if t["active"]
                        else "")
                     for t in tabs]
            return _text("\n".join(lines) or "no tabs")

        @tool("browser_open",
              "Open an address in the user's Firefox, in a new tab "
              "(default) or, with new_tab false, in `tab`, and show it "
              "to them.",
              {"type": "object",
               "properties": {"url": {"type": "string"},
                              "new_tab": {"type": "boolean"}, "tab": TAB},
               "required": ["url"], "additionalProperties": False})
        @guard
        async def browser_open(args):
            tab = None if args.get("new_tab", True) else await ff.tab(args.get("tab"))
            t = await ff.call("open", url=args["url"], tab=tab)
            where = await bring_forward("firefox.desktop")
            return _text(f"Opened {t['title'] or '(untitled)'} | {t['url']}{where}.")

        @tool("browser_switch",
              "Bring a Firefox tab to the front and show the user its "
              "window.",
              {"type": "object", "properties": {"tab": TAB},
               "required": ["tab"], "additionalProperties": False})
        @guard
        async def browser_switch(args):
            tab = await ff.tab(args.get("tab"))
            await ff.call("show", tab=tab)
            where = await bring_forward("firefox.desktop")
            return _text(f"Showing {await ff.where(tab)}{where}.")

        @tool("browser_close", "Close a Firefox tab.",
              {"type": "object", "properties": {"tab": TAB},
               "required": ["tab"], "additionalProperties": False})
        @guard
        async def browser_close(args):
            tab = await ff.tab(args.get("tab"))
            was = await ff.where(tab)
            await ff.call("close", tab=tab)
            return _text(f"Closed {was}.")

        @tool("browser_go",
              "In a Firefox tab: go to an address, or 'back', 'forward' "
              "or 'reload'.",
              {"type": "object",
               "properties": {"to": {"type": "string"}, "tab": TAB},
               "required": ["to"], "additionalProperties": False})
        @guard
        async def browser_go(args):
            tab = await ff.tab(args.get("tab"))
            t = await ff.call("go", tab=tab, to=args["to"])
            return _text(f"Now at {t['title'] or '(untitled)'} | {t['url']}.")

        @tool("browser_read",
              "Read a page in the user's Firefox as text: headings, text, "
              "lists, tables, and every link, field and button numbered "
              "in braces, like {12 input:email \"Email\" = \"\"}. The "
              "numbers are what browser_click and browser_fill take, until "
              "the page is read again. selector narrows it to part of the "
              "page (CSS); max_chars defaults to 12000.",
              {"type": "object",
               "properties": {"tab": TAB, "selector": {"type": "string"},
                              "max_chars": {"type": "integer"}},
               "additionalProperties": False})
        @guard
        async def browser_read(args):
            tab = await ff.tab(args.get("tab"))
            return _text(await ff.read(tab, int(args.get("max_chars") or 12000),
                                       args.get("selector")))

        @tool("browser_click",
              "Click something on a page: by its number from browser_read "
              "(ref), a CSS selector, or the text on it. Waits for any page "
              "it opens.",
              {"type": "object",
               "properties": {"tab": TAB, "ref": {"type": "integer"},
                              "selector": {"type": "string"},
                              "text": {"type": "string"},
                              "double": {"type": "boolean"}},
               "additionalProperties": False})
        @guard
        async def browser_click(args):
            tab = await ff.tab(args.get("tab"))
            await ff.page(tab, "click", wait=6, ref=args.get("ref"),
                          selector=args.get("selector"), text=args.get("text"),
                          double=bool(args.get("double")))
            return _text(f"Clicked. Now at {await ff.where(tab)}.")

        @tool("browser_fill",
              "Fill form fields: each by ref (from browser_read) or CSS "
              "selector, with a value. Text goes in as typing would; a "
              "select takes an option's text or value; a checkbox or "
              "radio takes true/false. submit then submits the form, by "
              "its own submit button where it has one.",
              {"type": "object",
               "properties": {
                   "tab": TAB,
                   "fields": {"type": "array", "items": {
                       "type": "object",
                       "properties": {"ref": {"type": "integer"},
                                      "selector": {"type": "string"},
                                      "value": {"type": ["string", "boolean",
                                                         "number"]}},
                       "required": ["value"]}},
                   "submit": {"type": "boolean"}},
               "required": ["fields"], "additionalProperties": False})
        @guard
        async def browser_fill(args):
            tab = await ff.tab(args.get("tab"))
            fields = []
            for f in args.get("fields") or []:
                if f.get("ref") is None and not f.get("selector"):
                    return _error("each field needs a ref or a selector")
                fields.append({k: f[k] for k in ("ref", "selector", "value")
                               if f.get(k) is not None})
            res = await ff.page(tab, "fill", wait=8 if args.get("submit") else 0,
                                fields=fields, submit=bool(args.get("submit")))
            tail = (f" and submitted; now at {await ff.where(tab)}"
                    if res.get("submitted") else "")
            return _text(f"Filled {', '.join(res.get('done', []))}{tail}.")

        @tool("browser_keys",
              "Press keys in a Firefox tab, in order, on whatever has "
              "focus: characters, or names like Enter, Tab, Escape, "
              "Backspace, ArrowDown, PageDown, and chords like Control+a. "
              "scroll (in screens, negative for up) scrolls instead.",
              {"type": "object",
               "properties": {"tab": TAB,
                              "keys": {"type": "array",
                                       "items": {"type": "string"}},
                              "scroll": {"type": "number"}},
               "additionalProperties": False})
        @guard
        async def browser_keys(args):
            tab = await ff.tab(args.get("tab"))
            if args.get("scroll"):
                await ff.page(tab, "scroll", pages=float(args["scroll"]))
                return _text("Scrolled.")
            keys = [str(k) for k in args.get("keys") or []]
            if not keys:
                return _error("give keys or scroll")
            await ff.page(tab, "keys", wait=4, keys=keys)
            return _text(f"Pressed. Now at {await ff.where(tab)}.")

        @tool("browser_upload",
              "Put a local file into a page's upload box, as picking it in "
              "the file dialog would: to send it in a chat, attach it to "
              "a mail, upload it to a form. path is the file. ref (an "
              "upload box from browser_read, which lists hidden ones too) "
              "or selector says where; leave both out and the page's upload "
              "box that takes this kind of file is used. Many apps "
              "(WhatsApp, Gmail) only create their upload boxes once their "
              "attach menu is opened: click that first. drop instead drops "
              "the file onto ref/selector, as if dragged in. Afterwards the "
              "app usually shows a preview: press its send button.",
              {"type": "object",
               "properties": {"tab": TAB, "path": {"type": "string"},
                              "ref": {"type": "integer"},
                              "selector": {"type": "string"},
                              "drop": {"type": "boolean"}},
               "required": ["path"], "additionalProperties": False})
        @guard
        async def browser_upload(args):
            tab = await ff.tab(args.get("tab"))
            res = await ff.upload(tab, args["path"], ref=args.get("ref"),
                                  selector=args.get("selector"),
                                  drop=bool(args.get("drop")))
            kb = res["size"] / 1024
            how = ("dropped onto " + res["into"] if res.get("how") == "drop"
                   else f"in the upload box ({res['into']})")
            return _text(f"{os.path.basename(args['path'])} ({kb:.0f} KB) is "
                         f"{how}. Now at {await ff.where(tab)}.")

        @tool("browser_js",
              "Run JavaScript in a page of the user's Firefox, in the "
              "page's own context (its variables, its DOM, signed in as "
              "the user). The value of the last expression comes back as "
              "JSON; for promises, end with an awaited expression inside "
              "an async function you call.",
              {"type": "object",
               "properties": {"tab": TAB, "code": {"type": "string"}},
               "required": ["code"], "additionalProperties": False})
        @guard
        async def browser_js(args):
            tab = await ff.tab(args.get("tab"))
            return _text(await ff.js(tab, args["code"]))

        @tool("browser_screenshot",
              "See what a Firefox tab shows (its visible area), even when "
              "it is not on screen.",
              {"type": "object", "properties": {"tab": TAB},
               "additionalProperties": False})
        @guard
        async def browser_screenshot(args):
            tab = await ff.tab(args.get("tab"))
            image = decode(await ff.screenshot(tab))
            if image is None:
                return _error("could not decode the screenshot")
            b64, tw, th = _encode(image, SHOT_MAX_SIDE)
            return {"content": [{"type": "image", "data": b64,
                                 "mimeType": "image/jpeg"},
                                {"type": "text", "text":
                                 f"{await ff.where(tab)}, {tw}x{th}."}]}

        return [browser_tabs, browser_open, browser_switch, browser_close,
                browser_go, browser_read, browser_click, browser_fill,
                browser_keys, browser_upload, browser_js, browser_screenshot]

    # ── lights ──────────────────────────────────────────────────────────
    def _light_tools(self) -> list:
        """The WiZ bulbs on the LAN — see agent/wiz.py."""
        from claude_agent_sdk import tool
        wiz = self.lights

        async def found(target: str | None = None):
            # A command for lights it knows goes straight to them; a full
            # search (~1s) only for the list, or when that falls short.
            if target is not None:
                bulbs = await wiz.discover(sweep=False)
                try:
                    if bulbs and len(bulbs) == len(wiz.remembered()):
                        return wiz.resolve(target, bulbs)
                except WizError:
                    pass
            bulbs = await wiz.discover()
            if not bulbs:
                raise WizError("no WiZ lights answered on the local network")
            return bulbs if target is None else wiz.resolve(target, bulbs)

        @tool("lights",
              "List the WiZ lights at home: each one's name and room (if "
              "given), what it shows now, whether it does colour or only "
              "white, and its white range.",
              {"type": "object", "properties": {},
               "additionalProperties": False})
        async def lights(args):
            try:
                bulbs = await found()
            except WizError as e:
                return _error(str(e))
            unnamed = sum(1 for b in bulbs if not b.name)
            tail = (f"\n{unnamed} unnamed: lights_identify + lights_name "
                    "to name them with the user." if unnamed else "")
            return _text("\n".join(b.line() for b in bulbs) + tail)

        @tool("lights_set",
              "Change WiZ lights. target: 'all', or light names, rooms, "
              "MACs or addresses, comma-separated. Give any of: on "
              "(false turns off), brightness (10-100 %), temp (white, in "
              "kelvin, e.g. 2700 warm .. 6500 cool), color ('#rrggbb' or "
              f"a word: {', '.join(COLORS)}), colors (a list, spread "
              "over the targets in turn), scene (a built-in scene by "
              "name, e.g. Cozy, Fireplace, Ocean, Party, Club, Night "
              "light, Focus, Relax, Sunset, Candlelight; with speed "
              "10-200 for moving ones). Setting anything turns a light "
              "on. White-only lights take temp, not colours.",
              {"type": "object",
               "properties": {
                   "target": {"type": "string"},
                   "on": {"type": "boolean"},
                   "brightness": {"type": "integer"},
                   "temp": {"type": "integer"},
                   "color": {"type": "string"},
                   "colors": {"type": "array", "items": {"type": "string"}},
                   "scene": {"type": "string"},
                   "speed": {"type": "integer"}},
               "required": ["target"], "additionalProperties": False})
        async def lights_set(args):
            try:
                # Words that mean nothing are wrong for every light: say
                # so once, before asking any of them.
                if args.get("scene") is not None:
                    parse_scene(args["scene"])
                for c in (args.get("colors") or []) + (
                        [args["color"]] if args.get("color") else []):
                    parse_color(c)
                bulbs = await found(args.get("target") or "all")
                colors = args.get("colors") or []
                plans, skipped = [], []
                for i, b in enumerate(bulbs):
                    color = colors[i % len(colors)] if colors \
                        else args.get("color")
                    try:
                        plans.append((b, wiz.plan(
                            b, on=args.get("on"),
                            brightness=args.get("brightness"),
                            temp=args.get("temp"), color=color,
                            scene=args.get("scene"),
                            speed=args.get("speed"))))
                    except WizError as e:
                        skipped.append(f"{b.label}: {e}")
                if not plans:
                    return _error("\n".join(skipped) or "nothing to change")
                results = await wiz.apply(plans)
            except WizError as e:
                return _error(str(e))
            lines = [f"{b.label}: " + (f"failed: {err}" if err
                                       else b.showing())
                     for b, err in results] + skipped
            failed = all(err for _, err in results)
            return (_error if failed else _text)("\n".join(lines))

        @tool("lights_identify",
              "Blink WiZ lights twice, then put them back as they were, so "
              "the user can tell you which light is which. target as for "
              "lights_set; one light at a time is clearest.",
              {"type": "object",
               "properties": {"target": {"type": "string"}},
               "required": ["target"], "additionalProperties": False})
        async def lights_identify(args):
            try:
                bulbs = await found(args.get("target") or "")
                await wiz.identify(bulbs)
            except WizError as e:
                return _error(str(e))
            return _text("Blinked " + ", ".join(b.label for b in bulbs)
                         + " and put back as before.")

        @tool("lights_name",
              "Give a WiZ light a name, and optionally a room, so it can "
              "be addressed by them (rooms address every light in them). "
              "light: its current name, MAC or address.",
              {"type": "object",
               "properties": {"light": {"type": "string"},
                              "name": {"type": "string"},
                              "room": {"type": "string"}},
               "required": ["light", "name"],
               "additionalProperties": False})
        async def lights_name(args):
            name = str(args.get("name", "")).strip()
            if not name or "," in name:
                return _error("a name, without commas")
            try:
                hit = await found(args.get("light", ""))
            except WizError as e:
                return _error(str(e))
            if len(hit) != 1:
                return _error("that matches "
                              + ", ".join(b.label for b in hit)
                              + "; name one light at a time")
            room = args.get("room")
            wiz.name(hit[0].mac, name, room.strip() if room else None)
            return _text(f"{hit[0].mac} is now {name!r}"
                         + (f" in {room.strip()!r}." if room else "."))

        return [lights, lights_set, lights_identify, lights_name]

    # ── spotify ─────────────────────────────────────────────────────────
    def _spotify_tools(self, agent) -> list:
        """The app over MPRIS, the catalogue, and the user's account —
        see agent/spotify.py for which needs what."""
        from claude_agent_sdk import tool
        sp = self.spotify

        def failed(status: int, data) -> dict:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                msg = err.get("message", "")
                if err.get("reason") == "PREMIUM_REQUIRED":
                    msg += " (needs Premium)"
            else:
                msg = str(err or data or "")
            if status == 404 and "device" in msg.lower():
                msg += ". Open Spotify on a device first, or play something"
            return _error(f"Spotify {status}: {msg}"[:600])

        def playlist_id(value: str) -> str:
            value = (spotify_uri(value) or value).strip()
            return value.rsplit(":", 1)[-1]

        async def call(method, path, **kw):
            try:
                return await sp.api(method, path, **kw)
            except SpotifyError as e:
                return None, str(e)

        @tool("spotify",
              "The Spotify desktop app: action 'status' (what is playing), "
              "'play', 'pause', 'toggle', 'next' or 'previous'. Starts the "
              "app if needed. Needs no sign-in.",
              {"type": "object",
               "properties": {"action": {"type": "string", "enum": [
                   "status", "play", "pause", "toggle", "next",
                   "previous"]}},
               "required": ["action"], "additionalProperties": False})
        async def spotify(args):
            action = args.get("action")
            if action != "status":
                if not await sp.app_start():
                    return _error("Spotify did not start")
                rc, out = await sp.app({"toggle": "play-pause"}.get(action,
                                                                   action))
                if rc != 0:
                    return _error(out or f"{action} failed")
                await asyncio.sleep(0.4)
            if not await sp.app_running():
                return _text("Spotify is not running.")
            rc, out = await sp.app(
                "metadata", "--format",
                "{{status}}\t{{artist}}\t{{title}}\t{{album}}")
            parts = out.split("\t")
            if rc != 0 or len(parts) < 3:
                return _text(f"Spotify is open; nothing loaded ({out}).")
            status, artist, title = parts[:3]
            album = parts[3] if len(parts) > 3 else ""
            return _text(f"{status}: {title} by {artist}"
                         + (f" ({album})" if album else ""))

        @tool("spotify_play",
              "Play a track, album, playlist, artist or episode in the "
              "Spotify app now, from a spotify: URI or an open.spotify.com "
              "link. Starts the app if needed. Needs no sign-in.",
              {"uri": str})
        async def spotify_play(args):
            target = spotify_uri(str(args.get("uri", "")))
            if target is None:
                return _error("not a spotify: URI or open.spotify.com link")
            if not await sp.app_start():
                return _error("Spotify did not start")
            rc, out = await sp.app("open", target)
            return _text(f"Playing {target}.") if rc == 0 else _error(
                out or "Spotify refused it")

        @tool("spotify_search",
              "Search Spotify. kind: track (default), album, playlist, "
              "artist, show or episode. Returns names with spotify: URIs.",
              {"type": "object",
               "properties": {"query": {"type": "string"},
                              "kind": {"type": "string", "enum": [
                                  "track", "album", "playlist", "artist",
                                  "show", "episode"]},
                              "limit": {"type": "integer"}},
               "required": ["query"], "additionalProperties": False})
        async def spotify_search(args):
            kind = args.get("kind") or "track"
            status, data = await call(
                "GET", "search", user=sp.signed_in(),
                params={"q": args["query"], "type": kind,
                        "limit": max(1, min(int(args.get("limit") or 8), 20))})
            if status is None:
                return _error(f"{data} Meanwhile: find the open.spotify.com "
                              "link with web search and use spotify_play.")
            if status != 200:
                return failed(status, data)
            items = (data.get(f"{kind}s") or {}).get("items") or []
            return _text("\n".join(track_line(i) for i in items if i)
                         or "nothing found")

        @tool("spotify_login",
              "Sign the user in to Spotify for the account tools (queue, "
              "playlists, library, playback state): opens Spotify's "
              "approval page in their browser and waits for them to accept "
              "(up to four minutes). Tell them first, on the call.", {})
        async def spotify_login(args):
            try:
                who = await sp.login(
                    lambda url: proc.launch(["xdg-open", url],
                                            app_id="xdg-open"))
            except SpotifyError as e:
                return _error(str(e))
            return _text(f"Signed in to Spotify as {who}.")

        @tool("spotify_now",
              "What is playing on the user's Spotify account, on which "
              "device, with progress, volume, shuffle and repeat. Needs "
              "the sign-in.", {})
        async def spotify_now(args):
            status, data = await call("GET", "me/player")
            if status is None:
                return _error(data)
            if status == 204 or not data:
                return _text("Nothing is playing on any device.")
            if status != 200:
                return failed(status, data)
            item = data.get("item") or {}
            dev = data.get("device") or {}

            def mmss(ms):
                return f"{int(ms or 0) // 60000}:{int(ms or 0) // 1000 % 60:02d}"
            return _text(
                f"{'Playing' if data.get('is_playing') else 'Paused'}: "
                f"{track_line(item)}\n"
                f"{mmss(data.get('progress_ms'))} / "
                f"{mmss(item.get('duration_ms'))} on {dev.get('name')} "
                f"({dev.get('type')}, volume {dev.get('volume_percent')}%), "
                f"shuffle {'on' if data.get('shuffle_state') else 'off'}, "
                f"repeat {data.get('repeat_state')}")

        @tool("spotify_queue",
              "Add tracks or episodes to the queue (spotify: URIs or "
              "links, in order), then show what is coming up. With no "
              "`add`, only shows the queue. Needs the sign-in and Premium.",
              {"type": "object",
               "properties": {"add": {"type": "array",
                                      "items": {"type": "string"}}},
               "additionalProperties": False})
        async def spotify_queue(args):
            for item in args.get("add") or []:
                target = spotify_uri(item)
                if target is None:
                    return _error(f"not a spotify URI or link: {item}")
                status, data = await call("POST", "me/player/queue",
                                          params={"uri": target})
                if status is None:
                    return _error(data)
                if status not in (200, 202, 204):
                    return failed(status, data)
            status, data = await call("GET", "me/player/queue")
            if status is None:
                return _error(data)
            if status != 200:
                return failed(status, data)
            now = data.get("currently_playing")
            lines = [f"now: {track_line(now)}" if now else "now: nothing"]
            lines += [f"{i}. {track_line(t)}"
                      for i, t in enumerate((data.get("queue") or [])[:12], 1)]
            return _text("\n".join(lines))

        @tool("spotify_playlists",
              "The user's playlists. action 'list' (theirs), 'show' (the "
              "tracks of `playlist`), 'create' (a new one named `name`, "
              "optionally with `uris` in it), 'add' or 'remove' (`uris` to "
              "or from `playlist`). `playlist` is an id, URI or link. "
              "Needs the sign-in.",
              {"type": "object",
               "properties": {
                   "action": {"type": "string", "enum": [
                       "list", "show", "create", "add", "remove"]},
                   "playlist": {"type": "string"},
                   "name": {"type": "string"},
                   "description": {"type": "string"},
                   "public": {"type": "boolean"},
                   "uris": {"type": "array", "items": {"type": "string"}}},
               "required": ["action"], "additionalProperties": False})
        async def spotify_playlists(args):
            action = args["action"]
            uris = [spotify_uri(u) or u for u in args.get("uris") or []]
            if action == "list":
                status, data = await call("GET", "me/playlists",
                                          params={"limit": 50})
                if status != 200:
                    return _error(data) if status is None else failed(status, data)
                return _text("\n".join(
                    f"{p['name']} ({(p.get('tracks') or p.get('items') or {}).get('total', '?')} "
                    f"tracks)  {p['uri']}" for p in data.get("items") or [] if p)
                    or "no playlists")
            if action == "create":
                user = sp.user_id()
                if not args.get("name"):
                    return _error("give the new playlist a name")
                status, data = await call(
                    "POST", f"users/{user}/playlists" if user else "me/playlists",
                    body={"name": args["name"],
                          "description": args.get("description", ""),
                          "public": bool(args.get("public", False))})
                if status not in (200, 201):
                    return _error(data) if status is None else failed(status, data)
                pid, link = data["id"], data.get("uri", "")
                if uris:
                    status, err = await call("POST", f"playlists/{pid}/tracks",
                                             body={"uris": uris[:100]})
                    if status not in (200, 201):
                        return failed(status, err)
                return _text(f"Created {args['name']}  {link}"
                             + (f" with {len(uris)} tracks" if uris else ""))
            if not args.get("playlist"):
                return _error("which playlist?")
            pid = playlist_id(args["playlist"])
            if action == "show":
                status, data = await call("GET", f"playlists/{pid}/tracks",
                                          params={"limit": 100})
                if status != 200:
                    return _error(data) if status is None else failed(status, data)
                return _text("\n".join(
                    f"{i}. {track_line(it.get('track') or it.get('item') or {})}"
                    for i, it in enumerate(data.get("items") or [], 1))
                    or "empty")
            if not uris:
                return _error("give the uris to add or remove")
            if action == "add":
                status, data = await call("POST", f"playlists/{pid}/tracks",
                                          body={"uris": uris[:100]})
            else:
                status, data = await call(
                    "DELETE", f"playlists/{pid}/tracks",
                    body={"tracks": [{"uri": u} for u in uris[:100]]})
            if status not in (200, 201):
                return _error(data) if status is None else failed(status, data)
            return _text(f"{'Added' if action == 'add' else 'Removed'} "
                         f"{len(uris)} tracks.")

        @tool("spotify_library",
              "The user's library and listening: action 'liked' (latest "
              "liked songs), 'like' or 'unlike' (`uris`), 'top' (their top "
              "tracks), 'recent' (recently played). Needs the sign-in.",
              {"type": "object",
               "properties": {
                   "action": {"type": "string", "enum": [
                       "liked", "like", "unlike", "top", "recent"]},
                   "uris": {"type": "array", "items": {"type": "string"}}},
               "required": ["action"], "additionalProperties": False})
        async def spotify_library(args):
            action = args["action"]
            if action in ("like", "unlike"):
                ids = [(spotify_uri(u) or u).rsplit(":", 1)[-1]
                       for u in args.get("uris") or []]
                if not ids:
                    return _error("give the uris")
                status, data = await call(
                    "PUT" if action == "like" else "DELETE", "me/tracks",
                    params={"ids": ",".join(ids[:50])})
                if status not in (200, 204):
                    return _error(data) if status is None else failed(status, data)
                return _text(f"{'Liked' if action == 'like' else 'Unliked'} "
                             f"{len(ids)}.")
            path, params = {
                "liked": ("me/tracks", {"limit": 20}),
                "top": ("me/top/tracks", {"limit": 20,
                                          "time_range": "medium_term"}),
                "recent": ("me/player/recently-played", {"limit": 20}),
            }[action]
            status, data = await call("GET", path, params=params)
            if status != 200:
                return _error(data) if status is None else failed(status, data)
            items = data.get("items") or []
            return _text("\n".join(
                f"{i}. {track_line(it.get('track', it))}"
                for i, it in enumerate(items, 1)) or "nothing")

        @tool("spotify_api",
              "Any other Spotify Web API call, as the user: method (GET, "
              "POST, PUT, DELETE), path under https://api.spotify.com/v1 "
              "(e.g. 'me/player/volume'), optional query and JSON body. "
              "For volume, shuffle, repeat, seek, devices and transfer, "
              "following artists, and anything the other tools lack. "
              "Needs the sign-in; playback control needs Premium.",
              {"type": "object",
               "properties": {
                   "method": {"type": "string", "enum": [
                       "GET", "POST", "PUT", "DELETE"]},
                   "path": {"type": "string"},
                   "query": {"type": "object"},
                   "body": {}},
               "required": ["method", "path"], "additionalProperties": False})
        async def spotify_api(args):
            status, data = await call(
                args["method"], str(args["path"]),
                params=args.get("query") or None, body=args.get("body"))
            if status is None:
                return _error(data)
            text = (json.dumps(data, ensure_ascii=False)
                    if isinstance(data, (dict, list)) else str(data or ""))
            if len(text) > 8000:
                text = text[:8000] + " …(truncated)"
            result = f"{status}" + (f" {text}" if text else "")
            return _text(result) if status < 400 else _error(result)

        return [spotify, spotify_play, spotify_search, spotify_login,
                spotify_now, spotify_queue, spotify_playlists,
                spotify_library, spotify_api]
