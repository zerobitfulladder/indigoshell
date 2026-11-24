"""The launcher's modes for Jack, and what they read and open.

    .venv/bin/python tests/browser_launcher.py              all of it; the
                                                             launcher shows for
                                                             about two seconds
    .venv/bin/python tests/browser_launcher.py --off-screen  1-6 only: nothing shows

1. places (browser/places.py): what a typed line means, the history
   writer's rules and its pruning, the bookmarks file (atomic writes,
   hand edits, a broken file).
2. The address mode (services/addresses.py) on a scratch history and
   bookmarks: the rows and their order for typed addresses, searches,
   bookmark and host prefixes, history; Shift+Return; removing a
   bookmark's row; and what a search costs with 5,000 and 10,000 pages
   of history.
3. The launcher widget (widgets/launcher.py), driven with KeyEvents and
   painted off screen, never in a window: the apps as before (Return,
   Ctrl+L, Ctrl+H, Escape), the address mode (the URL selected, typing
   replacing it, the preselection, Shift+Return, Escape), the actions
   (a dimmed row refused with the flash, filtering, Ctrl+J/K), a second
   step and back, type-ahead (keys handed over in order, ahead of the
   ones typed there; an Escape among them closing it; none taken once it
   closed or for a mode it no longer shows), cancel; Shift+Delete on a
   bookmark (gone in place) and on every other row (refused), plain
   Delete still the query's.
4. `open_launcher` against a stand-in daemon: pending mode, in-place
   swap, the placement passed on, no launcher configured.
5. The shell's side of Jack (browser/context.py, `JackWindow`) with the
   launcher and the keyboard grab replaced by recorders: the keys a main
   window gives the shell and a single one does not; Ctrl+L, Ctrl+T,
   Ctrl+M as the service hands them over (the keyboard held first, the
   mode, the keys typed after the press, the chord again while it is up),
   load here / in a tab, the actions and the bookmark commands,
   Shift+Delete's remove with a url, the keyboard given back when the
   launcher does not take it, prompt when there is no launcher, a
   `launcher` message, messages it does not know, a closed browser
   window.
6. Placement (window.py): centred under a rect, clamped to the monitor
   holding it, forgotten when the window parks; outside clicks.
7. On screen: a copy of the launcher's window spec with no keyboard
   grab, no pointer grab and no focus, opened by the real Daemon.open
   through the real AppService for a stand-in browser window (unmapped),
   on a Ctrl+L press delivered to the service's handler as the X server
   would report it once the key grab fired (nothing is typed into the
   display; the service's own keyboard hold is recorded, never made):
   centred on it, its top at the window's top edge with no top bar, at
   a top bar's bottom edge with one (a stand-in bar: no window with a
   strut is ever made here); its own pixels read (never the root); keys
   typed after the press into the query in order, an Escape among them
   closing it and a key after that dropped; the time from the press to
   its first frame, then opened the MOD+D way and found back at the
   usual spot.

The real launcher window (which grabs the keyboard and pointer) is never
opened. Nothing here touches the running shell or the real history and
bookmarks: every XDG directory and socket is a scratch one.
"""

import argparse
import asyncio
import dataclasses
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
    from indigoshell.browser import places

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
    from indigoshell.browser import places
    h = places.History(db)
    for url, title, visits, age in rows:
        for _ in range(visits):
            h.visit(url, title, now=NOW - age)
    h.close()


