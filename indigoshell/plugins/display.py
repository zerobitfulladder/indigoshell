"""Display menu: the connected outputs; pick one to use it alone.

The chosen output becomes the only one lit, at its largest resolution
and the fastest refresh rate that resolution offers, both taken from the
output's mode table; every other output goes off with it (indigoshell/wm
says how). The output already in sole use is lit in the menu.

The pick is remembered in the state file (`display.output`). `restore()`
enforces it at shell start and whenever the outputs change: if the
remembered output is connected it is used; if it is not, the laptop
panel takes over without forgetting the choice, so plugging the monitor
back in returns to it. With nothing remembered the screen is left alone
— unless every active output has gone, which is the unplugged-and-dark
case, and then the panel comes on. A restore that would change nothing
is skipped, so neither a `reload` nor the burst of changes its own
switch raises ever re-modesets the screen. The choice is a temporary
output change, which niri drops when its config's outputs are reloaded;
the next shell start enforces it again.
"""

import logging

from .. import wm
from ..core import state
from ..plugin import Item, Menu

log = logging.getLogger(__name__)

STATE_KEY = "display.output"
_EMBEDDED_PREFIXES = ("eDP", "LVDS")


async def _query() -> list[wm.Output]:
    """The outputs, in order: name, connected, active (lit, with a place
    on the screen), modes as (width, height, rate), and current — the
    mode in use, or None."""
    return await wm.get().outputs()


def _best_mode(modes):
    """Largest resolution first, then the fastest rate at it."""
    if not modes:
        return None
    return max(modes, key=lambda m: (m[0] * m[1], m[0], m[2]))


def _is_sole(outputs, name: str) -> bool:
    """True when `name` is the only active output and runs its best
    mode — nothing a switch to it would change."""
    target = next((o for o in outputs if o.name == name), None)
    if target is None or not target.connected:
        return False
    if {o.name for o in outputs if o.active} != {name}:
        return False
    best = _best_mode(target.modes)
    return best is None or target.current == best


async def _use_only(name: str, *, remember: bool = True,
                    outputs=None) -> None:
    outputs = outputs if outputs is not None else await _query()
    target = next((o for o in outputs if o.name == name), None)
    if target is None or not target.connected:
        raise RuntimeError(f"{name} is not connected")
    # Off for every other connected output, and for a disconnected one
    # still holding a geometry — unplugged but never released.
    off = [o.name for o in outputs
           if o.name != name and (o.connected or o.active)]
    await wm.get().use_only(name, _best_mode(target.modes), off)
    if remember:
        state.set(STATE_KEY, name)


async def _items() -> list[Item]:
    outputs = await _query()
    connected = [o for o in outputs if o.connected]
    if not connected:
        return [Item("NO OUTPUTS")]
    return [Item(o.name, lambda name=o.name: _use_only(name),
                 active=_is_sole(outputs, o.name))
            for o in connected]


def _panel(outputs):
    return next((o.name for o in outputs if o.connected
                 and o.name.startswith(_EMBEDDED_PREFIXES)), None)


async def restore() -> None:
    """Startup and screen-change hook: enforce the remembered output,
    or fall back to the laptop panel while it is unplugged."""
    wanted = state.get(STATE_KEY)
    outputs = await _query()
    connected = {o.name for o in outputs if o.connected}
    if wanted and wanted in connected:
        target, remember = wanted, True
    elif wanted or not any(o.active and o.connected for o in outputs):
        # The remembered output is unplugged — or nothing was ever
        # remembered but the output in use just went away, which would
        # leave the screen dark. Either way: the panel, for now.
        target, remember = _panel(outputs), False
        if target is None:
            log.warning("display: no connected output to fall back to; "
                        "leaving the screen as it is")
            return
        log.info("display: %s; using %s meanwhile",
                 f"{wanted} not connected" if wanted else "active output gone",
                 target)
    else:
        return
    if _is_sole(outputs, target):
        return
    await _use_only(target, remember=remember, outputs=outputs)


MENUS = [
    Menu("display", "DISPLAY", _items),
]
STARTUP = [restore]
SCREEN = [restore]
