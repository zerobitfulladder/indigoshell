"""Desktop entries — the `.desktop` files every installed app ships.

The parts of the freedesktop Desktop Entry spec a launcher needs:
reading the `[Desktop Entry]` group with locale-aware names, deciding
whether an entry should be shown at all, and turning its `Exec` line
into an argv. Pure functions over files; finding the files and keeping
the list current is `services/apps.py`.

https://specifications.freedesktop.org/desktop-entry-spec/latest/
"""

import logging
import os
import shutil
from dataclasses import dataclass

log = logging.getLogger(__name__)

_GROUP = "Desktop Entry"

# The general escapes for string values. `\;` only means something
# inside a list, and `_split_list` handles it before these run.
_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}

# Field codes that stand for files or URLs. A launcher opens an app with
# none, so an argument that is only one of these disappears. The last
# four are deprecated and expand to nothing everywhere.
_FILE_CODES = frozenset({"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N",
                         "%v", "%m"})

# Inside a quoted Exec argument these four must be backslash-escaped.
_QUOTED_ESCAPES = frozenset('"`$\\')


@dataclass(frozen=True)
class DesktopEntry:
    id: str                         # desktop file ID, "org.gnome.Evince.desktop"
    path: str
    name: str
    generic_name: str = ""
    comment: str = ""
    keywords: tuple[str, ...] = ()
    icon: str = ""
    exec: str = ""                  # string-unescaped, still quoted
    workdir: str = ""               # the `Path` key
    terminal: bool = False
    no_display: bool = False
    hidden: bool = False
    only_show_in: tuple[str, ...] = ()
    not_show_in: tuple[str, ...] = ()
    try_exec: str = ""


# ── reading ─────────────────────────────────────────────────────────────
def locales() -> tuple[str, ...]:
    """Locale keys to try for `Name[...]`, most specific first.

    `lang_COUNTRY.ENCODING@MODIFIER` is tried as lang_COUNTRY@MODIFIER,
    lang_COUNTRY, lang@MODIFIER, lang — the spec's order, with the
    encoding never part of a key.
    """
    raw = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
           or os.environ.get("LANG") or "")
    if raw in ("", "C", "POSIX") or raw.startswith("C."):
        return ()
    lang, _, modifier = raw.partition("@")
    lang = lang.split(".", 1)[0]
    base, _, country = lang.partition("_")
    out = []
    if country and modifier:
        out.append(f"{base}_{country}@{modifier}")
    if country:
        out.append(f"{base}_{country}")
    if modifier:
        out.append(f"{base}@{modifier}")
    out.append(base)
    return tuple(out)


def _unescape(value: str) -> str:
    out = []
    chars = iter(value)
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
        else:
            out.append(ch)
    return "".join(out)


def _split_list(value: str) -> tuple[str, ...]:
    """A `;`-separated list. `\\;` is a literal semicolon in an item."""
    items: list[str] = []
    current: list[str] = []
    chars = iter(value)
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            current.append(";" if nxt == ";" else "\\" + nxt)
        elif ch == ";":
            items.append(_unescape("".join(current)))
            current = []
        else:
            current.append(ch)
    if current:
        items.append(_unescape("".join(current)))
    return tuple(item for item in items if item)


def _read_group(path: str) -> dict[str, str] | None:
    """The `[Desktop Entry]` group's raw key/values; None if absent."""
    values: dict[str, str] = {}
    group = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                if group == _GROUP:
                    break               # actions and the rest: not needed
                group = line[1:line.find("]")] if "]" in line else None
                continue
            if group != _GROUP:
                continue
            key, sep, value = line.partition("=")
            if sep:
                # First occurrence wins, as in GLib's key file.
                values.setdefault(key.strip(), value.strip())
    return values if group == _GROUP else None


