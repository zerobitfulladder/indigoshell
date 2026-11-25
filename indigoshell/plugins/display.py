"""Display menu: the connected outputs; pick one to use it alone.

The chosen output becomes the only active one and the primary (on X; niri
has no primary), at its largest resolution and the fastest refresh rate
that resolution offers, both taken from the output's mode table. Every
other output goes off with it — on X in the same xrandr call, one call
and not one per output, or there is a frame with no output enabled and X
collapses the screen to 640x480 (indigoshell/wm says how). The output
already in sole use is lit.

The pick is remembered in the state file, one per window manager:
`display.output` on X and `display.output.niri` on niri, as the two name
the same connector differently (X's HDMI-0 is niri's HDMI-A-1), so X's
pick never reads as a monitor niri doesn't have. `restore()` enforces it at shell start and whenever RandR reports a
change: if the remembered output is connected it is used; if it is not,
the laptop panel takes over without forgetting the choice, so plugging
the monitor back in returns to it. With nothing remembered the screen
is left alone — unless every active output has gone, which is the
unplugged-and-dark case, and then the panel comes on. A restore that
would change nothing is skipped, so neither a `reload` nor the burst of
events its own xrandr call raises ever re-modesets the screen. On niri
the choice is a temporary output change, which niri drops when its
config's outputs are reloaded; the next shell start enforces it again.

While braindance (the VITURE glasses session) runs, it owns the outputs:
it lights the glasses alone and switches their modes itself. It marks
its whole session with a RandR monitor named `braindance`, and restore()
leaves the screen alone while that monitor exists (X only: niri has no
RandR monitors).
"""

import logging

from .. import wm
from ..core import state
from ..plugin import Item, Menu

log = logging.getLogger(__name__)

STATE_KEY = "display.output"        # X's; another window manager's ends in its name
_EMBEDDED_PREFIXES = ("eDP", "LVDS")
_BRAINDANCE_MONITOR = "braindance"


def _state_key() -> str:
    """Where this window manager's pick is remembered (the docstring)."""
    name = wm.get().name
    return STATE_KEY if name == "x11" else f"{STATE_KEY}.{name}"


async def _query() -> list[wm.Output]:
    """The outputs, in order: name, connected, primary, active (lit,
    with a place on the screen), modes as (width, height, rate), and
    current — the mode in use, or None."""
    return await wm.get().outputs()


def _best_mode(modes):
    """Largest resolution first, then the fastest rate at it."""
    if not modes:
        return None
    return max(modes, key=lambda m: (m[0] * m[1], m[0], m[2]))


def _is_sole(outputs, name: str) -> bool:
    """True when `name` is the only active output, is primary (where
    there is one), and runs its best mode — nothing a switch to it would
    change."""
    target = next((o for o in outputs if o.name == name), None)
    if target is None or not target.connected:
        return False
    if wm.get().has_primary and not target.primary:
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
        state.set(_state_key(), name)


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


async def _braindance_running() -> bool:
    return _BRAINDANCE_MONITOR in await wm.get().virtual_monitors()


async def restore() -> None:
    """Startup and screen-change hook: enforce the remembered output,
    or fall back to the laptop panel while it is unplugged."""
    if await _braindance_running():
        return
    wanted = state.get(_state_key())
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
