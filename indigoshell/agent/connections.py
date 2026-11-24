"""Where the agent keeps its links to things outside the computer.

Each one has its own folder in the shell's config dir,
`~/.config/indigoshell/connections/<name>/`:

    spotify/   config.json: the Spotify app's client_id and client_secret
    wiz/       config.json: the bulbs' names

Tokens the shell gets by signing in (Spotify's) are state, not settings,
and stay in the state dir.

Until 2026-11 these files sat loose in the config dir (spotify.json,
wiz.json). `tidy()`, run when the agent's tools start, moves them into
their folders once; until it has run, `path()` finds a file where it
used to be.
"""

import logging
import os

from ..core import paths

log = logging.getLogger(__name__)

# (connection, file) -> its old name, loose in the config dir.
LEGACY = {
    ("spotify", "config.json"): "spotify.json",
    ("wiz", "config.json"): "wiz.json",
}


def folder(name: str) -> str:
    return os.path.join(paths.config_dir(), "connections", name)


def path(name: str, file: str) -> str:
    """A connection's file: in its folder, or where it was before
    `tidy()` moved it, if it is still there."""
    new = os.path.join(folder(name), file)
    old = LEGACY.get((name, file))
    if old and not os.path.exists(new):
        loose = os.path.join(paths.config_dir(), old)
        if os.path.exists(loose):
            return loose
    return new


def tidy() -> dict[str, str]:
    """Move loose files into their folders (never over one already
    there); returns {old path: new path}."""
    moved = {}
    for (name, file), old in LEGACY.items():
        src = os.path.join(paths.config_dir(), old)
        dst = os.path.join(folder(name), file)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            os.makedirs(folder(name), mode=0o700, exist_ok=True)
            os.replace(src, dst)
        except OSError as e:
            log.warning("couldn't move %s to %s: %s", src, dst, e)
            continue
        moved[src] = dst
    if moved:
        log.info("moved into %s: %s", os.path.dirname(folder("")),
                 ", ".join(os.path.basename(p) for p in moved))
    return moved
