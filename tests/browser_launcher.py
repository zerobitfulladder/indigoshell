"""Flux's address inputs and actions in the top bar, the inputs and menus
of any app's bar, and what they read and open.

    .venv/bin/python tests/browser_launcher.py              all of it; a small
                                                             list shows in the
                                                             bottom-right corner
                                                             for about a second
    .venv/bin/python tests/browser_launcher.py --off-screen  1-6 only: nothing shows

1. places (flux/places.py): what a typed line means, the history
   writer's rules and its pruning, the bookmarks file (atomic writes,
   hand edits, a broken file).
2. Flux's answers to its address inputs (flux/addresses.py, pure) on a
   scratch history and bookmarks: the rows and their order for typed
   addresses, searches, bookmark and host prefixes, history; what a
   submit names (Shift+Return: the text); an answer's shape; removing a
   bookmark's row; and what a search costs with 5,000 and 10,000 pages
   of history.
3. An input of an app's bar (applink/prompt.py) and its list, driven
   with KeyEvents, the app's answers given by hand, painted off screen:
   the text selected and replaced by typing, one query in flight (newer
   text waiting, only the newest sent), stale answers and ones never
   asked for dropped, the count, a row action that drops its row and
   queries again (and one refused), Enter waiting for the latest
   answer (and past ENTER_WAIT_S, the text alone), Shift+Enter, Escape;
   `keep`; `steps` (find: Enter to stepping, n / N, its chord back to
   typing); static rows filtered here; a toggle's chord.
4. Menus: the same list, a submenu beside its row (Right, Return, Left
   back), filtered by typing (the filter line), a dimmed row refused,
   `pick`, Escape. 3b. MOD+D's app search (applink/catalog.py) as the
   same input: the apps, filtered, launched; RUN for nothing matching,
   Shift+Return; a command line that is none flashing; the service
   putting it in the top bar's app section, an app's section giving
   way while it is open; no top bar, no search.
5. What Flux sends the shell (flux/bar.py, pure): a window's state as
   its bar: the tabs by id with the load and the agent's hold, the host
   with its marks, the inputs (Ctrl+L, Ctrl+T, Ctrl+F) and the actions
   (Ctrl+M: the bookmark actions, "Let Lilac drive again"), a blank
   page, a single window's, the agent's own window's, Flux's own find,
   the size of a 30-tab bar.
6. Placement (window.py): centred under a rect, clamped to the monitor
   holding it, forgotten when the window parks; outside clicks.
7. On screen: the real drop-down window spec, opened by the real
   Daemon.open through the real AppService for a stand-in browser (an
   unmapped window, announced, its bar sent and its queries answered
   over the socket as Flux does) under a stand-in top bar in the screen's bottom-right
   corner (no window with a strut is ever made here), on a Ctrl+L press
   delivered to the service's handler as the X server would report it
   once the key grab fired (nothing is typed into the display; the
   keyboard and pointer grabs are recorded, never made): the box in the
   section within the press's handling, the list flush under the bar at
   the section's left and width, its own pixels read (never the root);
   keys typed after the press into the box in order, an Escape among
   them closing it and a key after that dropped; the time from the
   answer to the list's first frame, cold and from parked. Then MOD+D
   the way `toggle launcher` does it: the app search in the shell's own
   section, its keys on the bar's window, the toggle again closing it.

Nothing here touches the running shell or the real history and
bookmarks: every XDG directory and socket is a scratch one.
"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SCRATCH = Path(tempfile.mkdtemp(prefix="indigoshell-launcher-test-"))
# Before anything of the shell's reads them.
os.environ.update({"XDG_DATA_HOME": str(SCRATCH / "data"),
                   "XDG_CONFIG_HOME": str(SCRATCH / "config"),
                   "XDG_STATE_HOME": str(SCRATCH / "state"),
                   "XDG_CACHE_HOME": str(SCRATCH / "cache")})

from indigoshell.core.naming import APP              # noqa: E402
from indigoshell.core.paths import runtime_dir       # noqa: E402

FAILED: list[str] = []
NOW = 1_800_000_000.0


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── 1. places ───────────────────────────────────────────────────────────

def test_places() -> None:
    from flux import places

    section("1. places: typed lines, history, bookmarks")
    cases = {
        "example.org": ("open", "http://example.org"),
        "example.org/a b": ("search", places.SEARCH + "example.org%2Fa+b"),
        "https://x.org/p?q=1": ("open", "https://x.org/p?q=1"),
        "localhost:8080/x": ("open", "http://localhost:8080/x"),
        "LOCALHOST": ("open", "http://LOCALHOST"),
        "10.0.0.7": ("open", "http://10.0.0.7"),
        "10.0.0.7:631": ("open", "http://10.0.0.7:631"),
        "[::1]:8000": ("open", "http://[::1]:8000"),
        "about:blank": ("open", "about:blank"),
        "yo": ("search", places.SEARCH + "yo"),
        "what is 1.5": ("search", places.SEARCH + "what+is+1.5"),
        "şeker fiyatı": ("search", places.SEARCH + "%C5%9Feker+fiyat%C4%B1"),
        ".hidden": ("search", places.SEARCH + ".hidden"),
        "/tmp/a.html": ("open", "file:///tmp/a.html"),
        "   ": ("", ""),
    }
    wrong = {t: places.classify(t) for t, want in cases.items() if places.classify(t) != want}
    check("typed lines: scheme, localhost, IP, a dot without spaces, a path; else a search",
          not wrong, str(wrong or f"{len(cases)} lines"))
    check("page_key: fragment and user:password dropped, scheme and host lowered",
          places.page_key("HTTPS://u:p@Example.ORG/A?b=1#frag") == "https://example.org/A?b=1"
          and places.page_key("https://x.org") == "https://x.org/")
    skipped = [u for u in ("about:blank", "data:text/html,x", "blob:https://x.org/1",
                           "chrome://gpu", "devtools://devtools/x", "view-source:https://x.org",
                           "https://x.org/" + "a" * 5000) if places.recordable(u)]
    check("not history: about:, data:, blob:, chrome:, devtools:, view-source:, huge addresses",
          not skipped and places.recordable("https://x.org/") and places.recordable("file:///a"),
          str(skipped))

    db = SCRATCH / "places" / "h.sqlite"
    h = places.History(db, limit=50)
    h.visit("https://a.org/x#one", "A page", now=NOW)
    h.visit("https://a.org/x#two", "", again=True, now=NOW + 5)
    h.visit("https://a.org/x", "A page", now=NOW + 10)
    h.visit("https://u:secret@b.org/", "https://u:secret@b.org/", now=NOW)
    h.visit("about:blank", now=NOW)
    h.retitle("https://b.org/", "B, titled late")
    rows = {r[0]: r[1:] for r in h.db.execute("SELECT url, title, visits, last FROM history")}
    check("visits: one row per page, a fragment and `again` move its time without counting",
          rows.get("https://a.org/x") == ("A page", 2, NOW + 10), f"{rows.get('https://a.org/x')}")
    check("no password kept; an address for a title is no title; the title can arrive later",
          "https://b.org/" in rows and rows["https://b.org/"][0] == "B, titled late"
          and not any("secret" in u for u in rows), f"{list(rows)}")
    mode = h.db.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL mode", mode == "wal", mode)
    for i in range(60):
        # Old and visited once; recent and visited often.
        h.visit(f"https://old.org/{i}", f"Old {i}", now=NOW - 90 * 86400)
    for i in range(10):
        for _ in range(3):
            h.visit(f"https://kept.org/{i}", f"Kept {i}", now=NOW)
    dropped = h.prune(now=NOW)
    count = h.db.execute("SELECT count(*) FROM history").fetchone()[0]
    kept = h.db.execute("SELECT count(*) FROM history WHERE host = 'kept.org'").fetchone()[0]
    check("pruned past the limit to 90% of it, the old pages visited once first",
          count == 45 and kept == 10 and dropped > 0, f"{count} pages left, {kept}/10 kept.org")
    reader = places.HistoryReader(db)
    seen = len(reader.candidates("", 1000, NOW))
    h.visit("https://new.org/", "New", now=NOW)
    check("the reader (read-only) sees what the writer wrote, while it is open",
          len(reader.candidates("", 1000, NOW)) == seen + 1)
    h.close()
    check("... and after it closed", len(places.HistoryReader(db).candidates("new", 10, NOW)) == 1)
    check("no history file: no rows, and nothing created",
          places.HistoryReader(SCRATCH / "none.sqlite").candidates("", 10) == []
          and not (SCRATCH / "none.sqlite").exists())

    path = SCRATCH / "places" / "bookmarks.json"
    bf = places.BookmarkFile(path)
    added = bf.add("https://a.org/", "A", now=NOW)
    ino = path.stat().st_ino
    again = bf.add("https://a.org", "A again")
    bf.add("https://b.org/", "B", now=NOW)
    data = json.loads(path.read_text())
    check("add: [{name, url, added}], a second add of the same page refused",
          added and not again and data == [{"name": "A", "url": "https://a.org/", "added": int(NOW)},
                                            {"name": "B", "url": "https://b.org/", "added": int(NOW)}])
    check("written whole: a new file renamed over the old (new inode), no temporary left",
          path.stat().st_ino != ino and [p.name for p in path.parent.iterdir()
                                         if p.name.startswith(".bookmarks-")] == [])
    path.write_text(json.dumps([{"name": "Renamed", "url": "https://a.org/", "added": 1,
                                 "tags": ["mine"]}, {"oops": True},
                                {"name": "B", "url": "https://b.org/", "added": 2}]))
    changed = bf.load()
    check("a hand edit is read: renamed kept, a broken entry skipped",
          changed and [e["name"] for e in bf.entries] == ["Renamed", "B"])
    bf.remove("https://b.org/")
    kept_raw = json.loads(path.read_text())
    check("written again: what the file held beyond our use (an odd entry, a key) is kept",
          kept_raw == [{"name": "Renamed", "url": "https://a.org/", "added": 1, "tags": ["mine"]},
                       {"oops": True}])
    path.write_text("[{ broken")
    bf.load()
    refused = False
    try:
        bf.add("https://c.org/", "C")
    except places.BookmarksBroken:
        refused = True
    check("a file that does not parse: the list before it stays, and it is not written over",
          bf.broken and refused and bf.contains("https://a.org") and path.read_text() == "[{ broken")


# ── 2. the address mode ─────────────────────────────────────────────────

def _history(db: Path, rows) -> None:
    from flux import places
    h = places.History(db)
    for url, title, visits, age in rows:
        for _ in range(visits):
            h.visit(url, title, now=NOW - age)
    h.close()


def test_addresses() -> None:
    from flux import places
    from flux.addresses import BOOKMARK, HISTORY, TYPED, Addresses

    section("2. Flux's answers to its address inputs (flux/addresses.py, pure): rows, order, "
            "preselection, cost")
    db = SCRATCH / "addr" / "h.sqlite"
    marks = SCRATCH / "addr" / "bookmarks.json"
    _history(db, [("https://www.youtube.com/watch?v=1", "Cats - YouTube", 5, 600),
                  ("https://www.youtube.com/", "YouTube", 3, 3600 * 3),
                  ("https://github.com/x/y", "x/y: a repository", 2, 86400 * 2),
                  ("https://en.wikipedia.org/wiki/Yodeling", "Yodeling - Wikipedia", 1, 86400 * 40),
                  ("https://news.ycombinator.com/", "Hacker News", 10, 60),
                  ("http://10.0.0.1/", "Router", 1, 86400 * 9)])
    marks.write_text(json.dumps([{"name": "Arch Wiki", "url": "https://wiki.archlinux.org/",
                                  "added": 1},
                                 {"name": "Hacker News", "url": "https://news.ycombinator.com",
                                  "added": 2}]))

    def mode():
        return Addresses(history=db, bookmarks=marks, now=NOW)

    m = mode()

    def rows(q):
        found, start = m.search(q)
        return [(r.get("tag") or ("B" if r.get("icon") == BOOKMARK else
                                  "H" if r.get("icon") == HISTORY else "?"),
                 r["title"], r["id"] if not r.get("tag") else Addresses.url_of(r["id"], q))
                for r in found], start

    r, s = rows("")
    check("nothing typed: no typed row; bookmarks and history, most frecent first; first selected",
          s == 0 and r[0][0] == "B" and all(k in "BH" for k, _, _ in r)
          and len(r) == 7 and sum(1 for k, t, _ in r if t == "Hacker News") == 1,
          f"{[(k, t) for k, t, _ in r]}")
    r, s = rows("example.org/path")
    check("a typed address: OPEN it first, and selected", r[0] == ("OPEN", "example.org/path",
                                                                  "http://example.org/path")
          and s == 0, f"{r[:2]} start {s}")
    r, s = rows("how to yodel")
    check("a typed search: SEARCH it first, selected (no prefix to jump to)",
          r[0][0] == "SEARCH" and r[0][2] == places.SEARCH + "how+to+yodel" and s == 0, f"{r[:2]}")
    r, s = rows("yo")
    check("'yo': YouTube's front page right below the typed row, and selected (a host prefix)",
          r[0][0] == "SEARCH" and r[1] == ("H", "YouTube", "https://www.youtube.com/") and s == 1,
          f"{r[:4]} start {s}")
    r, s = rows("arch")
    check("'arch': the bookmark Arch Wiki, marked as a bookmark, selected (a name prefix)",
          r[s] == ("B", "Arch Wiki", "https://wiki.archlinux.org/") and s == 1, f"{r[:3]} start {s}")
    r, s = rows("hack")
    check("'hack': Hacker News once, as the bookmark (its history folded in)",
          [t for _, t, _ in r].count("Hacker News") == 1 and r[s][0] == "B", f"{r}")
    r, s = rows("cats")
    check("'cats': the history page matches, but the selection stays on the typed row",
          ("H", "Cats - YouTube", "https://www.youtube.com/watch?v=1") in r and s == 0, f"{r}")
    r, s = rows("10.0")
    check("'10.0': OPEN the address; the router's page below (no host-prefix jump for an IP"
          " typed in part, it is an address already)", r[0][0] == "OPEN"
          and any(t == "Router" for _, t, _ in r), f"{r} start {s}")
    found, start = m.search("yo")
    picked = [Addresses.url_of(found[start]["id"], "yo"), Addresses.url_of(None, "yo"),
              Addresses.url_of(TYPED, "yo"), Addresses.url_of(None, "   ")]
    check("what a submit names: the row's address; Shift+Return (no row) and the typed row: the "
          "typed text; nothing typed: nothing",
          picked == ["https://www.youtube.com/", places.SEARCH + "yo", places.SEARCH + "yo", None],
          f"{picked}")
    a = m.answer("address", 3, "yo")
    check("an answer: the input, the seq, the rows, the selected row's id, the total "
          "(bookmarks + history pages); a bookmark's rows allow `remove`, no other's",
          a["type"] == "answer" and a["input"] == "address" and a["seq"] == 3
          and a["selected"] == "https://www.youtube.com/" and a["total"] == 8
          and all((r.get("actions") == ["remove"]) == (r.get("icon") == BOOKMARK)
                  for r in a["rows"]), f"{a['total']} {a['selected']}")
    from indigoshell.applink import bar as B
    check("... in the vocabulary's shape", B.answer_problems(a) == [], f"{B.answer_problems(a)}")
    m.close()

    # Shift+Delete's side: a bookmark's row goes from this open's list.
    m = mode()
    hist = next(r["id"] for r in m.search("cats")[0] if r.get("icon") == HISTORY)
    refused = [m.remove(TYPED), m.remove(hist)]
    done = m.remove("https://news.ycombinator.com")
    after, _ = m.search("hack")
    rows_after = [(r.get("icon") == BOOKMARK, r["title"]) for r in after if not r.get("tag")]
    check("remove: a bookmark is taken out of the open's rows (the window writes the file); the "
          "typed row and a history row are none to take out",
          refused == [False, False] and done and m.total == 7, f"{refused} {done} {m.total}")
    check("... its page, in the history too, comes back as a history row; the file is not "
          "this module's to write", (False, "Hacker News") in rows_after
          and not any(b for b, _ in rows_after)
          and len(json.loads(marks.read_text())) == 2, f"{rows_after}")
    m.close()

    # What a keystroke costs: thousands of pages, queries of 1 to 5 characters.
    big = SCRATCH / "addr" / "big.sqlite"
    import random
    random.seed(7)
    words = ("video music news python linux arch wiki docs github issue pull release "
             "weather recipe şeker yemek tarifi map train ticket bank mail chat").split()
    hosts = [f"{w}{i}.example.org" for i, w in enumerate(words)] + ["www.youtube.com",
                                                                   "github.com"]
    rows_ = []
    for i in range(10_000):
        host = random.choice(hosts)
        path = "/".join(random.choices(words, k=random.randint(1, 4)))
        title = " ".join(random.choices(words, k=random.randint(2, 6))).title()
        rows_.append((f"https://{host}/{path}/{i}", title, random.randint(1, 4),
                      random.randint(0, 120 * 86400)))
    for n in (5_000, 10_000):
        target = SCRATCH / "addr" / f"big-{n}.sqlite"
        _history(target, rows_[:n])
        big_mode = Addresses(history=target, bookmarks=marks, now=NOW)
        times = []
        for query in ("", "y", "yo", "you", "yout", "youtu", "g", "gi", "git", "gith",
                      "w", "we", "wea", "weat", "şe", "şek", "zzzz"):
            t0 = time.perf_counter()
            big_mode.search(query)
            times.append((time.perf_counter() - t0) * 1000)
        big_mode.close()
        times.sort()
        check(f"a search with {n:,} pages of history: median and worst per keystroke",
              times[-1] < 40, f"median {times[len(times) // 2]:.1f} ms, worst {times[-1]:.1f} ms "
              f"over {len(times)} queries (first open included)")


# ── 3. the input and its list ───────────────────────────────────────────

class FakeWindow:
    """What a widget asks of its window, and no X."""

    def __init__(self, name="launcher"):
        self.spec = types.SimpleNamespace(name=name)
        self.keyboard_grabbed = False
        self.sizes = []

    def resize_content(self, w, h):
        self.sizes.append((w, h))

    def damage(self, *_a, **_k):
        pass


def key(sym, text="", **mods):
    from indigoshell.widgets.base import KeyEvent
    return KeyEvent(keysym=sym, text=text, **mods)


def typed(s):
    from indigoshell.widgets.base import KeyEvent
    return [KeyEvent(keysym=ord(c) if ord(c) < 0x100 else 0x01000000 + ord(c), text=c) for c in s]


RETURN, ESC, BKSP, DOWN, UP, LEFT, RIGHT = 0xFF0D, 0xFF1B, 0xFF08, 0xFF54, 0xFF52, 0xFF51, 0xFF53
DELETE = 0xFFFF


class Timer:
    def __init__(self, host, delay, fn):
        self.host, self.delay, self.fn = host, delay, fn

    def cancel(self):
        if self in self.host.timers:
            self.host.timers.remove(self)


class PromptHost:
    """What a Prompt asks of the app service: its list's changes, its
    closing, the query seq, a timer (run by hand here)."""

    def __init__(self):
        self.seq, self.timers, self.closed_, self.changes = 0, [], [], 0

    def changed(self, p):
        self.changes += 1

    def closed(self, p):
        self.closed_.append(p)

    def next_seq(self, p):
        self.seq += 1
        return self.seq

    def later(self, delay, fn):
        t = Timer(self, delay, fn)
        self.timers.append(t)
        return t

    def fire(self):
        for t in list(self.timers):
            self.timers.remove(t)
            t.fn()


class Owner:
    """An app window, as a prompt sends to it."""

    def __init__(self, bar=None):
        self.out, self.bar = [], bar or {}

    def send_event(self, e):
        self.out.append(e)

    def of(self, kind):
        return [m for m in self.out if m["type"] == kind]


def stand_in_bar(width=1800, left=12, bottom=42):
    """The top bar as the service sees it (its context area's window and
    rect): no window of it is made here."""
    import skia
    window = types.SimpleNamespace(wid=1, x=0, y=0, width=2560, height=bottom,
                                   content_root=(0, 0, 2560, bottom))
    return types.SimpleNamespace(window=window, rect=skia.Rect.MakeXYWH(left, 0, width, bottom))


def render_prompt(prompt, name: str, width=1200, bar_h=42):
    """The box (an input's) over its list, painted off screen at 1x as
    the bar and the drop-down would show them; RGB."""
    import numpy as np
    import skia

    from indigoshell import theme
    from indigoshell.applink.drop import DropList
    from indigoshell.widgets.base import Size
    drop = DropList()
    drop.show(prompt, width)
    w, h = drop.relayout()
    total_w = max(width, w)
    surface = skia.Surface(total_w, bar_h + h)
    canvas = surface.getCanvas()
    canvas.clear(theme.color(theme.BAR_BG))
    if prompt.box is not None:
        prompt.box.measure(Size(width, bar_h))
        prompt.box.arrange(skia.Rect.MakeWH(width, bar_h))
        prompt.box.paint(canvas)
    if prompt.wants_list():
        canvas.save()
        canvas.translate(0, bar_h)
        drop.arrange(skia.Rect.MakeWH(w, h))
        drop._opened_at = 0.0
        drop.paint(canvas)
        canvas.restore()
    image = surface.makeImageSnapshot()
    image.save(str(SCRATCH / f"{name}.png"), skia.kPNG)
    return np.array(image.toarray(colorType=skia.kRGBA_8888_ColorType))[..., :3].astype(int)


def colour_px(img, hex_, tol=60):
    import numpy as np
    rgb = np.array([int(hex_[i:i + 2], 16) for i in (1, 3, 5)])
    return int((np.abs(img - rgb).max(-1) < tol).sum())


def test_input() -> None:
    from indigoshell import theme
    from indigoshell.applink import bar as B
    from indigoshell.applink.prompt import Prompt

    section("3. an input and its list, driven with KeyEvents (the app's answers by hand)")
    host, app = PromptHost(), Owner()
    remove = {"remove": B.action("shift+delete", drops=True)}
    spec = B.input_(chords=["ctrl+l"], icon="search", placeholder="address or search",
                    text="https://x.org/cats", select=True, query=True, actions=remove)
    p = Prompt(host, app, "input", "address", spec, chord="ctrl+l")
    p.start()
    check("opened: the box with the input's text, selected; the first query out at once "
          "(seq 1, that text); no list until rows come",
          p.box.field.text == "https://x.org/cats" and p.box.field.selected_all
          and app.out == [{"type": "query", "input": "address", "seq": 1,
                           "text": "https://x.org/cats"}] and not p.wants_list(), f"{app.out}")
    for ev in typed("y"):
        p.key(ev)
    first = p.text
    for ev in typed("o"):
        p.key(ev)
    check("typing: the first key replaces the selection, each key shows at once in the box; "
          "seq 1 still out, so no second query yet (one in flight)",
          first == "y" and p.text == "yo" and len(app.of("query")) == 1 and p.dirty)
    p.answer(B.answer("address", 1, [B.row("typed", "https://x.org/cats", tag="OPEN")], total=9))
    q = app.of("query")
    check("seq 1 answered: its rows shown, the list wanted; only then the newest text goes "
          "(seq 2, \"yo\"), the in-between ones never",
          p.wants_list() and [r["id"] for r in p.rows] == ["typed"]
          and q[-1] == {"type": "query", "input": "address", "seq": 2, "text": "yo"}
          and len(q) == 2, f"{q}")
    rows = [B.row("typed", "yo", tag="SEARCH"),
            B.row("https://www.youtube.com/", "YouTube", detail="youtube.com", icon="history",
                  match=[0, 1]),
            B.row("https://yo.example/", "Yo marks", detail="yo.example", icon="bookmark",
                  actions=["remove"])]
    p.answer(B.answer("address", 2, rows, selected="https://www.youtube.com/", total=5321))
    p.answer(B.answer("address", 1, [B.row("old", "stale")], total=1))
    p.answer(B.answer("address", 7, [B.row("future", "never asked")], total=1))
    check("seq 2's answer: the selection on the row it names, the count its untagged rows over "
          "the total; a stale answer (seq 1) and one never asked for (seq 7) dropped",
          p.selected == 1 and [r["id"] for r in p.rows][1] == "https://www.youtube.com/"
          and p.count_text()[0] == "2/5321" and p.box.count[0] == "2/5321",
          f"{p.selected} {p.count_text()}")
    img = render_prompt(p, "input-yo")
    check("painted: the box lit over the list, the selection slab on YouTube (cyan)",
          colour_px(img[:42], theme.FG_STRONG, 40) > 300 and colour_px(img[42:], theme.CYAN_BRIGHT, 90)
          > 200, f"{colour_px(img[42:], theme.CYAN_BRIGHT, 90)} cyan px")

    # A row action that drops its row: Shift+Delete on the bookmark.
    p.key(key(DOWN))
    n = len(app.out)
    p.key(key(DELETE, shift=True))
    sent = app.out[n:]
    check("Shift+Delete on a row that allows `remove`: `act` with its id and the text, the row "
          "gone at once, the text queried again (the answer is the truth)",
          sent[0] == {"type": "act", "input": "address", "action": "remove",
                      "row": "https://yo.example/", "text": "yo"}
          and "https://yo.example/" not in [r["id"] for r in p.rows]
          and sent[1] == {"type": "query", "input": "address", "seq": 3, "text": "yo"},
          f"{sent}")
    p.answer(B.answer("address", 3, rows[:2], selected="https://www.youtube.com/", total=5320))
    p.select(0)
    n = len(app.out)
    p.key(key(DELETE, shift=True))
    p.key(key(0xFF50))                          # Home
    p.key(key(DELETE))
    check("... on a row that does not: refused with the flash, nothing sent but the text queried "
          "as plain Delete edits it", app.out[n:] == [{"type": "query", "input": "address",
                                                      "seq": 4, "text": "o"}]
          and p.reject_until > 0 and p.text == "o", f"{app.out[n:]}")
    p.answer(B.answer("address", 4, rows[:2], selected="https://www.youtube.com/", total=5320))
    p.box.field.set_text("yo")
    p.answer(B.answer("address", 5, rows[:2], selected="https://www.youtube.com/", total=5320))

    # Enter with the latest text unanswered waits for its answer.
    for ev in typed("u"):
        p.key(ev)
    n = len(app.out)
    p.key(key(RETURN))
    waiting = p.open and app.out[n:] == [] and p.enter is not None
    p.key(typed("x")[0])                        # nothing else counts meanwhile
    p.answer(B.answer("address", 6, [B.row("typed", "you", tag="SEARCH"),
                                     B.row("https://you.example/", "You")],
                      selected="https://you.example/"))
    check("Enter with the latest text unanswered: nothing sent until its answer; then the row "
          "that answer selected is submitted, and the input closes (`closed` by enter)",
          waiting and app.out[n:] == [
              {"type": "submit", "input": "address", "text": "you", "row": "https://you.example/",
               "shift": False}, {"type": "closed", "input": "address", "by": "enter"}]
          and not p.open and host.closed_ == [p], f"{app.out[n:]}")

    host2, app2 = PromptHost(), Owner()
    p2 = Prompt(host2, app2, "input", "new_tab", B.input_(chords=["ctrl+t"], query=True))
    p2.start()
    for ev in typed("ab"):
        p2.key(ev)
    p2.key(key(RETURN))
    pending = list(host2.timers)
    host2.fire()
    late = app2.out[-2:]
    check("Enter waiting past ENTER_WAIT_S with no answer: the text alone (row null), never a "
          "row of an older answer", [t.delay for t in pending] == [B.ENTER_WAIT_S]
          and late == [{"type": "submit", "input": "new_tab", "text": "ab", "row": None,
                        "shift": False}, {"type": "closed", "input": "new_tab", "by": "enter"}],
          f"{late}")
    p3 = Prompt(PromptHost(), app2, "input", "new_tab", B.input_(query=True))
    p3.start()
    p3.answer(B.answer("new_tab", 1, [B.row("x", "x")]))
    p3.key(key(RETURN, shift=True))
    p4 = Prompt(PromptHost(), app2, "input", "new_tab", B.input_(query=True))
    p4.start()
    p4.key(key(ESC))
    said = [m for m in app2.out[-4:] if m["type"] != "query"]
    check("Shift+Enter: the text itself (row null, shift); Escape: `closed` by escape",
          said == [{"type": "submit", "input": "new_tab", "text": "", "row": None, "shift": True},
                   {"type": "closed", "input": "new_tab", "by": "enter"},
                   {"type": "closed", "input": "new_tab", "by": "escape"}], f"{said}")

    # keep: Enter submits, the input stays; steps: Enter turns it to stepping.
    app3 = Owner()
    pk = Prompt(PromptHost(), app3, "input", "replace", B.input_(keep=True))
    pk.start()
    for ev in typed("x"):
        pk.key(ev)
    pk.key(key(RETURN))
    pk.key(key(RETURN, shift=True))
    check("keep: Enter submits and the input stays; Shift+Enter submits with shift",
          pk.open and [m.get("shift") for m in app3.of("submit")] == [False, True])
    app4 = Owner()
    find = B.input_(chords=["ctrl+f"], icon="search", placeholder="find", query=True, steps=True)
    pf = Prompt(PromptHost(), app4, "input", "find", find, chord="ctrl+f")
    pf.start()
    for ev in typed("cat"):
        pf.key(ev)
    pf.answer(B.answer("find", 1, count=B.count("3/12")))
    pf.answer(B.answer("find", 2, count=B.count("3/12")))
    typing_img = render_prompt(pf, "find-typing")
    counted = pf.box.count
    pf.key(key(RETURN))
    stepping = pf.stepping and pf.box.stepping and pf.open and not pf.wants_list()
    for ev in typed("nNq"):
        pf.key(ev)
    pf.answer(B.answer("find", 2, count=B.count("4/12")))
    stepping_img = render_prompt(pf, "find-stepping")
    subs = [(m["text"], m["row"], m["shift"]) for m in app4.of("submit")]
    check("steps (find): a count only, no list; Enter submits and turns the box to stepping "
          "(no caret, the text stays); n and N submit again (shift false / true), other keys "
          "swallowed; the latest answer coming again updates the count",
          counted == ("3/12", theme.CYAN_BRIGHT) and stepping and pf.text == "cat"
          and subs == [("cat", None, False), ("cat", None, False), ("cat", None, True)]
          and pf.box.count[0] == "4/12", f"{subs} {pf.box.count}")
    pf.key(key(ord("f"), ctrl=True))
    back = not pf.stepping and pf.open
    pf.key(key(RETURN))
    pf.key(key(RETURN))
    check("... its own chord again: back to typing; Enter while stepping: `closed` by enter",
          back and not pf.open and app4.out[-1] == {"type": "closed", "input": "find",
                                                    "by": "enter"}, f"{app4.out[-2:]}")
    check("find painted: the count at the box's right end, cyan; stepping drops the caret",
          colour_px(typing_img[:42, -80:], theme.CYAN_BRIGHT, 40) > 10
          and colour_px(stepping_img[:42, -80:], theme.CYAN_BRIGHT, 40) > 10)

    # Static rows: filtered here, no round trip.
    app5 = Owner()
    palette = B.input_(chords=["ctrl+shift+p"], placeholder="command", rows=[
        B.row("fmt", "Format Document", chord="ctrl+shift+i"), B.row("save", "Save All"),
        B.row("off", "Toggle Nothing", enabled=False)])
    pp = Prompt(PromptHost(), app5, "input", "palette", palette)
    pp.start()
    all_rows = [r["id"] for r in pp.rows]
    for ev in typed("sav"):
        pp.key(ev)
    filtered = [r["id"] for r in pp.rows]
    pp.key(key(RETURN))
    check("static rows: all of them in their order with nothing typed; filtered by the shell's "
          "fuzzy match as typed (no query sent); Enter submits the row",
          all_rows == ["fmt", "save", "off"] and filtered == ["save"] and not app5.of("query")
          and app5.out[0] == {"type": "submit", "input": "palette", "text": "sav", "row": "save",
                              "shift": False}, f"{filtered} {app5.out[:1]}")
    pq = Prompt(PromptHost(), app5, "input", "palette", palette)
    pq.start()
    for ev in typed("tog"):
        pq.key(ev)
    n = len(app5.out)
    pq.key(key(RETURN))
    check("... a row that cannot be picked: the flash, nothing sent, still open",
          pq.open and app5.out[n:] == [] and pq.reject_until > 0)
    pq.close("escape")

    # Toggles in the box.
    app6 = Owner()
    pt = Prompt(PromptHost(), app6, "input", "find", B.input_(query=True, buttons=[
        B.toggle("case", "case", chord="alt+c"), B.toggle("regex", "regex", on=True)]))
    pt.key(key(ord("c"), alt=True))
    pt.box.measure(__import__("indigoshell.widgets.base", fromlist=["Size"]).Size(800, 42))
    check("a toggle's chord: `press` for it, naming the input",
          app6.of("press") == [{"type": "press", "item": "case", "input": "find"}])


def test_menus() -> None:
    from indigoshell.applink import bar as B
    from indigoshell.applink.prompt import Prompt

    section("4. menus: the same list, filtered by typing, a submenu beside its row")
    menus = {
        "file": B.menu([B.row("open", "Open…", chord="ctrl+o"),
                        B.row("recent", "Open Recent", menu="recent"),
                        B.row("save", "Save", chord="ctrl+s"),
                        B.row("close", "Close Window", enabled=False)], chords=["alt+f"]),
        "recent": B.menu([B.row("/notes.md", "notes.md", detail="~/notes"),
                          B.row("/todo.md", "todo.md", detail="~/notes")])}
    app = Owner({"menus": menus})
    p = Prompt(PromptHost(), app, "menu", "file", menus["file"], chord="alt+f")
    p.start()
    start = ([r["id"] for r in p.list_rows()], p.level.selected, p.wants_list())
    p.key(key(DOWN))
    p.key(key(RIGHT))
    opened = [lv.name for lv in p.levels]
    img = render_prompt(p, "menu-submenu", width=400)
    p.key(key(DOWN))
    sub_sel = p.selected_row()["id"]
    p.key(key(LEFT))
    back = ([lv.name for lv in p.levels], p.selected_row()["id"])
    check("a menu: its rows in order, the list at once, the first selected; Down, then Right on "
          "a row with `menu` opens the submenu beside it (Down moves in it); Left goes back",
          start == (["open", "recent", "save", "close"], 0, True) and opened == ["file", "recent"]
          and sub_sel == "/todo.md" and back == (["file"], "recent"),
          f"{start} {opened} {sub_sel} {back}")
    check("... painted as two columns, the submenu to the right of the menu",
          img.shape[1] >= 600, f"{img.shape}")
    for ev in typed("sv"):
        p.key(ev)
    filtered = ([r["id"] for r in p.list_rows()], p.level.filter)
    img = render_prompt(p, "menu-filtered", width=400)
    p.key(key(BKSP))
    p.key(key(BKSP))
    unfiltered = [r["id"] for r in p.list_rows()]
    check("typing filters it (the filter line at its top); Backspace takes it back",
          filtered == (["save"], "sv") and unfiltered == ["open", "recent", "save", "close"],
          f"{filtered}")
    p.select(3)
    n = len(app.out)
    p.key(key(RETURN))
    refused = app.out[n:] == [] and p.open and p.reject_until > 0
    p.select(2)
    p.key(key(RETURN))
    check("Return on a dimmed row: the flash, nothing sent; on another: `pick` (menu, row), "
          "then `closed` by enter", refused and app.out[n:] == [
              {"type": "pick", "menu": "file", "row": "save"},
              {"type": "closed", "menu": "file", "by": "enter"}], f"{app.out[n:]}")
    q = Prompt(PromptHost(), app, "menu", "file", menus["file"])
    q.select(1)
    q.key(key(RETURN))
    q.key(key(RETURN))
    check("Return on a row with `menu` opens it too; Return in the submenu picks there (its "
          "menu named)", app.out[-2] == {"type": "pick", "menu": "recent", "row": "/notes.md"})
    r = Prompt(PromptHost(), app, "menu", "file", menus["file"])
    r.key(key(ESC))
    check("Escape at the first level: `closed` by escape",
          app.out[-1] == {"type": "closed", "menu": "file", "by": "escape"})


def test_apps_search() -> None:
    from indigoshell.applink import catalog as catalog_mod
    from indigoshell.applink.catalog import RUN, AppSearch
    from indigoshell.applink.prompt import Prompt
    from indigoshell.applink.service import AppService
    from indigoshell.applink.view import BarView
    from indigoshell.services.apps import Catalog

    section("3b. MOD+D: the app search, the same input in the top bar, the installed apps its "
            "rows (applink/catalog.py)")
    apps = SCRATCH / "apps" / "applications"
    apps.mkdir(parents=True)
    for name in ("Alpha Editor", "Beta Browser", "Gamma Terminal"):
        (apps / f"{name.split()[0].lower()}.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec=true\n")
    os.environ["XDG_DATA_DIRS"] = str(SCRATCH / "apps")
    os.environ["XDG_DATA_HOME"] = str(SCRATCH / "no-user-apps")
    catalog = Catalog()
    launched, ran = [], []
    catalog.launch = lambda app: launched.append(app.entry.name)

    def run(line):
        if line.count('"') % 2:
            raise ValueError("unbalanced")
        ran.append(line)
    catalog.run = run
    host = PromptHost()

    def search():
        p = Prompt(host, None, "input", "apps", AppSearch(catalog).spec, local=AppSearch(catalog))
        p.start()
        return p

    p = search()
    names = [r["title"] for r in p.rows]
    check("the apps, the usual placeholder, an empty box, the count of them",
          names == ["Alpha Editor", "Beta Browser", "Gamma Terminal"]
          and p.box.field.placeholder == catalog_mod.PLACEHOLDER and p.text == ""
          and p.count_text()[0] == "3/3" and p.wants_list(), f"{names}")
    for ev in typed("bet"):
        p.key(ev)
    filtered = [r["title"] for r in p.rows]
    img = render_prompt(p, "apps-bet", width=900)
    p.key(key(RETURN))
    check("typing filters (the matched characters lit); Return launches the selected app and "
          "closes", filtered == ["Beta Browser"] and launched == ["Beta Browser"]
          and not p.open and p.rows[0].get("match") == [0, 1, 2]
          and colour_px(img[42:], "#05d9e8", 90) > 100, f"{filtered} {launched}")
    p = search()
    for ev in typed("zzz"):
        p.key(ev)
    run_row = [(r["id"], r.get("tag"), r["title"]) for r in p.rows]
    p.key(key(RETURN))
    q = search()
    for ev in typed("bet ls"):
        q.key(ev)
    q.key(key(RETURN, shift=True))
    r = search()
    for ev in typed('bad "quote'):
        r.key(ev)
    r.key(key(RETURN))
    check("nothing matching: one row, RUN the text, and Return runs it as a command line; "
          "Shift+Return runs the text whatever is listed; a command line that is none flashes "
          "and the search stays", run_row == [(RUN, "RUN", "zzz")] and ran == ["zzz", "bet ls"]
          and not q.open and r.open and r.reject_until > 0, f"{run_row} {ran}")
    r.key(key(ESC))
    check("Escape closes it", not r.open)

    # The service: the app search in the top bar's app section.
    service = AppService(path="/nonexistent")   # no X here: no display
    service.context = stand_in_bar()
    shown, followed = [], []
    service.show_drop = lambda prompt, place: shown.append((prompt.name, place))
    service.hide_drop = lambda: shown.append("hide")
    service.follow(lambda w: followed.append(service.shown(w)))
    focused = types.SimpleNamespace(content=BarView())
    service.focused = focused
    drop = types.SimpleNamespace(catalog=catalog, icons=None)
    ok = service.open_apps(drop, None)
    p = service.prompt
    check("MOD+D (the drop-down opened by name with nothing to show): the app search in the top "
          "bar; the focused app's section gives way to the shell's own while it is open; the "
          "list drops at the section's left, the section's width",
          ok and p is not None and p.owner is None and service.shown(focused)
          is service.shell_view and service.shell_view.box is p.box
          and followed[-1] is service.shell_view and shown[-1] == ("apps", (12, 42, 1800)),
          f"{shown[-1:]}")
    p.key(key(ESC))
    check("... closed: the app's section back, the list hidden",
          service.prompt is None and followed[-1] is focused.content
          and service.shell_view.box is None and shown[-1] == "hide")
    service.context = None
    check("no top bar: no app search", service.open_apps(drop, None) is False
          and service.prompt is None)


# ── 5. Flux's bar ───────────────────────────────────────────────────────

def test_flux_bar() -> None:
    from flux import bar as fb
    from indigoshell.applink import bar as B

    section("5. what Flux sends the shell (flux/bar.py, pure): a window's state as its bar")
    tabs = [fb.tab("Inbox", "mail.example", False, id_=40),
            fb.tab("std - Rust", "doc.rust-lang.org", True, id_=41),
            fb.tab("Weather", "example.org", False, id_=44)]
    st = fb.state(tabs, 1, 62, False, url="http://doc.rust-lang.org/std/", bookmarked=True,
                  agent=[None, None, {"voice": "lilac", "state": "driving"}])
    bar = fb.bar_of(st)
    t = bar["start"][0]
    check("the tabs: one per page by its id, the current one's load as its progress, the agent's "
          "hold; a main window's can be closed, moved and torn off",
          t["kind"] == "tabs" and [x["id"] for x in t["tabs"]] == [40, 41, 44]
          and t["current"] == 41 and t["tabs"][1]["progress"] == 62 and t["tabs"][1]["busy"]
          and t["tabs"][2]["agent"] == {"voice": "lilac", "state": "driving"}
          and t["can"] == ["close", "move", "detach"], f"{t}")
    host = bar["end"][-1]
    check("the host: a bookmark, an open padlock in `error` (plain http), the host",
          host["spans"] == [{"icon": "bookmark"}, {"icon": "unlocked", "color": "error"},
                            {"text": "doc.rust-lang.org"}], f"{host}")
    inputs, menus = bar["inputs"], bar["menus"]
    check("the inputs: Ctrl+L the address (selected, answered by query, a bookmark's row taken "
          "out with Shift+Delete), Ctrl+T a new tab's, Ctrl+F find (steps); Ctrl+M the actions",
          B.chords(bar) == {"ctrl+l": ("input", "address"), "ctrl+t": ("input", "new_tab"),
                            "ctrl+f": ("input", "find"), "ctrl+m": ("menu", "actions")}
          and inputs["address"]["text"] == "http://doc.rust-lang.org/std/"
          and inputs["address"]["select"] and inputs["address"]["query"]
          and inputs["address"]["actions"] == {"remove": {"chord": "shift+delete", "drops": True}}
          and "text" not in inputs["new_tab"] and inputs["find"]["steps"]
          and inputs["find"]["query"], f"{inputs}")
    rows = menus["actions"]["rows"]
    check("the actions for a bookmarked page: Add dimmed, Remove applies; the bar is good",
          [(r["id"], r.get("enabled", True)) for r in rows] == [("add", False), ("remove", True)]
          and rows[0]["detail"] == "doc.rust-lang.org" and B.bar_problems(bar) == [],
          f"{rows} {B.bar_problems(bar)}")
    st2 = dict(st, agent=[None, {"voice": "indigo", "state": "paused"}, None])
    rows = fb.bar_of(st2, names={"indigo": "Indigo"})["menus"]["actions"]["rows"]
    check("a tab the user took over: \"Let Indigo drive again\" first (the name the shell gave), "
          "its id naming the tab, which and where in its detail",
          rows[0] == {"id": "resume:41", "title": "Let Indigo drive again",
                      "detail": "tab 2 · doc.rust-lang.org", "icon": "play"}, f"{rows[0]}")
    blank = fb.bar_of(fb.state([fb.tab("", "", False, id_=1)], 0, 100, True, url="about:blank",
                               bookmarked=False))
    check("a blank page: the address input empty, nothing selected; neither bookmark action "
          "applies", "text" not in blank["inputs"]["address"]
          and not blank["inputs"]["address"].get("select")
          and [r.get("enabled", True) for r in blank["menus"]["actions"]["rows"]] == [False, False])
    single = fb.bar_of(st, single=True)
    check("a single window (a popup, the inspector): its tabs can do nothing, no inputs, no menus "
          "(its keys stay its own)", single["start"][0]["can"] == [] and single["inputs"] == {}
          and single["menus"] == {} and B.chords(single) == {})
    hers = fb.bar_of(st, agent="Lilac")
    check("the agent's own window: its name first, in its voice's colour",
          hers["start"][0] == {"kind": "text", "id": "owner", "text": "Lilac",
                               "color": "voice:lilac"} and hers["start"][1]["kind"] == "tabs")
    own = fb.bar_of(dict(st, find=fb.find("cats", 3, 12, True)))
    item = own["end"][0]
    check("Flux's own find open (no shell showed its input): its line before the host",
          item["id"] == "find" and item["spans"][1]["text"] == "cats\u258f"
          and item["spans"][2] == {"text": "  3/12", "color": "info"}
          and B.bar_problems(own) == [], f"{item}")
    check("the same state, the same bar (what the link compares before sending)",
          fb.bar_of(st) == fb.bar_of(dict(st)))
    many = fb.state([fb.tab(f"A page title of some length {i}", "example.org", False, id_=i)
                     for i in range(30)], 3, 100, True, url="https://example.org/", bookmarked=False)
    size = len(json.dumps(fb.bar_of(many)))
    t0 = time.perf_counter()
    for _ in range(100):
        json.loads(json.dumps(fb.bar_of(many)))
    took = (time.perf_counter() - t0) / 100 * 1e6
    check("30 tabs: a few KB on the line", size < 6000, f"{size} bytes; {took:.0f} us to make, "
          "write and read one")


# ── 6. placement ────────────────────────────────────────────────────────

def test_placement() -> None:
    from indigoshell.backend.x11 import Monitor
    from indigoshell.window import Placement, Window, WindowSpec

    section("6. placement: centred under a rect, on its monitor, forgotten on park")

    class Display:
        screen = types.SimpleNamespace(root_depth=24)

        def monitors(self):
            return [Monitor(0, 0, 2560, 1440, True, "A"), Monitor(2560, 200, 1920, 1080, False, "B")]

        def primary_monitor(self):
            return self.monitors()[0]

    spec = WindowSpec(name="t", size=(760, 560))
    win = Window(Display(), spec)
    centre = win._compute_geometry(Display().primary_monitor())
    win.placement = Placement.below(100, 300, 1000, 28)
    under = win._compute_geometry(Display().primary_monitor())
    win.placement = Placement.below(2560 + 1600, 900, 300, 28)
    clamped = win._compute_geometry(Display().primary_monitor())
    win.placement = Placement.below(-500, -300, 100, 28)
    off = win._compute_geometry(Display().primary_monitor())
    check("no placement: the anchor's place (centred on the primary)",
          centre == ((2560 - 760) // 2, (1440 - 560) // 2, 760, 560), f"{centre}")
    check("below a 28 px rect at (100, 300) 1000 px wide: centred on it, top at its bottom",
          under == (100 + 500 - 380, 328, 760, 560), f"{under}")
    check("near the right edge of the second monitor: kept inside that monitor",
          clamped == (2560 + 1920 - 760, 200 + 1080 - 560, 760, 560), f"{clamped}")
    check("a point on no monitor: the primary, clamped", off == (0, 0, 760, 560), f"{off}")
    win.park()
    check("park() forgets it: the next open goes by the anchor", win.placement is None
          and win._compute_geometry(Display().primary_monitor()) == centre)

    # Outside clicks are tested against the content rect in the window's
    # own coordinates, so where the window sits does not change them.
    closes = []
    dspec = WindowSpec(name="t2", size=(760, 560), dismiss_on_outside_click=True)
    w2 = Window(Display(), dspec)
    w2.placement = Placement(1000, 500)
    import skia
    w2.content_rect = skia.Rect.MakeXYWH(30, 30, 760, 560)
    w2._request_close = lambda: closes.append(1)
    w2._dismiss_others = lambda: None
    w2._on_press(types.SimpleNamespace(event_x=10, event_y=40, detail=1))
    w2._on_press(types.SimpleNamespace(event_x=400, event_y=300, detail=1))
    check("a placed window: a press outside its content closes it, one inside does not",
          closes == [1])


# ── 7. on screen ────────────────────────────────────────────────────────

class Fake:
    """A stand-in browser on the shell's app socket."""

    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.setblocking(False)
        self.buf = b""

    def send(self, msg: dict) -> None:
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def read(self) -> list[dict]:
        try:
            self.buf += self.sock.recv(65536)
        except BlockingIOError:
            pass
        *lines, self.buf = self.buf.split(b"\n")
        return [json.loads(x) for x in lines if x]

    def close(self) -> None:
        self.sock.close()


async def until(pred, timeout=5.0, every=0.01):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(every)


async def on_screen() -> None:
    import numpy as np
    import skia
    import xcffib
    import xcffib.composite
    import xcffib.xproto as xp

    from indigoshell import config_default, theme
    from indigoshell.applink import bar as B
    from indigoshell.applink.drop import DROP_WINDOW
    from indigoshell.applink.prompt import Prompt
    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from indigoshell.core import daemon as daemon_mod
    from indigoshell.window import Window
    from flux import bar as fb

    section("7. on screen: the drop-down under a stand-in top bar, for a stand-in browser")
    real = next(s_ for s_ in config_default.WINDOWS if s_.name == DROP_WINDOW)
    check("the drop-down's spec takes no keyboard, no pointer, no focus (the open input holds "
          "them), and has no effect around it", not real.grab_keyboard
          and not real.dismiss_on_outside_click and not real.focusable
          and real.override_redirect and real.effects == () and real.keep_alive)
    # When a window is mapped with its first frame drawn: what show()
    # does before it returns.
    presented: dict[str, list[float]] = {}
    original = Window.show

    def show(self):
        result = original(self)
        presented.setdefault(self.spec.name, []).append(time.monotonic())
        return result

    Window.show = show
    loop = asyncio.get_running_loop()
    display = Display()
    display.attach_loop(loop)
    # Never the person's keyboard or pointer: both grabs recorded only.
    grabs = []
    display.grab_pointer = lambda wid: grabs.append(("pointer", wid)) or True
    display.ungrab_pointer = lambda: grabs.append("pointer let go")

    def pump():
        while display.pump():
            pass

    loop.add_reader(display.fd, pump)
    d = daemon_mod.Daemon({"windows": [real]})
    d.display, d.loop = display, loop
    sock = os.path.join(runtime_dir(), f"{APP}-blt-{os.getpid()}-apps.sock")
    service = AppService(path=sock, display=display)
    kb = []
    service.hold_keyboard = lambda w: kb.append(("hold", w.wid)) or True
    service.release_keyboard = lambda: kb.append("release")
    service.start()
    other = xcffib.connect()
    screen = other.get_setup().roots[0]
    composite = other(xcffib.composite.key)
    composite.QueryVersion(0, 4).reply()
    mon = display.primary_monitor()
    # A stand-in top bar in the bottom-right corner (no window with a
    # strut is ever made here), its app section 600 px wide; the list
    # drops from its bottom edge, small, in that corner.
    bar_x, bar_y = mon.x + mon.width - 700, mon.y + mon.height - 420
    bar_win = types.SimpleNamespace(wid=1, x=bar_x, y=bar_y, width=680, height=42,
                                    content_root=(bar_x, bar_y, 680, 42), damage=lambda *a, **k: None)
    service.context = types.SimpleNamespace(window=bar_win, rect=skia.Rect.MakeXYWH(40, 0, 600, 42))
    bx, by, bw, bh = mon.x + mon.width - 600, mon.y + mon.height - 360, 400, 300
    stand = other.generate_id()
    # Unmapped: the stand-in only has to exist and have a place.
    other.core.CreateWindow(screen.root_depth, stand, screen.root, bx, by, bw, bh, 0,
                            xp.WindowClass.InputOutput, screen.root_visual,
                            xp.CW.BackPixel | xp.CW.OverrideRedirect, [0x00FF00, 1])
    other.core.GetInputFocus().reply()
    fake = None
    real_answer = Prompt.answer
    answering = None
    try:
        fake = Fake(sock)
        fake.send(B.announce("flux", 1, stand))
        fake.send(fb.bar_of(fb.state([fb.tab("Stand-in", "example.org", False, id_=1)], 0, 100,
                                     True, url="https://example.org/", bookmarked=False)))
        said: list[dict] = []

        async def answer_queries():
            """The stand-in browser answers each query as Flux would: what
            was typed, as the row to open."""
            while True:
                for m in fake.read():
                    said.append(m)
                    if m.get("type") == "query":
                        fake.send(B.answer(m["input"], m["seq"], [
                            B.row("typed", m["text"] or "nothing", tag="OPEN")], total=0))
                await asyncio.sleep(0.002)

        answering = asyncio.ensure_future(answer_queries())
        attached = await until(lambda: any(m.get("type") == "attached" for m in said), 5)
        grabbed = await until(lambda: stand in service.grabs, 2)
        jw = service.window_for(stand)
        service.focused = jw
        check("the stand-in browser attached, its keys grabbed on its window",
              bool(attached) and bool(grabbed))

        def press(sym, state=0):
            """A key press as the X server reports one to the service while
            its grab holds the keyboard: on the browser's window."""
            ev = xp.KeyPressEvent.synthetic(display.keycodes(sym)[0], 0, display.root, stand,
                                            0, 0, 0, 0, 0, state, 1)
            ev.response_type = 2
            display.dispatch(ev)

        boxed_at = []
        real_show_box = jw.view.show_box

        def show_box(box):
            if box is not None:
                boxed_at.append(time.perf_counter())
            real_show_box(box)

        jw.view.show_box = show_box
        answered_at = []

        def answer(self, msg):
            answered_at.append(time.monotonic())
            real_answer(self, msg)

        Prompt.answer = answer

        async def ctrl_l():
            presented.pop(DROP_WINDOW, None)
            boxed_at.clear()
            answered_at.clear()
            t0 = time.perf_counter()
            press(ord("l"), xp.ModMask.Control)
            boxed = (boxed_at[0] - t0) * 1000 if boxed_at else None
            ok = await until(lambda: presented.get(DROP_WINDOW), 5)
            return boxed, (presented[DROP_WINDOW][0] - answered_at[0]) * 1000 \
                if ok and answered_at else None

        cold_box, cold = await ctrl_l()
        win = d.instances.get(DROP_WINDOW)
        cx, cy, cw, ch = win.content_root if win else (0, 0, 0, 0)
        check("Ctrl+L: the box in the section at once, the press handled (the bar paints it at "
              "its next frame); the keyboard held for it, the pointer too",
              cold_box is not None and cold_box < 16 and kb[:1] == [("hold", stand)]
              and grabs[:1] == [("pointer", 1)], f"{cold_box} ms; {kb} {grabs}")
        check("the list, at the first answer: a window of its own, flush under the bar's bottom "
              "edge at the section's left, the section's width, no bleed",
              win is not None and (cx, cy, cw) == (bar_x + 40, bar_y + 42, 600) and win.bleed == 0,
              f"content {cw}x{ch}+{cx}+{cy}")
        await asyncio.sleep(0.25)
        done = read_window(other, composite, win.wid, win.width, win.height)
        save_png(SCRATCH / "on-screen-drop.png", done)
        bg = np.array([int(theme.BAR_BG[i:i + 2], 16) for i in (1, 3, 5)])
        edge = done[-6:, :, :3].astype(int)
        share = float((np.abs(edge - bg).max(-1) <= 6).mean())
        cyan = colour_px(done[..., :3].astype(int), theme.CYAN_BRIGHT, 90)
        check("its own pixels: the bar's background round the rows, the typed row selected (the "
              "cyan slab)", share > 0.95 and cyan > 100, f"background {share:.0%}, cyan px {cyan}")
        press(ord("e"))
        press(ord("x"))
        typed_in = service.prompt.box.field.text if service.prompt else None
        press(0xFF1B)
        press(ord("q"))
        parked = await until(lambda: DROP_WINDOW in d._parked, 3)
        check("keys typed after the press, on the browser's window: into the box in order, "
              "replacing the selected URL; Escape among them closes it (the list parked, the "
              "keyboard and pointer given back), and a key after that goes nowhere",
              typed_in == "ex" and bool(parked) and service.prompt is None
              and kb[-1] == "release" and grabs[-1] == "pointer let go" and jw.view.box is None,
              f"{typed_in!r}")
        warm_box, warm = await ctrl_l()
        press(0xFF1B)
        await until(lambda: DROP_WINDOW in d._parked, 3)
        print(f"  (timing) key press -> the box in the section: {cold_box:.2f} ms, "
              f"{warm_box:.2f} ms; the first answer -> the list mapped with its first frame: "
              f"{cold:.0f} ms cold (a new window), {warm:.0f} ms from parked", flush=True)
        check("the list's first frame within 50 ms of the answer, from parked",
              warm is not None and warm < 50, f"{warm:.0f} ms")

        # MOD+D: `indigoshell toggle launcher`, the drop-down opened by name.
        real.content.service = service
        display.grab_keyboard = lambda wid: grabs.append(("keyboard", wid)) or True
        presented.pop(DROP_WINDOW, None)
        t0 = time.monotonic()
        toggled = d.toggle(DROP_WINDOW)
        ok = await until(lambda: presented.get(DROP_WINDOW), 5)
        took = (presented[DROP_WINDOW][0] - t0) * 1000 if ok else None
        win = d.instances.get(DROP_WINDOW)
        cx, cy, cw, ch = win.content_root if win else (0, 0, 0, 0)
        p = service.prompt
        listed = [r["title"] for r in p.rows] if p is not None else None
        check("MOD+D (toggle launcher): the drop-down opened by name is the app search: its box "
              "in the shell's own section (an app's bar gives way), the apps listed, flush under "
              "the bar at the section's left and width; the keyboard taken on the bar's window "
              "(recorded, never made)", toggled == "opened" and p is not None
              and p.owner is None and service.shell_view.box is p.box
              and listed == ["Alpha Editor", "Beta Browser", "Gamma Terminal"]
              and (cx, cy, cw) == (bar_x + 40, bar_y + 42, 600) and ("keyboard", 1) in grabs,
              f"{toggled} {listed} {(cx, cy, cw)}")
        for c in "bet":
            ev = xp.KeyPressEvent.synthetic(display.keycodes(ord(c))[0], 0, display.root, 1,
                                            0, 0, 0, 0, 0, 0, 1)
            ev.response_type = 2
            display.dispatch(ev)
        typed_rows = [r["title"] for r in p.rows] if p is not None else None
        toggled_again = d.toggle(DROP_WINDOW)
        closed = await until(lambda: DROP_WINDOW in d._parked, 3)
        check("its keys on the bar's window, into its box in order (filtering the apps); the "
              "toggle again closes it, and the shell's own section with it",
              typed_rows == ["Beta Browser"] and toggled_again == "closed" and bool(closed)
              and service.prompt is None and service.shell_view.box is None, f"{typed_rows}")
        print(f"  (timing) MOD+D's toggle -> the app search's list mapped with its first frame: "
              f"{took:.0f} ms (from parked)", flush=True)
        check("... its first frame within 100 ms of the toggle, from parked",
              took is not None and took < 100, f"{took:.0f} ms")

        # Made ahead (what the service asks of the daemon after its start):
        # parked unshown, and no app search for it.
        d._parked.pop(DROP_WINDOW).destroy()
        n = len(grabs)
        d.prepare(DROP_WINDOW)
        prepared = DROP_WINDOW in d._parked and DROP_WINDOW not in d.instances
        check("the drop-down made ahead (Daemon.prepare): parked unshown, no app search opened, "
              "no keyboard asked for", prepared and service.prompt is None and grabs[n:] == []
              and not d._parked[DROP_WINDOW].mapped)
        presented.pop(DROP_WINDOW, None)
        t0 = time.monotonic()
        d.toggle(DROP_WINDOW)
        ok = await until(lambda: presented.get(DROP_WINDOW), 5)
        first = (presented[DROP_WINDOW][0] - t0) * 1000 if ok else None
        opened = service.prompt is not None and service.prompt.owner is None
        d.toggle(DROP_WINDOW)
        await until(lambda: DROP_WINDOW in d._parked, 3)
        check("... then MOD+D: the app search from the window made ahead, its first frame "
              "within 100 ms", opened and first is not None and first < 100, f"{first} ms")
    finally:
        Window.show = original
        Prompt.answer = real_answer
        if answering is not None:
            answering.cancel()
        if fake is not None:
            fake.close()
        service.stop()
        for w in list(d.instances.values()) + list(d._parked.values()) + list(d._dying):
            w.destroy()
        d.instances.clear()
        d._parked.clear()
        daemon_mod._DAEMON = None
        other.core.DestroyWindow(stand)
        other.flush()
        other.disconnect()
        loop.remove_reader(display.fd)
        display.close()
        if os.path.exists(sock):
            os.unlink(sock)


def read_window(conn, composite, wid, w, h):
    """A window of ours, from its composite pixmap: RGBA."""
    import numpy as np
    import xcffib.xproto as xp
    pix = conn.generate_id()
    composite.NameWindowPixmap(wid, pix)
    try:
        img = conn.core.GetImage(xp.ImageFormat.ZPixmap, pix, 0, 0, w, h, 0xFFFFFFFF).reply()
    finally:
        conn.core.FreePixmap(pix)
        conn.flush()
    a = np.frombuffer(bytes(img.data.buf()), np.uint8)[:w * h * 4].reshape(h, w, 4)
    return a[..., [2, 1, 0, 3]].copy()


def save_png(path, rgba) -> None:
    import numpy as np
    import skia
    skia.Image.fromarray(np.ascontiguousarray(rgba)).save(str(path), skia.kPNG)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--off-screen", action="store_true", help="sections 1-6 only")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    print(f"scratch {SCRATCH}", flush=True)
    import logging
    logging.basicConfig(level=logging.WARNING, filename=SCRATCH / "test.log",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    t0 = time.monotonic()
    try:
        test_places()
        test_addresses()
        test_input()
        test_menus()
        test_apps_search()
        test_flux_bar()
        test_placement()
        if not args.off_screen:
            asyncio.run(on_screen())
    except Exception:
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    keep = bool(FAILED) or args.keep
    print(f"\n{time.monotonic() - t0:.0f} s" + (f"; kept in {SCRATCH}" if keep else ""))
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    if not keep:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
