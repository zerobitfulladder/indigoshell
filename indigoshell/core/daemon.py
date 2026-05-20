import os
import sys
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

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
        # Registered dialog scripts: short name → argv-prefix the
        # Pipeline orchestrator runs. Manifest `command` arrays reference
        # these by name, so scripts stay path-agnostic.
        self.scripts: dict[str, Any] = config.get("scripts") or {}
        # Pipeline entry points: name → initial manifest command. Used
        # by daemon.open(name) to detect a pipeline trigger and route
        # to start_pipeline() instead of looking up self.kinds[name].
        self.pipelines: dict[str, list[str]] = config.get("pipelines") or {}
        # Per-toast-name TermToast widget refs, so Pipeline orchestrators
        # can drive the widget's lifecycle after open (e.g. start_linger).
        self._toast_widgets: dict[str, Any] = {}
        # Active dialog pipeline, if any. Single-slot for now — opening
        # a new pipeline tears down any in progress.
        self._pipeline: Any = None
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
        # Pipeline trigger: routes to the dialog-tree orchestrator
        # instead of opening a popup directly.
        if name in self.pipelines:
            self.start_pipeline(name, list(self.pipelines[name]))
            return name
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
        self._toast_widgets.pop(name, None)
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

    def toast(
        self,
        command: list[str],
        *,
        name: str = "toast",
        cols: int = 80,
        rows: int = 20,
        linger_ms: int | None = None,
        corner_margin: tuple[int, int] | None = None,
        on_close: "object" = None,
        on_child_exit: "object" = None,
        on_grow: "object" = None,
        auto_close_on_exit: bool = True,
        auto_grow: bool = False,
        max_rows: int = 30,
        env: dict[str, str] | None = None,
    ) -> str:
        """Spawn a one-shot Terminal popup at the top-right that runs
        `command`, then auto-closes via a perimeter-trace animation
        after the command exits. Default slot name is "toast"; pass a
        unique `name` (e.g. pipeline session) to have multiple toasts
        open simultaneously.

        `corner_margin` overrides the default top-right offset (useful
        when stacking under another popup — see Pipeline).
        `on_close` is invoked after the toast tears itself down (linger
        end or manual close). `on_child_exit` fires the moment the
        child process exits, *before* any linger — pipeline scripts
        use this to read a manifest and decide the next stage.
        `auto_close_on_exit=False` keeps the toast visible after the
        child exits; the orchestrator can later call `start_linger()`
        on the toast widget to turn it into a leaf."""
        from .. import theme
        from ..widgets.term_toast import TermToast
        from ..windows.popup import PopupKind

        if name in self.instances:
            self.close(name)
        self.kinds.pop(name, None)

        def _done():
            self.close(name)
            if callable(on_close):
                on_close()

        kwargs: dict = {}
        if env is not None:
            kwargs["env"] = env
        toast_widget = TermToast(
            command,
            cols=cols, rows=rows,
            linger_ms=theme.TOAST_LINGER_MS if linger_ms is None else linger_ms,
            on_done=_done,
            on_child_exit=on_child_exit if callable(on_child_exit) else None,
            on_grow=on_grow if callable(on_grow) else None,
            auto_close_on_exit=auto_close_on_exit,
            auto_grow=auto_grow,
            max_rows=max_rows,
            **kwargs,
        )
        # Notification chrome (bevel/border/padding/anchor) but the bg
        # stays the standard semi-transparent POPUP_BG so picom's blur
        # shows through — matches the look of the other terminal popups
        # (fastfetch / sptlrx / spotify-player / nmtui).
        kind = PopupKind(
            name=name,
            content=toast_widget,
            corner="top-right",
            # Symmetric margin: NOTIF_OFFSET_Y is the lift used to clear
            # the bottom bar for notifications — top-anchored popups don't
            # need that, so we mirror NOTIF_OFFSET_X on both axes.
            corner_margin=corner_margin or (theme.NOTIF_OFFSET_X, theme.NOTIF_OFFSET_X),
            bevel=theme.NOTIF_BEVEL,
            bevel_corners=theme.NOTIF_BEVEL_CORNERS,
            border=theme.NOTIF_FRAME_NORMAL,
            border_thick=theme.NOTIF_BORDER_THICK,
            padding=theme.NOTIF_PADDING_Y,
            # UTILITY is focusable so the toast can grab keyboard focus
            # on spawn (matters when wrapped pkexec / sudo agents want
            # to interact). Always-on-top is preserved by the qtile
            # floating_layout Match on wm_class="indigoshell-popup".
            type_hint=Gdk.WindowTypeHint.UTILITY,
        )
        kind._daemon = self
        self.kinds[name] = kind
        # Keep a handle to the widget so callers (e.g. a Pipeline) can
        # invoke methods like `start_linger()` once the orchestrator
        # decides this toast is the leaf node.
        self._toast_widgets[name] = toast_widget
        return self.open(name)

    def start_pipeline(self, session: str, initial_command: list[str]) -> None:
        """Start a dialog-tree cascade rooted at `initial_command` (which
        must reference a script registered in self.scripts). Closes any
        in-progress pipeline first."""
        from .pipeline import Pipeline
        if self._pipeline is not None:
            try:
                self._pipeline._close_all()
            except Exception:
                pass
        self._pipeline = Pipeline(self, session, initial_command)

    def toast_widget(self, name: str):
        """Return the TermToast widget for a previously-opened toast, or
        None if the slot is closed. Used by Pipeline to drive the
        widget's lifecycle (start_linger) after spawn."""
        return self._toast_widgets.get(name)

    def reload(self) -> None:
        for name in list(self.instances):
            self.close(name)
        if self._ipc is not None:
            self._ipc.stop()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def quit(self) -> None:
        """Tear down all windows and exit the GTK main loop."""
        for name in list(self.instances):
            self.close(name)
        if self._ipc is not None:
            self._ipc.stop()
        Gtk.main_quit()
