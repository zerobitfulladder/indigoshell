"""What a tool lent to the hub's agent is: a `Hand`, the `Call` it gets,
the `HandError` it raises, and the content blocks it answers with.

The hub's protocol (lilac's PROTOCOL.md, "Hands") is the reference: a
device lists its hands in its `hello`, the hub gives them to the agent as
`<device>_<name>`, and a use of one comes to the device as a `call` that
it answers with a `result`. agent/hands.py serves the calls (`Hands`)
and holds what the laptop lends; flux/hands.py makes Flux's hands of
these same types.

Pure (standard library only), so that Flux, which runs where none of the
shell's own dependencies are installed, can import it.
"""

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# How long a call may run when the hub doesn't say.
DEFAULT_TIMEOUT_S = 30.0
# The longest a hand may ask the hub to wait for its calls.
MAX_TIMEOUT_S = 1800
# Answer this much before the hub's own deadline, so the answer is there
# before it gives up.
TIMEOUT_MARGIN_S = 0.5


DECLINED = "the user declined"


class HandError(Exception):
    """A tool that could not do its job: the message goes to the agent."""


# Asks the user on this machine's screen: (title, detail, seconds to
# wait) → whether they allowed it.
Asker = Callable[[str, str, float], Awaitable[bool]]


@dataclass
class Call:
    """One use of a hand by the agent, as the hand sees it."""

    id: str
    tool: str
    args: dict
    timeout: float                  # what the hub gives the whole call
    asker: Asker | None = None
    started: float = field(default_factory=time.monotonic)

    def left(self) -> float:
        """Seconds left to answer in, before the hub gives up."""
        return (self.timeout - TIMEOUT_MARGIN_S
                - (time.monotonic() - self.started))

    async def ask(self, title: str, detail: str) -> bool:
        """The user's OK, asked on this machine's screen; no screen, or
        no answer in the time left, is a no."""
        if self.asker is None:
            return False
        return await self.asker(title, detail, max(0.0, self.left() - 1.0))


def text_block(value: str) -> dict:
    return {"type": "text", "text": value}


def no_input() -> dict:
    return {"type": "object", "properties": {}, "required": []}


@dataclass(frozen=True)
class Hand:
    """One tool lent to the agent. `timeout`: how long the hub waits for
    its calls, in seconds (up to MAX_TIMEOUT_S); None leaves it to the
    hub (a minute)."""

    name: str                   # lowercase letters, digits and _
    description: str
    run: Callable[[Call], Awaitable[list[dict]]]
    input: dict = field(default_factory=no_input)
    timeout: float | None = None

    def lent(self) -> dict:
        """As `hello.hands` lists it."""
        lent = {"name": self.name, "description": self.description,
                "input": self.input}
        if self.timeout is not None:
            lent["timeout"] = int(max(1, min(self.timeout, MAX_TIMEOUT_S)))
        return lent
