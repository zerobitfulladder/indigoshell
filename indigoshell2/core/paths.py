"""Filesystem locations, all derived from `naming.APP`.

v2 runs alongside v1 during the migration, so every runtime path is
namespaced by the app name — sharing a socket or lock file would make
the two daemons fight over the singleton and the IPC endpoint. At
cutover the name changes and these follow.
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
