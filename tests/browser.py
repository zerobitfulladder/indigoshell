"""The browser (indigoshell/browser/qt), unattended.

    .venv/bin/python tests/browser.py              off screen, about a minute
    .venv/bin/python tests/browser.py --on-screen  the shell's app service and a real window, ~40 s

Off screen (QT_QPA_PLATFORM=offscreen), nothing appears:
  1. pure parts: the key table, sites.json rules, the messages (every
     one typed; the state check), Widevine found/copied in fake
     directories, Chromium's flags
  2. one browser in this process, driven through its QWindow the way X
     delivers keys: every tab key, link/popup/close requests, the
     window's own fallback line with no shell (only after the shell has
     been away three seconds; find included), the address dialog when
     Ctrl+T / Ctrl+L reach the window, the shell link (the announce, a
     main window's and the inspector's; the line gone once the shell has
     the window; the state whole and only on change; commands;
     fullscreen), per-site identity against a local server reached as
     127.0.0.1 and as localhost (headers, navigator, frames, client
     hints, edits and a broken file), a download; Ctrl+L / Ctrl+T /
     Ctrl+M reaching the window with a shell there (a key the shell
     could not grab): the window's own answers, nothing sent; load here
     and in a new tab, prompt, a new window's `launcher`, a release
     whose press the window never saw going to the page, find ended by a
     load; find in page (both phases, the page never seeing a key find
     took), bookmarks through the command and by hand; bookmarks that redirect
     (a 301, a 302 chain onto another host name, a script while loading,
     a script after the load event) counting for the page they land on,
     with what QtWebEngine reported for each, until a link, Back or a new
     address; Remove taking out the right entry, other entries' keys
     kept; the command with a url; the history recorded; the shell away
     (the line after three seconds, never for a reload-sized gap, ten
     for a shell that said it is restarting) and a new window's dialog
     with no shell; URLs handed to the most recently used window, and a
     shutdown without Qt's profile warning. Timing printed: a picked
     address to its new tab painted.
  3. the real entry point as subprocesses: a second invocation hands its
     URL to the first and exits 0 ({type: open} answered {type: ok});
     Ctrl+C and SIGTERM end it cleanly, and
     so does its last window closing; its log file (<state_dir>/
     browser.log): what each end says, info by default and the page's
     console only at debug, a crash's trace, an exception from a Qt slot,
     the terminal only when stderr is one, the rotation

--on-screen runs the shell's own app service (indigoshell/applink) in
this process and the browser with three tabs in its unattended corner
window (floating, never takes focus), and reads the browser window's
own pixels (its composite pixmap: never the root, never the screen):
the page from the window's top edge, its keys grabbed by the service,
the top bar's content drawn from the browser's state, the line only
when the shell is away: tests/topbar.py's on-screen section, which has
the details.

Everything runs in a scratch directory (XDG_DATA_HOME, XDG_CACHE_HOME,
XDG_CONFIG_HOME, downloads) with sockets of its own, so nothing touches
the real profile, history or bookmarks, or a browser or shell someone
has open. Chromium's own chatter goes to a log file in the scratch
directory, named at the end.

Waits are for events (a load finishing, a message arriving, a count),
with a timeout, not fixed sleeps: the checks that failed one run in five
were reading a page before its load had begun.
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, env          # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir        # noqa: E402

PY = sys.executable
SCRATCH = Path(tempfile.mkdtemp(prefix=f"{APP}-browser-test-"))
TAG = f"{APP}-bt-{os.getpid()}"
SOCK = {name: os.path.join(runtime_dir(), f"{TAG}-{name}.sock")
        for name in ("browser", "apps", "browser2", "apps2", "browser3", "apps3")}

# Before anything reads them: every directory the browser uses is ours.
os.environ.update({
    "XDG_DATA_HOME": str(SCRATCH / "data"),
    "XDG_CACHE_HOME": str(SCRATCH / "cache"),
    "XDG_CONFIG_HOME": str(SCRATCH / "config"),
    "XDG_STATE_HOME": str(SCRATCH / "state"),
    env("BROWSER_SOCKET"): SOCK["browser"],
    env("APPS_SOCKET"): SOCK["apps"],
    env("BROWSER_WIDEVINE"): "off",
})
DOWNLOADS = SCRATCH / "downloads"
(SCRATCH / "config").mkdir(parents=True, exist_ok=True)
DOWNLOADS.mkdir()
(SCRATCH / "config" / "user-dirs.dirs").write_text(f'XDG_DOWNLOAD_DIR="{DOWNLOADS}"\n')

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── a local site, reachable as 127.0.0.1 and as localhost ───────────────

PAGE = ("<!doctype html><title>{title}</title>"
        "<body style='margin:0;background:#1a1a2a;color:#ccc'>{body}</body>")
LINKS = "".join(
    f"<a id=l{i} href='/page/L{i}' style='position:absolute;left:10px;top:{10 + 50 * (i - 1)}px;"
    f"font:30px sans-serif;color:#fc0'>LINK{i}</a>" for i in (1, 2, 3, 4))
BLOB = bytes(range(256)) * 1024          # 256 KiB
TEXT = ("<p>Bravo bravo BRAVO, şeker Şeker.</p>"
        "<p style='margin-top:1500px'>end</p>"
        "<script>window.keys = [];"
        "addEventListener('keydown', e => keys.push('down:' + e.key));"
        "addEventListener('keyup', e => keys.push('up:' + e.key));</script>")
LATE = "<p>late</p><script>setTimeout(() => document.title = 'Late title', 300)</script>"
PAINTED = ("<p>painted</p><script>requestAnimationFrame(() => requestAnimationFrame(() =>"
           " window.painted = performance.timeOrigin + performance.now()))</script>")
CONSOLE = "the page says hi"            # what /Console prints on its console
# Where the redirects land: a page with one big link at the top left.
LAND = ("<a id=away href='/page/Away' style='position:absolute;left:0;top:0;width:400px;"
        "height:200px;font:40px sans-serif;color:#fc0'>AWAY</a>")


class Site(BaseHTTPRequestHandler):
    seen: list[dict] = []
    hits: dict[str, int] = {}

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        route = parts.path
        Site.seen.append({"host": self.headers.get("Host", "").rsplit(":", 1)[0],
                          "path": self.path, "ua": self.headers.get("User-Agent"),
                          "ch": self.headers.get("sec-ch-ua"),
                          "cache": self.headers.get("Cache-Control")})
        Site.hits[route] = Site.hits.get(route, 0) + 1
        if route.startswith("/page/"):
            name = route[len("/page/"):]
            self._send(PAGE.format(title=name, body=f"<p>{name}</p>"))
        elif route == "/links":
            self._send(PAGE.format(title="Links", body=LINKS))
        elif route == "/text":
            self._send(PAGE.format(title="Text", body=TEXT))
        elif route == "/late":
            self._send("<!doctype html><body>" + LATE + "</body>")
        elif route == "/painted":
            self._send(PAGE.format(title="Painted", body=PAINTED))
        elif route == "/top":
            other, n = query["other"][0], query.get("n", ["0"])[0]
            self._send(PAGE.format(title="Top", body=(
                f"<iframe id=f src='http://{other}/frame?n={n}'></iframe>")))
        elif route == "/frame":
            self._send(PAGE.format(title="Frame", body="frame"))
        elif route == "/Console":
            self._send(PAGE.format(title="Console",
                                   body=f"<script>console.log('{CONSOLE}')</script>"))
        # Redirects, the ways an old bookmark ends somewhere else.
        elif route.startswith("/land/"):
            self._send(PAGE.format(title=route[len("/land/"):], body=LAND))
        elif route.startswith("/moved/"):                   # 301
            self._redirect(301, "/land/" + route[len("/moved/"):])
        elif route.startswith("/hop1/"):                    # 302, 302, onto another host name
            self._redirect(302, f"http://localhost:{PORT}/hop2/{route[len('/hop1/'):]}")
        elif route.startswith("/hop2/"):
            self._redirect(302, "/land/" + route[len("/hop2/"):])
        elif route.startswith("/scripted/"):                # a script, while loading
            self._send(PAGE.format(title="Scripted", body=(
                f"<script>location.replace('/land/{route[len('/scripted/'):]}')</script>")))
        elif route.startswith("/later/"):                   # a script, after the load event
            ms = int(query.get("ms", ["300"])[0])
            self._send(PAGE.format(title="Later", body=(
                "<script>addEventListener('load', () => setTimeout(() => "
                f"location.href = '/land/{route[len('/later/'):]}', {ms}))</script>")))
        elif route == "/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="blob.bin"')
            self.send_header("Content-Length", str(len(BLOB)))
            self.end_headers()
            self.wfile.write(BLOB)
        else:
            self.send_error(404)

    def _redirect(self, code: int, where: str) -> None:
        self.send_response(code)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


HTTP = ThreadingHTTPServer(("127.0.0.1", 0), Site)
threading.Thread(target=HTTP.serve_forever, daemon=True).start()
PORT = HTTP.server_address[1]


def url(path: str, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{PORT}{path}"


def requests_to(path_prefix: str) -> list[dict]:
    return [s for s in Site.seen if s["path"].startswith(path_prefix)]


# ── a stand-in for the shell: just the socket ──────────────────────────

class FakeShell:
    """Listens where the shell would, answers {"type": "attached"} to
    every announce, and keeps every message per connection with the time
    it arrived. It grabs no key: Ctrl+L, Ctrl+T, Ctrl+M reach the window
    here as they do when the shell could not grab them."""

    class Conn:
        def __init__(self, sock):
            self.sock, self.msgs, self.times, self.open = sock, [], [], True

        def states(self):
            return self.of("state")

        def of(self, type_):
            return [m for m in self.msgs if m.get("type") == type_]

        def when(self, msg) -> float:
            return next(t for m, t in zip(self.msgs, self.times) if m is msg)

        def send(self, msg):
            self.sock.sendall((json.dumps(msg) + "\n").encode())

    def __init__(self, path: str) -> None:
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        self.conns: list[FakeShell.Conn] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            conn = FakeShell.Conn(sock)
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            for line in conn.sock.makefile("rb"):
                msg = json.loads(line)
                conn.times.append(time.perf_counter())
                conn.msgs.append(msg)
                if msg.get("type") == "announce":
                    conn.send({"type": "attached"})
        except (OSError, ValueError):
            pass
        conn.open = False

    def conn_of(self, window):
        """The open connection a browser window announced itself on."""
        wid = int(window.winId())
        return next((c for c in reversed(self.conns) if c.open and c.msgs
                     and c.msgs[0].get("wid") == wid), None)

    def drop(self, conn) -> None:
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.sock.close()

    def close(self) -> None:
        self.server.close()
        for c in self.conns:
            self.drop(c)
        if os.path.exists(self.path):
            os.unlink(self.path)


# ── 1. pure parts ───────────────────────────────────────────────────────

def test_pure() -> None:
    from PySide6.QtCore import Qt

    from indigoshell.browser import protocol
    from indigoshell.browser.qt import startup
    from indigoshell.browser.qt.identity import ORIGINAL, Rules
    from indigoshell.browser.qt.keys import action_for

    section("1. pure parts")
    K, M = Qt.Key, Qt.KeyboardModifier
    alt, shift, ctrl = M.AltModifier, M.ShiftModifier, M.ControlModifier
    us_shifted = "!@#$%^&*("
    expect = []
    for n in range(1, 10):
        expect.append((f"Alt+{n}", int(K.Key_1) + n - 1, alt, 0, ("tab", n)))
        expect.append((f"Alt+Shift+{n} as Qt sends it on X (US: Alt+{us_shifted[n - 1]}, keycode {9 + n})",
                       ord(us_shifted[n - 1]), alt | shift, 9 + n, ("move", n)))
        expect.append((f"Alt+Shift+{n} as a digit", int(K.Key_1) + n - 1, alt | shift, 0, ("move", n)))
    expect += [
        ("Alt+& (AZERTY 1)", int(K.Key_Ampersand), alt, 10, ("tab", 1)),
        ("Ctrl+T", int(K.Key_T), ctrl, 0, "new_tab"),
        ("Ctrl+W", int(K.Key_W), ctrl, 0, "close_tab"),
        ("Ctrl+Tab", int(K.Key_Tab), ctrl, 0, "next_tab"),
        ("Ctrl+Shift+Tab (X: Backtab)", int(K.Key_Backtab), ctrl | shift, 0, "prev_tab"),
        ("Ctrl+Shift+Tab (Tab)", int(K.Key_Tab), ctrl | shift, 0, "prev_tab"),
        ("Ctrl+L", int(K.Key_L), ctrl, 0, "address"),
        ("Ctrl+M", int(K.Key_M), ctrl, 0, "actions"),
        ("Ctrl+F", int(K.Key_F), ctrl, 0, "find"),
        ("n (find takes it, not the table)", int(K.Key_N), M.NoModifier, 57, None),
        ("Alt+Left", int(K.Key_Left), alt, 0, "back"),
        ("Alt+Right", int(K.Key_Right), alt, 0, "forward"),
        ("Ctrl+R", int(K.Key_R), ctrl, 0, "reload"),
        ("F5", int(K.Key_F5), M.NoModifier, 0, "reload"),
        ("F12", int(K.Key_F12), M.NoModifier, 0, "inspector"),
        ("Ctrl++ (Ctrl+Shift+=)", int(K.Key_Plus), ctrl | shift, 0, "zoom_in"),
        ("Ctrl+=", int(K.Key_Equal), ctrl, 0, "zoom_in"),
        ("Ctrl+keypad +", int(K.Key_Plus), ctrl | M.KeypadModifier, 0, "zoom_in"),
        ("Ctrl+-", int(K.Key_Minus), ctrl, 0, "zoom_out"),
        ("Ctrl+0", int(K.Key_0), ctrl, 0, "zoom_reset"),
        ("a", int(K.Key_A), M.NoModifier, 38, None),
        ("Shift alone", int(K.Key_Shift), shift, 50, None),
        ("Ctrl+Alt+1", int(K.Key_1), ctrl | alt, 10, None),
        ("Ctrl+F5", int(K.Key_F5), ctrl, 0, "hard_reload"),
        ("Ctrl+Shift+R", int(K.Key_R), ctrl | shift, 0, "hard_reload"),
        ("Shift+F5", int(K.Key_F5), shift, 0, "hard_reload"),
    ]
    wrong = [(name, got) for name, key, mods, code, want in expect
             if (got := action_for(key, mods, code)) != want]
    check("key table", not wrong, f"{len(expect)} presses, wrong: {wrong}" if wrong
          else f"{len(expect)} presses resolve as listed")

    rules, warnings = Rules.parse({"identity": {
        "accounts.google.com": "original", "*.example.com": "original",
        "*.deep.example.com": "chrome", "x.deep.example.com": "original",
        "bad host!": "original", "ok.org": "firefox"}})
    cases = {"accounts.google.com": ORIGINAL, "ACCOUNTS.google.com.": ORIGINAL,
             "mail.google.com": "chrome", "example.com": ORIGINAL, "a.example.com": ORIGINAL,
             "notexample.com": "chrome", "y.deep.example.com": "chrome",
             "x.deep.example.com": ORIGINAL, "ok.org": "chrome"}
    wrong = {h: rules.identity_for(h) for h, want in cases.items() if rules.identity_for(h) != want}
    check("sites.json rules: exact, *.domain, most specific wins", not wrong, str(wrong or ""))
    check("sites.json rules: bad entries skipped with a reason",
          len(warnings) == 2 and "bad host!" in warnings[0] + warnings[1], f"{warnings}")
    bad = []
    for data in ([1, 2], {"identity": ["x"]}, "text"):
        try:
            Rules.parse(data)
            bad.append(data)
        except ValueError:
            pass
    check("sites.json rules: a wrong shape is refused as a whole", not bad, str(bad or ""))

    good = protocol.state([protocol.tab("A", "a.org", False)], 0, 100, True, url="https://a.org/",
                          bookmarked=True, find=protocol.find("q", None, None, True))
    plain = protocol.state([protocol.tab("A", "a.org", False)], 0, 100, True, url="",
                           bookmarked=False, find=None)
    broken = [dict(good, current=3), dict(good, progress=101),
              dict(good, tabs=[{"title": "A"}]), dict(good, tabs=[]),
              {"type": "state", "tabs": good["tabs"], "current": 0, "progress": 100,
               "secure": True},
              dict(good, bookmarked="yes"), dict(good, find={"query": 1}),
              dict(good, find=protocol.find("q", -1, 3, False)), dict(good, url=5)]
    check("state check: the documented shape, every key there (find null or an object), "
          "unknown keys ignored; malformed or partial ones refused",
          good["type"] == "state" and protocol.state_problems(good) == []
          and protocol.state_problems(plain) == []
          and protocol.state_problems({**good, "future": 1}) == []
          and all(protocol.state_problems(b) for b in broken),
          f"{[protocol.state_problems(b) for b in broken if not protocol.state_problems(b)]}")
    lines = [b'{"type": "attached"}', b'{"attached": true}', b"[1]", b"nope", b'{"type": 4}']
    check("every message has its type: one without, or with one that is no string, is no "
          "message; the announce, a download, a launcher, a command, the instance's open",
          [protocol.decode(x) for x in lines] == [{"type": "attached"}, None, None, None, None]
          and protocol.announce(7, False) == {"type": "announce", "app": "jack", "wid": 7,
                                              "single": False}
          and protocol.download("f", "done", 100) == {"type": "download", "name": "f",
                                                      "state": "done", "percent": 100}
          and protocol.launcher("address") == {"type": "launcher", "action": "address"}
          and protocol.command("load", url="u", where="tab") == {"type": "load", "url": "u",
                                                                 "where": "tab"}
          and protocol.open_urls(["u"]) == {"type": "open", "urls": ["u"]})
    check("the find line's count: n/total, no matches, nothing before the count",
          [protocol.find_count(protocol.find(q, a, t, True)) for q, a, t in
           (("x", 3, 12), ("x", 0, 0), ("", 0, 0), ("x", None, None))]
          == ["3/12", "no matches", "", ""])

    # Widevine: fake Firefox profiles, fake modules; never the real one.
    root = SCRATCH / "widevine"
    ff = root / "firefox"
    own = root / "data" / "widevine"
    for profile, version, payload in (("a.default", "4.10.2000.0", b"old"),
                                      ("b.default-release", "4.10.3112.0", b"new"),
                                      ("c.junk", "not-a-version", b"junk")):
        d = ff / profile / "gmp-widevinecdm" / version
        d.mkdir(parents=True)
        (d / "libwidevinecdm.so").write_bytes(payload)
        (d / "manifest.json").write_text(json.dumps({"version": version}))
    nothing = startup.resolve_widevine(own_dir=root / "none", roots=[root / "absent"], override=None)
    check("Widevine: none anywhere -> start without", nothing is None, str(nothing))
    got = startup.resolve_widevine(own_dir=own, roots=[root / "absent", ff], override=None)
    check("Widevine: the newest Firefox copy is copied with its manifest",
          got == own / "libwidevinecdm.so" and got.read_bytes() == b"new"
          and json.loads((own / "manifest.json").read_text())["version"] == "4.10.3112.0",
          f"-> {got and got.relative_to(root)}")
    (ff / "b.default-release" / "gmp-widevinecdm" / "4.10.3112.0" / "libwidevinecdm.so").write_bytes(b"newer")
    again = startup.resolve_widevine(own_dir=own, roots=[ff], override=None)
    check("Widevine: our copy is used once it exists", again == got and again.read_bytes() == b"new")
    check("Widevine: off / an explicit path",
          startup.resolve_widevine(own_dir=own, roots=[ff], override="off") is None
          and startup.resolve_widevine(own_dir=root / "none", roots=[], override=str(got)) == got)
    flags = startup.chromium_flags("--foo --bar=1", got)
    flags2 = startup.chromium_flags("--widevine-path=/x.so", got)
    check("Chromium flags keep what was there, add dark, no smooth scrolling and Widevine",
          flags == f"--foo --bar=1 {startup.DARK} {startup.NO_SMOOTH} --widevine-path={got}"
          and flags2 == f"--widevine-path=/x.so {startup.DARK} {startup.NO_SMOOTH}", flags)


# ── 2. one browser, in this process ─────────────────────────────────────

class Log:
    """Every record from the browser's loggers, kept."""

    def __init__(self):
        import logging
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        file = logging.FileHandler(SCRATCH / "browser-in-process.log")
        file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(handler)
        root.addHandler(file)
        root.setLevel(logging.WARNING)
        logging.getLogger(APP).setLevel(logging.DEBUG)

    def messages(self, level=0, since=0):
        return [r.getMessage() for r in self.records[since:] if r.levelno >= level]


