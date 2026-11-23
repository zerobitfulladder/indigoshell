"""A call to an agent that lives on another machine.

`RemoteAgent` stands in for `Agent` (session.py) wherever the call
overlay, its corner chip and `CallRouter` read one: the same attributes
and methods, kept from a remote agent's events instead of a Claude Code
process of our own. The first is lilac, the house agent on a Raspberry
Pi; its repo's `lilac/protocol.py` is the reference for the protocol:
one JSON object per line over the stdin and stdout of a command,
usually `ssh <host> <bridge>`, so the SSH key is the login.

**The line stays up** while the shell runs, so a turn that ends while
the call is closed still rings. It comes back by itself when either
machine was away, with a growing pause between tries: the remote's
firewall refuses rapid SSH connections, and every refused try restarts
its block. Opening the call or saying a line cuts the pause short.

**Ringing is ours**, as for the desktop's agent: the remote only says
that a turn ended or asks. A turn started from another of the remote's
clients (a terminal, its website) doesn't ring here; one started from
this call, or by the remote agent itself, does.

What only the desktop's agent has stays empty here: the glance at the
desktop, the progress bars of long commands (`jobs`), the desktop tools.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from typing import Callable

from .activity import Activity, Approval
from .session import (ACTIVITY_KEEP, DEFAULT_EFFORT, EFFORT_NAMES, EFFORTS,
                      MODELS)

log = logging.getLogger(__name__)

# What this call calls itself to the remote, so its own lines are known.
CLIENT = "overlay"
MAX_LINE = 16 * 1024 * 1024
RETRY_MIN_S = 5.0
RETRY_MAX_S = 120.0
# A line that held this long was healthy: the next try comes quickly.
HEALTHY_S = 60.0


class _NoJobs:
    """No progress bars for a remote agent's commands."""

    def shown(self) -> list:
        return []

    def shape(self) -> tuple:
        return ()

    def by_step(self, _step: str):
        return None


class _Session:
    """A past conversation, as the call's history list reads one."""

    __slots__ = ("session_id", "summary", "first_prompt", "last_modified")

    def __init__(self, item: dict) -> None:
        self.session_id = str(item.get("session_id") or "")
        self.summary = item.get("summary")
        self.first_prompt = item.get("first_prompt")
        self.last_modified = item.get("last_modified") or 0


