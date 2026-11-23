"""This machine's hands: the tools it lends the hub's agent.

A device connected to the hub (remote.py) can lend its agent tools. The
connection lists them in its `hello`; the hub gives them to the agent as
`<device>_<name>` (`laptop_about`), and when the agent uses one it sends
the connection a `call` and waits for the `result`. lilac's PROTOCOL.md
("Hands") is the reference.

`Hands` holds the tools and serves the calls: each call runs as its own
task and answers within the call's `timeout`; one the hub gives up on
(`cancel`), or that is still running when the line goes down, is
dropped without an answer. A tool is a `Hand`: its name, what it does
(the agent reads that to decide when to use it), the JSON Schema of its
arguments, and the coroutine that runs it, which returns the result's
content blocks or raises `HandError` with a message for the agent.

For now the laptop lends one tool, `about`. The desktop's own tools
(tools.py) are meant to come here as more `Hand`s; what needs the user's
OK before it runs is then this machine's to decide, and to ask on its
own screen ("the user declined" is the answer when they say no).
"""

import asyncio
import glob
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

# How long a call may run when the hub doesn't say.
DEFAULT_TIMEOUT_S = 30.0
# Answer this much before the hub's own deadline, so the answer is there
# before it gives up.
TIMEOUT_MARGIN_S = 0.5


class HandError(Exception):
    """A tool that could not do its job: the message goes to the agent."""


def text_block(value: str) -> dict:
    return {"type": "text", "text": value}


def no_input() -> dict:
    return {"type": "object", "properties": {}, "required": []}


@dataclass(frozen=True)
class Hand:
    """One tool lent to the agent."""

    name: str                   # lowercase letters, digits and _
    description: str
    run: Callable[[dict], Awaitable[list[dict]]]
    input: dict = field(default_factory=no_input)

    def lent(self) -> dict:
        """As `hello.hands` lists it."""
        return {"name": self.name, "description": self.description,
                "input": self.input}


class Hands:
    """The tools a connection lends, and the calls to them in flight."""

    def __init__(self, hands: Iterable[Hand] = ()) -> None:
        self._hands = {h.name: h for h in hands}
        self._calls: dict[str, asyncio.Task] = {}

    def lent(self) -> list[dict]:
        return [h.lent() for h in self._hands.values()]

    def names(self) -> list[str]:
        return list(self._hands)

    def serve(self, call: dict, reply: Callable[[dict], object]) -> None:
        """Run the hub's `call` in a task of its own; `reply` sends the
        `result` back on the line the call came in on."""
        call_id = str(call.get("id") or "")
        tool = str(call.get("tool") or "")
        args = call.get("args")
        try:
            timeout = float(call.get("timeout") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        if call_id in self._calls:
            log.warning("call %s is already running; the hub sent it twice",
                        call_id)
            return
        task = asyncio.get_running_loop().create_task(self._run(
            call_id, tool, args if isinstance(args, dict) else {}, timeout,
            reply))
        self._calls[call_id] = task
        task.add_done_callback(lambda _t: self._calls.pop(call_id, None))

    def cancel(self, call_id: str) -> None:
        """The hub gave up on a call: stop it, and answer nothing."""
        task = self._calls.pop(str(call_id), None)
        if task is not None:
            log.info("call %s cancelled by the hub", call_id)
            task.cancel()

    def drop(self) -> None:
        """The line went down: the hub fails these calls by itself."""
        for task in self._calls.values():
            task.cancel()
        self._calls.clear()

    async def _run(self, call_id: str, tool: str, args: dict, timeout: float,
                   reply: Callable[[dict], object]) -> None:
        def answer(ok: bool, content: list[dict] | None = None,
                   error: str | None = None) -> None:
            reply({"type": "result", "call": call_id, "ok": ok,
                   "content": content or [], "error": error})

        hand = self._hands.get(tool)
        if hand is None:
            answer(False, error=f"this device lends no tool named {tool!r}")
            return
        log.info("call %s: %s", call_id, tool)
        try:
            content = await asyncio.wait_for(
                hand.run(args), max(0.1, timeout - TIMEOUT_MARGIN_S))
        except asyncio.TimeoutError:
            answer(False, error=f"{tool} took longer than {timeout:g} s")
        except HandError as e:
            answer(False, error=str(e) or f"{tool} failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("call %s: %s failed", call_id, tool)
            answer(False, error=f"{tool} failed: {type(e).__name__}: {e}")
        else:
            answer(True, content)


# ── about: the laptop at a glance ───────────────────────────────────────
def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _span(seconds: float) -> str:
    """3 d 4 h, 4 h 12 min, 12 min."""
    m = int(seconds // 60)
    d, h, m = m // 1440, m // 60 % 24, m % 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def _uptime() -> str:
    raw = _read("/proc/uptime").split()
    return f"up {_span(float(raw[0]))}" if raw else ""


def _load() -> str:
    raw = _read("/proc/loadavg").split()
    if len(raw) < 3:
        return ""
    cpus = os.cpu_count()
    return (f"load {raw[0]}, {raw[1]}, {raw[2]} (1, 5, 15 min"
            + (f"; {cpus} CPUs)" if cpus else ")"))


def _memory() -> str:
    info: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        value = rest.split()
        if value and value[0].isdigit():
            info[key] = int(value[0])           # kB
    total, free = info.get("MemTotal"), info.get("MemAvailable")
    if not total or free is None:
        return ""
    gib = 1024 * 1024
    used = total - free
    out = (f"memory {used / gib:.1f} of {total / gib:.1f} GiB used "
           f"({100 * used / total:.0f}%)")
    swap, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    if swap:
        out += f"; swap {(swap - swap_free) / gib:.1f} of {swap / gib:.1f} GiB"
    return out


def _power() -> list[str]:
    """The batteries (not a mouse's or a headset's) and the charger."""
    lines, mains = [], None
    for dev in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = _read(f"{dev}/type")
        if kind == "Mains":
            mains = mains or _read(f"{dev}/online") == "1"
            continue
        if kind != "Battery" or _read(f"{dev}/scope") == "Device":
            continue
        capacity = _read(f"{dev}/capacity")
        status = _read(f"{dev}/status").lower() or "unknown"
        line = f"battery {capacity}%, {status}" if capacity \
            else f"battery {status}"
        left = _time_left(dev, status)
        if left:
            line += f", {left}"
        lines.append(line)
    if not lines:
        lines.append("no battery")
    if mains is not None:
        lines.append("on mains power" if mains else "on battery power")
    return lines


def _time_left(dev: str, status: str) -> str:
    """How long until empty (or full), from the battery's own rate."""
    for now, full, rate in (("energy_now", "energy_full", "power_now"),
                            ("charge_now", "charge_full", "current_now")):
        try:
            n = int(_read(f"{dev}/{now}"))
            f = int(_read(f"{dev}/{full}"))
            r = abs(int(_read(f"{dev}/{rate}")))
        except ValueError:
            continue
        if not r:
            return ""
        if status == "discharging":
            return f"about {_span(n / r * 3600)} left"
        if status == "charging" and f > n:
            return f"full in about {_span((f - n) / r * 3600)}"
        return ""
    return ""


def about_text() -> str:
    return "\n".join(line for line in (_uptime(), _load(), _memory(),
                                       *_power()) if line)


async def _about(_args: dict) -> list[dict]:
    return [text_block(about_text())]


ABOUT = Hand(
    name="about",
    description="This laptop: uptime, load, memory, battery.",
    run=_about)


def laptop_hands() -> Hands:
    """What the laptop lends the hub's agent."""
    return Hands([ABOUT])
