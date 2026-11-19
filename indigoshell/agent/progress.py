"""Progress of the agent's long commands: how far a download, a
transcode or a skill's pipeline has got, for the call to show.

**The channel.** The shell listens on a Unix datagram socket,
`$XDG_RUNTIME_DIR/indigoshell-progress-<uid>.sock` (0600). The agent's
Claude Code process gets its path as `$INDIGO_PROGRESS`, and every
command it runs inherits that. A report is one JSON object per
datagram; every field is optional, and null clears one:

    label    what this is: "transcribe", a file name
    stages   the pipeline's steps, once: ["download", "audio", ...]
    stage    the step running now (a new one clears the numbers)
    done     how far: bytes, seconds of media, items, or percent
    total    out of what; none means busy, with no end in sight
    unit     "B" bytes, "s" seconds of media, "%", or "" for items
    text     a short status line: "Turkish, 9:48 long"
    exit     the reporter's exit code, when it is done

Nothing is sent back, and a reporter never waits: with no shell
listening its datagrams go nowhere and the job carries on.

**Who reports.** Skill scripts, through the ten-line `report()` that
new-skill's SKILL.md gives them. yt-dlp, ffmpeg, curl and rsync, through
the shims this module puts at the front of the agent's PATH: each runs
the real program under `watch.py`, which reads the progress it prints
(see there). And the shell's own helpers, such as the phone transfers.

**Which step.** The socket tells the shell which process sent each
datagram (SO_PASSCRED). Walking up that process's parents finds where it
started. Under the agent's Claude Code process, it is the `bash -c` the
CLI started for one Bash call, and that command line holds the command
the agent wrote. So the job is matched to its step, background commands
included. Directly under the shell, it is one of the shell's own tool
helpers, the step of whichever shell tool is running. Everything one
Bash call starts is one job: a skill's stages and the ffmpeg it runs
inside one of them make a single row, the skill's own numbers first.

A job ends when the process at its root exits (a pidfd says when).
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shlex
import shutil
import socket
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..core import paths
from ..core.naming import APP
from .watch import ENV, PROFILES

log = logging.getLogger(__name__)

# The programs that get a shim: the ones watch.py knows how to read.
TOOLS = tuple(PROFILES)
WATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch.py")
# A job shows once it has run this long: a quick `curl` of an API
# reports a meter too, and a bar flashing up for it would be noise.
SHOW_AFTER_S = 0.6
# A finished job that belongs to no step stays up this long, done.
LINGER_S = 4.0
RATE_WINDOW_S = 6.0         # speed is measured over this much history
MAX_DGRAM = 8192
_UNITS = ("B", "s", "%", "")


def socket_path() -> str:
    return os.path.join(paths.runtime_dir(),
                        f"{APP}-progress-{os.getuid()}.sock")


def shim_dir() -> str:
    return os.path.join(paths.runtime_dir(), f"{APP}-shims-{os.getuid()}")


# ── showing numbers ─────────────────────────────────────────────────────
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _str(value, limit: int = 160) -> str:
    value = " ".join(str(value).split()) if value is not None else ""
    return value[:limit]


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 and value == value else None


# ── a job ───────────────────────────────────────────────────────────────
@dataclass(eq=False)
class Job:
    """One command's progress, as the reports have left it."""

    root: int                   # the process the job lives and dies with
    under: str                  # "cli", "shell", or "" (on its own)
    cmdline: str                # the root's command line
    id: int = 0
    step: str = ""              # the tool_use id it belongs to, if known
    label: str = ""
    stages: list[str] = field(default_factory=list)
    stage: str = ""
    done: float | None = None
    total: float | None = None
    unit: str = ""
    text: str = ""
    failed: bool = False
    started: float = field(default_factory=time.monotonic)
    updated: float = field(default_factory=time.monotonic)
    ended: float | None = None
    # Which reporter set what, by its distance from the root: a report
    # from nearer the root (the skill) outranks one from further down
    # (the ffmpeg it runs).
    _owner: dict = field(default_factory=dict, repr=False)
    _samples: deque = field(default_factory=lambda: deque(maxlen=64),
                            repr=False)

    def apply(self, msg: dict, depth: int) -> None:
        def mine(name: str) -> bool:
            return depth <= self._owner.get(name, depth)

        now = time.monotonic()
        self.updated = now
        if "stage" in msg and mine("stage"):
            stage = _str(msg["stage"], 60)
            self._owner["stage"] = depth
            if stage != self.stage:
                # A new stage: its numbers start over, from whoever has
                # them (the skill, or a program it runs).
                self.stage = stage
                self.done = self.total = None
                self.unit = ""
                self._samples.clear()
                for name in ("done", "total", "unit"):
                    self._owner.pop(name, None)
        if "label" in msg and mine("label"):
            self.label = _str(msg["label"], 120)
            self._owner["label"] = depth
        if "stages" in msg and mine("stages"):
            got = msg["stages"] if isinstance(msg["stages"], list) else []
            self.stages = [_str(s, 40) for s in got[:12]]
            self._owner["stages"] = depth
        if any(k in msg for k in ("done", "total", "unit")) and mine("done"):
            for name in ("done", "total", "unit"):
                self._owner[name] = depth
            if "unit" in msg:
                unit = _str(msg["unit"], 4)
                self.unit = unit if unit in _UNITS else ""
            if "total" in msg:
                self.total = _num(msg["total"]) or None
            if "done" in msg:
                done = _num(msg["done"])
                if done is None or (self.done is not None
                                    and done < self.done):
                    self._samples.clear()      # a new file starts at 0
                self.done = done
                if done is not None:
                    self._samples.append((now, done))
        if "text" in msg:
            self.text = _str(msg["text"])
        code = msg.get("exit")
        if isinstance(code, int) and not isinstance(code, bool) \
                and mine("exit"):
            # The outermost reporter's word: a skill that retries after
            # its ffmpeg failed has not failed.
            self._owner["exit"] = depth
            self.failed = code != 0

    # ── reading it ──────────────────────────────────────────────────────
    @property
    def live(self) -> bool:
        return self.ended is None

    def has_content(self) -> bool:
        return (self.done is not None or bool(self.stage or self.stages
                                              or self.text))

    def visible(self, now: float | None = None) -> bool:
        if not self.has_content():
            return False
        end = self.ended if self.ended is not None else (
            now if now is not None else time.monotonic())
        return end - self.started >= SHOW_AFTER_S

    def fraction(self) -> float | None:
        if self.done is None or not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    def rate(self) -> float | None:
        """Units a second, over the last RATE_WINDOW_S."""
        if len(self._samples) < 2:
            return None
        t1, d1 = self._samples[-1]
        t0, d0 = next(((t, d) for t, d in self._samples
                       if t1 - t <= RATE_WINDOW_S), self._samples[0])
        if t1 - t0 < 0.5 or d1 <= d0:
            return None
        return (d1 - d0) / (t1 - t0)

    def eta(self) -> float | None:
        rate, frac = self.rate(), self.fraction()
        if rate is None or frac is None or self.total is None:
            return None
        return (self.total - (self.done or 0)) / rate

    def elapsed(self, now: float | None = None) -> float:
        end = self.ended if self.ended is not None else (
            now if now is not None else time.monotonic())
        return end - self.started

    def title(self) -> str:
        return self.label or self.stage or "working"

    def amount(self) -> str:
        """How far, in the job's own unit: "12.4 MB / 51.0 MB"."""
        d, t = self.done, self.total
        if d is None or self.unit == "%":
            return ""
        if self.unit == "B":
            return f"{human(d)} / {human(t)}" if t else human(d)
        if self.unit == "s":
            return f"{clock(d)} / {clock(t)}" if t else clock(d)
        return f"{d:g}/{t:g}" if t else f"{d:g}"

    def speed(self) -> str:
        rate = self.rate()
        if rate is None or self.unit == "%":
            return ""
        if self.unit == "B":
            return f"{human(rate)}/s"
        if self.unit == "s":
            return f"{rate:.1f}x" if rate < 10 else f"{rate:.0f}x"
        return f"{rate:.1f}/s"

    def percent(self) -> str:
        frac = self.fraction()
        return "" if frac is None else f"{int(frac * 100)}%"

    def summary(self) -> str:
        """What a finished job did, for its step: "51.0 MB in 0:16"."""
        took = clock(self.elapsed())
        if self.failed:
            pct = self.percent()
            return f"stopped{' at ' + pct if pct else ''}, {took}"
        if self.done is not None and self.unit in ("B", "s", ""):
            amount = (human(self.total or self.done) if self.unit == "B"
                      else clock(self.total or self.done)
                      if self.unit == "s" else f"{self.total or self.done:g}")
            return f"{amount} in {took}"
        return f"done in {took}"

    def describe(self) -> str:
        """One line for the agent's `progress` tool."""
        parts = [self.title()]
        if self.stages:
            here = self.stage if self.stage in self.stages else ""
            parts.append(f"stage {self.stages.index(here) + 1} of "
                         f"{len(self.stages)} ({here})" if here
                         else f"stages: {', '.join(self.stages)}")
        elif self.stage and self.stage != self.title():
            parts.append(f"stage {self.stage}")
        if self.live:
            nums = [x for x in (self.percent(), self.amount(), self.speed())
                    if x]
            eta = self.eta()
            if eta is not None:
                nums.append(f"about {clock(eta)} left")
            nums.append(f"running {clock(self.elapsed())}")
            parts.append(", ".join(nums))
        else:
            parts.append(("failed: " if self.failed else "finished: ")
                         + self.summary())
        if self.text:
            parts.append(self.text)
        return " | ".join(parts)


