"""Synthetic keyboard and mouse over XTEST.

The take is performed with these: events enter the X server exactly as a
physical device's would, so qtile's bindings, the shell's grabs and
hover-to-focus all react as they would to a person. No xdotool — xcffib
already speaks XTEST.

Everything pressed is tracked and released on `close()`, so an aborted
take never leaves Super held down.
"""

import math
import random
import time

import xcffib
import xcffib.xtest

KEY_PRESS, KEY_RELEASE, BUTTON_PRESS, BUTTON_RELEASE, MOTION = 2, 3, 4, 5, 6

SUPER, SHIFT, CTRL = 0xFFEB, 0xFFE1, 0xFFE3
RETURN, ESCAPE, TAB, BACKSPACE = 0xFF0D, 0xFF1B, 0xFF09, 0xFF08
LEFT_BUTTON, RIGHT_BUTTON, WHEEL_UP, WHEEL_DOWN = 1, 3, 4, 5

MOVE_HZ = 144


def _ease(t: float) -> float:
    """easeInOutCubic: a hand starts slow, travels, settles."""
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


class Synth:
    def __init__(self, seed: int = 2077) -> None:
        self.conn = xcffib.connect()
        self.xtest = self.conn(xcffib.xtest.key)
        setup = self.conn.get_setup()
        self.root = setup.roots[0].root
        self.rng = random.Random(seed)      # same take, same jitter
        self._held_keys: list[int] = []
        self._held_buttons: list[int] = []
        self._codes = self._keymap(setup)
        self.x, self.y = self.pointer()

    # ── keymap ──────────────────────────────────────────────────────────
    def _keymap(self, setup) -> dict[int, tuple[int, bool]]:
        """keysym -> (keycode, needs Shift), lowest keycode winning."""
        lo, hi = setup.min_keycode, setup.max_keycode
        reply = self.conn.core.GetKeyboardMapping(lo, hi - lo + 1).reply()
        per, syms = reply.keysyms_per_keycode, list(reply.keysyms)
        codes: dict[int, tuple[int, bool]] = {}
        for i in range(hi - lo + 1):
            row = syms[i * per:(i + 1) * per]
            for level, sym in enumerate(row[:2]):
                if sym and sym not in codes:
                    codes[sym] = (lo + i, level == 1)
        return codes

    def _code(self, keysym: int) -> tuple[int, bool]:
        hit = self._codes.get(keysym)
        if hit is None:
            raise KeyError(f"no key produces keysym 0x{keysym:x} on this layout")
        return hit

    # ── raw events ──────────────────────────────────────────────────────
    def _fake(self, kind: int, detail: int, x: int = 0, y: int = 0) -> None:
        root = self.root if kind == MOTION else 0
        self.xtest.FakeInput(kind, detail, 0, root, x, y, 0)
        self.conn.flush()

    def pointer(self) -> tuple[int, int]:
        q = self.conn.core.QueryPointer(self.root).reply()
        return q.root_x, q.root_y

    # ── keyboard ────────────────────────────────────────────────────────
    def key_down(self, keysym: int) -> None:
        code, _ = self._code(keysym)
        self._fake(KEY_PRESS, code)
        self._held_keys.append(code)

    def key_up(self, keysym: int) -> None:
        code, _ = self._code(keysym)
        self._fake(KEY_RELEASE, code)
        if code in self._held_keys:
            self._held_keys.remove(code)

    def tap(self, keysym: int, hold: float = 0.045) -> None:
        self.key_down(keysym)
        time.sleep(hold)
        self.key_up(keysym)

    def chord(self, *keysyms: int, gap: float = 0.03) -> None:
        """Hold every key but the last, tap the last, release in reverse."""
        *mods, last = keysyms
        for m in mods:
            self.key_down(m)
            time.sleep(gap)
        self.tap(last)
        for m in reversed(mods):
            time.sleep(gap)
            self.key_up(m)

    def type(self, text: str, cps: float = 9.0, jitter: float = 0.35) -> None:
        """Type at `cps` characters a second, unevenly, the way hands do."""
        for ch in text:
            keysym = RETURN if ch == "\n" else ord(ch)
            _, shifted = self._code(keysym)
            if shifted:
                self.key_down(SHIFT)
            self.tap(keysym, hold=0.03 + self.rng.random() * 0.03)
            if shifted:
                self.key_up(SHIFT)
            base = 1.0 / cps
            time.sleep(max(0.02, base * (1 + self.rng.uniform(-jitter, jitter))))

    # ── mouse ───────────────────────────────────────────────────────────
    def move(self, x: int, y: int, duration: float | None = None) -> None:
        """Glide to (x, y) along a slight arc, eased at both ends."""
        x0, y0 = self.x, self.y
        dist = math.hypot(x - x0, y - y0)
        if dist < 1:
            return
        if duration is None:
            duration = min(0.85, 0.28 + dist / 2600)
        # A control point pushed off the straight line, to one side or
        # the other: hands do not travel in rulers.
        side = self.rng.choice((-1, 1))
        bend = dist * self.rng.uniform(0.04, 0.10) * side
        mx, my = (x0 + x) / 2, (y0 + y) / 2
        nx, ny = -(y - y0) / dist, (x - x0) / dist
        cx, cy = mx + nx * bend, my + ny * bend
        steps = max(2, int(duration * MOVE_HZ))
        for i in range(1, steps + 1):
            t = _ease(i / steps)
            px = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x
            py = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y
            self._fake(MOTION, 0, round(px), round(py))
            time.sleep(duration / steps)
        self.x, self.y = x, y

    def button_down(self, button: int = LEFT_BUTTON) -> None:
        self._fake(BUTTON_PRESS, button)
        self._held_buttons.append(button)

    def button_up(self, button: int = LEFT_BUTTON) -> None:
        self._fake(BUTTON_RELEASE, button)
        if button in self._held_buttons:
            self._held_buttons.remove(button)

    def click(self, button: int = LEFT_BUTTON, hold: float = 0.07) -> None:
        self.button_down(button)
        time.sleep(hold)
        self.button_up(button)

    def scroll(self, up: bool = True, notches: int = 1,
               interval: float = 0.16) -> None:
        for _ in range(notches):
            self.click(WHEEL_UP if up else WHEEL_DOWN, hold=0.02)
            time.sleep(interval)

    def drag(self, x: int, y: int, *, button: int = LEFT_BUTTON,
             mods: tuple[int, ...] = (SUPER,),
             duration: float | None = None) -> None:
        """Press `mods` and `button` where the pointer is, glide to (x, y),
        release — qtile's mod+drag to move or resize a floating window."""
        for m in mods:
            self.key_down(m)
        time.sleep(0.05)
        self.button_down(button)
        time.sleep(0.08)
        self.move(x, y, duration)
        time.sleep(0.08)
        self.button_up(button)
        time.sleep(0.04)
        for m in reversed(mods):
            self.key_up(m)

    # ── teardown ────────────────────────────────────────────────────────
    def close(self) -> None:
        for button in list(self._held_buttons):
            self._fake(BUTTON_RELEASE, button)
        for code in reversed(self._held_keys):
            self._fake(KEY_RELEASE, code)
        self._held_buttons.clear()
        self._held_keys.clear()
        self.conn.disconnect()
