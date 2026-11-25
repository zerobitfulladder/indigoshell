#!/usr/bin/env python3
"""Synthetic input *inside* a Wayland compositor, for testing overlay.py.

Uses the compositor's own virtual-input protocols (zwlr_virtual_pointer_v1,
zwp_virtual_keyboard_v1), so the events exist only in that compositor —
meant for a nested niri, where nothing reaches the host session.

    poke.py click X Y     move the pointer to X,Y (output px) and left-click
    poke.py move X Y
    poke.py key esc|enter|up|down
"""

import os
import subprocess
import sys
import time

from pywayland.client import Display

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocols  # noqa: E402

wl = protocols.load()
BTN_LEFT = 0x110
KEYS = {"esc": 1, "enter": 28, "up": 103, "down": 108}


def main() -> None:
    cmd = sys.argv[1]
    display = Display(os.environ.get("WAYLAND_DISPLAY"))
    display.connect()
    registry = display.get_registry()
    found: dict[str, tuple[int, int]] = {}
    registry.dispatcher["global"] = lambda _r, n, i, v: found.__setitem__(i, (n, v))
    display.roundtrip()
    seat = registry.bind(found["wl_seat"][0], wl.wayland.WlSeat, 1)
    output = registry.bind(found["wl_output"][0], wl.wayland.WlOutput, 1)
    size = [0, 0]

    def mode(_o, flags, w, h, _refresh):
        if flags & 1:                           # WL_OUTPUT_MODE_CURRENT
            size[:] = [w, h]

    output.dispatcher["mode"] = mode
    output.dispatcher["geometry"] = lambda *a: None
    display.roundtrip()
    ms = lambda: int(time.monotonic() * 1000) & 0xFFFFFFFF  # noqa: E731

    if cmd in ("click", "move"):
        mgr = registry.bind(found["zwlr_virtual_pointer_manager_v1"][0],
                            wl.wlr_virtual_pointer_unstable_v1.ZwlrVirtualPointerManagerV1, 1)
        vp = mgr.create_virtual_pointer(seat)
        x, y = int(sys.argv[2]), int(sys.argv[3])
        vp.motion_absolute(ms(), x, y, size[0], size[1])
        vp.frame()
        display.roundtrip()
        if cmd == "click":
            time.sleep(0.05)
            vp.button(ms(), BTN_LEFT, 1)
            vp.frame()
            display.roundtrip()
            time.sleep(0.05)
            vp.button(ms(), BTN_LEFT, 0)
            vp.frame()
        display.roundtrip()
        vp.destroy()
    elif cmd == "key":
        vk_mod = wl.virtual_keyboard_unstable_v1
        mgr = registry.bind(found["zwp_virtual_keyboard_manager_v1"][0],
                            vk_mod.ZwpVirtualKeyboardManagerV1, 1)
        kb = mgr.create_virtual_keyboard(seat)
        keymap = subprocess.run(["xkbcli", "compile-keymap", "--layout", "us"],
                                capture_output=True, check=True).stdout + b"\0"
        fd = os.memfd_create("keymap", os.MFD_CLOEXEC)
        os.write(fd, keymap)
        kb.keymap(1, fd, len(keymap))           # WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1
        os.close(fd)
        display.roundtrip()
        key = KEYS[sys.argv[2]]
        kb.key(ms(), key, 1)
        display.roundtrip()
        time.sleep(0.03)
        kb.key(ms(), key, 0)
        display.roundtrip()
        kb.destroy()
    else:
        raise SystemExit(__doc__)
    display.roundtrip()
    display.disconnect()


if __name__ == "__main__":
    main()
