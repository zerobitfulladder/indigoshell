"""The laptop's desktop, as hands for the hub's agent (hands.py).

The desktop's own tools (tools.py, the shell's agent's) adapted to the
hub: the same helpers and services, each tool a `Hand` the hub hands its
agent as `laptop_<name>`. Every hand answers within the hub's call
timeout (a minute, or what the hand declares: `run` takes ten) and
returns text, or a PNG for a screenshot.

    open              a link or a file in its usual program, brought
                      forward on its workspace
    screenshot        the whole screen, one monitor or one window
    windows           the workspaces and the windows on each
    focus             bring a window forward, or show a workspace
    launch            start an app by name
    read_clipboard / write_clipboard
    spotify           the desktop app, over MPRIS: what is playing,
                      play, pause, skip, or play a link
    run               a shell command as the user (bash -lc)
    job_start / job_status / job_output / job_cancel
                      a long command in the background (`Jobs`): it
                      outlives the call, the link to the hub and a
                      reload of the shell, and the hub hears when it
                      ends (a `job_done` event)
    read_file / write_file
                      a file's text, or its bytes in base64, either way;
                      the hub also syncs skills' code through these, into
                      ~/.cache/lilac/skills/

**Root asks first.** A command (or a job) that mentions sudo, doas,
pkexec, su or run0 asks the user in the call (Allow / Deny, rung if the call is
closed), and no answer before the hub gives up is a no. Root needs no
password on this laptop, so that question is the gate: commands that
weren't allowed also run with those five shadowed on their PATH, so a
script that calls one by name is refused rather than let through.

What stays with the shell's own agent: the screenshot from before the
call opened (glance.py), progress bars, toasts, the Firefox bridge (the
shell's own browser gets its agent side later), the lights (the hub's,
later) and the phone (LilacDeck's hands).
"""

import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import json
import re
import secrets
import shutil
import signal
import tempfile
import time

import skia

from ..core import paths
from ..services import clipboard
from . import tools as desk
from .hands import DECLINED, Call, Hand, HandError, text_block

log = logging.getLogger(__name__)

# The desktop's helpers, looked up here so tests can stand in for them.
_run = desk._run
_qtile = desk._qtile
decode = desk.decode
open_target = desk.open_target
bring_forward = desk.bring_forward

SHOT_MAX_SIDE = 1568        # long side of a screenshot, as the model sees it
CLIPBOARD_MAX = 20000       # characters
RUN_DEFAULT_S = 120.0
RUN_MAX_S = 600             # what `run` asks the hub to wait for it
OUTPUT_MAX = 30 * 1024      # bytes of a command's output: half head, half tail
FILE_MAX = 10 * 1024 * 1024
ROOT = ("sudo", "doas", "pkexec", "su", "run0")
_ROOT_WORD = re.compile(r"(?<![\w.-])(" + "|".join(ROOT) + r")(?![\w.-])")


def _home() -> str:
    return os.path.expanduser("~")


def _path(value) -> str:
    """A path the agent gave: ~ expanded, relative ones from home."""
    path = os.path.expanduser(str(value or "").strip())
    if not path:
        raise HandError("give a path")
    return os.path.normpath(path if os.path.isabs(path)
                            else os.path.join(_home(), path))


def _texts(*values: str) -> list[dict]:
    return [text_block(v) for v in values]


