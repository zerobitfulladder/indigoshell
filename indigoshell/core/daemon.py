import os
import sys
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .ipc import IPCServer
from .registry import build_registry
from .singleton import acquire_lock
from .store import Store

_DAEMON: "Daemon | None" = None


def get_daemon() -> "Daemon":
    """Module-level accessor for the running daemon."""
    if _DAEMON is None:
        raise RuntimeError("daemon is not running")
    return _DAEMON


class Daemon:
    """Owns the GTK main loop, the window registry, the state store, and IPC."""

    def __init__(self, config: dict) -> None:
        global _DAEMON
        if _DAEMON is not None:
            raise RuntimeError("daemon already constructed in this process")
        _DAEMON = self

        self.config = config
        self.store = Store()
        self.kinds = build_registry(config)
        for kind in self.kinds.values():
            kind._daemon = self
        self.instances: dict[str, Gtk.Window] = {}
        self.anchors: dict[str, Any] = {}
        self._lock_fd: int | None = None
        self._ipc: IPCServer | None = None

    def run(self) -> None:
        self._lock_fd = acquire_lock()
        self._ipc = IPCServer(self)
        self._ipc.start()
        GLib.idle_add(self._autostart)
        Gtk.main()

    def _autostart(self) -> bool:
        for kind in self.kinds.values():
            if kind.autostart:
                self.open(kind.name)
        return False

    def register_anchors(self, widget) -> None:
        """Scan a built bar widget (and any nested widgets) for click handlers
        tagged with `_indigo_popup_name`, and record each as the anchor for
        that popup so IPC `open <name>` can position it like a bar click."""
        handler_attrs = (
            "on_left_click", "on_right_click", "on_middle_click",
            "on_scroll_up", "on_scroll_down",
            "on_hover_enter", "on_hover_leave",
        )
        for w in widget.walk():
            gtk_w = getattr(w, "gtk_widget", None)
            if gtk_w is None:
                continue
            for attr in handler_attrs:
                handler = getattr(w, attr, None)
                name = getattr(handler, "_indigo_popup_name", None)
                if name:
                    self.anchors[name] = gtk_w

    # ── public API (called by IPC and by widgets) ────────────────────────
    def open(self, name: str, params: dict | None = None, anchor: Any = None) -> str:
        if name not in self.kinds:
            raise KeyError(f"unknown window kind: {name}")
        kind = self.kinds[name]
        if kind.singleton and name in self.instances:
            return name
        if anchor is None:
            anchor = self.anchors.get(name)
        win = kind.build(self.store, params or {}, anchor=anchor, config=self.config)
        win.show_all()
        self.instances[name] = win
        win.connect("destroy", lambda _w, n=name: self.instances.pop(n, None))
        return name

    def close(self, name: str) -> bool:
        win = self.instances.pop(name, None)
        if win is None:
            return False
        kind = self.kinds.get(name)
        if kind is not None:
            kind.teardown(win)
        else:
            win.destroy()
        return True

    def toggle(self, name: str, params: dict | None = None, anchor: Any = None) -> str:
        if name in self.instances:
            self.close(name)
            return "closed"
        if anchor is None:
            anchor = self.anchors.get(name)
        self.open(name, params, anchor)
        return "opened"

    def list_instances(self) -> list[str]:
        return list(self.instances.keys())

    def list_kinds(self) -> list[str]:
        return list(self.kinds.keys())

    def reload(self) -> None:
        for name in list(self.instances):
            self.close(name)
        if self._ipc is not None:
            self._ipc.stop()
        os.execv(sys.executable, [sys.executable, *sys.argv])
