import argparse
import importlib.util
import logging
import os
import sys

from .core import log as logsetup
from .core.client import VERBS, main as client_main
from .core.daemon import Daemon
from .core.naming import APP, env
from .core.paths import config_dir, log_path


def _load_config():
    """User config wins; otherwise the shipped defaults.

    A config exports `WINDOWS` and optionally `SERVICES` — long-lived
    non-window services (the notification daemon) the Daemon starts once
    its loop is up.
    """
    path = os.path.join(config_dir(), "config.py")
    spec = (importlib.util.spec_from_file_location(f"{APP}_user_config", path)
            if os.path.isfile(path) else None)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        windows = getattr(module, "WINDOWS", None)
        if windows is not None:
            return (windows, getattr(module, "SERVICES", ()),
                    getattr(module, "MENUS", {}),
                    getattr(module, "STARTUP", ()),
                    getattr(module, "SCREEN", ()), path)
    from .config_default import MENUS, SCREEN, SERVICES, STARTUP, WINDOWS
    return WINDOWS, SERVICES, MENUS, STARTUP, SCREEN, "config_default"


def main() -> None:
    argv = sys.argv[1:]

    # Client mode: first positional arg matches a known verb.
    if argv and argv[0] in VERBS:
        client_main(argv)
        return

    parser = argparse.ArgumentParser(prog=APP)
    parser.add_argument("--watch", action="store_true",
                        help="restart on file changes")
    parser.add_argument("--log-level", choices=logsetup.LEVELS,
                        default=logsetup.default_level(),
                        help=f"default: info, or ${env('LOG_LEVEL')}")
    parser.add_argument("--log-file", nargs="?", const=log_path(), default=None,
                        metavar="PATH",
                        help=f"also log to a rotating file (default: {log_path()})")
    args = parser.parse_args(argv)

    logsetup.setup(args.log_level, args.log_file)

    windows, services, menus, startup, screen, source = _load_config()
    logging.getLogger(APP).debug(
        "loaded %d window spec(s), %d service(s) from %s",
        len(windows), len(services), source)
    sys.exit(Daemon({"windows": windows, "services": services,
                     "menus": menus, "startup": startup, "screen": screen},
                    watch=args.watch).run())


if __name__ == "__main__":
    main()
