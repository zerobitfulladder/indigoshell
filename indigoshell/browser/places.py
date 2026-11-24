"""Places: where the browser has been, what the user keeps, and what a
typed line means.

Pure Python (sqlite3, json, the shell's pure `fuzzy.fold`), because both
halves use it: the browser records history and writes bookmarks; the
daemon's launcher reads both for its address mode (services/addresses.py)
and never writes either.

What a typed line means (`classify`): an address when it has a scheme
the browser knows, starts with `localhost`, is an IP address, is a path
on disk, or has a dot and no spaces; otherwise a Google search. A bare
host gets http://, as Qt's fromUserInput gives it (a site that wants
https redirects there).

History: `<data_dir>/browser-history.sqlite`, beside the profile's own
directory rather than in it (Chromium keeps its files in there). One row
per page: the address (fragment and any user:password dropped), its
title, its host, how often and when it was first and last visited, and
`search`, the title and address folded the way the launcher matches
(case and diacritics dropped) so the reader can narrow its candidates in
SQL. WAL mode: the daemon reads while the browser writes, neither waits.
Kept to HISTORY_LIMIT pages: past that, the pages with the lowest
frecency (visits weighted by how long ago the last one was; ties: the
oldest) go, down to 90% of it, checked at start and every
PRUNE_EVERY visits.

Bookmarks: `<config_dir>/browser/bookmarks.json`, next to sites.json, a
list a person can edit:

    [{"name": "YouTube", "url": "https://www.youtube.com/", "added": 1763900000}]

The browser is the only writer, and replaces the file whole (a temporary
file renamed over it). It keeps whatever else the file holds (entries it
cannot use, keys it does not know) when it writes, and it does not write
over a file it cannot parse: a typo in a hand edit is reported, not lost.
"""

import ipaddress
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

from indigoshell.core.paths import config_dir, data_dir
from indigoshell.fuzzy import fold

log = logging.getLogger(__name__)

SEARCH = "https://www.google.com/search?q="
SCHEMES = ("http", "https", "file", "about", "chrome", "data", "blob", "view-source",
           "devtools", "ftp", "ws", "wss", "qrc")
# Pages that are not places: the browser's own, and ones made of their
# own contents.
UNRECORDED = ("about", "data", "blob", "chrome", "chrome-error", "chrome-extension",
              "devtools", "view-source", "javascript", "qrc")
MAX_URL = 4096                  # longer addresses (tracking links) are not kept

HISTORY_LIMIT = 10_000
PRUNE_EVERY = 100
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


# ── addresses ───────────────────────────────────────────────────────────
def classify(text: str) -> tuple[str, str]:
    """("open", url) or ("search", url) for a typed line; ("", "") for
    nothing typed."""
    text = text.strip()
    if not text:
        return "", ""
    m = _SCHEME.match(text)
    if m and m.group(1).lower() in SCHEMES:
        return "open", text
    if text.startswith(("/", "~/")):
        return "open", "file://" + os.path.expanduser(text)
    if " " not in text and _looks_like_host(text):
        return "open", "http://" + text
    return "search", SEARCH + quote_plus(text)


def _looks_like_host(text: str) -> bool:
    host = re.split(r"[/?#]", text, maxsplit=1)[0]
    if host.startswith("["):                    # [::1]:8080
        host = host[1:].split("]", 1)[0]
        try:
            ipaddress.IPv6Address(host)
            return True
        except ValueError:
            return False
    name = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if name.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." in name.strip(".") and not name.startswith(".") and not name.endswith(".")