def test_in_process() -> None:
    import logging

    import shiboken6
    from PySide6.QtCore import QEvent, QPoint, Qt, QUrl
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from indigoshell.browser import protocol
    from indigoshell.browser.qt import app as browser_app
    from indigoshell.browser.qt import instance, startup
    from indigoshell.browser.qt.identity import CHROME, ORIGINAL
    from indigoshell.browser.qt.profile import make_profile

    logs = Log()
    K, M = Qt.Key, Qt.KeyboardModifier

    def wait(pred, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            QTest.qWait(20)
        return bool(pred())

    def js(page_or_frame, code, timeout=5.0):
        box = []
        page_or_frame.runJavaScript(code, 0, box.append)
        wait(lambda: box, timeout)
        return box[0] if box else None

    def loaded(tab, title=None, timeout=10.0) -> bool:
        return wait(lambda: not tab.loading and tab.page.url().toString() != ""
                    and (title is None or tab.page.title() == title), timeout)

    def press(window, key, mods=M.NoModifier):
        QTest.keyClick(window.windowHandle(), key, mods)
        QTest.qWait(30)

    def titles(window):
        return [t.page.title() for t in window.tabs]

    finished: dict = {}             # loads finished, per tab

    def track(tab):
        if tab not in finished:
            finished[tab] = 0
            tab.page.loadFinished.connect(
                lambda _ok, t=tab: finished.__setitem__(t, finished[t] + 1))

    def navigate(tab, do, *, title=None, part=None, timeout=10.0) -> bool:
        """Do what loads a page in `tab`, then wait for that load (not
        one before it) to finish."""
        track(tab)
        n = finished[tab]
        do()
        return wait(lambda: finished[tab] > n and not tab.loading
                    and (title is None or tab.page.title() == title)
                    and (part is None or part in tab.page.url().toString()), timeout)

    def prompt_up(window) -> bool:
        p = window.prompt
        return p is not None and shiboken6.isValid(p) and p.isVisible()

    def type_text(window, text):
        """Keys that type `text`, as X delivers them: text in the event."""
        for ch in text:
            for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                QApplication.sendEvent(window.windowHandle(), QKeyEvent(
                    kind, int(K.Key_Space) if ch == " " else ord(ch.upper()),
                    M.ShiftModifier if ch.isupper() else M.NoModifier, ch))
        QTest.qWait(30)

    section("2. one browser in this process (off screen)")
    startup.apply_flags()
    app = browser_app.make_application()
    profile = make_profile()
    check("profile where the prototype kept it",
          profile.persistentStoragePath() == str(SCRATCH / "data" / APP / "browser")
          and profile.cachePath().startswith(str(SCRATCH / "cache" / APP / "browser"))
          and profile.storageName() == "browser"
          and profile.persistentCookiesPolicy().name == "ForcePersistentCookies",
          f"{profile.persistentStoragePath()} / {profile.cachePath()}")
    lock = instance.try_lock()
    browser = browser_app.Browser(profile)
    browser.listen()
    sites = Path(os.environ["XDG_CONFIG_HOME"]) / APP / "browser" / "sites.json"
    check("sites.json made on first start with the two Google hosts",
          json.loads(sites.read_text()) == {"identity": {"accounts.google.com": "original",
                                                         "accounts.youtube.com": "original"}})

    w = browser.open_urls([url("/page/Alpha"), url("/page/Beta"), url("/page/Gamma")])
    opened_at = time.monotonic()
    line_at: list[float] = []
    w.link.fallback_changed.connect(lambda on: line_at.append(time.monotonic() - opened_at))
    ok = all(loaded(t) for t in w.tabs)
    wait(lambda: titles(w) == ["Alpha", "Beta", "Gamma"])
    check("three URLs open as three tabs, the first in front",
          ok and titles(w) == ["Alpha", "Beta", "Gamma"] and w.current == 0, f"{titles(w)}")

    # ── the line alone ──────────────────────────────────────────────────
    wait(lambda: w.line.isVisible(), 5)
    QTest.qWait(100)
    text = w.line.text()
    check("no shell: the window's own line shows after three seconds (not before: a shell "
          "reload is back sooner), carrying the tabs and the host",
          not w.link.attached and w.line.isVisible() and line_at and 2.8 <= line_at[0] <= 4.0
          and "1 Alpha" in text and "2 Beta" in text and "3 Gamma" in text
          and "127.0.0.1" in text and "not secure" in text,
          f"shown {line_at[0] if line_at else -1:.2f} s after the window; {text!r}")

    # ── keys ────────────────────────────────────────────────────────────
    print("  keys, delivered to the window's QWindow as X would:")
    press(w, K.Key_2, M.AltModifier)
    a2 = w.current
    press(w, K.Key_3, M.AltModifier)
    a3 = w.current
    press(w, K.Key_1, M.AltModifier)
    a1 = w.current
    press(w, K.Key_9, M.AltModifier)
    check("Alt+2, Alt+3, Alt+1; Alt+9 with three tabs does nothing",
          (a2, a3, a1, w.current) == (1, 2, 0, 0), f"{(a2, a3, a1, w.current)}")
    press(w, K.Key_Tab, M.ControlModifier)
    n1 = w.current
    press(w, K.Key_Tab, M.ControlModifier)
    press(w, K.Key_Tab, M.ControlModifier)
    n2 = w.current
    press(w, K.Key_Backtab, M.ControlModifier | M.ShiftModifier)
    p1 = w.current
    press(w, K.Key_Tab, M.ControlModifier | M.ShiftModifier)
    p2 = w.current
    check("Ctrl+Tab forward and round; Ctrl+Shift+Tab back (as Backtab and as Tab)",
          (n1, n2, p1, p2) == (1, 0, 2, 1), f"{(n1, n2, p1, p2)}")
    press(w, K.Key_1, M.AltModifier | M.ShiftModifier)       # Beta to the front
    m1 = (titles(w), w.current)
    # Alt+Shift+3 as X delivers it on a US layout: "#", keycode 12.
    QApplication.sendEvent(w.windowHandle(), QKeyEvent(
        QEvent.Type.KeyPress, K.Key_NumberSign, M.AltModifier | M.ShiftModifier, 12, 0, 0, "#"))
    QTest.qWait(30)
    m3 = (titles(w), w.current)
    press(w, K.Key_9, M.AltModifier | M.ShiftModifier)
    m9 = (titles(w), w.current)
    check("Alt+Shift+1 moves the tab to the front; Alt+Shift+3 (sent as '#', keycode 12) to third",
          m1 == (["Beta", "Alpha", "Gamma"], 0) and m3 == (["Alpha", "Gamma", "Beta"], 2),
          f"{m1} {m3}")
    check("Alt+Shift+9 with three tabs does nothing", m9 == m3)
    press(w, K.Key_2, M.AltModifier)
    press(w, K.Key_3, M.AltModifier | M.ShiftModifier)        # back to Alpha Beta Gamma
    press(w, K.Key_1, M.AltModifier)

    t0 = time.perf_counter()
    press(w, K.Key_T, M.ControlModifier)
    shown = wait(lambda: prompt_up(w), 3)
    took = (time.perf_counter() - t0) * 1000
    check("Ctrl+T with no shell: the browser's own dialog at once, and no tab until a pick",
          shown and len(w.tabs) == 3 and w.prompt.windowTitle() == "New tab"
          and w.prompt.textValue() == "", f"{len(w.tabs)} tabs; dialog up {took:.0f} ms after "
          "the press (polled)")
    if shown:
        w.prompt.setTextValue(f"127.0.0.1:{PORT}/page/Delta")
        w.prompt.accept()
    ok = wait(lambda: len(w.tabs) == 4) and loaded(w.tabs[3], "Delta")
    check("the dialog's address loads in a new tab, at the end, in front",
          ok and w.current == 3, f"{titles(w)}")
    press(w, K.Key_L, M.ControlModifier)
    shown = wait(lambda: prompt_up(w), 3)
    value = w.prompt.textValue() if shown else None
    if shown:
        w.prompt.reject()
    QTest.qWait(50)
    check("Ctrl+L with no shell: the dialog, holding the current address; Escape opens nothing",
          shown and value == url("/page/Delta") and len(w.tabs) == 4, repr(value))
    tab3 = w.tabs[3]
    epsilon = navigate(tab3, lambda: tab3.page.load(QUrl(url("/page/Epsilon"))), title="Epsilon")
    back = navigate(tab3, lambda: press(w, K.Key_Left, M.AltModifier), title="Delta")
    fwd = navigate(tab3, lambda: press(w, K.Key_Right, M.AltModifier), title="Epsilon")
    check("Alt+Left / Alt+Right: back to the page before and forward again",
          epsilon and back and fwd, f"{epsilon} {back} {fwd}")
    hits = Site.hits.get("/page/Epsilon", 0)
    r1 = navigate(tab3, lambda: press(w, K.Key_R, M.ControlModifier)) \
        and wait(lambda: Site.hits.get("/page/Epsilon", 0) == hits + 1)
    r2 = navigate(tab3, lambda: press(w, K.Key_F5)) \
        and wait(lambda: Site.hits.get("/page/Epsilon", 0) == hits + 2)
    check("Ctrl+R and F5 reload", r1 and r2,
          f"server hits {hits} -> {Site.hits.get('/page/Epsilon')}")

    def last_cache():
        return next((s["cache"] for s in reversed(Site.seen)
                     if s["path"] == "/page/Epsilon"), None)
    plain = last_cache()
    r3 = navigate(tab3, lambda: press(w, K.Key_R, M.ControlModifier | M.ShiftModifier)) \
        and wait(lambda: Site.hits.get("/page/Epsilon", 0) == hits + 3)
    hard = last_cache()
    check("Ctrl+Shift+R reloads past the cache (as Firefox): the request says no-cache",
          r3 and hard == "no-cache" and plain != "no-cache", f"Ctrl+R sent {plain!r}, Ctrl+Shift+R {hard!r}")
    zooms = []
    for key, mods in ((K.Key_Plus, M.ControlModifier | M.ShiftModifier), (K.Key_Equal, M.ControlModifier),
                      (K.Key_Minus, M.ControlModifier), (K.Key_0, M.ControlModifier)):
        press(w, key, mods)
        zooms.append(round(w.tabs[3].page.zoomFactor(), 2))
    check("Ctrl++, Ctrl+=, Ctrl+-, Ctrl+0 zoom", zooms == [1.1, 1.25, 1.1, 1.0], f"{zooms}")
    press(w, K.Key_F12)
    inspector = w.tabs[3].inspector
    ok = inspector is not None and wait(lambda: inspector.isVisible()) and inspector.single
    check("F12 opens the inspector in a window of its own", ok)
    if ok:
        inspector.close()
    esc = QKeyEvent(QEvent.Type.KeyPress, K.Key_Escape, M.NoModifier)
    check("Esc outside fullscreen is the page's, not ours",
          w.eventFilter(w.windowHandle(), esc) is False)

    # ── find, with no shell: the line says it ───────────────────────────
    w.select(0)
    press(w, K.Key_F, M.ControlModifier)
    type_text(w, "alp")
    counted = wait(lambda: w.find_result == (1, 1), 3) and wait(lambda: "find" in w.line.text(), 1)
    typing_text = w.line.text()
    press(w, K.Key_Return)
    stepping = wait(lambda: w.find_phase == "stepping" and "\u258f" not in w.line.text(), 2)
    step_text = w.line.text()
    press(w, K.Key_Escape)
    ended = wait(lambda: w.find_phase is None and "1 Alpha" in w.line.text(), 2)
    check("find with no shell: the line says it, the caret while typing, none while "
          "stepping; Escape gives the tabs back",
          counted and typing_text.startswith("find  alp\u258f  1/1") and stepping
          and step_text.startswith("find  alp  1/1") and ended, f"{typing_text!r}, {step_text!r}")

    for i in range(5, 11):
        w.add_tab(QUrl(url(f"/page/T{i}")), background=True)
    press(w, K.Key_9, M.AltModifier)
    check("Alt+9 with ten tabs is the ninth, not the last",
          len(w.tabs) == 10 and w.current == 8, f"{len(w.tabs)} tabs, current {w.current}")
    w.select(4)
    press(w, K.Key_W, M.ControlModifier)
    after_close = (len(w.tabs), w.current)
    w.select(len(w.tabs) - 1)
    press(w, K.Key_W, M.ControlModifier)
    check("Ctrl+W closes the tab; the next one slides in, or the one before at the end",
          after_close == (9, 4) and (len(w.tabs), w.current) == (8, 7),
          f"{after_close}, then {(len(w.tabs), w.current)}")
    while len(w.tabs) > 3:
        w.close_tab(len(w.tabs) - 1)
    w.select(0)

    # ── what pages ask for ──────────────────────────────────────────────
    print("  requests from pages:")
    w.tabs[0].page.load(QUrl(url("/links")))
    loaded(w.tabs[0], "Links")
    QTest.qWait(300)
    js(w.tabs[0].page, f"window.open('{url('/page/Fore')}')")
    wait(lambda: len(w.tabs) == 4)
    check("window.open: a tab right after its opener, in front",
          len(w.tabs) == 4 and w.current == 1 and loaded(w.tabs[1], "Fore"), f"{titles(w)}")
    w.select(0)
    QTest.qWait(300)
    proxy = w.tabs[0].view.focusProxy()
    for y in (25, 75):
        QTest.mouseClick(proxy, Qt.MouseButton.MiddleButton, M.NoModifier, QPoint(40, y))
        QTest.qWait(150)
    wait(lambda: len(w.tabs) == 6 and all(loaded(t, timeout=0.1) for t in w.tabs))
    wait(lambda: titles(w)[1:3] == ["L1", "L2"])
    check("middle clicks: background tabs after the opener, in click order",
          titles(w)[:4] == ["Links", "L1", "L2", "Fore"] and w.current == 0, f"{titles(w)}")
    QTest.mouseClick(proxy, Qt.MouseButton.LeftButton, M.ShiftModifier, QPoint(40, 125))
    wait(lambda: len(w.tabs) == 7)
    check("Shift+click (a new window): a tab in this window, in front",
          len(browser.windows) == 1 and len(w.tabs) == 7 and w.current == 3
          and loaded(w.tabs[3], "L3"), f"{titles(w)}, {len(browser.windows)} windows")
    w.select(0)
    js(w.tabs[0].page, f"window.open('{url('/page/Pop')}', 'pop', 'width=420,height=300')")
    wait(lambda: len(browser.windows) == 2)
    popup = next((x for x in browser.windows if x is not w), None)
    ok = popup is not None and popup.single and bool(popup.windowFlags() & Qt.WindowType.Dialog) \
        and len(popup.tabs) == 1 and loaded(popup.tabs[0], "Pop")
    check("a popup with a size: its own small floating window, one page",
          ok and len(w.tabs) == 7 and (popup.width(), popup.height()) == (420, 300),
          f"{popup and (popup.width(), popup.height())}")
    if ok:
        js(popup.tabs[0].page, f"window.open('{url('/page/FromPopup')}')")
        wait(lambda: len(w.tabs) == 8)
        check("a new tab asked for inside the popup lands in the main window",
              len(w.tabs) == 8 and len(popup.tabs) == 1 and loaded(w.tabs[-1], "FromPopup"),
              f"{titles(w)}")
        js(popup.tabs[0].page, "window.close()")
        closed = wait(lambda: popup not in browser.windows)
        check("window.close() in the popup closes the popup", closed)
    fore = titles(w).index("Fore")                              # opened by script
    w.select(fore)
    count = len(w.tabs)
    js(w.tabs[fore].page, "window.close()")
    wait(lambda: "Fore" not in titles(w))
    check("window.close() in a tab closes the tab",
          "Fore" not in titles(w) and len(w.tabs) == count - 1, f"{titles(w)}")
    while len(w.tabs) > 1:
        w.close_tab(len(w.tabs) - 1)
    w.tabs[0].page.load(QUrl(url("/page/Alpha")))
    w.add_tab(QUrl(url("/page/Beta")), background=True)
    w.add_tab(QUrl(url("/page/Gamma")), background=True)
    wait(lambda: titles(w) == ["Alpha", "Beta", "Gamma"] and not any(t.loading for t in w.tabs))
    w.select(0)

    # ── the shell link ──────────────────────────────────────────────────
    print("  the shell link (a stand-in shell on a socket of its own):")
    shell = FakeShell(SOCK["apps"])
    attached = wait(lambda: w.link.attached, timeout=5)
    QTest.qWait(50)
    check("a shell that starts later is found (retry every 2 s once the line shows); it shows "
          "the window's state, so the line goes at once, the page at the window's top edge",
          attached and not w.line.isVisible() and w.stack.y() == 0,
          f"line visible {w.line.isVisible()}, page at y {w.stack.y()}")
    conn = shell.conns[0] if shell.conns else None
    wait(lambda: conn and conn.states())
    first = conn.msgs[0] if conn and conn.msgs else {}
    check("announce first: {type: announce, app: jack, wid, single: false} (a main window)",
          first == {"type": "announce", "app": "jack", "wid": int(w.winId()), "single": False},
          f"{first}")
    states = conn.states() if conn else []
    check("then the state, in the documented shape, every key there",
          bool(states) and protocol.state_problems(states[-1]) == []
          and set(states[-1]) == {"type", *protocol.STATE_KEYS, *protocol.OPTIONAL_STATE_KEYS}
          and states[-1]["agent"] == [None, None, None]
          and [t["title"] for t in states[-1]["tabs"]] == ["Alpha", "Beta", "Gamma"]
          and states[-1]["current"] == 0 and states[-1]["secure"] is False
          and states[-1]["url"] == url("/page/Alpha"),
          f"{states[-1] if states else None}")
    mark = len(logs.records)
    press(w, K.Key_M, M.ControlModifier)
    press(w, K.Key_L, M.ControlModifier)
    asked = wait(lambda: prompt_up(w), 3)
    value = w.prompt.textValue() if asked else None
    if asked:
        w.prompt.reject()
    said = [m for m in logs.messages(logging.INFO, mark) if "Ctrl+M" in m]
    check("Ctrl+M and Ctrl+L reaching the window with a shell there (one that could not grab "
          "them): Ctrl+M one line in the log, Ctrl+L the window's own dialog holding the "
          "address; nothing but states said to the shell",
          len(said) == 1 and asked and value == url("/page/Alpha")
          and all(m["type"] == "state" for m in conn.msgs[1:]), f"{said} {value!r}")
    count = len(conn.states())
    for _ in range(5):
        w.refresh()
        QTest.qWait(20)
    QTest.qWait(150)
    unchanged = len(conn.states()) == count
    w.select(2)
    wait(lambda: len(conn.states()) > count)
    QTest.qWait(150)
    new = conn.states()[count:]
    check("only on change: refreshes with nothing new send nothing; a switch sends one",
          unchanged and len(new) == 1 and new[0]["current"] == 2, f"{len(new)} new: {new}")
    every = conn.states()
    dupes = sum(a == b for a, b in zip(every, every[1:]))
    bad = [p for s in every for p in protocol.state_problems(s)]
    check("every state on this connection well formed, none repeated",
          not bad and dupes == 0, f"{len(every)} states, problems {bad[:3]}, repeats {dupes}")
    conn.send({"type": "tab", "index": 0})
    wait(lambda: w.current == 0)
    conn.send({"type": "open", "url": url("/page/Opened")})
    wait(lambda: len(w.tabs) == 4)
    opened = len(w.tabs) == 4 and loaded(w.tabs[3], "Opened") and w.current == 3
    conn.send({"type": "back"})
    QTest.qWait(200)
    conn.send({"type": "close"})
    wait(lambda: len(w.tabs) == 3)
    check("commands from the shell: tab, open, close", opened and len(w.tabs) == 3
          and titles(w) == ["Alpha", "Beta", "Gamma"], f"{titles(w)}")
    w.select(0)

    class Fullscreen:
        def __init__(self, on):
            self.on = on

        def accept(self):
            pass

        def toggleOn(self):
            return self.on

    since = len(conn.msgs)
    w.on_fullscreen(w.tabs[0], Fullscreen(True))
    wait(lambda: w.isFullScreen(), 2)
    on = (w.isFullScreen(), w.line.isVisible())
    w.on_fullscreen(w.tabs[0], Fullscreen(False))
    wait(lambda: not w.isFullScreen(), 2)
    QTest.qWait(100)
    off = (w.isFullScreen(), w.line.isVisible())
    said = [m for m in conn.msgs[since:] if m["type"] != "state"]
    check("fullscreen: no line over the page (the shell shows the state), none after; nothing "
          "but states said to the shell", on == (True, False) and off == (False, False)
          and not said, f"{on} {off} {said}")
    old_wid, old_handle, before = int(w.winId()), w.windowHandle(), len(shell.conns)
    w.destroy()
    w.create()
    w.show()
    told = wait(lambda: len(shell.conns) > before and shell.conns[-1].msgs
                and shell.conns[-1].msgs[0].get("wid") == int(w.winId()), 3)
    press(w, K.Key_2, M.AltModifier)
    key_ok = w.current == 1
    press(w, K.Key_1, M.AltModifier)
    check("Qt replacing the native window: keys taken on the new one, the shell told at once",
          int(w.winId()) != old_wid and w.windowHandle() is not old_handle and told and key_ok,
          f"0x{old_wid:x} -> 0x{int(w.winId()):x}, announced {told}, Alt+2 {key_ok}")
    wait(lambda: w.link.attached, 2)
    shell.drop(shell.conn_of(w))
    dropped = wait(lambda: not w.link.attached, 3)
    check("the line dropped: not attached, and no line of its own yet (that waits three "
          "seconds: below)", dropped and not w.line.isVisible())
    wait(lambda: w.link.attached, 4)
    before = len(shell.conns)
    press(w, K.Key_F12)
    inspector = w.current_tab().inspector
    told = wait(lambda: any(c.msgs and c.msgs[0].get("single") is True
                            for c in shell.conns[before:]), 5)
    said = next((c.msgs[0] for c in shell.conns[before:] if c.msgs
                 and c.msgs[0].get("single") is True), {})
    wid = int(inspector.winId()) if inspector is not None else None
    if inspector is not None:
        inspector.close()
    check("the inspector (a single window) announces itself {single: true}: the shell leaves "
          "its keys to it", told and said == {"type": "announce", "app": "jack", "wid": wid,
                                               "single": True}, f"{said}")

    # ── identity ────────────────────────────────────────────────────────
    print("  identity per site (127.0.0.1 and localhost are the same server):")
    agents = browser.identity.agents
    chrome_ua, original_ua = agents[CHROME], agents[ORIGINAL]
    check("the two identities: Qt's string, and it without the QtWebEngine token",
          "QtWebEngine/" in original_ua and "QtWebEngine" not in chrome_ua
          and profile.httpUserAgent() == chrome_ua, chrome_ua)

    def write_sites(data, raw=None):
        tmp = sites.with_suffix(".tmp")
        tmp.write_text(raw if raw is not None else json.dumps(data))
        os.replace(tmp, sites)                       # the way editors save

    def reloaded_to(want_text, timeout=5):
        return wait(lambda: browser.identity._text == want_text, timeout)

    TOP_JS = ("JSON.stringify({ua: navigator.userAgent, app: navigator.appVersion,"
              " brands: navigator.userAgentData.brands.map(b => b.brand + ' ' + b.version)})")
    BLANK_JS = ("JSON.stringify((() => { const f = document.createElement('iframe');"
                " document.body.appendChild(f); return f.contentWindow.navigator.userAgent; })())")
    tab = w.tabs[0]
    n = [0]

    def visit(host, other):
        """Load host's /top with an iframe from `other`; what each saw.
        Every visit's page is titled Top, so the wait is for this
        visit's address (its n), and then for its frame's document."""
        n[0] += 1
        tag = f"n={n[0]}"
        navigate(tab, lambda: tab.page.load(QUrl(url(f"/top?other={other}:{PORT}&{tag}", host))),
                 title="Top", part=tag)
        frames = []
        wait(lambda: (frames.__setitem__(slice(None), tab.page.mainFrame().children())
                      or (len(frames) == 1 and tag in frames[0].url().toString()
                          and js(frames[0], "document.title") == "Frame")), 5)

        def heard_now():
            return {r["path"].split("?")[0]: r for r in Site.seen if r["path"].endswith(tag)}
        wait(lambda: {"/top", "/frame"} <= set(heard_now()), 5)
        top = json.loads(js(tab.page, TOP_JS) or "{}")
        sub = json.loads(js(frames[0], TOP_JS) or "{}") if frames else {}
        blank = json.loads(js(tab.page, BLANK_JS) or "null")
        return top, sub, blank, heard_now()

    write_sites({"identity": {"localhost": "original"}})
    reloaded_to(sites.read_text())
    top, sub, blank, heard = visit("127.0.0.1", "localhost")
    check("127.0.0.1 page, localhost iframe: headers chrome / original",
          heard.get("/top", {}).get("ua") == chrome_ua
          and heard.get("/frame", {}).get("ua") == original_ua,
          f"top {heard.get('/top', {}).get('ua', '?')[-40:]!r}, "
          f"frame {heard.get('/frame', {}).get('ua', '?')[-40:]!r}")
    check("... and navigator.userAgent / appVersion likewise, per frame",
          top.get("ua") == chrome_ua and sub.get("ua") == original_ua
          and top.get("app") == chrome_ua[len("Mozilla/"):]
          and sub.get("app") == original_ua[len("Mozilla/"):])
    top2, sub2, blank2, heard2 = visit("localhost", "127.0.0.1")
    check("localhost page, 127.0.0.1 iframe: the other way round (headers and navigator)",
          heard2.get("/top", {}).get("ua") == original_ua
          and heard2.get("/frame", {}).get("ua") == chrome_ua
          and top2.get("ua") == original_ua and sub2.get("ua") == chrome_ua)
    check("an about:blank frame made by a mapped page shows the mapped identity too",
          blank2 == original_ua and blank == chrome_ua, f"{(blank2 or '')[-40:]!r}")
    hints = {r["ch"] for r in Site.seen if r["ch"]}
    brands = {json.dumps(x.get("brands")) for x in (top, sub, top2, sub2)}
    check("client hints untouched: one sec-ch-ua for both identities, no Google Chrome brand",
          len(hints) == 1 and "Google Chrome" not in next(iter(hints), "") and len(brands) == 1
          and "Google Chrome" not in json.dumps(profile.clientHints().fullVersionList()),
          f"{hints} {brands}")
    write_sites({"identity": {"127.0.0.1": "original"}})
    edited = reloaded_to(sites.read_text())
    top3, sub3, _, heard3 = visit("127.0.0.1", "localhost")
    check("sites.json edited while running: takes effect on the next load, no restart",
          edited and heard3.get("/top", {}).get("ua") == original_ua
          and heard3.get("/frame", {}).get("ua") == chrome_ua
          and top3.get("ua") == original_ua and sub3.get("ua") == chrome_ua)
    write_sites({"identity": {"*.localhost": "original"}})
    reloaded_to(sites.read_text())
    tab.page.load(QUrl(url("/page/Wild", "a.localhost")))
    loaded(tab, "Wild")
    wild = [r["ua"] for r in Site.seen if r["path"] == "/page/Wild"]
    wild_nav = js(tab.page, "navigator.userAgent")
    top4, sub4, _, heard4 = visit("localhost", "127.0.0.1")
    check("*.localhost covers a.localhost and localhost itself, not 127.0.0.1",
          wild == [original_ua] and wild_nav == original_ua
          and heard4.get("/top", {}).get("ua") == original_ua
          and heard4.get("/frame", {}).get("ua") == chrome_ua)
    warned = len(logs.records)
    write_sites(None, raw="{ this is not json")
    reloaded_to("{ this is not json")
    QTest.qWait(100)
    warning = [m for m in logs.messages(logging.WARNING, warned) if "sites.json" in m]
    top5, _, _, heard5 = visit("localhost", "127.0.0.1")
    check("a malformed sites.json: a warning, and the rules before it stay",
          bool(warning) and heard5.get("/top", {}).get("ua") == original_ua
          and top5.get("ua") == original_ua, f"{warning[:1]}")
    write_sites({"identity": {"accounts.google.com": "original"}})
    reloaded_to(sites.read_text())

    # ── downloads ───────────────────────────────────────────────────────
    conn = shell.conn_of(w)
    since = len(conn.msgs)
    tab.page.load(QUrl(url("/download")))
    target = DOWNLOADS / "blob.bin"
    done = wait(lambda: any(m["type"] == "download" and m["state"] == "done"
                            for m in conn.msgs[since:]), 10)
    notes = [m for m in conn.msgs[since:] if m["type"] == "download"]
    check("a download lands in the download directory; the shell hears started and done, as "
          "{type: download, name, state, percent}",
          done and target.exists() and target.read_bytes() == BLOB
          and notes[0]["state"] == "started" and notes[-1]["state"] == "done"
          and set(notes[-1]) == {"type", "name", "state", "percent"}
          and notes[-1]["name"] == "blob.bin",
          f"{[n_['state'] for n_ in notes]}, {target.stat().st_size if target.exists() else 'no file'}")

    # ── the launcher's answers, find, bookmarks ─────────────────────────
    print("  what the shell's launcher sends (it took the keys: tests/topbar.py and "
          "tests/browser_launcher.py):")
    w.select(0)
    tab = w.tabs[0]
    navigate(tab, lambda: tab.page.load(QUrl(url("/text"))), title="Text")
    wait(lambda: conn.states() and conn.states()[-1].get("url") == url("/text"))
    st = conn.states()[-1] if conn.states() else {}
    check("the state carries the address, bookmarked and find",
          st.get("url") == url("/text") and st.get("bookmarked") is False
          and st.get("find") is None and protocol.state_problems(st) == [], f"{sorted(st)}")

    def page_keys():
        return json.loads(js(tab.page, "JSON.stringify(window.keys)") or "null")

    def clear_keys():
        """And the window active again, as a person's focused Jack is: the
        dialogs above take the activation, and an inactive window here
        hands its keys to no widget."""
        w.activateWindow()
        wait(lambda: w.isActiveWindow(), 2)
        js(tab.page, "window.keys = []")

    tabs_before = len(w.tabs)
    here = navigate(tab, lambda: conn.send({"type": "load", "url": url("/page/Here"),
                                            "where": "here"}), title="Here")
    check("{type: load, where: here}: in the current tab, no new tab",
          here and w.tabs[0] is tab and len(w.tabs) == tabs_before)

    timing = {}
    real_add = w.add_tab

    def timed_add(*args, **kwargs):
        timing["add"] = time.perf_counter()
        tab_ = real_add(*args, **kwargs)
        timing["made"] = time.perf_counter()
        tab_.page.loadFinished.connect(lambda _ok: timing.setdefault("loaded", time.perf_counter()))
        return tab_
    w.add_tab = timed_add
    sent_epoch = time.time() * 1000
    t_sent = time.perf_counter()
    conn.send({"type": "load", "url": url("/painted"), "where": "tab"})
    made = wait(lambda: len(w.tabs) == tabs_before + 1 and "loaded" in timing, 10)
    w.add_tab = real_add
    new = w.tabs[-1]
    painted = wait(lambda: js(new.page, "window.painted || 0"), 5)
    painted_at = js(new.page, "window.painted || 0") or 0
    check("{type: load, where: tab}: a new tab, at the end, in front",
          made and w.current == len(w.tabs) - 1 and new.page.title() == "Painted")
    if made and painted:
        print(f"  (timing) picked -> tab made {(timing['made'] - t_sent) * 1000:.1f} ms (the tab "
              f"itself {(timing['made'] - timing['add']) * 1000:.1f} ms), page loaded "
              f"{(timing['loaded'] - t_sent) * 1000:.0f} ms, first frame painted "
              f"{painted_at - sent_epoch:.0f} ms", flush=True)
    w.close_tab(new)
    w.select(0)

    # The shell took the key but has no launcher: the window asks itself.
    asked = {}
    for action in ("new_tab", "address"):
        conn.send({"type": "prompt", "action": action})
        shown = wait(lambda: prompt_up(w), 3)
        asked[action] = (w.prompt.windowTitle(), w.prompt.textValue()) if shown else None
        if shown:
            w.prompt.reject()
        QTest.qWait(50)
    mark = len(logs.records)
    conn.send({"type": "prompt", "action": "actions"})
    conn.send({"type": "prompt", "action": "nonsense"})
    conn.send({"type": "from-the-future", "x": 1})
    QTest.qWait(200)
    said = [m for m in logs.messages(0, mark)
            if "Ctrl+M" in m or "not understood" in m]
    check("{type: prompt}: the window's own answer, as for the key: new_tab the dialog empty, "
          "address the dialog holding the address, actions a line in the log; nothing made; a "
          "prompt for no action and a type it does not know ignored with a debug line each",
          asked == {"new_tab": ("New tab", ""), "address": ("Open", url("/page/Here"))}
          and len(w.tabs) == tabs_before and len(said) == 3, f"{asked} {said}")

    # Find left open under the launcher: a load into its tab ends it.
    clear_keys()
    press(w, K.Key_F, M.ControlModifier)
    type_text(w, "her")
    opened = wait(lambda: w.find_phase == "typing", 2)
    navigate(tab, lambda: conn.send({"type": "load", "url": url("/text"), "where": "here"}),
             title="Text")
    check("find open when the shell loads an address into its tab (the launcher had the keys "
          "meanwhile): the new page ends it", opened and w.find_phase is None
          and wait(lambda: conn.states()[-1].get("find") is None, 2))

    # A key's release goes where its press went.
    clear_keys()
    handle = w.windowHandle()
    taken = w.eventFilter(handle, QKeyEvent(QEvent.Type.KeyPress, K.Key_9, M.AltModifier))
    ours = w.eventFilter(handle, QKeyEvent(QEvent.Type.KeyRelease, K.Key_9, M.AltModifier))
    stray = w.eventFilter(handle, QKeyEvent(QEvent.Type.KeyRelease, K.Key_Return,
                                            M.NoModifier, "\r"))
    check("a release goes where its press went: Alt+9's (the window took the press) not the "
          "page's; one whose press the window never saw (the launcher's last key, let go after "
          "it closed) the page's", taken is True and ours is True and stray is False)

    # ── find in page ────────────────────────────────────────────────────
    navigate(tab, lambda: tab.page.load(QUrl(url("/text"))), title="Text")
    clear_keys()

    def find_state():
        states_ = conn.states()
        return states_[-1].get("find") if states_ else "no state"

    def wait_find(**want):
        return wait(lambda: isinstance(find_state(), dict)
                    and all(find_state().get(k) == v for k, v in want.items()), 3)

    def selection():
        return js(tab.page, "window.getSelection().toString()")

    last = w.find_query
    press(w, K.Key_F, M.ControlModifier)
    opened = wait_find(query=last, typing=True) and w.find_fresh
    type_text(w, "bravo")
    counted = wait_find(query="bravo", active=1, total=3, typing=True)
    check("Ctrl+F: typing, the last query back; searched as typed, case-insensitive for a "
          "lower-case query (3 of Bravo bravo BRAVO); the shell told {query, active, total, "
          "typing}",
          opened and counted, f"{find_state()}")
    press(w, K.Key_Backspace)
    edited = wait_find(query="brav", total=3)
    type_text(w, "o")
    press(w, K.Key_Return)
    stepping = wait_find(query="bravo", typing=False)
    press(w, K.Key_N)
    nxt = wait_find(active=2)
    type_text(w, "N")
    prev = wait_find(active=1)
    check("Backspace edits; Enter: stepping (no caret); n the next match, N the previous",
          edited and stepping and nxt and prev, f"{find_state()}")
    seen = page_keys()
    press(w, K.Key_Down)
    type_text(w, " ")
    press(w, K.Key_X)
    passed = page_keys()
    chord = ["down:Control", "up:Control"]      # Ctrl+F's own, pressed before find was on
    check("while finding the page saw no key find took (n, N, the typing, Enter, presses "
          "or releases); arrows and Space still scroll it; other plain keys are swallowed",
          seen == chord and passed == chord + ["down:ArrowDown", "up:ArrowDown", "down: ", "up: "],
          f"during: {seen}, then {passed}")
    press(w, K.Key_Return)
    ended = wait(lambda: find_state() is None, 2)
    QTest.qWait(150)
    landed = selection()
    clear_keys()
    press(w, K.Key_N)
    QTest.qWait(150)
    check("Enter while stepping: find ends, the match left selected; n is the page's again",
          ended and landed == "Bravo" and page_keys() == ["down:n", "up:n"],
          f"selected {landed!r}, page {page_keys()}")
    press(w, K.Key_F, M.ControlModifier)
    back_on = wait_find(query="bravo", typing=True)
    type_text(w, "Bravo")
    smart = wait_find(query="Bravo", total=1)
    check("Ctrl+F again: the last query, replaced by the first key typed; an upper-case "
          "letter makes it case-sensitive (1 match)", back_on and smart, f"{find_state()}")
    for _ in range(5):
        press(w, K.Key_Backspace)
    type_text(w, "şeker")
    turkish = wait_find(query="şeker", total=2)
    for _ in range(5):
        press(w, K.Key_Backspace)
    type_text(w, "Şeker")
    turkish_case = wait_find(query="Şeker", total=1)
    check("Turkish letters typed into the query; ş finds Şeker too, Ş only itself",
          turkish and turkish_case, f"{find_state()}")
    for _ in range(5):
        press(w, K.Key_Backspace)
    type_text(w, "zzz")
    none = wait_find(query="zzz", total=0)
    count_text = protocol.find_count(find_state()) if none else None
    press(w, K.Key_Return)
    check("no matches: the count says so; Enter ends find",
          none and count_text == "no matches" and wait(lambda: find_state() is None, 2))
    press(w, K.Key_F, M.ControlModifier)
    type_text(w, "bravo")
    wait_find(query="bravo", total=3)
    press(w, K.Key_Return)
    wait_find(typing=False)
    press(w, K.Key_N)
    wait_find(active=2)
    press(w, K.Key_Escape)
    ended = wait(lambda: find_state() is None, 2)
    QTest.qWait(150)
    check("Escape while stepping: find ends and the highlight goes (nothing selected)",
          ended and selection() == "", repr(selection()))
    press(w, K.Key_F, M.ControlModifier)
    type_text(w, "bra")
    wait_find(query="bra", total=3)
    # Through the window, as X delivers a click: the window sees it first.
    QTest.mouseClick(w.windowHandle(), Qt.MouseButton.LeftButton, M.NoModifier,
                     QPoint(20, w.stack.y() + 300))
    clicked = wait(lambda: find_state() is None, 2)
    clear_keys()
    type_text(w, "q")
    check("a click in the page ends find; what is typed next is the page's",
          clicked and page_keys() == ["down:q", "up:q"], f"{page_keys()}")
    press(w, K.Key_F, M.ControlModifier)
    type_text(w, "bra")
    wait_find(query="bra", total=3)
    press(w, K.Key_Tab, M.ControlModifier)
    switched = wait(lambda: w.current == 1 and w.find_phase is None, 2)
    w.select(0)
    press(w, K.Key_F, M.ControlModifier)
    press(w, K.Key_2, M.AltModifier)
    alt_switched = wait(lambda: w.current == 1 and w.find_phase is None, 2)
    w.select(0)
    check("in find mode Ctrl+Tab and Alt+2 still switch tabs, and switching ends find",
          switched and alt_switched)

    # ── bookmarks ───────────────────────────────────────────────────────
    marks = Path(os.environ["XDG_CONFIG_HOME"]) / APP / "browser" / "bookmarks.json"
    navigate(tab, lambda: tab.page.load(QUrl(url("/page/Here"))), title="Here")

    def bookmarked():
        states_ = conn.states()
        return states_[-1].get("bookmarked") if states_ else None

    conn.send({"type": "bookmark", "op": "add"})
    added = wait(lambda: bookmarked() is True and marks.exists(), 3)
    data = json.loads(marks.read_text()) if marks.exists() else None
    check("{cmd: bookmark, op: add}: bookmarks.json gets {name: the title, url, added}; "
          "the state says bookmarked",
          added and len(data) == 1 and data[0]["name"] == "Here"
          and data[0]["url"] == url("/page/Here") and isinstance(data[0]["added"], int),
          f"{data}")
    ino = marks.stat().st_ino
    conn.send({"type": "bookmark", "op": "remove"})
    removed = wait(lambda: bookmarked() is False, 3)
    leftovers = [p.name for p in marks.parent.iterdir() if p.name.startswith(".bookmarks-")]
    check("op: remove: out of the file and the state; the file replaced whole (new inode, "
          "no temporary left)", removed and json.loads(marks.read_text()) == []
          and marks.stat().st_ino != ino and not leftovers, f"{leftovers}")

    def hand_edit(text):
        tmp = marks.with_suffix(".edit")
        tmp.write_text(text)
        os.replace(tmp, marks)              # the way editors save

    hand_edit(json.dumps([{"name": "Renamed by hand", "url": url("/page/Here"), "added": 5}]))
    picked_up = wait(lambda: bookmarked() is True, 3)
    names = [e["name"] for e in browser.bookmarks.file.entries]
    check("a hand edit is picked up: the page is a bookmark again, under the new name",
          picked_up and names == ["Renamed by hand"], f"{names}")
    warned = len(logs.records)
    hand_edit("[{ not json")
    complained = wait(lambda: any("bookmarks.json" in m
                                  for m in logs.messages(logging.WARNING, warned)), 3)
    conn.send({"type": "bookmark", "op": "remove"})
    QTest.qWait(300)
    check("a malformed file: a warning, the bookmarks before it kept, and not written over",
          complained and bookmarked() is True and marks.read_text() == "[{ not json")
    hand_edit("[]")
    wait(lambda: bookmarked() is False, 3)

    # ── bookmarks whose address now leads elsewhere ─────────────────────
    print("  bookmarks that redirect (imported ones, with keys of their own):")
    from indigoshell.browser import places
    from indigoshell.browser.qt import tabs as tabs_mod
    imported = [
        {"name": "Moved", "url": url("/moved/A"), "added": 1, "folder": "Imported"},
        {"name": "Chain", "url": url("/hop1/B"), "added": 2, "folder": "Imported"},
        {"name": "Landing", "url": url("/land/B", "localhost"), "added": 3, "folder": "Imported"},
        {"name": "Scripted", "url": url("/scripted/C"), "added": 4, "folder": "Imported"},
        {"name": "Later", "url": url("/later/D?ms=300"), "added": 5, "folder": "Imported"},
        {"name": "Touched", "url": url("/later/E?ms=1500"), "added": 6, "folder": "Imported"},
        {"name": "Keep", "url": url("/page/Keep"), "added": 7, "folder": "Toolbar", "tags": ["x"]},
    ]
    hand_edit(json.dumps(imported, indent=2))
    wait(lambda: len(browser.bookmarks.file.entries) == len(imported), 3)
    left = list(imported)                   # what the file should hold, as it was written

    def file_holds(entries) -> bool:
        return json.loads(marks.read_text()) == entries

    seen_nav, seen_loads = [], []

    def on_nav(request):
        if request.isMainFrame():
            seen_nav.append(request.navigationType().name.replace("Navigation", ""))

    def on_loading(info):
        if info.status().name != "LoadStartedStatus":
            seen_loads.append(info.status().name.replace("Status", ""))

    tab.page.navigationRequested.connect(on_nav)
    tab.page.loadingChanged.connect(on_loading)

    def state_now():
        states_ = conn.states()
        return states_[-1] if states_ else {}

    def open_mark(mark, land):
        """What the address list does with a picked bookmark: load it in
        this tab. Then until the shell has the landing page's state."""
        seen_nav.clear()
        seen_loads.clear()
        done = navigate(tab, lambda: conn.send({"type": "load", "url": mark["url"],
                                                "where": "here"}), title=land)
        return done and wait(lambda: state_now().get("url") == tab.url().toString(), 3)

    def counts(mark) -> bool:
        """The page counts as `mark`: bookmarked, at its own address."""
        st = state_now()
        return (st.get("bookmarked") is True and "/land/" in st.get("url", "")
                and places.page_key(tab.came_from or "") == places.page_key(mark["url"]))

    def remove_current():
        conn.send({"type": "bookmark", "op": "remove"})       # Ctrl+M's Remove bookmark

    def click_away():
        QTest.qWait(300)                    # laid out before the click
        QTest.mouseClick(w.windowHandle(), Qt.MouseButton.LeftButton, M.NoModifier,
                         QPoint(100, w.stack.y() + 60))

    w.select(w.tabs.index(tab))
    cases = [
        ("a 301", imported[0], "A", ["Typed", "Redirect"], 1, True),
        ("a 302 chain onto another host name", imported[1], "B",
         ["Typed", "Redirect", "Redirect"], 1, True),
        ("a script's location.replace while it loads", imported[3], "C",
         ["Typed", "Redirect"], 1, True),
        ("a script's navigation 300 ms after the load event", imported[4], "D",
         ["Typed", "LinkClicked"], 2, False),
    ]
    for what, mark, land, kinds, loads, requested in cases:
        text_before = marks.read_text()
        ok = open_mark(mark, land)
        check(f"{what}: the page it lands on counts as the bookmark (state bookmarked, its own "
              "address in the state); the file not rewritten", ok and counts(mark)
              and marks.read_text() == text_before,
              f"state {state_now().get('url')} bookmarked {state_now().get('bookmarked')}, "
              f"came from {tab.came_from}")
        check(f"... as measured: navigationRequested {kinds}, {loads} finished load(s), "
              f"requestedUrl {'the bookmark' if requested else 'not the bookmark'}",
              seen_nav == kinds and seen_loads == ["LoadSucceeded"] * loads
              and (places.page_key(tab.page.requestedUrl().toString())
                   == places.page_key(mark["url"])) == requested,
              f"{seen_nav}, {seen_loads}, requested {tab.page.requestedUrl().toString()}")

        if land == "A":
            # The memory ends with the user's next navigation.
            away = navigate(tab, click_away, title="Away")
            after_link = (state_now().get("bookmarked"), tab.came_from)
            back = navigate(tab, lambda: press(w, K.Key_Left, M.AltModifier), title="A")
            back = back and wait(lambda: state_now().get("url") == tab.url().toString(), 2)
            after_back = (state_now().get("bookmarked"), tab.came_from)
            check("a link followed from there ends it, and so does going back to it",
                  away and back and after_link == (False, None) and after_back == (False, None),
                  f"after the link {after_link}, back {after_back}")
            open_mark(mark, land)
            typed = navigate(tab, lambda: conn.send({"type": "load", "url": url("/page/Here"),
                                                     "where": "here"}), title="Here")
            check("... and a new address", typed and wait(lambda: state_now().get("bookmarked")
                                                         is False, 2) and tab.came_from is None)
            open_mark(mark, land)
        if land == "B":
            reloaded = navigate(tab, lambda: press(w, K.Key_F5), title="B")
            check("a reload keeps it", reloaded and counts(mark), f"came from {tab.came_from}")
        if land == "D":
            # Past the window a page's own navigation is a beginning.
            tabs_mod.CLIENT_REDIRECT_S = 0.1
            try:
                open_mark(mark, land)
                late = (state_now().get("bookmarked"), tab.came_from)
            finally:
                tabs_mod.CLIENT_REDIRECT_S = 5.0
            check(f"... only soon after the load: with the window at 0.1 s it is a page of its "
                  "own", late == (False, None), f"{late}")
            open_mark(mark, land)

        remove_current()
        left.remove(mark)
        if land == "B":
            # Its own address is a bookmark too: the one it came from goes first.
            first = wait(lambda: file_holds(left), 3)
            still = wait(lambda: state_now().get("bookmarked") is True, 1)
            remove_current()
            left.remove(imported[2])
            gone = wait(lambda: file_holds(left), 3)
            check("landing on a bookmark of its own: Remove takes the one it came from first, "
                  "then the page's own", first and still and gone, f"{json.loads(marks.read_text())}")
        removed = wait(lambda: file_holds(left), 3) and wait(
            lambda: state_now().get("bookmarked") is False, 2)
        check(f"... Remove (Ctrl+M, no url) takes out that bookmark, by its own address; every "
              "other entry as it was, folder and other keys included",
              removed, f"{[e['name'] for e in json.loads(marks.read_text())]}")

    # A click on the page before it moves on by itself: the user's doing.
    touched = imported[5]
    first = navigate(tab, lambda: conn.send({"type": "load", "url": touched["url"],
                                             "where": "here"}), title="Later")
    click = (first and wait(lambda: state_now().get("url") == tab.url().toString(), 2)
             and state_now().get("bookmarked") is True)
    QTest.mouseClick(w.windowHandle(), Qt.MouseButton.LeftButton, M.NoModifier,
                     QPoint(300, w.stack.y() + 300))
    moved = navigate(tab, lambda: None, title="E", timeout=5)
    check("a page that moves on by itself after a click on it: a page of its own, not the "
          "bookmark", click and moved and state_now().get("bookmarked") is False
          and tab.came_from is None, f"came from {tab.came_from}")

    # The address list's Shift+Delete: a named address, whatever is shown.
    shown_url = tab.url().toString()
    conn.send({"type": "bookmark", "op": "remove", "url": url("/page/Keep")})
    left.remove(imported[6])
    named = wait(lambda: file_holds(left), 3)
    conn.send({"type": "bookmark", "op": "add", "url": url("/page/Unseen")})
    added_named = wait(lambda: any(e["url"] == url("/page/Unseen")
                                   for e in json.loads(marks.read_text())), 3)
    entry = next((e for e in json.loads(marks.read_text()) if e["url"] == url("/page/Unseen")), {})
    conn.send({"type": "bookmark", "op": "remove", "url": url("/page/Unseen")})
    named_gone = wait(lambda: file_holds(left), 3)
    check("{cmd: bookmark, op: remove, url}: that address's bookmark, not the page shown's; "
          "add with a url names it by its address when no tab shows it",
          named and added_named and entry.get("name") == url("/page/Unseen") and named_gone
          and tab.url().toString() == shown_url, f"{json.loads(marks.read_text())}")
    tab.page.navigationRequested.disconnect(on_nav)
    tab.page.loadingChanged.disconnect(on_loading)
    hand_edit("[]")
    wait(lambda: not browser.bookmarks.file.entries, 3)

    # ── history ─────────────────────────────────────────────────────────
    from indigoshell.browser import places
    reader = places.HistoryReader(Path(os.environ["XDG_DATA_HOME"]) / APP
                                  / "browser-history.sqlite")

    def history():
        reader.close()                      # a fresh read each time, as the launcher opens
        return {v.url: v for v in reader.candidates("", 10_000)}

    navigate(tab, lambda: tab.page.load(QUrl("http://127.0.0.1:1/nothing")))
    navigate(tab, lambda: tab.page.load(QUrl("data:text/html,<title>D</title>")))
    navigate(tab, lambda: tab.page.load(QUrl(url("/late"))))
    late = wait(lambda: history().get(url("/late")) is not None
                and history()[url("/late")].title == "Late title", 3)
    navigate(tab, lambda: tab.page.load(QUrl(url("/text"))), title="Text")
    js(tab.page, "history.pushState({}, '', '/pushed')")
    pushed = wait(lambda: url("/pushed") in history(), 3)
    visits = history().get(url("/pushed"))
    js(tab.page, "location.hash = 'x'")
    QTest.qWait(200)
    after_hash = history().get(url("/pushed"))
    h = history()
    check("history: pages loaded are there with their titles and visit counts",
          h.get(url("/page/Alpha")) is not None and h[url("/page/Alpha")].title == "Alpha"
          and h[url("/page/Here")].visits >= 2, f"{len(h)} pages")
    check("not there: about:blank, a data: page, a load that failed",
          not any(u.startswith(("about:", "data:")) for u in h)
          and not any("127.0.0.1:1/" in u for u in h), f"{[u for u in h if ':1/' in u]}")
    check("a title that arrives after the load is recorded; a pushState move counts as a "
          "visit, an anchor on it does not", late and pushed and visits is not None
          and after_hash is not None and after_hash.visits == visits.visits)
    reader.close()

    # ── the shell away, and back ────────────────────────────────────────
    print("  the shell away and back (the line waits three seconds):")
    shell.close()
    gone_at = time.monotonic()
    before_line = len(line_at)
    wait(lambda: not w.link.attached, 2)
    QTest.qWait(1800)
    early = w.line.isVisible()
    shown = wait(lambda: w.line.isVisible(), 4)
    after = time.monotonic() - gone_at
    check("shell gone: no line for three seconds, then the line with the tabs",
          not early and shown and 2.8 <= after <= 4.0 and "1 " in w.line.text(),
          f"shown {after:.2f} s after the shell went; {w.line.text()[:40]!r}")
    lone = browser.open_urls([])        # a new window wanting an address, no shell anywhere
    alone = wait(lambda: prompt_up(lone), 3)
    if alone:
        lone.prompt.reject()
    lone.close()
    wait(lambda: lone not in browser.windows, 3)
    check("a new window with no address and no shell to ask: its own dialog at once", alone)
    shell = FakeShell(SOCK["apps"])
    t0 = time.monotonic()
    hidden = wait(lambda: w.link.attached and not w.line.isVisible(), 4)
    check("a shell back: the line goes (the retry is every 2 s while it shows)",
          hidden, f"{time.monotonic() - t0:.2f} s")
    # A reload: the shell gone for 1.5 s and back (the daemon re-executes
    # itself in about that). The line must never show for it.
    flips = len(line_at)
    shell.close()
    QTest.qWait(1500)
    shell = FakeShell(SOCK["apps"])
    t0 = time.monotonic()
    back = wait(lambda: w.link.attached, 3)
    QTest.qWait(3500)
    check("a reload-sized gap (1.5 s): back within half a second's retry, and the line never "
          "showed (the page did not move)",
          back and len(line_at) == flips and not w.line.isVisible(),
          f"reattached {time.monotonic() - t0 - 3.5:.2f} s after the shell came back; "
          f"line changes {len(line_at) - flips}")
    # A reload that says so, and takes longer than three seconds.
    from indigoshell.browser.qt import link as link_mod
    flips = len(line_at)
    shell.conn_of(w).send({"type": "restarting"})
    told = wait(lambda: w.link.restarting, 2)
    shell.close()
    wait(lambda: not w.link.attached, 2)
    interval = w.link.away.interval()
    QTest.qWait(4500)
    quiet = not w.line.isVisible() and len(line_at) == flips
    shell = FakeShell(SOCK["apps"])
    back = wait(lambda: w.link.attached, 3)
    check("a shell that says it is restarting: the line waits ten seconds instead of three; "
          "back after 4.5 s, the line never showed",
          told and interval == link_mod.RESTART_MS == 10000 and quiet and back
          and not w.link.restarting and not w.line.isVisible(),
          f"away timer {interval} ms; line changes {len(line_at) - flips}")
    assert before_line <= len(line_at)

    # ── URLs handed over: the most recently used window ─────────────────
    print("  URLs handed over by a second invocation (a thread here):")
    w2 = browser.open_urls([])          # a second window, blank, wanting an address
    wanted = wait(lambda: shell.conn_of(w2) is not None
                  and {"type": "launcher", "action": "address"} in shell.conn_of(w2).msgs, 5)
    check("a new window with no address, a shell there: once the shell has the window, "
          "{type: launcher, action: address} (its launcher, as for Ctrl+L); no dialog of its "
          "own", wanted and not prompt_up(w2))

    def use(window) -> bool:
        """What a user does: activate it. The browser follows activation."""
        window.activateWindow()
        return wait(lambda: browser.windows[0] is window, 3)

    check("activating a window makes it the most recently used",
          use(w2) and use(w), f"{[titles(x) for x in browser.windows]}")

    def hand_off(urls):
        result = {}

        def client():
            try:
                result["fd"] = instance.claim_or_hand_off(urls, timeout=10)
            except Exception as e:      # noqa: BLE001
                result["error"] = repr(e)

        t = threading.Thread(target=client)
        t.start()
        wait(lambda: not t.is_alive(), 12)
        return result

    before = (len(w.tabs), len(w2.tabs))
    r = hand_off([url("/page/Handed")])
    ok = r.get("fd", "x") is None and wait(lambda: len(w.tabs) == before[0] + 1)
    check("into the most recently used window, at the end, in front",
          ok and len(w2.tabs) == before[1] and w.current == len(w.tabs) - 1
          and loaded(w.tabs[-1], "Handed"), f"{r}, tabs {before} -> {(len(w.tabs), len(w2.tabs))}")
    use(w2)
    r = hand_off([url("/page/Second")])
    check("... and after the other window was used, into that one",
          r.get("fd", "x") is None and wait(lambda: len(w2.tabs) == before[1] + 1)
          and loaded(w2.tabs[-1], "Second"))
    count = len(browser.windows)
    r = hand_off([])
    ok = r.get("fd", "x") is None and wait(lambda: len(browser.windows) == count + 1)
    check("no URL: a new window", ok, f"{count} -> {len(browser.windows)} windows")

    def ask_instance(line: bytes) -> bytes:
        """One line to the instance socket, its answer, from a thread."""
        got = []

        def client():
            s_ = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s_.settimeout(5)
            try:
                s_.connect(SOCK["browser"])
                s_.sendall(line)
                got.append(s_.recv(4096))
            except OSError as e:
                got.append(repr(e).encode())
            finally:
                s_.close()

        t = threading.Thread(target=client)
        t.start()
        wait(lambda: not t.is_alive(), 6)
        return got[0] if got else b""

    count = len(browser.windows)
    wrong = ask_instance(b'{"type": "open", "urls": "not a list"}\n')
    unknown = ask_instance(b'{"type": "from-the-future"}\n')
    check("the instance socket: {type: open} with no list answered {type: error, error}; a type "
          "it does not know answered nothing (the line closed); no window made",
          json.loads(wrong or b"{}").get("type") == "error" and unknown == b""
          and len(browser.windows) == count, f"{wrong!r} {unknown!r}")

    # ── closing the last tab ────────────────────────────────────────────
    w3 = browser.windows[0]
    w3.show()
    press(w3, K.Key_W, M.ControlModifier)
    gone = wait(lambda: w3 not in browser.windows and not shiboken6.isValid(w3), 3)
    check("Ctrl+W on the last tab closes the window", gone)

    # ── the end ─────────────────────────────────────────────────────────
    shell.close()
    mark = len(logs.records)
    browser.shutdown()
    shiboken6.delete(browser)
    shiboken6.delete(profile)
    QTest.qWait(50)
    released = [m for m in logs.messages(0, mark) if "Release of profile" in m]
    check("shutdown: pages deleted before the profile, no Qt warning about it", not released,
          f"{released}")
    errors = logs.messages(logging.ERROR)
    check("no errors logged by the browser during the run", not errors, f"{errors[:3]}")
    os.close(lock)
    del app


# ── 3. the real entry point, as processes ───────────────────────────────

def browser_log(state: Path) -> Path:
    """Where a browser run with XDG_STATE_HOME=`state` keeps its log."""
    return state / APP / "browser.log"


def test_processes() -> None:
    section("3. python -m indigoshell.browser.qt, as processes")
    shell = FakeShell(SOCK["apps2"])
    state1 = SCRATCH / "state-1"
    envs = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_STATE_HOME=str(state1), **{
        env("BROWSER_SOCKET"): SOCK["browser2"], env("APPS_SOCKET"): SOCK["apps2"]})
    log_path = SCRATCH / "browser-process-1.log"
    with open(log_path, "w") as err:
        first = subprocess.Popen([PY, "-m", f"{APP}.browser.qt", "--log-level", "debug",
                                  url("/page/One")], cwd=REPO, env=envs,
                                 stdout=err, stderr=err)

    def states(conn_index=0):
        return shell.conns[conn_index].states() if len(shell.conns) > conn_index else []

    def until(pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    up = until(lambda: any(t["title"] == "One" and not t["loading"]
                           for s in states() for t in s["tabs"]), 30)
    check("the first invocation starts and reports its tab", up)
    t0 = time.monotonic()
    second = subprocess.run([PY, "-m", f"{APP}.browser.qt", url("/page/Two")], cwd=REPO,
                            env=envs, capture_output=True, text=True, timeout=30)
    took = time.monotonic() - t0
    got = until(lambda: any([t["title"] for t in s["tabs"]] == ["One", "Two"] and s["current"] == 1
                            for s in states()), 10)
    check("a second invocation hands its URL over and exits 0; a tab in the first",
          second.returncode == 0 and got, f"exit {second.returncode} after {took:.2f} s "
          f"{second.stderr.strip()[-200:]!r}")
    check("... without loading Qt (fast)", took < 3.0, f"{took:.2f} s")
    third = subprocess.run([PY, "-m", f"{APP}.browser.qt"], cwd=REPO, env=envs,
                           capture_output=True, text=True, timeout=30)
    windows = until(lambda: len(shell.conns) >= 2, 10)
    check("with no URL: exits 0, and the running browser opens a new window",
          third.returncode == 0 and windows, f"exit {third.returncode}, {len(shell.conns)} windows")
    first.send_signal(signal.SIGINT)
    try:
        code = first.wait(15)
    except subprocess.TimeoutExpired:
        first.kill()
        code = "hung"
    stderr_text = log_path.read_text(errors="replace")
    logged = (browser_log(state1).read_text(errors="replace")
              if browser_log(state1).exists() else "")
    text = stderr_text + logged
    ours = [line for line in text.splitlines()
            if re.match(r"\d\d:\d\d:\d\d (ERROR|CRITICAL)", line)]
    check("Ctrl+C (SIGINT): exits 0, cleanly",
          code == 0 and "Release of profile" not in text and "Traceback" not in text
          and not ours and not os.path.exists(SOCK["browser2"]),
          f"exit {code}; profile warning {'Release of profile' in text}; errors logged "
          f"{ours[:2]}; socket left {os.path.exists(SOCK['browser2'])}")
    check("the log is <state_dir>/browser.log: the start with its pid, the hand-offs, the "
          "signal, the clean end",
          f"starting: pid {first.pid}" in logged and "handed 1 URL(s)" in logged
          and "SIGINT: quitting" in logged and "ending: SIGINT received" in logged
          and "ended cleanly (exit code 0)" in logged,
          f"{len(logged.splitlines())} lines; last: {logged.strip().splitlines()[-1:]}")
    check("stderr not a terminal: no log lines there, only the file has them",
          "starting: pid" not in stderr_text and "profile in" not in stderr_text)
    shell.close()

    def run_until_shown(state, title, *args):
        """The browser on a page, in a state dir of its own, until its
        shell (a stand-in) says the page has loaded."""
        shell_ = FakeShell(SOCK["apps3"])
        envs_ = dict(envs, XDG_STATE_HOME=str(state), **{
            env("BROWSER_SOCKET"): SOCK["browser3"], env("APPS_SOCKET"): SOCK["apps3"]})
        out = SCRATCH / f"browser-process-{state.name}.log"
        with open(out, "w") as err:
            proc_ = subprocess.Popen([PY, "-m", f"{APP}.browser.qt", *args, url(f"/{title}")],
                                     cwd=REPO, env=envs_, stdout=err, stderr=err)
        up_ = until(lambda: shell_.conns and any(t["title"] == title and not t["loading"]
                                                 for s in shell_.conns[0].states()
                                                 for t in s["tabs"]), 30)
        return proc_, shell_, up_, out

    def ended(proc_) -> int | str:
        try:
            return proc_.wait(15)
        except subprocess.TimeoutExpired:
            proc_.kill()
            return "hung"

    def read_log(state) -> str:
        path = browser_log(state)
        return path.read_text(errors="replace") if path.exists() else ""

    state2 = SCRATCH / "state-2"
    proc, shell, up, out = run_until_shown(state2, "Console")
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    code = ended(proc)
    logged = read_log(state2)
    text = out.read_text(errors="replace") + logged
    check("SIGTERM: exits 0, cleanly, and the log says so", up and code == 0
          and "Release of profile" not in text and "Traceback" not in text
          and "ending: SIGTERM received" in logged and "ended cleanly (exit code 0)" in logged,
          f"exit {code}")
    check("at info by default: no debug lines, and the page's console not there",
          up and "profile in" in logged and CONSOLE not in logged
          and not re.search(r"^\d\d:\d\d:\d\d DEBUG", logged, re.M))
    shell.close()

    # The last window closing ends it too, and the log tells that from a signal.
    state3 = SCRATCH / "state-3"
    proc, shell, up, out = run_until_shown(state3, "Console", "--log-level", "debug")
    if up:
        shell.conns[0].send({"type": "close"})
    code = ended(proc)
    logged = read_log(state3)
    check("its last tab closed (the shell's close): exits 0, and the log says the window went "
          "and why the browser ended", up and code == 0
          and "a window closed: its last tab closed" in logged
          and "ending: its last window closed" in logged and "ended cleanly (exit code 0)" in logged,
          f"exit {code}; {[ln for ln in logged.splitlines() if 'clos' in ln or 'end' in ln]}")
    check("--log-level debug: the page's console is there, at debug",
          re.search(rf"^\d\d:\d\d:\d\d DEBUG .*js: {CONSOLE}", logged, re.M) is not None)
    shell.close()

    # A crash: faulthandler's trace of the Python side, with Chromium loaded.
    state4 = SCRATCH / "state-4"
    proc, shell, up, out = run_until_shown(state4, "Console")
    family = descendants(proc.pid)
    proc.send_signal(signal.SIGSEGV)
    code = ended(proc)
    logged = read_log(state4)
    left_over = [p for p in family if until(lambda p=p: not alive(*p), 5) is False]
    for pid, _start in left_over:
        if alive(pid, _start):          # still that process, not a pid reused
            os.kill(pid, signal.SIGKILL)
    check("a crash (SIGSEGV): the Python stack of every thread in the log, no clean end; "
          "Chromium's processes go with it", up and code == -signal.SIGSEGV
          and "Fatal Python error: Segmentation fault" in logged and "ended cleanly" not in logged
          and not left_over, f"exit {code}; {len(family)} child processes, left over {left_over}")
    shell.close()
    test_log_file()


def _stat(pid) -> list[str] | None:
    """/proc/<pid>/stat's fields after the command name: [state, ppid, ...]."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def descendants(pid: int) -> list[tuple[int, str]]:
    """Every process under `pid`, with its start time (so a pid reused
    later is not taken for it), for a test that ends it."""
    children: dict[int, list[tuple[int, str]]] = {}
    for entry in os.listdir("/proc"):
        fields = _stat(entry) if entry.isdigit() else None
        if fields and len(fields) > 19:
            children.setdefault(int(fields[1]), []).append((int(entry), fields[19]))
    out, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child[0])
    return out


def alive(pid: int, start: str) -> bool:
    fields = _stat(pid)
    return bool(fields) and len(fields) > 19 and fields[19] == start and fields[0] != "Z"


# The browser's log set up as __main__ does, in a process of its own:
# an exception escaping a Qt slot, a line after it, and (with "rotate")
# 3.5 MiB of lines.
LOG_CHILD = """
import logging, sys
from PySide6.QtCore import QTimer
from indigoshell.browser.qt import app, logs
logs.setup("info")
qt = app.make_application()
log = logging.getLogger("indigoshell.browser.qt.test")
def boom():
    raise RuntimeError("from a slot")
QTimer.singleShot(0, boom)
QTimer.singleShot(50, lambda: log.info("still going after it"))
QTimer.singleShot(100, qt.quit)
qt.exec()
if "rotate" in sys.argv:
    for i in range(3600):
        log.info("line %d %s", i, "x" * 1000)
"""


def test_log_file() -> None:
    import pty

    from indigoshell.core import log as shell_log

    def child(name, *args, stderr=subprocess.PIPE):
        state = SCRATCH / f"state-{name}"
        envs = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_STATE_HOME=str(state))
        proc = subprocess.run([PY, "-c", LOG_CHILD, *args], cwd=REPO, env=envs,
                              stdout=subprocess.DEVNULL, stderr=stderr, timeout=60)
        path = browser_log(state)
        return proc, path, path.read_text(errors="replace") if path.exists() else ""

    proc, path, logged = child("slot")
    err = proc.stderr.decode(errors="replace")
    check("an exception escaping a Qt slot: in the log with its traceback, at error, and the "
          "browser goes on; nothing on a stderr that is no terminal",
          proc.returncode == 0 and re.search(r"^\d\d:\d\d:\d\d ERROR .*unhandled exception$",
                                             logged, re.M) is not None
          and "Traceback" in logged and "RuntimeError: from a slot" in logged
          and logged.index("from a slot") < logged.index("still going after it")
          and "unhandled exception" not in err,
          f"exit {proc.returncode}; {logged.strip().splitlines()[:1]}")

    # stderr a terminal: read as it is written (a pty's buffer is small).
    state = SCRATCH / "state-tty"
    leader, follower = pty.openpty()
    seen = b""
    try:
        tty_proc = subprocess.Popen([PY, "-c", LOG_CHILD], cwd=REPO, stdout=subprocess.DEVNULL,
                                    stderr=follower, env=dict(os.environ, XDG_STATE_HOME=str(state),
                                                              QT_QPA_PLATFORM="offscreen"))
        os.close(follower)
        follower = -1
        while True:
            try:
                chunk = os.read(leader, 65536)
            except OSError:             # EIO: every end of the terminal closed
                break
            if not chunk:
                break
            seen += chunk
        tty_proc.wait(30)
    finally:
        if follower >= 0:
            os.close(follower)
        os.close(leader)
    shown = seen.decode(errors="replace")
    logged = browser_log(state).read_text(errors="replace") if browser_log(state).exists() else ""
    check("stderr a terminal: the same lines there too", "unhandled exception" in shown
          and "still going after it" in shown and "still going after it" in logged)

    proc, path, logged = child("rotate", "rotate")
    files = sorted(p.name for p in path.parent.iterdir())
    sizes = [p.stat().st_size for p in path.parent.iterdir()]
    check(f"the file rotates: at {shell_log.LOG_FILE_BYTES >> 20} MiB, "
          f"{shell_log.LOG_FILE_BACKUPS} kept",
          proc.returncode == 0 and files == ["browser.log", "browser.log.1", "browser.log.2",
                                             "browser.log.3"]
          and max(sizes) <= shell_log.LOG_FILE_BYTES and "line 3599" in logged,
          f"{files}, largest {max(sizes)} bytes")


# ── --on-screen: the shell's app service and the real window ────────────

def test_on_screen() -> None:
    """The real AppService (indigoshell/applink), in this process on an
    X connection and socket of its own, and the real browser:
    tests/topbar.py's on-screen section, whose ok/FAIL lines count here
    too."""
    import asyncio

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import topbar

    asyncio.run(topbar.on_screen(SCRATCH))
    FAILED.extend(topbar.FAILED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--on-screen", action="store_true",
                        help="only the on-screen check with the shell's app service")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    print(f"scratch {SCRATCH}", flush=True)

    noise = SCRATCH / "chromium-stderr.log"
    saved = os.dup(2)
    stream = open(noise, "w")
    os.dup2(stream.fileno(), 2)         # Chromium and Qt talk on fd 2
    t0 = time.monotonic()
    try:
        if args.on_screen:
            test_on_screen()
        else:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            test_pure()
            test_in_process()
            test_processes()
    except Exception:
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    finally:
        os.dup2(saved, 2)
        HTTP.shutdown()
    keep = bool(FAILED) or args.keep
    print(f"\n{time.monotonic() - t0:.0f} s" + (f"; logs kept in {SCRATCH}" if keep else ""))
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    if not keep:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    for path in SOCK.values():
        for leftover in (path, path + ".lock"):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