def test_addresses() -> None:
    from indigoshell.browser import places
    from indigoshell.services.addresses import BOOKMARK, HISTORY, AddressMode

    section("2. the address mode: rows, order, preselection, cost")
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
    picked = []

    def mode(text=""):
        return AddressMode(text=text, on_load=picked.append, history=db, bookmarks=marks,
                           now=NOW)

    m = mode()

    def rows(q):
        entries, start = m.search(q)
        return [(e.key or ("B" if e.glyph == BOOKMARK else "H" if e.glyph == HISTORY else "?"),
                 e.title, e.value) for e in entries], start

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
    entries, start = m.search("yo")
    m.pick(entries[start], "yo")
    m.pick(None, "yo")
    m.pick(entries[0], "yo")
    check("pick: the row's address; Shift+Return (no row) and the typed row: the typed text",
          picked == ["https://www.youtube.com/", places.SEARCH + "yo", places.SEARCH + "yo"],
          f"{picked}")
    check("the count: listed / everything (bookmarks + history pages)",
          m.count(entries) == f"{len(entries) - 1}/8", m.count(entries))
    m.close()

    # Shift+Delete's side of the mode: a bookmark row goes, nothing else does.
    removed = []
    m = AddressMode(on_load=picked.append, on_remove=lambda u: removed.append(u) or True,
                    history=db, bookmarks=marks, now=NOW)
    entries, _ = m.search("hack")
    mark_row = next(e for e in entries if e.glyph == BOOKMARK)
    typed_row = entries[0]
    history_row = next((e for e in m.search("cats")[0] if e.glyph == HISTORY), None)
    refused = [m.remove(typed_row), m.remove(history_row)]
    done = m.remove(mark_row)
    after, _ = m.search("hack")
    rows_after = [(e.glyph == BOOKMARK, e.title) for e in after if not e.key]
    check("remove: a bookmark's row is taken out (on_remove gets its address as the file has "
          "it); the typed row and a history row are refused",
          refused == [False, False] and done and removed == ["https://news.ycombinator.com"]
          and m.count(after) == f"{len(after) - 1}/7", f"{removed} {m.count(after)}")
    check("... its page, in the history too, comes back as a history row; the file is not "
          "the mode's to write", (False, "Hacker News") in rows_after
          and not any(b for b, _ in rows_after)
          and len(json.loads(marks.read_text())) == 2, f"{rows_after}")
    gone = AddressMode(on_load=picked.append, on_remove=lambda u: False, history=db,
                       bookmarks=marks, now=NOW)
    entries, start = gone.search("arch")
    none = AddressMode(on_load=picked.append, history=db, bookmarks=marks, now=NOW)
    entries2, start2 = none.search("arch")
    check("refused when the browser that asked is gone (on_remove False) or cannot take it "
          "(no on_remove): the row stays",
          gone.remove(entries[start]) is False and none.remove(entries2[start2]) is False
          and any(e.title == "Arch Wiki" for e in gone.search("arch")[0]))
    for mode_ in (m, gone, none):
        mode_.close()

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
        big_mode = AddressMode(on_load=picked.append, history=target, bookmarks=marks, now=NOW)
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


# ── 3. the launcher widget ──────────────────────────────────────────────

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


RETURN, ESC, BKSP, DOWN, UP = 0xFF0D, 0xFF1B, 0xFF08, 0xFF54, 0xFF52


def render(launcher, name: str):
    """The launcher painted off screen, saved, and how much of it is lit."""
    import numpy as np
    import skia

    from indigoshell.widgets.base import Size
    size = launcher.measure(Size(10_000, 10_000))
    w, h = int(size.width), int(size.height)
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.clear(0)
    launcher.arrange(skia.Rect.MakeWH(w, h))
    launcher.paint(canvas)
    image = surface.makeImageSnapshot()
    image.save(str(SCRATCH / f"{name}.png"), skia.kPNG)
    return np.array(image.toarray(colorType=skia.kRGBA_8888_ColorType))


