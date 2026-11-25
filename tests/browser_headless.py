"""Flux headless, its profiles, and its hands without the shell: what a
Raspberry Pi with no display runs to lend the agent a browser of its own.

    .venv/bin/python tests/browser_headless.py      about a minute, nothing on screen

This process plays the Pi's device process: before anything of the
shell's is imported, xcffib, skia, dbus_fast and psutil are blocked (None
in sys.modules), and PySide6 is never imported here. The agent's Hand,
HandError and Call come from agent/hands.py loaded by its path, as the
Pi gets them once indigoshell.agent imports without the shell's toolkit
(the package's __init__ pulls in skia). Every Flux runs as a process of
its own (`main.py browser ...`) with no $DISPLAY and no $WAYLAND_DISPLAY,
in scratch XDG directories, with sockets of the test's; a stand-in shell
listens where the shell's apps connect, and counts them.

  1. pure: the default profile's places exactly as before (storage,
     cache, history, bookmarks, log, socket and lock); a named profile's
     beside them; names refused; the start command (`--headless`, the
     profile, this Python and the repository's main.py); no display
     means headless; the upload check refuses every profile's cache; the
     imports (none of the blocked modules, no PySide6, no indigoshell
     .agent); the hands made of core's Hand, raising its HandError, the
     same shapes as the laptop's, their headless words (no laptop, no
     workspace, no X window), the laptop's words kept.
  2. a headless Flux, `--headless --no-window --profile pi`, through
     browser_hands(profile="pi", headless=True): tabs (headless, no
     windows, then no X window and no focus); open; read (outline refs);
     act (click, type, fill) and the page changing; a screenshot drawn
     (the page's colour in the PNG) whose viewport never shrinks (no
     fallback line, after more than the link's FALLBACK_MS); look
     (headless words, the screenshot, no image under a pointer); a dialog
     outside the agent's session answered at once (the page not stuck);
     one inside it held for browser_dialog; a permission request denied;
     borrow and return (the agent's window shown at once, no placing, no
     workspace words); downloads (into the scratch Downloads, waited
     for); the stand-in shell never connected to; its last tab closed and
     Flux still there; its log in browser-pi.log; a quit request ends it
     cleanly (answered first, then the process gone, its socket too).
  3. two profiles side by side: the default profile's headless Flux and
     pi's, each on its own socket, its own tabs, storage, cache, history
     and log; `main.py browser --profile pi URL` handed to pi's, not the
     default one's.
  4. `ensure_running(profile="auto")` with no display and no systemd
     (PATH holding only setsid): started detached with plain setsid
     (its own session, not the test's child, in no app scope), headless,
     this Python and main.py; connected; then ended with SIGTERM.
  5. the Qt 6.8 profile path (no QWebEngineProfileBuilder: a child with
     it hidden): the same storage, cache, storage name and policies as
     the builder's, and a cookie set in one run there in the next.

Never touched: the user's profile, history, bookmarks, Downloads, browser
and shell; no window is shown anywhere, no input reaches the display.
"""

import sys

# The Pi's device process has PySide6 and none of these: blocked before
# anything of the shell's is imported.
BLOCKED = ("xcffib", "skia", "dbus_fast", "psutil")
for _name in BLOCKED:
    sys.modules[_name] = None

import argparse                 # noqa: E402
import asyncio                  # noqa: E402
import base64                   # noqa: E402
import json                     # noqa: E402
import os                       # noqa: E402
import re                       # noqa: E402
import shutil                   # noqa: E402
import signal                   # noqa: E402
import socket                   # noqa: E402
import subprocess               # noqa: E402
import tempfile                 # noqa: E402
import threading                # noqa: E402
import time                     # noqa: E402
import traceback                # noqa: E402
import zlib                     # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer     # noqa: E402
from pathlib import Path        # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, env          # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir        # noqa: E402

PY = sys.executable
FAILED: list[str] = []
SCRATCH = Path("/nonexistent")          # made in main
TAG = f"{APP}-hl-{os.getpid()}"
SOCK = {name: os.path.join(runtime_dir(), f"{TAG}-{name}.sock") for name in ("browser", "apps")}
PORT = 0
SHELL_SEEN: list[str] = []              # connections the stand-in shell took
PROCS: list[subprocess.Popen] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def url(path: str) -> str:
    return f"http://127.0.0.1:{PORT}{path}"


