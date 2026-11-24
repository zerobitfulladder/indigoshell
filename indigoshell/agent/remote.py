"""A call to the agent on the hub: lilac, on a Raspberry Pi.

`RemoteAgent` is what the call overlay, the top bar's chip and
`CallRouter` read: the conversation's attributes and methods, kept from
the hub's events. The hub's agent is one mind with two voices, Lilac and
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

**Voices:** each line said in the call goes to the agent in `voice`,
which the call switches (`switch_voice`, Ctrl+S) and remembers across
reloads; the voices themselves (names, colours) are the hub's, from
`welcome.voices`, and are remembered too, so the call can name them
while the line is down. The conversation is shared, so the turn on show
may be in the other voice (asked from the phone, or before a switch):
`name` and `accent`, which the overlay paints the agent's plate and
chip with, follow the voice of the turn on show.

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

**Look and shown things:** a line can carry what the user is looking at
(`say(..., attach=)`, the call's Look: Flux's tab, the selection, a
screenshot), and the agent can show a picture or a file in the chat
(`shown`): it takes its place in the turn's story where it arrives, and
its bytes come only when asked for (`want_media`, `fetch_media`: the
hub's `fetch` and `file`), cached by id. `save_media` puts one in
~/Downloads, never over another file.

**Devices:** `devices` is the hub's device list (who is online, when the
others were last seen, the hands each lends); the call shows it on
Ctrl+D.

**Hands:** `hands` (hands.py) are the tools this machine lends the
agent; the hub sends their calls down this line, and their results go
back up it. What a hand asks the user first (root, in laptop.py) is
asked here, in the call: the overlay's question, answered on this
laptop without the hub, and rung like the hub's when the call is
closed. It takes the screen over a question of the hub's, which comes
back once it is answered.

The call can draw progress bars of long commands (`jobs`); nothing on
the hub reports them yet, so they stay empty.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import ssl
import time
from collections import OrderedDict, deque
from typing import Callable
from urllib.parse import urlsplit

from ..core import state
from .activity import Activity, Approval
from .hands import Hands

log = logging.getLogger(__name__)

# What Ctrl+M and Ctrl+T cycle through in the call, sent to the hub with
# each line. Aliases: Claude Code resolves each to the newest model of
# its line.
MODELS = ("opus", "sonnet", "haiku", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# What the call calls them: chrome pushed harder. The hub keeps the real
# levels.
EFFORT_NAMES = {"low": "idle", "medium": "stock", "high": "boost",
                "xhigh": "overclock", "max": "redline"}
# Claude Code's own default for the current models, when none is set.
DEFAULT_EFFORT = "medium"
# Everything the agent did this turn, so the overlay can expand it.
ACTIVITY_KEEP = 80

PROTOCOL = 1
# What this connection is to the hub.
CLIENT = "call"
# Lines and turns of the hub's own (a job ended, a scheduled turn).
HUB_CLIENT = "hub"
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
# What a line may carry (the hub takes at most this many blocks).
ATTACH_MAX = 8
# Shown things' bytes kept at once, and the biggest fetched by itself
# (larger pictures show as files, fetched when opened).
MEDIA_KEEP = 24
MEDIA_AUTO_MAX = 12 * 1024 * 1024
FETCH_TIMEOUT_S = 60.0
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
    fields of the SDK's session info, as the hub lists them)."""

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


