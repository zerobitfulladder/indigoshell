"""Plugin discovery.

Plugins are Python modules exporting `MENUS`, `STARTUP` or `SCREEN`,
loaded from two places, later winning:

  1. `indigoshell/plugins/` — the shipped set.
  2. `~/.config/indigoshell/plugins/` — the user's.

A user file whose module name matches a shipped one *replaces* it, so
overriding the audio plugin means copying it and editing, not patching
around it.

Every plugin is loaded inside its own try/except. Plugins run in-process
— that is the trade for not isolating them as subprocesses — so the one
thing the loader must guarantee is that a plugin which fails to import
costs its own menus and nothing else. A syntax error in a half-written
plugin should not take the bar down with it.
"""

import importlib.util
import logging
import os
import sys

from .naming import APP
from .paths import config_dir

log = logging.getLogger(__name__)

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")


def user_dir() -> str:
    return os.path.join(config_dir(), "plugins")


def _module_files(directory: str) -> dict[str, str]:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {}
    return {n[:-3]: os.path.join(directory, n) for n in names
            if n.endswith(".py") and not n.startswith("_")}


def _load(stem: str, path: str):
    # Loaded *into the package's namespace* (`indigoshell.plugins.audio`)
    # even when the file lives in the user's config dir. Python resolves
    # relative imports by module name, not by file location, so this is
    # what lets a plugin say `from ..plugin import Slider` — and it means
    # a shipped plugin copied to `~/.config/…/plugins/` to be edited
    # keeps working unchanged, which is the whole point of the override.
    # It also keeps the app name out of plugin source, so a package
    # rename doesn't break them.
    spec = importlib.util.spec_from_file_location(
        f"{APP}.plugins.{stem}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Plugins:
    """What the plugins contribute: menus, and startup hooks.

    A startup hook is an async callable in a module's `STARTUP` list. The
    daemon awaits them once, before it creates any window, so a hook
    that reconfigures the screen (restoring the chosen display) runs
    before the bar is placed on it.
    """

    def __init__(self) -> None:
        self.menus: list = []
        self.startup: list = []
        # `SCREEN` hooks: awaited after the screen or an output's
        # connection changes — an unplugged monitor, a plugged one.
        self.screen: list = []


def discover() -> Plugins:
    """Every plugin's menus and startup hooks, shipped ones first."""
    files: dict[str, str] = {}
    files.update(_module_files(BUILTIN_DIR))
    overrides = _module_files(user_dir())
    for stem in overrides:
        if stem in files:
            log.info("user plugin %r overrides the shipped one", stem)
    files.update(overrides)

    found = Plugins()
    for stem, path in files.items():
        try:
            module = _load(stem, path)
        except Exception:
            log.exception("plugin %r failed to load, skipping", stem)
            continue
        menus = list(getattr(module, "MENUS", None) or ())
        hooks = list(getattr(module, "STARTUP", None) or ())
        screen = list(getattr(module, "SCREEN", None) or ())
        if not menus and not hooks and not screen:
            log.warning("plugin %r exports no MENUS, STARTUP or SCREEN", stem)
            continue
        found.menus.extend(menus)
        found.startup.extend(hooks)
        found.screen.extend(screen)
        log.debug("plugin %r: %d menu(s), %d startup hook(s) from %s",
                  stem, len(menus), len(hooks), path)
    return found


def by_name(menus) -> dict:
    """Menus keyed by name. A later plugin's menu replaces an earlier one
    of the same name, so a user plugin can redefine a shipped menu."""
    table: dict = {}
    for menu in menus:
        if menu.name in table:
            log.info("menu %r redefined by a later plugin", menu.name)
        table[menu.name] = menu
    return table
