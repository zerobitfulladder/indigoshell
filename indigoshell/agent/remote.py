"""A call to the agent on the hub: lilac, on a Raspberry Pi.

`RemoteAgent` stands in for `Agent` (session.py) wherever the call
overlay, its corner chip and `CallRouter` read one: the same attributes
and methods, kept from the hub's events instead of a Claude Code process
of our own. The hub's agent is one mind with two voices, Lilac and
Indigo; lilac's PROTOCOL.md is the reference for how devices talk to it.

**The line:** a WebSocket over TLS to the hub's gateway, with this
machine's own certificate (`tls`: the hub's CA, this device's
certificate and key). The addresses in `endpoints` are tried in order,
so the home one goes first and the public one after it. Only the hub's
CA is trusted, and its certificate must carry the name `server_name`,
whatever address answered. The hub names this device from its
certificate (`welcome.device`); the call says only what it is: a `call`
client, a screen, and the hands it lends.

**It stays up** while the shell runs, so a turn that ends while the call
is closed still rings. It comes back by itself when either machine was
away, with a growing pause between tries; opening the call or saying a
line cuts the pause short.

**Voices:** the lines said in this call go to the agent in `voice`. The
conversation is shared, so the turn on show may be in the other voice
(asked from the phone, say): `name` and `accent`, which the overlay
paints the agent's plate and chip with, follow the voice of the turn on
show, in the name and colour the hub gives it (`welcome.voices`).

**Ringing:** the hub says who rings (its `ring` event names devices,
`*` for all): what was asked from this laptop rings here, what was asked
from another device rings there, and here too if that device can't be
reached or doesn't answer. The hub keeps the last ended turn unseen
until a screen reports it `seen`: this call does when it shows it (the
call open as it ends, opened later, or the ring answered), and rings on
reconnecting for one meant for this device that came while the line was
down; so does an open question meant for it. When another screen
reports a turn seen, the ring stops. `ring_line`
is the notification the agent wrote for it, for the corner chip.

**Devices:** `devices` is the hub's device list (who is online, when the
others were last seen, the hands each lends); the call shows it on
Ctrl+D.

**Hands:** `hands` (hands.py) are the tools this machine lends the
agent; the hub sends their calls down this line, and their results go
back up it.

What only the desktop's own agent has stays empty here: the glance at
the desktop and the progress bars of long commands (`jobs`).
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import ssl
import time
from collections import deque
from typing import Callable
from urllib.parse import urlsplit

from .activity import Activity, Approval
from .hands import Hands
from .session import (ACTIVITY_KEEP, DEFAULT_EFFORT, EFFORT_NAMES, EFFORTS,
                      MODELS)

log = logging.getLogger(__name__)

PROTOCOL = 1
# What this connection is to the hub.
CLIENT = "call"
MAX_MESSAGE = 16 * 1024 * 1024
RETRY_MIN_S = 5.0
RETRY_MAX_S = 120.0
# A line that held this long was healthy: the next try comes quickly.
HEALTHY_S = 60.0
# How long to give the first address (home) and the others (away).
HOME_TIMEOUT_S = 3.0
AWAY_TIMEOUT_S = 10.0
# How long the hub has to answer `hello` with `welcome`.
WELCOME_TIMEOUT_S = 10.0
PING_S = 20
# The hub's phases that the overlay knows by another name.
PHASES = {"starting": "dialing"}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


# ── the line ────────────────────────────────────────────────────────────
class _Link:
    """The protocol over a WebSocket with TLS: one JSON object per text
    message."""

    def __init__(self, ws, url: str) -> None:
        self.ws = ws
        self.url = url
        self.error = ""

    @classmethod
    async def open(cls, endpoints: list[str], tls: dict,
                   server_name: str) -> "_Link":
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(os.path.expanduser(tls["ca"]))
        ctx.load_cert_chain(os.path.expanduser(tls["cert"]),
                            os.path.expanduser(tls["key"]))
        problems = []
        for i, url in enumerate(endpoints):
            try:
                ws = await connect(
                    url, ssl=ctx, server_hostname=server_name,
                    open_timeout=HOME_TIMEOUT_S if i == 0 else AWAY_TIMEOUT_S,
                    max_size=MAX_MESSAGE, compression=None,
                    ping_interval=PING_S, ping_timeout=PING_S)
                return cls(ws, url)
            except (OSError, asyncio.TimeoutError, ssl.SSLError,
                    WebSocketException) as e:
                problems.append(f"{urlsplit(url).hostname}: "
                                f"{_why(e, server_name)}")
        raise ConnectionError("; ".join(problems))

    async def recv(self) -> str | None:
        from websockets.exceptions import ConnectionClosed
        try:
            message = await self.ws.recv()
        except ConnectionClosed as e:
            rcvd = e.rcvd
            if rcvd is not None and rcvd.code == 4403:
                self.error = "the gateway doesn't know this device"
            elif rcvd is not None and rcvd.reason:
                self.error = rcvd.reason
            return None
        return message if isinstance(message, str) else message.decode()

    async def send(self, text: str) -> None:
        await self.ws.send(text)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()


def _why(e: BaseException, server_name: str) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "no answer"
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"not {server_name}'s certificate"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror.lower()
    return str(e) or type(e).__name__


# ── what the call reads ─────────────────────────────────────────────────
class _NoJobs:
    """No progress bars for the hub's commands."""

    def shown(self) -> list:
        return []

    def shape(self) -> tuple:
        return ()

    def by_step(self, _step: str):
        return None


