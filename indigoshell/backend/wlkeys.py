"""The keyboard: wl_keyboard's keys as the shell's KeyEvents.

The compositor sends the keymap itself (wl_keyboard.keymap: an XKB
keymap in a file descriptor) and the modifier state separately
(wl_keyboard.modifiers); xkbcommon (python-xkbcommon, a distro package)
turns a key into its keysym and text under that state, every layout
group and level included: "us,tr" switched by the compositor is just
another group here.

`Keys` makes the KeyEvent: the keysym names the key, Shift/Control/Alt/Super are the modifiers in
effect, and `text` is what the key types, empty for any chord holding
Control, Alt or Super and with control characters taken out (xkb types
"\\r" for Return and "\\x01" for Ctrl+A; the shell's text fields take
neither).

A Wayland compositor does not repeat keys; the client does, at the rate
and after the delay wl_keyboard.repeat_info gives. `Repeat` is that
timer: the newest key held repeats until it comes up, another key goes
down or the keyboard leaves.
"""

import logging
import time
import unicodedata

from ..widgets.base import KeyEvent

log = logging.getLogger(__name__)

# wl_keyboard.key is the evdev code; XKB's keycodes are those plus 8.
EVDEV_OFFSET = 8
# The usual repeat (X's old default), for a compositor that never says.
DEFAULT_RATE, DEFAULT_DELAY_MS = 25, 600

# The real modifiers as XKB names them; Alt is Mod1 and Super Mod4.
# AltGr is Mod5 and is no modifier here: it
# picks a level, and the text it types counts.
_MODS = {"shift": "Shift", "ctrl": "Control", "alt": "Mod1", "super": "Mod4"}


def printable(text: str) -> str:
    """`text` without control characters (Return's "\\r", Tab's "\\t",
    Escape, BackSpace, Delete, Ctrl+A's "\\x01"): what a text field may
    insert."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


class Keys:
    """The keymap the compositor sent and the modifier state, through
    xkbcommon. Raises ImportError without python-xkbcommon."""

    def __init__(self) -> None:
        from xkbcommon import xkb
        self._xkb = xkb
        self._effective = xkb.StateComponent.XKB_STATE_MODS_EFFECTIVE
        self.context = xkb.Context()
        self.keymap = None
        self.state = None
        self._mods: dict[str, int | None] = {}

    @property
    def ready(self) -> bool:
        return self.state is not None

    def load(self, text: bytes) -> bool:
        """A keymap in XKB's text format (wl_keyboard.keymap's), NUL
        terminated or not. False, keeping the one before, when it does
        not compile."""
        try:
            keymap = self.context.keymap_new_from_buffer(text.rstrip(b"\0"))
        except Exception as e:
            log.warning("keyboard: the compositor's keymap does not compile (%s)", e)
            return False
        self._adopt(keymap)
        return True

    def load_names(self, layout: str = "us", variant: str | None = None,
                   options: str | None = None) -> None:
        """A keymap from RMLVO names (tests, and the virtual keyboard
        that types into a nested compositor)."""
        self._adopt(self.context.keymap_new_from_names(layout=layout, variant=variant,
                                                       options=options))

    def _adopt(self, keymap) -> None:
        self.keymap = keymap
        self.state = keymap.state_new()
        self._mods = {}
        for field, name in _MODS.items():
            try:
                index = keymap.mod_get_index(name)
            except Exception:
                index = None
            self._mods[field] = index if isinstance(index, int) and index < 32 else None

    def mask(self, *fields: str) -> int:
        """The modifier mask of `fields` ("shift", "ctrl", ...) in this
        keymap, as wl_keyboard.modifiers carries it."""
        out = 0
        for f in fields:
            index = self._mods.get(f)
            if index is not None:
                out |= 1 << index
        return out

    def modifiers(self, depressed: int, latched: int, locked: int, group: int) -> None:
        """wl_keyboard.modifiers: the state every key after it is read
        under (the layout group too)."""
        if self.state is not None:
            self.state.update_mask(depressed, latched, locked, 0, 0, group)

    def _active(self, field: str) -> bool:
        index = self._mods.get(field)
        if index is None or self.state is None:
            return False
        try:
            return bool(self.state.mod_index_is_active(index, self._effective))
        except Exception:
            return False

    def event(self, key: int) -> KeyEvent | None:
        """The KeyEvent for evdev code `key` under the current state, or
        None with no keymap yet."""
        if self.state is None:
            return None
        code = key + EVDEV_OFFSET
        keysym = self.state.key_get_one_sym(code)
        shift, ctrl, alt, sup = (self._active(f) for f in ("shift", "ctrl", "alt", "super"))
        text = ""
        if not (ctrl or alt or sup):
            try:
                text = printable(self.state.key_get_string(code) or "")
            except Exception:
                text = ""
        return KeyEvent(keysym=int(keysym), text=text, shift=shift, ctrl=ctrl, alt=alt,
                        super=sup)

    def repeats(self, key: int) -> bool:
        if self.keymap is None:
            return False
        try:
            return bool(self.keymap.key_repeats(key + EVDEV_OFFSET))
        except Exception:
            return False

    def code_for(self, char: str) -> tuple[int, bool] | None:
        """The evdev code that types `char` in the first layout, and
        whether it takes Shift: for a virtual keyboard typing text
        (tests)."""
        km = self.keymap
        if km is None:
            return None
        for code in range(km.min_keycode(), km.max_keycode() + 1):
            for level in (0, 1):
                try:
                    syms = km.key_get_syms_by_level(code, 0, level)
                except Exception:
                    continue
                for sym in syms or ():
                    if self._xkb.keysym_to_string(sym) == char:
                        return code - EVDEV_OFFSET, level == 1
        return None


class Repeat:
    """The client's own key repeat (wl_keyboard.repeat_info): after
    `delay` ms the newest key held fires again `rate` times a second
    until it comes up. `fire(key)` is called with the evdev code; the
    key is read again under the state then, so a modifier changed while
    it repeats counts."""

    def __init__(self, loop, fire) -> None:
        self.loop = loop
        self.fire = fire
        self.rate = DEFAULT_RATE
        self.delay_ms = DEFAULT_DELAY_MS
        self.key: int | None = None
        self._handle = None
        self._next = 0.0

    def info(self, rate: int, delay_ms: int) -> None:
        self.rate, self.delay_ms = max(0, int(rate)), max(0, int(delay_ms))
        if not self.rate:
            self.stop()

    def press(self, key: int, repeats: bool = True) -> None:
        """`key` went down: it repeats if it may, and whatever repeated
        before stops."""
        self.stop()
        if not repeats or not self.rate or self.loop is None:
            return
        self.key = key
        self._next = time.monotonic() + self.delay_ms / 1000.0
        self._handle = self.loop.call_at(self._loop_time(self._next), self._tick)

    def release(self, key: int) -> None:
        if key == self.key:
            self.stop()

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self.key = None

    def _loop_time(self, mono: float) -> float:
        return self.loop.time() + (mono - time.monotonic())

    def _tick(self) -> None:
        self._handle = None
        key = self.key
        if key is None:
            return
        try:
            self.fire(key)
        except Exception:
            log.exception("keyboard: a repeated key failed")
        if self.key != key:
            return                      # the handler stopped it (the window closed)
        now = time.monotonic()
        self._next += 1.0 / self.rate
        if self._next < now:
            self._next = now + 1.0 / self.rate      # behind: no burst to catch up
        self._handle = self.loop.call_at(self._loop_time(self._next), self._tick)
