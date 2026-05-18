from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class WindowKind:
    """A registered top-level window type.

    Subclasses describe their lifecycle policy (autostart, singleton, transient)
    and implement `build()` to return a `Gtk.Window`. The daemon owns the
    window's lifetime; kinds only describe what they are.
    """

    name: str = ""
    autostart: bool = False
    singleton: bool = True
    transient: bool = False
    default_timeout_ms: int | None = None

    # Back-ref set by Daemon at registry build. Kinds use it to call back into
    # daemon.close (e.g. Escape key on a popup).
    _daemon: Any = None

    def build(self, store, params: dict, *, anchor: Any = None, config: dict | None = None) -> Gtk.Window:
        raise NotImplementedError

    def teardown(self, window: Gtk.Window) -> None:
        window.destroy()
