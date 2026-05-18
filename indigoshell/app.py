import argparse
import importlib.util
import os
import sys
import threading

from .config_default import BAR as DEFAULT_BAR
from .core.client import VERBS, main as client_main
from .core.daemon import Daemon


def _load_user_config() -> dict | None:
    candidates = [
        os.path.expanduser("~/.config/indigoshell/config.py"),
        os.path.join(os.getcwd(), "config.py"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("indigoshell_user_config", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["indigoshell_user_config"] = module
            spec.loader.exec_module(module)
            return getattr(module, "BAR", None)
    return None


def _start_watcher():
    """Watch source + config files; re-exec process on change."""
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    pkg_dir = os.path.dirname(__file__)
    cwd = os.getcwd()
    user_cfg_dir = os.path.expanduser("~/.config/indigoshell")
    timer: threading.Timer | None = None

    def restart():
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _schedule_restart():
        nonlocal timer
        if timer:
            timer.cancel()
        timer = threading.Timer(0.15, restart)
        timer.daemon = True
        timer.start()

    def _matches(event):
        if event.is_directory:
            return False
        paths = [str(event.src_path)]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(str(dest))
        for path in paths:
            if not path.endswith(".py"):
                continue
            if "__pycache__" in path or os.path.basename(path).startswith("."):
                continue
            return True
        return False

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if _matches(event):
                _schedule_restart()

        def on_created(self, event):
            if _matches(event):
                _schedule_restart()

        def on_moved(self, event):
            if _matches(event):
                _schedule_restart()

    observer = Observer()
    observer.schedule(Handler(), pkg_dir, recursive=True)
    observer.schedule(Handler(), cwd, recursive=False)
    if os.path.isdir(user_cfg_dir):
        observer.schedule(Handler(), user_cfg_dir, recursive=False)
    observer.daemon = True
    observer.start()


def main():
    argv = sys.argv[1:]

    # Client mode: first positional arg matches a known verb.
    if argv and argv[0] in VERBS:
        client_main(argv)
        return

    # Daemon mode.
    parser = argparse.ArgumentParser(prog="indigoshell")
    parser.add_argument("--watch", action="store_true", help="restart on file changes")
    args = parser.parse_args(argv)

    if args.watch:
        _start_watcher()

    config = _load_user_config() or DEFAULT_BAR
    Daemon(config).run()


if __name__ == "__main__":
    main()