# ── the shell's end ─────────────────────────────────────────────────────
class Jobs:
    """The socket, the jobs it has heard of, and the agent's shims.

    `claim(job)` names the step a new job belongs to (the agent sets it);
    listeners get ("new" | "update" | "show" | "end" | "drop", job)."""

    def __init__(self, path: str | None = None,
                 shims: str | None = None) -> None:
        self.path = path or socket_path()
        self.shims = shims or shim_dir()
        self.claim: Callable[[Job], str] = lambda job: ""
        self._jobs: dict[int, Job] = {}          # by root pid
        self._places: dict[int, tuple[int, int]] = {}   # pid -> root, depth
        self._listeners: list[Callable[[str, Job], None]] = []
        self._sock: socket.socket | None = None
        self._pidfds: dict[int, int] = {}        # root -> pidfd
        self._ids = itertools.count(1)

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._sock is not None:
            return
        try:
            self._sock = self._bind()
        except OSError:
            log.exception("progress: cannot listen on %s", self.path)
            return
        asyncio.get_running_loop().add_reader(self._sock.fileno(),
                                              self._readable)
        self.refresh()

    def _bind(self) -> socket.socket:
        if _listening(self.path):
            # Another shell (or a test) listening there keeps its socket;
            # this one takes a path of its own.
            self.path = self.path.replace(".sock", f"-{os.getpid()}.sock")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)             # left by a shell that died
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        old = os.umask(0o177)
        try:
            s.bind(self.path)
        finally:
            os.umask(old)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        s.setblocking(False)
        return s

    def stop(self) -> None:
        for root in list(self._pidfds):
            self._unwatch(root)
        if self._sock is not None:
            loop = _loop()
            if loop is not None:
                loop.remove_reader(self._sock.fileno())
            self._sock.close()
            self._sock = None
            with contextlib.suppress(OSError):
                os.unlink(self.path)

    def refresh(self) -> None:
        """Write the shims again: a program installed or moved since
        is picked up on the agent's next start."""
        try:
            write_shims(self.shims)
        except OSError:
            log.exception("progress: cannot write the shims in %s",
                          self.shims)

    def env(self) -> dict[str, str]:
        """For the agent's Claude Code process: where to report, and
        the shims ahead of everything else on its PATH."""
        if self._sock is None:
            return {}
        return {ENV: self.path,
                "PATH": os.pathsep.join(
                    p for p in (self.shims, os.environ.get("PATH")) if p)}

    def child_env(self) -> dict[str, str]:
        """For the shell's own helpers: where to report."""
        return {ENV: self.path} if self._sock is not None else {}

    # ── listeners ───────────────────────────────────────────────────────
    def subscribe(self, fn: Callable[[str, Job], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, event: str, job: Job) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, job)
            except Exception:
                log.exception("progress listener failed on %r", event)

    # ── reading the jobs ────────────────────────────────────────────────
    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.id)

    def shown(self) -> list[Job]:
        now = time.monotonic()
        return [j for j in self.all() if j.visible(now)]

    def running(self) -> list[Job]:
        return [j for j in self.shown() if j.live]

    def by_step(self, step: str) -> Job | None:
        if not step:
            return None
        found = [j for j in self._jobs.values() if j.step == step]
        return max(found, key=lambda j: j.id) if found else None

    def shape(self) -> tuple:
        """What the call's rows depend on: which jobs show, where, and
        whether each has a line of stages or text."""
        now = time.monotonic()
        return tuple((j.id, j.step, bool(j.stages or j.stage or j.text),
                      j.live) for j in self.all() if j.visible(now))

    def describe(self) -> str:
        jobs = self.shown()
        return "\n".join(j.describe() for j in jobs)

    # ── the socket ──────────────────────────────────────────────────────
    def _readable(self) -> None:
        for _ in range(256):            # what has arrived, within reason
            try:
                data, anc, _flags, _addr = self._sock.recvmsg(
                    MAX_DGRAM, socket.CMSG_SPACE(struct.calcsize("iII")))
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                log.debug("progress: receive failed", exc_info=True)
                return
            pid = _sender(anc)
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if pid is not None and isinstance(msg, dict):
                self._report(pid, msg)

    def _report(self, pid: int, msg: dict) -> None:
        root, depth, under, cmdline = self._place(pid)
        job = self._jobs.get(root)
        if job is None:
            job = Job(root=root, under=under, cmdline=cmdline,
                      id=next(self._ids))
            self._jobs[root] = job
            self._watch(job)
            try:
                job.step = self.claim(job) or ""
            except Exception:
                log.exception("progress: claiming a job failed")
            job.apply(msg, depth)
            self._emit("new", job)
            # It may only be worth showing in a moment: say when.
            asyncio.get_running_loop().call_later(
                SHOW_AFTER_S, self._emit, "show", job)
            return
        job.apply(msg, depth)
        self._emit("update", job)

    def _place(self, pid: int) -> tuple[int, int, str, str]:
        """The root of the job `pid` reports for, how many parents up it
        is, what it is under, and its command line."""
        hit = self._places.get(pid)
        if hit is not None and hit[0] in self._jobs:
            job = self._jobs[hit[0]]
            return hit[0], hit[1], job.under, job.cmdline
        me = os.getpid()
        chain: list[int] = []
        p = pid
        while p > 1 and len(chain) < 64:
            if p in self._jobs:              # inside a job already known
                job = self._jobs[p]
                self._places[pid] = (p, len(chain))
                return p, len(chain), job.under, job.cmdline
            parent = _ppid(p)
            if parent is None:
                break
            chain.append(p)
            if parent == me:
                top = p
                if _is_claude(top):
                    if len(chain) < 2:
                        break                # the CLI itself: not a job
                    root, under = chain[-2], "cli"
                else:
                    root, under = top, "shell"
                depth = chain.index(root)
                self._places[pid] = (root, depth)
                return root, depth, under, _cmdline(root)
            p = parent
        self._places[pid] = (pid, 0)
        return pid, 0, "", _cmdline(pid)

    # ── endings ─────────────────────────────────────────────────────────
    def _watch(self, job: Job) -> None:
        try:
            fd = os.pidfd_open(job.root)
        except OSError:
            job.ended = time.monotonic()     # gone already
            asyncio.get_running_loop().call_later(LINGER_S, self._drop, job)
            return
        self._pidfds[job.root] = fd
        asyncio.get_running_loop().add_reader(fd, self._exited, job)

    def _unwatch(self, root: int) -> None:
        fd = self._pidfds.pop(root, None)
        if fd is not None:
            loop = _loop()
            if loop is not None:
                loop.remove_reader(fd)
            os.close(fd)

    def _exited(self, job: Job) -> None:
        self._unwatch(job.root)
        job.ended = time.monotonic()
        self._emit("end", job)
        asyncio.get_running_loop().call_later(LINGER_S, self._drop, job)

    def _drop(self, job: Job) -> None:
        if self._jobs.get(job.root) is job:
            del self._jobs[job.root]
        for pid in [p for p, (root, _d) in self._places.items()
                    if root == job.root]:
            del self._places[pid]
        self._emit("drop", job)


