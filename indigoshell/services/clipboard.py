"""Putting text on the selections, and reading it back.

Owning a selection means answering every request for it for as long as
we hold it, which a window that parks and unparks is badly placed to do
— so the text is handed to a tool that forks and serves it until
something else takes the selection: wl-copy (indigoshell/wm). PRIMARY is
what a middle click pastes; CLIPBOARD is Ctrl+V.
"""

from .. import wm


def copy(value: str, selection: str = "clipboard") -> bool:
    """Hand `value` to the clipboard tool for `selection`. False if
    there is nothing to copy or the tool is missing."""
    return wm.get().copy(value, selection)


async def paste(selection: str = "clipboard") -> str:
    """The text on `selection`; WMError when it is empty or not text."""
    return await wm.get().paste(selection)
