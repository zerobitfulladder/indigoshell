"""Addresses: the launcher's list for a browser's Ctrl+L and Ctrl+T.

A launcher mode (services/modes.py) beside the app catalog: the same
window, the same rows and keys, other things in the rows. Reads Flux's
history and bookmarks (browser/places.py), read-only; picking a row
hands its address to `on_load`, which the shell's side of Flux
(browser/context.py) turns into a `load` command for the browser window
that asked.

The rows, top to bottom:

1. What was typed, always first: OPEN <it> when it reads as an address
   (places.classify), else SEARCH <it>. Shift+Return takes this row
   whatever is selected.
2. The bookmarks and the history that match, each with its title and its
   address, ranked the way the app list is ranked: fzy's score on the
   title (or, weighted down, on the address), a bonus for the query
   starting the title, and frecency (visits weighted by how recent) on
   a log scale. A bookmark gets a bonus of its own and stands for its
   page's history too.
3. When the query starts a host that has been visited ("yo" for
   www.youtube.com: a leading "www." does not count), that host's front
   page, ranked high.

Shift+Delete on a bookmark's row takes the bookmark out: `on_remove`
gets its address (the shell sends the browser that asked a `bookmark`
"remove" naming it; the browser writes the file), and the row leaves
the list at once, the query as it was. If the page is in the history it
comes back as a history row: the history keeps what it had. On any
other row, or with no `on_remove`, Shift+Delete is refused.

The selection starts on the best of the rows the query is a prefix of:
a bookmark's name, or a visited host's front page. So "yo" and Return go
to YouTube. Otherwise it starts on the typed row, and Return opens or
searches what was typed. With nothing typed there is no typed row:
bookmarks and the most frecent history, the selection on the first.

Cost per keystroke stays flat as the history grows: SQL narrows it to
the CANDIDATES most frecent pages holding the query's characters in
order, and only those are scored in Python, their prepared texts kept
for the rest of the open (tests/browser_launcher.py measures it).
"""

import logging
import math
import time
from pathlib import Path
from typing import Callable

from .. import fuzzy
from ..browser import places
from .modes import Entry, Mode

log = logging.getLogger(__name__)

CANDIDATES = 150                # history rows scored per keystroke, at most
MAX_ROWS = 80                   # rows listed, at most
PREFIX_BONUS = 2.0              # the query starts the title (as for app names)
BOOKMARK_BONUS = 1.0
ORIGIN_BONUS = 3.0              # a visited host's front page, when the query starts the host
ADDRESS_WEIGHT = 0.7            # a match in the address counts for less than in the title
HISTORY_WEIGHT = 0.5            # frecency, log-scaled: the apps' history weight

BOOKMARK = ""             # Nerd Font: fa-bookmark
HISTORY = ""              # Nerd Font: fa-history


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


