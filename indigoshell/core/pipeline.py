"""Dialog-tree pipeline orchestrator.

Each pipeline step is a "dialog script" — a registered command the user
declares in `config["scripts"]: dict[str, list[str]]` — that:

  1. Runs as a TermToast (its stdout is what the user sees).
  2. Writes a JSON manifest to the path in `$INDIGOSHELL_MANIFEST`
     listing zero or more follow-up options, each:
        {"label": "<menu text>", "command": ["<script-name>", "arg1", ...]}
  3. Exits.

The orchestrator reads the manifest on child-exit and decides:

  * **0 options** → leaf node. Triggers the linger trace on that toast,
    closes the entire cascade when it ends.
  * **1 option**  → auto-advance. Runs that option's command as the
    next stage in a new toast stacked below.
  * **N options** → opens a chord Menu beneath the current toast; on
    selection runs the chosen option's command.

Every toast in the cascade stays visible until the leaf's linger
completes — the user sees the full history of stdout from each step.

If the user dismisses any popup in the cascade (Escape, mod+q, click
outside a grabbed menu, …), the whole cascade tears down — anything
else would leave orphans on screen.

Scripts are referenced by short name so a script's argv is portable —
the orchestrator resolves `["display_set", "HDMI-A-0"]` to the actual
path + interpreter at run time.
"""

import json
import os
import sys
import tempfile

from .. import theme

_GAP_PX = 8                  # vertical gap between stacked popups in a cascade
# Fallbacks used only when a popup's allocation isn't realised yet
# (e.g. when stacking under a toast that just got opened in the same
# event-loop tick). After the first paint we query Gtk.Window's actual
# allocated height instead.
_TOAST_HEIGHT_FALLBACK = 220
_MENU_HEIGHT_FALLBACK  = 50


