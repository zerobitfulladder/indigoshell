"""Installed applications: the catalog, searching it, launching from it.

The catalog is every visible desktop entry under the XDG data dirs, one
per desktop file ID with the user's own copy winning. It is re-read only
when something changed: `refresh()` stats the directories and files it
read last time (~100 stats, well under a millisecond) and rescans only
on a difference, so a launcher can call it on every open.

Search is fuzzy (see `fuzzy.py`) over the name first, and the generic
name, keywords and program name second; launch history then lifts the
apps actually used. History lives in the shared state file, so it
survives reloads.
"""

import logging
import math
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import fuzzy
from ..core import state
from . import desktop_entry, proc
from .desktop_entry import DesktopEntry

log = logging.getLogger(__name__)

# How much each signal counts, in fzy's units: a consecutive character
# is worth 1.0 and a word-start 0.8, so a good name match lands at 3-6.
PREFIX_BONUS = 2.0          # the query starts the name
WORD_BONUS = 1.0            # the query starts one of the name's words
# A match on the generic name, keywords or program is a hint, not a
# hit: "browser" should find Firefox, but below anything *called* that.
EXTRA_WEIGHT = 0.5
# History is a tiebreak that can grow into a preference: log-scaled, so
# the tenth launch matters less than the second, and capped by how far
# a weak match can climb.
HISTORY_WEIGHT = 0.5
HISTORY_KEY = "launcher.history"
HISTORY_MAX = 100           # entries kept
HISTORY_AGING = 1000.0      # total count past which every count decays


def application_dirs() -> list[str]:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [data_home, *(d for d in data_dirs.split(":") if d)]
    seen: set[str] = set()
    return [os.path.join(d, "applications") for d in dirs
            if not (d in seen or seen.add(d))]


@dataclass(eq=False)
class App:
    entry: DesktopEntry
    name: fuzzy.Text = field(init=False)
    extra: tuple[fuzzy.Text, ...] = field(init=False)

    def __post_init__(self) -> None:
        e = self.entry
        self.name = fuzzy.Text(e.name)
        try:
            program = os.path.basename(desktop_entry.split_exec(e.exec)[0])
        except (ValueError, IndexError):
            program = ""
        self.extra = tuple(fuzzy.Text(t) for t in
                           (e.generic_name, *e.keywords, program) if t)


class Result(NamedTuple):
    app: App
    score: float
    positions: tuple[int, ...]      # matched indices in the name; () if
                                    # it matched on another field


