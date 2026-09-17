"""Subprocess helpers on asyncio.

Everything is a coroutine on the one loop, so there is no thread and
nothing to marshal — a callback fires directly on the loop that owns the
widget tree.

Three shapes:
  run(cmd)               -> stdout as a string ("" on missing binary)
  fire(cmd)              -> fire-and-forget
  subscribe(cmd, on_line)-> long-running; on_line per stdout line
"""

import asyncio
import logging
import shutil
from typing import Callable, Sequence

log = logging.getLogger(__name__)


async def run(cmd: Sequence[str], timeout: float = 5.0) -> str:
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return out.decode(errors="replace")


def fire(cmd: Sequence[str], *, detach: bool = False) -> None:
    """Run and forget. Deliberately not awaited — callers are click
    handlers, which must not block the loop waiting on pactl."""
    async def _go():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=detach)
        except (OSError, FileNotFoundError):
            log.debug("cannot run %s", cmd, exc_info=True)
            return
        # Reaped so it doesn't linger as a zombie.
        await proc.wait()

    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        log.debug("no running loop; dropped %s", cmd)


class Subscription:
    """A long-running command whose stdout lines drive a callback."""

    def __init__(self, cmd: Sequence[str], on_line: Callable[[str], None],
                 *, on_missing: Callable[[], None] | None = None) -> None:
        self.cmd = list(cmd)
        self.on_line = on_line
        self.on_missing = on_missing
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        if shutil.which(self.cmd[0]) is None:
            log.warning("%s not found", self.cmd[0])
            if self.on_missing:
                self.on_missing()
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            if self.on_missing:
                self.on_missing()
            return
        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                try:
                    self.on_line(raw.decode(errors="replace").rstrip("\n"))
                except Exception:
                    log.exception("subscriber for %s raised", self.cmd[0])
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await self._proc.wait()
            except (ProcessLookupError, OSError):
                pass
        self._proc = None

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def subscribe(cmd: Sequence[str], on_line: Callable[[str], None],
              *, on_missing: Callable[[], None] | None = None) -> Subscription:
    sub = Subscription(cmd, on_line, on_missing=on_missing)
    sub.start()
    return sub