# ── a local site ────────────────────────────────────────────────────────
GREEN = (10, 200, 30)
PAGES = {
    "/form": f"""<!doctype html><title>Form</title>
<body style="margin:0;background:rgb{GREEN}">
<p id=said>nothing yet</p>
<button id=press onclick="document.getElementById('said').textContent='pressed'">Press me</button>
<input id=name aria-label="Name">
<input id=city aria-label="City">
<a href="/file.txt" download>Get the file</a>
<button id=ask onclick="document.getElementById('said').textContent = confirm('Sure?') ? 'yes' : 'no'">Ask me</button>
<button id=notify onclick="Notification.requestPermission().then(r => document.getElementById('said').textContent = 'notify ' + r)">Notify me</button>
</body>""",
    "/alert": """<!doctype html><title>Alerting</title><body><p id=p>before</p>
<script>alert('nobody sees this'); document.getElementById('p').textContent = 'after the alert';</script>""",
}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in PAGES:
            self._send(200, PAGES[path].encode(), "text/html; charset=utf-8")
        elif path.startswith("/page/"):
            title = path[len("/page/"):]
            self._send(200, f"<!doctype html><title>{title}</title><body><p>{title} page</p>"
                       .encode(), "text/html")
        elif path == "/file.txt":
            body = b"the downloaded file\n" * 100
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition": 'attachment; filename="flux-headless.txt"'})
        elif path == "/setcookie":
            self._send(200, b"<!doctype html><title>Set</title><body>set", "text/html",
                       {"Set-Cookie": "signed=in-68; Max-Age=86400; Path=/"})
        elif path == "/cookie":
            got = self.headers.get("Cookie") or "none"
            self._send(200, f"<!doctype html><title>{got}</title><body>{got}".encode(),
                       "text/html")
        else:
            self._send(404, b"no", "text/plain")

    def _send(self, code: int, body: bytes, kind: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                        # a favicon Chromium stopped waiting for


class StandInShell:
    """Where the shell's apps connect (applink): it takes connections
    and remembers them; a headless Flux must never come."""

    def __init__(self, path: str) -> None:
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            conn.settimeout(1.0)
            try:
                SHELL_SEEN.append(conn.recv(4096).decode(errors="replace"))
            except OSError:
                SHELL_SEEN.append("")
            conn.close()

    def close(self) -> None:
        self.running = False
        self.sock.close()


# ── a PNG's pixels, without Qt ──────────────────────────────────────────
def png_pixel(data: bytes, fx: float, fy: float) -> tuple:
    """The RGB of the pixel at (fx, fy), fractions of the width and
    height, of an 8-bit RGB or RGBA PNG."""
    pos, chunks = 8, []
    width = height = 0
    channels = 3
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            width, height = int.from_bytes(body[0:4], "big"), int.from_bytes(body[4:8], "big")
            channels = {2: 3, 6: 4}[body[9]]
        elif kind == b"IDAT":
            chunks.append(body)
        pos += 12 + n
    raw = zlib.decompress(b"".join(chunks))
    stride = width * channels
    prev = bytearray(stride)
    want = int(fy * (height - 1))
    for y in range(want + 1):
        line = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        f, cur = line[0], bytearray(line[1:])
        for i in range(stride):
            a = cur[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if f == 1:
                cur[i] = (cur[i] + a) & 255
            elif f == 2:
                cur[i] = (cur[i] + b) & 255
            elif f == 3:
                cur[i] = (cur[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = cur
    x = int(fx * (width - 1)) * channels
    return tuple(prev[x:x + 3])


def near(a: tuple, b: tuple, tol: int = 6) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


# ── what a hand is, as the Pi gets it ──────────────────────────────────
def agent_hands():
    """core/hands.py: Hand, Call, HandError (pure)."""
    from indigoshell.core import hands
    return hands


# ── Flux processes ──────────────────────────────────────────────────────
def start_flux(*args: str) -> subprocess.Popen:
    proc = subprocess.Popen([PY, "-B", str(REPO / "main.py"), "browser", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=dict(os.environ))
    PROCS.append(proc)
    return proc


def wait_listening(profile: str | None, timeout: float = 45.0) -> bool:
    from flux import control

    async def go():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                c = await control.connect(profile=profile)
            except control.NotRunning:
                await asyncio.sleep(0.1)
                continue
            await c.close()
            return True
        return False
    return asyncio.run(go())


def stop(proc_or_pid, timeout: float = 20.0) -> int | None:
    """SIGTERM, then the exit code (a Popen) or None (a pid not ours)."""
    if isinstance(proc_or_pid, subprocess.Popen):
        proc_or_pid.terminate()
        try:
            return proc_or_pid.wait(timeout)
        except subprocess.TimeoutExpired:
            proc_or_pid.kill()
            return proc_or_pid.wait(5)
    pid = proc_or_pid
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            break
        time.sleep(0.1)
    return None


def log_of(profile: str | None) -> str:
    from flux import profiles
    path = Path(profiles.log_path(profile))
    return path.read_text(errors="replace") if path.exists() else ""


# ── 1. pure ─────────────────────────────────────────────────────────────
def test_pure() -> None:
    section("1. pure: profiles, the start command, the imports, the hands")
    from flux import control, profiles, protocol, uploads
    from indigoshell.core.paths import cache_dir, config_dir, data_dir, state_dir

    saved = os.environ.pop(env("BROWSER_SOCKET"))
    try:
        default_sock = protocol.instance_socket_path()
        pi_sock = protocol.instance_socket_path("pi")
        default_lock = protocol.instance_lock_path()
    finally:
        os.environ[env("BROWSER_SOCKET")] = saved
    from flux import places
    from flux.qt import logs
    before = {"storage": os.path.join(data_dir(), "browser"),
              "cache": os.path.join(cache_dir(), "browser"),
              "history": str(Path(data_dir()) / "browser-history.sqlite"),
              "bookmarks": str(Path(config_dir()) / "browser" / "bookmarks.json"),
              "log": os.path.join(state_dir(), "browser.log"),
              "socket": os.path.join(runtime_dir(), f"{APP}-browser-{os.getuid()}.sock"),
              "lock": os.path.join(runtime_dir(), f"{APP}-browser-{os.getuid()}.sock.lock")}
    now = {"storage": profiles.storage_path(), "cache": profiles.cache_path(),
           "history": str(places.history_path()), "bookmarks": str(places.bookmarks_path()),
           "log": logs.path(), "socket": default_sock, "lock": default_lock}
    check("the default profile's places are exactly as before", now == before,
          "" if now == before else f"{now} != {before}")
    named = {"storage": profiles.storage_path("pi"), "cache": profiles.cache_path("pi"),
             "history": str(places.history_path("pi")), "bookmarks": str(places.bookmarks_path("pi")),
             "log": logs.path("pi"), "socket": pi_sock}
    want = {"storage": os.path.join(data_dir(), "browser-pi"),
            "cache": os.path.join(cache_dir(), "browser-pi"),
            "history": os.path.join(data_dir(), "browser-pi-history.sqlite"),
            "bookmarks": os.path.join(config_dir(), "browser", "bookmarks-pi.json"),
            "log": os.path.join(state_dir(), "browser-pi.log"),
            "socket": os.path.join(runtime_dir(), f"{APP}-browser-{os.getuid()}-pi.sock")}
    check("a named profile's places beside them", named == want, "" if named == want
          else f"{named} != {want}")
    check("... and the test's socket gets the name too",
          protocol.control_socket_path("pi") == SOCK["browser"][:-5] + "-pi.sock")
    refused = []
    for bad in ("Pi", "../x", "a b", "-x", "x" * 33, "pi.sock", "ü"):
        try:
            profiles.check(bad)
        except ValueError:
            refused.append(bad)
    check("names that are no profile's refused", len(refused) == 7, f"{refused}")
    check("an empty name is the default", profiles.check("") is None and profiles.check(None) is None)

    cmd = control.start_command("pi", headless=True)
    check("start command: this Python, the repository's main.py, `browser --no-window "
          "--headless --profile pi`",
          cmd == [PY, str(REPO / "main.py"), "browser", "--no-window", "--headless",
                  "--profile", "pi"], f"{cmd}")
    check("... and the default one as it was",
          control.start_command() == [PY, str(REPO / "main.py"), "browser", "--no-window"])
    other = "/usr/bin/python3"
    pinned = control.start_command("pi", headless=True, python=other)
    check("start command with python=: that Python runs the repository's main.py",
          pinned == [other, str(REPO / "main.py"), "browser", "--no-window", "--headless",
                     "--profile", "pi"], f"{pinned}")
    os.environ[control.PYTHON_ENV] = other
    try:
        from_env = control.start_command("pi", headless=True)
    finally:
        del os.environ[control.PYTHON_ENV]
    check(f"... and from ${control.PYTHON_ENV}", from_env == pinned, f"{from_env}")
    check("... unset again: this Python", control.start_command("pi", headless=True) == cmd)
    check("no $DISPLAY nor $WAYLAND_DISPLAY: no display (headless)", control.no_display())

    cache = Path(profiles.cache_path("pi"))
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "secret.bin").write_bytes(b"x")
    try:
        uploads.check([str(cache / "secret.bin")])
        ok = False
    except ValueError as e:
        ok = "Flux's cache" in str(e)
    check("uploads: a named profile's cache refused", ok)

    from flux import hands as browser_hands_module
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED and sys.modules[m])
    check("imports: browser.control and browser.hands with xcffib, skia, dbus_fast and psutil "
          "blocked", not loaded, f"{loaded}")
    check("... no PySide6 in this process", "PySide6" not in sys.modules)
    check("... nor the agent package", not any(m.startswith(f"{APP}.agent") for m in sys.modules),
          f"{[m for m in sys.modules if m.startswith(f'{APP}.agent')]}")

    ah = agent_hands()
    hl = browser_hands_module.browser_hands("pi", headless=True)
    check("hands: made of core's Hand", all(isinstance(h, ah.Hand) for h in hl) and len(hl) == 24,
          f"{len(hl)}")
    shapes = [(h.name, json.dumps(h.input, sort_keys=True), h.timeout) for h in hl]
    try:
        laptop = browser_hands_module.browser_hands()
    except Exception as e:      # noqa: BLE001
        laptop = []
        print(f"    {e}")
    check("... the same shapes as the laptop's (names, schemas, times)",
          shapes == [(h.name, json.dumps(h.input, sort_keys=True), h.timeout) for h in laptop])
    words = {h.name: h.description for h in hl}
    bad = {n: w for n, d in words.items()
           for w in ("laptop", "workspace", "X window id", "under the mouse pointer",
                     "The user watches you act") if w in d}
    check("... headless words: no laptop, workspace, X window, pointer or watching", not bad,
          f"{bad}")
    check("... and they say headless", all("headless" in d for d in words.values()))
    lap = {h.name: h.description for h in laptop}
    check("... the laptop's words as they were", bool(lap) and all(
        "on the laptop" in d for d in lap.values()) and "windows and focus hands" in lap["browser_tabs"]
        and "empty workspace" in lap["browser_borrow"])

    async def bad_tab():
        try:
            await hl[[h.name for h in hl].index("browser_switch")].run(
                ah.Call("c1", "browser_switch", {}, 10.0))
        except ah.HandError as e:
            return f"HandError: {e}"
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        return "no error"
    said = asyncio.run(bad_tab())
    check("... a hand's error is core's HandError", said.startswith("HandError: "), said)
    check("... still none of the blocked modules, nor the agent package", not any(
        m.split(".")[0] in BLOCKED or m.startswith(f"{APP}.agent") for m in sys.modules
        if sys.modules[m]))


# ── 2. a headless Flux through the hands ────────────────────────────────
def test_headless() -> None:
    section("2. a headless Flux (--headless --no-window --profile pi) through the hands")
    from flux import control, protocol
    from flux import hands as bh
    # link.py's, read from its source: importing it would load PySide6 here.
    FALLBACK_MS = int(re.search(r"^FALLBACK_MS = (\d+)", (REPO / "flux/qt/link.py")
                                .read_text(), re.M).group(1))
    ah = agent_hands()
    seen_before = len(SHELL_SEEN)
    proc = start_flux("--headless", "--no-window", "--profile", "pi")
    if not check("it listens on pi's socket", wait_listening("pi")):
        stop(proc)
        return
    hands = {h.name: h for h in bh.browser_hands("pi", headless=True)}

    async def run() -> dict:
        out: dict = {}

        async def call(name: str, args: dict, timeout: float = 60.0) -> tuple:
            try:
                blocks = await hands[name].run(ah.Call(f"c-{name}", name, args, timeout,
                                                       voice="lilac"))
            except ah.HandError as e:
                return "", [], f"HandError: {e}"
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            images = [base64.b64decode(b["data"]) for b in blocks if b["type"] == "image"]
            return text, images, None

        out["tabs0"] = await call("browser_tabs", {})
        out["open"] = await call("browser_open", {"url": url("/form")})
        out["tabs1"] = await call("browser_tabs", {})
        tab = int(out["open"][0].split("tab ", 1)[1].split()[0])
        out["tab"] = tab
        out["outline"] = await call("browser_read", {"tab": tab, "what": "outline"})
        refs = {}
        for line in out["outline"][0].splitlines():
            line = line.strip()
            if line.startswith("[") and "]" in line:
                refs[line.split("]", 1)[1].strip()] = int(line[1:line.index("]")])
        out["refs"] = refs
        press = next((r for k, r in refs.items() if k.startswith('button "Press me"')), None)
        name = next((r for k, r in refs.items() if k.startswith('textbox "Name"')), None)
        city = next((r for k, r in refs.items() if k.startswith('textbox "City"')), None)
        out["click"] = await call("browser_click", {"tab": tab, "ref": press})
        out["type"] = await call("browser_type", {"tab": tab, "ref": name, "text": "Ayşe"})
        out["fill"] = await call("browser_fill", {"tab": tab, "fields": [{"ref": city,
                                                                         "value": "İzmir"}]})
        out["values"] = await call("browser_js", {
            "tab": tab, "code": "[document.getElementById('said').textContent, "
                                "document.getElementById('name').value, "
                                "document.getElementById('city').value]"})
        await call("browser_release", {"tab": tab})
        # Past the link's fallback time: no line may have come over the page.
        await asyncio.sleep(FALLBACK_MS / 1000 + 1.0)
        out["shot"] = await call("browser_screenshot", {"tab": tab})
        out["look"] = await call("browser_look", {"screenshot": True, "image": True})
        # A dialog outside a session: answered at once, the page goes on.
        out["alert_open"] = await call("browser_open", {"url": url("/alert")})
        alert_tab = int(out["alert_open"][0].split("tab ", 1)[1].split()[0])
        out["alert_text"] = await call("browser_read", {"tab": alert_tab, "what": "text"})
        await call("browser_close", {"tab": alert_tab})
        await call("browser_switch", {"tab": tab})
        # One inside the session: held for browser_dialog.
        ask = next((r for k, r in refs.items() if k.startswith('button "Ask me"')), None)
        out["ask"] = await call("browser_click", {"tab": tab, "ref": ask})
        out["answer"] = await call("browser_dialog", {"tab": tab, "accept": True})
        notify = next((r for k, r in refs.items() if k.startswith('button "Notify me"')), None)
        out["notify"] = await call("browser_click", {"tab": tab, "ref": notify})
        await asyncio.sleep(0.5)
        out["said"] = await call("browser_js", {
            "tab": tab, "code": "document.getElementById('said').textContent"})
        # Downloads.
        link = next((r for k, r in refs.items() if k.startswith('link "Get the file"')), None)
        out["dl_click"] = await call("browser_click", {"tab": tab, "ref": link})
        out["downloads"] = await call("browser_downloads", {})
        dl = None
        for line in out["downloads"][0].splitlines():
            if line.startswith("download "):
                dl = int(line.split()[1].rstrip(":"))
                break
        out["dl_wait"] = (await call("browser_downloads", {"wait": dl, "timeout": 20})
                          if dl is not None else ("", [], "no download listed"))
        await call("browser_release", {})
        # Borrow and return: the agent's window at once.
        out["other"] = await call("browser_open", {"url": url("/page/Other"), "background": True})
        other = int(out["other"][0].split("tab ", 1)[1].split()[0])
        began = time.monotonic()
        out["borrow"] = await call("browser_borrow", {"tab": other})
        out["borrow_s"] = time.monotonic() - began
        out["tabs2"] = await call("browser_tabs", {})
        out["return"] = await call("browser_return", {"tab": other})
        # The last tabs closed: Flux stays.
        for t in (other, tab):
            await call("browser_switch", {"tab": t})    # only a tab that shows is closed
            out[f"close{t}"] = await call("browser_close", {"tab": t})
        await asyncio.sleep(0.5)
        out["tabs_end"] = await call("browser_tabs", {})
        return out

    try:
        out = asyncio.run(asyncio.wait_for(run(), 240))
    except Exception:
        print(traceback.format_exc())
        out = {}
    t0, t1 = out.get("tabs0", ("", [], "missing")), out.get("tabs1", ("", [], "missing"))
    check("tabs before any window: headless, no windows",
          "headless (no screen) with no windows" in t0[0], t0[0] or t0[2])
    check("open: the page loaded", "\"Form\"" in out.get("open", ("",))[0],
          out.get("open", ("", [], ""))[0][:200])
    check("tabs: headless, no X window, no focus",
          "headless" in t1[0] and "X window" not in t1[0] and "focus" not in t1[0], t1[0][:300])
    refs = out.get("refs", {})
    check("read: the outline's refs", len(refs) >= 6, f"{list(refs)[:8]}")
    values = out.get("values", ("", [], ""))[0]
    check("act: click, type (Turkish letters) and fill reached the page",
          '"pressed"' in values and "Ayşe" in values and "İzmir" in values, values[-160:])
    text, images, err = out.get("shot", ("", [], "missing"))
    px = png_pixel(images[0], 0.5, 0.9) if images else ()
    check("screenshot: drawn, the page's colour", near(px, GREEN), f"{px} {err or ''}")
    check(f"... its viewport still 1280x800 after {FALLBACK_MS / 1000 + 1:g} s (no fallback line)",
          "viewport is 1280x800" in text, text[:200])
    text, images, err = out.get("look", ("", [], "missing"))
    check("look: headless words, the screenshot, no image under a pointer",
          "headless: no one looks at it" in text and len(images) == 1
          and "No image in the selection." in text and "pointer" not in text, text[:300])
    check("a page's alert outside a session: answered at once, the page went on",
          "after the alert" in out.get("alert_text", ("",))[0], out.get("alert_text", ("",))[0][:200])
    ask, answer = out.get("ask", ("", [], "")), out.get("answer", ("", [], ""))
    check("a confirm inside the session: held for browser_dialog, then answered",
          "confirm dialog" in ask[0] and answer[2] is None, f"{ask[0][:160]} | {answer}")
    said = out.get("said", ("",))[0]
    check("the confirm answered yes, the permission request denied (no one to ask)",
          "notify denied" in said or "\"notify denied\"" in said, said[-120:])
    dl = out.get("dl_wait", ("", [], "missing"))
    files = list((SCRATCH / "downloads").glob("flux-headless*.txt"))
    check("downloads: listed, waited for, in the scratch Downloads",
          "[done" in dl[0] and files and files[0].stat().st_size == 2000, f"{dl[0][:200]} {dl[2]}")
    borrow = out.get("borrow", ("", [], "missing"))
    check("borrow: into the agent's window at once (no placing)",
          "Borrowed into your own window" in borrow[0] and out.get("borrow_s", 9) < 1.2,
          f"{out.get('borrow_s', 0):.2f} s {borrow[2] or ''}")
    t2 = out.get("tabs2", ("",))[0]
    check("... tabs names it Lilac's own, no workspace", "Lilac's own window: yours" in t2
          and "workspace" not in t2, t2[:400])
    check("return: back to its window", "back to its window" in out.get("return", ("",))[0],
          out.get("return", ("", [], ""))[0][:200])
    check("the last tab closed: Flux still runs, with no windows",
          "with no windows" in out.get("tabs_end", ("",))[0] and proc.poll() is None,
          out.get("tabs_end", ("", [], ""))[0][:120])
    check("the shell's app socket never connected to", len(SHELL_SEEN) == seen_before,
          f"{SHELL_SEEN[seen_before:][:2]}")
    async def release_it() -> str:
        blocks = await hands["browser_release"].run(ah.Call("r1", "browser_release", {}, 10.0))
        return blocks[0]["text"]
    released = asyncio.run(release_it())
    check("browser_release asks pi's Flux, not the default profile's",
          "not running" not in released and "not acting on any tab" in released, released)

    async def quit_it() -> str:
        c = await control.connect(profile="pi")
        try:
            await c.quit()
            return "answered"
        except Exception as e:          # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        finally:
            await c.close()
    said = asyncio.run(quit_it())
    try:
        code = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        code = stop(proc)
    log = log_of("pi")
    check("quit: answered, then ended cleanly, its socket gone",
          said == "answered" and code == 0 and "ending: a quit request" in log
          and "ended cleanly" in log
          and not os.path.exists(protocol.control_socket_path("pi")),
          f"{said}; exit {code}")
    check("its log is browser-pi.log: started headless, with profile pi",
          "profile pi, headless, no window" in log and "permission denied (headless" in log
          and "answered at once (headless" in log, log[:200])
    check("... no line to the shell in it", "connected to the shell" not in log)


# ── 3. two profiles side by side ────────────────────────────────────────
def test_two_profiles() -> None:
    section("3. two profiles side by side: the default one and pi")
    from flux import control, places, profiles
    default = start_flux("--headless", "--no-window")
    pi = start_flux("--headless", "--no-window", "--profile", "pi")
    both = wait_listening(None) and wait_listening("pi")
    if not check("both listen, each on its own socket", both):
        stop(default)
        stop(pi)
        return

    async def run() -> dict:
        out = {}
        async with await control.connect() as d, await control.connect(profile="pi") as p:
            a = await d.open(url("/page/DefaultOnly"))
            b = await p.open(url("/page/PiOnly"))
            await d.wait(a.tab)
            await p.wait(b.tab)
            out["default"] = [t.title for w in await d.tabs() for t in w.tabs]
            out["pi"] = [t.title for w in await p.tabs() for t in w.tabs]
            out["meta"] = ((await d.request("tabs")).get("profile"),
                           (await p.request("tabs")).get("profile"))
        return out

    out = asyncio.run(run())
    check("each its own tabs", out["default"] == ["DefaultOnly"] and out["pi"] == ["PiOnly"],
          f"{out}")
    check("tabs says which profile", out["meta"] == (None, "pi"), f"{out['meta']}")
    r = subprocess.run([PY, "-B", str(REPO / "main.py"), "browser", "--profile", "pi",
                        url("/page/HandedToPi")], env=dict(os.environ), capture_output=True,
                       timeout=60)

    async def after() -> tuple:
        async with await control.connect() as d, await control.connect(profile="pi") as p:
            await asyncio.sleep(1.5)
            return ([t.title for w in await d.tabs() for t in w.tabs],
                    [t.title or t.url for w in await p.tabs() for t in w.tabs])
    dt, pt = asyncio.run(after())
    check("`browser --profile pi URL` handed to pi's Flux, not the default one",
          r.returncode == 0 and dt == ["DefaultOnly"] and len(pt) == 2
          and any("HandedToPi" in t for t in pt), f"rc {r.returncode} {dt} {pt}")
    stop(default)
    stop(pi)
    hist = {p: [v.url for v in places.HistoryReader(places.history_path(p)).candidates("", 50)]
            for p in (None, "pi")}
    check("history: each in its own file",
          any("DefaultOnly" in u for u in hist[None]) and not any("PiOnly" in u for u in hist[None])
          and any("PiOnly" in u for u in hist["pi"]) and not any("DefaultOnly" in u
                                                                  for u in hist["pi"]),
          f"{hist}")
    dirs = {p: (Path(profiles.storage_path(p)).is_dir(), Path(profiles.cache_path(p)).is_dir())
            for p in (None, "pi")}
    check("storage and cache: browser and browser-pi", dirs == {None: (True, True),
                                                               "pi": (True, True)}, f"{dirs}")
    logs = (log_of(None), log_of("pi"))
    check("logs: browser.log and browser-pi.log, each its own browser's",
          "profile in " + profiles.storage_path() in logs[0]
          and "profile in " + profiles.storage_path("pi") in logs[1], "")


# ── 4. ensure_running with no display and no systemd ────────────────────
def test_ensure_running() -> None:
    section("4. ensure_running(profile=\"auto\"): no display, no systemd, plain setsid")
    from flux import control, protocol
    bin_dir = SCRATCH / "bin"
    bin_dir.mkdir(exist_ok=True)
    setsid = shutil.which("setsid")
    (bin_dir / "setsid").symlink_to(setsid)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = str(bin_dir)
    lock = protocol.instance_lock_path("auto")

    def pid_of() -> int | None:
        try:
            pid = int(Path(lock).read_text().split()[0])
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except (OSError, ValueError, IndexError):
            return None
        return pid if SOCK["browser"].encode() in environ else None

    async def run() -> dict:
        out = {}
        began = time.monotonic()
        flux = await control.ensure_running(timeout=60, profile="auto")
        out["s"] = time.monotonic() - began
        out["tabs"] = await flux.request("tabs")
        await flux.close()
        return out

    try:
        out = asyncio.run(run())
    except Exception as e:      # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {e}"}
    finally:
        os.environ["PATH"] = old_path
    pid = pid_of()
    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1] if pid else []
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split() if pid else []
    cgroup = Path(f"/proc/{pid}/cgroup").read_text() if pid else ""
    check("started and connected, headless, its profile",
          out.get("tabs", {}).get("headless") is True and out["tabs"].get("profile") == "auto",
          f"{out.get('error', '')} in {out.get('s', 0):.1f} s")
    check("... this Python and the repository's main.py, `browser --no-window --headless "
          "--profile auto`",
          cmd == [PY.encode(), str(REPO / "main.py").encode(), b"browser", b"--no-window",
                  b"--headless", b"--profile", b"auto"], f"{cmd}")
    check("... detached with plain setsid: its own session, not the test's child, no scope "
          "of its own (the test's cgroup)",
          bool(stat) and int(stat[3]) == pid and int(stat[1]) != os.getpid()
          and cgroup == Path("/proc/self/cgroup").read_text(),
          f"pid {pid} ppid {stat[1] if stat else '?'} sid {stat[3] if stat else '?'} "
          f"{cgroup.strip()[-60:]}")
    if pid:
        stop(pid)
        check("... ended by SIGTERM, cleanly", "ended cleanly" in log_of("auto"))


# ── 5. the Qt 6.8 profile path ──────────────────────────────────────────
QT68 = r"""
import os, sys, json
sys.path.insert(0, sys.argv[1])
from flux.qt import profile as profile_module
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage
mode, target = sys.argv[2], sys.argv[3]
app = QApplication(["qt68"])
out = {}
if mode == "compare":
    a = profile_module.make_profile("viabuilder")
    profile_module.Builder = None
    b = profile_module.make_profile("viaplain")
    for name, p in (("builder", a), ("plain", b)):
        out[name] = [p.storageName(), p.persistentStoragePath(), p.cachePath(),
                     p.persistentCookiesPolicy().name, p.persistentPermissionsPolicy().name,
                     p.isOffTheRecord()]
    print(json.dumps(out)); sys.exit(0)
profile_module.Builder = None
p = profile_module.make_profile("q68")
page = QWebEnginePage(p)
def done(ok):
    page.runJavaScript("document.title", 0, lambda t: (out.update(title=t, ok=ok), app.quit()))
page.loadFinished.connect(done)
page.load(QUrl(target))
QTimer.singleShot(30000, app.quit)
app.exec()
del page
import shiboken6; shiboken6.delete(p)
print(json.dumps(out))
"""


def test_qt68() -> None:
    section("5. the Qt 6.8 profile path (no QWebEngineProfileBuilder)")
    from flux import profiles

    def child(*args: str) -> dict:
        r = subprocess.run([PY, "-B", "-c", QT68, str(REPO), *args], capture_output=True,
                           text=True, timeout=90,
                           env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": r.stderr[-400:]}

    got = child("compare", "")
    b, p = got.get("builder"), got.get("plain")
    same = (b and p and b[3:] == p[3:] and b[0] == "browser-viabuilder"
            and p[0] == "browser-viaplain"
            and b[1] == profiles.storage_path("viabuilder") and p[1] == profiles.storage_path("viaplain")
            and b[2] == profiles.cache_path("viabuilder") and p[2] == profiles.cache_path("viaplain"))
    check("made without the builder: the same storage, cache, name and policies", bool(same),
          f"{got}")
    first = child("cookie", url("/setcookie"))
    second = child("cookie", url("/cookie"))
    check("... a cookie set in one run is sent in the next (sign-ins kept)",
          first.get("ok") and "signed=in-68" in (second.get("title") or ""), f"{first} {second}")


# ── main ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    global SCRATCH, PORT
    SCRATCH = Path(tempfile.mkdtemp(prefix=f"{APP}-headless-test-"))
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "QT_QPA_PLATFORM"):
        os.environ.pop(var, None)       # the Pi has no screen
    os.environ.update({
        "XDG_DATA_HOME": str(SCRATCH / "data"),
        "XDG_CACHE_HOME": str(SCRATCH / "cache"),
        "XDG_CONFIG_HOME": str(SCRATCH / "config"),
        "XDG_STATE_HOME": str(SCRATCH / "state"),
        env("BROWSER_SOCKET"): SOCK["browser"],
        env("APPS_SOCKET"): SOCK["apps"],
        env("BROWSER_WIDEVINE"): "off",
    })
    (SCRATCH / "config").mkdir(parents=True, exist_ok=True)
    (SCRATCH / "downloads").mkdir()
    (SCRATCH / "config" / "user-dirs.dirs").write_text(
        f'XDG_DOWNLOAD_DIR="{SCRATCH / "downloads"}"\n')
    http = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    PORT = http.server_address[1]
    shell = StandInShell(SOCK["apps"])
    print(f"scratch {SCRATCH}", flush=True)
    bookmarks = Path.home() / ".config" / APP / "browser" / "bookmarks.json"
    marks_before = bookmarks.read_bytes() if bookmarks.exists() else None
    t0 = time.monotonic()
    try:
        test_pure()
        test_headless()
        test_two_profiles()
        test_ensure_running()
        test_qt68()
    except Exception:
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    finally:
        for proc in PROCS:
            if proc.poll() is None:
                stop(proc)
        shell.close()
        http.shutdown()
    marks_after = bookmarks.read_bytes() if bookmarks.exists() else None
    check("the user's bookmarks untouched", marks_before == marks_after)
    keep = bool(FAILED) or args.keep
    print(f"\n{time.monotonic() - t0:.0f} s" + (f"; logs kept in {SCRATCH}" if keep else ""))
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    if not keep:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    for path in SOCK.values():
        for leftover in (path, path + ".lock", path[:-5] + "-pi.sock", path[:-5] + "-pi.sock.lock",
                         path[:-5] + "-auto.sock", path[:-5] + "-auto.sock.lock"):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
