"""The agent's hands on a page: real Qt input, sent to the page's view.

A page's view (QWebEngineView) takes input through its focus proxy, the
widget QtWebEngine draws into. A QMouseEvent, QKeyEvent, QWheelEvent or
QInputMethodEvent sent there is what the user's own input becomes once
Qt has it, and Chromium gives it to the page as trusted input
(`event.isTrusted`), the same as theirs. The agent's go there with
`sendEvent`, straight to that widget: never through the window, where
the Watch stops the user's input (watch.py), and never spontaneous (the
window system's events are, the agent's are not). While one is being
sent, `Injector.sending` is up: an input method commit arriving at the
view when it is not is the user's.

What QtWebEngine does with them, measured on PySide6 6.12 (off screen,
the tests have the cases):

- Keys need no focus: the window need not be active and the page need
  never have been clicked; a field focused from the agent's world
  (reader.js `focusField`) takes them.
- A key press with text types that text, non-ASCII too (Turkish ğ ş ı
  İ), given the key a keyboard would report: Qt's key for the character
  when it has one, else the code of the character's uppercase, as X11's
  xcb does for a symbol Qt has no key for.
- An input method commit inserts a whole text at once: the page sees
  beforeinput and input (trusted), no keydown. 2100 characters: 0.05 s
  as one commit, 2.4 s as key presses.
- The wheel moves by `angleDelta` alone (`pixelDelta` is ignored): one
  notch (120) scrolls the platform's wheel lines x 20 px, 60 px by
  default, smoothly.
- QtWebEngine counts clicks itself, from the presses' timestamps and
  places; a MouseButtonDblClick event adds nothing. A double click is two
  presses close in time, and two presses stamped alike (a constructed
  event's timestamp is 0) are one, even from two separate requests. So
  every event here is stamped with the monotonic clock in milliseconds
  (X's own time base on Linux, so the user's presses compare sanely),
  and a lone click never lands within the double-click interval of the
  press before it.

Qt: nothing here is newer than 6.8 (QWheelEvent's and QMouseEvent's
QPointF constructors are Qt 6.0, QStyleHints.wheelScrollLines 5.9).
"""

import time

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import (QInputEvent, QInputMethodEvent, QKeyEvent, QMouseEvent,
                           QWheelEvent)
from PySide6.QtWidgets import QApplication

K = Qt.Key
M = Qt.KeyboardModifier
B = Qt.MouseButton
T = QEvent.Type

BUTTONS = {"left": B.LeftButton, "middle": B.MiddleButton}

# Named keys: the Qt key and the text a keyboard gives with it.
NAMED = {
    "enter": (K.Key_Return, "\r"), "return": (K.Key_Return, "\r"),
    "escape": (K.Key_Escape, "\x1b"), "esc": (K.Key_Escape, "\x1b"),
    "tab": (K.Key_Tab, "\t"), "backspace": (K.Key_Backspace, "\x08"),
    "delete": (K.Key_Delete, "\x7f"), "del": (K.Key_Delete, "\x7f"),
    "space": (K.Key_Space, " "), "insert": (K.Key_Insert, ""),
    "arrowup": (K.Key_Up, ""), "up": (K.Key_Up, ""),
    "arrowdown": (K.Key_Down, ""), "down": (K.Key_Down, ""),
    "arrowleft": (K.Key_Left, ""), "left": (K.Key_Left, ""),
    "arrowright": (K.Key_Right, ""), "right": (K.Key_Right, ""),
    "home": (K.Key_Home, ""), "end": (K.Key_End, ""),
    "pageup": (K.Key_PageUp, ""), "pagedown": (K.Key_PageDown, ""),
    **{f"f{n}": (getattr(K, f"Key_F{n}"), "") for n in range(1, 13)},
}
MODIFIERS = {"ctrl": M.ControlModifier, "control": M.ControlModifier,
             "shift": M.ShiftModifier, "alt": M.AltModifier,
             "meta": M.MetaModifier, "super": M.MetaModifier, "cmd": M.MetaModifier}
COMMAND = M.ControlModifier | M.AltModifier | M.MetaModifier


def char_key(ch: str) -> int:
    """The Qt key a keyboard reports for a character typed."""
    if ch == " ":
        return int(K.Key_Space)
    up = ch.upper()
    return ord(up) if len(up) == 1 else 0


