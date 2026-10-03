"""Icon themes — the file behind an icon name, loaded for drawing.

Lookup follows the freedesktop Icon Theme spec: the user's theme, then
each theme it inherits, then hicolor, then the unthemed fallback dirs
(`/usr/share/pixmaps`). Within a theme a directory whose size matches
wins, otherwise the closest one does.

The spec describes that search as a stat() per candidate path, which for
fifty app icons is ~280ms of syscalls against the one event loop. Here
each theme is indexed once instead — one directory listing per
subdirectory, ~10ms for Adwaita + AdwaitaLegacy + hicolor — and every
lookup after that is a dict hit.

Loading never touches Skia's CPU rasteriser. An SVG is recorded into a
`skia.Picture` at the size it will be drawn and a PNG is decoded into an
image; both are drawn straight onto the window's own (GPU) canvas. The
first CPU raster draw in a process costs ~100ms of one-time setup, and
rasterising an icon offscreen would have paid it on the launcher's first
open.
"""

import configparser
import logging
import os
from dataclasses import dataclass

import skia

log = logging.getLogger(__name__)

FALLBACK_THEME = "hicolor"
# Skia decodes neither XPM nor the rest, so those files are invisible.
EXTENSIONS = (".png", ".svg")

_SAMPLING = skia.SamplingOptions(skia.FilterMode.kLinear,
                                 skia.MipmapMode.kLinear)


def base_dirs() -> list[str]:
    """Where themes live, in the spec's precedence order."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [os.path.expanduser("~/.icons"), os.path.join(data_home, "icons")]
    dirs += [os.path.join(d, "icons") for d in data_dirs.split(":") if d]
    dirs.append("/usr/share/pixmaps")
    seen: set[str] = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def gtk_theme() -> str:
    """The icon theme GTK apps use, which is the one to match."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for version in ("gtk-4.0", "gtk-3.0"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(os.path.join(config, version, "settings.ini"))
            name = parser.get("Settings", "gtk-icon-theme-name", fallback="")
        except configparser.Error:
            continue
        if name:
            return name.strip().strip('"')
    return FALLBACK_THEME


