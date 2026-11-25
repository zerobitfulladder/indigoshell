"""The agent's name and colour, as the shell and its apps draw her.

The hub's agent is Lilac, and the hub names her once for every screen in
its `welcome` (`agent: {name, color}`, lilac's PROTOCOL.md): every reply,
notification and mark is drawn in that name and colour. Until a call to
the hub has heard it, and in an app with no shell to ask (Flux on its
own, unattended on the hub), she is NAME in COLOR, the hub's own.

Pure Python, so that Flux imports it too. In the shell the call adopts
what the hub's welcome says (agent/remote.py); the shell tells its apps
when they announce a window (applink's `attached`), and Flux adopts that
in turn.
"""

import re

NAME = "Lilac"
COLOR = "#c8a2c8"

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_NAME_MAX = 32

_name = NAME
_color = COLOR


def name() -> str:
    """Her name: "Lilac"."""
    return _name


def color() -> str:
    """Her colour: "#c8a2c8"."""
    return _color


def look() -> dict:
    """{name, color}, as `welcome.agent` and applink's `attached` carry them."""
    return {"name": _name, "color": _color}


def clean(agent) -> dict | None:
    """`welcome.agent` (or `attached.agent`) as {name, color}, each field
    checked: a plain name of up to 32 characters, a #rrggbb colour. None
    when neither is usable; a field that isn't keeps the current one."""
    if not isinstance(agent, dict):
        return None
    value = agent.get("name")
    good_name = (value.strip() if isinstance(value, str) and value.strip()
                 and len(value.strip()) <= _NAME_MAX
                 and value.strip().isprintable() else None)
    value = agent.get("color")
    good_color = value if isinstance(value, str) and _COLOR.fullmatch(value) \
        else None
    if good_name is None and good_color is None:
        return None
    return {"name": good_name or _name, "color": (good_color or _color).lower()}


def adopt(agent) -> bool:
    """Take her name and colour from `welcome.agent` (or applink's
    `attached.agent`). Whether anything changed."""
    global _name, _color
    got = clean(agent)
    if got is None or (got["name"], got["color"]) == (_name, _color):
        return False
    _name, _color = got["name"], got["color"]
    return True