# ── history ─────────────────────────────────────────────────────────────
class History:
    """How often and how recently each app was launched.

    Frecency in the zoxide shape: a launch count, weighted by how long
    ago the last launch was, with every count decayed once the total
    passes `HISTORY_AGING` so old habits fade instead of pinning the top.
    """

    def __init__(self) -> None:
        raw = state.get(HISTORY_KEY, {})
        self._entries: dict[str, tuple[float, float]] = {}
        if isinstance(raw, dict):
            for app_id, value in raw.items():
                try:
                    count, last = float(value[0]), float(value[1])
                except (TypeError, ValueError, IndexError):
                    continue
                self._entries[app_id] = (count, last)

    def frecency(self, app_id: str, now: float) -> float:
        hit = self._entries.get(app_id)
        if hit is None:
            return 0.0
        count, last = hit
        age = now - last
        if age < 3600:
            weight = 4.0
        elif age < 86400:
            weight = 2.0
        elif age < 7 * 86400:
            weight = 1.0
        else:
            weight = 0.5
        return count * weight

    def boost(self, app_id: str, now: float) -> float:
        return HISTORY_WEIGHT * math.log1p(self.frecency(app_id, now))

    def record(self, app_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        count, _ = self._entries.get(app_id, (0.0, now))
        self._entries[app_id] = (count + 1.0, now)
        if sum(c for c, _ in self._entries.values()) > HISTORY_AGING:
            self._entries = {k: (c * 0.9, t) for k, (c, t)
                             in self._entries.items() if c * 0.9 >= 1.0}
        if len(self._entries) > HISTORY_MAX:
            keep = sorted(self._entries, reverse=True,
                          key=lambda k: self.frecency(k, now))[:HISTORY_MAX]
            self._entries = {k: self._entries[k] for k in keep}
        # A fresh dict every time: `state.set` skips the write when the
        # value equals what it holds, and a dict mutated in place would.
        state.set(HISTORY_KEY, {k: [round(c, 3), round(t)]
                                for k, (c, t) in self._entries.items()})


# ── catalog ─────────────────────────────────────────────────────────────
class Catalog:
    def __init__(self, *, terminal: tuple[str, ...] = ("kitty",)) -> None:
        self.terminal = tuple(terminal)
        self.history = History()
        self.apps: list[App] = []
        # Every directory and file the last scan read, with its mtime;
        # None for an application dir that did not exist.
        self._stamps: dict[str, int | None] = {}

    # ── scanning ────────────────────────────────────────────────────────
    def refresh(self) -> bool:
        """Rescan if anything changed since the last scan. True if the
        catalog was rebuilt."""
        if self._stamps and not self._changed():
            return False
        self._scan()
        return True

    def _changed(self) -> bool:
        for path, stamp in self._stamps.items():
            try:
                now = os.stat(path).st_mtime_ns
            except OSError:
                now = None
            if now != stamp:
                return True
        return False

    def _scan(self) -> None:
        t0 = time.perf_counter()
        stamps: dict[str, int | None] = {}
        found: dict[str, str] = {}          # desktop ID -> path, first wins
        for root in application_dirs():
            if not os.path.isdir(root):
                stamps[root] = None
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                stamps[dirpath] = _mtime(dirpath)
                for name in sorted(filenames):
                    if not name.endswith(".desktop"):
                        continue
                    path = os.path.join(dirpath, name)
                    stamps[path] = _mtime(path)
                    # The ID is the path below the applications dir with
                    # "/" as "-": kde4/foo.desktop is kde4-foo.desktop.
                    rel = os.path.relpath(path, root)
                    found.setdefault(rel.replace(os.sep, "-"), path)

        langs = desktop_entry.locales()
        desktops = desktop_entry.current_desktops()
        apps = []
        for desktop_id, path in found.items():
            entry = desktop_entry.parse(path, desktop_id, langs)
            if entry is not None and desktop_entry.visible(entry, desktops):
                apps.append(App(entry))
        apps.sort(key=lambda a: a.entry.name.casefold())
        self.apps, self._stamps = apps, stamps
        log.debug("catalog: %d apps from %d desktop files in %.1fms",
                  len(apps), len(found), (time.perf_counter() - t0) * 1000)

    # ── search ──────────────────────────────────────────────────────────
    def search(self, query: str) -> list[Result]:
        """Matching apps, best first. An empty query lists every app,
        most used first, then by name."""
        now = time.time()
        folded, _ = fuzzy.fold(query.strip())
        if not folded:
            ranked = [Result(app, self.history.boost(app.entry.id, now), ())
                      for app in self.apps]
            ranked.sort(key=lambda r: -r.score)     # stable: names stay sorted
            return ranked

        results = []
        for app in self.apps:
            hit = fuzzy.match(folded, app.name)
            if hit is not None:
                score = hit.score + _start_bonus(folded, app.name)
                positions = hit.positions
            else:
                extra = [m for m in (fuzzy.match(folded, t) for t in app.extra)
                         if m is not None]
                if not extra:
                    continue
                # An exact keyword is worth a run of consecutive matches,
                # not fzy's EXACT: that is for typing an app's own name,
                # and halved it would still bury every app *named* for
                # the word under every app merely tagged with it.
                best = min(max(m.score for m in extra),
                           len(folded) * fuzzy.MATCH_CONSECUTIVE)
                score = best * EXTRA_WEIGHT
                positions = ()
            score += self.history.boost(app.entry.id, now)
            results.append(Result(app, score, positions))
        results.sort(key=lambda r: -r.score)
        return results

    # ── launching ───────────────────────────────────────────────────────
    def launch(self, app: App) -> None:
        entry = app.entry
        argv = desktop_entry.command(entry, self.terminal)
        log.info("launching %s: %s", entry.id, shlex.join(argv))
        self.history.record(entry.id)
        proc.launch(argv, app_id=entry.id.removesuffix(".desktop"),
                    cwd=_workdir(entry.workdir))

    def run(self, command_line: str) -> None:
        """Run a typed command line — split like a shell would, but
        never handed to one."""
        argv = shlex.split(command_line)
        if argv:
            log.info("running %s", shlex.join(argv))
            proc.launch(argv, cwd=_workdir(""))


def _start_bonus(query: str, name: fuzzy.Text) -> float:
    if name.folded.startswith(query):
        return PREFIX_BONUS
    # Word starts are where fzy's bonus table found a boundary.
    for k, bonus in enumerate(name.bonus):
        if k and bonus and name.folded.startswith(query, k):
            return WORD_BONUS
    return 0.0


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _workdir(path: str) -> str:
    # Apps start in their own `Path`, else home — never in whatever
    # directory the shell happened to be started from.
    if path and os.path.isdir(path):
        return path
    return os.path.expanduser("~")
