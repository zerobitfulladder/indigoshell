"""Single-line text field.

Owns the editing and the caret, and knows nothing about what the text is
for: the launcher's query is the first user, and anything else that ever
needs typing gets the same keys for free.

Keys — readline's, where it has one:

    type                insert at the caret
    BackSpace / Delete  delete before / after
    Ctrl+BackSpace, Ctrl+W
                        delete the word before
    Ctrl+Delete         delete the word after
    Left / Right        move; with Ctrl, by word
    Home / End          to either end; Ctrl+A / Ctrl+E as well
    Ctrl+U / Ctrl+K     delete to the start / to the end

Anything else is left unconsumed, so Return, Escape, Up and Down reach
whatever holds the field.
"""

from typing import Callable

import skia

from .. import text, theme
from .base import KeyEvent, Size, Widget

CARET_W = 2.0
CARET_PAD_Y = 6.0
BLINK_S = 0.5           # half-period: on this long, then off this long
BLINK_FPS = 4
# Then the caret stays solid and the frame clock stops — GTK's
# gtk-cursor-blink-timeout. A field left open does not cost a frame
# every quarter second forever.
BLINK_FOR_S = 10.0
PAD_Y = 6

_BACKSPACE, _DELETE, _KP_DELETE = 0xFF08, 0xFFFF, 0xFF9F
_LEFT, _RIGHT, _HOME, _END = 0xFF51, 0xFF53, 0xFF50, 0xFF57
_KP_LEFT, _KP_RIGHT, _KP_HOME, _KP_END = 0xFF96, 0xFF98, 0xFF95, 0xFF9C


def ctrl_letter(ev: KeyEvent) -> str:
    """The letter of a Ctrl+letter chord, lower-cased; "" otherwise.
    Read from the keysym, since a Ctrl chord carries no text."""
    k = ev.keysym
    if ev.ctrl and (0x41 <= k <= 0x5A or 0x61 <= k <= 0x7A):
        return chr(k).lower()
    return ""


def _is_word(ch: str) -> bool:
    return ch.isalnum()


class TextInput(Widget):
    def __init__(self, *, placeholder: str = "",
                 size: float = theme.FONT_SIZE_LG,
                 color: str = theme.HUD_FG_BRIGHT,
                 placeholder_color: str = theme.BASE_MUTED,
                 caret_color: str = theme.CYAN_BRIGHT,
                 on_change: Callable[[str], None] | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder
        self.size = size
        self.color = color
        self.placeholder_color = placeholder_color
        self.caret_color = caret_color
        self.on_change = on_change
        self.text = ""
        self.caret = 0
        self._scroll_x = 0.0
        self._blink_age = 0.0        # seconds since the last edit or move
        self._blink_phase = 0.0
        self._caret_on = True

    # ── state ───────────────────────────────────────────────────────────
    @property
    def animation_fps(self) -> int:
        return BLINK_FPS if self._blink_age < BLINK_FOR_S else 0

    def set_text(self, value: str, caret: int | None = None) -> None:
        caret = len(value) if caret is None else max(0, min(caret, len(value)))
        changed = value != self.text
        self.text, self.caret = value, caret
        self._wake_caret()
        if changed and self.on_change is not None:
            self.on_change(value)

    def clear(self) -> None:
        self.set_text("")

    def _move(self, caret: int) -> None:
        caret = max(0, min(caret, len(self.text)))
        if caret != self.caret:
            self.caret = caret
        self._wake_caret()

    def _edit(self, start: int, end: int, insert: str = "") -> None:
        """Replace text[start:end] with `insert`; caret lands after it."""
        self.set_text(self.text[:start] + insert + self.text[end:],
                      start + len(insert))

    def _wake_caret(self) -> None:
        # Any edit or movement shows the caret solid for a full
        # half-period before it resumes blinking.
        self._blink_age = self._blink_phase = 0.0
        self._caret_on = True
        self.invalidate()

    def _word_left(self) -> int:
        i = self.caret
        while i > 0 and not _is_word(self.text[i - 1]):
            i -= 1
        while i > 0 and _is_word(self.text[i - 1]):
            i -= 1
        return i

    def _word_right(self) -> int:
        i, n = self.caret, len(self.text)
        while i < n and not _is_word(self.text[i]):
            i += 1
        while i < n and _is_word(self.text[i]):
            i += 1
        return i

    # ── input ───────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        k, letter = ev.keysym, ctrl_letter(ev)
        if ev.ctrl:
            if k == _BACKSPACE or letter == "w":
                self._edit(self._word_left(), self.caret)
            elif k in (_DELETE, _KP_DELETE):
                self._edit(self.caret, self._word_right())
            elif k in (_LEFT, _KP_LEFT):
                self._move(self._word_left())
            elif k in (_RIGHT, _KP_RIGHT):
                self._move(self._word_right())
            elif letter == "a":
                self._move(0)
            elif letter == "e":
                self._move(len(self.text))
            elif letter == "u":
                self._edit(0, self.caret)
            elif letter == "k":
                self._edit(self.caret, len(self.text))
            else:
                return False
            return True
        if ev.text:
            self._edit(self.caret, self.caret, ev.text)
        elif k == _BACKSPACE:
            if self.caret:
                self._edit(self.caret - 1, self.caret)
        elif k in (_DELETE, _KP_DELETE):
            self._edit(self.caret, self.caret + 1)
        elif k in (_LEFT, _KP_LEFT):
            self._move(self.caret - 1)
        elif k in (_RIGHT, _KP_RIGHT):
            self._move(self.caret + 1)
        elif k in (_HOME, _KP_HOME):
            self._move(0)
        elif k in (_END, _KP_END):
            self._move(len(self.text))
        else:
            return False
        return True

    # ── frame ───────────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)
        self._blink_age += dt
        if self._blink_age >= BLINK_FOR_S:
            self._caret_on = True
            return
        self._blink_phase += dt
        if self._blink_phase >= BLINK_S:
            self._blink_phase %= BLINK_S
            self._caret_on = not self._caret_on

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        return Size(0.0, text.line_height(self.size) + 2 * PAD_Y)

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        left, width = r.left(), r.width()
        caret_x = text.measure(self.text[:self.caret], self.size)
        # Scroll just far enough to keep the caret inside the field.
        if caret_x - self._scroll_x > width - CARET_W:
            self._scroll_x = caret_x - width + CARET_W
        elif caret_x < self._scroll_x:
            self._scroll_x = caret_x

        canvas.save()
        canvas.clipRect(r)
        baseline = text.baseline_in(r, self.size)
        if self.text:
            text.draw(canvas, self.text, left - self._scroll_x, baseline,
                      self.size, theme.color(self.color))
        elif self.placeholder:
            text.draw(canvas, self.placeholder, left + CARET_W * 3, baseline,
                      self.size, theme.color(self.placeholder_color))
        if self._caret_on:
            paint = skia.Paint(AntiAlias=False)
            paint.setColor(theme.color(self.caret_color))
            canvas.drawRect(skia.Rect.MakeXYWH(
                round(left + caret_x - self._scroll_x), r.top() + CARET_PAD_Y,
                CARET_W, r.height() - 2 * CARET_PAD_Y), paint)
        canvas.restore()
