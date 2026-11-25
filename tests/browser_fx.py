"""The agent's page effects in Flux (flux/qt/effects.py): the shell's
glitch, her tint and the panels' scanlines over a page she drives.

    .venv/bin/python tests/browser_fx.py              a small floating test window (and the
                                                      agent's, as small) in the bottom-right
                                                      corner, never focused; about a minute
    .venv/bin/python tests/browser_fx.py --off-screen only the parts that need no display

Off screen:
  1. the shaders: a fresh qsb build compared byte for byte with the
     committed .qsb files (skipped, saying so, where there is no qsb);
     their serialization format (9: what Qt 6.8 reads too).
  2. headless (a child process, QT_QPA_PLATFORM=offscreen): an agent's
     click locks the tab and the Watch's frame and mark show as ever,
     no effects are made (PageFx never constructed), and why is said
     once; with the platform check out of the way, the scene graph's
     Software API refuses them too.

On screen (this process, the real display; the test's own windows,
560x380, floating, focus-refusing, in the bottom-right corner; their
pixels read from their own composite pixmaps, never the root):
  3. the triggers, through the control socket as the agent drives:
     a lock starts with the glitch in (about 14 frames in 0.22 s), and
     the click that started it lands only once the glitch is over; the
     next click waits for nothing; a release plays the glitch out over
     the live page, and a click of the user's meanwhile reaches the page;
     a quiet session ends the same way; the user's key during the glitch
     in reverses it from where it is (the request then refused as taken
     over); a switch to another tab and a closed tab vanish with no
     glitch; repeats at random intervals within their range, none while
     the agent's script runs (skipped instead); a tab driven in the
     user's window and borrowed: gone there, its glitch owed until the
     agent's window shows and played then, its end there.
  4. pixels: the tint is her colour at PLATE_TINT, the raster
     the panels' cyan on every third row; the band runs down the page at the
     shell's speed; the glitch tears the page; the Watch's mark and
     frame stay as they are, a band across the mark too; the agent's
     screenshot has none of it; input passes the overlay (not a child
     under the pointer, no focus); no frame between passes or while
     the window is hidden, the scan back once it shows.
  5. falling back to the Watch alone, said once: a QML that fails to
     load, shaders that fail after the overlay's first frame (nothing
     drawn, the page as it was).

Never touched: the user's profile, history, bookmarks, Downloads, browser
and shell; no real input is injected into the display (Qt's own events in
this process only), no popup is shown, no workspace is switched and no
window but the test's own is moved.
"""

import argparse
import asyncio
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, env          # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir        # noqa: E402

PY = sys.executable
FAILED: list[str] = []
SCRATCH = Path("/nonexistent")          # made in main
TAG = f"{APP}-fx-{os.getpid()}"
SOCK = {name: os.path.join(runtime_dir(), f"{TAG}-{name}.sock") for name in ("browser", "apps")}
PORT = 0
WIN_W, WIN_H = 560, 380                 # the test's windows: small, in the corner
NO_REPEATS = (600.0, 600.0)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ── the pages ───────────────────────────────────────────────────────────
WHITE = """<!doctype html><title>White</title>
<body style="margin:0;background:#fff;height:100vh;overflow:hidden;font:15px sans-serif">
<div style="position:absolute;left:12px;top:10px;width:150px;color:#222">Signal locked</div>
<button id=b style="position:absolute;left:20px;top:60px;width:90px;height:30px"
  onclick="this.dataset.n=(+this.dataset.n||0)+1">Press</button>
<script>window.clicks=[];document.addEventListener('mousedown',e=>window.clicks.push(e.isTrusted));
</script>"""
OTHER = """<!doctype html><title>Other</title><body style="margin:0;background:#fff">other</body>"""


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/white": WHITE, "/other": OTHER}.get(urlsplit(self.path).path)
        if body is None:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def url(path: str) -> str:
    return f"http://127.0.0.1:{PORT}{path}"


class FakeShell:
    """A stand-in for the shell's app service: takes a window's line and
    says `attached`, so no window shows its own fallback line (which
    would move the page); `place` goes unanswered (shown after
    PLACE_WAIT_S anyway)."""

    def __init__(self, path: str) -> None:
        import socket
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            self.conns.append(sock)
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        try:
            for line in sock.makefile("rb"):
                if json.loads(line).get("type") == "announce":
                    sock.sendall(b'{"type": "attached"}\n')
        except (OSError, ValueError):
            pass

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
            self.qwait(5)
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

    def __init__(self, name: str):
        import logging
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        file = logging.FileHandler(SCRATCH / name)
        file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(handler)
        root.addHandler(file)
        root.setLevel(logging.WARNING)
        for _name in ("flux", APP):
            logging.getLogger(_name).setLevel(logging.DEBUG)

    def said(self, words: str) -> int:
        return sum(words in r.getMessage() for r in self.records)


