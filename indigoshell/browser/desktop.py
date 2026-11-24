"""Flux: the browser's entry in the launcher, and installing it.

Pure Python (no Qt, no X), so `indigoshell browser --install` never
loads either.

`flux.desktop`, next to this file, is the entry as the repository keeps
it: no path, no user, `Exec=<CLI> browser %U`, the browser's window
class, a stock icon, and the web MIME types so that it can be made the
default browser.

`install()` writes it to `$XDG_DATA_HOME/applications/` (by default
`~/.local/share/applications/`) as `<APP>-browser.desktop`, the desktop
file name the browser gives Qt (`setDesktopFileName`), with three
things settled for this machine:

- Exec: the command that ran the install. The CLI's name when the CLI
  on PATH is what ran it; otherwise this interpreter and the script it
  ran (`main.py` in the repository, the way the window manager starts
  the shell), since a command of that name on PATH can be another,
  older install of the shell altogether.
- StartupWMClass from `naming`, should the package be renamed.
- No MimeType, unless asked for. With no explicit default browser set,
  an entry in the user's own directory that declares the web types is
  preferred over the system's (GIO always; xdg-mime once the user's
  desktop database lists it), so declaring them would quietly make it
  the default browser. Making it so is the user's decision:
  `install(mime_types=True)`, then `xdg-settings set
  default-web-browser <APP>-browser.desktop`.

The launcher re-reads the application directories when one of them
changed since its last open (services/apps.py), so the entry shows on
the next MOD+D without a reload.
"""

import os
import shutil
import subprocess
import sys

from indigoshell.core.naming import APP, CLI

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flux.desktop")
DESKTOP_ID = f"{APP}-browser.desktop"
WM_CLASS = f"{APP}-browser"                 # browser/qt/window.py's NAME

# Characters that make an Exec argument need quoting (Desktop Entry
# spec, "The Exec key"), and those escaped inside the quotes.
_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_ESCAPED_IN_QUOTES = frozenset('"`$\\')


def applications_dir(data_home: str | None = None) -> str:
    base = data_home or os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(base, "applications")


def exec_command() -> list[str]:
    """What Flux runs, before ` browser %U` (see the module docstring)."""
    script = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    on_path = shutil.which(CLI)
    if on_path and script and os.path.realpath(on_path) == script:
        return [CLI]
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main_py = os.path.join(repo, "main.py")
    if os.path.isfile(main_py):
        return [sys.executable, main_py]
    return [CLI]


def exec_arg(arg: str) -> str:
    """One argument as an Exec value spells it: quoted when it must be,
    then escaped once more for the key file's own string rules."""
    arg = arg.replace("%", "%%")
    if any(c in _RESERVED for c in arg):
        arg = '"' + "".join("\\" + c if c in _ESCAPED_IN_QUOTES else c
                            for c in arg) + '"'
    return arg.replace("\\", "\\\\")


def render(*, mime_types: bool = False, command: list[str] | None = None) -> str:
    """The entry as it is installed: the template with the lines above
    settled."""
    with open(TEMPLATE, encoding="utf-8") as f:
        lines = f.read().splitlines()
    argv = command if command is not None else exec_command()
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key == "Exec":
            line = "Exec=" + " ".join(exec_arg(a) for a in [*argv, "browser"]) + " %U"
        elif key == "StartupWMClass":
            line = f"StartupWMClass={WM_CLASS}"
        elif key == "MimeType" and not mime_types:
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def install(*, mime_types: bool = False, data_home: str | None = None) -> str:
    """Write the entry; returns its path. The desktop database is
    refreshed only for an entry that declares MIME types: it indexes
    nothing else."""
    folder = applications_dir(data_home)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, DESKTOP_ID)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render(mime_types=mime_types))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    if mime_types and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", folder], check=False, timeout=60)
    return path


def main() -> int:
    """`indigoshell browser --install`."""
    path = install()
    with open(path, encoding="utf-8") as f:
        exec_line = next((line for line in f if line.startswith("Exec=")), "").strip()
    print(f"Flux is in the launcher: {path}\n  {exec_line}\n"
          "  (the default browser is unchanged)")
    return 0
