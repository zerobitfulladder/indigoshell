"""A call to the agent on the hub: lilac, on a Raspberry Pi.

`RemoteAgent` is what the call overlay, the top bar's chip and
`CallRouter` read: the chat's attributes and methods, kept from the
hub's events. The hub's agent is Lilac, in one chat that never ends;
lilac's PROTOCOL.md (protocol 2) is the reference for how devices talk
to it.

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

**Her:** the hub names her once, in its welcome (`welcome.agent`: Lilac,
#c8a2c8), and every reply and mark is drawn in that name and colour
(`name`, `accent`); the shell's apps hear it too (core/persona.py).

**The chat** is one conversation, shared with the other screens, and
compacted on the hub as it grows: `context` says how full it is (the
call's bar), and the phase `compacting` while it runs, which is not a
turn: `busy` stays False here and a line said meanwhile waits for it,
shown as asked. `compacted` is how it went.

**Scrollback:** the turn on show is the newest exchange; `exchanges`
holds the earlier ones the call has fetched (`scrollback`, a page at a
time, oldest first), only those she still holds word for word: the
hub's `edge` says whether older ones exist only in her memory
(`compacted`) or the chat begins there (`start`). A finished turn joins
them by its `done.exchange`; `forgotten` drops some; a compaction trims
them to its `from`.

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
import ssl
import time
from collections import OrderedDict, deque
from typing import Callable
from urllib.parse import urlsplit

from ..core import persona
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

PROTOCOL = 2
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
COMPACTING = "compacting"
# Earlier exchanges asked for at once (the hub's default; at most 100).
SCROLLBACK_PAGE = 30


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


def _exchange(item: dict) -> dict | None:
    """One exchange of the hub's scrollback, with every field there; None
    for one with no id."""
    xid = item.get("id")
    if not isinstance(xid, int) or isinstance(xid, bool):
        return None
    at = item.get("at")
    lines = item.get("lines")
    source = item.get("source")
    return {"id": xid,
            "at": float(at) if isinstance(at, (int, float)) else None,
            "source": source if source in ("user", "hub", "announcement")
            else "user",
            "device": str(item.get("device") or "") or None,
            "lines": [str(x) for x in lines] if isinstance(lines, list) else [],
            "reply": str(item.get("reply") or ""),
            "attached": int(item.get("attached") or 0),
            "shown": [x for x in item.get("shown") or [] if isinstance(x, dict)]}


def _context(value) -> dict | None:
    """`state.context`, its numbers checked: {used, compact_at, cap,
    window, from}, `used` None while the hub hasn't measured it."""
    if not isinstance(value, dict):
        return None

    def number(key):
        v = value.get(key)
        return int(v) if isinstance(v, (int, float)) \
            and not isinstance(v, bool) and v >= 0 else None

    out = {k: number(k) for k in ("used", "compact_at", "cap", "window", "from")}
    return out if out["cap"] else None


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
    of its own and a `CallRouter` watching it. Give it `endpoints` and
    `tls`, and the `hands` this machine lends (hands.py), if any.
    """

    # A line can carry what the user is looking at (the call's Look).
    attachments = True
    # Pictures bigger than this show as files, fetched when opened.
    media_auto_max = MEDIA_AUTO_MAX

    def __init__(self, *, endpoints: list[str], tls: dict,
                 server_name: str = "lilac", hands: Hands | None = None,
                 workspace: str | None = None) -> None:
        if not endpoints:
            raise ValueError("RemoteAgent needs the hub's endpoints")
        if not all(tls.get(k) for k in ("ca", "cert", "key")):
            raise ValueError("RemoteAgent needs tls: {ca, cert, key}")
        self.endpoints = list(endpoints)
        self.tls = dict(tls)
        self.server_name = server_name
        self.hands = hands
        if hands is not None:
            hands.asker = self._ask_here
        # Where a relative link in a reply would be opened from.
        self.workspace = workspace or os.path.expanduser("~")
        self.jobs = _NoJobs()
        self._tag = "hub call"          # in the log

        # ── mirrored from the hub ───────────────────────────────────
        self.phase = "offline"
        self.busy = False
        self.compacting = False         # the hub compacts the chat
        self.model: str | None = None
        self.effort: str | None = None
        self.error: str | None = None
        self.hint: str | None = None
        self.device: str | None = None      # this device, as the hub names it
        self.devices: list[dict] = []
        self._label = ""
        # How full the chat is: {used, compact_at, cap, window, from}.
        self.context: dict | None = None
        # The last compaction: {before, after, at}, `at` monotonic.
        self.compacted: dict | None = None

        # ── the scrollback ──────────────────────────────────────────
        self.exchanges: list[dict] = []     # earlier ones, oldest first
        self.more_back = True           # older ones to ask for
        self.edge: str | None = None    # with none: "start" | "compacted"
        self.back_loading = False       # a page asked for, not yet in
        self.back_rev = 0               # bumped whenever `exchanges` changes

        # ── the turn on show ────────────────────────────────────────
        self.exchange: int | None = None    # its id, once kept
        self.at: float | None = None        # when it began (Unix time)
        self.heard = ""                 # the line said last, on show
        self.lines: list[str] = []      # the turn's lines, the first first
        self.heard_on: str | None = None    # the device it was asked on
        self.message = ""
        self.thought = ""
        self.activity: deque[Activity] = deque(maxlen=ACTIVITY_KEEP)
        self._hub_ask: Approval | None = None     # the hub's question
        self._local_ask: Approval | None = None   # this laptop's own
        self.hangup_line: str | None = None
        self.turn_started = 0.0
        self.queued: list[str] = []     # said while the line was down
        self._queued_attach: list[list | None] = []
        self.heard_attached = 0         # blocks the line on show came with
        self.from_hub = False           # a turn the hub started
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
        # Said here and not yet heard back.
        self._mine: deque[str] = deque()
        self._local_turn = False        # begun here, the hub's `turn` due
        self._turn_ours = False         # the turn on show was asked here
        self._segments: list[str] = []
        self._seg_after: list[int] = []
        self._seg_shown: list[str | None] = []  # a shown thing's id
        # What the agent showed (`shown`), by id, and the bytes fetched.
        self.shown: dict[str, dict] = {}
        self._media: OrderedDict[str, dict] = OrderedDict()
        self._media_failed: dict[str, str] = {}
        self._fetching: list[str] = []      # asked for, in order
        self._media_waiters: dict[str, list[asyncio.Future]] = {}
        self.media_rev = 0                  # bumped when bytes arrive
        self._seg_map: dict = {}        # the hub's block numbers → ours
        self._interrupted = False
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._why_down = ""

    # ── her ─────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        """Her name, as the call's headings show it: "LILAC"."""
        return persona.name().upper()

    @property
    def accent(self) -> str:
        """Her colour, for every reply and mark."""
        return persona.color()

    @property
    def approval(self) -> Approval | None:
        """The question on show: this laptop's own, else the hub's."""
        return self._local_ask or self._hub_ask

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

    def say(self, line: str, attach: list[dict] | None = None) -> None:
        """Say a line; `attach`: what the user is looking at (the call's
        Look), up to ATTACH_MAX content blocks. While the hub compacts
        the chat, the line waits for it there, shown as asked."""
        line = line.strip()
        if not line:
            return
        attach = list(attach or [])[:ATTACH_MAX] or None
        if not self.connected:
            self.queued.append(line)
            self._queued_attach.append(attach)
            self._poke()
            self._emit()
            return
        if self.busy:
            self._said_into(line, len(attach or ()))
        else:
            self._begin_turn(line)
            self._local_turn = self._turn_ours = True
            self.heard_attached = len(attach or ())
        self._say(line, attach)
        self._emit()

    def _say(self, line: str, attach: list[dict] | None = None) -> None:
        self._mine.append(line)
        message = {"type": "say", "text": line}
        if attach:
            message["attach"] = attach
        self._send(message)

    def interrupt(self) -> None:
        """Stop the running turn. A compaction can't be stopped (the hub
        refuses), so nothing is sent for one."""
        if self.compacting and not self.busy:
            return
        self.queued.clear()
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

    # ── the scrollback ──────────────────────────────────────────────────
    def earlier(self) -> list[dict]:
        """The exchanges before the turn on show, oldest first."""
        return [x for x in self.exchanges if x["id"] != self.exchange]

    def want_older(self) -> bool:
        """Ask the hub for the page before the oldest exchange here (the
        newest page, with none yet). Whether a page is on its way."""
        if self.back_loading:
            return True
        if not self.more_back or not self.connected:
            return False
        before = self.exchanges[0]["id"] if self.exchanges else None
        message = {"type": "scrollback", "limit": SCROLLBACK_PAGE}
        if before is not None:
            message["before"] = before
        self.back_loading = self._send(message)
        return self.back_loading

    def _got_scrollback(self, ev: dict) -> None:
        """A page of the hub's scrollback: it replaces what this call had
        in the span it covers (from its oldest item up to `before`, or to
        the newest with none; from the start of what she holds when there
        is no more), so an exchange forgotten meanwhile goes too."""
        self.back_loading = False
        items = [x for x in map(_exchange, (i for i in ev.get("items") or []
                                            if isinstance(i, dict)))
                 if x is not None]
        before = ev.get("before")
        more = bool(ev.get("more"))
        low = items[0]["id"] if items and more else None
        if not items and more:
            return                          # nothing said: keep what is here
        kept = [x for x in self.exchanges
                if not ((low is None or x["id"] >= low)
                        and (before is None or x["id"] < before))]
        self.exchanges = sorted(kept + items, key=lambda x: x["id"])
        if low is None or low <= self.exchanges[0]["id"]:
            # A page that reaches the oldest here says what lies past it.
            self.more_back = more
            self.edge = None if more else str(ev.get("edge") or "start")
        self._trim()
        self.back_rev += 1

    def _trim(self) -> None:
        """Drop what she no longer holds word for word (older than
        `context.from`): the scrollback ends there, at her memory."""
        start = (self.context or {}).get("from")
        if start is None or not self.exchanges \
                or self.exchanges[0]["id"] >= start:
            return
        self.exchanges = [x for x in self.exchanges if x["id"] >= start]
        self.more_back, self.edge = False, "compacted"
        self.back_rev += 1

    def _keep_turn(self, xid: int, kind: str) -> None:
        """The turn on show ended and was kept as exchange `xid`: it joins
        the scrollback without asking (`done.exchange`)."""
        lines = list(self.lines)
        if kind == "hub":
            lines.insert(0, next((a.text for a in self.activity
                                  if a.kind == "note"), ""))
        item = {"id": xid, "at": self.at, "source": kind,
                "device": None if kind == "hub"
                else self.heard_on or self.device, "lines": lines,
                "reply": self.message, "attached": self.heard_attached,
                "shown": [self.shown[sid] for sid in self._seg_shown
                          if sid is not None and sid in self.shown]}
        self.exchanges = sorted([x for x in self.exchanges if x["id"] != xid]
                                + [item], key=lambda x: x["id"])
        self.exchange = xid
        self.back_rev += 1

    def _forget(self, ids) -> None:
        gone = {i for i in ids if isinstance(i, int)} \
            if isinstance(ids, list) else set()
        if not gone:
            return
        self.exchanges = [x for x in self.exchanges if x["id"] not in gone]
        self.back_rev += 1
        if self.exchange in gone and not self.busy:
            # The turn on show was forgotten: it leaves the screen too.
            self._clear_turn()
            self.heard, self.heard_on, self.heard_attached = "", None, 0
            self.lines = []
            self.exchange = self.at = None
            self.error = self.hint = None

    def job_of(self, _act: Activity):
        return None

    def loose_jobs(self) -> list:
        return []

    def said_parts(self) -> list[tuple[str, float, str]]:
        """As `Agent.said_parts`: the turn's words between its steps. A
        thing the agent showed is a part too, its key starting with "s"
        and its id in place of the words."""
        parts = []
        for i, (s, after, shown) in enumerate(zip(
                self._segments, self._seg_after, self._seg_shown)):
            if shown is not None:
                parts.append((f"s{i}_", float(after), shown))
            elif s.strip():
                parts.append((f"m{i}_", float(after), s.strip()))
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
        self.lines = [heard] if heard else []
        self.heard_on = None
        self.heard_attached = 0
        self._clear_turn()
        self.error = self.hint = None
        self._interrupted = False
        self.turn_started = time.monotonic()
        self.at = time.time()
        self.exchange = None
        self.from_hub = False
        # A line said while the hub compacts waits for it there.
        self.phase = COMPACTING if self.compacting else "thinking"

    def _clear_turn(self) -> None:
        self.message = self.thought = ""
        self.hangup_line = None
        self.activity.clear()
        self._segments, self._seg_after, self._seg_shown = [], [], []
        self._seg_map = {}
        self._turn = None

    def _new_segment(self, value: str) -> int:
        self._segments.append(value)
        self._seg_shown.append(None)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)
        return len(self._segments) - 1

    def _said_into(self, line: str, attached: int = 0,
                   device: str | None = None) -> None:
        """A line said into the running turn, here or on another screen
        (`device`): your plate shows it now, and her plate goes on with
        her reply. The turn keeps every line, for its exchange."""
        self.lines.append(line)
        self.heard = line
        self.heard_on = device if device and device != self.device else None
        self.heard_attached = attached

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
        self._seg_shown.append(sid)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)

    def _said(self) -> str:
        return "\n\n".join(s.strip() for s in self._segments if s.strip())

    def _show_view(self, view: dict) -> None:
        """Replace the turn on show with the hub's account of it. Its
        turn number and its reply block by block, so that a turn still
        running carries on where it is: later events join that turn, and
        a block still streaming grows on the end of its own text."""
        self._clear_turn()
        self._local_turn = self._turn_ours = False
        self._turn = view.get("turn")
        line = str(view.get("line") or "")
        # A turn the hub started: its line a note at the top, as `turn`
        # shows one, and kept as the hub's when it ends.
        self.from_hub = view.get("client") == HUB_CLIENT
        self.heard = "" if self.from_hub else line
        self.lines = [self.heard] if self.heard else []
        device = view.get("device")
        self.heard_on = str(device) if device and device != self.device \
            and not self.from_hub else None
        if self.from_hub:
            self.activity.append(Activity("note", line))
        xid, at = view.get("exchange"), view.get("at")
        self.exchange = xid if isinstance(xid, int) else None
        self.at = float(at) if isinstance(at, (int, float)) else None
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
            if welcome is None or welcome.get("type") != "welcome":
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
        self.compacting = False
        self.back_loading = False       # its answer won't come now
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
        phase = PHASES.get(st.get("phase"), st.get("phase")) or self.phase
        self.compacting = phase == COMPACTING
        if self._local_turn and (self.compacting or not st.get("busy")):
            # A line said here that the hub hasn't started on yet: keep
            # the turn the call already shows (waiting on a compaction,
            # it says so).
            if self.compacting:
                self.phase = phase
            elif self.phase == COMPACTING:
                self.phase = "thinking"     # it ended: the line goes in now
        else:
            # A compaction is the hub's busy, but no turn of the call's.
            self.busy = bool(st.get("busy")) and not self.compacting
            self.phase = phase
        self.model = st.get("model") or self.model
        self.effort = st.get("effort") or self.effort
        self._label = st.get("model_label") or self._label
        if "context" in st:
            self.context = _context(st.get("context"))
            self._trim()
        if not self._local_turn:
            self.error = st.get("error")
            self.hint = st.get("hint")

    def _on_welcome(self, ev: dict) -> None:
        self.connected = True
        self.device = str(ev.get("device") or "") or None
        persona.adopt(ev.get("agent"))
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
            said = list(zip(self.queued, self._queued_attach))
            self.queued.clear()
            self._queued_attach.clear()
            if not self.busy:
                self._begin_turn(said[0][0])
                self._local_turn = self._turn_ours = True
                self.heard_attached = len(said[0][1] or ())
            else:
                for line, attach in said:
                    self._said_into(line, len(attach or ()))
            for line, attach in said:
                self._say(line, attach)
        # The newest page of the scrollback, as the hub has it now: what
        # was forgotten or compacted away while the line was down goes.
        self.more_back = True
        self._ask_newest()
        if self.call_open:
            self._send({"type": "warm"})
        self._emit()

    def _ask_newest(self) -> None:
        """The newest page of the scrollback, whatever is here already."""
        self.back_loading = self._send({"type": "scrollback",
                                        "limit": SCROLLBACK_PAGE})

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
                # Another screen's, mid-turn: your plate shows it.
                self._said_into(text, int(ev.get("attached") or 0),
                                ev.get("device"))
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
                self.from_hub = True
                self.activity.append(Activity("note", line))
            elif self._local_turn and self.lines \
                    and line.strip() == self.lines[0].strip():
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
            final = str(ev.get("text") or "").strip() or self._said()
            if final:
                self.message = final
            if not ev.get("ok"):
                self.phase = "error"
                self.error = ev.get("error") or "the turn failed"
                self.hint = ev.get("hint")
            xid = ev.get("exchange")
            if isinstance(xid, int) and not isinstance(xid, bool):
                self._keep_turn(xid, "hub" if self.from_hub else "user")
            turn = ev.get("turn")
            self._unseen = turn
            if self.call_open or self._interrupted:
                # Seen as it happened, or stopped on purpose.
                self._seen(turn)
            self._turn_ours = False
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
        elif kind == "compacted":
            before, after = ev.get("before"), ev.get("after")
            self.compacted = {
                "before": before if isinstance(before, int) else None,
                "after": after if isinstance(after, int) else None,
                "at": time.monotonic()}
            start = ev.get("from")
            if isinstance(start, int):
                self.context = {**(self.context or {"used": None,
                                                    "compact_at": None,
                                                    "cap": None,
                                                    "window": None}),
                                "from": start}
                self._trim()
        elif kind == "scrollback":
            self._got_scrollback(ev)
        elif kind == "forgotten":
            self._forget(ev.get("ids"))
        elif kind == "error":
            request, reason = ev.get("request"), ev.get("reason")
            log.warning("%s: the hub refused %s: %s", self._tag, request,
                        reason)
            if request == "fetch":
                sid = str(ev.get("id") or "")
                if sid in self._fetching:
                    self._fetching.remove(sid)
                self._media_failed[sid] = str(reason or "not found")
                self._wake_media(sid, None)
                return                      # the picture's row says it
            if request == "scrollback":
                self.back_loading = False
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
