"""The agent alone in Jack: its own window, borrowed tabs, show, downloads,
uploads, the sites kept from it, and the hands for them.

    .venv/bin/python tests/browser_alone.py              off screen, about a minute
    .venv/bin/python tests/browser_alone.py --on-screen  a bare X window of the test's own,
                                                         then one Jack window, each placed on
                                                         an empty workspace; a few seconds

Off screen (QT_QPA_PLATFORM=offscreen), nothing appears:
  1. pure parts: the refused upload places (browser/uploads.py) against a
     scratch home holding fake secrets (.ssh, .gnupg, keyrings, pass, rbw,
     Firefox's profiles, the shell's data and connections, key-like
     names), reached directly and through links from /tmp; what may go;
     sites.json's agent table; the new requests and the announce; the
     hands' schemas (JSON Schema) and what their descriptions tell the
     agent.
  2. one browser in this process, its control socket and a stand-in
     shell on sockets of the test's (the shell answers `place` a moment
     late, with workspace 6), the client on an asyncio loop in a thread,
     the user's attention on another window: `open` in the agent's
     window (made, announced as Lilac's, asked to be placed before it is
     shown and not shown until the answer, its title and role, shown
     without activating and never raised or activated by Jack (the
     offscreen platform focuses whatever it shows: the window manager's
     part is checked on screen); `open` with no window still the
     user's);
     borrow (a tab that is not showing: its typed form, scroll, script
     state and cookie kept, gone from the user's window and the state
     the shell gets, the user's view unchanged; current in the agent's
     window, `borrowed` said; acting on it there: the Watch's overlay in
     that window; a window's last tab leaving a blank one, closed again
     on return; the refusals); return (to its old place in the
     background, with `show`, the agent's session ended, a tab of the
     agent's own to the user's most recent window, the agent's window
     closing with its last tab); the agent's window closed by the user
     while it holds a borrowed tab (the tab back home, its own tabs
     gone); show (a report with its stylesheet and image beside it, by
     path and by file:// URL, the user's window, no window raised; a
     missing path, a folder, a relative path refused); downloads (two,
     listed with host, tab and path, the file on disk in the scratch
     download folder; waited for, a wait timing out; an action's answer
     naming the download it started); upload (a shown field, a hidden
     field through its label, through the button that opens it, through
     neither (the F24 key); the page's change events trusted and its
     input.files the names and sizes; one file for a single field, a
     folder field, a click that opens no chooser, refused places through
     Jack, nothing reaching the page then; Qt's own chooser never asked;
     a chooser opened by a plain click refused); a host sites.json keeps
     from the agent (every request on its tab refused with the exact
     words, `open` and `go` to it too, `tabs` naming it with no title,
     `release` still fine); every new hand run against it; the log
     (requests named, no uploaded file's name).
  3. the shell's side (a child process, so skia and Qt stay apart): the
     workspace rule (applink.service.pick_desktop) case by case; the top
     bar for the agent's own window, its name first in the voice's
     colour; `place` with no X display answered with why.

--on-screen, on the real display; the user's current workspace and
focused window are read before and after each part and traced between:
  A. the shell's side with a real X window of the test's own: the
     app service (a child process, its own X connection, a socket of the
     test's) places the not-yet-mapped window; its _NET_WM_DESKTOP read
     back before the map; mapped, qtile keeps it on that workspace,
     unmapped, never focused; a second window placed `with` the first
     lands beside it. Both destroyed.
  B. one Jack window of the agent's (not the test kind: shown without
     activating, nothing else), placed by that app service before it
     maps: never mapped, never active, its role and title on the X
     window, its page live (timers, animation frames, visibility, a
     screenshot), then closed with its tab. Skipped when A failed.

Never touched: the user's profile, history, bookmarks, Downloads, browser
and shell; no real input is injected into the display, no Qt dialog or
file chooser is ever shown (Qt's are replaced by recorders), no workspace
is switched and no window but the test's own is moved.
"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, env          # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir        # noqa: E402

PY = sys.executable
FAILED: list[str] = []
SCRATCH = Path("/nonexistent")          # made in main (not in the children)
HOME = Path("/nonexistent")             # the scratch home the upload check is pointed at
TAG = f"{APP}-al-{os.getpid()}"
SOCK = {name: os.path.join(runtime_dir(), f"{TAG}-{name}.sock") for name in ("browser", "apps")}
PORT = 0
PLACED_ON = 6                           # what the stand-in shell answers `place` with
PLACE_DELAY_S = 0.4                     # ... this late
REAL_CACHE_ENV = "ALONE_TEST_REAL_CACHE"    # the user's cache dir, for qtile's socket only


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── the pages ───────────────────────────────────────────────────────────
FORM = """<!doctype html><title>Form</title><body style="height:4000px;margin:0">
<input id=name aria-label="Name"><textarea id=note aria-label="Note"></textarea>
<button id=press onclick="this.textContent='pressed'">Press</button>
<script>window.marker = 'set-before-the-move'; document.cookie = 'kept=yes; path=/';</script>"""
FILES = """<!doctype html><title>Files</title><body style="margin:0;font:16px sans-serif">
<input id=vis name=vis type=file multiple aria-label="Many" style="position:absolute;left:10px;top:10px;width:220px;height:30px">
<input id=one name=one type=file aria-label="One" style="position:absolute;left:10px;top:50px;width:220px;height:30px">
<label id=lab for=hid style="position:absolute;left:10px;top:90px;width:120px;height:30px">Choose a file</label>
<input id=hid name=hid type=file style="display:none">
<button id=open style="position:absolute;left:10px;top:130px;width:120px;height:30px" onclick="document.getElementById('hid2').click()">Browse</button>
<input id=hid2 name=hid2 type=file multiple style="display:none">
<input id=hid3 name=hid3 type=file style="display:none">
<input id=dir name=dir type=file webkitdirectory aria-label="Folder" style="position:absolute;left:10px;top:170px;width:220px;height:30px">
<button id=nothing style="position:absolute;left:10px;top:210px;width:120px;height:30px">Nothing</button>
<script>
window.changes = [];
for (const id of ['vis', 'one', 'hid', 'hid2', 'hid3', 'dir'])
  document.getElementById(id).addEventListener('change', e => changes.push(
    {id, trusted: e.isTrusted, files: Array.from(e.target.files).map(f => [f.name, f.size])}));
