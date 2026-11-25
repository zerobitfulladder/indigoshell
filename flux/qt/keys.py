"""Which key does what in a browser window.

    Alt+1 .. Alt+9              go to tab N (Alt+9 is the ninth, not the last)
    Alt+Shift+1 .. Alt+Shift+9  move the current tab to position N
    Ctrl+T                      new tab: an address for it; the tab is made
                                when one is picked
    Ctrl+W                      close the tab; the last one closes the window
    Ctrl+Shift+T                reopen the tab closed last, with its history
    Ctrl+Tab, Ctrl+Shift+Tab    next, previous tab
    Ctrl+L                      address or search, in this tab
    Ctrl+M                      the window's actions: bookmarks
    Ctrl+F                      find in the page (window.py has its keys)
    Alt+Left, Alt+Right         back, forward
    Ctrl+R, F5                  reload
    Ctrl+Shift+R, Ctrl+F5,      reload past the cache, as Firefox does
      Shift+F5
    F12                         inspector
    Ctrl++, Ctrl+-, Ctrl+0      zoom in, out, back to 100%, kept for the page's
                                site (flux/zoom.py); Ctrl+= is Ctrl++, the keypad's
                                + - 0 the same; Ctrl+wheel a step a notch (window.py)
    Esc                         leave fullscreen (only while fullscreen)

On a main window a shell takes Ctrl+L, Ctrl+T and Ctrl+M before they
get here (flux/protocol.py, KEYS) and answers them with its launcher.
When they do get here (no shell, a single window), Ctrl+L and Ctrl+T ask
with a small dialog of the browser's own, and Ctrl+M does nothing.

They are taken before the page sees them: the window filters key
presses on its QWindow, which gets every key first, whatever widget
inside has focus.

Digits are found by key position as well as by symbol. Alt+Shift+1
reaches Qt as "!" on a US layout, and on AZERTY the digits are the
shifted level; X's keycodes 10..19 are the top row's 1..9, 0 on every
layout (evdev's KEY_1.. plus X's offset of 8), so the position decides
when the symbol is not a digit.
"""

from PySide6.QtCore import Qt

K = Qt.Key
M = Qt.KeyboardModifier

_TOP_ROW = {10 + i: i + 1 for i in range(9)}     # X keycode -> digit 1..9
_ZERO = 19

_PLAIN = {
    (M.ControlModifier, K.Key_T): "new_tab",
    (M.ControlModifier, K.Key_W): "close_tab",
    (M.ControlModifier | M.ShiftModifier, K.Key_T): "reopen_tab",
    (M.ControlModifier, K.Key_Tab): "next_tab",
    (M.ControlModifier | M.ShiftModifier, K.Key_Tab): "prev_tab",
    (M.ControlModifier | M.ShiftModifier, K.Key_Backtab): "prev_tab",
    (M.ControlModifier, K.Key_Backtab): "prev_tab",
    (M.ControlModifier, K.Key_L): "address",
    (M.ControlModifier, K.Key_M): "actions",
    (M.ControlModifier, K.Key_F): "find",
    (M.AltModifier, K.Key_Left): "back",
    (M.AltModifier, K.Key_Right): "forward",
    (M.ControlModifier, K.Key_R): "reload",
    (M.NoModifier, K.Key_F5): "reload",
    (M.ControlModifier | M.ShiftModifier, K.Key_R): "hard_reload",
    (M.ControlModifier, K.Key_F5): "hard_reload",
    (M.ShiftModifier, K.Key_F5): "hard_reload",
    (M.NoModifier, K.Key_F12): "inspector",
}

_RELEVANT = M.ControlModifier | M.AltModifier | M.ShiftModifier | M.MetaModifier
COMMAND_MODIFIERS = M.ControlModifier | M.AltModifier | M.MetaModifier

# Keys that only modify others: never text, never an action of their own.
MODIFIER_KEYS = frozenset({K.Key_Shift, K.Key_Control, K.Key_Alt, K.Key_Meta,
                           K.Key_AltGr, K.Key_Super_L, K.Key_Super_R, K.Key_CapsLock,
                           K.Key_NumLock})
# What scrolls a page: passed to it in find mode.
SCROLL_KEYS = frozenset({K.Key_Left, K.Key_Right, K.Key_Up, K.Key_Down, K.Key_PageUp,
                         K.Key_PageDown, K.Key_Home, K.Key_End})


def _mods(modifiers) -> M:
    return M(modifiers) & _RELEVANT


WHEEL_NOTCH = 120               # angleDelta of one notch of a mouse wheel


def wheel_zooms(modifiers) -> bool:
    """Ctrl (Shift ignored, as for the keys) with the wheel: zoom."""
    return _mods(modifiers) in (M.ControlModifier, M.ControlModifier | M.ShiftModifier)


def command_held(modifiers) -> bool:
    """Ctrl, Alt or Meta: a shortcut, not typing."""
    return bool(M(modifiers) & COMMAND_MODIFIERS)


def action_for(key: int, modifiers, scancode: int = 0):
    """The action for a key press: a name, ("tab", n), ("move", n), or None."""
    mods = _mods(modifiers)
    alt, shift = M.AltModifier, M.ShiftModifier
    if mods in (alt, alt | shift):
        digit = None
        if K.Key_1 <= key <= K.Key_9:
            digit = key - K.Key_1 + 1
        elif scancode in _TOP_ROW:
            digit = _TOP_ROW[scancode]
        if digit is not None:
            return ("move" if mods & shift else "tab", digit)
    ctrl = M.ControlModifier
    if mods in (ctrl, ctrl | shift):
        # Zoom ignores Shift: Ctrl++ is Ctrl+Shift+= on most layouts.
        if key in (K.Key_Plus, K.Key_Equal):
            return "zoom_in"
        if key in (K.Key_Minus, K.Key_Underscore):
            return "zoom_out"
    if mods == ctrl and (key == K.Key_0 or scancode == _ZERO):
        return "zoom_reset"
    try:
        return _PLAIN.get((mods, K(key)))
    except ValueError:
        return None
