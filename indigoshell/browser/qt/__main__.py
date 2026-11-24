"""python -m indigoshell.browser.qt [url ...]

Opens the URLs as tabs of one window. With a browser already running,
hands them over instead (each becomes a tab in its most recently used
window; no URL asks it for a new window) and exits.

    --unattended     a test window: floating in the bottom-right corner,
                     never taking the keyboard, muted, every permission
                     request denied
    --no-window      with no URL, start with no window: the browser only
                     listens (its control socket), for the agent, whose
                     first request opens one (browser/control.py,
                     `ensure_running`). With a browser already running,
                     exit at once and hand it nothing.
    --log-level L    debug, info, warning, error (default: $<APP>_LOG_LEVEL or info);
                     the log is <state_dir>/browser.log, and the terminal
                     when stderr is one (logs.py)
    --install        put Jack, the browser's entry, in the launcher
                     (browser/desktop.py) and exit

The shell's CLI runs this as `indigoshell browser [url ...]` (app.py),
in its own process: it works with the daemon down, and a hand-off
still exits without loading Qt.

Environment (besides the XDG directories):
    <APP>_BROWSER_SOCKET     the single-instance and control socket (its lock is
                             <socket>.lock)
    <APP>_APPS_SOCKET        where the shell listens for its apps (applink)
    <APP>_BROWSER_WIDEVINE   a libwidevinecdm.so to use, or "off"
"""

import argparse
import logging
import os
import sys

from indigoshell.core import log as shell_log
from indigoshell.core.naming import APP

from . import instance, logs

log = logging.getLogger(f"{APP}.browser.qt")


def _absolute(arg: str) -> str:
    """A path on disk as a file:// URL, so a browser started elsewhere
    finds it; anything else as typed."""
    if "://" not in arg and os.path.exists(arg):
        return "file://" + os.path.abspath(arg)
    return arg


def main(argv: list[str], prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog or f"python -m {APP}.browser.qt",
        description="The shell's web browser. Opens the URLs as tabs of a new "
                    "window, or hands them to the browser already running (each a "
                    "tab in its most recently used window; none asks it for a new "
                    "window).")
    parser.add_argument("url", nargs="*")
    parser.add_argument("--unattended", action="store_true",
                        help="test window: corner, no focus, muted, permissions denied")
    parser.add_argument("--no-window", action="store_true",
                        help="with no URL, open no window: only listen (for the agent); "
                             "with a browser running, do nothing")
    parser.add_argument("--log-level", choices=shell_log.LEVELS)
    parser.add_argument("--install", action="store_true",
                        help="put Jack, the browser's entry, in the launcher "
                             "($XDG_DATA_HOME/applications) and exit; the default "
                             "browser is left as it is")
    args = parser.parse_args(argv)
    if args.install:
        from indigoshell.browser import desktop      # pure: no Qt
        return desktop.main()
    logs.setup(args.log_level)

    urls = [_absolute(u) for u in args.url]
    if args.no_window and not urls:
        # Started for the agent: a running browser (or one starting) is
        # all it wants, and a hand-off of nothing would open a window.
        lock = instance.try_lock()
        if lock is None:
            log.info("a browser is running already; --no-window leaves it be")
            return 0
    else:
        try:
            lock = instance.claim_or_hand_off(urls)
        except instance.HandoffFailed as e:
            log.error("%s", e)
            return 1
        if lock is None:
            log.info("handed %s to the running browser",
                     f"{len(urls)} URL(s)" if urls else "a new window")
            return 0

    from . import app          # Qt and Chromium load here, and only here
    return app.main(urls, unattended=args.unattended, lock=lock, no_window=args.no_window)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
