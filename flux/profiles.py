"""Flux's profiles: which one a browser process runs, and its places.

Pure Python (no Qt): the browser, its client (control.py) and the
shell's launcher all name the same files.

One browser process per profile. With no name it is the default
profile, the one Flux always had, its places exactly as they always
were (the user's sign-ins live there). `--profile NAME` (flux/qt
__main__) runs another, a Flux of its own beside the default one, with
its own storage, cache, history, bookmarks, log and socket:

                default                          NAME
    storage     <data_dir>/browser               <data_dir>/browser-NAME
    cache       <cache_dir>/browser              <cache_dir>/browser-NAME
    history     <data_dir>/browser-history.sqlite
                                                 <data_dir>/browser-NAME-history.sqlite
    bookmarks   <config_dir>/browser/bookmarks.json
                                                 <config_dir>/browser/bookmarks-NAME.json
    log         <state_dir>/browser.log          <state_dir>/browser-NAME.log
    socket      protocol.instance_socket_path()  the same with "-NAME" before ".sock"

What every profile shares: sites.json (the per-site identity and the
sites kept from the agent: the user's rules, for every browser of
theirs), the Widevine module and the Downloads folder.

A name is lowercase letters, digits, "-" and "_", starting with a
letter or digit, at most NAME_MAX characters: it goes into file names
and a socket path (which has room for 108 bytes in all).
"""

import os
import re

from indigoshell.core.paths import cache_dir, config_dir, data_dir, state_dir

BASE = "browser"                # the default profile's name in every place above
NAME_MAX = 32
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,%d}" % (NAME_MAX - 1))


def check(profile: str | None) -> str | None:
    """`profile` as a name (None for the default), or ValueError saying
    what a name may be."""
    if profile is None or profile == "":
        return None
    if not isinstance(profile, str) or not NAME_RE.fullmatch(profile):
        raise ValueError(f"{profile!r} is no profile name: lowercase letters, digits, - and _, "
                         f"at most {NAME_MAX}, starting with a letter or digit")
    return profile


def stem(profile: str | None = None) -> str:
    """"browser", or "browser-NAME"."""
    name = check(profile)
    return f"{BASE}-{name}" if name else BASE


def storage_path(profile: str | None = None) -> str:
    return os.path.join(data_dir(), stem(profile))


def cache_path(profile: str | None = None) -> str:
    return os.path.join(cache_dir(), stem(profile))


def history_path(profile: str | None = None) -> str:
    return os.path.join(data_dir(), f"{stem(profile)}-history.sqlite")


def bookmarks_path(profile: str | None = None) -> str:
    name = check(profile)
    return os.path.join(config_dir(), BASE, f"bookmarks-{name}.json" if name else "bookmarks.json")


def log_path(profile: str | None = None) -> str:
    return os.path.join(state_dir(), f"{stem(profile)}.log")


def socket_path(default: str, profile: str | None = None) -> str:
    """A profile's socket, from the default profile's: "-NAME" before its
    ".sock" (after the whole path when it has no such end)."""
    name = check(profile)
    if not name:
        return default
    root, ext = os.path.splitext(default)
    return f"{root}-{name}{ext}" if ext == ".sock" else f"{default}-{name}"