def test_launcher_widget() -> None:
    import numpy as np

    from indigoshell import theme
    from indigoshell.services.addresses import AddressMode
    from indigoshell.services.apps import Catalog
    from indigoshell.services.modes import Choice, Choices
    from indigoshell.widgets.launcher import Launcher

    section("3. the launcher widget, driven with keys, painted off screen")
    apps = SCRATCH / "apps" / "applications"
    apps.mkdir(parents=True)
    for name in ("Alpha Editor", "Beta Browser", "Gamma Terminal"):
        (apps / f"{name.split()[0].lower()}.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec=true\n")
    os.environ["XDG_DATA_DIRS"] = str(SCRATCH / "apps")
    os.environ["XDG_DATA_HOME"] = str(SCRATCH / "no-user-apps")
    catalog = Catalog()
    launched, ran, closed = [], [], []
    catalog.launch = lambda app: launched.append(app.entry.name)
    catalog.run = lambda line: ran.append(line)
    L = Launcher(rows=6, width=600, catalog=catalog)
    L._close = lambda: closed.append(L.mode)
    win = FakeWindow()

    L.attach(win)
    names = [r.app.entry.name for r in L.results]
    check("MOD+D: the apps, the usual placeholder, an empty query, the window sized once",
          L.mode is None and names == ["Alpha Editor", "Beta Browser", "Gamma Terminal"]
          and L.query.placeholder == "search applications" and L.query.text == ""
          and len(win.sizes) == 1, f"{names}")
    for ev in typed("bet"):
        L.key(ev)
    L.key(key(RETURN))
    L.key(key(ord("l"), ctrl=True))
    check("typing filters; Return and Ctrl+L launch the selected app",
          launched == ["Beta Browser", "Beta Browser"] and len(closed) == 2, f"{launched}")
    for ev in typed("zzz"):
        L.key(ev)
    L.key(key(RETURN))
    check("no match: Return runs the query as a command line, as before", ran == ["betzzz"])
    closed.clear()
    L.key(key(ord("h"), ctrl=True))
    esc = L.key(key(ESC))
    check("Ctrl+H closes from the only step; Escape is left to the window (closes it)",
          len(closed) == 1 and esc is False)
    j = L.key(key(ord("j"), ctrl=True))
    check("Ctrl+J / Ctrl+K are still the selection's, not the text field's", j is True)
    render(L, "apps")

    picked = []
    db = SCRATCH / "addr" / "h.sqlite"
    marks = SCRATCH / "addr" / "bookmarks.json"
    L.pending = AddressMode(text="https://www.youtube.com/watch?v=1", on_load=picked.append,
                            history=db, bookmarks=marks, now=NOW)
    L.attach(win)
    lit = render(L, "address-selected")
    check("Ctrl+L: the address mode, the current URL in the query and all of it selected",
          L.mode is not None and L.query.text == "https://www.youtube.com/watch?v=1"
          and L.query.selected_all and L.query.placeholder == "address or search"
          and L.results[0].key == "OPEN" and L.selected == 0)
    for ev in typed("y"):
        L.key(ev)
    first = L.query.text
    for ev in typed("o"):
        L.key(ev)
    lit2 = render(L, "address-yo")
    check("typing replaces the selected URL; 'yo' selects YouTube's front page",
          first == "y" and L.query.text == "yo"
          and L.results[L.selected].value == "https://www.youtube.com/", L.query.text)
    closed.clear()
    L.key(key(RETURN))
    check("Return loads the selected row and closes", picked == ["https://www.youtube.com/"]
          and len(closed) == 1, f"{picked}")
    L.key(key(RETURN, shift=True))
    check("Shift+Return takes the typed text whatever is selected",
          picked[-1].endswith("search?q=yo"), picked[-1])
    cyan = np.array([int(theme.CYAN_BRIGHT[i:i + 2], 16) for i in (1, 3, 5)])
    sel = np.abs(lit[..., :3].astype(int) - cyan).max(-1) < 90
    check("painted: the selection band behind the URL; the rows drawn (PNGs in the scratch dir)",
          sel.sum() > 200 and lit2[..., 3].astype(bool).sum() > 10_000, f"{int(sel.sum())} px")

    L.pending = AddressMode(text="", on_load=picked.append, history=db, bookmarks=marks, now=NOW)
    L.attach(win)
    n = len(picked)
    esc = L.key(key(ESC))
    check("Ctrl+T: an empty query; Escape loads nothing (the window closes)",
          L.query.text == "" and esc is False and len(picked) == n)
    L.attach(win)
    check("the next MOD+D after an address open: the apps again",
          L.mode is None and L.query.placeholder == "search applications"
          and [r.app.entry.name for r in L.results][:1] == ["Alpha Editor"])

    did = []
    actions = Choices([Choice("Add bookmark", lambda: did.append("add"), enabled=True,
                              detail="example.org"),
                       Choice("Remove bookmark", lambda: did.append("remove"), enabled=False,
                              detail="example.org")], placeholder="actions")
    L.pending = actions
    L.attach(win)
    rows = [(e.title, e.enabled) for e in L.results]
    closed.clear()
    L.key(key(ord("j"), ctrl=True))
    L.key(key(RETURN))
    refused = bool(L._reject_until) and not did and not closed
    shot = render(L, "actions-refused")
    L._reject_until = 0
    L.key(key(ord("k"), ctrl=True))
    L.key(key(ord("l"), ctrl=True))
    check("Ctrl+M: both actions listed, the one that does not apply dimmed; picking it is "
          "refused with the flash and the launcher stays; the other runs and closes",
          rows == [("Add bookmark", True), ("Remove bookmark", False)] and refused
          and did == ["add"] and len(closed) == 1, f"{rows}, did {did}")
    magenta = np.array([int(theme.MAGENTA_BRIGHT[i:i + 2], 16) for i in (1, 3, 5)])
    flash = np.abs(shot[..., :3].astype(int) - magenta).max(-1) < 80
    check("... the refusal painted in the reject colour", flash.sum() > 100, f"{int(flash.sum())} px")
    L.pending = Choices(actions.choices)
    L.attach(win)
    for ev in typed("rem"):
        L.key(ev)
    check("typing filters the actions", [e.title for e in L.results] == ["Remove bookmark"])

    second = Choices([Choice("Step one", lambda: [Choice("Deeper", lambda: did.append("deep"))]),
                      Choice("Other", lambda: None)])
    L.pending = second
    L.attach(win)
    for ev in typed("st"):
        L.key(ev)
    L.key(key(RETURN))
    deeper = [e.title for e in L.results]
    L.key(key(ESC))
    back = (L.query.text, [e.title for e in L.results][:1], len(L.steps))
    L.key(key(RETURN))
    L.key(key(RETURN))
    closed.clear()
    L.key(key(ESC))
    L.key(key(ord("h"), ctrl=True))
    check("an action that opens a further list: a second step; Escape back to the first "
          "with its query; Return there and on; Ctrl+H back, then closed",
          deeper == ["Deeper"] and back == ("st", ["Step one"], 1) and did[-1] == "deep"
          and len(closed) == 1, f"{deeper} {back}")

    # Type-ahead: keys typed in the browser's window before the launcher
    # had the keyboard, handed over as they come.
    m = AddressMode(text="https://x.org/", on_load=picked.append, history=db, bookmarks=marks,
                    now=NOW)
    L.pending = m
    L.attach(win)
    took = m.type_ahead(typed("yo"))
    for ev in typed("u"):
        L.key(ev)                       # then typed here, once it had the keyboard
    check("type-ahead: keys handed over replace the selected URL, in order; those typed here "
          "follow", took and L.shows(m) and L.query.text == "you", L.query.text)
    closed.clear()
    real_close = L._close
    L._close = lambda: (closed.append(L.mode), L.on_dismiss())     # as the window does
    m.type_ahead(typed("ab") + [key(ESC)] + typed("cd"))
    L._close = real_close
    check("an Escape among them closes the launcher (what the window does for a live Escape); "
          "what came after it is not typed", len(closed) == 1 and L.query.text == "youab"
          and not L.shows(m), L.query.text)
    check("closed: keys for it no longer taken", m.type_ahead(typed("z")) is False
          and L.query.text == "youab")
    m2 = AddressMode(text="", on_load=picked.append, history=db, bookmarks=marks, now=NOW)
    L.pending = m2
    L.attach(win)
    check("... nor for a mode the launcher no longer shows (the next open is another's)",
          m.type_ahead(typed("z")) is False and m2.type_ahead(typed("q"))
          and L.query.text == "q")
    closed.clear()
    m.cancel()
    m2.cancel()
    check("cancel from whoever asked closes it only while it shows that mode", len(closed) == 1)
    L.begin(None)
    check("begin(None) in place: the apps again", L.mode is None and m2.host is None)

    # Shift+Delete: a bookmark out of the address list, in place.
    DELETE, HOME = 0xFFFF, 0xFF50
    removed = []
    L.pending = AddressMode(text="", on_load=picked.append,
                            on_remove=lambda u: removed.append(u) or True,
                            history=db, bookmarks=marks, now=NOW)
    L.attach(win)
    sized = len(win.sizes)
    for ev in typed("arch"):
        L.key(ev)
    before = (L.selected, L.results[L.selected].title, [e.title for e in L.results])
    closed.clear()
    L._reject_until = 0
    L.key(key(DELETE, shift=True))
    titles_after = [e.title for e in L.results]
    check("Shift+Delete on a bookmark's row: its address to the browser, the row gone from the "
          "list at once; the query, the step and the window as they were, no flash",
          before[1] == "Arch Wiki" and removed == ["https://wiki.archlinux.org/"]
          and "Arch Wiki" in before[2] and "Arch Wiki" not in titles_after
          and L.query.text == "arch" and not closed and not L._reject_until
          and L.mode is not None and len(win.sizes) == sized
          and L.selected == min(before[0], len(L.results) - 1),
          f"{before[2]} -> {titles_after}, query {L.query.text!r}, selected {L.selected}")
    L.select(0)
    L.key(key(DELETE, shift=True))
    on_typed = (bool(L._reject_until), list(removed), L.query.text)
    L._reject_until = 0
    L.key(key(ord("u"), ctrl=True))
    for ev in typed("cats"):
        L.key(ev)
    L.select(next(i for i, e in enumerate(L.results) if e.title == "Cats - YouTube"))
    L.key(key(DELETE, shift=True))
    on_history = (bool(L._reject_until), list(removed), L.query.text,
                  [e.title for e in L.results].count("Cats - YouTube"))
    L._reject_until = 0
    check("... on the typed row or a history row: refused with the flash, nothing sent, the "
          "query kept",
          on_typed == (True, ["https://wiki.archlinux.org/"], "arch")
          and on_history == (True, ["https://wiki.archlinux.org/"], "cats", 1) and not closed,
          f"{on_typed} {on_history}")
    L.key(key(HOME))
    L.key(key(DELETE))
    check("plain Delete still edits the query (the character after the caret)",
          L.query.text == "ats" and removed == ["https://wiki.archlinux.org/"], L.query.text)
    L.pending = None
    L.attach(win)
    for ev in typed("bet"):
        L.key(ev)
    L.key(key(DELETE, shift=True))
    in_apps = (bool(L._reject_until), L.query.text, len(L.results))
    L._reject_until = 0
    L.pending = Choices(actions.choices)
    L.attach(win)
    L.key(key(DELETE, shift=True))
    in_actions = (bool(L._reject_until), len(L.results))
    L._reject_until = 0
    check("Shift+Delete in the apps or the actions: refused with the flash, nothing gone",
          in_apps == (True, "bet", 1) and in_actions == (True, 2) and not closed,
          f"{in_apps} {in_actions}")
    L.begin(None)