class RemoteAgent:
    """An agent on another machine, read like the desktop's `Agent`.

    A SERVICES entry (it needs the loop for its line), with a `CallHost`
    and a `CallStatus` of its own and a `CallRouter` watching it.
    `command` reaches the remote's protocol on stdin and stdout;
    `accent` is its colour on the call.
    """

    def __init__(self, *, name: str, command: list[str] | tuple[str, ...],
                 accent: str | None = None,
                 workspace: str | None = None) -> None:
        self.name = name
        self.command = list(command)
        self.accent = accent
        # Where a relative link in a reply would be opened from.
        self.workspace = workspace or os.path.expanduser("~")
        self.jobs = _NoJobs()

        # ── mirrored from the remote ────────────────────────────────
        self.phase = "offline"
        self.busy = False
        self.model: str | None = None
        self.effort: str | None = None
        self.session_id: str | None = None
        self.error: str | None = None
        self.hint: str | None = None
        self._label = ""

        # ── the turn on show ────────────────────────────────────────
        self.heard = ""
        self.message = ""
        self.thought = ""
        self.activity: deque[Activity] = deque(maxlen=ACTIVITY_KEEP)
        self.approval: Approval | None = None
        self.hangup_line: str | None = None
        self.turn_started = 0.0
        self.queued: list[str] = []     # said while the line was down

        # ── ours ────────────────────────────────────────────────────
        self.call_open = False
        self.ringing: str | None = None     # "done" | "ask" | "error"
        self.ring_started = 0.0
        self.connected = False

        self._listeners: list[Callable[[str], None]] = []
        self._ask_id: str | None = None
        self._segments: list[str] = []
        self._seg_after: list[int] = []
        self._seg_you: list[bool] = []
        self._open_seg: int | None = None
        self._seg_map: dict = {}        # the remote's block numbers → ours
        self._sessions: list[_Session] = []
        self._last_source: str | None = None
        self._turn_source: str | None = None
        self._local_turn = False        # started here, not yet confirmed
        self._interrupted = False
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._stderr_tail = ""

    # ── service hooks ───────────────────────────────────────────────────
    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._line())

    def stop(self) -> None:
        self._stopping = True
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
        if self._task is not None:
            self._task.cancel()

    # ── listeners ───────────────────────────────────────────────────────
    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners \
            else None

    def _emit(self, event: str = "change") -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                log.exception("%s listener failed on %r", self.name, event)

    # ── what the call calls ─────────────────────────────────────────────
    def set_call_open(self, value: bool) -> None:
        if value == self.call_open:
            return
        self.call_open = value
        if value:
            self.ringing = None
            self.dial()
        elif self.approval is not None:
            self._ring("ask")
        self._emit()

    def dial(self) -> None:
        """Have the remote start its Claude Code now; bring the line back
        first if it is down."""
        if not self._send({"type": "dial"}):
            self._poke()

    def say(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not self.connected:
            self.queued.append(line)
            self._poke()
            self._emit()
            return
        if self.busy:
            # Mid-turn: into the turn's story where it was typed.
            self._segments.append(line)
            self._seg_you.append(True)
            self._seg_after.append(self.activity[-1].id if self.activity
                                   else 0)
            self._send({"type": "say", "text": line})
            self._emit()
            return
        self._begin_turn(line)
        self._local_turn = True
        self._turn_source = CLIENT
        self._send({"type": "say", "text": line})
        self._emit()

    def interrupt(self) -> None:
        self.queued.clear()
        if self.busy:
            self._interrupted = True
        self._send({"type": "interrupt"})
        self._emit()

    def acknowledge(self) -> None:
        if self.ringing in ("done", "error"):
            self.ringing = None
            self._emit()

    def answer(self, decision: str) -> None:
        if self._ask_id is not None:
            self._send({"type": "answer", "id": self._ask_id,
                        "decision": decision})

    def new_conversation(self) -> bool:
        if self.busy or not self.connected:
            return False
        self._send({"type": "new"})
        self._clear_turn()
        self.heard = ""
        self.error = self.hint = None
        self._emit()
        return True

    def cycle_model(self, step: int = 1) -> None:
        current = self.model if self.model in MODELS else MODELS[0]
        nxt = MODELS[(MODELS.index(current) + step) % len(MODELS)]
        if self._send({"type": "set", "model": nxt}):
            self.model = nxt

    def cycle_effort(self, step: int = 1) -> None:
        current = self.effort if self.effort in EFFORTS else DEFAULT_EFFORT
        nxt = EFFORTS[(EFFORTS.index(current) + step) % len(EFFORTS)]
        if self._send({"type": "set", "effort": nxt}):
            self.effort = nxt

    def model_label(self) -> str:
        return self._label or self.model or "default"

    def effort_label(self) -> str:
        effort = self.effort or DEFAULT_EFFORT
        return EFFORT_NAMES.get(effort, effort)

    def lines(self, limit: int = 40) -> list[_Session]:
        """Past conversations as last heard; asks for a fresh list too."""
        self._send({"type": "sessions"})
        return self._sessions[:limit]

    def resume_line(self, session_id: str) -> bool:
        if self.busy or not self.connected:
            return False
        if session_id != self.session_id:
            self._send({"type": "resume", "id": session_id})
        return True

    def job_of(self, _act: Activity):
        return None

    def loose_jobs(self) -> list:
        return []

    def said_parts(self) -> list[tuple[str, float, str]]:
        """As `Agent.said_parts`: the turn's words between its steps."""
        parts = [(f"{'y' if you else 'm'}{i}_", float(after), s.strip())
                 for i, (s, after, you) in enumerate(zip(
                     self._segments, self._seg_after, self._seg_you))
                 if s.strip()]
        if not any(k.startswith("m") for k, _, _ in parts) \
                and self.message.strip():
            parts.append(("m", float("inf"), self.message.strip()))
        return parts

    # ── the turn on show ────────────────────────────────────────────────
    def _begin_turn(self, heard: str) -> None:
        self.busy = True
        self.ringing = None
        self.heard = heard
        self._clear_turn()
        self.error = self.hint = None
        self._interrupted = False
        self.turn_started = time.monotonic()
        self.phase = "thinking"

    def _clear_turn(self) -> None:
        self.message = self.thought = ""
        self.hangup_line = None
        self.activity.clear()
        self._segments, self._seg_after, self._seg_you = [], [], []
        self._open_seg = None
        self._seg_map = {}

    def _new_segment(self, value: str) -> int:
        self._segments.append(value)
        self._seg_you.append(False)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)
        self._open_seg = len(self._segments) - 1
        return self._open_seg

    def _said(self) -> str:
        return "\n\n".join(s.strip() for s, you in zip(self._segments,
                                                        self._seg_you)
                           if s.strip() and not you)

    def _ring(self, why: str) -> None:
        self.ringing = why
        self.ring_started = time.monotonic()
        self._emit("ring")

    # ── the line ────────────────────────────────────────────────────────
    def _send(self, message: dict) -> bool:
        writer = self._writer
        if writer is None or writer.is_closing():
            return False
        try:
            writer.write((json.dumps(message) + "\n").encode())
        except (ConnectionError, RuntimeError):
            return False
        return True

    def _poke(self) -> None:
        """Cut the pause before the next try short."""
        if self._wake is not None:
            self._wake.set()

    async def _line(self) -> None:
        pause = RETRY_MIN_S
        while not self._stopping:
            began = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: the line failed", self.name)
            self._lost()
            if self._stopping:
                return
            if time.monotonic() - began > HEALTHY_S:
                pause = RETRY_MIN_S
            assert self._wake is not None
            # Wait out the pause; a poke (a line said, the call opened)
            # cuts it short, but never below the minimum.
            await asyncio.sleep(RETRY_MIN_S)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(),
                                       max(0.0, pause - RETRY_MIN_S))
            self._wake.clear()
            pause = min(pause * 2, RETRY_MAX_S)

    async def _session(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE, start_new_session=True)
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None
        self._writer = proc.stdin
        self._stderr_tail = ""
        stderr = asyncio.get_running_loop().create_task(self._read_stderr(proc))
        try:
            self._send({"type": "hello", "client": CLIENT})
            self._send({"type": "sessions"})
            if self.call_open:
                self._send({"type": "dial"})
            while line := await proc.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    log.warning("%s: not a protocol line: %r", self.name,
                                line[:200])
                    continue
                if isinstance(event, dict):
                    try:
                        self._on_event(event)
                    except Exception:
                        log.exception("%s: event %r failed", self.name,
                                      event.get("type"))
        finally:
            self._writer = None
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), 3)
            with contextlib.suppress(asyncio.TimeoutError,
                                     asyncio.CancelledError):
                await asyncio.wait_for(stderr, 1)

    async def _read_stderr(self, proc) -> None:
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr_tail = text
                log.debug("%s: %s", self.name, text)

    def _lost(self) -> None:
        was_connected = self.connected
        self.connected = False
        if was_connected:
            log.info("%s: the line dropped", self.name)
        if self.busy:
            # Its end won't reach us now; the remote keeps working, and
            # the next hello shows where it got to.
            self.busy = False
        self.approval = None
        self._ask_id = None
        if self.ringing == "ask":
            self.ringing = None
        self.phase = "error"
        why = self._stderr_tail
        self.error = f"Can't reach {self.name.lower()}" + (f": {why}"
                                                            if why else ".")
        self.hint = None
        self._emit()

    # ── events ──────────────────────────────────────────────────────────
    def _apply_state(self, st: dict) -> None:
        self.phase = st.get("phase") or self.phase
        self.busy = bool(st.get("busy"))
        self.model = st.get("model") or self.model
        self.effort = st.get("effort") or self.effort
        self._label = st.get("model_label") or self._label
        self.session_id = st.get("session")
        self.error = st.get("error")
        self.hint = st.get("hint")

    def _show_turn(self, turn: dict) -> None:
        """Replace the turn on show with the remote's account of it."""
        self._clear_turn()
        self.heard = turn.get("heard") or ""
        self.message = turn.get("message") or ""
        for step in turn.get("steps") or []:
            self._add_step(step)

    def _add_step(self, ev: dict) -> None:
        act = Activity(
            "tool", str(ev.get("text") or ""),
            detail=str(ev.get("detail") or ""), sub=bool(ev.get("sub")),
            tool_id=str(ev.get("id") or ""), tool=str(ev.get("tool") or ""),
            command=str(ev.get("detail") or "").split("\n# ")[0]
            if ev.get("tool") == "Bash" else "")
        if ev.get("result") is not None:
            act.result = str(ev["result"])
            act.failed = bool(ev.get("failed"))
        self.activity.append(act)

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        event = "change"
        if kind == "hello":
            self.connected = True
            log.info("%s: on the line (protocol %s)", self.name,
                     ev.get("protocol"))
            self._apply_state(ev.get("state") or {})
            if self.busy or not self._local_turn:
                self._show_turn(ev.get("turn") or {})
            if self.busy:
                self.turn_started = time.monotonic()
            ask = ev.get("ask")
            if ask:
                self._on_event({"type": "ask", **ask})
            if self.queued:                 # said while the line was down
                if not self.busy:
                    self._begin_turn(self.queued[0])
                    self._local_turn = True
                    self._turn_source = CLIENT
                for line in self.queued:
                    self._send({"type": "say", "text": line})
                self.queued.clear()
        elif kind == "state":
            self._apply_state(ev)
        elif kind == "user":
            self._last_source = ev.get("source")
            if self.busy and ev.get("source") != CLIENT:
                # Another client's line, mid-turn.
                self._segments.append(str(ev.get("text") or ""))
                self._seg_you.append(True)
                self._seg_after.append(self.activity[-1].id
                                       if self.activity else 0)
        elif kind == "turn_start":
            heard = str(ev.get("heard") or "")
            if self._local_turn and heard == self.heard:
                self._local_turn = False       # ours, already on show
            else:
                self._local_turn = False
                self._begin_turn(heard)
                self._turn_source = self._last_source
            self._last_source = None
        elif kind == "thinking":
            self.thought += str(ev.get("text") or "")
        elif kind == "reply_start":
            self._seg_map[ev.get("seg")] = self._new_segment("")
            self.phase = "speaking"
        elif kind == "reply_delta":
            seg = self._seg_map.get(ev.get("seg"))
            if seg is None:
                seg = self._seg_map[ev.get("seg")] = self._new_segment("")
            self._segments[seg] += str(ev.get("text") or "")
            self.message = self._said()
        elif kind == "step":
            self.thought = ""
            self._open_seg = None
            self._add_step(ev)
        elif kind == "step_result":
            for act in reversed(self.activity):
                if act.tool_id and act.tool_id == ev.get("id"):
                    act.result = str(ev.get("result") or "")
                    act.failed = bool(ev.get("failed"))
                    break
        elif kind == "on_it":
            self.hangup_line = str(ev.get("text") or "") or "on it"
            self._new_segment(self.hangup_line)
            self.message = self._said()
            event = "hangup"
        elif kind == "ask":
            self._ask_id = str(ev.get("id") or "")
            self.approval = Approval(
                tool=str(ev.get("tool") or ""),
                title=str(ev.get("title") or "a question"),
                detail=str(ev.get("detail") or ""),
                future=asyncio.get_running_loop().create_future())
            if not self.call_open:
                self._ring("ask")
        elif kind == "ask_done":
            if ev.get("id") == self._ask_id:
                self.approval = None
                self._ask_id = None
                if self.ringing == "ask":
                    self.ringing = None
        elif kind == "turn_end":
            self.busy = False
            self._local_turn = False
            final = str(ev.get("message") or "").strip() or self._said()
            if final:
                self.message = final
            if not ev.get("ok"):
                self.phase = "error"
                self.error = ev.get("error") or "the turn failed"
                self.hint = ev.get("hint")
            ours = self._turn_source in (None, CLIENT)
            if not self.call_open and not self._interrupted and ours:
                self._ring("done" if ev.get("ok") else "error")
            self._turn_source = None
            self._send({"type": "sessions"})
        elif kind == "notice":
            self.activity.append(Activity("info", str(ev.get("text") or "")))
        elif kind == "turn_view":
            self._local_turn = False
            self._show_turn(ev)
        elif kind == "sessions":
            self._sessions = [_Session(i) for i in ev.get("items") or []
                              if isinstance(i, dict)]
            return                          # nothing on show changed
        elif kind == "refused":
            self.activity.append(Activity(
                "info", f"{ev.get('request')}: {ev.get('reason')}"))
        elif kind == "pong":
            return
        self._emit(event)
