"""The browser's log: `<state_dir>/browser.log`.

Pure Python, set up before anything else (`__main__`), so a hand-off to
a running browser logs here too without loading Qt.

Started from the launcher the browser's stdout and stderr go nowhere
(services/proc.py `launch`), so this file is where a browser that ended
by itself leaves a trace. It rotates as the shell's `--log-file` does
(core/log.py: at 1 MiB, three kept), at info by default (`--log-level`,
`$<APP>_LOG_LEVEL`); page console messages stay at debug. The terminal
gets the same lines when stderr is one.

What it holds besides the browser's own lines:

- every unhandled Python exception, with its traceback (`unhandled`, as
  sys.excepthook and threading.excepthook). PySide hands an exception
  escaping a slot or a Python override of a Qt virtual to sys.excepthook
  too, and the browser goes on; one escaping `main` ends it, with no
  exit line after.
- on a fatal signal (a crash in Qt or Chromium's browser side), the
  Python stack of every thread (faulthandler), appended to the file.
- at a clean end, the line saying why it ended (app.py): a signal, or
  its last window closed. A log that stops without one is a browser
  that did not end cleanly.
"""

import faulthandler
import logging
import os
import sys
import threading

from indigoshell.core import log as shell_log
from indigoshell.core.paths import state_dir

log = logging.getLogger(__name__.rsplit(".", 1)[0])

FILE = "browser.log"

_crash_file = None              # faulthandler's own handle on the file, kept open


def path() -> str:
    return os.path.join(state_dir(), FILE)


def setup(level: str | None = None) -> None:
    """The file, the terminal when stderr is one, and the hooks."""
    target = path()
    shell_log.setup(level, target, stderr=_tty(sys.stderr))
    install_hooks()
    _crash_trace(target)


def install_hooks() -> None:
    sys.excepthook = unhandled
    threading.excepthook = _thread_unhandled


def unhandled(kind, value, tb) -> None:
    if issubclass(kind, KeyboardInterrupt):
        log.info("interrupted (Ctrl+C before the browser could take it)")
        return
    log.error("unhandled exception", exc_info=(kind, value, tb))


def _thread_unhandled(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    log.error("unhandled exception in thread %s", name,
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _crash_trace(target: str) -> None:
    """faulthandler on a handle of its own: the logging handler closes
    and reopens its stream when it rotates, and a fatal signal must not
    find that descriptor closed (or reused). After a rotation it writes
    into the rotated file, which is still in the directory."""
    global _crash_file
    if _crash_file is not None:
        return
    try:
        _crash_file = open(target, "a", encoding="utf-8")
    except OSError:
        return
    faulthandler.enable(file=_crash_file, all_threads=True)


def _tty(stream) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError):
        return False
