"""TermToast: dual-mode VTE popup.

Two ways to use it:

  • **With a command** — runs the command in VTE, then on `child-exited`
    starts a perimeter-trace countdown over the popup's border; when the
    trace completes (or hover-pause finishes), fires `on_done` to tear
    the popup down. One-shot.

  • **Without a command** — spawns an interactive `$SHELL`, no linger
    animation. Behaves like a regular floating terminal: stays open
    until the shell exits (`exit` / Ctrl-D) or the popup is closed by
    the WM, at which point `on_done` fires so the popup tears itself
    down too.

Same widget class powers both modes; the popup registration just picks
which constructor argument shape to use.
"""

import os
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import theme
from .base import beveled_polyline, paint, stroke_partial
from .terminal import Terminal


def _default_shell() -> list[str]:
    shell = os.environ.get("SHELL") or "/bin/bash"
    return [shell]


class TermToast(Terminal):
    def __init__(
        self,
        command: list[str] | None = None,
        *,
        linger_ms: int = theme.TOAST_LINGER_MS,
        on_done: Callable[[], object] | None = None,
        on_child_exit: Callable[[], object] | None = None,
        on_grow: Callable[[], object] | None = None,
        auto_close_on_exit: bool = True,
        auto_grow: bool = False,
        max_rows: int = 30,
        popup_name: str | None = None,
        cols: int = 80,
        rows: int = 20,
        **kwargs,
    ):
        # No command → interactive shell mode: no linger, no hover-pause
        # timer; closing the popup or exiting the shell tears down via
        # on_done immediately.
        self.interactive = command is None or len(command) == 0
        resolved = command if not self.interactive else _default_shell()
        # Always transparent so the popup's blurred chrome shows through,
        # and never respawn — both modes are single-process.
        kwargs.setdefault("transparent", True)
        super().__init__(resolved, cols=cols, rows=rows, respawn=False, **kwargs)
        self.linger_ms = max(0, linger_ms)
        self.on_done: Callable[[], object] | None = on_done
        # Optional callback fired the moment the child process exits
        # (before any linger). Pipelines use this to read a manifest the
        # script wrote and decide the next stage. Distinct from on_done,
        # which only fires after auto-close (linger end or interactive).
        self.on_child_exit: Callable[[], object] | None = on_child_exit
        # When True (non-interactive script-runners), the VTE row count
        # grows as the cursor advances past the visible area, capped at
        # `max_rows`. `on_grow` notifies the orchestrator so popups
        # below this one can shift down (cascade reflow).
        self.auto_grow = auto_grow and not self.interactive
        self.max_rows = max(rows, max_rows)
        self.on_grow: Callable[[], object] | None = on_grow
        self._current_rows = rows
        # When False, the toast stays open after the child exits — no
        # linger, no auto-close. Used by pipeline intermediate stages
        # where the orchestrator manually closes the toast at flow end.
        self.auto_close_on_exit = auto_close_on_exit
        # If no explicit on_done is given but the popup name is, default
        # to closing that popup via the daemon — lets statically
        # registered popups (config_default.WINDOWS) tear themselves
        # down without the daemon wiring the callback at construction.
        self.popup_name = popup_name

        self._toplevel: Gtk.Window | None = None
        self._trace_progress: float = 0.0
        self._trace_timer: int | None = None
        self._trace_elapsed_ms: float = 0.0
        self._trace_last_tick_us: int = 0
        self._trace_paused: bool = False
        self._done_fired: bool = False

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        super().start()
        if self.gtk_widget is None:
            return
        top = self.gtk_widget.get_toplevel()
        if not isinstance(top, Gtk.Window):
            return
        self._toplevel = top
        # Force-focus the VTE on map so typing works immediately — both
        # for the interactive shell and for any one-shot toast that
        # might need to feed input to a wrapped sudo/pkexec/etc.
        top.set_accept_focus(True)
        top.set_focus_on_map(True)
        top.connect("map-event", self._grab_focus_on_map)
        if not self.interactive:
            # Trace overlay + hover-pause only matter for one-shot mode.
            top.connect_after("draw", self._draw_trace)
            top.add_events(
                Gdk.EventMask.ENTER_NOTIFY_MASK
                | Gdk.EventMask.LEAVE_NOTIFY_MASK
            )
            top.connect("enter-notify-event", self._on_hover_enter)
            top.connect("leave-notify-event", self._on_hover_leave)
        if self.auto_grow and self._term is not None:
            # VTE fires contents-changed for every visual update; we
            # poll cursor position and grow rows when content has
            # advanced past the current visible area.
            self._term.connect("contents-changed", self._on_contents_changed)

    def _on_contents_changed(self, _term) -> None:
        if not self.auto_grow or self._term is None:
            return
        try:
            _col, row = self._term.get_cursor_position()
        except Exception:
            return
        # `row` is 0-indexed; +1 for the cursor row itself, +1 leading
        # blank line of breathing room before the popup feels cramped.
        needed = max(self._current_rows, min(self.max_rows, row + 2))
        if needed <= self._current_rows:
            return
        self._current_rows = needed
        self._term.set_size(self.cols, needed)
        # GTK reacts to VTE's natural-size change on its own; do NOT
        # force `resize(1,1)` — that snaps the window to its minimum
        # natural, and VTE's natural can shrink back after the child
        # exits, leaving the toast suddenly tiny. Instead, lock the
        # new size as a floor on the next idle tick so the toast can
        # still grow further but never shrink below where it is now.
        if self._toplevel is not None:
            GLib.idle_add(self._lock_current_size)
        if self.on_grow is not None:
            try:
                self.on_grow()
            except Exception:
                pass

    def _lock_current_size(self) -> bool:
        if self._toplevel is None:
            return False
        w, h = self._toplevel.get_size()
        # set_size_request enforces a minimum; the popup can still
        # grow further (next _on_contents_changed bumps the floor).
        self._toplevel.set_size_request(w, h)
        return False

    def _grab_focus_on_map(self, _win, _event) -> bool:
        # `present()` raises + requests focus from the WM; `grab_focus`
        # on the VTE then makes it the keyboard target inside our window.
        if self._toplevel is not None:
            self._toplevel.present()
        if self.gtk_widget is not None:
            self.gtk_widget.grab_focus()
        return False

    def stop(self):
        if self._trace_timer is not None:
            GLib.source_remove(self._trace_timer)
            self._trace_timer = None
        super().stop()

    # ── child exit ──────────────────────────────────────────────────
    def _on_child_exited(self, _term, _status):
        # Skip super()._on_child_exited (which would respawn).
        self._pid = None
        if self._stopping:
            return
        # Pipeline orchestrators hook here to read a manifest the script
        # wrote and decide what to open next. Fires before any linger so
        # the orchestrator can choose to keep this toast open (when more
        # stages follow) or let it auto-close (linger trace, see below).
        if self.on_child_exit is not None:
            try:
                self.on_child_exit()
            except Exception:
                pass
        # Orchestrator opted out of auto-close: leave the toast visible
        # until something else (linger trigger, daemon.close) ends it.
        if not self.auto_close_on_exit:
            return
        # Interactive mode: shell exited (`exit` / Ctrl-D / killed) →
        # close the popup immediately, no linger.
        if self.interactive or self.linger_ms <= 0:
            self._fire_done()
            return
        # One-shot mode: kick off the linger trace.
        self.start_linger()

    def start_linger(self) -> None:
        """Manually trigger the linger trace + auto-close.
        Useful for pipeline orchestrators that opened this toast with
        `auto_close_on_exit=False` and want to turn it into a leaf node
        once the manifest declares no follow-up options."""
        if self.linger_ms <= 0:
            self._fire_done()
            return
        if self._trace_timer is not None:
            return  # already running
        self._trace_elapsed_ms = 0.0
        self._trace_last_tick_us = GLib.get_monotonic_time()
        self._trace_progress = 0.0
        if self._toplevel is not None:
            self._toplevel.queue_draw()
        self._trace_timer = GLib.timeout_add(theme.TOAST_TICK_MS, self._tick_trace)

    def _tick_trace(self) -> bool:
        now_us = GLib.get_monotonic_time()
        if not self._trace_paused:
            self._trace_elapsed_ms += (now_us - self._trace_last_tick_us) / 1000
        self._trace_last_tick_us = now_us
        self._trace_progress = min(1.0, self._trace_elapsed_ms / self.linger_ms)
        if self._toplevel is not None:
            self._toplevel.queue_draw()
        if self._trace_progress >= 1.0:
            self._trace_timer = None
            self._fire_done()
            return False
        return True

    # ── hover pause ──────────────────────────────────────────────────
    def _on_hover_enter(self, _w, ev) -> bool:
        if ev.detail == Gdk.NotifyType.INFERIOR:
            return False
        self._trace_paused = True
        return False

    def _on_hover_leave(self, _w, ev) -> bool:
        if ev.detail == Gdk.NotifyType.INFERIOR:
            return False
        self._trace_last_tick_us = GLib.get_monotonic_time()
        self._trace_paused = False
        return False

    def _fire_done(self) -> None:
        if self._done_fired:
            return
        self._done_fired = True
        if self.on_done is not None:
            self.on_done()
        elif self.popup_name is not None:
            # Deferred import — widgets → core.daemon cycle otherwise.
            from ..core.daemon import get_daemon
            get_daemon().close(self.popup_name)

    # ── trace overlay ───────────────────────────────────────────────
    def _draw_trace(self, w, cr) -> bool:
        if self._trace_progress <= 0.0:
            return False
        alloc = w.get_allocation()
        line_w = theme.NOTIF_BORDER_THICK
        pts = beveled_polyline(
            alloc.width, alloc.height,
            bevel=theme.NOTIF_BEVEL,
            corners=theme.NOTIF_BEVEL_CORNERS,
            inset=line_w / 2,
        )
        total = 0.0
        for i in range(len(pts) - 1):
            dx = pts[i + 1][0] - pts[i][0]
            dy = pts[i + 1][1] - pts[i][1]
            total += (dx * dx + dy * dy) ** 0.5
        paint(cr, theme.NOTIF_TIMER_BORDER_FG)
        cr.set_line_width(line_w)
        stroke_partial(cr, pts, total * self._trace_progress)
        return False