class _Session:
    """A past conversation, as the call's history list reads one (the
    fields of the SDK's session info, which the local agent lists)."""

    __slots__ = ("session_id", "summary", "first_prompt", "last_modified")

    def __init__(self, item: dict) -> None:
        self.session_id = str(item.get("session") or "")
        self.summary = item.get("title")
        self.first_prompt = None
        modified = item.get("modified")
        # Milliseconds, as the SDK counts them.
        self.last_modified = float(modified) * 1000 \
            if isinstance(modified, (int, float)) else 0


def _device(item: dict) -> dict:
    """One entry of the hub's device list, with every field there."""
    def names(value) -> list[str]:
        return [str(v) for v in value] if isinstance(value, list) else []

    seen = item.get("last_seen")
    return {"name": str(item.get("name") or "?"),
            "online": bool(item.get("online")),
            "last_seen": float(seen) if isinstance(seen, (int, float))
            else None,
            "clients": names(item.get("clients")),
            "hands": names(item.get("hands")),
            "wakes": bool(item.get("wakes"))}


def _notify_line(notify) -> str | None:
    """A ring's notification as one line: "title — text"."""
    if not isinstance(notify, dict):
        return None
    parts = (notify.get("title"), notify.get("text"))
    return " — ".join(str(p) for p in parts if p) or None