def parse(path: str, desktop_id: str,
          langs: tuple[str, ...] = ()) -> DesktopEntry | None:
    """Read one file. None if it cannot be read, is not an Application,
    or has nothing to launch."""
    try:
        values = _read_group(path)
    except OSError:
        log.debug("cannot read %s", path, exc_info=True)
        return None
    if values is None or values.get("Type") != "Application":
        return None

    def localized(key: str) -> str:
        for lang in langs:
            hit = values.get(f"{key}[{lang}]")
            if hit is not None:
                return _unescape(hit)
        return _unescape(values.get(key, ""))

    def flag(key: str) -> bool:
        return values.get(key, "").lower() == "true"

    name = localized("Name")
    exec_line = _unescape(values.get("Exec", ""))
    if not name or not exec_line:
        return None
    keywords = next((values[f"Keywords[{lang}]"] for lang in langs
                     if f"Keywords[{lang}]" in values),
                    values.get("Keywords", ""))
    return DesktopEntry(
        id=desktop_id,
        path=path,
        name=name,
        generic_name=localized("GenericName"),
        comment=localized("Comment"),
        keywords=_split_list(keywords),
        icon=localized("Icon"),
        exec=exec_line,
        workdir=_unescape(values.get("Path", "")),
        terminal=flag("Terminal"),
        no_display=flag("NoDisplay"),
        hidden=flag("Hidden"),
        only_show_in=_split_list(values.get("OnlyShowIn", "")),
        not_show_in=_split_list(values.get("NotShowIn", "")),
        try_exec=_unescape(values.get("TryExec", "")),
    )


# ── visibility ──────────────────────────────────────────────────────────
def current_desktops() -> frozenset[str]:
    return frozenset(d for d in os.environ.get("XDG_CURRENT_DESKTOP", "")
                     .split(":") if d)


def _installed(program: str) -> bool:
    if os.path.isabs(program):
        return os.access(program, os.X_OK)
    return shutil.which(program) is not None


def visible(entry: DesktopEntry, desktops: frozenset[str]) -> bool:
    """Whether a launcher should list the entry.

    `Hidden` means the user deleted it — a copy in their own data dir
    with Hidden=true masks the system one, which is why this is decided
    *after* precedence picks one file per ID. Like GLib, an entry whose
    program is not installed is left out, judged by TryExec when given
    and by Exec's own program either way.
    """
    if entry.hidden or entry.no_display:
        return False
    if entry.only_show_in and not desktops.intersection(entry.only_show_in):
        return False
    if desktops.intersection(entry.not_show_in):
        return False
    if entry.try_exec and not _installed(entry.try_exec):
        return False
    try:
        program = split_exec(entry.exec)[0]
    except (ValueError, IndexError):
        return False
    return _installed(program)


# ── Exec ────────────────────────────────────────────────────────────────
def split_exec(line: str) -> list[str]:
    """Split an (already string-unescaped) Exec value into arguments.

    Arguments are separated by spaces and may be double-quoted; inside
    quotes `"`, backtick, `$` and backslash are backslash-escaped. Not a
    shell: there is no other expansion, and nothing here invokes one.
    """
    args: list[str] = []
    current: list[str] = []
    quoted = started = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if quoted:
            if ch == "\\" and i + 1 < n and line[i + 1] in _QUOTED_ESCAPES:
                current.append(line[i + 1])
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                current.append(ch)
        elif ch == '"':
            quoted = started = True
        elif ch in " \t":
            if started or current:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
        i += 1
    if quoted:
        raise ValueError(f"unterminated quote in Exec: {line!r}")
    if started or current:
        args.append("".join(current))
    return args


def _expand(arg: str, entry: DesktopEntry) -> str:
    out = []
    i, n = 0, len(arg)
    while i < n:
        ch = arg[i]
        if ch == "%" and i + 1 < n:
            code = arg[i + 1]
            if code == "%":
                out.append("%")
            elif code == "c":
                out.append(entry.name)
            elif code == "k":
                out.append(entry.path)
            # Every other code — files, URLs, deprecated — is empty.
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def command(entry: DesktopEntry,
            terminal: tuple[str, ...] = ("kitty",)) -> list[str]:
    """The argv that launches `entry` with no files or URLs.

    `%i` becomes `--icon <Icon>` (or nothing when there is no icon); a
    terminal app is wrapped in `terminal`, which must take the program
    to run as its trailing arguments.
    """
    argv: list[str] = []
    for arg in split_exec(entry.exec):
        if arg in _FILE_CODES:
            continue
        if arg == "%i":
            if entry.icon:
                argv += ["--icon", entry.icon]
            continue
        argv.append(_expand(arg, entry))
    if not argv:
        raise ValueError(f"empty Exec in {entry.path}")
    if entry.terminal:
        argv = [*terminal, *argv]
    return argv
