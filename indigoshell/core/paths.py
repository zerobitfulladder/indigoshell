"""Filesystem locations, all derived from `naming.APP`.

Every runtime path is namespaced by the app name, so a second build
under a different package name gets its own socket, lock, config and
state instead of fighting the installed one over the singleton and the
IPC endpoint.
"""

import os

from .naming import APP


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


def socket_path() -> str:
    return os.path.join(runtime_dir(), f"{APP}-{os.getuid()}.sock")


def lock_path() -> str:
    return os.path.join(runtime_dir(), f"{APP}-{os.getuid()}.lock")


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, APP)


def log_path() -> str:
    """Default `--log-file` target. Not created unless the flag is used."""
    return os.path.join(state_dir(), f"{APP}.log")