# ── themes ──────────────────────────────────────────────────────────────
def _read_index(path: str) -> dict[str, dict[str, str]]:
    """An index.theme's sections. Plain `key=value` lines under
    `[section]` headers — configparser's generality (interpolation,
    continuation lines, its regexes) made it ~40% of the indexing time
    on Adwaita's 3600-line index, for a format that needs none of it."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in "#;":
                continue
            if line[0] == "[" and line[-1] == "]":
                current = sections.setdefault(line[1:-1], {})
            elif current is not None:
                key, sep, value = line.partition("=")
                if sep:
                    current.setdefault(key.strip(), value.strip())
    return sections


@dataclass(frozen=True)
class _Dir:
    size: int
    scale: int = 1
    kind: str = "Threshold"         # Fixed | Scalable | Threshold
    min_size: int = 0
    max_size: int = 0
    threshold: int = 2

    def matches(self, size: int, scale: int) -> bool:
        if self.scale != scale:
            return False
        if self.kind == "Fixed":
            return self.size == size
        if self.kind == "Scalable":
            return self.min_size <= size <= self.max_size
        return self.size - self.threshold <= size <= self.size + self.threshold

    def distance(self, size: int, scale: int) -> int:
        want = size * scale
        if self.kind == "Fixed":
            return abs(self.size * self.scale - want)
        if self.kind == "Scalable":
            lo, hi = self.min_size, self.max_size
        else:
            lo, hi = self.size - self.threshold, self.size + self.threshold
        if want < lo * self.scale:
            return lo * self.scale - want
        if want > hi * self.scale:
            return want - hi * self.scale
        return 0


class _Theme:
    """One theme's directories and, per icon name, its candidate files in
    the order the theme lists its directories."""

    def __init__(self, name: str, bases: list[str]) -> None:
        self.name = name
        self.inherits: tuple[str, ...] = ()
        self.files: dict[str, list[tuple[_Dir, str]]] = {}
        # A theme is usually installed under one base dir of the seven,
        # and Adwaita lists ~700 subdirectories: probing each one under
        # every base was most of the indexing time.
        roots = [os.path.join(b, name) for b in bases
                 if os.path.isdir(os.path.join(b, name))]
        index = next((os.path.join(r, "index.theme") for r in roots
                      if os.path.isfile(os.path.join(r, "index.theme"))), None)
        if index is None:
            return
        try:
            sections = _read_index(index)
        except OSError:
            log.warning("unreadable icon theme index %s", index)
            return
        head = sections.get("Icon Theme", {})
        self.inherits = tuple(t.strip() for t in head.get(
            "Inherits", "").split(",") if t.strip())
        subdirs = [d.strip() for key in ("Directories", "ScaledDirectories")
                   for d in head.get(key, "").split(",") if d.strip()]
        for sub in dict.fromkeys(subdirs):
            info = self._dir(sections.get(sub))
            if info is None:
                continue
            for root in roots:
                self._scan(os.path.join(root, sub), info)

    @staticmethod
    def _dir(section: dict[str, str] | None) -> _Dir | None:
        if section is None:
            return None
        try:
            size = int(section.get("Size", "0"))
            return _Dir(size=size,
                        scale=int(section.get("Scale", "1")),
                        kind=section.get("Type", "Threshold"),
                        min_size=int(section.get("MinSize", str(size))),
                        max_size=int(section.get("MaxSize", str(size))),
                        threshold=int(section.get("Threshold", "2")))
        except ValueError:
            return None

    def _scan(self, path: str, info: _Dir) -> None:
        try:
            with os.scandir(path) as it:
                for entry in it:
                    stem, ext = os.path.splitext(entry.name)
                    if ext in EXTENSIONS:
                        self.files.setdefault(stem, []).append(
                            (info, entry.path))
        except OSError:
            pass                        # this base dir lacks the subdir

    def lookup(self, name: str, size: int, scale: int) -> str | None:
        candidates = self.files.get(name)
        if not candidates:
            return None
        for info, path in candidates:
            if info.matches(size, scale):
                return path
        return min(candidates, key=lambda c: c[0].distance(size, scale))[1]


# ── drawable icons ──────────────────────────────────────────────────────
class Icon:
    """Something to draw into a square. `symbolic` icons are monochrome
    templates meant to be tinted with the text colour."""

    def __init__(self, path: str, size: int,
                 picture: skia.Picture | None = None,
                 image: skia.Image | None = None) -> None:
        self.path = path
        self.size = size
        self.picture = picture
        self.image = image
        self.symbolic = "-symbolic" in os.path.basename(path)

    def draw(self, canvas: skia.Canvas, rect: skia.Rect,
             tint: int | None = None) -> None:
        paint = None
        if tint is not None and self.symbolic:
            paint = skia.Paint()
            paint.setColorFilter(skia.ColorFilters.Blend(
                tint, skia.BlendMode.kSrcIn))
        if self.picture is not None:
            scale = rect.width() / self.size
            matrix = skia.Matrix.Translate(rect.left(), rect.top())
            matrix.preScale(scale, scale)
            canvas.drawPicture(self.picture, matrix, paint)
        elif self.image is not None:
            canvas.drawImageRect(self.image, rect, _SAMPLING, paint)


def _load(path: str, size: int) -> Icon | None:
    if path.endswith(".svg"):
        dom = skia.SVGDOM.MakeFromStream(skia.FILEStream.Make(path))
        if dom is None:
            return None
        recorder = skia.PictureRecorder()
        canvas = recorder.beginRecording(skia.Rect.MakeWH(size, size))
        intrinsic = dom.containerSize()
        if intrinsic.isEmpty():
            # viewBox only: the root fits itself to the container.
            dom.setContainerSize(skia.Size(size, size))
        else:
            canvas.scale(size / intrinsic.width(), size / intrinsic.height())
        dom.render(canvas)
        return Icon(path, size, picture=recorder.finishRecordingAsPicture())
    image = skia.Image.MakeFromEncoded(skia.Data.MakeFromFileName(path))
    if image is None:
        return None
    # Decoded now, so a first paint is not where a PNG gets inflated.
    return Icon(path, size, image=image.makeRasterImage())


# ── the service ─────────────────────────────────────────────────────────
class Icons:
    """Resolves and loads icons, caching both answers — including "there
    is no such icon", which is most of what a launcher's misses are."""

    def __init__(self, theme: str | None = None) -> None:
        self._theme_name = theme
        self._themes: dict[str, _Theme] = {}
        self._chain: list[_Theme] | None = None
        self._bases: list[str] = []
        self._cache: dict[tuple[str, int, int], Icon | None] = {}

    def refresh(self) -> None:
        """Forget every index and loaded icon — after apps are installed
        or removed, whose icons came or went with them."""
        self._themes.clear()
        self._chain = None
        self._cache.clear()

    def _theme(self, name: str) -> _Theme:
        theme = self._themes.get(name)
        if theme is None:
            theme = self._themes[name] = _Theme(name, self._bases)
        return theme

    def _themes_in_order(self) -> list[_Theme]:
        if self._chain is None:
            self._bases = base_dirs()
            chain: list[_Theme] = []
            seen: set[str] = set()

            def visit(name: str) -> None:
                if name in seen:
                    return
                seen.add(name)
                theme = self._theme(name)
                chain.append(theme)
                for parent in theme.inherits:
                    visit(parent)

            visit(self._theme_name or gtk_theme())
            visit(FALLBACK_THEME)
            self._chain = chain
            log.debug("icon themes: %s", " > ".join(t.name for t in chain))
        return self._chain

    def find(self, name: str, size: int, scale: int = 1) -> str | None:
        """Path of the best file for `name`, or None."""
        if not name:
            return None
        if os.path.isabs(name):
            return name if os.path.isfile(name) else None
        stem, ext = os.path.splitext(name)
        if ext in (*EXTENSIONS, ".xpm"):
            name = stem         # "foo.png": against the spec, but common
        for theme in self._themes_in_order():
            path = theme.lookup(name, size, scale)
            if path is not None:
                return path
        for base in self._bases:
            for ext in EXTENSIONS:
                path = os.path.join(base, name + ext)
                if os.path.isfile(path):
                    return path
        return None

    def get(self, name: str, size: int, scale: int = 1) -> Icon | None:
        key = (name, size, scale)
        if key in self._cache:
            return self._cache[key]
        icon = None
        path = self.find(name, size, scale)
        if path is not None:
            try:
                icon = _load(path, size * scale)
            except Exception:
                log.warning("cannot load icon %s", path, exc_info=True)
        self._cache[key] = icon
        return icon