class RemoteAgent:
    """The hub's agent, read like the desktop's `Agent`.

    A SERVICES entry (it needs the loop for its line), with a `CallHost`
    and a `CallStatus` of its own and a `CallRouter` watching it. Give it
    the `voice` its lines are said in, `endpoints` and `tls`, and the
    `hands` this machine lends (hands.py), if any. `accent` is the
    voice's colour until the hub gives its own.
    """

    def __init__(self, *, voice: str, endpoints: list[str], tls: dict,
                 server_name: str = "lilac", hands: Hands | None = None,
                 accent: str | None = None,
                 workspace: str | None = None) -> None:
        if not endpoints:
            raise ValueError("RemoteAgent needs the hub's endpoints")
        if not all(tls.get(k) for k in ("ca", "cert", "key")):
            raise ValueError("RemoteAgent needs tls: {ca, cert, key}")
        self.voice = voice
        self.endpoints = list(endpoints)
        self.tls = dict(tls)
        self.server_name = server_name
        self.hands = hands
        self._accent = accent
        # Where a relative link in a reply would be opened from.
        self.workspace = workspace or os.path.expanduser("~")
        self.jobs = _NoJobs()
        self._tag = f"{voice} call"     # in the log

        # ── mirrored from the hub ───────────────────────────────────
        self.phase = "offline"
        self.busy = False
        self.model: str | None = None
        self.effort: str | None = None
        self.session_id: str | None = None
        self.error: str | None = None
        self.hint: str | None = None
        self.device: str | None = None      # this device, as the hub names it
        self.devices: list[dict] = []
        self._label = ""
        self._voices: dict[str, dict] = {}

        # ── the turn on show ────────────────────────────────────────
        self.shown_voice = voice            # whose turn is on show
        self.heard = ""
        self.heard_on: str | None = None    # the device it was asked on
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
        self.ring_line: str | None = None   # the agent's words for it
        self.connected = False
        self.via = ""                   # the address the line is on

        self._listeners: list[Callable[[str], None]] = []
        self._turn = None               # the hub's id of the turn on show
        self._ask_id: str | None = None
        self._ring_turn = None          # the turn rung for
        self._unseen = None             # the hub's unseen turn
        self._seen_sent = None
        self._mine: deque[str] = deque()    # said here, not yet echoed
        self._local_turn = False        # begun here, the hub's `turn` due
        self._turn_ours = False         # the turn on show was asked here
        self._segments: list[str] = []
        self._seg_after: list[int] = []
        self._seg_you: list[bool] = []
        self._open_seg: int | None = None
        self._seg_map: dict = {}        # the hub's block numbers → ours
        self._sessions: list[_Session] = []
        self._interrupted = False
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._why_down = ""

    # ── who speaks ──────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        """The voice of the turn on show, as the call's heading names it."""
        voice = self._voices.get(self.shown_voice) or {}
        return str(voice.get("name") or self.shown_voice).upper()

    @property
    def accent(self) -> str | None:
        """The colour of the voice of the turn on show."""
        voice = self._voices.get(self.shown_voice) or {}
        if voice.get("color"):
            return voice["color"]
        return self._accent if self.shown_voice == self.voice else None

    # ── service hooks ───────────────────────────────────────────────────
    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._line())

    def stop(self) -> None:
        self._stopping = True
        if self.hands is not None:
            self.hands.drop()
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
                log.exception("%s: listener failed on %r", self._tag, event)

    # ── what the call calls ─────────────────────────────────────────────
    def set_call_open(self, value: bool) -> None:
        if value == self.call_open:
            return
        self.call_open = value
        if value:
            # Opening shows the last turn: that answers a ring, and the
            # turn is seen wherever it rings.
            self._answered()
            self.ringing = None
            if self._unseen is not None and not self.busy:
                self._seen(self._unseen)
            self.dial()
        elif self.approval is not None:
            self._ring("ask")
        self._emit()

    def dial(self) -> None:
        """Have the hub start its Claude Code now; bring the line back
        first if it is down."""
        if not self._send({"type": "warm"}):
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
            self._you_segment(line)
        else:
            self._begin_turn(line)
            self._local_turn = self._turn_ours = True
            self.shown_voice = self.voice
        self._mine.append(line)
        self._send({"type": "say", "text": line, "voice": self.voice})
        self._emit()

    def interrupt(self) -> None:
        self.queued.clear()
        if self.busy:
            self._interrupted = True
        self._send({"type": "stop"})
        self._emit()

    def acknowledge(self) -> None:
        if self.ringing in ("done", "error"):
            self._answered()
            self.ringing = None
            self._emit()

    def answer(self, decision: str) -> None:
        if self._ask_id is not None:
            self._send({"type": "answer", "ask": self._ask_id,
                        "decision": decision})

    def new_conversation(self) -> bool:
        if self.busy or not self.connected:
            return False
        self._send({"type": "new"})
        self._clear_turn()
        self.heard = ""
        self.heard_on = None
        self.shown_voice = self.voice
        self.error = self.hint = None
        self._emit()
        return True

    def cycle_model(self, step: int = 1) -> None:
        current = self.model if self.model in MODELS else MODELS[0]
        nxt = MODELS[(MODELS.index(current) + step) % len(MODELS)]
        if self._send({"type": "set", "model": nxt}):
            self.model = nxt
            self._label = ""

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
        self._send({"type": "history"})
        return self._sessions[:limit]

    def resume_line(self, session_id: str) -> bool:
        if self.busy or not self.connected:
            return False
        if session_id != self.session_id:
            self._send({"type": "resume", "session": session_id})
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
        self.heard_on = None
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
        self._turn = None

    def _new_segment(self, value: str) -> int:
        self._segments.append(value)
        self._seg_you.append(False)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)
        self._open_seg = len(self._segments) - 1
        return self._open_seg

    def _you_segment(self, line: str) -> None:
        """A line the user said mid-turn, here or on another screen."""
        self._segments.append(line)
        self._seg_you.append(True)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)

    def _said(self) -> str:
        return "\n\n".join(s.strip() for s, you in zip(self._segments,
                                                        self._seg_you)
                           if s.strip() and not you)

    def _show_view(self, view: dict) -> None:
        """Replace the turn on show with the hub's account of it. Its
        turn number and its reply block by block, so that a turn still
        running carries on where it is: later events join that turn, and
        a block still streaming grows on the end of its own text."""
        self._clear_turn()
        self._local_turn = self._turn_ours = False
        self._turn = view.get("turn")
        self.heard = str(view.get("line") or "")
        self.heard_on = None
        self.shown_voice = str(view.get("voice") or self.voice)
        for step in view.get("steps") or []:
            if isinstance(step, dict):
                self._add_step(step)
        # The order of the reply's blocks among the steps isn't told:
        # the reply goes after them, as it mostly ends a turn.
        for block in view.get("blocks") or []:
            if isinstance(block, dict) and block.get("block") is not None:
                self._seg_map[block["block"]] = self._new_segment(
                    str(block.get("text") or ""))
        self._open_seg = None
        self.message = str(view.get("text") or "") or self._said()

    def _add_step(self, ev: dict) -> None:
        detail = str(ev.get("detail") or "")
        device = ev.get("device")
        act = Activity(
            "tool", str(ev.get("title") or ev.get("tool") or "a step"),
            detail=detail, sub=bool(ev.get("sub")),
            tool_id=str(ev.get("id") or ""), tool=str(ev.get("tool") or ""),
            command=detail.split("\n# ")[0] if ev.get("tool") == "Bash"
            else "",
            # Where it ran, at the end of its line: a device's hands.
            summary=f"on {device}" if device else "")
        if ev.get("result") is not None:
            act.result = str(ev["result"])
            act.failed = bool(ev.get("failed"))
        self.activity.append(act)

    def _ring(self, why: str, turn=None, line: str | None = None) -> None:
        if why == self.ringing and (why == "ask" or turn == self._ring_turn):
            self.ring_line = line or self.ring_line     # already ringing
            return
        self.ringing = why
        self.ring_started = time.monotonic()
        self.ring_line = line
        if why != "ask":
            self._ring_turn = turn
        self._emit("ring")

    def _for_me(self, to) -> bool:
        if not isinstance(to, list):
            return True                 # better a ring than a missed one
        return "*" in to or (self.device is not None and self.device in to)

    def _answered(self) -> None:
        """The user saw the turn rung for: tell the hub, so other devices
        stop ringing for it too."""
        if self._ring_turn is not None:
            self._seen(self._ring_turn)
            self._ring_turn = None

    def _seen(self, turn) -> None:
        if turn is not None and turn != self._seen_sent:
            if self._send({"type": "seen", "turn": turn}):
                self._seen_sent = turn

    # ── the line ────────────────────────────────────────────────────────
    def _send(self, message: dict) -> bool:
        if self._outbox is None:
            return False
        self._outbox.put_nowait(json.dumps(message, ensure_ascii=False))
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
            except ConnectionError as e:
                self._why_down = str(e)
                log.info("%s: can't reach the hub: %s", self._tag, e)
            except Exception as e:
                self._why_down = str(e)
                log.exception("%s: the line failed", self._tag)
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

    def _hello(self) -> dict:
        hello = {"type": "hello", "client": CLIENT, "screen": True}
        if self.hands is not None:
            hello["hands"] = self.hands.lent()
        return hello

    async def _session(self) -> None:
        self._why_down = ""
        link = await _Link.open(self.endpoints, self.tls, self.server_name)
        self.via = urlsplit(link.url).hostname or link.url
        outbox: asyncio.Queue = asyncio.Queue()
        self._outbox = outbox

        async def write() -> None:
            while True:
                await link.send(await outbox.get())

        def reply(message: dict) -> None:
            # A call's result goes back on the line the call came in on,
            # or nowhere: the hub fails calls whose line dropped.
            if self._outbox is outbox:
                outbox.put_nowait(json.dumps(message, ensure_ascii=False))

        writer = asyncio.get_running_loop().create_task(write())
        try:
            self._send(self._hello())
            first = await asyncio.wait_for(link.recv(), WELCOME_TIMEOUT_S)
            if first is None:
                raise ConnectionError(link.error or "the hub hung up")
            welcome = self._decode(first)
            if welcome is None or welcome.get("type") != "welcome" \
                    or not isinstance(welcome.get("voices"), list):
                raise ConnectionError("the hub speaks another protocol "
                                      f"(this call speaks {PROTOCOL})")
            if welcome.get("protocol") != PROTOCOL:
                raise ConnectionError(
                    f"the hub speaks protocol {welcome.get('protocol')} "
                    f"(this call speaks {PROTOCOL})")
            self._on_welcome(welcome)
            while (text := await link.recv()) is not None:
                event = self._decode(text)
                if event is None:
                    continue
                kind = event.get("type")
                try:
                    if kind == "call":
                        self._on_call(event, reply)
                    elif kind == "cancel":
                        if self.hands is not None:
                            self.hands.cancel(str(event.get("call") or ""))
                    else:
                        self._on_event(event)
                except Exception:
                    log.exception("%s: event %r failed", self._tag, kind)
            self._why_down = link.error
        except asyncio.TimeoutError:
            raise ConnectionError("the hub didn't answer hello") from None
        finally:
            self._outbox = None
            writer.cancel()
            await link.close()

    def _decode(self, text: str) -> dict | None:
        try:
            event = json.loads(text)
        except ValueError:
            log.warning("%s: not a protocol message: %r", self._tag,
                        text[:200])
            return None
        return event if isinstance(event, dict) else None

    def _on_call(self, call: dict, reply: Callable[[dict], None]) -> None:
        """The agent uses one of this machine's hands."""
        if self.hands is None:
            reply({"type": "result", "call": call.get("id"), "ok": False,
                   "content": [], "error": "this connection lends no hands"})
            return
        self.hands.serve(call, reply)

    def _lost(self) -> None:
        if self.connected:
            log.info("%s: the line dropped%s", self._tag,
                     f" ({self._why_down})" if self._why_down else "")
        self.connected = False
        if self.hands is not None:
            self.hands.drop()
        if self.busy:
            # Its end won't reach us now; the hub keeps working, and the
            # next welcome shows where it got to.
            self.busy = False
        self._local_turn = False
        self._mine.clear()
        self.approval = None
        self._ask_id = None
        if self.ringing == "ask":
            self.ringing = None
        self.phase = "error"
        why = self._why_down
        self.error = "Can't reach the hub" + (f": {why}" if why else ".")
        self.hint = None
        self._emit()

    # ── events ──────────────────────────────────────────────────────────
    def _apply_state(self, st: dict) -> None:
        phase = st.get("phase")
        if self._local_turn and not st.get("busy"):
            # A line said here that the hub hasn't started on yet: keep
            # the turn the call already shows.
            pass
        else:
            self.busy = bool(st.get("busy"))
            if phase:
                self.phase = PHASES.get(phase, phase)
        self.model = st.get("model") or self.model
        self.effort = st.get("effort") or self.effort
        self._label = st.get("model_label") or self._label
        self.session_id = st.get("session")
        if not self._local_turn:
            self.error = st.get("error")
            self.hint = st.get("hint")

    def _on_welcome(self, ev: dict) -> None:
        self.connected = True
        self.device = str(ev.get("device") or "") or None
        voices = {}
        for v in ev.get("voices") or []:
            if isinstance(v, dict) and v.get("id"):
                color = v.get("color")
                voices[str(v["id"])] = {
                    "name": str(v.get("name") or v["id"]),
                    "color": color if isinstance(color, str)
                    and _COLOR.fullmatch(color) else None}
        self._voices = voices
        if self.voice not in voices:
            log.warning("%s: the hub has no voice %r (it has %s)", self._tag,
                        self.voice, ", ".join(voices) or "none")
        log.info("%s: on the line via %s as %s", self._tag, self.via,
                 self.device)
        self._apply_state(ev.get("state") or {})
        self._show_view(ev.get("view") or {})
        if self.busy:
            self.turn_started = time.monotonic()
        self.approval = None
        self._ask_id = None
        if isinstance(ev.get("ask"), dict):
            self._on_event({"type": "ask", **ev["ask"]})
        self.devices = [_device(d) for d in ev.get("devices") or []
                        if isinstance(d, dict)]
        self._seen_sent = None            # the hub may count afresh
        unseen = ev.get("unseen")
        self._unseen = unseen.get("turn") if isinstance(unseen, dict) \
            else None
        if self._unseen is None:
            if self.ringing in ("done", "error"):
                self.ringing = None     # seen elsewhere while we were away
                self._ring_turn = None
        elif not self.busy:
            # A turn that ended while the line was down.
            if self.call_open:
                self._seen(self._unseen)
            elif self._for_me(unseen.get("to")):
                self._ring("done" if unseen.get("ok") else "error",
                           self._unseen, _notify_line(unseen.get("notify")))
        if self.queued:                     # said while the line was down
            if not self.busy:
                self._begin_turn(self.queued[0])
                self._local_turn = self._turn_ours = True
                self.shown_voice = self.voice
            else:
                for line in self.queued:
                    self._you_segment(line)
            for line in self.queued:
                self._mine.append(line)
                self._send({"type": "say", "text": line,
                            "voice": self.voice})
            self.queued.clear()
        self._send({"type": "history"})
        if self.call_open:
            self._send({"type": "warm"})
        self._emit()

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        event = "change"
        if kind in ("thinking", "text", "step", "step_done", "working") \
                and self._turn is None:
            self._turn = ev.get("turn")     # the turn on show, joined
        if kind == "state":
            self._apply_state(ev)
        elif kind == "line":
            text = str(ev.get("text") or "").strip()
            if self._mine and text == self._mine[0] \
                    and ev.get("device") == self.device \
                    and ev.get("client") == CLIENT \
                    and ev.get("voice") == self.voice:
                self._mine.popleft()        # ours, already on show
                return
            if self.busy and not self._local_turn:
                self._you_segment(text)     # another screen's, mid-turn
            else:
                return                      # its turn shows it
        elif kind == "turn":
            line = str(ev.get("line") or "")
            if self._local_turn and line.strip() == self.heard.strip():
                self._local_turn = False    # ours, already on show
            else:
                self._begin_turn(line)
                self._turn_ours = False
                device = ev.get("device")
                self.heard_on = str(device) if device \
                    and device != self.device else None
            self._turn = ev.get("id")
            self.shown_voice = str(ev.get("voice") or self.voice)
        elif kind == "thinking":
            self.thought += str(ev.get("text") or "")
        elif kind == "text":
            block = ev.get("block")
            seg = self._seg_map.get(block)
            if seg is None:
                seg = self._seg_map[block] = self._new_segment("")
            self._segments[seg] += str(ev.get("text") or "")
            self.message = self._said()
        elif kind == "step":
            self.thought = ""
            self._open_seg = None
            self._add_step(ev)
        elif kind == "step_done":
            for act in reversed(self.activity):
                if act.tool_id and act.tool_id == str(ev.get("id")):
                    act.result = str(ev.get("result") or "")
                    act.failed = bool(ev.get("failed"))
                    break
        elif kind == "working":
            self.hangup_line = str(ev.get("text") or "") or "on it"
            self._new_segment(self.hangup_line)
            self.message = self._said()
            if self._turn_ours:
                event = "hangup"            # asked here: hang up on it
        elif kind == "ask":
            self._ask_id = str(ev.get("id") or "")
            device = ev.get("device")
            self.approval = Approval(
                tool=str(device or ""),
                title=str(ev.get("title") or "a question"),
                detail=str(ev.get("detail") or ""),
                future=asyncio.get_running_loop().create_future())
            # Who is rung for it, once the hub has decided: a call that
            # comes back with the question still open rings if it is one.
            to = ev.get("to")
            if not self.call_open and isinstance(to, list) \
                    and self._for_me(to):
                self._ring("ask")
        elif kind == "answered":
            if self._ask_id is None or str(ev.get("id")) == self._ask_id:
                self.approval = None
                self._ask_id = None
                if self.ringing == "ask":
                    self.ringing = None
        elif kind == "done":
            self.busy = False
            self._local_turn = False
            self._mine.clear()
            self.shown_voice = str(ev.get("voice") or self.shown_voice)
            final = str(ev.get("text") or "").strip() or self._said()
            if final:
                self.message = final
            if not ev.get("ok"):
                self.phase = "error"
                self.error = ev.get("error") or "the turn failed"
                self.hint = ev.get("hint")
            turn = ev.get("turn")
            self._unseen = turn
            if self.call_open or self._interrupted:
                # Seen as it happened, or stopped on purpose.
                self._seen(turn)
            self._turn_ours = False
            self._send({"type": "history"})
        elif kind == "ring":
            if not self._for_me(ev.get("to")):
                return
            line = _notify_line(ev.get("notify"))
            if ev.get("what") == "ask":
                if not self.call_open and self.approval is not None:
                    self._ring("ask", line=line)
            elif self.call_open:
                self._seen(ev.get("id"))
            else:
                self._ring("done" if ev.get("ok") else "error",
                           ev.get("id"), line)
            return                          # _ring emitted, if it rang
        elif kind == "seen":
            turn = ev.get("turn")
            if turn == self._unseen:
                self._unseen = None
            # Another screen (or this one) showed it: stop ringing.
            if self.ringing in ("done", "error") and (
                    self._ring_turn is None or turn == self._ring_turn):
                self.ringing = None
                self._ring_turn = None
        elif kind == "note":
            self.activity.append(Activity("info", str(ev.get("text") or "")))
        elif kind == "view":
            self._show_view(ev)
        elif kind == "devices":
            self.devices = [_device(d) for d in ev.get("items") or []
                            if isinstance(d, dict)]
        elif kind == "history":
            self._sessions = [_Session(i) for i in ev.get("items") or []
                              if isinstance(i, dict)]
            return                          # nothing on show changed
        elif kind == "error":
            request, reason = ev.get("request"), ev.get("reason")
            log.warning("%s: the hub refused %s: %s", self._tag, request,
                        reason)
            if request == "say":
                if self._mine:
                    self._mine.popleft()
                if self._local_turn:        # the turn never started
                    self._local_turn = False
                    self.busy = False
                    self.phase = "idle"
            self.activity.append(Activity("info", f"{request}: {reason}"))
        else:
            return                          # not for a screen, or not known
        self._emit(event)
