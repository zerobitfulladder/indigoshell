"""Putting text on the X selections.

Owning a selection means answering every SelectionRequest for as long as
we hold it, which a window that parks and unparks is badly placed to do —
so the text is handed to `xclip`, which forks and serves it until
something else takes the selection. PRIMARY is what a middle click
pastes; CLIPBOARD is Ctrl+V.
"""

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)


def copy(value: str, selection: str = "clipboard") -> bool:
    """Hand `value` to xclip for `selection`. False if xclip is missing."""
    if not value or shutil.which("xclip") is None:
        if value:
            log.warning("cannot copy: xclip is not installed")
        return False

    async def _go():
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", selection, "-in",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                # Its own session, so the forked server outlives a reload.
                start_new_session=True)
            await proc.communicate(value.encode())
        except OSError:
            log.debug("xclip failed", exc_info=True)

    asyncio.get_running_loop().create_task(_go())
    return True
