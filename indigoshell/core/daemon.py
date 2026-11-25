"""The daemon: one asyncio loop, one thread, one Wayland connection.

Everything the shell waits on is an fd or a timer on this loop — Wayland
events, IPC clients, D-Bus (dbus-fast), subprocesses, and the per-window
frame clocks. There is no second main loop, no worker thread, and no
cross-thread queue; the only thread that ever runs is the optional
`--watch` file observer, and it only pokes the loop through
`call_soon_threadsafe`.

The shell draws on one canvas on the compositor's layer-shell
(backend/wayland.py, canvas.py), and every live window is a region of
it (window.py).
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from ..backend.wayland import DisplayError, connect
from ..canvas import warm_shaders
from ..window import Window
from . import log as logsetup
from .ipc import IPCServer
from .paths import config_dir
from .singleton import AlreadyRunning, acquire_lock, release_lock

log = logging.getLogger(__name__)

WARM_S = 2.5                    # after the start: contents drawn once ahead (launcher)

_DAEMON: "Daemon | None" = None


def get_daemon() -> "Daemon":
    """Module-level accessor for the running daemon."""
    if _DAEMON is None:
        raise RuntimeError("daemon is not running")
    return _DAEMON


def _restart_argv() -> list[str]:
    """Rebuild the command line that started us, for `reload`.

    `[sys.executable, *sys.argv]` is wrong under `python -m pkg.mod`:
    Python rewrites argv[0] to the module's *file* path, so re-execing it
    runs the file as a top-level script and every relative import in the
    package fails. `__main__.__spec__` is set only for the -m form, and
    tells us the module name to hand back to -m.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec is not None and spec.name:
        # `python -m pkg` resolves to pkg.__main__; re-exec the package.
        module = spec.parent if spec.name.endswith(".__main__") else spec.name
        return [sys.executable, "-m", module, *sys.argv[1:]]
    # Console script or plain `python script.py` — argv[0] is runnable.
    return [sys.executable, *sys.argv]


