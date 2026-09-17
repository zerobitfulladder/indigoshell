"""Handlers for config files.

`on_left_click=toggle("system")` reads better than a lambda that
imports the daemon, and keeps config declarative. The daemon is looked up
lazily inside the handler because config is imported before it exists.
"""

from typing import Callable


def _handler(verb: str, name: str) -> Callable:
    def run(_source=None):
        from .core.daemon import get_daemon
        getattr(get_daemon(), verb)(name)

    # Lets the daemon find which widget anchors which window, the same way
    # v1 tagged handlers with _indigo_popup_name.
    run._indigo_window = name
    run.__name__ = f"{verb}_{name}"
    return run


def open(name: str) -> Callable:      # noqa: A001 - mirrors the IPC verb
    return _handler("open", name)


def close(name: str) -> Callable:
    return _handler("close", name)


def toggle(name: str) -> Callable:
    return _handler("toggle", name)