def small_corner(browser_app) -> None:
    """The test's windows: the unattended kind (floating, never focused),
    but small, in the bottom-right corner."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    def place(self, window, offset: int = 0) -> None:
        window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        area = QGuiApplication.primaryScreen().availableGeometry()
        window.resize(WIN_W, WIN_H)
        window.move(area.right() - WIN_W - 12 - offset, area.bottom() - WIN_H - 12 - offset)
    browser_app.Browser._out_of_the_way = place


def start_browser(unattended: bool):
    from PySide6.QtTest import QTest

    from flux import control
    from flux.qt import app as browser_app
    from flux.qt import instance, startup
    from flux.qt.profile import make_profile
    small_corner(browser_app)
    startup.apply_flags()
    app = browser_app.make_application()
    profile = make_profile()
    lock = instance.try_lock()
    browser = browser_app.Browser(profile, unattended=unattended)
    browser.listen(SOCK["browser"])
    client = Client(QTest.qWait)
    flux = client.run(control.connect())
    return app, profile, lock, browser, client, flux


def stop_browser(app, profile, lock, browser, client, flux) -> None:
    import shiboken6
    try:
        client.run(flux.close(), 5)
    except Exception:           # noqa: BLE001
        pass
    browser.shutdown()
    shiboken6.delete(browser)
    shiboken6.delete(profile)
    os.close(lock)


# ── 1. the shaders ──────────────────────────────────────────────────────
def test_shaders() -> None:
    from flux.qt import effects
    section("1. the shaders")
    qsb = effects.find_qsb()
    if qsb is None:
        print("  skip a fresh build compared with the committed .qsb: no qsb here "
              "(Qt Shader Tools)", flush=True)
    else:
        stale = effects.shaders_current(qsb)
        check("a fresh qsb --qt6 build of each .frag is byte for byte the committed .qsb",
              not stale, f"stale: {stale}" if stale else f"({qsb})")
    versions = {}
    for name in effects.SOURCES:
        data = (effects.SHADERS / f"{name}.qsb").read_bytes()
        versions[name] = struct.unpack(">i", zlib.decompress(data[4:])[:4])[0]
    check("the .qsb files are serialization format 9 (QShader's in Qt 6.8 as in 6.12)",
          set(versions.values()) == {9}, f"{versions}")


# ── 2. headless (a child process) ───────────────────────────────────────
def headless_part() -> int:
    """Run in a child with QT_QPA_PLATFORM=offscreen."""
    from PySide6.QtTest import QTest

    from flux.qt import effects
    os.environ[env("BROWSER_SOCKET")] = SOCK["browser"]     # this process's own
    os.environ[env("APPS_SOCKET")] = SOCK["apps"]
    logs = Log("browser-headless.log")
    made = []
    real_init = effects.PageFx.__init__

    def counted(self, *a, **k):
        made.append(1)
        real_init(self, *a, **k)
    effects.PageFx.__init__ = counted
    parts = start_browser(unattended=False)
    app, profile, lock, browser, client, flux = parts
    tab = client.run(flux.open(url("/white")))
    client.run(flux.wait(tab.tab, timeout=15), 20)
    w = browser.windows[0]
    done = client.run(flux.click(tab.tab, x=300, y=200))
    client.run(flux.release(tab.tab))
    done = client.run(flux.click(tab.tab, x=300, y=200))     # a second lock
    QTest.qWait(300)
    overlay = browser.watch.overlays.get(w.id)
    check("headless: the agent's click locks the tab, the Watch's frame and mark show",
          done.get("agent") == "driving" and overlay is not None and overlay.shown(),
          f"agent {done.get('agent')}")
    check("... and no page effects were made (PageFx never constructed), why said once",
          not made and not browser.watch.effects
          and (effects.off_reason() or "").startswith("no display (offscreen)")
          and logs.said("page effects are off") == 1,
          f"made {len(made)}, off: {effects.off_reason()!r}, said "
          f"{logs.said('page effects are off')}x over two locks")
    effects._off = None
    effects.HEADLESS = frozenset()
    fx = effects.make(w)
    check("with the platform check out of the way, the Software scene graph refuses them",
          fx is None and "Software" in (effects.off_reason() or ""),
          f"off: {effects.off_reason()!r}")
    client.run(flux.release(tab.tab))
    stop_browser(*parts)
    for path in SOCK.values():
        for leftover in (path, path + ".lock"):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return 0


def test_headless() -> None:
    section("2. headless (a child process, offscreen)")
    out = subprocess.run([PY, __file__, "--headless-part", str(SCRATCH), str(PORT)],
                         capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith(("  ok", "  FAIL"))]
    for line in lines:
        print(line, flush=True)
        if line.startswith("  FAIL"):
            FAILED.append("headless: " + line[7:60])
    check("the headless child ran to its end", out.returncode == 0 and len(lines) == 3,
          f"exit {out.returncode}" + (f": {out.stderr[-400:]}" if out.returncode else ""))


# ── on screen ───────────────────────────────────────────────────────────
class Reader:
    """A connection of the test's own, reading its own windows' pixels
    (their composite pixmaps), as tests/browser_act.py does."""

    def __init__(self) -> None:
        import xcffib
        import xcffib.composite
        self.conn = xcffib.connect()
        self.screen = self.conn.get_setup().roots[0]
        self.composite = self.conn(xcffib.composite.key)
        self.composite.QueryVersion(0, 4).reply()

    def region(self, wid: int, x: int, y: int, w: int, h: int):
        import numpy as np
        import xcffib.xproto as xp
        core = self.conn.core
        try:
            at = core.GetGeometry(wid).reply().border_width
            pix = self.conn.generate_id()
            self.composite.NameWindowPixmap(wid, pix)
            try:
                img = core.GetImage(xp.ImageFormat.ZPixmap, pix, at + x, at + y, w, h,
                                    0xFFFFFFFF).reply()
            finally:
                core.FreePixmap(pix)
                self.conn.flush()
        except (xp.DrawableError, xp.MatchError, xp.WindowError):
            return None
        a = np.frombuffer(bytes(img.data.buf()), np.uint8)[:w * h * 4]
        return a.reshape(h, w, 4)[..., [2, 1, 0]].astype(np.int16)

    def cardinal(self, wid: int, name: str) -> int | None:
        import xcffib.xproto as xp
        atom = self.conn.core.InternAtom(False, len(name), name.encode()).reply().atom
        try:
            r = self.conn.core.GetProperty(False, wid, atom, xp.Atom.CARDINAL, 0, 1).reply()
        except Exception:       # noqa: BLE001
            return None
        return r.value.to_atoms()[0] if r.value_len else None

    def to_current_desktop(self, wid: int) -> None:
        """The test's own window, asked onto the workspace shown when the
        window manager put it elsewhere; the workspaces left as they are."""
        import xcffib.xproto as xp
        current = self.cardinal(self.screen.root, "_NET_CURRENT_DESKTOP")
        mine = self.cardinal(wid, "_NET_WM_DESKTOP")
        if current is None or mine is None or mine in (current, 0xFFFFFFFF):
            return
        atom = self.conn.core.InternAtom(False, 15, b"_NET_WM_DESKTOP").reply().atom
        data = xp.ClientMessageData.synthetic([current, 1, 0, 0, 0], "I" * 5)
        event = xp.ClientMessageEvent.synthetic(32, wid, atom, data)
        self.conn.core.SendEvent(False, self.screen.root,
                                 xp.EventMask.SubstructureRedirect
                                 | xp.EventMask.SubstructureNotify, event.pack())
        self.conn.flush()


def test_on_screen() -> None:
    import numpy as np
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from flux import protocol
    from flux.qt import effects

    logs = Log("browser-on-screen.log")
    stamps: dict[int, list[float]] = {}     # fx id -> its frames' times
    real_make = effects.make

    def make(window, acting=None):
        fx = real_make(window, acting)
        if fx is not None:
            got = stamps.setdefault(id(fx), [])
            fx.widget.quickWindow().afterRendering.connect(lambda: got.append(time.monotonic()))
        return fx
    effects.make = make
    effects.REPEAT_S = NO_REPEATS
    protocol.QUIET_S = 60.0

    shell = FakeShell(SOCK["apps"])
    parts = start_browser(unattended=True)
    app, profile, lock, browser, client, flux = parts
    watch = browser.watch
    J = lambda coro, t=30.0: client.attempt(coro, t)      # noqa: E731
    injected: list[float] = []              # when the agent's clicks reach the page
    real_click = watch.inject.click
    watch.inject.click = lambda *a, **k: (injected.append(time.monotonic()), real_click(*a, **k))[1]

    def wait(pred, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            QTest.qWait(10)
        return bool(pred())

    def events_since(fx, t0: float) -> list[tuple[str, float]]:
        return [(e, t) for e, t in fx.events if t >= t0]

    def first(evs, what):
        return next((t for e, t in evs if e == what), None)

    tab0, err = J(flux.open(url("/white")))
    J(flux.wait(tab0.tab, timeout=15), 20)
    T = tab0.tab
    w = browser.windows[0]
    reader = Reader()
    wid = int(w.winId())
    reader.to_current_desktop(wid)
    QTest.qWait(800)
    check("the stand-in shell has the window (no fallback line to move the page)",
          w.line.isHidden(), "")
    st = w.stack.geometry()
    inner = st.adjusted(2, 2, -2, -2)
    plain = reader.region(wid, inner.x(), inner.y(), inner.width(), inner.height())
    check("on screen: the test's window shows the page (white beside its heading)",
          plain is not None and int(plain[200, 400].min()) >= 250,
          f"{plain[200, 400].tolist() if plain is not None else None}")

    # ── 3. triggers ─────────────────────────────────────────────────────
    section("3. the triggers")
    t0 = time.monotonic()
    future = client.start(flux.click(T, x=300, y=200))
    shots = []
    while not future.done() and time.monotonic() - t0 < 10:
        QTest.qWait(4)
        fx = watch.effects.get(w.id)
        if fx is not None and fx.phase == "in":
            got = reader.region(wid, inner.x(), inner.y(), inner.width(), inner.height())
            if got is not None:
                shots.append(got)
    done = future.result() if future.done() else None
    fx = watch.effects.get(w.id)
    evs = events_since(fx, t0) if fx else []
    lock_t, in_end = first(evs, "lock"), first(evs, "in-end")
    frames = [s for s in stamps.get(id(fx), []) if lock_t and in_end and lock_t <= s <= in_end + 0.002]
    gaps = np.diff(frames) * 1000 if len(frames) > 1 else np.array([0])
    check("the agent's first click locks the tab: effects made for the window, the glitch in "
          "plays (frames over its 0.22 s)",
          done is not None and done.get("agent") == "driving" and fx is not None
          and lock_t is not None and in_end is not None and 0.2 <= in_end - lock_t <= 0.45
          and len(frames) >= 10,
          f"{len(frames)} frames in {(in_end - lock_t) * 1000 if in_end and lock_t else 0:.0f} ms, "
          f"gaps median {np.median(gaps):.1f} max {gaps.max():.1f} ms")
    check("... the window keeps its X window (Qt made no new native window for the overlay)",
          int(w.winId()) == wid, f"{wid:#x} -> {int(w.winId()):#x}")
    check("... and the click lands on the page only once the glitch is over",
          bool(injected) and in_end is not None and injected[0] >= in_end - 0.005
          and injected[0] - lock_t >= effects.SPAWN_S - 0.01,
          f"landed {(injected[0] - lock_t) * 1000 if injected and lock_t else -1:.0f} ms after "
          f"the lock, glitch over at {(in_end - lock_t) * 1000 if in_end and lock_t else -1:.0f}")
    torn = 0.0
    tinted_now = reader.region(wid, inner.x(), inner.y(), inner.width(), inner.height())
    if shots and tinted_now is not None:
        torn = max(float((np.abs(s - plain).max(-1) > 40).mean()) for s in shots)
        torn_vs_tint = max(float((np.abs(s - tinted_now).max(-1) > 40).mean()) for s in shots)
    else:
        torn_vs_tint = 0.0
    check("... its frames, read from the window, tear the page (unlike the page before and "
          "the tinted page after)", torn > 0.02 and torn_vs_tint > 0.02,
          f"{len(shots)} frames read; most changed {torn * 100:.1f}% / {torn_vs_tint * 100:.1f}%")

    n_inj = len(injected)
    t1 = time.monotonic()
    hold = watch.hold_s(w.current_tab())
    done2, err = J(flux.click(T, x=320, y=220))
    check("a click while the lock holds waits for nothing (no glitch running, no new lock)",
          err is None and hold == 0.0 and len(injected) == n_inj + 1
          and not any(e == "lock" for e, _ in events_since(fx, t1)),
          f"hold {hold}, err {err}")

    # ── 4. pixels while it holds ────────────────────────────────────────
    section("4. pixels while the lock holds")
    wait(lambda: fx._y is None and not fx.timer.isActive(), 9)
    line_rgb = effects._premul(effects._lerp(effects.RASTER, effects.HUD_FG_BRIGHT,
                                             effects.RASTER_LIFT), effects.RASTER_ALPHA)
    tinted = np.array(effects.tint_rgb((255, 255, 255)))
    lined = np.array([round(c * 255 + (1 - line_rgb.w()) * v) for c, v in
                      zip((line_rgb.x(), line_rgb.y(), line_rgb.z()), tinted)])
    fy = fx.widget.geometry().y()
    reg = reader.region(wid, inner.x() + 300, inner.y() + 150, 40, 9)
    rows_ok, worst = True, 0
    for i in range(9):
        y = inner.y() + 150 + i - fy
        want = lined if y % effects.RASTER_PITCH == 0 else tinted
        d = int(np.abs(reg[i] - want).max())
        worst = max(worst, d)
        rows_ok &= d <= 2
    check("the tint is her colour at PLATE_TINT over white, the panels' cyan raster on "
          "every third row", reg is not None and rows_ok,
          f"tinted {tinted.tolist()}, lined {lined.tolist()}, read {reg[0, 0].tolist()} "
          f"{reg[1, 0].tolist()} {reg[2, 0].tolist()}, worst {worst}")
    shot, err = J(flux.screenshot(T))
    from PySide6.QtGui import QImage
    simg = QImage()
    import base64
    simg.loadFromData(base64.b64decode(shot["png"]) if shot else b"", "PNG")
    spx = simg.pixelColor(300, 160) if not simg.isNull() else None
    check("the agent's screenshot meanwhile has none of it (the page's white)",
          spx is not None and (spx.red(), spx.green(), spx.blue()) == (255, 255, 255),
          f"{(spx.red(), spx.green(), spx.blue()) if spx else err}")
    proxy = w.current_tab().view.focusProxy()
    pt = QPoint(inner.x() + 250, inner.y() + 200)
    check("input passes the overlay: under the pointer is the page's own widget, the overlay "
          "takes no mouse and no focus, the page's widget keeps it",
          w.childAt(pt) is proxy
          and fx.widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
          and fx.widget.focusPolicy() == Qt.FocusPolicy.NoFocus and w.focusWidget() is proxy,
          f"under the pointer {type(w.childAt(pt)).__name__}")
    overlay = watch.overlays[w.id]
    mg = overlay.mark.geometry()
    mark_on = reader.region(wid, mg.x(), mg.y(), mg.width(), mg.height())
    edge_on = reader.region(wid, st.x(), st.y() + 100, 2, 50)
    fx.widget.hide()
    QTest.qWait(150)
    mark_off = reader.region(wid, mg.x(), mg.y(), mg.width(), mg.height())
    edge_off = reader.region(wid, st.x(), st.y() + 100, 2, 50)
    fx.widget.show()
    h = fx.holo                                       # a band posed across the mark
    for name, value in (("stripTop", 0.0), ("stripH", float(fx.widget.height())),
                        ("texTop", 0.0), ("texH", float(fx.widget.height())),
                        ("head", float(mg.center().y() - fy)), ("scanning", 1.0)):
        h.setProperty(name, value)
    QTest.qWait(150)
    mark_band = reader.region(wid, mg.x(), mg.y(), mg.width(), mg.height())
    beside = reader.region(wid, inner.x() + 4, mg.center().y(), min(60, mg.x() - inner.x() - 8), 1)
    h.setProperty("scanning", 0.0)
    same = (mark_on is not None and np.array_equal(mark_on, mark_off),
            mark_band is not None and np.array_equal(mark_on, mark_band),
            edge_on is not None and np.array_equal(edge_on, edge_off))
    check("the Watch's mark and frame stay as they are: the same pixels with the effects on, "
          "off, and with a band across the mark (white beside it)",
          all(same) and int(np.median(beside[0], axis=0).min()) >= 250,
          f"mark {mg.width()}x{mg.height()} on=off {same[0]}, with the band {same[1]}, frame "
          f"{same[2]}; beside the mark (median) {np.median(beside[0], axis=0).tolist()}")

    # the band runs: two reads 0.35 s apart, its rows found as the white ones
    wait(lambda: fx._y is not None and 40 < fx._y < 120, 9)

    def band_at():
        col = reader.region(wid, inner.x() + 300, inner.y(), 60, inner.height())
        g = np.median(col[..., 1], axis=1)
        rows = [y for y in range(len(g)) if (y + inner.y() - fy) % 3 and g[y] >= 250]
        return (float(np.mean(rows)) if rows else None), fx._y, time.monotonic()
    b1, y1, s1 = band_at()
    QTest.qWait(350)
    b2, y2, s2 = band_at()
    moved = (b2 - b1) if b1 is not None and b2 is not None else None
    want = effects.SCAN_SPEED * (s2 - s1)
    check("the band runs down the page at the shell's 170 px/s, where the shader says it is",
          moved is not None and abs(moved - want) <= 25
          and abs(b1 - y1) <= 14 and abs(b2 - y2) <= 14,
          f"band at {b1} then {b2} (head {y1:.0f}, {y2:.0f}): moved {moved} in "
          f"{(s2 - s1) * 1000:.0f} ms, {want:.0f} expected")

    # no frame between passes; none while hidden; back once shown
    wait(lambda: fx._y is None and not fx.timer.isActive(), 9)
    QTest.qWait(100)                # the frame that cleared the band's strip
    n0 = len(stamps[id(fx)])
    QTest.qWait(1500)
    check("between passes the overlay draws no frame (1.5 s)", len(stamps[id(fx)]) == n0,
          f"{len(stamps[id(fx)]) - n0} frames")
    wait(lambda: fx._y is not None and fx._y < 60, 9)
    w.hide()
    QTest.qWait(200)
    n1 = len(stamps[id(fx)])
    QTest.qWait(1200)
    n2 = len(stamps[id(fx)])
    w.show()
    wait(lambda: w.windowHandle() is not None and w.windowHandle().isExposed(), 3)
    QTest.qWait(500)
    n3 = len(stamps[id(fx)])
    check("a hidden window: no frames while it is (mid-pass, 1.2 s); shown again, the scan "
          "goes on", n2 == n1 and n3 - n2 >= 5, f"hidden {n2 - n1}, after showing {n3 - n2}")

    # ── 3 again: the ends ───────────────────────────────────────────────
    section("3. the triggers: the ends")
    page = w.current_tab().page

    def page_clicks() -> int:
        box = []
        page.runJavaScript("window.clicks.length", 0, box.append)
        wait(lambda: box, 3)
        return int(box[0]) if box else -1
    c0 = page_clicks()
    t2 = time.monotonic()
    J(flux.release(T))
    in_out = fx.phase == "out"
    if in_out:
        QTest.mouseClick(w.windowHandle(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(inner.x() + 200, inner.y() + 250))
    wait(lambda: fx.phase == "off", 3)
    evs = events_since(fx, t2)
    out_frames = [s for s in stamps[id(fx)] if first(evs, "unlock") and s >= first(evs, "unlock")
                  and s <= (first(evs, "out-end") or 0) + 0.002]
    check("a release plays the glitch out over the live page, then nothing",
          first(evs, "unlock") is not None and first(evs, "out-end") is not None
          and len(out_frames) >= 8 and fx.phase == "off" and not fx.widget.isVisible(),
          f"{len(out_frames)} frames, {[e for e, _ in evs]}")
    QTest.qWait(200)
    check("... and a click of the user's during it reaches the page (the overlay takes none)",
          in_out and page_clicks() == c0 + 1, f"clicks {c0} -> {page_clicks()}")

    protocol.QUIET_S = 1.0
    t3 = time.monotonic()
    J(flux.click(T, x=300, y=200))
    wait(lambda: any(e == "out-end" for e, _ in events_since(fx, t3)), 4)
    evs = events_since(fx, t3)
    check("a session gone quiet ends the same way: the glitch out",
          [e for e, _ in evs if e in ("lock", "in-end", "unlock", "out-end")]
          == ["lock", "in-end", "unlock", "out-end"]
          and 0.9 <= first(evs, "unlock") - first(evs, "in-end") <= 1.6,
          f"{[e for e, _ in evs]}")
    protocol.QUIET_S = 60.0

    # take-over during the glitch in: it reverses from where it is
    t4 = time.monotonic()
    future = client.start(flux.click(T, x=300, y=200))
    wait(lambda: fx.phase == "in" and float(fx.glitch.property("appear")) >= 0.35, 5)
    a0 = float(fx.glitch.property("appear"))
    QTest.keyClick(w.windowHandle(), Qt.Key.Key_A)
    a1, phase1 = float(fx.glitch.property("appear")), fx.phase
    trail = []
    while fx.phase == "out" and time.monotonic() - t4 < 3:
        QTest.qWait(2)
        trail.append(float(fx.glitch.property("appear")))
    wait(lambda: future.done(), 5)
    _, err = (future.result(), None) if future.done() and not future.exception() else \
        (None, str(future.exception()) if future.done() else "no answer")
    evs = events_since(fx, t4)
    out_s = (first(evs, "out-end") or 0) - (first(evs, "unlock") or 0)
    check("the user's key during the glitch in reverses it from where it was (no jump) and it "
          "ends sooner, as far as it had come",
          phase1 == "out" and abs(a1 - a0) < 1e-6 and trail and trail[-1] == 0.0
          and all(b <= a + 1e-6 for a, b in zip(trail, trail[1:]))
          and abs(out_s - a0 * effects.SPAWN_S) <= 0.05,
          f"at {a0:.2f}, out over {out_s * 1000:.0f} ms ({a0 * effects.SPAWN_S * 1000:.0f} "
          f"expected)")
    check("... and the click it held is refused as taken over",
          err is not None and protocol.took_over(T) in err, f"{err}")
    watch.resume(w.current_tab())           # the user hands it back

    # a switch to another tab, and a closed tab: gone, no glitch
    other, _ = J(flux.open(url("/other"), background=True))
    J(flux.click(T, x=300, y=200))
    wait(lambda: fx.phase == "holo", 3)
    t5 = time.monotonic()
    n5 = len(stamps[id(fx)])
    w.select(next(i for i, t in enumerate(w.tabs) if t.id == other.tab))
    vanished = fx.phase == "off" and not fx.widget.isVisible()
    QTest.qWait(400)
    evs = events_since(fx, t5)
    check("the user switching to another tab ends the lock and the effects vanish at once "
          "(no glitch out, no frame)",
          vanished and [e for e, _ in evs] == ["vanish"] and len(stamps[id(fx)]) - n5 <= 1,
          f"{[e for e, _ in evs]}, {len(stamps[id(fx)]) - n5} frames")
    J(flux.click(other.tab, x=100, y=100))
    wait(lambda: fx.phase == "holo", 3)
    t6 = time.monotonic()
    w.close_tab(w.current_tab())
    QTest.qWait(300)
    evs = events_since(fx, t6)
    check("a locked tab closed: the effects vanish", fx.phase == "off"
          and [e for e, _ in evs] == ["vanish"], f"{[e for e, _ in evs]}")

    # repeats at random intervals; skipped while a script of the agent's runs
    effects.REPEAT_S = (0.5, 0.8)
    t7 = time.monotonic()
    J(flux.click(T, x=300, y=200))
    QTest.qWait(3600)
    evs = events_since(fx, t7)
    gaps, prev_end = [], None
    for e, t in evs:
        if e == "in-end":
            prev_end = t
        elif e == "repeat" and prev_end is not None:
            gaps.append(t - prev_end)
    reps = sum(e == "repeat" for e, _ in evs)
    check("while the lock holds the glitch comes again at random intervals, each pause within "
          "its range (0.5-0.8 s here)",
          reps >= 3 and gaps and all(0.48 <= g <= 0.9 for g in gaps),
          f"{reps} repeats, pauses {[round(g, 2) for g in gaps]}")
    future = client.start(flux.js(T, "await new Promise(r => setTimeout(r, 2500)); return 1",
                                  timeout=8))
    wait(lambda: watch.acting_on(T), 3)
    js_from = time.monotonic()
    wait(lambda: future.done(), 12)
    js_to = time.monotonic()
    evs = [(e, t) for e, t in fx.events if js_from + 0.25 <= t <= js_to - 0.05]
    check("... none while the agent's script runs on the tab (skipped, the pause drawn again)",
          future.done() and not future.exception()
          and not any(e == "repeat" for e, _ in evs) and sum(e == "skip" for e, _ in evs) >= 2,
          f"{[e for e, _ in evs]}")
    effects.REPEAT_S = NO_REPEATS
    J(flux.release(T))
    wait(lambda: fx.phase == "off", 3)

    # a tab driven in the user's window, then borrowed into the agent's
    J(flux.click(T, x=300, y=200))
    wait(lambda: fx.phase == "holo", 3)
    t8 = time.monotonic()
    got, err = J(flux.borrow(T))
    her = next((x for x in browser.windows if x.agent), None)
    hfx = watch.effects.get(her.id) if her is not None else None
    wait(lambda: hfx is not None and any(e == "in-end" for e, _ in hfx.events), 4)
    if her is not None:
        reader.to_current_desktop(int(her.winId()))
    hev = [e for e, _ in hfx.events] if hfx else []
    check("borrowed while driven: the effects vanish from the user's window, and in the "
          "agent's the lock starts, its glitch owed until the window shows, then played",
          err is None and [e for e, _ in events_since(fx, t8)] == ["vanish"] and hfx is not None
          and hev[:3] == ["lock", "in-skipped", "in-end"] and hfx.phase == "holo",
          f"{err or ''} user's {[e for e, _ in events_since(fx, t8)]}, agent's {hev}")
    J(flux.release(T))
    wait(lambda: hfx is not None and hfx.phase == "off", 3)
    check("... and released there, its glitch out there", hfx is not None
          and [e for e, _ in hfx.events][-2:] == ["unlock", "out-end"],
          f"{[e for e, _ in hfx.events] if hfx else None}")
    J(flux.return_tab(T, show=True))
    QTest.qWait(300)

    # ── 5. falling back ─────────────────────────────────────────────────
    section("5. falling back to the Watch alone")
    broken = SCRATCH / "broken"
    broken.mkdir()
    (broken / "effects.qml").write_text("import QtQuick\nItem { property int x: }\n")
    old = watch.effects.pop(w.id)
    old.close()
    effects._off = None
    effects.QML = broken / "effects.qml"
    said0 = logs.said("page effects are off")
    done, err = J(flux.click(T, x=300, y=200))
    QTest.qWait(300)
    J(flux.click(T, x=310, y=210))
    check("a QML that fails to load: no effects, the lock and the Watch's mark as ever, why "
          "said once (two locks' worth of tries)",
          err is None and done.get("agent") == "driving" and w.id not in watch.effects
          and watch.overlays[w.id].shown() and "effects.qml" in (effects.off_reason() or "")
          and logs.said("page effects are off") == said0 + 1,
          f"off: {effects.off_reason()!r}")
    J(flux.release(T))
    QTest.qWait(300)
    effects.QML = REPO / "indigoshell" / "browser" / "qt" / "effects.qml"
    bad = SCRATCH / "badshaders"
    bad.mkdir()
    for name in effects.SOURCES:
        (bad / f"{name}.qsb").write_bytes(b"not a shader pack")
    effects.SHADERS = bad
    effects._off = None
    done, err = J(flux.click(T, x=300, y=200))
    QTest.qWait(400)
    dead = watch.effects.get(w.id)
    now = reader.region(wid, inner.x(), inner.y(), inner.width(), inner.height())
    away = (slice(130, 260), slice(150, 400))
    check("shaders that fail on the overlay's first frame: the effects go (nothing drawn, the "
          "page as it was), why said once, the lock and the mark as ever",
          err is None and (dead is None or dead.dead) and watch.hold_s(w.current_tab()) == 0.0
          and "shader did not load" in (effects.off_reason() or "")
          and logs.said("page effects are off") == said0 + 2
          and now is not None and int(np.abs(now[away] - plain[away]).max()) <= 3
          and watch.overlays[w.id].shown(),
          f"off: {effects.off_reason()!r}, page diff "
          f"{int(np.abs(now[away] - plain[away]).max()) if now is not None else None}")
    J(flux.release(T))
    effects.SHADERS = REPO / "indigoshell" / "browser" / "qt" / "shaders"
    effects.make = real_make
    stop_browser(*parts)
    shell.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--off-screen", action="store_true",
                        help="only the parts that need no display")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--headless-part", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    global PORT, SCRATCH
    if args.headless_part:
        SCRATCH, PORT = Path(args.headless_part[0]), int(args.headless_part[1])
        try:
            return headless_part()
        except Exception:
            print("  FAIL headless part aborted\n" + traceback.format_exc(), flush=True)
            return 1
    SCRATCH = Path(tempfile.mkdtemp(prefix=f"{APP}-fx-test-"))
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
        test_shaders()
        test_headless()
        if not args.off_screen:
            test_on_screen()
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