def save_download(name: str, data: bytes, folder: str = "~/Downloads"
                  ) -> str:
    """Write `data` into `folder` as `name` (its last part only), or as
    "name (2).ext", "name (3).ext"... when that is taken. Its path."""
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    base = os.path.basename(str(name).replace("\0", "")).strip() \
        .lstrip(".") or "file"
    stem, ext = os.path.splitext(base)
    for n in range(1, 1000):
        path = os.path.join(folder, base if n == 1 else f"{stem} ({n}){ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    raise FileExistsError(f"too many files named {base} in {folder}")


class RemoteAgent:
    """The hub's agent, as the call reads it.

    A SERVICES entry (it needs the loop for its line), with a `CallHost`
    and a `CallStatus` of its own and a `CallRouter` watching it. Give it
    the `voice` that answers until the call switches it, `endpoints` and
    `tls`, and the `hands` this machine lends (hands.py), if any.
    `state_key` names what it remembers in the shell's state file (the
    voice chosen, the hub's voices); None remembers nothing. `accent` is
    the voice's colour until the hub gives its own.
    """

    # A line can carry what the user is looking at (the call's Look).
    attachments = True
    # Pictures bigger than this show as files, fetched when opened.
    media_auto_max = MEDIA_AUTO_MAX

    def __init__(self, *, voice: str, endpoints: list[str], tls: dict,
                 server_name: str = "lilac", hands: Hands | None = None,
                 accent: str | None = None, state_key: str | None = "hub",
                 workspace: str | None = None) -> None:
        if not endpoints:
            raise ValueError("RemoteAgent needs the hub's endpoints")
        if not all(tls.get(k) for k in ("ca", "cert", "key")):
            raise ValueError("RemoteAgent needs tls: {ca, cert, key}")
        self.voice = voice              # who answers the next line
        self.default_voice = voice
        self.state_key = state_key
        self.endpoints = list(endpoints)
        self.tls = dict(tls)
        self.server_name = server_name
        self.hands = hands
        if hands is not None:
            hands.asker = self._ask_here
        self._accent = accent
        # Where a relative link in a reply would be opened from.
        self.workspace = workspace or os.path.expanduser("~")
        self.jobs = _NoJobs()
        self._tag = "hub call"          # in the log

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
        self._hub_ask: Approval | None = None     # the hub's question
        self._local_ask: Approval | None = None   # this laptop's own
        self.hangup_line: str | None = None
        self.turn_started = 0.0
        self.queued: list[str] = []     # said while the line was down
        self._queued_voices: list[str] = []
        self._queued_attach: list[list | None] = []
        self.heard_attached = 0         # blocks the line on show came with
        self._line_attached: tuple[str, int] = ("", 0)

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
        # Said here and not yet heard back: (text, voice).
        self._mine: deque[tuple[str, str]] = deque()
        self._local_turn = False        # begun here, the hub's `turn` due
        self._turn_ours = False         # the turn on show was asked here
        self._segments: list[str] = []
        self._seg_after: list[int] = []
        self._seg_you: list[bool] = []
        self._seg_shown: list[str | None] = []  # a shown thing's id
        # What the agent showed (`shown`), by id, and the bytes fetched.
        self.shown: dict[str, dict] = {}
        self._media: OrderedDict[str, dict] = OrderedDict()
        self._media_failed: dict[str, str] = {}
        self._fetching: list[str] = []      # asked for, in order
        self._media_waiters: dict[str, list[asyncio.Future]] = {}
        self.media_rev = 0                  # bumped when bytes arrive
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
        return self._color(self.shown_voice)

    def _color(self, voice_id: str) -> str | None:
        voice = self._voices.get(voice_id) or {}
        if voice.get("color"):
            return voice["color"]
        return self._accent if voice_id == self.default_voice else None

    @property
    def voice_switch(self) -> tuple[str, str | None]:
        """Who answers the next line, as the call's switch shows it: the
        voice's name and colour."""
        voice = self._voices.get(self.voice) or {}
        return (str(voice.get("name") or self.voice.capitalize()),
                self._color(self.voice))

    def switch_voice(self) -> None:
        """The next line goes to the other voice (the next, if the hub
        ever has more); remembered across reloads."""
        ids = list(self._voices) or [self.default_voice]
        if self.voice not in ids:
            ids.insert(0, self.voice)
        self.voice = ids[(ids.index(self.voice) + 1) % len(ids)]
        self._remember("voice", self.voice)
        if not self.busy and not self.heard and not self.message:
            self.shown_voice = self.voice   # an empty call: whose it is
        self._emit()

    def _remember(self, what: str, value) -> None:
        if self.state_key:
            state.set(f"{self.state_key}.{what}", value)

    def _recall(self, what: str):
        return state.get(f"{self.state_key}.{what}") if self.state_key \
            else None

    @property
    def approval(self) -> Approval | None:
        """The question on show: this laptop's own, else the hub's."""
        return self._local_ask or self._hub_ask

    # ── service hooks ───────────────────────────────────────────────────
    def start(self) -> None:
        voices = self._recall("voices")
        if isinstance(voices, dict):
            self._voices = {str(k): v for k, v in voices.items()
                            if isinstance(v, dict)}
        voice = self._recall("voice")
        if isinstance(voice, str) and voice:
            self.voice = self.shown_voice = voice
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

    def say(self, line: str, attach: list[dict] | None = None) -> None:
        """Say a line in the chosen voice; `attach`: what the user is
        looking at (the call's Look), up to ATTACH_MAX content blocks."""
        line = line.strip()
        if not line:
            return
        attach = list(attach or [])[:ATTACH_MAX] or None
        if not self.connected:
            self.queued.append(line)
            self._queued_voices.append(self.voice)
            self._queued_attach.append(attach)
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
            self.heard_attached = len(attach or ())
        self._say(line, self.voice, attach)
        self._emit()

    def _say(self, line: str, voice: str,
             attach: list[dict] | None = None) -> None:
        self._mine.append((line, voice))
        message = {"type": "say", "text": line, "voice": voice}
        if attach:
            message["attach"] = attach
        self._send(message)

    def interrupt(self) -> None:
        self.queued.clear()
        self._queued_voices.clear()
        self._queued_attach.clear()
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
        local = self._local_ask
        if local is not None:
            if not local.future.done():
                local.future.set_result(decision)
        elif self._ask_id is not None:
            self._send({"type": "answer", "ask": self._ask_id,
                        "decision": decision})

    async def _ask_here(self, title: str, detail: str,
                        timeout: float) -> bool:
        """A question from this machine's hands (hands.Call.ask), asked
        in the call and answered here; rung if the call is closed. No
        answer within `timeout` is a no. One at a time: a second waits
        for the first to be answered."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._local_ask is not None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.2)
        ask = Approval(tool=self.device or "laptop", title=title,
                       detail=detail, future=loop.create_future())
        self._local_ask = ask
        log.info("%s: asking the user: %s", self._tag, title)
        if not self.call_open:
            self._ring("ask", line=title)
        self._emit()
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(ask.future), max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            if self._local_ask is ask:
                self._local_ask = None
                if self.ringing == "ask" and self.approval is None:
                    self.ringing = None
                self._emit()
        log.info("%s: the user's answer: %s", self._tag, decision)
        return decision == "allow"

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
        """As `Agent.said_parts`: the turn's words between its steps. A
        thing the agent showed is a part too, its key starting with "s"
        and its id in place of the words."""
        parts = []
        for i, (s, after, you, shown) in enumerate(zip(
                self._segments, self._seg_after, self._seg_you,
                self._seg_shown)):
            if shown is not None:
                parts.append((f"s{i}_", float(after), shown))
            elif s.strip():
                parts.append((f"{'y' if you else 'm'}{i}_", float(after),
                              s.strip()))
        if not any(k.startswith("m") for k, _, _ in parts) \
                and self.message.strip():
            parts.append(("m", float("inf"), self.message.strip()))
        return parts

    # ── what the agent showed ───────────────────────────────────────────
    def media(self, sid: str) -> dict | None:
        """A shown thing's bytes, if here: {name, mime, data}."""
        return self._media.get(sid)

    def media_failed(self, sid: str) -> str | None:
        return self._media_failed.get(sid)

    def want_media(self, sid: str) -> None:
        """Have a shown thing's bytes fetched, if they aren't here or on
        their way (a picture on show asks for itself)."""
        if sid in self._media or sid in self._fetching \
                or sid in self._media_failed:
            return
        if self._send({"type": "fetch", "id": sid}):
            self._fetching.append(sid)

    async def fetch_media(self, sid: str,
                          timeout: float = FETCH_TIMEOUT_S) -> dict:
        """A shown thing's bytes, fetched if need be."""
        self._media_failed.pop(sid, None)
        if sid in self._media:
            return self._media[sid]
        fut = asyncio.get_running_loop().create_future()
        self._media_waiters.setdefault(sid, []).append(fut)
        self.want_media(sid)
        if sid not in self._fetching:
            raise ConnectionError("not on the line to the hub")
        try:
            got = await asyncio.wait_for(fut, timeout)
        finally:
            waiters = self._media_waiters.get(sid) or []
            if fut in waiters:
                waiters.remove(fut)
        if got is None:
            raise LookupError(self._media_failed.get(sid) or "not found")
        return got

    def _got_media(self, ev: dict) -> None:
        sid = str(ev.get("id") or "")
        try:
            data = base64.b64decode(str(ev.get("data") or ""))
        except ValueError:
            data = None
        if sid in self._fetching:
            self._fetching.remove(sid)
        if not sid or data is None:
            if sid:
                self._media_failed[sid] = "the hub sent it garbled"
                self._wake_media(sid, None)
            return
        item = {"name": str(ev.get("name") or self.shown.get(sid, {}).get(
                    "name") or sid),
                "mime": str(ev.get("mime") or ""), "data": data}
        self._media[sid] = item
        self._media.move_to_end(sid)
        while len(self._media) > MEDIA_KEEP:
            self._media.popitem(last=False)
        self._media_failed.pop(sid, None)
        self.media_rev += 1
        self._wake_media(sid, item)

    def _wake_media(self, sid: str, item: dict | None) -> None:
        for fut in self._media_waiters.pop(sid, []):
            if not fut.done():
                fut.set_result(item)
        self.media_rev += 1

    async def save_media(self, sid: str) -> str:
        """Put a shown thing in ~/Downloads, under its own name or, if
        that is taken, "name (2).ext": never over another file. Its path."""
        item = await self.fetch_media(sid)
        return await asyncio.to_thread(save_download, item["name"],
                                       item["data"])

    # ── the turn on show ────────────────────────────────────────────────
    def _begin_turn(self, heard: str) -> None:
        self.busy = True
        self.ringing = None
        self.heard = heard
        self.heard_on = None
        self.heard_attached = 0
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
        self._seg_shown = []
        self._open_seg = None
        self._seg_map = {}
        self._turn = None

    def _new_segment(self, value: str) -> int:
        self._segments.append(value)
        self._seg_you.append(False)
        self._seg_shown.append(None)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)
        self._open_seg = len(self._segments) - 1
        return self._open_seg

    def _you_segment(self, line: str) -> None:
        """A line the user said mid-turn, here or on another screen."""
        self._segments.append(line)
        self._seg_you.append(True)
        self._seg_shown.append(None)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)

    def _shown_segment(self, item: dict) -> None:
        """Something the agent showed, where it arrives in the turn."""
        sid = str(item.get("id") or "")
        if not sid or sid in self._seg_shown:
            return
        self.shown[sid] = {"id": sid,
                           "kind": "image" if item.get("kind") == "image"
                           else "file",
                           "name": str(item.get("name") or sid),
                           "mime": str(item.get("mime") or ""),
                           "size": item.get("size")
                           if isinstance(item.get("size"), int) else None,
                           "caption": str(item.get("caption") or "")}
        self._segments.append("")
        self._seg_you.append(False)
        self._seg_shown.append(sid)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)
        self._open_seg = None

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
        for item in view.get("shown") or []:
            if isinstance(item, dict):
                self._shown_segment(item)
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
            if self.hands is not None:
                # What the hands report by themselves (jobs that ended)
                # goes out on this line while it lasts.
                self.hands.online(reply)
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
            if self.hands is not None:
                self.hands.offline()
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
        self._fetching.clear()          # asked again on the next line
        self._hub_ask = None
        self._ask_id = None
        if self.ringing == "ask" and self._local_ask is None:
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
        if voices:
            self._voices = voices
            self._remember("voices", voices)
        if self.voice not in voices:
            fallback = self.default_voice if self.default_voice in voices \
                else next(iter(voices), self.voice)
            log.warning("%s: the hub has no voice %r (it has %s); %s answers",
                        self._tag, self.voice, ", ".join(voices) or "none",
                        fallback)
            self.voice = fallback
        log.info("%s: on the line via %s as %s", self._tag, self.via,
                 self.device)
        self._apply_state(ev.get("state") or {})
        self._show_view(ev.get("view") or {})
        if self.busy:
            self.turn_started = time.monotonic()
        self._hub_ask = None
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
            said = list(zip(self.queued, self._queued_voices,
                            self._queued_attach))
            self.queued.clear()
            self._queued_voices.clear()
            self._queued_attach.clear()
            if not self.busy:
                self._begin_turn(said[0][0])
                self._local_turn = self._turn_ours = True
                self.shown_voice = said[0][1]
                self.heard_attached = len(said[0][2] or ())
            else:
                for line, _voice, _attach in said:
                    self._you_segment(line)
            for line, voice, attach in said:
                self._say(line, voice if voice in self._voices
                          else self.voice, attach)
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
            if self._mine and (text, ev.get("voice")) == self._mine[0] \
                    and ev.get("device") == self.device \
                    and ev.get("client") == CLIENT:
                self._mine.popleft()        # ours, already on show
                return
            # What it came with, for its turn's start.
            self._line_attached = (text, int(ev.get("attached") or 0))
            if ev.get("client") == HUB_CLIENT:
                if not self.busy:
                    return                  # its turn shows it
                # The hub's own line, mid-turn: a note, not your words.
                self.activity.append(Activity("note", text))
            elif self.busy and not self._local_turn:
                self._you_segment(text)     # another screen's, mid-turn
            else:
                return                      # its turn shows it
        elif kind == "turn":
            line = str(ev.get("line") or "")
            if ev.get("client") == HUB_CLIENT:
                # A turn the hub started (a job ended, a scheduled one):
                # its line is a note at the top, not something you said.
                self._local_turn = False
                self._begin_turn("")
                self._turn_ours = False
                self.activity.append(Activity("note", line))
            elif self._local_turn and line.strip() == self.heard.strip():
                self._local_turn = False    # ours, already on show
            else:
                self._begin_turn(line)
                self._turn_ours = False
                said, attached = self._line_attached
                if said == line.strip():
                    self.heard_attached = attached
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
            self._hub_ask = Approval(
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
                self._hub_ask = None
                self._ask_id = None
                if self.ringing == "ask" and self._local_ask is None:
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
        elif kind == "shown":
            if self._turn is None and ev.get("turn") is not None:
                self._turn = ev.get("turn")
            self._shown_segment(ev)
        elif kind == "file":
            self._got_media(ev)
        elif kind == "history":
            self._sessions = [_Session(i) for i in ev.get("items") or []
                              if isinstance(i, dict)]
            return                          # nothing on show changed
        elif kind == "error":
            request, reason = ev.get("request"), ev.get("reason")
            log.warning("%s: the hub refused %s: %s", self._tag, request,
                        reason)
            if request == "fetch" and self._fetching:
                # The hub answers in order: the oldest fetch failed.
                self._media_failed[self._fetching[0]] = str(reason or
                                                            "not found")
                self._wake_media(self._fetching.pop(0), None)
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
