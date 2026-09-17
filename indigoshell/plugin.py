"""Plugin contract: menus.

A plugin module exports `MENUS`, a list of `Menu`. A menu is what a
keybinding opens — `indigoshell menu power` — a titled stack of chips, one
per `Item`, picked with the digit keys, the mouse, or Escape to go back.

Depth is free. An item's action may return another `Menu`, and the shell
shows that as the next stage in the same window: pick OUTPUT, get the
list of sinks, pick one, done. State between stages travels in closures;
the shell never needs to know what it is. A multi-stage flow is just "a
function that returns the next menu" — everything runs on one loop, so a
stage hands a Python list back rather than writing a JSON manifest for a
subprocess orchestrator to find.

What an action may be, and what the shell does with what it returns:

  argv list          run it (fire and forget) and close the menu
  callable           call it; await the result if it is awaitable, then:
    -> None          close the menu
    -> Menu          show it as the next stage
    -> argv list     run it and close
    raises           log it, flash the row red, stay open

`items` may be a list, or a callable (sync or async) that produces one
when the stage is about to show — for lists that only exist once a tool
has been asked, like outputs from xrandr. The shell resolves it before
the stage appears, so the wait for a slow tool lands on the keypress.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence, Union

from .services import proc

__all__ = ["Menu", "Item", "field"]

Action = Union[Sequence[str], Callable[[], Any], None]


@dataclass(frozen=True)
class Item:
    label: str
    run: Action = None
    # Lit as the current choice — the layout in use, the default sink.
    active: bool = False
    # Small muted text at the row's right edge.
    hint: str | None = None


Items = Sequence[Item]
ItemsSource = Union[Items, Callable[[], Union[Items, Awaitable[Items]]]]


@dataclass(frozen=True)
class Menu:
    name: str
    title: str
    items: ItemsSource = ()


# ── read helper ─────────────────────────────────────────────────────────
def field(cmd: Sequence[str], key: str, *, sep: str = ":"):
    """Async reader for `key` out of `key: value` output — setxkbmap
    -query, tuned-adm active, optimus-manager --print-mode. The usual way
    to find out which item is `active`."""
    async def read():
        out = await proc.run(list(cmd))
        for line in out.splitlines():
            if sep not in line:
                continue
            found, value = line.split(sep, 1)
            if found.strip().lower() == key.lower():
                return value.strip()
        return None
    return read