class Daemon:
    def __init__(self, config: dict, *, watch: bool = False) -> None:
        global _DAEMON
        if _DAEMON is not None:
            raise RuntimeError("daemon already constructed in this process")
        _DAEMON = self

        self.config = config
        self.canvas: Any = None          # backend.wayland.Canvas
        self.loop: asyncio.AbstractEventLoop | None = None

        # name -> WindowSpec (declarative) and name -> Window (live).
        self.menus: dict[str, Any] = dict(config.get("menus", {}))
        self.kinds: dict[str, Any] = {
            spec.name: spec for spec in config.get("windows", ())
        }
        self.instances: dict[str, Any] = {}
        # Windows playing their despawn animation: no longer addressable
        # by name, not yet destroyed.
        self._dying: set[Any] = set()
        # Closed windows kept alive for their next open — WindowSpec.keep_alive.
        self._parked: dict[str, Any] = {}

        self._watch = watch
        self._observer: Any = None
        self._watch_handle: asyncio.TimerHandle | None = None
        self._lock_fd: int | None = None
        self._ipc: IPCServer | None = None
        self._stopped: asyncio.Event | None = None
        self._restart = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def run(self) -> int:
        try:
            asyncio.run(self._main())
        except AlreadyRunning as e:
            log.error("%s", e)
            return 1
        except DisplayError as e:
            log.error("%s", e)
            return 1
        except KeyboardInterrupt:
            pass
        # Re-exec only once the loop is closed and every fd is released,
        # so the replacement process finds a free lock and socket.
        if self._restart:
            argv = _restart_argv()
            log.info("restarting: %s", " ".join(argv))
            sys.stderr.flush()
            try:
                os.execv(argv[0], argv)
            except OSError:
                log.exception("re-exec failed; daemon is exiting")
                return 1
        log.info("stopped")
        return 0

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        if logsetup.is_debug():
            # Surfaces slow callbacks — the thing most likely to stutter a
            # frame once the render loop exists.
            self.loop.set_debug(True)

        self._lock_fd = acquire_lock()
        try:
            self._start_canvas(connect(self.loop))

            for sig in (signal.SIGINT, signal.SIGTERM):
                self.loop.add_signal_handler(sig, self._on_signal, sig)

            self._ipc = IPCServer(self)
            await self._ipc.start()

            if self._watch:
                self._start_watcher()

            # What setting up queued for the compositor.
            self._flush()

            # Plugin startup hooks first: one of them may reconfigure the
            # screen, and the bar should be created for the result.
            await self._run_startup()
            self._flush()

            self._autostart()
            # Long-lived non-window services (notification daemon, ...).
            # After autostart so a service that opens windows finds the
            # registry live.
            for svc in self.config.get("services", ()):
                try:
                    svc.start()
                except Exception:
                    log.exception("service %s failed to start",
                                  type(svc).__name__)
            log.info("ready (pid %d)", os.getpid())
            self.loop.call_later(WARM_S, self._warm_contents)
            await self._stopped.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        log.debug("shutting down")
        for svc in self.config.get("services", ()):
            try:
                svc.stop()
            except Exception:
                log.exception("service %s failed to stop",
                              type(svc).__name__)
        if self._observer is not None:
            self._observer.stop()
            self._observer = None
        if self._watch_handle is not None:
            self._watch_handle.cancel()
            self._watch_handle = None
        # Shutdown skips despawn animations — destroy outright rather than
        # waiting on a clock whose loop is about to stop.
        for name in list(self.instances):
            try:
                self.instances.pop(name).destroy()
            except Exception:
                log.exception("failed to close %s", name)
        for win in list(self._dying):
            try:
                win.destroy()
            except Exception:
                log.exception("failed to destroy a dismissing window")
        self._dying.clear()
        for name in list(self._parked):
            try:
                self._parked.pop(name).destroy()
            except Exception:
                log.exception("failed to destroy parked %s", name)
        if self._ipc is not None:
            await self._ipc.stop()
            self._ipc = None
        if self.canvas is not None:
            if self.loop is not None:
                try:
                    self.loop.remove_reader(self.canvas.fd)
                except Exception:
                    pass
            self.canvas.close()
            self.canvas = None
        release_lock(self._lock_fd)
        self._lock_fd = None

    def _start_canvas(self, canvas) -> None:
        """The canvas made and sized, its events on the loop, and the
        effects' shaders warmed on its GPU."""
        canvas.attach_loop(self.loop)
        canvas.start()
        canvas.on_resize = self._on_screen_change
        self.canvas = canvas
        self.loop.add_reader(canvas.fd, self._on_readable)
        if canvas.scene.gpu is not None:
            import time
            t0 = time.perf_counter()
            try:
                n = warm_shaders(canvas.scene.gpu, list(self.kinds.values()))
            except Exception:
                log.exception("warming the GPU's shaders failed")
            else:
                log.info("canvas: %d effect chain(s) and the hologram warmed on the GPU "
                         "in %.0f ms", n, (time.perf_counter() - t0) * 1000)

    def _warm_contents(self) -> None:
        """A moment after the start, each kind whose content can (`warm`:
        the launcher's first page of app icons) draws once on the GPU,
        small and thrown away, so its first open does not pay for that
        (canvas.py `warm_shaders` does the effects' programs)."""
        gpu = self._gpu_scratch()
        if gpu is None:
            return
        import time
        for name, spec in self.kinds.items():
            warm = getattr(spec.content, "warm", None)
            if warm is None:
                continue
            t0 = time.perf_counter()
            try:
                n = warm(gpu)
            except Exception:
                log.exception("warming %s failed", name)
                continue
            log.info("%s warmed: %d row(s) drawn ahead in %.0f ms", name, n,
                     (time.perf_counter() - t0) * 1000)
        self._flush()

    def _gpu_scratch(self):
        """Something to draw on in the windows' GPU context, small and
        thrown away (canvas.py GpuScratch); None without the GPU."""
        return self.canvas.scene.gpu if self.canvas is not None else None

    def _flush(self) -> None:
        """Send the compositor what is queued."""
        if self.canvas is not None:
            self.canvas.flush()

    # A display switch arrives as a burst of resizes; wait for it to
    # settle before re-placing every window once.
    RELOCATE_DELAY = 0.3
    STARTUP_TIMEOUT = 15.0

    def _on_screen_change(self, _ev) -> None:
        if self.loop is None:
            return
        handle = getattr(self, "_relocate_handle", None)
        if handle is not None:
            handle.cancel()
        self._relocate_handle = self.loop.call_later(
            self.RELOCATE_DELAY, self._screen_settled)

    def _screen_settled(self) -> None:
        self._relocate_handle = None
        if getattr(self, "_screen_task", None) is not None:
            # Still handling the previous burst; run once more after it.
            self._screen_again = True
            return
        self._screen_task = self.loop.create_task(self._handle_screen_change())

    async def _handle_screen_change(self) -> None:
        """Plugins first — one may reconfigure outputs (the display
        fallback) — then re-place the windows on the result. A hook's
        own output change raises another burst, which finds nothing left
        to do."""
        try:
            hooks = list(self.config.get("screen", ()))
            async def one(hook):
                name = getattr(hook, "__qualname__", repr(hook))
                try:
                    await asyncio.wait_for(hook(), self.STARTUP_TIMEOUT)
                except asyncio.TimeoutError:
                    log.error("screen hook %s timed out", name)
                except Exception:
                    log.exception("screen hook %s failed", name)
            if hooks:
                await asyncio.gather(*(one(h) for h in hooks))
            self._relocate_windows()
        finally:
            self._screen_task = None
            if getattr(self, "_screen_again", False):
                self._screen_again = False
                self._screen_settled()

    def _relocate_windows(self) -> None:
        log.info("screen changed; re-placing windows")
        for win in list(self.instances.values()) + list(self._parked.values()):
            try:
                win.relocate()
            except Exception:
                log.exception("relocate failed for %s", win.spec.name)
        self._flush()

    async def _run_startup(self) -> None:
        hooks = list(self.config.get("startup", ()))
        if not hooks:
            return
        async def one(hook):
            name = getattr(hook, "__qualname__", repr(hook))
            try:
                await asyncio.wait_for(hook(), self.STARTUP_TIMEOUT)
            except asyncio.TimeoutError:
                log.error("startup hook %s timed out after %.0fs",
                          name, self.STARTUP_TIMEOUT)
            except Exception:
                log.exception("startup hook %s failed", name)
        await asyncio.gather(*(one(h) for h in hooks))

    def _autostart(self) -> None:
        for name, spec in self.kinds.items():
            if getattr(spec, "autostart", False):
                try:
                    self.open(name)
                except Exception:
                    log.exception("autostart failed for %s", name)

    def _on_signal(self, sig: int) -> None:
        log.info("caught %s", signal.Signals(sig).name)
        self.quit()

    def quit(self) -> None:
        if self._stopped is not None:
            self._stopped.set()

    def reload(self) -> None:
        self._restart = True
        self.quit()

    @property
    def restarting(self) -> bool:
        """Whether this stop is a reload (the process re-executes itself
        once the loop is closed): a service can tell its clients it will
        be back in a moment."""
        return self._restart

    # ── the compositor's events ─────────────────────────────────────────
    def _on_readable(self) -> None:
        try:
            self.canvas.on_readable()
        except DisplayError as e:
            log.error("%s", e)
            self.quit()

    # ── window registry ─────────────────────────────────────────────────
    def primary_monitor(self):
        """The monitor windows are placed on: the canvas's output."""
        return self.canvas.scene.primary_monitor()

    def _new_window(self, spec):
        return Window(self.canvas.scene, spec, self.loop)

    def open(self, name: str, params: dict | None = None, *,
             placement=None) -> str:
        """Open a window of kind `name`. `placement` (window.Placement)
        puts this open somewhere other than its anchor; an open window
        given one moves there."""
        spec = self.kinds.get(name)
        if spec is None:
            raise KeyError(f"unknown window kind: {name}")
        if spec.singleton and name in self.instances:
            if placement is not None:
                self.instances[name].place(placement)
            return name
        assert self.canvas is not None
        win = self._parked.pop(name, None)
        if win is not None and win.spec is spec:
            win.placement = placement
            win.unpark()
        else:
            if win is not None:
                # The kind was re-registered with a new spec since this
                # window was parked; it is stale.
                win.destroy()
            win = self._new_window(spec)
            win.placement = placement
            win.create()
        win.show()
        self.instances[name] = win
        self._flush()
        return name

    def prepare(self, name: str) -> None:
        """Make a keep_alive window of kind `name` and park it unshown, so
        that its first open costs what a reopen does (its surface, its
        content attached and laid out already)."""
        spec = self.kinds.get(name)
        if (spec is None or not spec.keep_alive or self.canvas is None
                or name in self.instances or name in self._parked):
            return
        win = self._new_window(spec)
        win.create()
        win.park()
        self._parked[name] = win
        self._flush()

    def close(self, name: str) -> bool:
        win = self.instances.pop(name, None)
        if win is None:
            return False
        # Dropped from `instances` immediately so the name is free to be
        # reopened, but kept alive until its despawn animation finishes.
        self._dying.add(win)
        win.dismiss(lambda w=win: self._reap(w))
        self._flush()
        return True

    def _reap(self, win) -> None:
        self._dying.discard(win)
        name = win.spec.name
        # Park rather than destroy when the kind wants it, its spec is
        # still the registered one, and nothing else already holds the
        # name — a close-then-reopen during the despawn opens a second
        # window, and the one finishing its despawn is the stale one.
        if (win.spec.keep_alive and self.kinds.get(name) is win.spec
                and name not in self.instances and name not in self._parked):
            win.park()
            self._parked[name] = win
        else:
            win.destroy()
        self._flush()

    def menu(self, name: str) -> None:
        """Open a plugin menu by name — see widgets/menu.py."""
        menu = self.menus.get(name)
        if menu is None:
            raise KeyError(f"unknown menu: {name}")
        from ..widgets.menu import open_menu
        assert self.loop is not None
        self.loop.create_task(open_menu(self, menu))

    def list_menus(self) -> list[str]:
        return list(self.menus)

    def toggle(self, name: str, params: dict | None = None) -> str:
        if name in self.instances:
            self.close(name)
            return "closed"
        self.open(name, params)
        return "opened"

    def dismiss_others(self, except_name: str | None = None) -> None:
        """Close every open window that dismisses on an outside click.

        A pointer grab with owner_events=True reports clicks on our *own*
        other windows to those windows directly, bypassing the grab — so a
        click on the bar is invisible to an open panel. The window that
        received it has to say so on the panel's behalf.
        """
        for name in list(self.instances):
            if name == except_name:
                continue
            spec = self.kinds.get(name)
            if getattr(spec, "dismiss_on_outside_click", False):
                self.close(name)

    def list_instances(self) -> list[str]:
        return list(self.instances)

    def list_kinds(self) -> list[str]:
        return list(self.kinds)

    # ── --watch ─────────────────────────────────────────────────────────
    def _start_watcher(self) -> None:
        """Re-exec on source or config change.

        watchdog runs its own thread; it never touches daemon state
        directly, it only schedules a debounced reload on the loop.
        """
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        pkg_dir = os.path.dirname(os.path.dirname(__file__))
        cwd = os.getcwd()
        user_cfg = config_dir()
        loop = self.loop
        assert loop is not None

        def _schedule(path: str) -> None:
            if self._watch_handle is not None:
                self._watch_handle.cancel()
            # Editors write in bursts (temp file, rename, chmod); coalesce
            # so one save is one restart.
            log.debug("changed: %s", path)
            self._watch_handle = loop.call_later(0.15, self.reload)

        def _match(event) -> str | None:
            if event.is_directory:
                return None
            paths = [str(event.src_path)]
            dest = getattr(event, "dest_path", None)
            if dest:
                paths.append(str(dest))
            for path in paths:
                if not path.endswith(".py"):
                    continue
                if "__pycache__" in path or os.path.basename(path).startswith("."):
                    continue
                return path
            return None

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                path = _match(event)
                if path:
                    loop.call_soon_threadsafe(_schedule, path)

            on_created = on_modified
            on_moved = on_modified

        observer = Observer()
        observer.schedule(Handler(), pkg_dir, recursive=True)
        observer.schedule(Handler(), cwd, recursive=False)
        if os.path.isdir(user_cfg):
            observer.schedule(Handler(), user_cfg, recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        log.info("watching %s for changes", pkg_dir)