def page_key(url: str) -> str:
    """The address a page is known by: fragment and user:password
    dropped, scheme and host in lower case, an empty path as "/"."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc.rsplit("@", 1)[-1].lower()
    path = parts.path or ("/" if netloc else "")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def recordable(url: str) -> bool:
    """Whether a page belongs in the history."""
    if not url or len(url) > MAX_URL:
        return False
    m = _SCHEME.match(url)
    if m is None or m.group(1).lower() in UNRECORDED:
        return False
    return m.group(1).lower() == "file" or bool(host_of(url))


def display(url: str) -> str:
    """An address as a row shows it: no http(s):// and no trailing slash
    on a bare host."""
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if url.count("/") == 1 and url.endswith("/"):
        url = url[:-1]
    return url


def search_text(title: str, url: str) -> str:
    return fold(title)[0] + "\n" + fold(url)[0]


def frecency(visits: float, last: float, now: float) -> float:
    """Visits weighted by how recent the last one is (the shape of the
    launcher's app history, with a step for a month)."""
    return visits * _recency(now - last)


def _recency(age: float) -> float:
    if age < 3600:
        return 4.0
    if age < 86400:
        return 2.0
    if age < 7 * 86400:
        return 1.0
    if age < 30 * 86400:
        return 0.5
    return 0.25


# The same weights in SQL, with :now bound.
_FRECENCY_SQL = ("visits * CASE WHEN :now - last < 3600 THEN 4.0 "
                 "WHEN :now - last < 86400 THEN 2.0 "
                 "WHEN :now - last < 604800 THEN 1.0 "
                 "WHEN :now - last < 2592000 THEN 0.5 ELSE 0.25 END")


# ── history ─────────────────────────────────────────────────────────────
def history_path() -> Path:
    return Path(data_dir()) / "browser-history.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    url    TEXT PRIMARY KEY,
    title  TEXT NOT NULL DEFAULT '',
    host   TEXT NOT NULL DEFAULT '',
    visits INTEGER NOT NULL DEFAULT 0,
    first  REAL NOT NULL,
    last   REAL NOT NULL,
    search TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS history_last ON history(last);
"""


class History:
    """The browser's writer. Every failure is logged once and the
    browser carries on without history: a full disk must not take the
    browser with it."""

    def __init__(self, path: Path | None = None, *, limit: int = HISTORY_LIMIT) -> None:
        self.path = Path(path) if path else history_path()
        self.limit = limit
        self.db: sqlite3.Connection | None = None
        self._since_prune = 0
        self._failed = False

    def _open(self) -> sqlite3.Connection | None:
        if self.db is not None or self._failed:
            return self.db
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), timeout=2.0)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            db.commit()
        except sqlite3.Error as e:
            self._fail(e)
            return None
        self.db = db
        self.prune()
        return db

    def _fail(self, e: Exception) -> None:
        if not self._failed:
            log.warning("history %s: %s; not recording history", self.path, e)
        self._failed = True
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None

    def visit(self, url: str, title: str = "", *, again: bool = False,
              now: float | None = None) -> str | None:
        """A page was shown. `again`: the same page as this tab's last
        visit (a reload, a fragment), which moves its time and does not
        count. Returns the address kept, or None when it is not kept."""
        if not recordable(url):
            return None
        db = self._open()
        if db is None:
            return None
        key = page_key(url)
        title = _title(title, url, key)
        now = time.time() if now is None else now
        try:
            db.execute(
                "INSERT INTO history (url, title, host, visits, first, last, search) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET visits = visits + excluded.visits, "
                "last = excluded.last, "
                "title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END, "
                "search = CASE WHEN excluded.title != '' THEN excluded.search ELSE search END",
                (key, title, host_of(key), 0 if again else 1, now, now,
                 search_text(title, key)))
            db.commit()
        except sqlite3.Error as e:
            self._fail(e)
            return None
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self.prune(now)
        return key

    def retitle(self, url: str, title: str) -> None:
        """The title arrived after the load."""
        if not recordable(url) or self.db is None:
            return
        key = page_key(url)
        title = _title(title, url, key)
        if not title:
            return
        try:
            self.db.execute("UPDATE history SET title = ?, search = ? WHERE url = ? "
                            "AND title != ?", (title, search_text(title, key), key, title))
            self.db.commit()
        except sqlite3.Error as e:
            self._fail(e)

    def prune(self, now: float | None = None) -> int:
        """Down to 90% of the limit once past it; returns how many went."""
        self._since_prune = 0
        if self.db is None:
            return 0
        now = time.time() if now is None else now
        try:
            count = self.db.execute("SELECT count(*) FROM history").fetchone()[0]
            if count <= self.limit:
                return 0
            drop = count - int(self.limit * 0.9)
            self.db.execute(
                "DELETE FROM history WHERE url IN (SELECT url FROM history "
                f"ORDER BY {_FRECENCY_SQL} ASC, last ASC LIMIT :drop)",
                {"now": now, "drop": drop})
            self.db.commit()
        except sqlite3.Error as e:
            self._fail(e)
            return 0
        log.info("history at %d pages, past its %d: the %d least visited went",
                 count, self.limit, drop)
        return drop

    def close(self) -> None:
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None


def _title(title: str, url: str, key: str) -> str:
    """A page with no <title> has its address for one; that is no title."""
    title = (title or "").strip()
    return "" if title in (url, key, display(url), display(key)) else title


@dataclass(frozen=True)
class Visited:
    url: str
    title: str
    host: str
    visits: int
    last: float
    frecency: float


class HistoryReader:
    """The daemon's side: read-only, and quick. Every query is narrowed
    in SQL (the query's characters, in order, in `search`) and capped, so
    the work in Python per keystroke does not grow with the history."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else history_path()
        self.db: sqlite3.Connection | None = None
        self._warned = False

    def open(self) -> bool:
        if self.db is not None:
            return True
        if not self.path.exists():
            return False                # no history yet: nothing to create
        try:
            # Read-only, and a short busy wait: in WAL a reader is never
            # blocked by the writer, and nothing here may stall the loop.
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0.05)
        except sqlite3.Error as e:
            self._warn(e)
            return False
        return True

    def _warn(self, e: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("history %s unreadable: %s", self.path, e)

    def _rows(self, sql: str, params) -> list:
        if not self.open():
            return []
        try:
            return self.db.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            self._warn(e)
            return []

    def count(self) -> int:
        rows = self._rows("SELECT count(*) FROM history", ())
        return rows[0][0] if rows else 0

    def candidates(self, folded: str, limit: int, now: float | None = None) -> list[Visited]:
        """The `limit` most frecent pages whose title or address hold the
        folded query's characters in order (all of them for "")."""
        now = time.time() if now is None else now
        pattern = "%" + "".join(_like(c) + "%" for c in folded)
        rows = self._rows(
            f"SELECT url, title, host, visits, last, {_FRECENCY_SQL} AS f FROM history "
            "WHERE search LIKE :pattern ESCAPE '\\' ORDER BY f DESC, last DESC LIMIT :limit",
            {"now": now, "pattern": pattern, "limit": limit})
        return [Visited(*row) for row in rows]

    def hosts(self, now: float | None = None) -> list[tuple[str, bool, float]]:
        """Every host visited: (host, whether any of it was https, its
        frecency), most frecent first."""
        now = time.time() if now is None else now
        return [(h, bool(s), f) for h, s, f in self._rows(
            f"SELECT host, max(substr(url, 1, 6) = 'https:'), sum({_FRECENCY_SQL}) AS f "
            "FROM history WHERE host != '' GROUP BY host ORDER BY f DESC", {"now": now})]

    def page(self, url: str) -> Visited | None:
        rows = self._rows("SELECT url, title, host, visits, last, 0 FROM history "
                          "WHERE url = ?", (url,))
        return Visited(*rows[0]) if rows else None

    def close(self) -> None:
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None


def _like(ch: str) -> str:
    return "\\" + ch if ch in "%_\\" else ch


# ── bookmarks ───────────────────────────────────────────────────────────
def bookmarks_path() -> Path:
    return Path(config_dir()) / "browser" / "bookmarks.json"


class BookmarksBroken(Exception):
    """The file is there but is not a list: it is not written over."""


def parse_bookmarks(text: str) -> tuple[list, list[dict], list[str]]:
    """(the file's list as it is, the usable entries, what was skipped).
    Raises ValueError when the file is not a JSON list."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("the file is not a JSON list of bookmarks")
    entries, warnings = [], []
    for i, item in enumerate(data):
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or not url.strip():
            warnings.append(f"entry {i} has no \"url\"; skipped")
            continue
        name = item.get("name")
        added = item.get("added", 0)
        entries.append({"name": name if isinstance(name, str) and name.strip() else url,
                        "url": url.strip(),
                        "added": added if isinstance(added, (int, float))
                        and not isinstance(added, bool) else 0})
    return data, entries, warnings


class BookmarkFile:
    """bookmarks.json, read when asked; written whole when changed."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else bookmarks_path()
        self.raw: list = []             # the file's list, unusable entries included
        self.entries: list[dict] = []
        self.broken = False
        self.text: str | None = None    # what the last read or write found
        self.loads = 0

    def load(self) -> bool:
        """Read the file; True when the bookmarks changed. A missing file
        is no bookmarks; one that does not parse keeps the list before
        it."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            changed = bool(self.entries)
            self.raw, self.entries, self.broken, self.text = [], [], False, None
            return changed
        except OSError as e:
            log.warning("could not read %s (%s); keeping the bookmarks", self.path, e)
            return False
        if text == self.text:
            return False
        self.text = text
        try:
            raw, entries, warnings = parse_bookmarks(text)
        except ValueError as e:
            self.broken = True
            log.warning("%s: %s; keeping the bookmarks before it, and not writing "
                        "over it", self.path, e)
            return False
        for w in warnings:
            log.warning("%s: %s", self.path, w)
        self.broken = False
        self.loads += 1
        changed = entries != self.entries
        self.raw, self.entries = raw, entries
        return changed

    def find(self, url: str) -> dict | None:
        key = page_key(url)
        return next((e for e in self.entries if page_key(e["url"]) == key), None)

    def contains(self, url: str) -> bool:
        return bool(url) and self.find(url) is not None

    def add(self, url: str, name: str, now: float | None = None) -> bool:
        """False when it is a bookmark already. Raises BookmarksBroken, or
        OSError when the file cannot be written."""
        self._writable()
        if self.contains(url):
            return False
        entry = {"name": name.strip() or url, "url": url,
                 "added": int(time.time() if now is None else now)}
        self._write(self.raw + [entry])
        return True

    def remove(self, url: str) -> bool:
        self._writable()
        key = page_key(url)
        keep = [item for item in self.raw
                if not (isinstance(item, dict) and isinstance(item.get("url"), str)
                        and page_key(item["url"].strip()) == key)]
        if len(keep) == len(self.raw):
            return False
        self._write(keep)
        return True

    def _writable(self) -> None:
        self.load()                     # a hand edit made since is kept
        if self.broken:
            raise BookmarksBroken(f"{self.path} does not parse")

    def _write(self, data: list) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".bookmarks-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.text = None
        self.load()