</script>"""
DL = """<!doctype html><title>Downloads</title><body style="margin:0">
<a id=get href="/download/data.bin" style="position:absolute;left:10px;top:10px">Get the data</a>
<a id=slow href="/slow" style="position:absolute;left:10px;top:50px">Get it slowly</a>"""
LIVE = """<!doctype html><title>Live</title><body style="background:#fff"><p>live</p>
<script>window.ticks = {timer: 0, raf: 0};
setInterval(() => ticks.timer++, 100); (function f() { ticks.raf++; requestAnimationFrame(f); })();
</script>"""
PLAIN = "<!doctype html><title>{name}</title><body style='margin:0;background:#fff'><p>{name}</p>"
DATA = bytes(range(256)) * 200           # 51200 bytes
SLOW_SIZE, SLOW_CHUNKS = 300_000, 15


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/download/data.bin":
            self._send(DATA, "application/octet-stream",
                       {"Content-Disposition": 'attachment; filename="data.bin"'})
            return
        if route == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="slow.bin"')
            self.send_header("Content-Length", str(SLOW_SIZE))
            self.end_headers()
            chunk = SLOW_SIZE // SLOW_CHUNKS
            try:
                for _ in range(SLOW_CHUNKS):
                    self.wfile.write(b"s" * chunk)
                    self.wfile.flush()
                    time.sleep(0.12)
            except OSError:
                pass
            return
        body = {"/form": FORM, "/files": FILES, "/dl": DL, "/live": LIVE}.get(route)
        if body is None and route.startswith("/page/"):
            body = PLAIN.format(name=route[len("/page/"):])
        if body is None:
            self.send_error(404)
            return
        self._send(body.encode(), "text/html; charset=utf-8", {})

    def _send(self, data: bytes, kind: str, headers: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def url(path: str, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{PORT}{path}"


# ── the scratch home: fake secrets and files that may go ────────────────
def make_home() -> dict:
    """Files under the scratch home and the scratch XDG directories:
    {"refused": {label: path}, "allowed": {label: path}}."""
    from indigoshell.core.paths import cache_dir, config_dir, data_dir
    h = HOME
    refused = {
        "ssh config": h / ".ssh/config", "ssh key": h / ".ssh/id_ed25519",
        "gnupg": h / ".gnupg/pubring.kbx", "keyring": h / ".local/share/keyrings/login.keyring",
        "pass": h / ".password-store/bank.gpg", "rbw data": h / ".local/share/rbw/db.json",
        "rbw cache": h / ".cache/rbw/x", "rbw config": h / ".config/rbw/config.json",
        "firefox old": h / ".mozilla/firefox/p/cookies.sqlite",
        "firefox new": h / ".config/mozilla/firefox/p/logins.json",
        "jack profile": Path(data_dir()) / "browser/Cookies",
        "jack history": Path(data_dir()) / "browser-history.sqlite",
        "jack cache": Path(cache_dir()) / "browser/x",
        "connections": Path(config_dir()) / "connections/phone/key.json",
        "xdg keyrings": Path(os.environ["XDG_DATA_HOME"]) / "keyrings/x",
        "named id_rsa": h / "docs/id_rsa", "named .pem": h / "docs/server.PEM",
        "named .key": h / "docs/tls.key", "named credentials": h / "docs/.credentials.json",
    }
    allowed = {"text": h / "docs/upload-a.txt", "binary": h / "docs/upload-b.bin",
               "public key": h / "docs/id_rsa.pub", "in /tmp": SCRATCH / "tmp-note.txt"}
    for p in [*refused.values(), *allowed.values()]:
        p.parent.mkdir(parents=True, exist_ok=True)
    for p in refused.values():
        p.write_text("not a real secret\n")
    allowed["text"].write_text("hello")
    allowed["binary"].write_bytes(b"\0" * 1234)
    allowed["public key"].write_text("ssh-ed25519 AAAA fake\n")
    allowed["in /tmp"].write_text("from /tmp\n")
    links = SCRATCH / "links"
    links.mkdir()
    (links / "to-ssh").symlink_to(refused["ssh config"])
    (links / "innocent.txt").symlink_to(refused["keyring"])
    (links / "to-etc").symlink_to("/etc/hostname")
    (links / "to-docs").symlink_to(allowed["text"])
    (h / ".local/share/innocent-dir").symlink_to(h / ".ssh")
    refused.update({"link to .ssh": links / "to-ssh", "link named innocently": links / "innocent.txt",
                    "link out of home": links / "to-etc",
                    "through a linked folder": h / ".local/share/innocent-dir/config",
                    "a folder": h / "docs", "missing": h / "docs/nothing.txt",
                    "relative": Path("docs/upload-a.txt")})
    allowed["link to an allowed file"] = links / "to-docs"
    return {"refused": {k: str(v) for k, v in refused.items()},
            "allowed": {k: str(v) for k, v in allowed.items()}}


# ── 1. pure parts ───────────────────────────────────────────────────────
def test_pure(files: dict) -> None:
    from indigoshell.agent.hands import Hand
    from indigoshell.applink import protocol as link
    from indigoshell.browser import protocol, uploads
    from indigoshell.browser.hands import browser_hands
    from indigoshell.browser.qt.identity import AGENT_TABLE, Rules

    section("1. pure parts")
    home = str(HOME)
    wrong = {k: uploads.refusal(p, home) for k, p in files["refused"].items()
             if uploads.refusal(p, home) is None}
    check("upload: every secret place, key-like name, link into one, folder, missing and "
          "relative path refused", not wrong, f"{wrong}")
    said = {k: uploads.refusal(p, home) for k, p in files["refused"].items()}
    check("... saying why: the place and what it holds, the name, outside the home",
          ".ssh (SSH keys)" in said["ssh config"] and "keyrings" in said["keyring"]
          and "the shell's data" in said["jack profile"] and "connections" in said["connections"]
          and "private key" in said["named id_rsa"] and "outside the home" in said["link out of home"]
          and "keyrings" in said["link named innocently"] and ".ssh" in said["through a linked folder"],
          f"{said['ssh config']} | {said['link out of home']} | {said['jack profile']}")
    bad = {k: uploads.refusal(p, home) for k, p in files["allowed"].items()
           if uploads.refusal(p, home) is not None}
    check("upload: files under the home and /tmp may go (an id_*.pub too, a link to one)",
          not bad, f"{bad}")
    try:
        uploads.check([files["allowed"]["text"], files["refused"]["ssh key"]])
        whole = None
    except ValueError as e:
        whole = str(e)
    check("upload: one refused path refuses the whole list", whole and "id_ed25519" in whole,
          f"{whole}")
    places = [p for p, _ in uploads.places(home)]
    check("upload: the places are one list (uploads.places), the home's and the shell's",
          any(p.endswith("/.ssh") for p in places) and any(p.endswith("/connections")
                                                           for p in places), f"{len(places)}")

    rules, warnings = Rules.parse({"identity": {}, "agent": {
        "bank.example": "never", "*.health.example": "never", "x.org": "sometimes"}}, AGENT_TABLE)
    cases = {"bank.example": "never", "BANK.example.": "never", "my.health.example": "never",
             "health.example": "never", "other.example": None, "x.org": None}
    wrong = {h: rules.value_for(h) for h, want in cases.items() if rules.value_for(h) != want}
    plain, w2 = Rules.parse({"identity": {}, "agent": {}}, AGENT_TABLE)
    none_, w3 = Rules.parse({"identity": {}}, AGENT_TABLE)
    _, w4 = Rules.parse({"identity": {}, "agent": {}})
    check("sites.json agent table: exact and *.domain, \"never\" the one policy (a bad one "
          "skipped, said); none by default; the identity table no longer calls it unknown",
          not wrong and len(warnings) == 1 and "sometimes" in warnings[0]
          and plain.value_for("bank.example") is None and none_.value_for("a.org") is None
          and not w2 and not w3 and not w4, f"{wrong} {warnings} {w4}")

    ann = protocol.announce(0x400001, False, "Lilac")
    check("the announce names the agent's voice for its own window, nothing for the user's",
          ann.get("agent") == "lilac" and "agent" not in protocol.announce(0x400001, False),
          f"{ann}")
    check("place / placed on the apps' line", link.place() == {"type": "place"}
          and link.place(5) == {"type": "place", "with": 5}
          and link.placed(6) == {"type": "placed", "desktop": 6}
          and link.placed(None, "why") == {"type": "placed", "desktop": None, "why": "why"})
    check("the requests: borrow, return, show, downloads, upload; the words",
          {"borrow", "return", "show", "downloads", "upload"} <= set(protocol.REQUESTS)
          and protocol.kept("bank.example") == "the user keeps bank.example to themselves"
          and protocol.agent_title("indigo") == "Indigo · Jack")

    hands = {h.name: h for h in browser_hands()}
    new = ["browser_borrow", "browser_return", "browser_show", "browser_downloads",
           "browser_upload"]
    problems = []
    try:
        import jsonschema
        meta = jsonschema.Draft202012Validator
    except ImportError:
        meta = None
    samples = {"browser_borrow": {"tab": 1}, "browser_return": {"tab": 1, "show": True},
               "browser_show": {"path": "/tmp/r.html"}, "browser_downloads": {"wait": 3},
               "browser_upload": {"tab": 1, "ref": 2, "paths": ["/tmp/x"]},
               "browser_open": {"url": "x", "window": "agent"}}
    for name in [*new, "browser_open"]:
        h = hands.get(name)
        if not isinstance(h, Hand):
            problems.append(f"{name} missing")
            continue
        if meta is not None:
            try:
                meta.check_schema(h.input)
                meta(h.input).validate(samples[name])
            except Exception as e:      # noqa: BLE001
                problems.append(f"{name}: {e}")
        if h.input.get("additionalProperties") is not False:
            problems.append(f"{name}: extra keys allowed")
        json.dumps(h.lent())
    check("hands: the five new ones and browser_open (window \"agent\" or an id): valid JSON "
          "Schema" + (" (jsonschema)" if meta else " (shape only)") + ", no extra keys",
          not problems, f"{problems}")
    d = {n: hands[n].description for n in hands}
    told = {
        "alone vs watch": "work there on what is yours to do alone" in d["browser_open"]
                          and "act in the user's own tab" in d["browser_borrow"],
        "borrow then return": "browser_return it" in d["browser_borrow"]
                              and "Return every tab you borrowed" in d["browser_return"],
        "never switch": "Never switch the user's workspace" in d["browser_borrow"],
        "not showing may be borrowed": "also one that is not showing" in d["browser_borrow"],
        "showing rule": "browser_switch first" in d["browser_upload"],
        "refused places": all(s in d["browser_upload"] for s in ("~/.ssh", "keyrings",
                                                                 "connections", "*.pem")),
        "show": "HTML report" in d["browser_show"] and "not raised or focused" in d["browser_show"],
        "kept sites": "keeps <site> to themselves" in d["browser_open"],
    }
    check("hand descriptions: when to work alone and when where the user looks, borrow then "
          "return, never the user's workspace, the showing rule, the refused upload places, "
          "show, the kept sites", all(told.values()), f"{[k for k, v in told.items() if not v]}")


# ── 2. one browser, in this process ─────────────────────────────────────
class FakeShell:
    """The shell's app service, standing in: takes each window's line,
    says `attached`, answers `place` with PLACED_ON after PLACE_DELAY_S,
    keeps every message with its connection and time."""

    def __init__(self, path: str) -> None:
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        self.conns: list[socket.socket] = []
        self.msgs: list[tuple[int, float, dict]] = []      # (connection, time, message)
        self.answered: list[tuple[int, float]] = []        # `placed` sent: (connection, time)
        self.answer_place = True        # False: an older shell, which never answers `place`
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            self.conns.append(sock)
            n = len(self.conns) - 1
            threading.Thread(target=self._serve, args=(sock, n), daemon=True).start()

    def _serve(self, sock, n):
        try:
            for line in sock.makefile("rb"):
                msg = json.loads(line)
                self.msgs.append((n, time.monotonic(), msg))
                if msg.get("type") == "announce":
                    sock.sendall(b'{"type": "attached"}\n')
                elif msg.get("type") == "place" and self.answer_place:
                    time.sleep(PLACE_DELAY_S)
                    self.answered.append((n, time.monotonic()))
                    sock.sendall(json.dumps({"type": "placed", "desktop": PLACED_ON}).encode()
                                 + b"\n")
        except (OSError, ValueError):
            pass

    def of(self, wid: int) -> list[dict]:
        """What the connection that announced `wid` sent, in order."""
        conns = {n for n, _, m in self.msgs if m.get("type") == "announce" and m.get("wid") == wid}
        return [m for n, _, m in self.msgs if n in conns]

    def close(self):
        self.server.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        if os.path.exists(self.path):
            os.unlink(self.path)


class Client:
    """An asyncio loop in a thread for the client; `run` pumps Qt while
    it waits."""

    def __init__(self, qwait) -> None:
        self.qwait = qwait
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def start(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, timeout: float = 30.0):
        future = self.start(coro)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            self.qwait(10)
        if not future.done():
            future.cancel()
            raise TimeoutError(f"no answer in {timeout} s")
        return future.result()

    def attempt(self, coro, timeout: float = 30.0):
        """(result, None) or (None, the error's text)."""
        try:
            return self.run(coro, timeout), None
        except Exception as e:      # noqa: BLE001
            return None, str(e)


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

    def lines(self, level=20) -> list[str]:
        return [r.getMessage() for r in self.records
                if r.name.startswith(f"{APP}.browser") and r.levelno >= level]


def make_report() -> Path:
    from PySide6.QtGui import QColor, QImage
    report = HOME / "reports/today"
    report.mkdir(parents=True)
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff0000"))
    image.save(str(report / "dot.png"))
    (report / "style.css").write_text("body { background: rgb(0, 128, 0); } "
                                      "h1 { color: rgb(1, 2, 3); }")
    (report / "index.html").write_text('<!doctype html><title>Report</title>'
                                       '<link rel=stylesheet href="style.css">'
                                       '<h1>Findings</h1><img id=dot src="dot.png">')
    return report / "index.html"


def test_in_process(files: dict) -> None:
    import shiboken6
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication, QWidget

    from indigoshell.agent.hands import Call
    from indigoshell.browser import control, protocol, uploads
    from indigoshell.browser.hands import browser_hands
    from indigoshell.browser.qt import app as browser_app
    from indigoshell.browser.qt import instance, startup
    from indigoshell.browser.qt.profile import make_profile
    from indigoshell.browser.qt.window import Window
    from indigoshell.core.paths import config_dir

    logs = Log()
    users: list[tuple] = []             # what reached Qt's own dialogs and chooser
    QWebEnginePage.chooseFiles = lambda self, *a: users.append(("files", a[0].name)) or []
    QWebEnginePage.javaScriptAlert = lambda self, o, m: users.append(("alert", m))
    calls: list[tuple] = []             # raise_ and activateWindow, on any Jack window
    for name in ("raise_", "activateWindow"):
        original = getattr(Window, name)

        def record(self, *a, _name=name, _original=original):
            calls.append((_name, self.id, self.agent))
            return _original(self, *a)
        setattr(Window, name, record)
    uploads.home = lambda: str(HOME)    # the check's home: the scratch one

    def wait(pred, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            QTest.qWait(20)
        return bool(pred())

    section("2. one browser in this process (off screen)")
    sites = Path(config_dir()) / "browser" / "sites.json"
    sites.parent.mkdir(parents=True, exist_ok=True)
    sites.write_text(json.dumps({"identity": {"accounts.google.com": "original"},
                                 "agent": {"localhost": "never"}}))
    shell = FakeShell(SOCK["apps"])
    startup.apply_flags()
    app = browser_app.make_application()
    profile = make_profile()
    downloads_dir = SCRATCH / "downloads"
    downloads_dir.mkdir()
    profile.setDownloadPath(str(downloads_dir))
    lock = instance.try_lock()
    browser = browser_app.Browser(profile, unattended=False)
    browser.listen(SOCK["browser"])
    client = Client(QTest.qWait)
    jack = client.run(control.connect())
    J = lambda coro, t=30.0: client.attempt(coro, t)      # noqa: E731

    def js(tab_id, code, timeout=5.0):
        window, tab = next((w, t) for w in browser.windows for t in w.tabs if t.id == tab_id)
        box = []
        tab.page.runJavaScript(code, 0, box.append)
        wait(lambda: box, timeout)
        return box[0] if box else None

    def refs_of(tab_id):
        page_, _ = J(jack.read(tab_id, "outline", hidden=True))
        out = {}
        for line in (page_ or {}).get("outline", []):
            line = line.strip()
            if line.startswith("["):
                n, rest = line[1:].split("] ", 1)
                out[rest.split(" (")[0].split(" =")[0].split(" ->")[0]] = int(n)
        return out

    def window_of(tab_id):
        return next((w for w in browser.windows for t in w.tabs if t.id == tab_id), None)

    def titles(w):
        return [t.page.title() for t in w.tabs]

    # The user's window: the form, and another tab in front of it.
    form, _ = J(jack.open(url("/form")))
    J(jack.wait(form.tab, timeout=15), 20)
    user = window_of(form.tab)
    other, _ = J(jack.open(url("/page/Other")))
    J(jack.wait(other.tab, timeout=15), 20)
    elsewhere = QWidget()
    elsewhere.resize(200, 100)
    elsewhere.show()
    elsewhere.activateWindow()
    wait(lambda: QApplication.activeWindow() is elsewhere, 3)
    wait(lambda: user.link.attached, 5)
    calls.clear()

    # ── the agent's own window ──────────────────────────────────────────
    future = client.start(jack.open(url("/page/Mine"), window="agent", voice="lilac"))
    shown_early, her = False, None
    deadline = time.monotonic() + 10
    while not future.done() and time.monotonic() < deadline:
        QTest.qWait(10)
        her = next((w for w in browser.windows if w.agent), her)
        if her is not None and her.isVisible() and not shell.answered:
            shown_early = True
    mine = future.result() if future.done() else None
    her = next((w for w in browser.windows if w.agent), None)
    hid = int(her.winId()) if her is not None else 0
    said = shell.of(hid)
    announce = next((m for m in said if m.get("type") == "announce"), {})
    kinds = [m["type"] for m in said if m.get("type") in ("announce", "place")]
    check("open in the agent's window: a window of its own, made for Lilac, announced as hers, "
          "`place` asked right after the announce",
          her is not None and her.agent == "lilac" and announce.get("agent") == "lilac"
          and announce.get("single") is False and kinds[:2] == ["announce", "place"]
          and mine is not None and mine.window == her.id, f"{kinds} {announce}")
    check("... not shown until the shell answered (a moment late on purpose), then shown, "
          f"placed on workspace {PLACED_ON}", not shown_early and her.isVisible()
          and her.placed_on == PLACED_ON and her.ready, f"early {shown_early}")
    J(jack.wait(mine.tab, timeout=15), 20)
    QTest.qWait(100)
    check("... its title says whose (\"Lilac · Jack · Mine\"), its role is the agent's, its class "
          "Jack's", her.windowTitle() == "Lilac · Jack · Mine"
          and her.windowRole() == protocol.AGENT_ROLE
          and QApplication.applicationName() == f"{APP}-browser", f"{her.windowTitle()!r}")
    # The offscreen platform gives every window it shows the focus, this one
    # too; whether the window manager does is the on-screen part's check.
    check("... never raised or activated by Jack: shown without activating (its QWindow says so, "
          "which X11 hears as user time 0), no raise_ or activateWindow",
          not calls and her.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
          and her.windowHandle().property("_q_showWithoutActivating") is True, f"{calls}")
    plain, _ = J(jack.open(url("/page/Plain")))
    check("open with no window: still the user's window, never the agent's",
          plain is not None and plain.window == user.id, f"{plain}")
    J(jack.close_tab(plain.tab))
    windows, _ = J(jack.tabs())
    hers = [w for w in windows or () if w.agent]
    check("tabs: the agent's window listed as Lilac's", len(hers) == 1 and hers[0].window == her.id
          and hers[0].agent == "lilac")
    again, _ = J(jack.open(url("/page/Second"), window="agent", voice="lilac"))
    check("a second open in the agent's window: the same window, not placed again",
          again is not None and again.window == her.id
          and [m["type"] for m in shell.of(hid)].count("place") == 1)
    J(jack.close_tab(again.tab))

    # ── borrow ──────────────────────────────────────────────────────────
    J(jack.switch(other.tab))
    js(form.tab, "document.getElementById('name').value = 'half typed'; "
                 "document.getElementById('note').value = 'a draft'; scrollTo(0, 1234); "
                 "window.marker = 'changed-before-the-move'; 1")
    STATE = ("JSON.stringify({name: document.getElementById('name').value, note: "
             "document.getElementById('note').value, scroll: scrollY, marker: window.marker, "
             "cookie: document.cookie})")
    before = json.loads(js(form.tab, STATE))
    page_before = next(t.page for t in user.tabs if t.id == form.tab)
    states_before = len([m for m in shell.of(int(user.winId())) if m.get("type") == "state"])
    done, err = J(jack.borrow(form.tab, voice="lilac"))
    after = json.loads(js(form.tab, STATE) or "{}")
    moved = window_of(form.tab)
    check("borrow a tab that is not showing: now the current tab of the agent's window, "
          "`borrowed` and `from` saying where it was",
          done is not None and moved is her and her.current_tab().id == form.tab
          and done["from"] == {"window": user.id, "index": 0}
          and done["borrowed"] == {"window": user.id, "index": 0} and done["current"], f"{err}")
    check("... its state kept: the typed form, the scroll, the page's script state, its cookie "
          "(the same page object, in a new view)", before == after and before["scroll"] == 1234
          and her.current_tab().page is page_before, f"{before} -> {after}")
    QTest.qWait(100)
    last_state = [m for m in shell.of(int(user.winId())) if m.get("type") == "state"][-1]
    check("... gone from the user's window and from the state the shell gets; the user's window "
          "still shows the tab it showed", titles(user) == ["Other"]
          and user.current_tab().id == other.tab
          and [t["title"] for t in last_state["tabs"]] == ["Other"]
          and len([m for m in shell.of(int(user.winId())) if m.get("type") == "state"])
          > states_before, f"{titles(user)} {last_state['tabs']}")
    press = refs_of(form.tab)['button "Press"']
    acted, err = J(jack.click(form.tab, press, voice="lilac"))
    overlay = browser.watch.overlays.get(her.id)
    check("acting on it in the agent's window: the Watch drives it there, its overlay in that "
          "window", acted is not None and acted["agent"] == "driving" and overlay is not None
          and overlay.shown() and js(form.tab, "document.getElementById('press').textContent")
          == "pressed", f"{err}")
    _, e_again = J(jack.borrow(form.tab))
    _, e_mine = J(jack.borrow(mine.tab))
    check("borrow refused: a tab in the agent's window already (borrowed, or its own)",
          e_again and "own window already" in e_again and e_mine and "already" in e_mine,
          f"{e_again} / {e_mine}")
    # The user goes to the agent's window and types: theirs, as anywhere.
    QWidget.activateWindow(her)         # the user's doing (not Jack's: not recorded)
    wait(lambda: QApplication.activeWindow() is her, 3)
    QTest.keyClick(her.windowHandle(), Qt.Key.Key_Q)
    _, e_taken = J(jack.click(form.tab, press, voice="lilac"))
    check("a key the user presses in the agent's window takes the tab over there too: the next "
          "request says so, the overlay goes", e_taken == f"the user took over tab {form.tab}"
          and not overlay.shown() and browser.watch.paused(form.tab), f"{e_taken}")
    QWidget.activateWindow(elsewhere)
    wait(lambda: QApplication.activeWindow() is elsewhere, 3)

    # ── return ──────────────────────────────────────────────────────────
    held = json.loads(js(form.tab, STATE) or "{}")
    done, err = J(jack.return_tab(form.tab))
    check("return: back to its window at its old place, in the background (the user's window "
          "still shows its tab), the agent's session on it ended",
          done is not None and done["to"] == "its window" and window_of(form.tab) is user
          and user.tabs[0].id == form.tab and user.current_tab().id == other.tab
          and form.tab not in browser.watch.sessions and done["borrowed"] is None, f"{err}")
    back = json.loads(js(form.tab, STATE) or "{}")
    check("... still the same page with its state (the scroll where the agent's click left it)",
          back == held and back["name"] == "half typed" and back["marker"] == before["marker"],
          f"{held} -> {back}")
    J(jack.borrow(form.tab, voice="lilac"))
    done, err = J(jack.return_tab(form.tab, show=True))
    check("return with show: the current tab of its window (the window not raised or activated)",
          done is not None and user.current_tab().id == form.tab and done["current"]
          and not calls, f"{err} {calls}")
    # A window's last tab: a blank one keeps its place.
    lone = browser.new_window()
    lone_tab = lone.add_tab(browser_app.to_url(url("/page/Lone")))
    lone.show()
    J(jack.wait(lone_tab.id, timeout=15), 20)
    done, err = J(jack.borrow(lone_tab.id, voice="lilac"))
    blank = [t.url().toString() for t in lone.tabs] if shiboken6.isValid(lone) else None
    check("borrow a window's last tab: the window stays, a blank tab in its place",
          done is not None and shiboken6.isValid(lone) and blank == ["about:blank"], f"{err} {blank}")
    done, err = J(jack.return_tab(lone_tab.id))
    check("... returned: back in that window, the blank tab gone",
          done is not None and shiboken6.isValid(lone) and [t.id for t in lone.tabs] == [lone_tab.id]
          and done["to"] == "its window", f"{err} {[t.url().toString() for t in lone.tabs]}")
    lone.close()
    QTest.qWait(100)
    # A tab of the agent's own goes to the user's most recent window.
    done, err = J(jack.return_tab(mine.tab))
    check("return a tab the agent opened itself: to the end of the user's most recently used "
          "window; the agent's window closes with its last tab",
          done is not None and done["to"] == "another window" and window_of(mine.tab) is user
          and user.tabs[-1].id == mine.tab and not any(w.agent for w in browser.windows), f"{err}")
    _, e_not_hers = J(jack.return_tab(other.tab))
    check("return refused for a tab that is not in the agent's window",
          e_not_hers and "not in your own window" in e_not_hers, f"{e_not_hers}")

    # The agent's window closed by the user while it holds a borrowed tab.
    own, _ = J(jack.open(url("/page/Own"), window="agent", voice="lilac"))
    her2 = window_of(own.tab)
    J(jack.borrow(form.tab, voice="lilac"))
    order = [t.id for t in user.tabs]
    her2.close()
    QTest.qWait(300)
    check("the agent's window closed while holding a borrowed tab: the tab goes back to the "
          "user's window, at its place; the agent's own tab closes with the window",
          window_of(form.tab) is user and user.tabs[0].id == form.tab
          and window_of(own.tab) is None and not any(w.agent for w in browser.windows)
          and json.loads(js(form.tab, STATE) or "{}").get("name") == "half typed",
          f"{order} -> {[t.id for t in user.tabs]}")

    # ── show ────────────────────────────────────────────────────────────
    report = make_report()
    calls.clear()
    shown, err = J(jack.show(str(report)))
    J(jack.wait(shown["tab"], timeout=15), 20) if shown else None
    look = json.loads(js(shown["tab"], "JSON.stringify({img: document.getElementById('dot')"
                         ".naturalWidth, bg: getComputedStyle(document.body).backgroundColor, h1: "
                         "getComputedStyle(document.querySelector('h1')).color})") or "{}") \
        if shown else {}
    check("show a report: a new tab of the user's most recent window, its current tab; its "
          "stylesheet and image beside it loaded; no window raised or activated",
          shown is not None and shown["window"] == user.id and shown["current"]
          and not shown["background"] and look == {"img": 30, "bg": "rgb(0, 128, 0)",
                                                   "h1": "rgb(1, 2, 3)"} and not calls,
          f"{err} {look} {calls}")
    by_url, err2 = J(jack.show(report.as_uri()))
    _, e_missing = J(jack.show(str(HOME / "reports/none.html")))
    _, e_dir = J(jack.show(str(HOME / "reports")))
    _, e_rel = J(jack.show("reports/today/index.html"))
    check("show by file:// URL too; a missing path, a folder, a relative path refused, saying so",
          by_url is not None and e_missing and "does not exist" in e_missing
          and e_dir and "is not a file" in e_dir and e_rel and "absolute" in e_rel,
          f"{err2} / {e_missing} / {e_dir} / {e_rel}")
    for t in (shown, by_url):
        if t:
            J(jack.close_tab(t["tab"]))

    # ── downloads ───────────────────────────────────────────────────────
    dl, _ = J(jack.open(url("/dl")))
    J(jack.wait(dl.tab, timeout=15), 20)
    R = refs_of(dl.tab)
    clicked, err = J(jack.click(dl.tab, R['link "Get the data"']))
    wait(lambda: any(d["name"] == "data.bin" for d in browser.download_list), 10)
    listed, _ = J(jack.downloads())
    first = next((d for d in listed or () if d["name"] == "data.bin"), None)
    waited, err_w = J(jack.wait_download(first["download"], timeout=20), 30) if first else (None, "")
    on_disk = downloads_dir / "data.bin"
    check("a download: listed with its id, name, path in the download folder, the site and the "
          "tab that started it; waited for: done, every byte on disk",
          first is not None and first["host"] == "127.0.0.1" and first["tab"] == dl.tab
          and first["path"] == str(on_disk) and waited is not None and waited["state"] == "done"
          and waited["percent"] == 100 and on_disk.exists() and on_disk.read_bytes() == DATA,
          f"{err} {err_w} {first} {waited}")
    check("... the click's answer named it when it had begun by then (else downloads lists it)",
          clicked is not None and (not clicked.get("downloads")
                                   or clicked["downloads"][0]["name"] == "data.bin"),
          f"{clicked and clicked.get('downloads')}")
    J(jack.switch(dl.tab))
    J(jack.click(dl.tab, R['link "Get it slowly"']))
    wait(lambda: any(d["name"] == "slow.bin" for d in browser.download_list), 10)
    slow = next((d for d in browser.download_list if d["name"] == "slow.bin"), None)
    _, e_short = J(jack.wait_download(slow["download"], timeout=0.2), 10) if slow else (None, "")
    done_slow, err_s = J(jack.wait_download(slow["download"], timeout=30), 40) if slow else (None, "")
    listed, _ = J(jack.downloads())
    check("a slow download: a short wait times out saying how far; a long one sees it done; the "
          "list newest first", e_short and e_short.startswith("timed out after 0.2 s")
          and done_slow is not None and done_slow["state"] == "done"
          and done_slow["received"] == SLOW_SIZE and listed[0]["name"] == "slow.bin"
          and (downloads_dir / "slow.bin").stat().st_size == SLOW_SIZE,
          f"{e_short} {err_s} {done_slow}")
    _, e_none = J(jack.wait_download(99999, timeout=1))
    check("waiting for a download that is not there: refused", e_none and "no download 99999"
          in e_none, f"{e_none}")
    J(jack.close_tab(dl.tab))

    # ── upload ──────────────────────────────────────────────────────────
    up, _ = J(jack.open(url("/files"), window="agent", voice="lilac"))
    J(jack.wait(up.tab, timeout=15), 20)
    R = refs_of(up.tab)
    a, b = files["allowed"]["text"], files["allowed"]["binary"]

    def changes():
        return json.loads(js(up.tab, "JSON.stringify(changes.splice(0))") or "[]")

    def holds(ident):
        return json.loads(js(up.tab, f"JSON.stringify(Array.from(document.getElementById("
                                     f"'{ident}').files).map(f => [f.name, f.size]))") or "[]")

    users.clear()
    done, err = J(jack.upload(up.tab, R['button "Many"'], [a, b], voice="lilac"))
    got = changes()
    check("upload into a shown field: the page's input.files are the names and sizes, its "
          "change event trusted, as from its own chooser; the answer says them",
          done is not None and holds("vis") == [["upload-a.txt", 5], ["upload-b.bin", 1234]]
          and got == [{"id": "vis", "trusted": True, "files": [["upload-a.txt", 5],
                                                               ["upload-b.bin", 1234]]}]
          and done["files"] == [{"name": "upload-a.txt", "size": 5},
                                {"name": "upload-b.bin", "size": 1234}]
          and done["agent"] == "driving", f"{err} {got}")
    by_name = {f.get("name"): f["ref"] for form_ in J(jack.read(up.tab, "forms", hidden=True))[0]
               ["forms"] for f in form_["fields"]}
    hidden = {}
    for label, ref, ident in (("by its own ref, through its label", by_name.get("hid"), "hid"),
                              ("through the button that opens it", R.get('button "Browse"'),
                               "hid2")):
        done, err = J(jack.upload(up.tab, ref, [a], voice="lilac"))
        hidden[label] = (done is not None and holds(ident) == [["upload-a.txt", 5]], err,
                         done and done.get("field"))
    check("a hidden field by its own ref (its shown label clicked), and through the button that "
          "opens it: the files arrive; `field` names the field, or none when the page's own "
          "button opened it", all(v[0] for v in hidden.values())
          and hidden["through the button that opens it"][2] is None, f"{hidden}")
    hid3 = by_name.get("hid3")
    done, err = J(jack.upload(up.tab, hid3, [b], voice="lilac"))
    check("a hidden field with no label shown, by its own ref: a key lets the page open its "
          "chooser, a click in the agent's world opens it, the file arrives",
          done is not None and holds("hid3") == [["upload-b.bin", 1234]], f"{err} ref {hid3}")
    changes()
    _, e_one = J(jack.upload(up.tab, R['button "One"'], [a, b], voice="lilac"))
    _, e_dir = J(jack.upload(up.tab, R['button "Folder"'], [a], voice="lilac"))
    _, e_nothing = J(jack.upload(up.tab, R['button "Nothing"'], [a], voice="lilac"))
    refused_through = {}
    for k in ("ssh config", "link named innocently", "jack profile", "named .pem", "connections"):
        _, refused_through[k] = J(jack.upload(up.tab, R['button "Many"'],
                                              [a, files["refused"][k]], voice="lilac"))
    check("refused: two files for a field that takes one, a folder field, a click that opens no "
          "chooser (saying what to give instead)", e_one and "takes one file" in e_one
          and e_dir and "takes a folder" in e_dir and e_nothing
          and "opened no file chooser" in e_nothing, f"{e_one} / {e_dir} / {e_nothing}")
    check("refused through Jack: a secret place, a link into one, Jack's profile, a key-like "
          "name, the shell's connections; nothing reached the page; Qt's own chooser never asked",
          all(v and ("is in" in v or "named like" in v) for v in refused_through.values())
          and changes() == [] and holds("vis") == [["upload-a.txt", 5], ["upload-b.bin", 1234]]
          and not users, f"{refused_through} {users}")
    held = holds("hid2")
    done, err = J(jack.click(up.tab, R['button "Browse"'], voice="lilac"))
    got = changes()
    check("a chooser a plain click opens while the agent drives: refused, said in the answer; "
          "no file added (Chromium empties the field, as for the user's Cancel: Qt gives no "
          "files back to keep)", done is not None
          and any("only upload gives a page files" in n for n in done.get("notes", []))
          and held == [["upload-a.txt", 5]] and holds("hid2") == []
          and all(c["files"] == [] for c in got) and not users,
          f"{err} {done and done.get('notes')} {held} -> {holds('hid2')} {got} {users}")
    _, e_click = J(jack.click(up.tab, R['button "One"']))
    check("click on a file field: refused, pointing to upload",
          e_click and e_click.endswith("is a file field: give it files with upload"), f"{e_click}")
    J(jack.release())

    # ── a site kept from the agent ──────────────────────────────────────
    kept_tab = user.add_tab(browser_app.to_url(url("/page/Kept", host="localhost")))
    wait(lambda: kept_tab.page.title() == "Kept", 15)
    k = kept_tab.id
    words = "the user keeps localhost to themselves"
    refusals = {"look": J(jack.look(window=user.id))[1], "read": J(jack.read(k, "text"))[1], "screenshot": J(jack.screenshot(k))[1],
                "click": J(jack.click(k, x=5, y=5))[1], "borrow": J(jack.borrow(k))[1],
                "wait": J(jack.wait(k, timeout=1))[1], "close": J(jack.close_tab(k))[1],
                "open": J(jack.open(url("/page/K2", host="localhost")))[1]}
    J(jack.switch(form.tab))
    refusals["switch"] = J(jack.switch(k))[1]
    refusals["go"] = J(jack.go(form.tab, url("/page/K3", host="localhost")))[1]
    windows, _ = J(jack.tabs())
    listed = next((t for w in windows for t in w.tabs if t.tab == k), None)
    released, err_r = J(jack.release(k))
    check("a host sites.json keeps from the agent: every request on its tab refused with the "
          "exact words (look at it too), open and go to it as well",
          all(v == words for v in refusals.values()), f"{refusals}")
    check("... tabs lists it with no title and its address cut to the site; release still works",
          listed is not None and listed.title == "" and listed.kept
          and listed.url == f"http://localhost/" and released == [], f"{listed} {err_r}")
    user.close_tab(kept_tab)

    # ── an older shell, which never answers `place` ─────────────────────
    shell.answer_place = False
    began = time.monotonic()
    other_voice, err = J(jack.open(url("/page/Indigo"), window="agent", voice="indigo"))
    took = time.monotonic() - began
    hers2 = window_of(other_voice.tab) if other_voice else None
    check("a shell that never answers `place`: the agent's window shows anyway after "
          "PLACE_WAIT_S (placed nowhere), one for each voice", hers2 is not None
          and hers2.agent == "indigo" and hers2.isVisible() and hers2.placed_on is None
          and 1.3 < took < 4.0 and hers2.windowTitle().startswith("Indigo · Jack"),
          f"{err} {took:.2f} s")
    J(jack.close_tab(other_voice.tab))
    shell.answer_place = True

    # ── the hands ───────────────────────────────────────────────────────
    hands = {h.name: h for h in browser_hands()}

    def call(name, args, voice="lilac", timeout=120.0):
        blocks, err = J(hands[name].run(Call(f"c-{name}", name, args, timeout, voice=voice)),
                        timeout)
        return "\n".join(b["text"] for b in blocks or ()), err

    out = {}
    out["open"] = call("browser_open", {"url": url("/page/Handy"), "window": "agent"})
    handy = next((t for w in browser.windows if w.agent for t in w.tabs), None)
    out["borrow"] = call("browser_borrow", {"tab": form.tab})
    out["tabs"] = call("browser_tabs", {})
    up2 = next(t.id for w in browser.windows if w.agent for t in w.tabs if t.id == form.tab)
    J(jack.go(up2, url("/files")))
    J(jack.wait(up2, timeout=15), 20)
    R = refs_of(up2)
    out["upload"] = call("browser_upload", {"tab": up2, "ref": R['button "Many"'], "paths": [a]})
    out["upload_refused"] = call("browser_upload", {"tab": up2, "ref": R['button "Many"'],
                                                    "paths": [files["refused"]["ssh key"]]})
    out["return"] = call("browser_return", {"tab": form.tab, "show": True})
    out["show"] = call("browser_show", {"path": str(report)})
    out["downloads"] = call("browser_downloads", {})
    out["wait"] = call("browser_downloads", {"wait": first["download"], "timeout": 5})
    errors = {k: v[1] for k, v in out.items() if v[1] and k != "upload_refused"}
    check("every new hand runs against Jack (open in the agent's window, borrow, tabs, upload, "
          "return, show, downloads and a wait)", not errors and handy is not None, f"{errors}")
    texts = {k: v[0] for k, v in out.items()}
    check("... and says what happened: whose window, where a tab came from and went, the files, "
          "the download's path",
          "in your own window" in texts["open"] and "Borrowed into your own window" in texts["borrow"]
          and "Lilac's own window" in texts["tabs"] and "borrowed from window" in texts["tabs"]
          and "upload-a.txt (5 bytes)" in texts["upload"]
          and "back to its window (as its current tab)" in texts["return"]
          and "Shown in the user's window" in texts["show"]
          and str(on_disk) in texts["downloads"] and "[done" in texts["wait"],
          f"{json.dumps(texts)[:600]}")
    check("... a refused upload's error names the place", out["upload_refused"][1]
          and ".ssh" in out["upload_refused"][1], f"{out['upload_refused'][1]}")

    # ── the log ─────────────────────────────────────────────────────────
    lines = logs.lines()
    named = {k for k in ("borrow", "return", "show", "downloads", "upload")
             if any(m.startswith(f"agent: {k}") for m in lines)}
    leaks = [m for m in lines if any(s in m for s in ("upload-a.txt", "upload-b.bin", "half typed",
                                                      "a draft"))]
    check("the log: a line per new request, the moves and placements; never an uploaded file's "
          "name or what the page holds", named == {"borrow", "return", "show", "downloads",
                                                   "upload"}
          and any("placed on workspace" in m for m in lines)
          and any("moved from window" in m for m in lines) and not leaks,
          f"named {named}; leaks {leaks[:2]}")
    import logging
    errors = [r.getMessage() for r in logs.records if r.levelno >= logging.ERROR]
    check("no errors logged by the browser during the run", not errors, f"{errors[:3]}")

    client.run(jack.close())
    elsewhere.close()
    browser.shutdown()
    shiboken6.delete(browser)
    shiboken6.delete(profile)
    QTest.qWait(50)
    os.close(lock)
    shell.close()
    del app


# ── 3. the shell's side, in a child process ─────────────────────────────
def shell_side() -> None:
    """Run as `--shell-side`: prints one JSON line of what it found."""
    import numpy as np
    import skia

    from indigoshell import theme
    from indigoshell.applink.service import AppService, pick_desktop
    from indigoshell.browser import protocol
    from indigoshell.browser.context import JackWindow
    from indigoshell.widgets.base import Size

    out = {}
    U = 0xFFFFFFFF
    zero = [0] * 18
    out["rule"] = {
        "highest empty": pick_desktop(9, 1, zero, [(0, True), (1, True), (2, False), (3, False),
                                                   (7, False), (8, False)]),
        "one placed already": pick_desktop(9, 1, zero, [(0, True), (1, True)], {8}),
        "another monitor": pick_desktop(9, 1, zero[:16] + [1920, 0], [(1, True)]),
        "a mapped window shows it": pick_desktop(6, 0, [0] * 12, [(5, True)]),
        "sticky ignored": pick_desktop(3, 0, [0] * 6, [(U, True)]),
        "none empty": pick_desktop(3, 0, [0] * 6, [(1, False), (2, False)]),
        "all shown": pick_desktop(2, 0, [0, 0, 1920, 0], []),
        "no workspaces": pick_desktop(0, None, [], []),
    }

    class Line:
        def __init__(self, service):
            self.service, self.out = service, []

        def send(self, msg):
            self.out.append(msg)

    class Host:
        def damage(self, widget=None, *, layout=False):
            pass

    def paint(widget, w, h):
        surface = skia.Surface(w, h)
        canvas = surface.getCanvas()
        canvas.clear(theme.color(theme.BAR_BG))
        widget.measure(Size(w, h))
        widget.arrange(skia.Rect.MakeWH(w, h))
        widget.paint(canvas)
        return surface.makeImageSnapshot().toarray(
            colorType=skia.kRGBA_8888_ColorType, alphaType=skia.kUnpremul_AlphaType
        )[..., :3].astype("int16")

    service = AppService({"jack": JackWindow}, path="/nonexistent")
    lilac = np.array([int(protocol.voice_color("lilac")[i:i + 2], 16) for i in (1, 3, 5)])
    state = protocol.state([protocol.tab("Alpha", "x.org", False)], 0, 100, True,
                           url="https://x.org/", bookmarked=False, find=None)
    for name, ann in (("hers", protocol.announce(0x400001, False, "lilac")),
                      ("users", protocol.announce(0x400002, False))):
        j = JackWindow(Line(service), ann["wid"], ann)
        j.content.attach(Host())
        j.on_message(state)
        img = paint(j.content, 900, theme.TOP_BAR_HEIGHT)
        first = j.content.tabs.layout()[0]["x"]
        lead = img[:, :int(first)]
        out[name] = {"agent": j.agent, "first_tab_x": first,
                     "lilac_before_tabs": int((np.abs(lead - lilac).max(-1) <= 10).sum()),
                     "owner": j.content.owner.label if j.content.owner else None}
    j = JackWindow(Line(service), 0x400003, protocol.announce(0x400003, False, "lilac"))
    service.place(j, {"type": "place"})
    out["no_display"] = j.link.out[-1] if j.link.out else None
    print(json.dumps(out))


def test_shell_side() -> None:
    section("3. the shell's side (a child process): the workspace rule, the top bar")
    proc = subprocess.run([PY, "-B", __file__, "--shell-side"], capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0:
        check("the shell-side child ran", False, proc.stderr.strip()[-400:])
        return
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    r = {k: tuple(v) for k, v in out["rule"].items()}
    check("the workspace rule: the highest-numbered empty one no screen shows; one placed already "
          "counts as used; another monitor's and one with a mapped window count as shown; a "
          "window on every workspace counts for none",
          r["highest empty"][0] == 6 and r["one placed already"][0] == 7
          and r["another monitor"][0] == 7 and r["a mapped window shows it"][0] == 4
          and r["sticky ignored"][0] == 2, f"{out['rule']}")
    check("... none empty: the highest-numbered hidden one; every one shown, or none named: no "
          "workspace, with why", r["none empty"][0] == 2 and "no workspace is empty" in
          r["none empty"][1] and r["all shown"] == (None, "every workspace is shown")
          and r["no workspaces"][0] is None, f"{r['none empty']} {r['all shown']}")
    check("the top bar for the agent's own window: \"Lilac\" first, in Lilac's colour, the tabs "
          "after it; the user's window as ever",
          out["hers"]["agent"] == "lilac" and out["hers"]["owner"] == "Lilac"
          and out["hers"]["lilac_before_tabs"] > 30 and out["users"]["owner"] is None
          and out["users"]["lilac_before_tabs"] == 0
          and out["hers"]["first_tab_x"] > out["users"]["first_tab_x"] + 20,
          f"{out['hers']} {out['users']}")
    check("place with no X display: answered placed null, saying why",
          out["no_display"] == {"type": "placed", "desktop": None, "why": "no X display"},
          f"{out['no_display']}")


# ── --on-screen ─────────────────────────────────────────────────────────
class Desk:
    """The test's own X connection: the root's workspace properties, its
    own windows' state; never anything of the user's but reading."""

    def __init__(self) -> None:
        import xcffib
        import xcffib.xproto  # noqa: F401  (the core protocol, before connecting)
        self.conn = xcffib.connect()
        self.screen = self.conn.get_setup().roots[0]
        self.root = self.screen.root
        self._atoms = {}
        self.trace: list[tuple] = []

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.conn.core.InternAtom(False, len(name),
                                                          name.encode()).reply().atom
        return self._atoms[name]

    def ints(self, wid: int, name: str, length: int = 1024) -> list[int]:
        import struct

        import xcffib.xproto as xp
        try:
            r = self.conn.core.GetProperty(False, wid, self.atom(name), xp.GetPropertyType.Any,
                                           0, length).reply()
        except Exception:       # noqa: BLE001
            return []
        raw = bytes(r.value.buf())
        return list(struct.unpack(f"<{len(raw) // 4}I", raw)) if r.format == 32 and raw else []

    def one(self, wid: int, name: str):
        v = self.ints(wid, name, 1)
        return v[0] if v else None

    def text(self, wid: int, name: str) -> bytes:
        import xcffib.xproto as xp
        try:
            r = self.conn.core.GetProperty(False, wid, self.atom(name), xp.GetPropertyType.Any,
                                           0, 256).reply()
        except Exception:       # noqa: BLE001
            return b""
        return bytes(r.value.buf())

    def user(self) -> tuple:
        """(current workspace, active window): the user's, read."""
        now = (self.one(self.root, "_NET_CURRENT_DESKTOP"), self.one(self.root, "_NET_ACTIVE_WINDOW"))
        self.trace.append((round(time.monotonic(), 2), *now))
        return now

    def mapped(self, wid: int) -> int | None:
        try:
            return self.conn.core.GetWindowAttributes(wid).reply().map_state
        except Exception:       # noqa: BLE001
            return None


def placement_child(sock: str) -> None:
    """Run as `--placement SOCK`: the app service on its own X connection
    and socket; a bare window of this process's, not mapped, asks to be
    placed; then mapped, watched, destroyed. Prints one JSON line."""
    import struct

    import xcffib.xproto as xp

    from indigoshell.applink import protocol as link
    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display

    desk = Desk()
    app = desk.conn                     # the "app": a connection of its own
    out = {"before": desk.user()}

    def make() -> int:
        wid = app.generate_id()
        s = desk.screen
        app.core.CreateWindow(s.root_depth, wid, desk.root, 0, 0, 240, 160, 0,
                              xp.WindowClass.InputOutput, s.root_visual, xp.CW.BackPixel,
                              [s.black_pixel])
        raw = b"indigoshell\0indigoshell\0"      # floats, by the user's qtile rules
        app.core.ChangeProperty(xp.PropMode.Replace, wid, xp.Atom.WM_CLASS, xp.Atom.STRING, 8,
                                len(raw), raw)
        app.core.ChangeProperty(xp.PropMode.Replace, wid, xp.Atom.WM_HINTS, xp.Atom.WM_HINTS, 32,
                                9, struct.pack("<9I", 1, 0, 0, 0, 0, 0, 0, 0, 0))   # no input
        app.core.ChangeProperty(xp.PropMode.Replace, wid, desk.atom("_NET_WM_USER_TIME"),
                                xp.Atom.CARDINAL, 32, 1, struct.pack("<I", 0))
        app.flush()
        return wid

    async def ask(wid: int, near: int | None = None) -> dict:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(link.encode(link.announce("probe", wid)) + link.encode(link.place(near)))
        await writer.drain()
        answer = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), 3)
            msg = link.decode(line)
            if msg and msg["type"] == link.PLACED:
                answer = msg
                break
        answer["_writer"] = writer
        return answer

    async def main():
        service = AppService({}, path=sock, display=Display())
        service.start()
        await asyncio.sleep(0.2)
        wid = make()
        out["wid"] = wid
        placed = await ask(wid)
        out["placed"] = placed.get("desktop")
        out["why"] = placed.get("why")
        out["before_map"] = {"desktop": desk.one(wid, "_NET_WM_DESKTOP"), "map": desk.mapped(wid)}
        out["samples"] = []
        if out["placed"] is not None:
            app.core.MapWindow(wid)
            app.flush()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.5:
                await asyncio.sleep(0.05)
                sample = (desk.one(wid, "_NET_WM_DESKTOP"), desk.mapped(wid), *desk.user())
                out["samples"].append(sample)
                if sample[1] == xp.MapState.Viewable or sample[3] == wid:
                    break               # on the user's screen: away at once
            # qtile's own word (read only); its socket is under the real cache dir.
            info = subprocess.run(["qtile", "cmd-obj", "-o", "window", str(wid), "-f", "info"],
                                  capture_output=True, text=True, timeout=5,
                                  env={**os.environ, "XDG_CACHE_HOME": os.environ.get(
                                      REAL_CACHE_ENV, "")}).stdout
            try:
                out["qtile"] = {k: json.loads(info).get(k) for k in ("group", "floating")}
            except ValueError:
                out["qtile"] = None
            second = make()
            beside = await ask(second, wid)
            out["beside"] = {"placed": beside.get("desktop"),
                             "desktop": desk.one(second, "_NET_WM_DESKTOP")}
            app.core.DestroyWindow(second)
            beside["_writer"].close()
        app.core.DestroyWindow(wid)
        app.flush()
        placed["_writer"].close()
        await asyncio.sleep(0.4)
        service.stop()

    asyncio.run(main())
    out["after"] = desk.user()
    out["trace"] = desk.trace
    print(json.dumps(out))


def serve_apps(sock: str) -> None:
    """Run as `--serve-apps SOCK`: the app service on its own X connection
    until stdin closes; a JSON line for each window placed."""
    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from indigoshell.browser.context import JackWindow

    async def main():
        service = AppService({"jack": JackWindow}, path=sock, display=Display())
        place = service.place

        def placing(window, msg):
            place(window, msg)
            print(json.dumps({"wid": window.wid, "placed": window.placed,
                              "agent": getattr(window, "agent", None)}), flush=True)
        service.place = placing
        service.start()
        print(json.dumps({"ready": True}), flush=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sys.stdin.read)
        service.stop()

    asyncio.run(main())


def test_on_screen() -> None:
    section("on screen A: the shell places a bare X window of the test's own before it maps")
    proc = subprocess.run([PY, "-B", __file__, "--placement", SOCK["apps"]], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        check("the placement child ran", False, proc.stderr.strip()[-600:])
        return
    a = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"  the user's (workspace, active window): before {a['before']}, after {a['after']}; "
          f"trace {a['trace']}", flush=True)
    seen = {tuple(s[:2]) for s in a["samples"]}
    current = a["before"][0]
    ok_a = check("placed before the map: _NET_WM_DESKTOP set and read back while the window was "
                 "still unmapped", a["placed"] is not None and a["placed"] != current
                 and a["before_map"] == {"desktop": a["placed"], "map": 0},
                 f"placed {a['placed']} ({a['why']}); before map {a['before_map']}")
    ok_a &= check("mapped: qtile keeps it on that workspace (its group), unmapped, never focused; "
                  "the user's workspace never switched to it",
                  seen == {(a["placed"], 0)} and all(s[2] != a["placed"] for s in a["samples"])
                  and all(s[3] != a.get("wid") for s in a["samples"])
                  and (a.get("qtile") or {}).get("group") == str(a["placed"] + 1),
                  f"samples {sorted(seen)}; qtile {a.get('qtile')}")
    check("a second window placed `with` the first: on the same workspace",
          a.get("beside") == {"placed": a["placed"], "desktop": a["placed"]}, f"{a.get('beside')}")
    check("the user's workspace and focused window the same after as before (a change between "
          "is the user's own: see the trace)", a["after"] == a["before"],
          f"{a['before']} -> {a['after']}")
    if not ok_a:
        print("  B skipped: the placement did not hold", flush=True)
        return

    section("on screen B: the agent's own Jack window, placed before it maps")
    service = subprocess.Popen([PY, "-B", __file__, "--serve-apps", SOCK["apps"]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        _agent_window_on_screen(service)
    finally:
        if service.poll() is None:
            service.stdin.close()
            service.wait(10)


def _agent_window_on_screen(service) -> None:
    import shiboken6
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from indigoshell.browser import control, protocol
    from indigoshell.browser.qt import app as browser_app
    from indigoshell.browser.qt import instance, startup
    from indigoshell.browser.qt.profile import make_profile

    ready = service.stdout.readline()
    if '"ready"' not in ready:
        check("the app service child started", False, ready)
        return
    desk = Desk()
    before = desk.user()
    Log()
    startup.apply_flags()
    app = browser_app.make_application()
    profile = make_profile()
    lock = instance.try_lock()
    browser = browser_app.Browser(profile, unattended=False)
    browser.listen(SOCK["browser"])
    client = Client(QTest.qWait)
    jack = client.run(control.connect())
    tab = client.run(jack.open(url("/live"), window="agent", voice="lilac"))
    her = next(w for w in browser.windows if w.agent)
    wid = int(her.winId())
    client.run(jack.wait(tab.tab, timeout=15), 20)
    samples = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        QTest.qWait(100)
        samples.append((desk.one(wid, "_NET_WM_DESKTOP"), desk.mapped(wid), *desk.user()))
        if samples[-1][1] == 2 or samples[-1][3] == wid:
            her.close()                 # on the user's screen: away at once
            QTest.qWait(200)
            check("her window stayed off the user's screen", False, f"{samples[-1]}")
            browser.shutdown()
            os.close(lock)
            return

    def ticks():
        box = []
        her.current_tab().page.runJavaScript(
            "JSON.stringify({t: ticks, vis: document.visibilityState})", 0, box.append)
        end = time.monotonic() + 5
        while not box and time.monotonic() < end:
            QTest.qWait(20)
        return json.loads(box[0]) if box else None
    t1 = ticks()
    QTest.qWait(1500)
    t2 = ticks()
    shot = client.run(jack.screenshot(tab.tab))
    role, title = desk.text(wid, "WM_WINDOW_ROLE"), desk.text(wid, "_NET_WM_NAME")
    client.run(jack.close_tab(tab.tab))
    QTest.qWait(300)
    after = desk.user()
    service.stdin.close()
    lines = [json.loads(x) for x in service.stdout.read().splitlines() if x.strip()]
    service.wait(10)
    placed = next((x for x in lines if x.get("wid") == wid), None)
    print(f"  the user's (workspace, active window): before {before}, after {after}; "
          f"trace {desk.trace}", flush=True)
    check("the app service placed her window (announced as Lilac's) on an empty workspace "
          "before it mapped", placed is not None and placed["agent"] == "lilac"
          and placed["placed"] is not None and placed["placed"] != before[0]
          and her.placed_on == placed["placed"], f"{placed} {lines}")
    check("never mapped, never active, never the shown workspace while it lived",
          samples and all(s[0] == placed["placed"] and s[1] == 0 for s in samples)
          and all(s[3] != wid and s[2] != placed["placed"] for s in samples)
          and QApplication.activeWindow() is None, f"{samples[-3:]}")
    rate = ((t2["t"]["raf"] - t1["t"]["raf"]) / 1.5) if t1 and t2 else 0
    check("its page live there: visible to itself, timers and animation frames running, a "
          "screenshot of it", t2 is not None and t2["vis"] == "visible" and rate > 20
          and t2["t"]["timer"] > t1["t"]["timer"] and shot["width"] > 100,
          f"rAF {rate:.0f}/s {t2}")
    check("its X window: role and title the agent's",
          role == protocol.AGENT_ROLE.encode() and title.startswith("Lilac · Jack".encode()),
          f"{role} {title}")
    check("the user's workspace and focused window the same after as before (a change between "
          "is the user's own: see the trace)", after == before, f"{before} -> {after}")
    client.run(jack.close())
    browser.shutdown()
    shiboken6.delete(browser)
    shiboken6.delete(profile)
    os.close(lock)
    del app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--on-screen", action="store_true",
                        help="only the on-screen checks: placing on an empty workspace")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--shell-side", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--placement", help=argparse.SUPPRESS)
    parser.add_argument("--serve-apps", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.shell_side:
        shell_side()
        return 0
    if args.placement:
        placement_child(args.placement)
        return 0
    if args.serve_apps:
        serve_apps(args.serve_apps)
        return 0
    global PORT, SCRATCH, HOME
    os.environ[REAL_CACHE_ENV] = (os.environ.get("XDG_CACHE_HOME")
                                  or os.path.expanduser("~/.cache"))
    SCRATCH = Path(tempfile.mkdtemp(prefix=f"{APP}-alone-test-"))
    HOME = SCRATCH / "home"
    os.environ.update({
        "XDG_DATA_HOME": str(SCRATCH / "data"),
        "XDG_CACHE_HOME": str(SCRATCH / "cache"),
        "XDG_CONFIG_HOME": str(SCRATCH / "config"),
        "XDG_STATE_HOME": str(SCRATCH / "state"),
        env("BROWSER_SOCKET"): SOCK["browser"],
        env("APPS_SOCKET"): SOCK["apps"],
        env("BROWSER_WIDEVINE"): "off",
    })
    for var in ("GNUPGHOME", "PASSWORD_STORE_DIR"):
        os.environ.pop(var, None)
    (SCRATCH / "config").mkdir(parents=True, exist_ok=True)
    http = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    PORT = http.server_address[1]
    print(f"scratch {SCRATCH}", flush=True)
    bookmarks = Path.home() / ".config" / APP / "browser" / "bookmarks.json"
    marks_before = bookmarks.read_bytes() if bookmarks.exists() else None

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
            files = make_home()
            test_pure(files)
            test_in_process(files)
            test_shell_side()
    except Exception:
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    finally:
        os.dup2(saved, 2)
        http.shutdown()
    marks_after = bookmarks.read_bytes() if bookmarks.exists() else None
    check("the user's bookmarks untouched", marks_before == marks_after)
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