# ── /proc ───────────────────────────────────────────────────────────────
def _sender(ancillary) -> int | None:
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            return struct.unpack("iII", data[:12])[0]
    return None


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # The name in parentheses may hold spaces or parentheses itself.
    return int(stat.rsplit(")", 1)[1].split()[1])


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")


def _is_claude(pid: int) -> bool:
    """Whether `pid` is a Claude Code process the SDK started."""
    line = _cmdline(pid)
    return "--output-format" in line and "stream-json" in line


def runs_command(cmdline: str, command: str) -> bool:
    """Whether a Bash call's `bash -c` line runs `command`: Claude Code
    hands it to `eval` in single quotes."""
    if not command:
        return False
    quoted = "'" + command.replace("'", "'\"'\"'") + "'"
    return f"eval {quoted}" in cmdline


def _listening(path: str) -> bool:
    """Whether a live socket is bound at `path` (not one left behind)."""
    if not os.path.exists(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ── the shims ───────────────────────────────────────────────────────────
def write_shims(folder: str) -> None:
    """One small script per program in TOOLS that runs the real one
    under watch.py. A program not installed gets none."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    mine = os.path.realpath(folder)
    search = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and os.path.realpath(p) != mine)
    for name in TOOLS:
        dest = os.path.join(folder, name)
        real = shutil.which(name, path=search)
        if real is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(dest)
            continue
        body = ("#!/bin/sh\n"
                "# indigoshell: shows this program's progress on the call\n"
                "# (agent/progress.py). Rewritten whenever the agent starts.\n"
                f"exec {shlex.quote(sys.executable)} -I -S "
                f"{shlex.quote(WATCH)} {shlex.quote(real)} \"$@\"\n")
        try:
            with open(dest) as f:
                if f.read() == body:
                    continue
        except OSError:
            pass
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
