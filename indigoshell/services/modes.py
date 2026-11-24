"""Launcher modes: lists the launcher shows instead of the apps.

MOD+D opens the launcher (widgets/launcher.py) on the installed apps. A
mode replaces that list for one open: Flux's addresses
(services/addresses.py), Flux's actions (a `Choices`). Picking a row
may close the launcher, refuse (the row flashes and the launcher stays),
or bring the next step, another mode, in the same window: Escape and
Ctrl+H go back a step, and close it from the first.

A mode is an object with

    placeholder         the query's grey text while it is empty
    text                the query it starts with ("" for none)
    select_text         that query starts selected: the first key typed
                        replaces it
    search(query)       -> (entries, index): the rows for the query, best
                        first, and the one the selection starts on
    pick(entry, query)  Return on `entry`, or with entry None, Shift+Return:
                        the typed text itself. Returns True (done: the
                        launcher closes), False (refused: it flashes and
                        stays) or a mode (the next step)
    remove(entry)       Shift+Delete on `entry`: take it out of what the
                        mode lists. True (done: the launcher lists the
                        query's rows again, in place) or False (refused:
                        it flashes). The base refuses every row.
    count(entries)      the "n/m" beside the query
    close()             the launcher is done with it

The launcher sets `host` to itself while it shows the mode. Whoever
made the mode may hand it keys typed before the launcher had the
keyboard (`type_ahead`), or give up on it (`cancel`).

Pure Python, so modes can be built and tested without a display.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import fuzzy

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Entry:
    """One row."""

    title: str
    detail: str = ""            # muted, at the right: an address
    key: str = ""               # a word before the title, as the RUN row has
    glyph: str = ""             # where an app's icon would be
    positions: tuple[int, ...] = ()     # matched characters of the title
    enabled: bool = True        # dimmed when not; picking it is refused
    value: Any = None           # the mode's own


class Mode:
    placeholder = ""
    text = ""
    select_text = False
    host = None                 # the launcher, while it shows this mode

    def search(self, query: str) -> tuple[list[Entry], int]:
        raise NotImplementedError

    def pick(self, entry: Entry | None, query: str):
        raise NotImplementedError

    def remove(self, entry: Entry) -> bool:
        return False

    def count(self, entries: list[Entry]) -> str:
        return str(len(entries))

    def close(self) -> None:
        pass

    # ── what a sender of keys or a cancel calls ─────────────────────────
    def type_ahead(self, keys: list) -> bool:
        """Keys typed before the launcher had the keyboard (KeyEvents), as
        if typed there. False when no launcher shows this mode."""
        return self.host is not None and bool(self.host.type_ahead(self, keys))

    def cancel(self) -> None:
        """Whoever asked for this mode no longer wants it."""
        if self.host is not None:
            self.host.cancel(self)


# ── a list of actions ───────────────────────────────────────────────────
@dataclass(eq=False)
class Choice:
    """An action in a `Choices` list. `run` returns None (done), False
    (refused), or the next step: a mode, or a list of Choice."""

    label: str
    run: Callable[[], Any]
    enabled: bool = True
    detail: str = ""
    glyph: str = ""
    _text: fuzzy.Text | None = field(default=None, repr=False)

    def text(self) -> fuzzy.Text:
        if self._text is None:
            self._text = fuzzy.Text(self.label)
        return self._text


class Choices(Mode):
    """Actions, filtered by the query as the apps are, in their own
    order when nothing is typed. The selection starts on the first one
    that can be done."""

    def __init__(self, choices: list[Choice], *, placeholder: str = "") -> None:
        self.choices = list(choices)
        self.placeholder = placeholder

    def search(self, query: str) -> tuple[list[Entry], int]:
        folded, _ = fuzzy.fold(query.strip())
        rows = []
        for i, choice in enumerate(self.choices):
            if not folded:
                rows.append((0.0, i, choice, ()))
                continue
            hit = fuzzy.match(folded, choice.text())
            if hit is not None:
                bonus = 2.0 if choice.text().folded.startswith(folded) else 0.0
                rows.append((hit.score + bonus, i, choice, hit.positions))
        rows.sort(key=lambda r: (-r[0], r[1]))
        entries = [Entry(c.label, detail=c.detail, glyph=c.glyph, positions=pos,
                         enabled=c.enabled, value=c) for _, _, c, pos in rows]
        first = next((i for i, e in enumerate(entries) if e.enabled), 0)
        return entries, first

    def pick(self, entry: Entry | None, query: str):
        if entry is None or not entry.enabled:
            return False
        result = entry.value.run()
        if result is None or result is True:
            return True
        if result is False:
            return False
        if isinstance(result, list):
            return Choices(result, placeholder=self.placeholder)
        return result

    def count(self, entries: list[Entry]) -> str:
        return f"{len(entries)}/{len(self.choices)}"
