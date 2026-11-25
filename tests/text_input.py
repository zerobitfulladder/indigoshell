"""The shell's text field (widgets/text_input.py): selecting, and the
clipboard keys every text box of the shell gets from it (the launcher,
the call, the top bar's inputs).

    .venv/bin/python tests/text_input.py

  1. Shift+arrows/Home/End select (with Ctrl, by word); typing replaces
     the selection, a deleting key deletes it, Left/Right drop it at
     that end; select_all as before (the address bar's whole address);
  2. Ctrl+C / Ctrl+X copy / cut the selection; with nothing selected
     they are left to the holder (the call stops the agent on Ctrl+C);
  3. Ctrl+V and Shift+Insert paste at the caret, over the selection,
     line breaks as spaces; an empty or unreadable clipboard pastes
     nothing.

No display, and never the real clipboard: the field's clipboard is a
stand-in here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indigoshell.widgets.base import KeyEvent                 # noqa: E402
from indigoshell.widgets.text_input import TextInput           # noqa: E402

LEFT, RIGHT, HOME, END = 0xFF51, 0xFF53, 0xFF50, 0xFF57
BACKSPACE, INSERT = 0xFF08, 0xFF63
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed += not ok


def field(text: str = "", caret: int | None = None, clip: str = "") -> TextInput:
    f = TextInput()
    f.invalidate = lambda layout=False: None
    f.board = {"text": clip}

    async def read() -> str:
        if f.board["text"] is None:
            raise RuntimeError("the clipboard is empty")
        return f.board["text"]

    def write(value: str) -> bool:
        f.board["text"] = value
        return True
    f.read_clipboard, f.write_clipboard = read, write
    f.set_text(text, caret)
    return f


def key(f: TextInput, keysym: int, text: str = "", **mods) -> bool:
    return f.key(KeyEvent(keysym, text, **mods))


def ctrl(f: TextInput, letter: str, **mods) -> bool:
    return f.key(KeyEvent(ord(letter), "", ctrl=True, **mods))


print("1. selecting")
f = field("hello world", caret=11)
key(f, LEFT, shift=True)
key(f, LEFT, shift=True)
check("Shift+Left twice: the last two characters", f.selection == (9, 11), f"{f.selection}")
key(f, LEFT, shift=True, ctrl=True)
check("Ctrl+Shift+Left: on to the word's start", f.selection == (6, 11), f"{f.selection}")
key(f, 0x58, "X")
check("typing replaces the selection", f.text == "hello X" and f.selection is None, f.text)
f = field("hello world", caret=0)
key(f, END, shift=True)
check("Shift+End: to the end", f.selection == (0, 11) and f.selected_all)
key(f, BACKSPACE)
check("a deleting key deletes the selection", f.text == "" and f.caret == 0)
f = field("hello world", caret=5)
key(f, RIGHT, shift=True)
key(f, RIGHT, shift=True)
key(f, LEFT)
check("Left drops the selection at its start", f.selection is None and f.caret == 5, f"{f.caret}")
f = field("abc", caret=1)
key(f, HOME, shift=True)
key(f, RIGHT)
check("Right drops it at its end", f.selection is None and f.caret == 1, f"{f.caret}")
f = field("https://example.com/")
f.select_all()
check("select_all: the whole text, as the address bar holds it", f.selected_all)
key(f, 0x64, "d")
check("... the first key typed replaces it", f.text == "d")

print("2. copy and cut")
f = field("hello world", caret=11, clip="before")
check("Ctrl+C with nothing selected: left to the holder",
      ctrl(f, "c") is False and f.board["text"] == "before")
check("Ctrl+X with nothing selected: left to the holder", ctrl(f, "x") is False)
key(f, LEFT, shift=True, ctrl=True)
check("Ctrl+C copies the selection", ctrl(f, "c") is True and f.board["text"] == "world"
      and f.text == "hello world", f.board["text"])
f.board["text"] = ""
check("Ctrl+X cuts it", ctrl(f, "x") is True and f.board["text"] == "world"
      and f.text == "hello " and f.selection is None, repr(f.text))

print("3. paste")
f = field("go to ", caret=6, clip="example.org")
check("Ctrl+V pastes at the caret", ctrl(f, "v") is True and f.text == "go to example.org"
      and f.caret == 17, f.text)
f = field("x", caret=1, clip="yz")
check("Shift+Insert pastes too", key(f, INSERT, shift=True) is True and f.text == "xyz", f.text)
f = field("one TWO three", caret=4, clip="2")
key(f, RIGHT, shift=True, ctrl=True)
ctrl(f, "v")
check("over the selection", f.text == "one 2 three", f.text)
f = field("", clip="first line\nsecond line\r\n\tthird\n")
ctrl(f, "v")
check("line breaks become spaces (one line)", f.text == "first line second line third", f.text)
f = field("keep", caret=4, clip=None)
check("an empty clipboard: nothing pasted", ctrl(f, "v") is True and f.text == "keep")

print(f"\n{'all ok' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