def _schema(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


# ── screen and windows ──────────────────────────────────────────────────
def png_block(image: skia.Image, max_side: int = SHOT_MAX_SIDE
              ) -> tuple[dict, int, int]:
    """A PNG image block of `image`, its long side at most `max_side`."""
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
    data = image.encodeToData(skia.EncodedImageFormat.kPNG, 100)
    return ({"type": "image", "data": base64.b64encode(bytes(data)).decode(),
             "mime": "image/png"}, tw, th)


async def _screenshot(call: Call) -> list[dict]:
    args = call.args
    target = args.get("target") or "screen"
    crop = None
    what = "the whole screen"
    try:
        if target == "monitor":
            screens = await _qtile("-o", "root", "-f", "get_screens") or []
            if args.get("monitor") is not None:
                index = int(args["monitor"])
            else:
                index = int((await _qtile("-o", "screen", "-f", "info")
                             or {}).get("index", 0))
            screen = next((s for s in screens if s.get("index") == index),
                          None)
            if screen is None:
                raise HandError(f"no monitor {index}; there are "
                                f"{len(screens)} (0 to {len(screens) - 1})")
            crop = (screen["x"], screen["y"], screen["width"],
                    screen["height"])
            what = f"monitor {index}"
        elif target == "window":
            wid = args.get("window_id")
            info = await (_qtile("-o", "window", str(int(wid)), "-f", "info")
                          if wid is not None
                          else _qtile("-o", "window", "-f", "info"))
            crop = (info["x"], info["y"], info["width"], info["height"])
            cls = (info.get("wm_class") or ["?"])[-1]
            what = f"window {info.get('id', wid)} ({cls}: {info.get('name', '')})"
    except (KeyError, TypeError, ValueError, RuntimeError) as e:
        raise HandError(f"can't find what to capture: {e}") from None
    argv = ["import", "-silent", "-window", "root"]
    if crop is not None:
        x, y, w, h = crop
        argv += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
    rc, data, err = await _run(argv + ["png:-"], timeout=20)
    image = decode(data) if rc == 0 else None
    if image is None:
        raise HandError(f"the screenshot failed: {err or rc}")
    block, tw, th = png_block(image)
    scale = tw / image.width()
    note = (f"{what}, {image.width()}x{image.height()}, shown at {tw}x{th}"
            + (f" (scale {scale:.2f})" if scale < 1 else "")
            + ". The call overlay is in the top-right corner when open.")
    return [block, text_block(note)]


SCREENSHOT = Hand(
    "screenshot",
    "Look at the screen. target: 'screen' (default: every monitor), "
    "'monitor' (one monitor: `monitor`, its number, or the one in use) or "
    "'window' (`window_id` from laptop_windows, or the focused window). "
    "Comes back as a PNG, scaled down to at most "
    f"{SHOT_MAX_SIDE} px on its long side; the reply says by how much.",
    _screenshot,
    _schema({"target": {"type": "string",
                        "enum": ["screen", "monitor", "window"]},
             "monitor": {"type": "integer", "minimum": 0},
             "window_id": {"type": "integer"}}))


async def _open(call: Call) -> list[dict]:
    target = str(call.args.get("target") or "").strip()
    if not target:
        raise HandError("give a link or a file")
    ok, said = await open_target(target, _home())
    if not ok:
        raise HandError(said)
    return _texts(said)


OPEN = Hand(
    "open",
    "Open something for the user to look at, in the program that normally "
    "opens it, and bring that window forward on its workspace. target: a "
    "link (http, https, mailto) or a file on the laptop (an HTML report, "
    "an image, a video, a PDF); relative paths are from the home folder. "
    "It goes to the desktop's default program for that kind of thing (as "
    "xdg-open picks it: the web browser for pages); the reply names it.",
    _open,
    _schema({"target": {"type": "string"}}, ("target",)))


async def _windows(_call: Call) -> list[dict]:
    try:
        groups = await _qtile("-o", "root", "-f", "get_groups")
        wins = await _qtile("-o", "root", "-f", "windows")
        current = await _qtile("-o", "group", "-f", "info")
    except RuntimeError as e:
        raise HandError(str(e)) from None
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
    return _texts("\n".join(lines) or "no workspaces")


WINDOWS = Hand(
    "windows",
    "The workspaces (qtile groups), which one is shown, and the windows on "
    "each: [window id] app: title. The ids are what laptop_focus and "
    "laptop_screenshot take.",
    _windows)


async def _focus(call: Call) -> list[dict]:
    args = call.args
    try:
        if args.get("window_id") is not None:
            wid = str(int(args["window_id"]))
            info = await _qtile("-o", "window", wid, "-f", "info")
            await _qtile("-o", "group", str(info["group"]), "-f", "toscreen")
            await _qtile("-o", "window", wid, "-f", "focus")
            return _texts(f"Focused {info.get('name', wid)}, on workspace "
                          f"{info['group']}.")
        name = str(args.get("workspace") or "").strip()
        if not name:
            raise HandError("give a window_id or a workspace")
        await _qtile("-o", "group", name, "-f", "toscreen")
        return _texts(f"Workspace {name} is shown.")
    except (RuntimeError, KeyError, TypeError, ValueError) as e:
        raise HandError(f"can't focus it: {e}") from None


FOCUS = Hand(
    "focus",
    "Bring a window forward (window_id, from laptop_windows), switching to "
    "its workspace; or show a workspace by name (workspace).",
    _focus,
    _schema({"window_id": {"type": "integer"},
             "workspace": {"type": "string"}}))


_catalog = None


async def _launch(call: Call) -> list[dict]:
    global _catalog
    from ..services.apps import Catalog
    if _catalog is None:
        _catalog = Catalog()
    _catalog.refresh()
    results = _catalog.search(str(call.args.get("name") or ""))
    if not results:
        raise HandError("no installed application matches that")
    app = results[0].app
    _catalog.launch(app)
    return _texts(f"Started {app.entry.name}.")


LAUNCH = Hand(
    "launch",
    "Start an application by name, matched as the launcher matches it "
    "(e.g. 'firefox', 'files', 'spotify').",
    _launch,
    _schema({"name": {"type": "string"}}, ("name",)))


# ── clipboard ───────────────────────────────────────────────────────────
async def _read_clipboard(call: Call) -> list[dict]:
    selection = "primary" if call.args.get("selection") == "primary" \
        else "clipboard"
    rc, out, err = await _run(["xclip", "-selection", selection, "-o"],
                              timeout=5)
    if rc != 0:
        raise HandError(err or f"the {selection} is empty or not text")
    value = out.decode(errors="replace")
    if len(value) > CLIPBOARD_MAX:
        value = (value[:CLIPBOARD_MAX]
                 + f"\n… (cut: {len(value)} characters in all)")
    return _texts(value)


READ_CLIPBOARD = Hand(
    "read_clipboard",
    "The clipboard's text (what Ctrl+V pastes), or with selection "
    "'primary' the text highlighted last (what a middle click pastes).",
    _read_clipboard,
    _schema({"selection": {"type": "string",
                           "enum": ["clipboard", "primary"]}}))


async def _write_clipboard(call: Call) -> list[dict]:
    value = str(call.args.get("text") or "")
    if not value:
        raise HandError("give some text")
    if not clipboard.copy(value):
        raise HandError("xclip is missing")
    return _texts("Copied.")


WRITE_CLIPBOARD = Hand(
    "write_clipboard",
    "Put text on the clipboard, for the user to paste.",
    _write_clipboard,
    _schema({"text": {"type": "string"}}, ("text",)))


# ── Spotify, the desktop app ────────────────────────────────────────────
def spotify_hand(spotify=None) -> Hand:
    from .spotify import Spotify, uri as spotify_uri
    sp = spotify or Spotify()

    async def run(call: Call) -> list[dict]:
        action = call.args.get("action") or "status"
        if action != "status":
            if not await sp.app_start():
                raise HandError("Spotify didn't start")
            if action == "play" and call.args.get("uri"):
                target = spotify_uri(str(call.args["uri"]))
                if target is None:
                    raise HandError("not a spotify: URI or an "
                                    "open.spotify.com link")
                rc, out = await sp.app("open", target)
            else:
                rc, out = await sp.app({"toggle": "play-pause"}.get(
                    action, action))
            if rc != 0:
                raise HandError(out or f"{action} failed")
            await asyncio.sleep(0.4)
        if not await sp.app_running():
            return _texts("Spotify isn't running.")
        rc, out = await sp.app("metadata", "--format",
                               "{{status}}\t{{artist}}\t{{title}}\t{{album}}")
        parts = out.split("\t")
        if rc != 0 or len(parts) < 3:
            return _texts(f"Spotify is open; nothing is loaded ({out}).")
        status, artist, title = parts[:3]
        album = parts[3] if len(parts) > 3 else ""
        return _texts(f"{status}: {title} by {artist}"
                      + (f" ({album})" if album else ""))

    return Hand(
        "spotify",
        "The Spotify desktop app (playing through the laptop's speakers): "
        "action 'status' (what is playing; the default), 'play', 'pause', "
        "'toggle', 'next' or 'previous'. With 'play', uri (a spotify: URI "
        "or an open.spotify.com link to a track, album, playlist, artist "
        "or episode) plays that instead. Starts the app if needed.",
        run,
        _schema({"action": {"type": "string",
                            "enum": ["status", "play", "pause", "toggle",
                                     "next", "previous"]},
                 "uri": {"type": "string"}}))


# ── run ─────────────────────────────────────────────────────────────────
def needs_root(command: str) -> bool:
    """Whether a command mentions sudo, doas, pkexec, su or run0."""
    return _ROOT_WORD.search(command) is not None


def _shims() -> str:
    """A folder of stand-ins for the root commands, put first on the PATH
    of commands that weren't allowed root."""
    folder = os.path.join(paths.runtime_dir(), "indigoshell-hands-bin")
    os.makedirs(folder, exist_ok=True)
    for name in ROOT:
        path = os.path.join(folder, name)
        script = (f'#!/bin/sh\necho "{name}: refused: root needs the '
                  "user's OK; put it in the command given to laptop_run, so "
                  'the laptop asks them" >&2\nexit 126\n')
        try:
            with open(path) as f:
                if f.read() == script:
                    continue
        except OSError:
            pass
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
    return folder


class _Output:
    """A command's output, kept to its first and last OUTPUT_MAX / 2
    bytes."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = OUTPUT_MAX // 2 - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            del self.tail[:-(OUTPUT_MAX // 2)]

    def text(self) -> str:
        head = self.head.decode(errors="replace")
        if self.total <= len(self.head) + len(self.tail):
            return head + self.tail.decode(errors="replace")
        cut = self.total - len(self.head) - len(self.tail)
        return (head + f"\n… ({cut} bytes cut; {self.total} in all) …\n"
                + self.tail.decode(errors="replace"))


async def _kill(proc: asyncio.subprocess.Process) -> None:
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except asyncio.TimeoutError:
            continue


async def _run_command(call: Call) -> list[dict]:
    args = call.args
    command = str(args.get("command") or "")
    if not command.strip():
        raise HandError("give a command")
    cwd = _path(args.get("cwd") or "~")
    if not os.path.isdir(cwd):
        raise HandError(f"no such folder: {cwd}")
    root = needs_root(command)
    if root and not await call.ask("Run this as root on the laptop?",
                                   command):
        raise HandError(DECLINED)
    try:
        wanted = float(args.get("timeout") or RUN_DEFAULT_S)
    except (TypeError, ValueError):
        wanted = RUN_DEFAULT_S
    limit = max(1.0, min(wanted, call.left() - 1.5))

    env = dict(os.environ, LAPTOP_RUN_CMD=command)
    script = 'c=$LAPTOP_RUN_CMD; unset LAPTOP_RUN_CMD; eval "$c"'
    if not root:
        env["LAPTOP_RUN_SHIMS"] = _shims()
        script = ('PATH="$LAPTOP_RUN_SHIMS:$PATH"; unset LAPTOP_RUN_SHIMS; '
                  + script)
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", script, cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
    except OSError as e:
        raise HandError(f"couldn't start bash: {e}") from None
    out = _Output()

    async def pump() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            out.add(chunk)
        await proc.wait()

    began = time.monotonic()
    killed = False
    task = asyncio.ensure_future(pump())
    try:
        await asyncio.wait_for(asyncio.shield(task), limit)
    except asyncio.TimeoutError:
        killed = True
        await _kill(proc)
        with contextlib.suppress(asyncio.TimeoutError,
                                 asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    except asyncio.CancelledError:
        await _kill(proc)
        task.cancel()
        raise
    seconds = time.monotonic() - began
    text = out.text().rstrip("\n") or "(no output)"
    if killed:
        why = (f"; the hub waits {call.timeout:g} s for a laptop tool"
               if limit < wanted else "")
        end = f"[stopped after {seconds:.0f} s: the time limit{why}]"
    else:
        end = f"[exit code {proc.returncode}, {seconds:.1f} s]"
    return _texts(f"{text}\n{end}")


RUN = Hand(
    "run",
    "Run a shell command on the laptop as the user (bash -lc), in cwd "
    "(default: the home folder). Returns its output (stdout and stderr "
    f"together, the first and last {OUTPUT_MAX // 2048} kB of it if longer) "
    "and its exit code. timeout in seconds (default "
    f"{RUN_DEFAULT_S:g}, at most about {RUN_MAX_S // 60} minutes); a "
    "command still running then is stopped, so start longer work in the "
    "background (setsid … > log 2>&1 &) and check on it. "
    "Anything with sudo, doas, pkexec, su or run0 in it asks the user on "
    "the laptop first.",
    _run_command,
    _schema({"command": {"type": "string"}, "cwd": {"type": "string"},
             "timeout": {"type": "number", "minimum": 1,
                         "maximum": RUN_MAX_S}}, ("command",)),
    timeout=RUN_MAX_S)


# ── jobs ────────────────────────────────────────────────────────────────
JOBS_DIR = os.path.join("~", ".cache", "lilac", "jobs")
JOB_POLL_S = 3.0            # how often to look for jobs that ended
JOB_KEEP_S = 30 * 86400     # a reported job's folder is kept this long
JOB_TAIL_LINES = 20         # the output's end, in job_status and job_done
JOB_TAIL_MAX = 4096         # ... at most this many bytes of it
JOB_OUTPUT_LINES = 200      # job_output's default
CANCEL_GRACE_S = 5.0

# Runs the job, in a session of its own (setsid --fork): the command in a
# login bash, its output to a file, and its exit code and end time to
# another. It catches TERM (a trap, not ignored, so the command doesn't
# inherit it) so that a cancelled command still gets its code written.
_JOB_WRAPPER = """\
d=$LAPTOP_JOB_DIR; unset LAPTOP_JOB_DIR
echo $$ > "$d/pid"
trap 'true' TERM
bash -lc "$LAPTOP_JOB_INNER" > "$d/output" 2>&1 < /dev/null
code=$?
printf '%s %s\\n' "$code" "$(date +%s)" > "$d/exit.tmp" && mv "$d/exit.tmp" "$d/exit"
"""


def _tail(data: bytes, lines: int, limit: int) -> str:
    text = data.decode(errors="replace").rstrip("\n")
    out = "\n".join(text.splitlines()[-lines:]) if lines else ""
    return out[-limit:]


class Jobs:
    """Long commands in the background, each in its own folder under
    ~/.cache/lilac/jobs/<id>/: job.json (what was asked), pid, output,
    exit ("<code> <end time>"), and the marks cancelled and reported.

    The folders are the whole record, so a job started before a reload
    of the shell is as known after it as before. While the line to the
    hub is up (`online`), the jobs that ended and aren't yet reported go
    to the hub as `job_done` events, each once."""

    def __init__(self, folder: str | None = None) -> None:
        self.folder = os.path.expanduser(folder or JOBS_DIR)
        self._send = None
        self._watch: asyncio.Task | None = None

    # ── the record ──────────────────────────────────────────────────────
    def _dir(self, job: str) -> str:
        job = str(job or "").strip()
        if not re.fullmatch(r"[0-9a-f]{8}", job):
            raise HandError(f"no job {job!r}: a job is the 8-character id "
                            "laptop_job_start answered")
        path = os.path.join(self.folder, job)
        if not os.path.isdir(path):
            raise HandError(f"no job {job} on the laptop")
        return path

    @staticmethod
    def _read(path: str) -> str:
        try:
            with open(path, errors="replace") as f:
                return f.read().strip()
        except OSError:
            return ""

    def info(self, job: str) -> dict:
        """Everything known about a job: its job.json, plus `state`
        (starting, running, done, gone), `exit` and `ended`."""
        path = self._dir(job)
        try:
            with open(os.path.join(path, "job.json")) as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        info["job"] = job
        ended = self._read(os.path.join(path, "exit")).split()
        if ended:
            info["state"] = "done"
            info["exit"] = int(ended[0]) if ended[0].lstrip("-").isdigit() \
                else None
            info["ended"] = float(ended[1]) if len(ended) > 1 else None
        elif self._alive(job, path):
            info["state"] = "running"
        elif not os.path.exists(os.path.join(path, "pid")) \
                and time.time() - info.get("started", 0) < 10:
            info["state"] = "starting"
        else:
            # Killed outright, or the laptop restarted under it.
            info["state"] = "gone"
            info["exit"] = None
        info["cancelled"] = os.path.exists(os.path.join(path, "cancelled"))
        info["reported"] = os.path.exists(os.path.join(path, "reported"))
        return info

    def _alive(self, job: str, path: str) -> bool:
        pid = self._read(os.path.join(path, "pid"))
        if not pid.isdigit():
            return False
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f"lilac-job-{job}".encode() in f.read()
        except OSError:
            return False

    def _output(self, job: str) -> bytes:
        try:
            with open(os.path.join(self._dir(job), "output"), "rb") as f:
                return f.read()
        except OSError:
            return b""

    def _tail_of(self, job: str, lines: int = JOB_TAIL_LINES,
                 limit: int = JOB_TAIL_MAX) -> str:
        path = os.path.join(self._dir(job), "output")
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 64 * 1024))
                data = f.read()
        except OSError:
            return ""
        return _tail(data, lines, limit)

    def all(self) -> list[str]:
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        return sorted(n for n in names if re.fullmatch(r"[0-9a-f]{8}", n))

    # ── doing ───────────────────────────────────────────────────────────
    async def start(self, command: str, *, cwd: str, title: str,
                    root: bool) -> str:
        """Start `command` in the background; its id."""
        self._prune()
        job = secrets.token_hex(4)
        path = os.path.join(self.folder, job)
        os.makedirs(path, mode=0o700)
        with open(os.path.join(path, "job.json"), "w") as f:
            json.dump({"command": command, "cwd": cwd, "title": title,
                       "root": root, "started": time.time()}, f)
        inner = 'unset LAPTOP_JOB_INNER; c=$LAPTOP_RUN_CMD; unset LAPTOP_RUN_CMD; eval "$c"'
        env = dict(os.environ, LAPTOP_RUN_CMD=command, LAPTOP_JOB_DIR=path)
        if not root:
            env["LAPTOP_RUN_SHIMS"] = _shims()
            inner = ('PATH="$LAPTOP_RUN_SHIMS:$PATH"; unset LAPTOP_RUN_SHIMS; '
                     + inner)
        env["LAPTOP_JOB_INNER"] = inner
        try:
            proc = await asyncio.create_subprocess_exec(
                "setsid", "--fork", "bash", "-c", _JOB_WRAPPER,
                f"lilac-job-{job}", cwd=cwd, env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        except OSError as e:
            shutil.rmtree(path, ignore_errors=True)
            raise HandError(f"couldn't start the job: {e}") from None
        for _ in range(40):                 # until it has said its pid
            if os.path.exists(os.path.join(path, "pid")):
                break
            await asyncio.sleep(0.05)
        log.info("job %s started: %s", job, title)
        self._poke()
        return job

    async def cancel(self, job: str) -> dict:
        path = self._dir(job)
        info = self.info(job)
        if info["state"] not in ("running", "starting"):
            return info
        open(os.path.join(path, "cancelled"), "w").close()
        pid = int(self._read(os.path.join(path, "pid")) or 0)
        if pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, sig)
                deadline = time.monotonic() + CANCEL_GRACE_S
                while time.monotonic() < deadline \
                        and self.info(job)["state"] == "running":
                    await asyncio.sleep(0.1)
                if self.info(job)["state"] != "running":
                    break
        log.info("job %s cancelled", job)
        return self.info(job)

    def _prune(self) -> None:
        """Forget jobs reported long ago."""
        now = time.time()
        for job in self.all():
            mark = os.path.join(self.folder, job, "reported")
            with contextlib.suppress(OSError):
                if now - os.path.getmtime(mark) > JOB_KEEP_S:
                    shutil.rmtree(os.path.join(self.folder, job),
                                  ignore_errors=True)

    # ── telling the hub ─────────────────────────────────────────────────
    def online(self, send) -> None:
        """A line to the hub is up: report what ended, and keep watching."""
        self._send = send
        if self._watch is None or self._watch.done():
            self._watch = asyncio.get_running_loop().create_task(
                self._watching())

    def offline(self) -> None:
        self._send = None
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None

    def _poke(self) -> None:
        if self._send is not None and (self._watch is None
                                       or self._watch.done()):
            self.online(self._send)

    def event(self, job: str) -> dict:
        info = self.info(job)
        code = info.get("exit")
        return {"type": "event", "kind": "job_done", "job": job,
                "ok": code == 0, "exit": code,
                "title": info.get("title") or "",
                "tail": self._tail_of(job)}

    def report(self) -> int:
        """Tell the hub about every job that ended and isn't reported;
        how many. Each is marked once it is on its way."""
        sent = 0
        for job in self.all():
            if self._send is None:
                break
            try:
                info = self.info(job)
            except HandError:
                continue
            if info["reported"] or info["state"] not in ("done", "gone"):
                continue
            self._send(self.event(job))
            open(os.path.join(self.folder, job, "reported"), "w").close()
            log.info("job %s reported (exit %s)", job, info.get("exit"))
            sent += 1
        return sent

    def _pending(self) -> bool:
        """Whether any job is still running or not yet reported."""
        for job in self.all():
            if not os.path.exists(os.path.join(self.folder, job,
                                               "reported")):
                return True
        return False

    async def _watching(self) -> None:
        while self._send is not None:
            try:
                self.report()
            except Exception:
                log.exception("jobs: reporting failed")
            if not self._pending():
                return                      # woken again by the next start
            await asyncio.sleep(JOB_POLL_S)

    # ── the hands ───────────────────────────────────────────────────────
    def _describe(self, info: dict, lines: int = JOB_TAIL_LINES) -> str:
        now = time.time()
        started = info.get("started") or now
        state = info["state"]
        if state == "done":
            took = (info.get("ended") or now) - started
            head = (f"finished: exit code {info['exit']}, after "
                    f"{_span(took)}"
                    + (" (cancelled)" if info["cancelled"] else ""))
        elif state == "gone":
            head = ("cancelled" if info["cancelled"] else
                    "ended without an exit code (killed, or the laptop "
                    "restarted)")
        else:
            head = f"{state}, for {_span(now - started)}"
        tail = self._tail_of(info["job"], lines)
        return (f"job {info['job']}: {info.get('title') or ''}\n{head}\n"
                + (f"last lines of its output:\n{tail}" if tail
                   else "no output yet" if state in ("running", "starting")
                   else "no output"))

    def hands(self) -> list[Hand]:
        async def start(call: Call) -> list[dict]:
            args = call.args
            command = str(args.get("command") or "")
            if not command.strip():
                raise HandError("give a command")
            cwd = _path(args.get("cwd") or "~")
            if not os.path.isdir(cwd):
                raise HandError(f"no such folder: {cwd}")
            title = " ".join(str(args.get("title") or "").split()) \
                or " ".join(command.split())[:60]
            root = needs_root(command)
            if root and not await call.ask(
                    "Start this as root on the laptop?", command):
                raise HandError(DECLINED)
            job = await self.start(command, cwd=cwd, title=title, root=root)
            return _texts(f"job {job} started")

        async def status(call: Call) -> list[dict]:
            return _texts(self._describe(self.info(call.args.get("job"))))

        async def output(call: Call) -> list[dict]:
            job = call.args.get("job")
            self._dir(job)
            try:
                lines = int(call.args.get("tail") or JOB_OUTPUT_LINES)
            except (TypeError, ValueError):
                lines = JOB_OUTPUT_LINES
            data = self._output(job)
            text = _tail(data, max(1, lines), OUTPUT_MAX)
            return _texts(text or "(no output yet)")

        async def cancel(call: Call) -> list[dict]:
            info = self.info(call.args.get("job"))
            if info["state"] not in ("running", "starting"):
                return _texts(f"job {info['job']} isn't running: "
                              + self._describe(info).split("\n")[1])
            return _texts(self._describe(await self.cancel(info["job"])))

        job = {"job": {"type": "string",
                       "description": "the id laptop_job_start answered"}}
        return [
            Hand("job_start",
                 "Start a long command on the laptop in the background, as "
                 "the user (bash -lc, the same environment as laptop_run: "
                 "its GPU, its files), in cwd (default: the home folder). "
                 "Answers at once, 'job <id> started'; the job keeps running "
                 "whatever happens to this conversation or the laptop's link "
                 "to the hub, and when it ends the hub hears of it and a "
                 "turn of its own collects the result. title: a few words "
                 "for it (default: the command). For transcribing, "
                 "encoding, downloads, training: anything longer than a "
                 "minute. Anything with sudo, doas, pkexec, su or run0 in "
                 "it asks the user on the laptop first.",
                 start,
                 _schema({"command": {"type": "string"},
                          "cwd": {"type": "string"},
                          "title": {"type": "string"}}, ("command",)),
                 timeout=RUN_MAX_S),
            Hand("job_status",
                 "A background job on the laptop: running (for how long) or "
                 "finished (its exit code, how long it took), and the last "
                 "lines of its output.",
                 status, _schema(job, ("job",))),
            Hand("job_output",
                 "A background job's output so far (stdout and stderr "
                 f"together): its last `tail` lines (default "
                 f"{JOB_OUTPUT_LINES}), at most {OUTPUT_MAX // 1024} kB.",
                 output,
                 _schema({**job, "tail": {"type": "integer", "minimum": 1}},
                         ("job",))),
            Hand("job_cancel",
                 "Stop a background job on the laptop (TERM, then KILL "
                 f"after {CANCEL_GRACE_S:g} s).",
                 cancel, _schema(job, ("job",))),
        ]


def _span(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s // 60 % 60} min"


# ── files ───────────────────────────────────────────────────────────────
async def _read_file(call: Call) -> list[dict]:
    path = _path(call.args.get("path"))
    if not os.path.isfile(path):
        raise HandError(f"no such file: {path}" if not os.path.exists(path)
                        else f"not a file: {path}")
    size = os.path.getsize(path)
    if size > FILE_MAX:
        raise HandError(f"{path} is {size / 1048576:.1f} MB, more than "
                        f"{FILE_MAX // 1048576} MB")

    def read() -> bytes:
        with open(path, "rb") as f:
            return f.read(FILE_MAX + 1)
    try:
        data = await asyncio.to_thread(read)
    except OSError as e:
        raise HandError(f"can't read {path}: {e.strerror or e}") from None
    try:
        text = data.decode("utf-8")
        if "\x00" in text:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL")
    except UnicodeDecodeError:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return _texts(f"{path}: {len(data)} bytes, binary ({mime}); base64 "
                      "follows.", base64.b64encode(data).decode())
    return _texts(f"{path}: {len(data)} bytes of UTF-8 text follow.", text)


READ_FILE = Hand(
    "read_file",
    "Read a file on the laptop (relative paths are from the home folder): "
    "its text, or for a binary file its bytes in base64, up to "
    f"{FILE_MAX // 1048576} MB. A first text block says which, and the "
    "size; the content is the second.",
    _read_file,
    _schema({"path": {"type": "string"}}, ("path",)))


async def _write_file(call: Call) -> list[dict]:
    args = call.args
    path = _path(args.get("path"))
    has_text, has_b64 = args.get("text") is not None, \
        args.get("base64") is not None
    if has_text == has_b64:
        raise HandError("give either text or base64")
    if has_text:
        data = str(args["text"]).encode("utf-8")
    else:
        try:
            data = base64.b64decode(str(args["base64"]), validate=True)
        except ValueError:
            raise HandError("that isn't valid base64") from None
    if len(data) > FILE_MAX:
        raise HandError(f"{len(data) / 1048576:.1f} MB is more than "
                        f"{FILE_MAX // 1048576} MB")
    if os.path.isdir(path):
        raise HandError(f"{path} is a folder")
    existed = os.path.exists(path)
    if existed and not args.get("overwrite"):
        raise HandError(f"{path} exists; pass overwrite: true to replace it")

    def write() -> None:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if existed:
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            else:
                os.chmod(tmp, 0o666 & ~_umask())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    try:
        await asyncio.to_thread(write)
    except OSError as e:
        raise HandError(f"can't write {path}: {e.strerror or e}") from None
    return _texts(f"{'Replaced' if existed else 'Wrote'} {path} "
                  f"({len(data)} bytes).")


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


WRITE_FILE = Hand(
    "write_file",
    "Write a file on the laptop (relative paths are from the home folder; "
    "missing folders are made): text, or base64 for binary content, up to "
    f"{FILE_MAX // 1048576} MB. An existing file is only replaced with "
    "overwrite: true.",
    _write_file,
    _schema({"path": {"type": "string"}, "text": {"type": "string"},
             "base64": {"type": "string"},
             "overwrite": {"type": "boolean"}}, ("path",)))


def desktop_hands(*, spotify=None) -> list[Hand]:
    """The laptop's desktop as hands, in the order they are lent."""
    return [OPEN, SCREENSHOT, WINDOWS, FOCUS, LAUNCH, READ_CLIPBOARD,
            WRITE_CLIPBOARD, spotify_hand(spotify), RUN, READ_FILE,
            WRITE_FILE]
