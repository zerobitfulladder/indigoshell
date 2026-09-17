"""System menus: power, keyboard layout, tuned profile, GPU mode.

The current layout, profile and mode are read when the menu opens, so
the choice already in effect is lit.
"""

from ..plugin import Item, Menu, field

# tuned's own names read as jargon, so the chips carry friendly labels
# and the values stay real.
_PROFILES = (("PERFORMANCE", "throughput-performance"),
             ("BALANCED", "desktop"),
             ("POWERSAVE", "powersave"))
_LAYOUTS = (("US", "us"), ("TR", "tr"))
_GPU_MODES = (("NVIDIA", "nvidia"), ("INTEGRATED", "integrated"),
              ("HYBRID", "hybrid"))

_active_layout = field(["setxkbmap", "-query"], "layout")
_active_profile = field(["tuned-adm", "active"], "Current active profile")
_active_gpu = field(["optimus-manager", "--print-mode"], "Current mode")


async def _layout_items():
    current = await _active_layout()
    # caps:escape is re-applied on every switch because setxkbmap resets
    # options when the layout changes.
    return [Item(label, ["setxkbmap", value, "-option", "caps:escape"],
                 active=(current == value))
            for label, value in _LAYOUTS]


async def _profile_items():
    current = await _active_profile()
    return [Item(label, ["tuned-adm", "profile", value], active=(current == value))
            for label, value in _PROFILES]


async def _gpu_items():
    # optimus-manager talks to its root daemon over a socket, so no
    # pkexec is needed. With no display manager the switch is staged
    # until the next X restart.
    current = await _active_gpu()
    return [Item(label, ["optimus-manager", "--switch", value, "--no-confirm"],
                 active=(current == value))
            for label, value in _GPU_MODES]


MENUS = [
    Menu("power", "POWER", [
        Item("SUSPEND", ["systemctl", "suspend"]),
        Item("POWEROFF", ["systemctl", "poweroff"]),
        Item("REBOOT", ["systemctl", "reboot"]),
        # qtile's IPC shutdown — adjust if you swap WMs.
        Item("LOGOUT", ["qtile", "cmd-obj", "-o", "cmd", "-f", "shutdown"]),
    ]),
    Menu("layout", "LAYOUT", _layout_items),
    Menu("profile", "PROFILE", _profile_items),
    Menu("graphics", "GRAPHICS", _gpu_items),
]
