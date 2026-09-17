"""Display menu: the connected outputs; pick one to use it alone.

The chosen output becomes the only active one and the primary, at its
largest resolution and the fastest refresh rate that resolution offers,
both taken from xrandr's mode table. Every other output goes off in the
same xrandr call — one call, not one per output, or there is a frame
with no output enabled and X collapses the screen to 640x480. The output
already in sole use is lit.

The pick is remembered (`display.output` in the state file) and
`restore()` enforces it at shell start and whenever RandR reports a
change: if the remembered output is connected it is used; if it is not,
the laptop panel takes over without forgetting the choice, so plugging
the monitor back in returns to it. With nothing remembered the screen
is left alone — unless every active output has gone, which is the
unplugged-and-dark case, and then the panel comes on. A restore that
would change nothing is skipped, so neither a `reload` nor the burst of
events its own xrandr call raises ever re-modesets the screen.
"""

import logging
import re

from ..core import state
from ..plugin import Item, Menu
from ..services import proc

log = logging.getLogger(__name__)

STATE_KEY = "display.output"
_EMBEDDED_PREFIXES = ("eDP", "LVDS")

_HEADER = re.compile(r"^(\S+) (connected|disconnected)( primary)?")
_GEOMETRY = re.compile(r"\d+x\d+\+\d+\+\d+")
_MODE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")
_RATE = re.compile(r"(\d+(?:\.\d+)?)([*+]*)")


async def _query():
    """Outputs from `xrandr --query`, in order.

    Each is a dict: name, connected, primary, active (has a current
    geometry), modes as (width, height, rate) triples, and current —
    the mode xrandr marks with `*`, or None.
    """
    out = await proc.run(["xrandr", "--query"])
    outputs: list[dict] = []
    for line in out.splitlines():
        m = _HEADER.match(line)
        if m:
            outputs.append({
                "name": m.group(1),
                "connected": m.group(2) == "connected",
                "primary": bool(m.group(3)),
                "active": bool(_GEOMETRY.search(line)),
                "modes": [],
                "current": None,
            })
            continue
        mm = _MODE.match(line)
        if mm and outputs:
            w, h = int(mm.group(1)), int(mm.group(2))
            for rate, flags in _RATE.findall(mm.group(3)):
                mode = (w, h, float(rate))
                outputs[-1]["modes"].append(mode)
                if "*" in flags:
                    outputs[-1]["current"] = mode
    return outputs


def _best_mode(modes):
    """Largest resolution first, then the fastest rate at it."""
    if not modes:
        return None
    return max(modes, key=lambda m: (m[0] * m[1], m[0], m[2]))


def _is_sole(outputs, name: str) -> bool:
    """True when `name` is the only active output, is primary, and runs
    its best mode — nothing a switch to it would change."""
    target = next((o for o in outputs if o["name"] == name), None)
    if target is None or not target["connected"] or not target["primary"]:
        return False
    if {o["name"] for o in outputs if o["active"]} != {name}:
        return False
    best = _best_mode(target["modes"])
    return best is None or target["current"] == best


async def _use_only(name: str, *, remember: bool = True,
                    outputs=None) -> None:
    outputs = outputs if outputs is not None else await _query()
    target = next((o for o in outputs if o["name"] == name), None)
    if target is None or not target["connected"]:
        raise RuntimeError(f"{name} is not connected")
    argv = ["xrandr", "--output", name, "--primary"]
    best = _best_mode(target["modes"])
    if best is not None:
        w, h, rate = best
        argv += ["--mode", f"{w}x{h}", "--rate", f"{rate:g}"]
    else:
        argv += ["--auto"]
    for other in outputs:
        # Off for every other connected output, and for a disconnected
        # one still holding a geometry — unplugged but never released.
        if other["name"] != name and (other["connected"] or other["active"]):
            argv += ["--output", other["name"], "--off"]
    await proc.run(argv, timeout=15.0)
    if remember:
        state.set(STATE_KEY, name)


async def _items() -> list[Item]:
    outputs = await _query()
    connected = [o for o in outputs if o["connected"]]
    if not connected:
        return [Item("NO OUTPUTS")]
    return [Item(o["name"], lambda name=o["name"]: _use_only(name),
                 active=_is_sole(outputs, o["name"]))
            for o in connected]


def _panel(outputs):
    return next((o["name"] for o in outputs if o["connected"]
                 and o["name"].startswith(_EMBEDDED_PREFIXES)), None)


async def restore() -> None:
    """Startup and screen-change hook: enforce the remembered output,
    or fall back to the laptop panel while it is unplugged."""
    wanted = state.get(STATE_KEY)
    outputs = await _query()
    connected = {o["name"] for o in outputs if o["connected"]}
    if wanted and wanted in connected:
        target, remember = wanted, True
    elif wanted or not any(o["active"] and o["connected"] for o in outputs):
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
