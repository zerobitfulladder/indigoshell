"""tuned-adm profile helpers (port of the old `tuned_profile.bash` script).

Maps friendly names to tuned profile names, switches via tuned-adm, and
notifies with the transition. The notify uses the friendly names since
the internal tuned names ("throughput-performance", "desktop") read as
jargon to anyone but the tuned maintainers.
"""

from ..services import proc

_PROFILES = {
    "performance": "throughput-performance",
    "balanced":    "desktop",
    "powersave":   "powersave",
}


def _active_friendly() -> str:
    """Return the friendly name of the currently-active tuned profile,
    falling back to the raw name if it isn't in our map."""
    raw = proc.run(["tuned-adm", "active"]).replace("Current active profile:", "").strip()
    for friendly, internal in _PROFILES.items():
        if internal == raw:
            return friendly
    return raw or "?"


def _set(friendly: str) -> None:
    previous = _active_friendly()
    proc.fire(["tuned-adm", "profile", _PROFILES[friendly]])
    proc.fire(["notify-send", "Power Profile", f"{previous} → {friendly}"])


def performance() -> None: _set("performance")
def balanced()    -> None: _set("balanced")
def powersave()   -> None: _set("powersave")
