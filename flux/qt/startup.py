"""What has to be settled before the QApplication exists: the platform,
Chromium's command line, and where the Widevine module is.

Pure Python. Qt reads its platform's variables when the application is
made and QtWebEngine reads `QTWEBENGINE_CHROMIUM_FLAGS` once, when the
first web engine object is made, so all of this runs first.

The platform (`settle_platform`, from __main__'s flags):

- On a Wayland session ($WAYLAND_DISPLAY set, as niri sets it for what
  it starts) Qt's Wayland platform, xcb only if that fails
  (`QT_QPA_PLATFORM=wayland;xcb`), whatever $XDG_SESSION_TYPE says:
  the shell then knows the window as a Wayland client (the app bar
  protocol's "On Wayland"); under Xwayland niri would know it only as
  Xwayland's. A platform the environment names stays.
- `--software-gl`: a window on a screen, drawn without the GPU: no
  OpenGL from the X server at all (`QT_XCB_GL_INTEGRATION=none`), Qt
  Quick's software scene graph (`QT_QUICK_BACKEND=software`) and
  Chromium's `--disable-gpu`. For a window shown over `ssh -X`, where
  the X server is another machine's and GLX either fails or is not
  offered. Qt finds by itself that MIT-SHM does not reach a remote X
  server and does without it. It leaves a variable the environment
  already set as it is, and turns off the page effects (they need a GPU
  scene graph).

Flux always runs on a screen: a bare compositor with nothing else on it
(cage, as on a machine that lends the agent its Flux) is one too.

Flags added to whatever the environment already has:

- `--blink-settings=preferredColorScheme=0`: pages see a dark scheme.
  Asking Qt for dark (styleHints) is ignored by platform themes that do
  not implement the request, qt6ct among them, so Blink is told directly.
- `--widevine-path=<libwidevinecdm.so>`: protected streams (Netflix and
  the like). QtWebEngine does not find a Widevine module by itself.
- `--disable-smooth-scrolling`: wheel and keys scroll at once, without
  an animation (the user's choice; the profile's ScrollAnimatorEnabled
  is off too). A page's own CSS `scroll-behavior: smooth` still glides.

Hardware video decoding is deliberately not enabled: on NVIDIA it
decodes, and the picture is black (Chromium's dmabuf frames have no way
into the compositor there). Software decoding is the accepted cost.

Widevine, in order:
1. `$<APP>_BROWSER_WIDEVINE`: a path to the .so, or "off".
2. `<data_dir>/widevine/libwidevinecdm.so`, our own copy.
3. Firefox's copy (gmp-widevinecdm/<version>/ in any profile under
   $XDG_CONFIG_HOME/mozilla/firefox or ~/.mozilla/firefox), the newest
   version, copied with its manifest.json into <data_dir>/widevine/ so
   it survives Firefox going away. Not updated after that: delete the
   copy to take a newer one from Firefox again.
4. None: start without it, and say so once.
"""

import logging
import os
import re
import shutil
from pathlib import Path

from indigoshell.core.naming import env
from indigoshell.core.paths import data_dir

log = logging.getLogger(__name__)

FLAGS_ENV = "QTWEBENGINE_CHROMIUM_FLAGS"
WIDEVINE_ENV = env("BROWSER_WIDEVINE")
WIDEVINE_SO = "libwidevinecdm.so"
DARK = "--blink-settings=preferredColorScheme=0"
NO_SMOOTH = "--disable-smooth-scrolling"
NO_GPU = "--disable-gpu"


def firefox_roots() -> list[Path]:
    """Where Firefox keeps its profiles: the XDG place newer versions
    use, then the old one."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [Path(config) / "mozilla" / "firefox",
            Path(os.path.expanduser("~/.mozilla/firefox"))]


def _version(text: str) -> tuple[int, ...]:
    """'4.10.3112.0' -> (4, 10, 3112, 0); anything else sorts first."""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else ()


def find_firefox_widevine(roots: list[Path]) -> Path | None:
    """The newest libwidevinecdm.so in any Firefox profile under `roots`."""
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for so in root.glob(f"*/gmp-widevinecdm/*/{WIDEVINE_SO}"):
            if so.is_file():
                found.append((_version(so.parent.name), so.stat().st_mtime, so))
    if not found:
        return None
    return max(found, key=lambda f: (f[0], f[1]))[2]


def copy_widevine(source: Path, dest_dir: Path) -> Path:
    """Copy the module and what sits beside it (manifest.json, LICENSE)
    into `dest_dir`, whole or not at all: built next to it, then renamed
    into place."""
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    building = dest_dir.with_name(f"{dest_dir.name}.part-{os.getpid()}")
    shutil.rmtree(building, ignore_errors=True)
    building.mkdir()
    try:
        for name in (WIDEVINE_SO, "manifest.json", "LICENSE"):
            if (source.parent / name).is_file():
                shutil.copy2(source.parent / name, building / name)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)     # no module in it, or we would not be here
        building.rename(dest_dir)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return dest_dir / WIDEVINE_SO


def resolve_widevine(own_dir: Path | None = None,
                     roots: list[Path] | None = None,
                     override: str | None = None) -> Path | None:
    """The libwidevinecdm.so to hand Chromium, copying Firefox's if that
    is the only one. None when there is none (or `override` turns it off)."""
    if override is not None:
        if override.strip().lower() in ("", "off", "0", "no", "none"):
            return None
        path = Path(override).expanduser()
        if path.is_file():
            return path
        log.warning("%s=%s: no such file; looking elsewhere", WIDEVINE_ENV, override)

    own_dir = own_dir or Path(data_dir()) / "widevine"
    own = own_dir / WIDEVINE_SO
    if own.is_file():
        return own

    source = find_firefox_widevine(firefox_roots() if roots is None else roots)
    if source is None:
        return None
    try:
        copied = copy_widevine(source, own_dir)
    except OSError as e:
        log.warning("could not copy Widevine from %s (%s); using it where it is",
                    source.parent, e)
        return source
    log.info("Widevine %s copied from Firefox into %s", source.parent.name, own_dir)
    return copied


def settle_platform(*, software: bool = False) -> None:
    """The platform's variables: Wayland's on a Wayland session, and
    those for `--software-gl` (the module docstring)."""
    if os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
    if software:
        os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")


def chromium_flags(existing: str, widevine: Path | None, *, software: bool = False) -> str:
    """`existing` with ours appended; a widevine path already there wins.
    `software`: Chromium without its GPU process too."""
    flags = existing.split()
    for flag in (DARK, NO_SMOOTH, *((NO_GPU,) if software else ())):
        if flag not in flags:
            flags.append(flag)
    if widevine is not None and not any(f.startswith("--widevine-path") for f in flags):
        flags.append(f"--widevine-path={widevine}")
    return " ".join(flags)


def apply_flags(*, software: bool = False) -> Path | None:
    """Settle Widevine and write the flags into the environment (with
    `software`, --software-gl's, no GPU). Returns the Widevine module in
    use."""
    existing = os.environ.get(FLAGS_ENV, "")
    widevine = None
    if "--widevine-path" not in existing:
        widevine = resolve_widevine(override=os.environ.get(WIDEVINE_ENV))
        if widevine is None:
            log.info("no Widevine module: protected streams (Netflix and the like) "
                     "will not play")
    os.environ[FLAGS_ENV] = chromium_flags(existing, widevine, software=software)
    return widevine
