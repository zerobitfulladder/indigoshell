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
    Shift+Left/Right, Shift+Home/End (with Ctrl: by word)
                        select
    Ctrl+C / Ctrl+X     copy / cut the selection to the clipboard
    Ctrl+V, Shift+Insert
                        paste the clipboard at the caret, over the
                        selection; line breaks become spaces (one line)

Anything else is left unconsumed, so Return, Escape, Up and Down reach
whatever holds the field — and so do Ctrl+C and Ctrl+X when nothing is
selected (the call stops the agent on Ctrl+C then).

A selection runs from `anchor` to the caret. The text can be selected
whole (`select_all`), the way a browser's address bar holds the current
address. While something is selected the first character typed
replaces it, a deleting key deletes it, Left/Home and Right/End drop it
at that end. Keys the field does not take leave it selected.
"""

import asyncio
import logging

from typing import Callable

import skia

from .. import text, theme
from .base import KeyEvent, Size, Widget

log = logging.getLogger(__name__)

CARET_W = 2.0
CARET_PAD_Y = 6.0
BLINK_S = 0.5           # half-period: on this long, then off this long
BLINK_FPS = 4
# Then the caret stays solid and the frame clock stops — GTK's
# gtk-cursor-blink-timeout. A field left open does not cost a frame
# every quarter second forever.
BLINK_FOR_S = 10.0
PAD_Y = 6
SELECTION = (theme.CYAN_BRIGHT, 0.28)   # behind selected text

_BACKSPACE, _DELETE, _KP_DELETE = 0xFF08, 0xFFFF, 0xFF9F
_LEFT, _RIGHT, _HOME, _END = 0xFF51, 0xFF53, 0xFF50, 0xFF57
_KP_LEFT, _KP_RIGHT, _KP_HOME, _KP_END = 0xFF96, 0xFF98, 0xFF95, 0xFF9C
_INSERT, _KP_INSERT = 0xFF63, 0xFF9E
_STARTS = (_LEFT, _KP_LEFT, _HOME, _KP_HOME)
_ENDS = (_RIGHT, _KP_RIGHT, _END, _KP_END)


async def _clipboard_text() -> str:
    from ..services import clipboard        # the window manager's tool (indigoshell/wm)
    return await clipboard.paste("clipboard")


def _to_clipboard(value: str) -> bool:
    from ..services import clipboard
    return clipboard.copy(value, "clipboard")


def one_line(value: str) -> str:
    """Pasted text for a single-line field: line breaks and tabs as
    single spaces, none at either end."""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return " ".join(line.strip().replace("\t", " ") for line in lines if line.strip())


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
                 blink_for: float = BLINK_FOR_S,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder
        # How long the caret blinks after the last edit before it holds
        # still and lets the frame clock stop. `math.inf` blinks for as
        # long as the field is shown — the call, where a steady caret
        # read as a line gone dead.
        self.blink_for = blink_for
        self.size = size
        self.color = color
        self.placeholder_color = placeholder_color
        self.caret_color = caret_color
        self.on_change = on_change
        self.text = ""
        self.caret = 0
        self.anchor: int | None = None  # the selection's other end; None: nothing selected
        # The clipboard, as the field reads and writes it (tests stand in).
        self.read_clipboard = _clipboard_text
        self.write_clipboard = _to_clipboard
        # False hides the caret and stops its blinking: a field shown but
        # not typed into (the top bar's find, stepping through matches).
        self.caret_shown = True
        self._scroll_x = 0.0
        self._blink_age = 0.0        # seconds since the last edit or move
        self._blink_phase = 0.0
        self._caret_on = True

    # ── state ───────────────────────────────────────────────────────────
    @property
    def animation_fps(self) -> int:
        return BLINK_FPS if self.caret_shown and self._blink_age < self.blink_for else 0

    @property
    def selection(self) -> tuple[int, int] | None:
        """(start, end) of what is selected; None when nothing is."""
        if self.anchor is None or self.anchor == self.caret:
            return None
        return min(self.anchor, self.caret), max(self.anchor, self.caret)

    @property
    def selected_all(self) -> bool:
        return bool(self.text) and self.selection == (0, len(self.text))

    @selected_all.setter
    def selected_all(self, value: bool) -> None:
        if value:
            self.select_all()
        else:
            self.anchor = None

    def set_text(self, value: str, caret: int | None = None, *,
                 notify: bool = True) -> None:
        """Replace the text. `notify=False` keeps `on_change` quiet, for
        a holder that will act on the new text itself."""
        caret = len(value) if caret is None else max(0, min(caret, len(value)))
        changed = value != self.text
        self.text, self.caret = value, caret
        self.anchor = None
        self._wake_caret()
        if changed and notify and self.on_change is not None:
            self.on_change(value)

    def select_all(self) -> None:
        """Select the whole text (nothing, when there is none)."""
        self.anchor = 0 if self.text else None
        self.caret = len(self.text)
        self._wake_caret()

    def clear(self) -> None:
        self.set_text("")

    def _move(self, caret: int, *, extend: bool = False) -> None:
        """The caret to `caret`; `extend`: the selection grows or shrinks
        with it (from where the caret was), else it is dropped."""
        if extend:
            if self.anchor is None:
                self.anchor = self.caret
        else:
            self.anchor = None
        caret = max(0, min(caret, len(self.text)))
        if caret != self.caret:
            self.caret = caret
        self._wake_caret()

    # ── the clipboard ───────────────────────────────────────────────────
    def copy_selection(self, *, cut: bool = False) -> bool:
        """The selection to the clipboard (and out of the field, `cut`);
        False when nothing is selected, so the key goes to the holder."""
        sel = self.selection
        if sel is None:
            return False
        if not self.write_clipboard(self.text[sel[0]:sel[1]]):
            log.info("nothing on the clipboard: its tool is missing")
        if cut:
            self._edit(*sel)
        return True

    def paste(self) -> None:
        """The clipboard's text at the caret, over the selection, once it
        is read (asynchronously: the clipboard is another program's)."""
        async def run() -> None:
            try:
                value = one_line(await self.read_clipboard())
            except Exception as e:      # noqa: BLE001  (empty, not text, no tool)
                log.debug("nothing pasted: %s", e)
                return
            if value:
                self.insert(value)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run())          # no loop (a test): at once
        else:
            loop.create_task(run())

    def insert(self, value: str) -> None:
        """`value` at the caret, over the selection."""
        start, end = self.selection or (self.caret, self.caret)
        self._edit(start, end, value)

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
        if (ev.ctrl and letter == "v") or (ev.shift and not ev.ctrl
                                           and k in (_INSERT, _KP_INSERT)):
            self.paste()
            return True
        if ev.ctrl and letter in ("c", "x"):
            return self.copy_selection(cut=letter == "x")
        if ev.shift and k in _STARTS + _ENDS:
            if k in (_HOME, _KP_HOME):
                target = 0
            elif k in (_END, _KP_END):
                target = len(self.text)
            elif k in (_LEFT, _KP_LEFT):
                target = self._word_left() if ev.ctrl else self.caret - 1
            else:
                target = self._word_right() if ev.ctrl else self.caret + 1
            self._move(target, extend=True)
            return True
        if self.selection is not None and self._key_on_selection(ev):
            return True
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

    def _key_on_selection(self, ev: KeyEvent) -> bool:
        """A key while something is selected; False when it is not one
        that acts on the selection (the caller then handles it, with the
        selection still there)."""
        k, letter = ev.keysym, ctrl_letter(ev)
        start, end = self.selection
        deleting = (k in (_BACKSPACE, _DELETE, _KP_DELETE)
                    or (ev.ctrl and letter in ("w", "u", "k")))
        if deleting or (ev.text and not ev.ctrl):
            self._edit(start, end, "" if deleting else ev.text)
        elif k in (_HOME, _KP_HOME) or (ev.ctrl and letter == "a"):
            self._move(0)
        elif k in (_LEFT, _KP_LEFT):
            self._move(start)
        elif k in (_END, _KP_END) or (ev.ctrl and letter == "e"):
            self._move(len(self.text))
        elif k in (_RIGHT, _KP_RIGHT):
            self._move(end)
        else:
            return False
        return True

    # ── frame ───────────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)
        self._blink_age += dt
        if self._blink_age >= self.blink_for:
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
        sel = self.selection
        if sel is not None:
            band = skia.Paint(AntiAlias=False)
            band.setColor(theme.color(*SELECTION))
            x0 = text.measure(self.text[:sel[0]], self.size)
            x1 = text.measure(self.text[:sel[1]], self.size)
            canvas.drawRect(skia.Rect.MakeLTRB(
                left - self._scroll_x + x0, r.top() + CARET_PAD_Y,
                left - self._scroll_x + x1, r.bottom() - CARET_PAD_Y), band)
        if self.text:
            text.draw(canvas, self.text, left - self._scroll_x, baseline,
                      self.size, theme.color(self.color))
        elif self.placeholder:
            text.draw(canvas, self.placeholder, left + CARET_W * 3, baseline,
                      self.size, theme.color(self.placeholder_color))
        if self._caret_on and self.caret_shown:
            paint = skia.Paint(AntiAlias=False)
            paint.setColor(theme.color(self.caret_color))
            canvas.drawRect(skia.Rect.MakeXYWH(
                round(left + caret_x - self._scroll_x), r.top() + CARET_PAD_Y,
                CARET_W, r.height() - 2 * CARET_PAD_Y), paint)
        canvas.restore()
