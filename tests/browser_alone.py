"""The agent alone in Flux: its own window, borrowed tabs, show, downloads,
uploads, the sites kept from it, and the hands for them.

    .venv/bin/python tests/browser_alone.py              off screen, about a minute

Out of sight (a hidden cage's client: tests/_cage.py), nothing appears:
  1. pure parts: the refused upload places (flux/uploads.py) against a
     scratch home holding fake secrets (.ssh, .gnupg, keyrings, pass, rbw,
     Firefox's profiles, the shell's data and connections, key-like
     names), reached directly and through links from /tmp; what may go;
     sites.json's agent table; the new requests and the agent's own
     window's bar (her name first); the
     hands' schemas (JSON Schema) and what their descriptions tell the
     agent.
  2. one browser in this process, its control socket and a stand-in
     shell on sockets of the test's (the shell answers `place` a moment
     late, with workspace 6), the client on an asyncio loop in a thread,
     the user's attention on another window: `open` in the agent's
     window (made, its bar starting with Lilac's name, asked to be
     placed before it is
     shown and not shown until the answer, its title and role, shown
     without activating and never raised or activated by Flux (the
     hidden cage focuses whatever it shows); `open` with no window still
     the user's);
     borrow (a tab that is not showing: its typed form, scroll, script
     state and cookie kept, gone from the user's window and the bar
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
     Flux, nothing reaching the page then; Qt's own chooser never asked;
     a chooser opened by a plain click refused); a host sites.json keeps
     from the agent (every request on its tab refused with the exact
     words, `open` and `go` to it too, `tabs` naming it with no title,
     `release` still fine); every new hand run against it; the log
     (requests named, no uploaded file's name).
  3. the shell's side (a child process, so skia and Qt stay apart): the
     top bar for the agent's own window, her name first in her colour;
     `place` with no window manager answered with why. (Where niri puts
     the window: tests/wayland.py, its spare workspace and the service's
     placing.)

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
sys.path.insert(0, str(REPO / "tests"))

import _cage                    # noqa: E402

_cage.inside()                  # out of sight: this test runs as a hidden cage's client

from indigoshell.core.naming import APP, env          # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir        # noqa: E402

PY = sys.executable
FAILED: list[str] = []
# Where the hands say Flux shows (flux/hands.py `where`): the laptop's.
SCREEN = "This Flux shows on the laptop's screen, where the user works."
SCRATCH = Path("/nonexistent")          # made in main (not in the children)
HOME = Path("/nonexistent")             # the scratch home the upload check is pointed at
TAG = f"{APP}-al-{os.getpid()}"
SOCK = {name: os.path.join(runtime_dir(), f"{TAG}-{name}.sock") for name in ("browser", "apps")}
PORT = 0
PLACED_ON = 6                           # what the stand-in shell answers `place` with
PLACE_DELAY_S = 0.4                     # ... this late


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
        "flux profile": Path(data_dir()) / "browser/Cookies",
        "flux history": Path(data_dir()) / "browser-history.sqlite",
        "flux cache": Path(cache_dir()) / "browser/x",
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
    from indigoshell.core.hands import Hand
    from indigoshell.applink import protocol as link
    from flux import protocol, uploads
    from flux.hands import browser_hands
    from flux.qt.identity import AGENT_TABLE, Rules

    section("1. pure parts")
    home = str(HOME)
    wrong = {k: uploads.refusal(p, home) for k, p in files["refused"].items()
             if uploads.refusal(p, home) is None}
    check("upload: every secret place, key-like name, link into one, folder, missing and "
          "relative path refused", not wrong, f"{wrong}")
    said = {k: uploads.refusal(p, home) for k, p in files["refused"].items()}
    check("... saying why: the place and what it holds, the name, outside the home",
          ".ssh (SSH keys)" in said["ssh config"] and "keyrings" in said["keyring"]
          and "the shell's data" in said["flux profile"] and "connections" in said["connections"]
          and "private key" in said["named id_rsa"] and "outside the home" in said["link out of home"]
          and "keyrings" in said["link named innocently"] and ".ssh" in said["through a linked folder"],
          f"{said['ssh config']} | {said['link out of home']} | {said['flux profile']}")
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

    from flux import bar as fb
    st = fb.state([fb.tab("A", "a.org", False, id_=1)], 0, 100, True, url="https://a.org/",
                  bookmarked=False)
    hers, users = fb.bar_of(st, agent=True), fb.bar_of(st)
    check("the agent's own window's bar starts with her name in her colour (the `agent` role); "
          "the user's with the tabs",
          hers["start"][0] == {"kind": "text", "id": "owner", "text": "Lilac", "color": "agent"}
          and users["start"][0]["kind"] == "tabs", f"{hers['start'][0]}")
    check("place / placed on the apps' line", link.place() == {"type": "place"}
          and link.place(5) == {"type": "place", "with": 5}
          and link.placed(6) == {"type": "placed", "desktop": 6}
          and link.placed(None, "why") == {"type": "placed", "desktop": None, "why": "why"})
    check("the requests: borrow, return, show, downloads, upload; the words",
          {"borrow", "return", "show", "downloads", "upload"} <= set(protocol.REQUESTS)
          and protocol.kept("bank.example") == "the user keeps bank.example to themselves"
          and protocol.agent_title() == "Lilac · Flux")

    hands = {h.name: h for h in browser_hands(where=SCREEN)}
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

    def of(self, window: int) -> list[dict]:
        """What the connection that announced Flux's window `window` (by
        its own id) sent, in order."""
        conns = {n for n, _, m in self.msgs if m.get("type") == "announce"
                 and m.get("window") == window}
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
        for _name in ("flux", APP):
            logging.getLogger(_name).setLevel(logging.DEBUG)

    def lines(self, level=20) -> list[str]:
        return [r.getMessage() for r in self.records
                if r.name.startswith("flux") and r.levelno >= level]


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

    from indigoshell.core.hands import Call
    from flux import control, protocol, uploads
    from flux.hands import browser_hands
    from flux.qt import app as browser_app
    from flux.qt import instance, startup
    from flux.qt.profile import make_profile
    from flux.qt.window import Window
    from indigoshell.core.paths import config_dir

    logs = Log()
    users: list[tuple] = []             # what reached Qt's own dialogs and chooser
    QWebEnginePage.chooseFiles = lambda self, *a: users.append(("files", a[0].name)) or []
    QWebEnginePage.javaScriptAlert = lambda self, o, m: users.append(("alert", m))
    calls: list[tuple] = []             # raise_ and activateWindow, on any Flux window
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
    flux = client.run(control.connect())
    J = lambda coro, t=30.0: client.attempt(coro, t)      # noqa: E731

    def js(tab_id, code, timeout=5.0):
        window, tab = next((w, t) for w in browser.windows for t in w.tabs if t.id == tab_id)
        box = []
        tab.page.runJavaScript(code, 0, box.append)
        wait(lambda: box, timeout)
        return box[0] if box else None

    def refs_of(tab_id):
        page_, _ = J(flux.read(tab_id, "outline", hidden=True))
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
    form, _ = J(flux.open(url("/form")))
    J(flux.wait(form.tab, timeout=15), 20)
    user = window_of(form.tab)
    other, _ = J(flux.open(url("/page/Other")))
    J(flux.wait(other.tab, timeout=15), 20)
    elsewhere = QWidget()
    elsewhere.resize(200, 100)
    elsewhere.show()
    _cage.focus(elsewhere)
    wait(lambda: user.link.attached, 5)
    calls.clear()

    # ── the agent's own window ──────────────────────────────────────────
    future = client.start(flux.open(url("/page/Mine"), window="agent"))
    shown_early, her = False, None
    deadline = time.monotonic() + 10
    while not future.done() and time.monotonic() < deadline:
        QTest.qWait(10)
        her = next((w for w in browser.windows if w.agent), her)
        if her is not None and her.isVisible() and not shell.answered:
            shown_early = True
    mine = future.result() if future.done() else None
    her = next((w for w in browser.windows if w.agent), None)
    hid = her.id if her is not None else 0
    said = shell.of(hid)
    announce = next((m for m in said if m.get("type") == "announce"), {})
    kinds = [m["type"] for m in said if m.get("type") in ("announce", "place")]
    bar = next((m for m in said if m.get("type") == "bar"), {})
    check("open in the agent's window: a window of its own, made for her, announced (Flux, "
          "its id), `place` asked right after the announce; its bar starts with her name",
          her is not None and her.agent is True and announce.get("app") == "flux"
          and announce.get("window") == her.id and kinds[:2] == ["announce", "place"]
          and (bar.get("start") or [{}])[0].get("text") == "Lilac"
          and mine is not None and mine.window == her.id, f"{kinds} {announce}")
    check("... not shown until the shell answered (a moment late on purpose), then shown, "
          f"placed on workspace {PLACED_ON}", not shown_early and her.isVisible()
          and her.placed_on == PLACED_ON and her.ready, f"early {shown_early}")
    J(flux.wait(mine.tab, timeout=15), 20)
    QTest.qWait(100)
    check("... its title says whose (\"Lilac · Flux · Mine\"), its role is the agent's, its class "
          "Flux's", her.windowTitle() == "Lilac · Flux · Mine"
          and her.windowRole() == protocol.AGENT_ROLE
          and QApplication.applicationName() == f"{APP}-browser", f"{her.windowTitle()!r}")
    # The hidden cage gives every window it shows the focus, this one too;
    # niri does not (its `open-focused false` rule: docs/niri.kdl).
    check("... never raised or activated by Flux: shown without activating (its QWindow says "
          "so), no raise_ or activateWindow",
          not calls and her.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
          and her.windowHandle().property("_q_showWithoutActivating") is True, f"{calls}")
    plain, _ = J(flux.open(url("/page/Plain")))
    check("open with no window: still the user's window, never the agent's",
          plain is not None and plain.window == user.id, f"{plain}")
    J(flux.close_tab(plain.tab))
    windows, _ = J(flux.tabs())
    hers = [w for w in windows or () if w.agent]
    check("tabs: the agent's window listed as hers (`agent`: true), the only one",
          len(hers) == 1 and hers[0].window == her.id and hers[0].agent is True)
    again, _ = J(flux.open(url("/page/Second"), window="agent"))
    check("a second open in the agent's window: the same window, not placed again",
          again is not None and again.window == her.id
          and [m["type"] for m in shell.of(hid)].count("place") == 1)
    J(flux.close_tab(again.tab))

    # ── borrow ──────────────────────────────────────────────────────────
    J(flux.switch(other.tab))
    js(form.tab, "document.getElementById('name').value = 'half typed'; "
                 "document.getElementById('note').value = 'a draft'; scrollTo(0, 1234); "
                 "window.marker = 'changed-before-the-move'; 1")
    STATE = ("JSON.stringify({name: document.getElementById('name').value, note: "
             "document.getElementById('note').value, scroll: scrollY, marker: window.marker, "
             "cookie: document.cookie})")
    before = json.loads(js(form.tab, STATE))
    page_before = next(t.page for t in user.tabs if t.id == form.tab)
    states_before = len([m for m in shell.of(user.id) if m.get("type") == "bar"])
    done, err = J(flux.borrow(form.tab))
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
    last_bar = [m for m in shell.of(user.id) if m.get("type") == "bar"][-1]
    last_tabs = last_bar["start"][-1]["tabs"]
    check("... gone from the user's window and from the bar the shell gets; the user's window "
          "still shows the tab it showed", titles(user) == ["Other"]
          and user.current_tab().id == other.tab
          and [t["title"] for t in last_tabs] == ["Other"]
          and len([m for m in shell.of(user.id) if m.get("type") == "bar"])
          > states_before, f"{titles(user)} {last_tabs}")
    press = refs_of(form.tab)['button "Press"']
    acted, err = J(flux.click(form.tab, press))
    overlay = browser.watch.overlays.get(her.id)
    check("acting on it in the agent's window: the Watch drives it there, its overlay in that "
          "window", acted is not None and acted["agent"] == "driving" and overlay is not None
          and overlay.shown() and js(form.tab, "document.getElementById('press').textContent")
          == "pressed", f"{err}")
    _, e_again = J(flux.borrow(form.tab))
    _, e_mine = J(flux.borrow(mine.tab))
    check("borrow refused: a tab in the agent's window already (borrowed, or its own)",
          e_again and "own window already" in e_again and e_mine and "already" in e_mine,
          f"{e_again} / {e_mine}")
    # The user goes to the agent's window and types: theirs, as anywhere.
    QWidget.activateWindow(her)         # the user's doing (not Flux's: not recorded)
    wait(lambda: QApplication.activeWindow() is her, 3)
    QTest.keyClick(her.windowHandle(), Qt.Key.Key_Q)
    _, e_taken = J(flux.click(form.tab, press))
    check("a key the user presses in the agent's window takes the tab over there too: the next "
          "request says so, the overlay goes", e_taken == f"the user took over tab {form.tab}"
          and not overlay.shown() and browser.watch.paused(form.tab), f"{e_taken}")
    QWidget.activateWindow(elsewhere)
    wait(lambda: QApplication.activeWindow() is elsewhere, 3)

    # ── return ──────────────────────────────────────────────────────────
    held = json.loads(js(form.tab, STATE) or "{}")
    done, err = J(flux.return_tab(form.tab))
    check("return: back to its window at its old place, in the background (the user's window "
          "still shows its tab), the agent's session on it ended",
          done is not None and done["to"] == "its window" and window_of(form.tab) is user
          and user.tabs[0].id == form.tab and user.current_tab().id == other.tab
          and form.tab not in browser.watch.sessions and done["borrowed"] is None, f"{err}")
    back = json.loads(js(form.tab, STATE) or "{}")
    check("... still the same page with its state (the scroll where the agent's click left it)",
          back == held and back["name"] == "half typed" and back["marker"] == before["marker"],
          f"{held} -> {back}")
    J(flux.borrow(form.tab))
    done, err = J(flux.return_tab(form.tab, show=True))
    check("return with show: the current tab of its window (the window not raised or activated)",
          done is not None and user.current_tab().id == form.tab and done["current"]
          and not calls, f"{err} {calls}")
    # A window's last tab: a blank one keeps its place.
    lone = browser.new_window()
    lone_tab = lone.add_tab(browser_app.to_url(url("/page/Lone")))
    lone.show()
    J(flux.wait(lone_tab.id, timeout=15), 20)
    done, err = J(flux.borrow(lone_tab.id))
    blank = [t.url().toString() for t in lone.tabs] if shiboken6.isValid(lone) else None
    check("borrow a window's last tab: the window stays, a blank tab in its place",
          done is not None and shiboken6.isValid(lone) and blank == ["about:blank"], f"{err} {blank}")
    done, err = J(flux.return_tab(lone_tab.id))
    check("... returned: back in that window, the blank tab gone",
          done is not None and shiboken6.isValid(lone) and [t.id for t in lone.tabs] == [lone_tab.id]
          and done["to"] == "its window", f"{err} {[t.url().toString() for t in lone.tabs]}")
    lone.close()
    QTest.qWait(100)
    # A tab of the agent's own goes to the user's most recent window.
    done, err = J(flux.return_tab(mine.tab))
    check("return a tab the agent opened itself: to the end of the user's most recently used "
          "window; the agent's window closes with its last tab",
          done is not None and done["to"] == "another window" and window_of(mine.tab) is user
          and user.tabs[-1].id == mine.tab and not any(w.agent for w in browser.windows), f"{err}")
    _, e_not_hers = J(flux.return_tab(other.tab))
    check("return refused for a tab that is not in the agent's window",
          e_not_hers and "not in your own window" in e_not_hers, f"{e_not_hers}")

    # The agent's window closed by the user while it holds a borrowed tab.
    own, _ = J(flux.open(url("/page/Own"), window="agent"))
    her2 = window_of(own.tab)
    J(flux.borrow(form.tab))
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
    shown, err = J(flux.show(str(report)))
    J(flux.wait(shown["tab"], timeout=15), 20) if shown else None
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
    by_url, err2 = J(flux.show(report.as_uri()))
    _, e_missing = J(flux.show(str(HOME / "reports/none.html")))
    _, e_dir = J(flux.show(str(HOME / "reports")))
    _, e_rel = J(flux.show("reports/today/index.html"))
    check("show by file:// URL too; a missing path, a folder, a relative path refused, saying so",
          by_url is not None and e_missing and "does not exist" in e_missing
          and e_dir and "is not a file" in e_dir and e_rel and "absolute" in e_rel,
          f"{err2} / {e_missing} / {e_dir} / {e_rel}")
    for t in (shown, by_url):
        if t:
            J(flux.close_tab(t["tab"]))

    # ── downloads ───────────────────────────────────────────────────────
    dl, _ = J(flux.open(url("/dl")))
    J(flux.wait(dl.tab, timeout=15), 20)
    R = refs_of(dl.tab)
    clicked, err = J(flux.click(dl.tab, R['link "Get the data"']))
    wait(lambda: any(d["name"] == "data.bin" for d in browser.download_list), 10)
    listed, _ = J(flux.downloads())
    first = next((d for d in listed or () if d["name"] == "data.bin"), None)
    waited, err_w = J(flux.wait_download(first["download"], timeout=20), 30) if first else (None, "")
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
    J(flux.switch(dl.tab))
    J(flux.click(dl.tab, R['link "Get it slowly"']))
    wait(lambda: any(d["name"] == "slow.bin" for d in browser.download_list), 10)
    slow = next((d for d in browser.download_list if d["name"] == "slow.bin"), None)
    _, e_short = J(flux.wait_download(slow["download"], timeout=0.2), 10) if slow else (None, "")
    done_slow, err_s = J(flux.wait_download(slow["download"], timeout=30), 40) if slow else (None, "")
    listed, _ = J(flux.downloads())
    check("a slow download: a short wait times out saying how far; a long one sees it done; the "
          "list newest first", e_short and e_short.startswith("timed out after 0.2 s")
          and done_slow is not None and done_slow["state"] == "done"
          and done_slow["received"] == SLOW_SIZE and listed[0]["name"] == "slow.bin"
          and (downloads_dir / "slow.bin").stat().st_size == SLOW_SIZE,
          f"{e_short} {err_s} {done_slow}")
    _, e_none = J(flux.wait_download(99999, timeout=1))
    check("waiting for a download that is not there: refused", e_none and "no download 99999"
          in e_none, f"{e_none}")
    J(flux.close_tab(dl.tab))

    # ── upload ──────────────────────────────────────────────────────────
    up, _ = J(flux.open(url("/files"), window="agent"))
    J(flux.wait(up.tab, timeout=15), 20)
    R = refs_of(up.tab)
    a, b = files["allowed"]["text"], files["allowed"]["binary"]

    def changes():
        return json.loads(js(up.tab, "JSON.stringify(changes.splice(0))") or "[]")

    def holds(ident):
        return json.loads(js(up.tab, f"JSON.stringify(Array.from(document.getElementById("
                                     f"'{ident}').files).map(f => [f.name, f.size]))") or "[]")

    users.clear()
    done, err = J(flux.upload(up.tab, R['button "Many"'], [a, b]))
    got = changes()
    check("upload into a shown field: the page's input.files are the names and sizes, its "
          "change event trusted, as from its own chooser; the answer says them",
          done is not None and holds("vis") == [["upload-a.txt", 5], ["upload-b.bin", 1234]]
          and got == [{"id": "vis", "trusted": True, "files": [["upload-a.txt", 5],
                                                               ["upload-b.bin", 1234]]}]
          and done["files"] == [{"name": "upload-a.txt", "size": 5},
                                {"name": "upload-b.bin", "size": 1234}]
          and done["agent"] == "driving", f"{err} {got}")
    by_name = {f.get("name"): f["ref"] for form_ in J(flux.read(up.tab, "forms", hidden=True))[0]
               ["forms"] for f in form_["fields"]}
    hidden = {}
    for label, ref, ident in (("by its own ref, through its label", by_name.get("hid"), "hid"),
                              ("through the button that opens it", R.get('button "Browse"'),
                               "hid2")):
        done, err = J(flux.upload(up.tab, ref, [a]))
        hidden[label] = (done is not None and holds(ident) == [["upload-a.txt", 5]], err,
                         done and done.get("field"))
    check("a hidden field by its own ref (its shown label clicked), and through the button that "
          "opens it: the files arrive; `field` names the field, or none when the page's own "
          "button opened it", all(v[0] for v in hidden.values())
          and hidden["through the button that opens it"][2] is None, f"{hidden}")
    hid3 = by_name.get("hid3")
    done, err = J(flux.upload(up.tab, hid3, [b]))
    check("a hidden field with no label shown, by its own ref: a key lets the page open its "
          "chooser, a click in the agent's world opens it, the file arrives",
          done is not None and holds("hid3") == [["upload-b.bin", 1234]], f"{err} ref {hid3}")
    changes()
    _, e_one = J(flux.upload(up.tab, R['button "One"'], [a, b]))
    _, e_dir = J(flux.upload(up.tab, R['button "Folder"'], [a]))
    _, e_nothing = J(flux.upload(up.tab, R['button "Nothing"'], [a]))
    refused_through = {}
    for k in ("ssh config", "link named innocently", "flux profile", "named .pem", "connections"):
        _, refused_through[k] = J(flux.upload(up.tab, R['button "Many"'],
                                              [a, files["refused"][k]]))
    check("refused: two files for a field that takes one, a folder field, a click that opens no "
          "chooser (saying what to give instead)", e_one and "takes one file" in e_one
          and e_dir and "takes a folder" in e_dir and e_nothing
          and "opened no file chooser" in e_nothing, f"{e_one} / {e_dir} / {e_nothing}")
    check("refused through Flux: a secret place, a link into one, Flux's profile, a key-like "
          "name, the shell's connections; nothing reached the page; Qt's own chooser never asked",
          all(v and ("is in" in v or "named like" in v) for v in refused_through.values())
          and changes() == [] and holds("vis") == [["upload-a.txt", 5], ["upload-b.bin", 1234]]
          and not users, f"{refused_through} {users}")
    held = holds("hid2")
    done, err = J(flux.click(up.tab, R['button "Browse"']))
    got = changes()
    check("a chooser a plain click opens while the agent drives: refused, said in the answer; "
          "no file added (Chromium empties the field, as for the user's Cancel: Qt gives no "
          "files back to keep)", done is not None
          and any("only upload gives a page files" in n for n in done.get("notes", []))
          and held == [["upload-a.txt", 5]] and holds("hid2") == []
          and all(c["files"] == [] for c in got) and not users,
          f"{err} {done and done.get('notes')} {held} -> {holds('hid2')} {got} {users}")
    _, e_click = J(flux.click(up.tab, R['button "One"']))
    check("click on a file field: refused, pointing to upload",
          e_click and e_click.endswith("is a file field: give it files with upload"), f"{e_click}")
    J(flux.release())

    # ── a site kept from the agent ──────────────────────────────────────
    kept_tab = user.add_tab(browser_app.to_url(url("/page/Kept", host="localhost")))
    wait(lambda: kept_tab.page.title() == "Kept", 15)
    k = kept_tab.id
    words = "the user keeps localhost to themselves"
    refusals = {"look": J(flux.look(window=user.id))[1], "read": J(flux.read(k, "text"))[1], "screenshot": J(flux.screenshot(k))[1],
                "click": J(flux.click(k, x=5, y=5))[1], "borrow": J(flux.borrow(k))[1],
                "wait": J(flux.wait(k, timeout=1))[1], "close": J(flux.close_tab(k))[1],
                "open": J(flux.open(url("/page/K2", host="localhost")))[1]}
    J(flux.switch(form.tab))
    refusals["switch"] = J(flux.switch(k))[1]
    refusals["go"] = J(flux.go(form.tab, url("/page/K3", host="localhost")))[1]
    windows, _ = J(flux.tabs())
    listed = next((t for w in windows for t in w.tabs if t.tab == k), None)
    released, err_r = J(flux.release(k))
    check("a host sites.json keeps from the agent: every request on its tab refused with the "
          "exact words (look at it too), open and go to it as well",
          all(v == words for v in refusals.values()), f"{refusals}")
    check("... tabs lists it with no title and its address cut to the site; release still works",
          listed is not None and listed.title == "" and listed.kept
          and listed.url == f"http://localhost/" and released == [], f"{listed} {err_r}")
    user.close_tab(kept_tab)

    # ── an older shell, which never answers `place` ─────────────────────
    closed, _ = J(flux.close_tab(up.tab))      # her window goes with its last tab
    shell.answer_place = False
    began = time.monotonic()
    again, err = J(flux.open(url("/page/Again"), window="agent"))
    took = time.monotonic() - began
    hers2 = window_of(again.tab) if again else None
    check("a shell that never answers `place`: the agent's window, made again, shows anyway "
          "after PLACE_WAIT_S (placed nowhere)", closed and closed["window_closed"]
          and hers2 is not None and hers2.agent is True and hers2.isVisible()
          and hers2.placed_on is None and 1.3 < took < 4.0
          and hers2.windowTitle().startswith("Lilac · Flux"), f"{err} {took:.2f} s")
    J(flux.close_tab(again.tab))
    shell.answer_place = True

    # ── the hands ───────────────────────────────────────────────────────
    hands = {h.name: h for h in browser_hands(where=SCREEN)}

    def call(name, args, timeout=120.0):
        blocks, err = J(hands[name].run(Call(f"c-{name}", name, args, timeout)), timeout)
        return "\n".join(b["text"] for b in blocks or ()), err

    out = {}
    out["open"] = call("browser_open", {"url": url("/page/Handy"), "window": "agent"})
    handy = next((t for w in browser.windows if w.agent for t in w.tabs), None)
    out["borrow"] = call("browser_borrow", {"tab": form.tab})
    out["tabs"] = call("browser_tabs", {})
    up2 = next(t.id for w in browser.windows if w.agent for t in w.tabs if t.id == form.tab)
    J(flux.go(up2, url("/files")))
    J(flux.wait(up2, timeout=15), 20)
    R = refs_of(up2)
    out["upload"] = call("browser_upload", {"tab": up2, "ref": R['button "Many"'], "paths": [a]})
    out["upload_refused"] = call("browser_upload", {"tab": up2, "ref": R['button "Many"'],
                                                    "paths": [files["refused"]["ssh key"]]})
    out["return"] = call("browser_return", {"tab": form.tab, "show": True})
    out["show"] = call("browser_show", {"path": str(report)})
    out["downloads"] = call("browser_downloads", {})
    out["wait"] = call("browser_downloads", {"wait": first["download"], "timeout": 5})
    errors = {k: v[1] for k, v in out.items() if v[1] and k != "upload_refused"}
    check("every new hand runs against Flux (open in the agent's window, borrow, tabs, upload, "
          "return, show, downloads and a wait)", not errors and handy is not None, f"{errors}")
    texts = {k: v[0] for k, v in out.items()}
    check("... and says what happened: whose window, where a tab came from and went, the files, "
          "the download's path",
          "in your own window" in texts["open"] and "Borrowed into your own window" in texts["borrow"]
          and "(your own window" in texts["tabs"]
          and "borrowed from window" in texts["tabs"]
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

    client.run(flux.close())
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
    from indigoshell.applink import bar as B
    from indigoshell.applink.service import AppService, AppWindow
    from flux import bar as fb
    from indigoshell.core import persona
    from indigoshell.widgets.base import Size

    out = {}

    class Line:
        pid = os.getpid()

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

    service = AppService(path="/nonexistent")
    lilac = np.array([int(persona.color()[i:i + 2], 16) for i in (1, 3, 5)])
    state = fb.state([fb.tab("Alpha", "x.org", False, id_=1)], 0, 100, True,
                     url="https://x.org/", bookmarked=False)
    for name, wid, agent in (("hers", 1, True), ("users", 2, False)):
        j = AppWindow(Line(service), B.announce("flux", wid))
        j.content.attach(Host())
        j.on_message(fb.bar_of(state, agent=agent))
        img = paint(j.content, 900, theme.TOP_BAR_HEIGHT)
        first = j.view.item("tabs").layout()[0]["x"]
        lead = img[:, :int(first)]
        out[name] = {"first_tab_x": first,
                     "lilac_before_tabs": int((np.abs(lead - lilac).max(-1) <= 10).sum()),
                     "owner": j.view.item("owner").layout()[0]["text"]
                     if j.view.item("owner") else None}
    j = AppWindow(Line(service), B.announce("flux", 3))
    service.place(j, {"type": "place"})
    out["no_wm"] = j.link.out[-1] if j.link.out else None
    print(json.dumps(out))


def test_shell_side() -> None:
    section("3. the shell's side (a child process): the top bar, placing")
    proc = subprocess.run([PY, "-B", __file__, "--shell-side"], capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0:
        check("the shell-side child ran", False, proc.stderr.strip()[-400:])
        return
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    check("the top bar for the agent's own window: \"Lilac\" first, in Lilac's colour, the tabs "
          "after it; the user's window as ever",
          out["hers"]["owner"] == "Lilac"
          and out["hers"]["lilac_before_tabs"] > 30 and out["users"]["owner"] is None
          and out["users"]["lilac_before_tabs"] == 0
          and out["hers"]["first_tab_x"] > out["users"]["first_tab_x"] + 20,
          f"{out['hers']} {out['users']}")
    check("place with no window manager to place it with: answered placed null, saying why",
          out["no_wm"] == {"type": "placed", "desktop": None,
                           "why": "no window manager to place it with"}, f"{out['no_wm']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--shell-side", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.shell_side:
        shell_side()
        return 0
    global PORT, SCRATCH, HOME
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