# ── 4. open_launcher ────────────────────────────────────────────────────

def test_open_launcher() -> None:
    from indigoshell.services.modes import Choices
    from indigoshell.widgets.launcher import Launcher, open_launcher
    from indigoshell.window import Placement

    section("4. open_launcher against a stand-in daemon")
    L = Launcher(rows=3, width=400)

    class Daemon:
        def __init__(self, kinds):
            self.kinds, self.instances, self.opened = kinds, {}, []

        def open(self, name, params=None, *, placement=None):
            self.opened.append((name, placement))
            self.instances[name] = object()

    d = Daemon({"launcher": types.SimpleNamespace(content=L)})
    mode = Choices([])
    where = Placement.below(100, 200, 800, 28)
    ok = open_launcher(d, mode, placement=where)
    check("closed: the mode waits for the open, the placement goes to Daemon.open",
          ok and L.pending is mode and d.opened == [("launcher", Placement(500, 228))])
    begun = []
    L.begin = begun.append
    mode2 = Choices([])
    open_launcher(d, mode2, placement=None)
    check("open already: the mode swapped in place, the window opened (moved) again",
          begun == [mode2] and L.pending is mode and len(d.opened) == 2)
    check("no launcher window configured: False",
          open_launcher(Daemon({}), mode) is False
          and open_launcher(Daemon({"launcher": types.SimpleNamespace(content=None)}), mode)
          is False)


