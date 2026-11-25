"""Flux keeps to its own: the import rule in flux/__init__.py.

    .venv/bin/python tests/flux_imports.py

  1. every module under flux/ (flux/qt too) imports of the shell only
     its pure modules: indigoshell.core's naming, paths, log, hands and
     persona, applink.protocol and applink.bar, fuzzy and services.proc
     (read from the source, so flux/qt is checked without loading
     PySide6);
  2. Flux's pure modules import with xcffib, skia, dbus_fast, psutil and
     PySide6 blocked (as on a machine with none of them), and load no
     other shell module than those;
  3. the shell never imports flux.qt.

No display, no processes; a second or so.
"""

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWED = {
    "indigoshell", "indigoshell.core", "indigoshell.core.naming", "indigoshell.core.paths",
    "indigoshell.core.log", "indigoshell.core.hands", "indigoshell.core.persona",
    "indigoshell.applink",
    "indigoshell.applink.protocol", "indigoshell.applink.bar", "indigoshell.fuzzy",
    "indigoshell.services",
    "indigoshell.services.proc",
}
BLOCKED = ("xcffib", "skia", "dbus_fast", "psutil", "PySide6")
PURE = ("flux", "flux.protocol", "flux.places", "flux.profiles", "flux.uploads",
        "flux.desktop", "flux.control", "flux.hands", "flux.bar")

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed += not ok


def imported(path: Path, package: str) -> set[str]:
    """The absolute names a module imports, relative ones resolved."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[:len(base) - node.level + 1]
                name = ".".join(base + ([node.module] if node.module else []))
            else:
                name = node.module or ""
            out.add(name)
            out.update(f"{name}.{a.name}" for a in node.names)
    return out


def module_of(path: Path) -> tuple[str, str]:
    """(module name, its package) for a file under the repository."""
    parts = list(path.relative_to(REPO).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


def shell_names(names: set[str]) -> set[str]:
    """The shell's modules among `names` (a `from x import y` lists both
    x and x.y; y may be a name, not a module: keep what is a module)."""
    out = set()
    for name in names:
        if name.split(".")[0] != "indigoshell":
            continue
        path = REPO / name.replace(".", "/")
        if path.with_suffix(".py").exists() or (path / "__init__.py").exists() or path.is_dir():
            out.add(name)
    return out


print("1. what flux/ imports of the shell (source)")
bad = {}
for path in sorted((REPO / "flux").rglob("*.py")):
    name, package = module_of(path)
    extra = shell_names(imported(path, package)) - ALLOWED
    if extra:
        bad[name] = sorted(extra)
check("only the shell's pure modules, flux/qt included", not bad, f"{bad}")

print("2. Flux's pure modules with the shell's dependencies blocked")
probe = f"""
import sys
BLOCKED = {BLOCKED!r}
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"blocked: {{name}}")
        return None
sys.meta_path.insert(0, Block())
import importlib
for m in {PURE!r}:
    importlib.import_module(m)
from flux.hands import browser_hands
hands = browser_hands()
print(len(hands))
print(" ".join(sorted(m for m in sys.modules if m.split(".")[0] == "indigoshell")))
"""
run = subprocess.run([sys.executable, "-B", "-c", probe], cwd=REPO, capture_output=True,
                     text=True, timeout=30)
check("they import, and Flux's hands are made", run.returncode == 0,
      run.stderr.strip().splitlines()[-1] if run.stderr.strip() else "")
if run.returncode == 0:
    count, loaded = run.stdout.strip().splitlines()
    check("... all 24 hands", count == "24", count)
    extra = set(loaded.split()) - ALLOWED
    check("... loading no other shell module", not extra, f"{sorted(extra)}")

print("3. the shell never imports flux.qt")
bad = {}
for path in sorted((REPO / "indigoshell").rglob("*.py")):
    name, package = module_of(path)
    qt = sorted(n for n in imported(path, package) if n == "flux.qt" or n.startswith("flux.qt."))
    if qt and name != "indigoshell.app":         # the `browser` verb runs it, lazily
        bad[name] = qt
check("no module of the shell imports flux.qt (but the `browser` verb's)", not bad, f"{bad}")

print(f"\n{'all ok' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