class Pipeline:
    """Drives one dialog-tree cascade. Held by the daemon for the
    duration of the cascade; cleared when the leaf's linger ends or any
    popup is dismissed by the user."""

    def __init__(self, daemon, session: str, initial: list[str]):
        self.daemon = daemon
        self.session = session         # base name (e.g. "display-menu")
        self.popups: list[str] = []    # opened popup names in stacking order
        self.menus: list[str] = []     # subset of popups that are menus
        self._step_n: int = 0
        self._manifest_paths: list[str] = []
        self._closing: bool = False    # guard against re-entrant teardown
        # Per-menu cancel command (set from manifest when present). If
        # the user hits Escape / mod+q on a menu, the orchestrator runs
        # this command instead of tearing the cascade down.
        self._cancel_for: dict[str, list[str]] = {}
        # Slots already routed through user-dismiss handling, so the
        # destroy signal firing again later doesn't re-trigger.
        self._dismissed: set[str] = set()

        self._run_step(initial)

    # ── geometry ─────────────────────────────────────────────────────
    def _next_y(self) -> int:
        """Compute the corner_margin Y for the next popup we open by
        summing the real rendered heights of all popups already in the
        cascade. Falls back to constants when a window hasn't been
        allocated yet (rare — only inside the same event-loop tick as
        its own show)."""
        y = theme.NOTIF_OFFSET_X
        for slot in self.popups:
            win = self.daemon.instances.get(slot)
            if win is None:
                continue
            h = win.get_allocated_height()
            if h <= 1:
                # Not yet allocated; estimate by popup kind.
                h = _MENU_HEIGHT_FALLBACK if slot in self.menus else _TOAST_HEIGHT_FALLBACK
            y += h + _GAP_PX
        return y

    # ── stage runners ────────────────────────────────────────────────
    def _resolve(self, command: list[str]) -> list[str] | None:
        """Resolve a manifest `command` (head is a registered script
        name) to an executable argv. Returns None if name is unknown."""
        if not command:
            return None
        scripts = getattr(self.daemon, "scripts", {}) or {}
        script = scripts.get(command[0])
        if script is None:
            return None
        # `scripts` entries may be either a list[str] argv (e.g.
        # ["python3", "/path/to/foo.py"]) or a single str path (assumed
        # executable). Either way, append the rest of the manifest args.
        if isinstance(script, (list, tuple)):
            return [*script, *command[1:]]
        return [sys.executable, str(script), *command[1:]]

    def _run_step(self, command: list[str]) -> None:
        argv = self._resolve(command)
        if argv is None:
            # Unknown script name: silently end the cascade.
            self._close_all()
            return

        # NamedTemporaryFile w/ delete=False keeps a stable path; we
        # close immediately so the child script owns the open handle.
        tf = tempfile.NamedTemporaryFile(
            prefix="indigoshell-pipeline-", suffix=".json", delete=False, mode="w"
        )
        manifest_path = tf.name
        tf.close()
        os.unlink(manifest_path)   # script will recreate; an absent file = no options
        self._manifest_paths.append(manifest_path)

        slot = f"{self.session}:{self._step_n}"
        self._step_n += 1
        # Compute position BEFORE registering this slot so the new toast
        # sits below the last popup, not below itself.
        corner_y = self._next_y()
        self.popups.append(slot)

        env = {"INDIGOSHELL_MANIFEST": manifest_path}
        self.daemon.toast(
            argv,
            name=slot,
            # Start compact (4 rows) so a script that prints almost
            # nothing doesn't reserve a half-screen-tall blank slab.
            # auto_grow expands the VTE up to max_rows as output writes
            # past the current visible area; on_grow reflows popups
            # below this one to keep the cascade tidy.
            cols=80, rows=4, max_rows=24,
            corner_margin=(theme.NOTIF_OFFSET_X, corner_y),
            auto_close_on_exit=False,           # we drive lifecycle
            auto_grow=True,
            on_grow=self.reflow,
            on_child_exit=lambda p=manifest_path, s=slot: self._on_step_exit(p, s),
            env=env,
        )
        self._hook_destroy(slot)

    def _on_step_exit(self, manifest_path: str, slot: str) -> None:
        options, cancel = self._read_manifest(manifest_path)
        if not options:
            # Leaf: linger this toast, then close the whole cascade.
            widget = self.daemon.toast_widget(slot)
            if widget is None:
                self._close_all()
                return
            # Chain: when the toast's auto_close fires (linger end),
            # _done in Daemon.toast calls close(slot). We want full
            # cascade teardown, not just this slot — patch on_done.
            widget.on_done = self._close_all
            widget.auto_close_on_exit = True   # let start_linger() run cleanly
            widget.start_linger()
            return
        if len(options) == 1:
            # Auto-advance with no menu.
            self._run_step(options[0].get("command") or [])
            return
        # Branch — open a chord menu below the current stack.
        self._open_menu(options, cancel)

    @staticmethod
    def _read_manifest(path: str) -> tuple[list[dict], list[str] | None]:
        """Returns (options, cancel_command). Cancel command is the
        argv to run when the user dismisses the menu (Escape / mod+q);
        None if the manifest didn't declare one."""
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return [], None
        opts = data.get("options")
        opts = opts if isinstance(opts, list) else []
        cancel_block = data.get("cancel")
        cancel_cmd: list[str] | None = None
        if isinstance(cancel_block, dict):
            cmd = cancel_block.get("command")
            if isinstance(cmd, list) and cmd:
                cancel_cmd = [str(x) for x in cmd]
        return opts, cancel_cmd

    # ── menu stage ───────────────────────────────────────────────────
    def _open_menu(self, options: list[dict], cancel: list[str] | None) -> None:
        # Deferred imports — pipeline → widgets → daemon cycle.
        from ..widgets import MenuItem
        from ..widgets.menu import Menu
        from ..windows.popup import PopupKind

        slot = f"{self.session}:menu:{self._step_n}"
        self._step_n += 1
        corner_y = self._next_y()
        if cancel is not None:
            self._cancel_for[slot] = cancel

        items: list[MenuItem] = []
        for i, opt in enumerate(options, 1):
            label = str(opt.get("label", f"option {i}"))
            cmd = opt.get("command") or []
            items.append(MenuItem(str(i), label, lambda c=cmd: self._on_menu_pick(c)))

        on_cancel = (lambda s=slot: self._on_user_dismiss(s)) if cancel is not None else None
        menu_widget = Menu(
            popup_name=slot, items=items,
            auto_close=False, on_cancel=on_cancel,
        )
        kind = PopupKind(
            name=slot,
            content=menu_widget,
            corner="top-right",
            corner_margin=(theme.NOTIF_OFFSET_X, corner_y),
            bg=None, border=None, bevel=0, bevel_corners=(),
            blur=False, grab=True,
        )
        kind._daemon = self.daemon
        self.daemon.kinds[slot] = kind
        self.popups.append(slot)
        self.menus.append(slot)
        self.daemon.open(slot)
        self._hook_destroy(slot)

    def _on_menu_pick(self, command: list[str]) -> None:
        # Menu was opened with auto_close=False; we keep it visible.
        # Run the next step (a toast) underneath the menu.
        self._run_step(command)

    # ── reflow ───────────────────────────────────────────────────────
    def reflow(self) -> None:
        """Recompute Y positions for every popup in the cascade and
        move them. Called when a toast grows (auto_grow) so popups
        below shift down to make room."""
        y = theme.NOTIF_OFFSET_X
        for slot in self.popups:
            win = self.daemon.instances.get(slot)
            if win is None:
                continue
            # Right-anchored: keep x = screen_right - width - margin.
            try:
                screen = win.get_screen()
                monitor = screen.get_primary_monitor()
                geo = screen.get_monitor_geometry(monitor)
            except Exception:
                continue
            w = win.get_allocated_width() or win.get_size()[0]
            h = win.get_allocated_height() or win.get_size()[1]
            x = geo.x + geo.width - w - theme.NOTIF_OFFSET_X
            win.move(x, geo.y + y)
            y += h + _GAP_PX

    # ── teardown / user dismissal ────────────────────────────────────
    def _hook_destroy(self, slot: str) -> None:
        """Connect to the popup window's destroy signal. User dismissal
        of a menu (mod+q, click outside the grab) routes through
        _on_user_dismiss → cancel command if declared, else close-all.
        Toast destroy always tears the cascade down."""
        win = self.daemon.instances.get(slot)
        if win is None:
            return
        win.connect("destroy", lambda _w, s=slot: self._on_user_dismiss(s))

    def _on_user_dismiss(self, slot: str) -> None:
        """Called when a popup is dismissed by the user OR by Escape
        (via Menu.on_cancel). If this slot has a cancel command, run
        it as the next stage; otherwise tear the whole cascade down."""
        if self._closing or slot in self._dismissed:
            return
        self._dismissed.add(slot)
        cancel = self._cancel_for.get(slot)
        if cancel is None:
            self._close_all()
            return
        self._run_step(cancel)

    def _close_all(self) -> None:
        if self._closing:
            return
        self._closing = True
        for slot in reversed(self.popups):
            self.daemon.kinds.pop(slot, None)
            self.daemon.close(slot)
        for path in self._manifest_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        # Drop the daemon's reference to us so a re-trigger gets a fresh
        # pipeline rather than reusing torn-down state.
        if getattr(self.daemon, "_pipeline", None) is self:
            self.daemon._pipeline = None