class AddressMode(Mode):
    placeholder = "address or search"

    def __init__(self, *, text: str = "", on_load: Callable[[str], object],
                 on_remove: Callable[[str], bool] | None = None,
                 history: Path | None = None, bookmarks: Path | None = None,
                 now: float | None = None) -> None:
        self.text = text
        self.select_text = bool(text)
        self.on_load = on_load
        self.on_remove = on_remove
        self.now = now
        self.reader = places.HistoryReader(history)
        marks = places.BookmarkFile(bookmarks)
        marks.load()
        self.bookmarks = [(b, fuzzy.Text(b["name"]), fuzzy.Text(places.display(b["url"])[:250]))
                          for b in marks.entries]
        self._texts: dict[str, tuple[fuzzy.Text, fuzzy.Text]] = {}
        self._hosts: list[tuple[str, bool, float]] | None = None
        self._marked: dict[str, float] | None = None    # bookmark key -> its pages' frecency
        self._total: int | None = None

    def _now(self) -> float:
        return time.time() if self.now is None else self.now

    # ── search ──────────────────────────────────────────────────────────
    def search(self, query: str) -> tuple[list[Entry], int]:
        now = self._now()
        typed = query.strip()
        folded, _ = fuzzy.fold(typed)
        head: list[Entry] = []
        kind, url = places.classify(typed)
        if kind:
            head.append(Entry(typed, key="OPEN" if kind == "open" else "SEARCH",
                              value=url))

        rows: dict[str, tuple[float, Entry, bool]] = {}
        marked = self._bookmark_frecency(now)
        for b, name, address in self.bookmarks:
            hit = self._score(folded, name, address)
            if hit is None:
                continue
            score, positions, prefix = hit
            key = places.page_key(b["url"])
            score += BOOKMARK_BONUS + HISTORY_WEIGHT * math.log1p(marked.get(key, 0.0))
            rows[key] = (score, Entry(b["name"], detail=places.display(b["url"]),
                                      glyph=BOOKMARK, positions=positions, value=b["url"]),
                         prefix)

        for v in self.reader.candidates(folded, CANDIDATES, now):
            if v.url in rows:
                continue
            title, address = self._texts_of(v.url, v.title)
            hit = self._score(folded, title, address)
            if hit is None:
                continue
            score, positions, _prefix = hit
            score += HISTORY_WEIGHT * math.log1p(v.frecency)
            rows[v.url] = (score, Entry(v.title or places.display(v.url),
                                        detail=places.display(v.url) if v.title else "",
                                        glyph=HISTORY, positions=positions, value=v.url),
                           False)

        origin = self._origin(folded, now) if folded else None
        if origin is not None:
            key, score, entry = origin
            rows[key] = (score, entry, True)

        ranked = sorted(rows.values(), key=lambda r: -r[0])[:MAX_ROWS]
        entries = head + [entry for _, entry, _ in ranked]
        start = 0
        if folded:
            start = next((len(head) + i for i, (_, _, prefix) in enumerate(ranked) if prefix), 0)
        return entries, start

    def _score(self, folded: str, title: fuzzy.Text, address: fuzzy.Text):
        """(score, title positions, whether the query starts the title),
        or None for no match. Nothing typed matches everything, at 0."""
        if not folded:
            return 0.0, (), False
        t = fuzzy.match(folded, title)
        a = fuzzy.match(folded, address)
        if t is None and a is None:
            return None
        prefix = title.folded.startswith(folded)
        t_score = t.score + (PREFIX_BONUS if prefix else 0.0) if t else -math.inf
        a_score = a.score * ADDRESS_WEIGHT if a else -math.inf
        if t_score >= a_score:
            return t_score, t.positions, prefix
        return a_score, (), False

    def _texts_of(self, url: str, title: str) -> tuple[fuzzy.Text, fuzzy.Text]:
        got = self._texts.get(url)
        if got is None:
            shown = places.display(url)[:250]
            got = (fuzzy.Text((title or shown)[:250]), fuzzy.Text(shown))
            self._texts[url] = got
        return got

    def _bookmark_frecency(self, now: float) -> dict[str, float]:
        if self._marked is None:
            self._marked = {}
            for b, _, _ in self.bookmarks:
                key = places.page_key(b["url"])
                page = self.reader.page(key)
                if page is not None:
                    self._marked[key] = places.frecency(page.visits, page.last, now)
        return self._marked

    def _origin(self, folded: str, now: float):
        """The front page of the most frecent visited host the query
        starts, as (address, score, row)."""
        if self._hosts is None:
            self._hosts = self.reader.hosts(now)
        for host, https, fr in self._hosts:
            bare = _bare(host)
            if not (bare.startswith(folded) or host.startswith(folded)):
                continue
            url = ("https://" if https else "http://") + host + "/"
            page = self.reader.page(url)
            title = page.title if page is not None and page.title else ""
            shown = fuzzy.Text(title or bare)
            hit = fuzzy.match(folded, shown) or fuzzy.match(folded, fuzzy.Text(bare))
            score = (hit.score if hit else 0.0) + ORIGIN_BONUS + HISTORY_WEIGHT * math.log1p(fr)
            positions = fuzzy.match(folded, shown)
            return url, score, Entry(title or bare, detail=places.display(url),
                                     glyph=HISTORY,
                                     positions=positions.positions if positions else (),
                                     value=url)
        return None

    # ── the rest of a mode ──────────────────────────────────────────────
    def pick(self, entry: Entry | None, query: str):
        if entry is None or entry.key:
            kind, url = places.classify(query)
            if not kind:
                return False
        else:
            url = entry.value
        log.info("address picked: %s", url)
        self.on_load(url)
        return True

    def remove(self, entry: Entry) -> bool:
        """Shift+Delete on `entry`: a bookmark's row takes the bookmark
        out; False (refused) for any other row."""
        if self.on_remove is None or entry.key or entry.glyph != BOOKMARK:
            return False
        mark = next((b for b, _, _ in self.bookmarks if b["url"] == entry.value), None)
        if mark is None or self.on_remove(mark["url"]) is False:
            return False
        log.info("bookmark removed from the address list: %s", mark["url"])
        # The browser drops every entry for that page; so does the list.
        key = places.page_key(mark["url"])
        self.bookmarks = [t for t in self.bookmarks if places.page_key(t[0]["url"]) != key]
        self._marked = None
        self._total = None
        return True

    def count(self, entries: list[Entry]) -> str:
        if self._total is None:
            self._total = len(self.bookmarks) + self.reader.count()
        listed = sum(1 for e in entries if not e.key)
        return f"{listed}/{self._total}"

    def close(self) -> None:
        self.reader.close()
