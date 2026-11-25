"""Addresses: what Flux answers its address inputs with (Ctrl+L, Ctrl+T in
the shell's top bar: indigoshell/applink/PROTOCOL.md, `query` and
`answer`).

Pure Python, as the rest of Flux's side of the line: Flux's history and
bookmarks (places.py), read-only, made into the bar's rows
(applink/bar.py). One `Addresses` per open input: the history reader
stays open and the texts prepared for the rest of the open, and `close`
lets go of them. Picking a row (`submit`) is the window's: `url_of` says
which address a submit names.

A row's id is short and Flux's own ("r1", "r2", ... stable for the open,
one per address), never the address itself: the bar's ids are keys of at
most 128 characters, and a real history is full of addresses far longer
(a video's with its playlist, a search's), which as ids would make the
shell refuse the whole answer. `url_of` (and `address_of`) turn an id
back into its address. Titles and details are cut to TEXT_CUT
characters, well inside the bar's string bound.

The rows, top to bottom:

1. What was typed, always first (id "typed"): tagged OPEN when it reads
   as an address (places.classify), else SEARCH. Shift+Return takes
   this row whatever is selected.
2. The bookmarks and the history that match, each with its title and its
   address, ranked the way the app list is ranked: fzy's score on the
   title (or, weighted down, on the address), a bonus for the query
   starting the title, and frecency (visits weighted by how recent) on
   a log scale. A bookmark gets a bonus of its own and stands for its
   page's history too. Their ids are their addresses; a bookmark's row
   allows the `remove` action (Shift+Delete).
3. When the query starts a host that has been visited ("yo" for
   www.youtube.com: a leading "www." does not count), that host's front
   page, ranked high.

Shift+Delete on a bookmark's row (`act`, `remove`): the window takes the
bookmark out of the file, and `remove` here drops it from this open's
list; the shell queries the text again, and if the page is in the
history it comes back as a history row: the history keeps what it had.

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

from flux import places
from indigoshell import fuzzy
from indigoshell.applink import bar as B

log = logging.getLogger(__name__)

CANDIDATES = 150                # history rows scored per keystroke, at most
MAX_ROWS = 80                   # rows listed, at most
PREFIX_BONUS = 2.0              # the query starts the title (as for app names)
BOOKMARK_BONUS = 1.0
ORIGIN_BONUS = 3.0              # a visited host's front page, when the query starts the host
ADDRESS_WEIGHT = 0.7            # a match in the address counts for less than in the title
HISTORY_WEIGHT = 0.5            # frecency, log-scaled: the apps' history weight

BOOKMARK, HISTORY = "bookmark", "history"     # the rows' icons
TYPED = "typed"                 # the typed row's id
REMOVE = "remove"               # the action taking a bookmark's row out


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


TEXT_CUT = 300                  # characters of a row's title or detail, at most


def _cut(value: str) -> str:
    return value if len(value) <= TEXT_CUT else value[:TEXT_CUT - 1] + "…"


class Addresses:
    """One open address input's rows (the module docstring)."""

    def __init__(self, *, history: Path | None = None, bookmarks: Path | None = None,
                 now: float | None = None) -> None:
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
        self._ids: dict[str, str] = {}      # address -> its rows' id, for this open
        self._urls: dict[str, str] = {}     # ... and back

    def _id(self, url: str) -> str:
        rid = self._ids.get(url)
        if rid is None:
            rid = self._ids[url] = f"r{len(self._ids) + 1}"
            self._urls[rid] = url
        return rid

    def _row(self, title: str, *, url: str, detail: str = "", icon: str, positions=()) -> dict:
        shown = _cut(title)
        return B.row(self._id(url), shown, detail=_cut(detail) or None, icon=icon,
                     match=[p for p in positions if p < len(shown)] or None,
                     actions=[REMOVE] if icon == BOOKMARK else None)

    def _now(self) -> float:
        return time.time() if self.now is None else self.now

    # ── search ──────────────────────────────────────────────────────────
    def search(self, query: str) -> tuple[list[dict], int]:
        """The rows for what was typed, best first, and the one the
        selection starts on."""
        now = self._now()
        typed = query.strip()
        folded, _ = fuzzy.fold(typed)
        head: list[dict] = []
        kind, _url = places.classify(typed)
        if kind:
            head.append(B.row(TYPED, _cut(typed), tag="OPEN" if kind == "open" else "SEARCH"))

        rows: dict[str, tuple[float, dict, bool]] = {}
        marked = self._bookmark_frecency(now)
        for b, name, address in self.bookmarks:
            hit = self._score(folded, name, address)
            if hit is None:
                continue
            score, positions, prefix = hit
            key = places.page_key(b["url"])
            score += BOOKMARK_BONUS + HISTORY_WEIGHT * math.log1p(marked.get(key, 0.0))
            rows[key] = (score, self._row(b["name"], url=b["url"],
                                          detail=places.display(b["url"]),
                                     icon=BOOKMARK, positions=positions), prefix)

        for v in self.reader.candidates(folded, CANDIDATES, now):
            if v.url in rows:
                continue
            title, address = self._texts_of(v.url, v.title)
            hit = self._score(folded, title, address)
            if hit is None:
                continue
            score, positions, _prefix = hit
            score += HISTORY_WEIGHT * math.log1p(v.frecency)
            rows[v.url] = (score, self._row(v.title or places.display(v.url), url=v.url,
                                       detail=places.display(v.url) if v.title else "",
                                       icon=HISTORY, positions=positions), False)

        origin = self._origin(folded, now) if folded else None
        if origin is not None:
            key, score, row = origin
            rows[key] = (score, row, True)

        ranked = sorted(rows.values(), key=lambda r: -r[0])[:MAX_ROWS]
        out = head + [row for _, row, _ in ranked]
        start = 0
        if folded:
            start = next((len(head) + i for i, (_, _, prefix) in enumerate(ranked) if prefix), 0)
        return out, start

    def answer(self, input_id, seq: int, text: str) -> dict:
        """The `answer` to a `query` for `text`."""
        rows, start = self.search(text)
        return B.answer(input_id, seq, rows, selected=rows[start]["id"] if rows else None,
                        total=self.total)

    @property
    def total(self) -> int:
        """Everything there is to list: the bookmarks and the history's
        pages."""
        if self._total is None:
            self._total = len(self.bookmarks) + self.reader.count()
        return self._total

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
            return url, score, self._row(title or bare, url=url, detail=places.display(url),
                                    icon=HISTORY,
                                    positions=positions.positions if positions else ())
        return None

    # ── the rest of an open ─────────────────────────────────────────────
    def url_of(self, row, text: str) -> str | None:
        """The address a `submit` names: its row's, or for the typed row
        and none (Shift+Return), what was typed (None when it is
        nothing, or a row this open never listed)."""
        if row is None or row == TYPED:
            kind, url = places.classify(text)
            return url if kind else None
        return self.address_of(row)

    def address_of(self, row) -> str | None:
        """The address of a row this open listed, by its id."""
        return self._urls.get(row) if isinstance(row, str) else None

    def remove(self, url: str) -> bool:
        """A bookmark taken out of the file: out of this open's rows too.
        False when no bookmark here has that address."""
        mark = next((b for b, _, _ in self.bookmarks if b["url"] == url), None)
        if mark is None:
            return False
        log.info("bookmark removed from the address list: %s", url)
        # The window drops every entry for that page; so does the list.
        key = places.page_key(url)
        self.bookmarks = [t for t in self.bookmarks if places.page_key(t[0]["url"]) != key]
        self._marked = None
        self._total = None
        return True

    def close(self) -> None:
        self.reader.close()