def parse_key(spec: str) -> tuple[int, M, str]:
    """"Enter", "ctrl+a", "shift+Tab", "PageDown", "a": (Qt key,
    modifiers, text). ValueError says what is wrong."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("give a key, like Enter, ctrl+a or PageDown")
    parts = spec.strip().split("+")
    if len(parts) > 1 and parts[-1] == "" and parts[-2] == "":
        parts = parts[:-2] + ["+"]              # ctrl++
    *mods_named, name = parts
    mods = M.NoModifier
    for m in mods_named:
        flag = MODIFIERS.get(m.strip().lower())
        if flag is None:
            raise ValueError(f"{m!r} is no modifier (ctrl, shift, alt, meta)")
        mods |= flag
    command = bool(mods & COMMAND)
    if len(name) == 1:
        text = name.upper() if mods & M.ShiftModifier else name
        return char_key(name), mods, "" if command else text
    named = NAMED.get(name.strip().lower())
    if named is None:
        raise ValueError(f"no key named {name!r}")
    key, text = named
    if key == K.Key_Tab and mods & M.ShiftModifier:
        key, text = K.Key_Backtab, ""           # as Qt reports Shift+Tab
    return int(key), mods, "" if command else text


def wheel_px_per_notch() -> float:
    """How far one notch of the wheel (120) scrolls a page."""
    return QApplication.styleHints().wheelScrollLines() * 20.0


class Injector:
    """Sends the agent's input to a page's view. `sending` is up while
    it does."""

    def __init__(self) -> None:
        self.sending = 0
        self._pressed: int | None = None        # the last press's timestamp

    @staticmethod
    def now() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def send(self, widget, event, stamp: int | None = None) -> bool:
        if isinstance(event, QInputEvent):      # an input method's has no time
            event.setTimestamp(self.now() if stamp is None else stamp)
        self.sending += 1
        try:
            return QApplication.sendEvent(widget, event)
        finally:
            self.sending -= 1

    # ── the mouse ───────────────────────────────────────────────────────
    def _mouse(self, widget, kind, pos: QPointF, button, buttons, stamp: int) -> None:
        at = QPointF(pos)
        self.send(widget, QMouseEvent(kind, at, QPointF(widget.mapToGlobal(at)), button,
                                      buttons, M.NoModifier), stamp)

    def move(self, widget, pos: QPointF) -> None:
        self._mouse(widget, T.MouseMove, pos, B.NoButton, B.NoButton, self.now())

    def click(self, widget, pos: QPointF, button: str = "left", double: bool = False) -> None:
        """Moved there, pressed and released; for `double`, pressed and
        released again a millisecond later."""
        b = BUTTONS[button]
        stamp = self.now()
        gap = QApplication.styleHints().mouseDoubleClickInterval()
        if self._pressed is not None and 0 <= stamp - self._pressed <= gap:
            stamp = self._pressed + gap + 1     # a click of its own, not a double's second
        self._mouse(widget, T.MouseMove, pos, B.NoButton, B.NoButton, stamp)
        for n in range(2 if double else 1):
            self._mouse(widget, T.MouseButtonPress, pos, b, b, stamp + n)
            self._mouse(widget, T.MouseButtonRelease, pos, b, B.NoButton, stamp + n)
        self._pressed = stamp + (1 if double else 0)

    def wheel(self, widget, pos: QPointF, dx: float, dy: float) -> None:
        """About dx, dy pixels (right, down positive): the wheel's notches
        for them."""
        per = wheel_px_per_notch() or 60.0
        angle = QPoint(-round(dx / per * 120), -round(dy / per * 120))
        at = QPointF(pos)
        self.send(widget, QWheelEvent(at, QPointF(widget.mapToGlobal(at)), QPoint(0, 0), angle,
                                      B.NoButton, M.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                                      False))

    # ── keys ────────────────────────────────────────────────────────────
    def key(self, widget, key: int, mods=M.NoModifier, text: str = "") -> None:
        self.send(widget, QKeyEvent(T.KeyPress, key, mods, text))
        self.send(widget, QKeyEvent(T.KeyRelease, key, mods, text))

    def char(self, widget, ch: str) -> None:
        """One character, typed."""
        if ch == "\n":
            self.key(widget, int(K.Key_Return), M.NoModifier, "\r")
        elif ch == "\t":
            self.key(widget, int(K.Key_Tab), M.NoModifier, "\t")
        else:
            mods = M.ShiftModifier if ch != ch.lower() else M.NoModifier
            self.key(widget, char_key(ch), mods, ch)

    def commit(self, widget, text: str) -> None:
        """A text as an input method commits it: inserted whole."""
        event = QInputMethodEvent("", [])
        event.setCommitString(text)
        self.send(widget, event)