# ── 5. the shell's side of Jack ─────────────────────────────────────────

class Line:
    """What a JackWindow has of its connection: the service, and a send
    that keeps what was sent."""

    def __init__(self, service):
        self.service = service
        self.out = []

    def send(self, msg):
        self.out.append(msg)


def test_jack_flow() -> None:
    from indigoshell.applink.service import AppService
    from indigoshell.browser import protocol
    from indigoshell.browser.context import JackWindow
    from indigoshell.services.addresses import AddressMode
    from indigoshell.services.modes import Choices

    section("5. the shell's side of Jack, the launcher and the keyboard grab replaced by recorders")
    service = AppService({"jack": JackWindow}, path="/nonexistent")   # no X here: no display
    held = []
    service.hold_keyboard = lambda w: held.append(("hold", w.wid)) or True
    service.release_keyboard = lambda: held.append("release")
    shown = []

    class Host:
        """The launcher, as Jack's side needs it."""
        window = types.SimpleNamespace(keyboard_grabbed=True)

        def __init__(self):
            self.got, self.modes = [], []

        def shows(self, m):
            return m in self.modes

        def type_ahead(self, m, keys):
            if not self.shows(m):
                return False
            self.got.append(("keys", m, [k.text for k in keys]))
            return True

        def cancel(self, m):
            self.got.append(("cancel", m))
            self.modes = []

    host = Host()

    def show(mode, placement):
        mode.host = host
        host.modes = [mode]
        shown.append((mode, placement))
        return True

    service.show_launcher = show

    def jack(wid, single=False):
        line = Line(service)
        return JackWindow(line, wid, protocol.announce(wid, single)), line

    def page(url, bookmarked=False):
        return protocol.state([protocol.tab("Cats", "x.org", False)], 0, 100, True, url=url,
                              bookmarked=bookmarked, find=None)

    ctrl = {c: key(ord(c), ctrl=True) for c in "ltm"}
    s, line = jack(0x400001)
    w = types.SimpleNamespace(out=line.out)
    check("a main window gives the shell Ctrl+L, Ctrl+T and Ctrl+M; a single one (a popup, the "
          "inspector) none, and nor does one whose announce says neither",
          s.keys == {"ctrl+l": "address", "ctrl+t": "new_tab", "ctrl+m": "actions"}
          and jack(0x400005, True)[0].keys == {}
          and JackWindow(Line(service), 0x400006,
                         protocol.message("announce", app="jack", wid=0x400006)).keys == {})
    s.on_message(page("https://x.org/cats"))
    s.on_key("address", ctrl["l"])
    mode, where = shown[-1]
    check("Ctrl+L, handed over by the service: the keyboard held first, then an AddressMode "
          "with the page's address as its query, selected; no placement without X; the keyboard "
          "not given back (the launcher has it)",
          held == [("hold", 0x400001)] and isinstance(mode, AddressMode)
          and mode.text == "https://x.org/cats" and mode.select_text and where is None)
    s.on_typed(key(ord("y"), "y"))
    s.on_typed(key(ord("o"), "o"))
    check("keys typed after the press, before the launcher had the keyboard: to its mode, in "
          "order", host.got == [("keys", mode, ["y"]), ("keys", mode, ["o"])], f"{host.got}")
    s.on_key("address", ctrl["l"])
    check("Ctrl+L again while it is up: a key for it, not a second launcher",
          len(shown) == 1 and host.got[-1] == ("keys", mode, [""])
          and held == [("hold", 0x400001)])
    mode.pick(None, "example.org")
    check("a pick here: {type: load, where: here}",
          w.out[-1] == {"type": "load", "url": "http://example.org", "where": "here"},
          f"{w.out[-1]}")
    host.modes = []                     # closed
    got = len(host.got)
    s.on_typed(key(ord("z"), "z"))
    check("a key after its launcher closed: dropped", len(host.got) == got)
    s.on_key("new_tab", ctrl["t"])
    shown[-1][0].pick(None, "yo")
    check("Ctrl+T: an empty query; the pick loads in a new tab",
          shown[-1][0].text == "" and w.out[-1]["type"] == "load"
          and w.out[-1]["where"] == "tab" and w.out[-1]["url"].endswith("q=yo"))
    host.modes = []
    s.on_key("actions", ctrl["m"])
    actions = shown[-1][0]
    rows, start = actions.search("")
    check("Ctrl+M: Add bookmark (applies) and Remove bookmark (dimmed), Add selected",
          isinstance(actions, Choices) and [(e.title, e.enabled) for e in rows]
          == [("Add bookmark", True), ("Remove bookmark", False)] and start == 0
          and rows[0].detail == "x.org")
    refused = actions.pick(rows[1], "")
    done = actions.pick(rows[0], "")
    check("the dimmed one refused; the other sends {type: bookmark, op: add} and is done",
          refused is False and done is True and w.out[-1] == {"type": "bookmark", "op": "add"})
    s.on_message(page("https://x.org/cats", bookmarked=True))
    host.modes = []
    s.on_key("actions", ctrl["m"])
    rows, start = shown[-1][0].search("")
    check("on a bookmarked page Remove applies and is selected", start == 1
          and [e.enabled for e in rows] == [False, True])
    s.on_message(page("about:blank"))
    host.modes = []
    s.on_key("address", ctrl["l"])
    check("Ctrl+L on a blank page: an empty query", shown[-1][0].text == ""
          and not shown[-1][0].select_text)

    host.modes = []
    Host.window.keyboard_grabbed = False
    n = len(held)
    s.on_key("address", ctrl["l"])
    Host.window.keyboard_grabbed = True
    check("the launcher up but without the keyboard: the keyboard given back at once",
          held[n:] == [("hold", 0x400001), "release"], f"{held[n:]}")
    service.show_launcher = lambda mode, placement: False
    host.modes = []
    n = len(held)
    s.on_key("new_tab", ctrl["t"])
    check("no launcher to show it in: the keyboard held, then given back; the browser told "
          "{type: prompt, action: new_tab}", held[n:] == [("hold", 0x400001), "release"]
          and w.out[-1] == {"type": "prompt", "action": "new_tab"}, f"{w.out[-1]}")
    n = len(held)
    s.on_message(protocol.launcher("address"))
    check("{type: launcher, action: address} (a new window, no key pressed) with no launcher: "
          "nothing held or given back, {type: prompt, action: address}",
          held[n:] == [] and w.out[-1] == {"type": "prompt", "action": "address"})
    service.show_launcher = show
    s.on_message(page("https://x.org/cats"))
    s.on_message(protocol.launcher("address"))
    check("... and with a launcher: the address list for that window, nothing held",
          isinstance(shown[-1][0], AddressMode) and shown[-1][0].text == "https://x.org/cats"
          and held[n:] == [])
    n, state = len(shown), s.state
    s.on_message(protocol.launcher("nonsense"))
    s.on_message(protocol.message("from-the-future", x=1))
    s.on_message(protocol.message("state", tabs=[]))
    check("a launcher for no action, a type it does not know, a malformed state: ignored",
          len(shown) == n and s.state is state)

    # Shift+Delete in the address list: that row's address.
    browser_marks = Path(os.environ["XDG_CONFIG_HOME"]) / APP / "browser" / "bookmarks.json"
    browser_marks.parent.mkdir(parents=True, exist_ok=True)
    browser_marks.write_text(json.dumps([{"name": "Shop", "url": "https://shop.example/cart",
                                          "added": 1, "folder": "Imported"}]))
    host.modes = []
    s.on_key("address", ctrl["l"])
    marks_mode = shown[-1][0]
    rows, _ = marks_mode.search("")
    removed = marks_mode.remove(rows[0])
    check("Shift+Delete on a bookmark: {type: bookmark, op: remove, url: the row's}",
          removed and w.out[-1] == {"type": "bookmark", "op": "remove",
                                    "url": "https://shop.example/cart"}, f"{w.out[-1:]}")
    s3, _ = jack(0x400003)
    s3.closed = True
    check("... and refused once that browser window is gone",
          s3._remove_bookmark("https://shop.example/cart") is False)
    browser_marks.unlink()

    sent = len(w.out)
    s.close()
    marks_mode.on_load("https://late.org/")
    check("the browser window gone: its launcher cancelled (closed if it still shows its mode), "
          "a late pick sends nothing",
          host.got[-1] == ("cancel", marks_mode) and s.request is None and len(w.out) == sent)
    s2, _ = jack(0x400002)
    s2.on_message({**page("x"), "future": [1, 2]})
    check("a state with keys it does not know: shown, those keys not kept",
          s2.state.get("url") == "x" and "future" not in s2.state)


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
    import xcffib
    import xcffib.composite
    import xcffib.xproto as xp

    from indigoshell import config_default
    from indigoshell.backend.x11 import Display
    from indigoshell.applink.service import AppService
    from indigoshell.browser import protocol
    from indigoshell.browser.context import JackWindow
    from indigoshell.core import daemon as daemon_mod
    from indigoshell.widgets.launcher import Launcher, open_launcher
    from indigoshell.window import Window

    section("7. on screen: a no-grab copy of the launcher, placed under a stand-in browser")
    real = next(s for s in config_default.WINDOWS if s.name == config_default.LAUNCHER)
    spec = dataclasses.replace(real, name="test-launcher", grab_keyboard=False,
                               dismiss_on_outside_click=False, focusable=False,
                               content=Launcher(rows=12, width=760))
    check("the copy takes no keyboard, no pointer, no focus; the real one is untouched",
          not spec.grab_keyboard and not spec.dismiss_on_outside_click and not spec.focusable
          and spec.override_redirect and real.grab_keyboard and real.dismiss_on_outside_click)
    # When a window is mapped with its first frame drawn: what show()
    # does before it returns (the spawn then plays from that frame on).
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

    def pump():
        while display.pump():
            pass

    loop.add_reader(display.fd, pump)
    d = daemon_mod.Daemon({"windows": [spec]})
    d.display, d.loop = display, loop
    sock = os.path.join(runtime_dir(), f"{APP}-blt-{os.getpid()}-apps.sock")
    service = AppService({"jack": JackWindow}, path=sock, display=display)
    service.show_launcher = lambda mode, placement: open_launcher(
        d, mode, name="test-launcher", placement=placement)
    # The keyboard is never taken here: the service's hold is recorded.
    kb = []
    service.hold_keyboard = lambda w: kb.append(("hold", w.wid)) or False
    service.release_keyboard = lambda: kb.append("release")
    service.start()
    other = xcffib.connect()
    screen = other.get_setup().roots[0]
    composite = other(xcffib.composite.key)
    composite.QueryVersion(0, 4).reply()
    mon = display.primary_monitor()
    bx, by, bw, bh = mon.x + mon.width // 2 - 500, mon.y + 120, 1000, 700
    stand = other.generate_id()
    # Unmapped: the stand-in only has to exist and have a place; only
    # the launcher shows.
    other.core.CreateWindow(screen.root_depth, stand, screen.root, bx, by, bw, bh, 0,
                            xp.WindowClass.InputOutput, screen.root_visual,
                            xp.CW.BackPixel | xp.CW.OverrideRedirect, [0x00FF00, 1])
    other.core.GetInputFocus().reply()
    fake = None
    try:
        fake = Fake(sock)
        fake.send(protocol.announce(stand, False))
        fake.send(protocol.state([protocol.tab("Stand-in", "example.org", False)], 0, 100, True,
                                 url="https://example.org/", bookmarked=False, find=None))
        attached = await until(lambda: any(m.get("type") == "attached" for m in fake.read()), 5)
        grabbed = await until(lambda: stand in service.grabs, 2)
        check("the stand-in browser attached, its keys grabbed on its window",
              bool(attached) and bool(grabbed))

        def press(sym, state=0):
            """A key press as the X server reports one to the service while
            its grab holds the keyboard: on the browser's window."""
            ev = xp.KeyPressEvent.synthetic(display.keycodes(sym)[0], 0, display.root, stand,
                                            0, 0, 0, 0, 0, state, 1)
            ev.response_type = 2
            display.dispatch(ev)

        async def ctrl_l():
            presented.pop("test-launcher", None)
            t0 = time.monotonic()
            press(ord("l"), xp.ModMask.Control)
            ok = await until(lambda: presented.get("test-launcher"), 5)
            return (presented["test-launcher"][0] - t0) * 1000 if ok else None

        cold = await ctrl_l()
        win = d.instances.get("test-launcher")
        cx, cy, cw, ch = win.content_root if win else (0, 0, 0, 0)
        want_x = min(max(bx + bw // 2 - cw // 2, mon.x), mon.x + mon.width - cw)
        check("no top bar: centred on the browser window, its top at the window's top edge",
              win is not None and (cx, cy) == (want_x, by),
              f"content {cw}x{ch}+{cx}+{cy}, browser {bw}x{bh}+{bx}+{by}")
        check("the spawn's bleed around it as anywhere: the X window is the content plus the "
              "bleed on each side", win is not None and win.bleed > 0
              and (win.x, win.y, win.width, win.height)
              == (cx - win.bleed, cy - win.bleed, cw + 2 * win.bleed, ch + 2 * win.bleed),
              f"bleed {win.bleed if win else '?'}")
        await asyncio.sleep(0.06)
        mid = read_window(other, composite, win.wid, win.width, win.height)
        await asyncio.sleep(0.45)
        done = read_window(other, composite, win.wid, win.width, win.height)
        save_png(SCRATCH / "on-screen-spawn.png", mid)
        save_png(SCRATCH / "on-screen-placed.png", done)
        b = win.bleed
        inside = done[b + 40:b + ch - 40, b + 40:b + cw - 40, 3]
        margin = done[:b // 2, :, 3]
        check("its own pixels after the spawn: the plate opaque inside, the bleed margin clear",
              float((inside > 200).mean()) > 0.95 and float((margin == 0).mean()) > 0.99,
              f"plate {float((inside > 200).mean()):.1%} opaque, margin "
              f"{float((margin == 0).mean()):.1%} clear (PNGs in {SCRATCH})")
        mode = spec.content.mode
        check("the address mode is showing, the page's URL selected; the service held the "
              "keyboard first, and gave it back when this copy took none",
              mode is not None and spec.content.query.text == "https://example.org/"
              and spec.content.query.selected_all and kb == [("hold", stand), "release"],
              f"{kb}")
        press(ord("e"))
        press(ord("x"))
        typed_in = spec.content.query.text
        press(0xFF1B)
        press(ord("q"))
        parked = await until(lambda: "test-launcher" in d._parked, 3)
        check("keys typed after the press, on the browser's window: into the query in order, "
              "replacing the selected URL; Escape among them closes the launcher (parked after "
              "its despawn), and a key after that goes nowhere",
              typed_in == "ex" and bool(parked) and spec.content.query.text == "ex",
              f"{typed_in!r}")
        # A top bar over the monitor, as the context area tells the
        # service where it is: a stand-in with the real one's geometry.
        bar_h = config_default.top_bar().size[1]
        service.context = types.SimpleNamespace(window=types.SimpleNamespace(
            wid=1, content_root=(mon.x, mon.y, mon.width, bar_h)))
        warm = await ctrl_l()
        win = d.instances.get("test-launcher")
        again = win.content_root[:2] if win else None
        check("with a top bar: opened again from parked (the usual case), dropping from the "
              "bar: centred on the browser window, its top at the bar's bottom edge",
              bool(parked) and again == (want_x, mon.y + bar_h), f"{again}")
        service.context = None
        d.close("test-launcher")
        await until(lambda: "test-launcher" in d._parked, 3)
        presented.pop("test-launcher", None)
        open_launcher(d, None, name="test-launcher")     # what MOD+D does
        await until(lambda: presented.get("test-launcher"), 5)
        win = d.instances.get("test-launcher")
        cx, cy, cw, ch = win.content_root
        check("then MOD+D: back at the usual spot (centred on the primary), listing the apps",
              (cx, cy) == (mon.x + (mon.width - cw) // 2, mon.y + (mon.height - ch) // 2)
              and spec.content.mode is None, f"+{cx}+{cy}")
        d.close("test-launcher")
        await until(lambda: "test-launcher" in d._parked, 3)
        print(f"  (timing) key press handled -> launcher mapped with its first frame: "
              f"{cold:.0f} ms cold (a new window), {warm:.0f} ms from parked; the spawn plays "
              f"{spec.effects[0].duration * 1000:.0f} ms from there", flush=True)
        check("the launcher's first frame within 100 ms of the press, from parked",
              warm is not None and warm < 100, f"{warm:.0f} ms")
    finally:
        Window.show = original
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
        test_launcher_widget()
        test_open_launcher()
        test_jack_flow()
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
