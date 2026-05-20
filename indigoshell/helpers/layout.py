"""Keyboard-layout helpers (port of the old `modify_keys.bash` script).

Each helper calls setxkbmap with the caps:escape option so Caps Lock
keeps mapping to Escape across layout switches.
"""

from ..services import proc


def _set(layout: str) -> None:
    proc.fire(["setxkbmap", layout, "-option", "caps:escape"])


def us() -> None:
    _set("us")


def tr() -> None:
    _set("tr")
