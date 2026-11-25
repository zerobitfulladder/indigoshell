"""python -m flux.qt [url ...]

Opens the URLs as tabs of one window. With a browser already running,
hands them over instead (each becomes a tab in its most recently used
window; no URL asks it for a new window) and exits.

    --unattended     nobody attends this browser (the agent's own, as on
                     a machine that lends it to the hub): a page's dialog
                     outside the agent's session is answered at once, a
                     file chooser outside an upload gets nothing, every
                     permission request is denied, pages are muted, no
                     address dialog of its own, and its last window
                     closing does not end it (SIGTERM or a `quit` request
                     does). protocol.py, "Unattended".
    --no-window      with no URL, start with no window: the browser only
                     listens (its control socket), for the agent, whose
                     first request opens one (flux/control.py,
                     `ensure_running`). With a browser already running,
                     exit at once and hand it nothing.
    --software-gl    a window drawn without the GPU, for a screen that is
                     another machine's (`ssh -X`): startup.py
    --profile NAME   a browser of the profile NAME, beside the default one:
                     its own storage, cache, history, bookmarks, log and
                     socket (flux/profiles.py). Hand-offs go to that
                     profile's browser.
    --log-level L    debug, info, warning, error (default: $<APP>_LOG_LEVEL or info);
                     the log is <state_dir>/browser.log (browser-NAME.log
                     for a profile), and the terminal when stderr is one
                     (logs.py)
    --install        put Flux, the browser's entry, in the launcher
                     (flux/desktop.py) and exit

The shell's CLI runs this as `indigoshell browser [url ...]` (app.py),
in its own process: it works with the daemon down, and a hand-off
still exits without loading Qt.

Environment (besides the XDG directories):
    <APP>_BROWSER_SOCKET     the single-instance and control socket (its lock is
                             <socket>.lock); a profile's is this with -NAME
                             before .sock
    <APP>_APPS_SOCKET        where the shell listens for its apps (applink)
    <APP>_BROWSER_WIDEVINE   a libwidevinecdm.so to use, or "off"
"""

import argparse
import logging
import os
import sys

from flux import profiles
from indigoshell.core import log as shell_log

from . import instance, logs, startup

log = logging.getLogger("flux.qt")


def _absolute(arg: str) -> str:
    """A path on disk as a file:// URL, so a browser started elsewhere
    finds it; anything else as typed."""
    if "://" not in arg and os.path.exists(arg):
        return "file://" + os.path.abspath(arg)
    return arg


def _profile(text: str) -> str:
    try:
        return profiles.check(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv: list[str], prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog or "python -m flux.qt",
        description="The shell's web browser. Opens the URLs as tabs of a new "
                    "window, or hands them to the browser already running (each a "
                    "tab in its most recently used window; none asks it for a new "
                    "window).")
    parser.add_argument("url", nargs="*")
    parser.add_argument("--unattended", action="store_true",
                        help="nobody attends it: stray dialogs answered, permissions "
                             "denied, muted, outlives its last window")
    parser.add_argument("--no-window", action="store_true",
                        help="with no URL, open no window: only listen (for the agent); "
                             "with a browser running, do nothing")
    parser.add_argument("--software-gl", action="store_true",
                        help="draw the windows without the GPU (a screen over ssh -X)")
    parser.add_argument("--profile", type=_profile, metavar="NAME",
                        help="the profile NAME's browser, beside the default one")
    parser.add_argument("--log-level", choices=shell_log.LEVELS)
    parser.add_argument("--install", action="store_true",
                        help="put Flux, the browser's entry, in the launcher "
                             "($XDG_DATA_HOME/applications) and exit; the default "
                             "browser is left as it is")
    args = parser.parse_args(argv)
    if args.install:
        from flux import desktop      # pure: no Qt
        return desktop.main()
    logs.setup(args.log_level, args.profile)

    urls = [_absolute(u) for u in args.url]
    if args.no_window and not urls:
        # Started for the agent: a running browser (or one starting) is
        # all it wants, and a hand-off of nothing would open a window.
        lock = instance.try_lock(args.profile)
        if lock is None:
            log.info("a browser is running already; --no-window leaves it be")
            return 0
    else:
        try:
            lock = instance.claim_or_hand_off(urls, profile=args.profile)
        except instance.HandoffFailed as e:
            log.error("%s", e)
            return 1
        if lock is None:
            log.info("handed %s to the running browser",
                     f"{len(urls)} URL(s)" if urls else "a new window")
            return 0

    startup.settle_platform(software=args.software_gl)
    from . import app          # Qt and Chromium load here, and only here
    return app.main(urls, unattended=args.unattended, lock=lock, no_window=args.no_window,
                    software=args.software_gl, profile=args.profile)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
