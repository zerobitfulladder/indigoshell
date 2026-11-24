"""The shell's top bar and its line to the shell's own apps
(indigoshell/applink, applink/flux.py), and the shell code under it.

    .venv/bin/python tests/topbar.py              all of it, about a minute;
                                                  a test browser window shows in
                                                  the bottom-right corner
    .venv/bin/python tests/topbar.py --off-screen  1-7 only: nothing shows

1. Display (backend/x11.py) on throwaway windows of a second connection:
   several handlers on one window, event masks merged per subscriber
   and taken back one subscriber at a time, `watch_window_properties`
   before and after another selection, a destroyed window forgotten,
   the root path as it was, errors on foreign windows quiet and the
   rest as loud as before; the workspaces widget's own calls, against a
   window standing in for a client.
2. The Xlib error guard (backend/gl.py), in child processes: an X error
   on EGL's connection is logged once and the process lives; the same
   error kills a process whose guard was taken away (so 1 means
   something); EGL drawing to a window destroyed under it.
3. Flux: the repository's desktop file, its install into a scratch
   data dir, and the launcher's own catalog code finding, showing and
   launching it, and rescanning when it appears.
4. Flux's content for the top bar, painted off screen and read back as
   pixels: the tabs (the current one strong, the rest muted, long titles
   shortened with an ellipsis to fit), the host with the not-secure
   padlock and the bookmark glyph, find while typing (the caret) and
   while stepping (none), "no matches" in the error colour, the load
   line along the bottom edge.
5. The app service on a socket of its own: the announce and the answer
   both ways (Flux; an app kind it does not know; an announce naming no
   kind; windows that are no window; lines that are no message, types
   it does not know), focus followed between two Flux windows, a dialog
   of one of them and a window that is no app's (the context area
   empty), a Flux window in the background changing without repainting
   the bar, a connection lost and back, the service stopped and
   started; the root listener path; the key grabs going with the
   connection and the service, and made again on the next announce,
   checked with X itself (another client's grab refused while the
   service holds one, taken once it is gone).
5b. The keys the shell takes, on real unmapped windows of the test's
   own, the service's X connection wrapped in a recorder (keyboard
   grabs recorded and never sent): GrabKey for Ctrl+L, Ctrl+T, Ctrl+M on
   a main Flux window, by the keycodes the keymap gives (an oracle read
   on another connection), once per combination of Caps Lock and Num
   Lock, asynchronous, and none on a single window; another client's
   grab of one chord refused (the warning once, that chord left to the
   app, the rest grabbed); a press delivered to the handler (the keyboard
   held first, the launcher opened, keys typed after it into the query in
   order, a release ignored); a keymap change (MappingNotify) re-making
   the grabs; the grabs released with the window, not when an older
   connection for the same window goes.
6. Where Flux's launcher drops from: centred on its window, its top at
   the top bar's bottom edge, or at the window's own top edge with no
   bar over it (real unmapped windows of the test's own; no launcher).
7. The top bar's spec, as computed data: anchors, size, layer, the
   strut it would set (never set on a window here), its look against
   the bottom bar's, its place in WINDOWS, a user widget added the
   documented way, the whole bar painted off screen; the user's own
   config.py (when there is one) imported by its path with scratch XDG
   directories and no bytecode written, and the top bar in what it
   exports.
8. On screen: the real `AppService` on an X connection, loop and socket
   of its own, and the real browser (`--unattended`: floating in the
   corner, never focused) on sockets and XDG directories of its own.
   The browser announces itself as a main Flux window, and the service
   grabs its keys (another client's grab of them refused, so X itself
   says so); its state reaches the service; the context area draws it;
   the browser's window has nothing of the shell's in it, no child
   window and no line (its own pixels, from its composite pixmap, never
   the root or the screen: the page's colour from the top edge);
   the service stopped: the grabs gone, no line in the browser's window
   for three seconds, then its own line, gone again when the service is
   back (and the grabs with it). The grab's activation by a real key
   press is not tried: that would need input injected into the display.
   A test window that its window manager puts on a workspace not shown
   is asked back onto the current one (its own window, nobody else's).

Prints ok/FAIL lines, exits 1 if anything failed. Nothing here touches
the running shell: no socket, lock, state or window of its. No window
with a strut is ever mapped (the window manager would move the
desktop's windows for it).
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, CLI, env      # noqa: E402  (pure)
from indigoshell.core.paths import runtime_dir          # noqa: E402

PY = sys.executable
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


class Records(logging.Handler):
    """Every record of the loggers it is put on, from DEBUG up."""

    def __init__(self, *names: str) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        for name in names:
            lg = logging.getLogger(name)
            lg.addHandler(self)
            lg.setLevel(logging.DEBUG)

    def emit(self, record) -> None:
        self.records.append(record)

    def count(self, text: str, level: int = 0, since: int = 0) -> int:
        return sum(1 for r in self.records[since:]
                   if r.levelno >= level and text in r.getMessage())

    def at_least(self, level: int, since: int = 0) -> list[str]:
        return [r.getMessage() for r in self.records[since:] if r.levelno >= level]


# ── 1. Display ──────────────────────────────────────────────────────────

def test_display() -> None:
    import xcffib
    import xcffib.xproto as xp

    from indigoshell.backend.x11 import Display
    from indigoshell.widgets.workspaces import Workspaces

    section("1. Display: handlers and event masks per window")
    rec = Records("indigoshell.backend.x11")
    d = Display()
    other = xcffib.connect()                    # the "other client"
    root = other.get_setup().roots[0]
    P, S = xp.EventMask.PropertyChange, xp.EventMask.StructureNotify
    atom = other.core.InternAtom(False, len(b"_BST_TEST"), b"_BST_TEST").reply().atom

    def window() -> int:
        """Unmapped and override-redirect: no window manager involved,
        nothing on screen."""
        wid = other.generate_id()
        other.core.CreateWindow(root.root_depth, wid, root.root, 0, 0, 50, 50, 0,
                                xp.WindowClass.InputOutput, root.root_visual,
                                xp.CW.OverrideRedirect, [1])
        other.core.GetInputFocus().reply()       # made before `d` names it
        return wid

    def poke(wid: int) -> None:
        other.core.ChangeProperty(xp.PropMode.Replace, wid, atom, xp.Atom.CARDINAL, 32,
                                  1, (1).to_bytes(4, "little"))
        other.flush()

    def resize(wid: int, w: int) -> None:
        other.core.ConfigureWindow(wid, xp.ConfigWindow.Width, [w])
        other.flush()

    def settle() -> None:
        d.conn.core.GetInputFocus().reply()      # the server has done all of ours
        other.core.GetInputFocus().reply()
        time.sleep(0.05)
        d.conn.core.GetInputFocus().reply()
        while d.pump():
            pass

    def mask(wid: int) -> int:
        return d.conn.core.GetWindowAttributes(wid).reply().your_event_mask

    def kinds(log_):
        return [type(e).__name__.replace("Event", "") for e in log_]

    a, b = [], []
    ha, hb = a.append, b.append
    w1 = window()
    d.on(w1, ha)
    d.on(w1, hb)
    d.on(w1, hb)                                 # twice is once
    d.select(w1, P, "a")
    settle()
    poke(w1)
    settle()
    check("two handlers on one window both get its event (one added twice: once)",
          kinds(a) == ["PropertyNotify"] and kinds(b) == ["PropertyNotify"],
          f"a {kinds(a)}, b {kinds(b)}")
    d.select(w1, S, "b")
    settle()
    merged = mask(w1)
    a.clear(), b.clear()
    resize(w1, 60)
    poke(w1)
    settle()
    check("masks merge: PropertyChange (a) | StructureNotify (b), both kinds arrive",
          merged == P | S and set(kinds(a)) == {"ConfigureNotify", "PropertyNotify"},
          f"mask 0x{merged:x}, a got {kinds(a)}")
    d.unselect(w1, "b")
    d.off(w1, hb)
    settle()
    left = mask(w1)
    a.clear(), b.clear()
    resize(w1, 70)
    poke(w1)
    settle()
    check("one taken back: the other's mask and events stay, its own stop",
          left == P and kinds(a) == ["PropertyNotify"] and b == [],
          f"mask 0x{left:x}, a {kinds(a)}, b {kinds(b)}")

    w2, w3 = window(), window()
    d.select(w2, S, "other")
    d.watch_window_properties(w2)
    d.watch_window_properties(w3)
    d.select(w3, S, "other")
    settle()
    check("watch_window_properties after a StructureNotify selection, and before one: both kept",
          mask(w2) == P | S and mask(w3) == P | S, f"0x{mask(w2):x}, 0x{mask(w3):x}")

    c = []
    d.on(w2, c.append)
    other.core.DestroyWindow(w2)
    other.flush()
    settle()
    check("a window destroyed with handlers attached: they hear it, then it is forgotten",
          kinds(c)[-1:] == ["DestroyNotify"] and w2 not in d._handlers and w2 not in d._selected,
          f"heard {kinds(c)}")

    before = (d._root_mask, list(d._handlers.get(d.root, [])))
    got = []
    d.add_root_listener(got.append)
    d.add_root_listener(got.append)
    d._select_root(xp.EventMask.SubstructureNotify)
    settle()
    roots = d._handlers.get(d.root, [])
    check("root path as before: one handler, masks merged in _root_mask, two listeners kept",
          before == (0, []) and roots == [d._on_root_event]
          and d._root_mask == P | xp.EventMask.SubstructureNotify
          and mask(d.root) == d._root_mask and d._root_listeners == [got.append, got.append],
          f"root mask 0x{mask(d.root):x}")
    d.conn.core.ChangeWindowAttributes(d.root, xp.CW.EventMask, [0])    # leave the root be
    d.off(d.root)

    # Errors: on a window another client destroyed, quiet; any other,
    # loud as ever.
    mark = len(rec.records)
    w4 = window()
    other.core.DestroyWindow(w4)
    other.flush()
    settle()
    d.select(w4, S, "late")                      # BadWindow, a foreign window
    own_gone = d.conn.generate_id()               # an id of ours never made
    d.conn.core.MapWindow(own_gone)              # BadWindow, ours: a bug
    d.conn.flush()
    settle()
    quiet = rec.count("X error on window", logging.DEBUG, mark)
    loud = rec.count("X protocol error", logging.WARNING, mark)
    check("X errors: on a foreign window quiet (debug), on ours a warning",
          quiet == 1 and loud == 1, f"quiet {quiet}, warnings {loud}")

    # The workspaces widget's own calls, with the client list pointing at
    # a window of ours: it watches it, someone else here selects more on
    # it, the widget sees only what it asked for, and dropping it as stale
    # leaves the other's handler and mask alone.
    section("1b. the workspaces widget beside another watcher, on one window")
    w5 = window()
    clients = [w5]
    real_get_ints = d.get_ints

    def get_ints(wid, name, length=64):
        if wid == d.root and name == "_NET_CLIENT_LIST":
            return list(clients)
        return real_get_ints(wid, name, length)

    d.get_ints = get_ints
    d._select_root = lambda mask_: None          # no root selection for this part

    class FakeWindow:
        display = d

        def damage(self, *_a, **_k):
            pass

    ws = Workspaces()
    delivered, refreshed = [], []
    real_handler, real_refresh = ws._on_client_property, ws._refresh

    def spy_refresh():
        refreshed.append(1)
        real_refresh()

    ws._refresh = spy_refresh
    ws.attach(FakeWindow())
    other_events = []
    d.select(w5, S, "other")
    d.on(w5, other_events.append)
    settle()
    handlers = d._handlers.get(w5, [])
    m_both = mask(w5)
    # Spy on what reaches the widget's handler, after it registered.
    d._handlers[w5] = [h if h != real_handler else (lambda ev: (delivered.append(ev),
                                                                  real_handler(ev)))
                       for h in handlers]
    refreshed.clear()
    resize(w5, 80)
    poke(w5)
    settle()
    props = sum(isinstance(e, xp.PropertyNotifyEvent) for e in delivered)
    check("watched by the widget and another: mask Property|Structure; both hear both kinds",
          m_both == P | S and len(handlers) == 2
          and {"ConfigureNotify", "PropertyNotify"} <= set(kinds(delivered))
          and {"ConfigureNotify", "PropertyNotify"} <= set(kinds(other_events)),
          f"mask 0x{m_both:x}, widget got {kinds(delivered)}")
    check("the widget refreshes on its PropertyNotify only, not on the other's ConfigureNotify",
          len(refreshed) == props == 1, f"refreshes {len(refreshed)}, property events {props}")
    d._handlers[w5] = handlers                   # spy off
    clients.clear()
    ws._clients_dirty = True
    real_refresh()                               # w5 is stale now
    other_events.clear()
    resize(w5, 90)
    settle()
    check("the widget dropping it as stale leaves the other's handler and mask",
          d._handlers.get(w5) == [other_events.append] and mask(w5) & S
          and kinds(other_events) == ["ConfigureNotify"],
          f"handlers {len(d._handlers.get(w5, []))}, mask 0x{mask(w5):x}")
    d.unselect(w5, "other")
    settle()
    check("... and the other letting go then leaves what the widget had selected",
          mask(w5) == P, f"0x{mask(w5):x}")
    for wid in (w1, w3, w4, w5):
        other.core.DestroyWindow(wid)
    other.flush()
    settle()
    other.disconnect()
    d.close()


# ── 2. the Xlib guard ───────────────────────────────────────────────────

XLIB_CHILD = r"""
import ctypes, logging, sys, time
sys.path.insert(0, sys.argv[1])
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
for noisy in ("indigoshell.backend.x11",):
    logging.getLogger(noisy).setLevel(logging.WARNING)
from indigoshell.backend.x11 import Display, xp
from indigoshell.backend import gl
mode = sys.argv[2]
d = Display()
b = gl.backend(d)
x11 = b.lib.x11
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XCreateSimpleWindow.restype = ctypes.c_ulong
x11.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_int] * 2 \
    + [ctypes.c_uint] * 3 + [ctypes.c_ulong] * 2
for name in ("XDestroyWindow", "XMapWindow"):
    getattr(x11, name).argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
if mode == "unguarded":
    x11.XSetErrorHandler(None)          # Xlib's own handler again
if mode in ("guarded", "unguarded"):
    # A window of this connection's own, made and destroyed: mapping it
    # then is a BadWindow that touches nobody else's window.
    w = x11.XCreateSimpleWindow(b.xdpy, x11.XDefaultRootWindow(b.xdpy), 0, 0, 10, 10, 0, 0, 0)
    x11.XDestroyWindow(b.xdpy, w)
    x11.XSync(b.xdpy, 0)
    x11.XMapWindow(b.xdpy, w)
    x11.XSync(b.xdpy, 0)
else:
    # EGL drawing into a window destroyed under it: made on the xcb
    # connection, its surface on EGL's, then destroyed on xcb.
    conn = d.conn
    wid = conn.generate_id()
    cmap = conn.generate_id()
    conn.core.CreateColormap(xp.ColormapAlloc._None, cmap, d.root, b.visual_id)
    conn.core.CreateWindow(32, wid, d.root, -100, -100, 64, 16, 0, xp.WindowClass.InputOutput,
                           b.visual_id, xp.CW.BackPixel | xp.CW.BorderPixel
                           | xp.CW.OverrideRedirect | xp.CW.Colormap, [0, 0, 1, cmap])
    conn.core.GetInputFocus().reply()
    s = b.create(wid, 64, 16)
    b.make_current(s)
    s.skia_surface.getCanvas().clear(0xFF00FF00)
    s.swap()
    conn.core.DestroyWindow(wid)
    conn.core.GetInputFocus().reply()
    time.sleep(0.1)
    bound = b.make_current(s)
    s.skia_surface.getCanvas().clear(0xFFFF0000)
    s.swap()
    b.make_idle()
    s.destroy()
    x11.XSync(b.xdpy, 0)
    print("make_current on the dead window:", bound)
print("survived; Xlib errors", gl.xlib_errors, flush=True)
"""


def test_xlib_guard(scratch: Path) -> None:
    section("2. the Xlib error guard on EGL's connection (child processes)")
    script = scratch / "xlib_child.py"
    script.write_text(XLIB_CHILD)

    def run(mode):
        p = subprocess.run([PY, str(script), str(REPO), mode], capture_output=True,
                           text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    code, out = run("guarded")
    warned = [line for line in out.splitlines()
              if "Xlib error" in line and not line.startswith("survived")]
    check("guarded: a BadWindow on EGL's Xlib connection is logged once, the process lives",
          code == 0 and "survived; Xlib errors 1" in out and len(warned) == 1
          and warned[0].startswith("WARNING"), f"exit {code}; {warned}")
    code, out = run("unguarded")
    check("control, the guard taken away: the same error ends the process (Xlib's handler)",
          code != 0 and "survived" not in out and "BadWindow" in out,
          f"exit {code}; {[ln for ln in out.splitlines() if 'Error' in ln][:1]}")
    code, out = run("egl")
    lines = [ln for ln in out.splitlines() if "Xlib error" in ln or "survived" in ln
             or "make_current" in ln]
    check("EGL drawing to a window destroyed under it: the process lives",
          code == 0 and any("survived" in ln for ln in lines), f"exit {code}; {lines}")


# ── 3. Flux ─────────────────────────────────────────────────────────────

def test_desktop(scratch: Path) -> None:
    from flux import desktop
    from indigoshell.services import apps, desktop_entry
    from indigoshell.services.icons import Icons

    section("3. Flux: the desktop file, its install, the launcher's catalog")
    text = Path(desktop.TEMPLATE).read_text()
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    home = os.path.expanduser("~")
    check("repository copy: Name=Flux, Exec through the CLI, the window class, a stock icon",
          values.get("Name") == "Flux" and values.get("Exec") == f"{CLI} browser %U"
          and values.get("StartupWMClass") == desktop.WM_CLASS == f"{APP}-browser"
          and values.get("Icon") == "web-browser" and values.get("Terminal") == "false"
          and values.get("Categories") == "Network;WebBrowser;", f"Exec={values.get('Exec')}")
    mimes = set(filter(None, values.get("MimeType", "").split(";")))
    check("... the web MIME types, and no path or user anywhere in it",
          {"text/html", "x-scheme-handler/http", "x-scheme-handler/https"} <= mimes
          and "/" not in values.get("Exec", "") and home not in text
          and os.environ.get("USER", "\0") not in text, str(sorted(mimes)))

    data = scratch / "xdg-data"
    t0 = time.time()
    os.environ["XDG_DATA_HOME"] = str(data)
    os.environ["XDG_DATA_DIRS"] = str(scratch / "no-system-apps")
    catalog = apps.Catalog()
    catalog.refresh()
    before = [a.entry.name for a in catalog.apps]
    path = desktop.install(data_home=str(data))
    installed = Path(path).read_text()
    entry = desktop_entry.parse(path, os.path.basename(path), desktop_entry.locales())
    argv = desktop_entry.command(entry) if entry else []
    want = desktop.exec_command() + ["browser"]
    check(f"installed as {desktop.DESKTOP_ID}, Exec = the command that ran the install",
          os.path.basename(path) == f"{APP}-browser.desktop" and argv == want,
          f"{argv}")
    check("installed without MimeType (the default browser stays as it is)",
          "MimeType" not in installed and not (data / "applications" / "mimeinfo.cache").exists())
    os.utime(data / "applications", (t0 + 2, t0 + 2))   # mtime granularity
    rescanned = catalog.refresh()
    names = [a.entry.name for a in catalog.apps]
    check("the catalog rescans on its next open and shows Flux (visible(): its program runs)",
          before == [] and rescanned and names == ["Flux"]
          and desktop_entry.visible(entry, desktop_entry.current_desktops()), f"{names}")
    hits = [r.app.entry.name for r in catalog.search("flux")]
    browse = [r.app.entry.name for r in catalog.search("browser")]
    check("searching 'flux' and 'browser' finds it", hits[:1] == ["Flux"] and "Flux" in browse)
    with_mime = desktop.render(mime_types=True)
    check("install(mime_types=True) would keep the MIME types (for when Flux is to be the default)",
          "MimeType=text/html;" in with_mime)
    odd = ["/tmp/a dir/py$thon", "/x/main\\.py", "50%"]
    line = desktop.render(command=odd).split("Exec=", 1)[1].splitlines()[0]
    fake = desktop_entry.DesktopEntry(id="x", path="x", name="x",
                                      exec=desktop_entry._unescape(line))
    check("Exec quoting survives spaces, $, backslash and % (parsed back by desktop_entry)",
          desktop_entry.command(fake) == [*odd, "browser"], line)
    os.environ.pop("XDG_DATA_HOME")
    os.environ.pop("XDG_DATA_DIRS")
    icon = Icons().find(entry.icon if entry else "", 26)     # the system's themes
    check("its icon resolves in the launcher's icon lookup", icon is not None, str(icon))



# ── helpers for 4 on ────────────────────────────────────────────────────

class Host:
    """What a widget tree needs of its window, off screen: the damage it
    reports is kept."""

    def __init__(self, display=None) -> None:
        self.display = display
        self.damaged: list = []

    def damage(self, widget=None, *, layout: bool = False) -> None:
        self.damaged.append((widget, layout))


class Line:
    """What an AppWindow has of its connection: the service, and a send
    that keeps what was sent."""

    def __init__(self, service) -> None:
        self.service = service
        self.out: list[dict] = []

    def send(self, msg: dict) -> None:
        self.out.append(msg)


def rgb_of(image):
    import skia
    return image.toarray(colorType=skia.kRGBA_8888_ColorType,
                         alphaType=skia.kUnpremul_AlphaType)[..., :3].astype("int16")


def paint(widget, w: int, h: int):
    """`widget` laid out in w x h on the bar's background and painted
    off screen (CPU raster), as RGB."""
    import skia

    from indigoshell import theme
    from indigoshell.widgets.base import Size
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.clear(theme.color(theme.BAR_BG))
    widget.measure(Size(w, h))
    widget.arrange(skia.Rect.MakeWH(w, h))
    widget.paint(canvas)
    return rgb_of(surface.makeImageSnapshot())


def save(path: Path, rgb) -> None:
    """An RGB array as a PNG, to look at."""
    import numpy as np
    import skia
    h, w = rgb.shape[:2]
    rgba = np.concatenate([rgb.astype(np.uint8), np.full((h, w, 1), 255, np.uint8)], axis=2)
    skia.Image.fromarray(np.ascontiguousarray(rgba)).save(str(path), skia.kPNG)


def count_color(region, hex_: str, tol: int = 40) -> int:
    import numpy as np
    rgb = np.array([int(hex_[i:i + 2], 16) for i in (1, 3, 5)])
    return int((np.abs(region - rgb).max(axis=-1) <= tol).sum())


def share_of(region, rgb, tol: int = 3) -> float:
    import numpy as np
    return float((np.abs(region - np.array(rgb)).max(-1) <= tol).mean())


def hex_rgb(hex_: str) -> tuple[int, int, int]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def flux_state(titles, current=0, *, host="127.0.0.1", secure=False, loading=False,
               progress=100, bookmarked=False, find=None) -> dict:
    from flux import protocol
    tabs = [protocol.tab(t, host, loading and i == current) for i, t in enumerate(titles)]
    return protocol.state(tabs, current, progress, secure, url=f"http://{host}/",
                          bookmarked=bookmarked, find=find)


# ── 4. Flux's content ───────────────────────────────────────────────────

def test_content(scratch: Path) -> None:
    from indigoshell import theme
    from indigoshell.applink.service import AppService
    from flux import protocol
    from indigoshell.applink.flux import (FIND_FG, HOST_FG, INSECURE_FG, FluxWindow,
                                             NO_MATCHES_FG, PROGRESS_FG, PROGRESS_THICK,
                                             TAB_CURRENT_FG, TAB_FG, fit_tabs)

    section("4. Flux's content for the top bar, painted off screen")
    service = AppService({"flux": FluxWindow}, path="/nonexistent")
    W, H = 1600, theme.TOP_BAR_HEIGHT

    def flux():
        j = FluxWindow(Line(service), 0x400001, {"single": False})
        host = Host()
        j.content.attach(host)
        return j, host

    def in_rect(img, x, w):
        return img[:, max(0, int(x)):int(x + w) + 1]

    j, host = flux()
    j.on_message(flux_state(["Alpha", "Beta", "Gamma"]))
    img = paint(j.content, W, H)
    save(scratch / "content-tabs.png", img)
    tabs = j.content.tabs.layout()
    strong = [count_color(in_rect(img, t["x"], t["w"]), TAB_CURRENT_FG) for t in tabs]
    muted = [count_color(in_rect(img, t["x"], t["w"]), TAB_FG) for t in tabs]
    check("three tabs from the left, numbered: the current one in the strong colour, the rest "
          "muted", [t["text"] for t in tabs] == ["1 Alpha", "2 Beta", "3 Gamma"]
          and tabs[0]["x"] < 8 and strong[0] > 20 and muted[0] < 5
          and all(m > 20 for m in muted[1:]) and all(v < 5 for v in strong[1:]),
          f"strong {strong}, muted {muted}")
    h = j.content.host.layout()
    mark = count_color(in_rect(img, h["x"] - h["mark_w"], h["mark_w"] - 1), INSECURE_FG)
    hpx = count_color(in_rect(img, h["x"], h["w"]), HOST_FG)
    check("the host at the right end, after the open padlock (plain http) in the error colour",
          h["text"] == "127.0.0.1" and not h["secure"] and h["mark_w"] > 0 and mark > 10
          and hpx > 20 and h["x"] + h["w"] > W - 20 and h["bookmark_w"] == 0,
          f"{h['text']!r} ends at {h['x'] + h['w']:.0f} of {W}; padlock px {mark}, host px {hpx}")
    line = count_color(img[-PROGRESS_THICK:], PROGRESS_FG, 6)
    check("no load line while nothing loads", line == 0 and j.content.progress is None,
          f"{line} px")

    host.damaged.clear()
    j.on_message(flux_state(["Alpha", "Beta", "Gamma"]))
    same = list(host.damaged)
    j.on_message(flux_state(["Alpha", "Beta", "Gamma"], 1))
    check("the same state again damages nothing; a change does", same == [] and host.damaged,
          f"{len(same)} then {len(host.damaged)} damage(s)")

    titles = [f"A rather long page title number {i} about nothing in particular"
              for i in range(1, 13)]
    j.on_message(flux_state(titles, 5))
    img = paint(j.content, 900, H)
    save(scratch / "content-many.png", img)
    tabs = j.content.tabs.layout()
    room = j.content.tabs.rect
    numbered = all(t["text"].startswith(str(t["index"] + 1)) for t in tabs)
    ends = max(t["x"] + t["w"] for t in tabs)
    current = next(t for t in tabs if t["current"])
    check("twelve long titles in 900 px: each keeps its number, the titles shortened with an "
          "ellipsis to fit, the current one still strong",
          len(tabs) == 12 and numbered and all("…" in t["text"] for t in tabs)
          and ends <= room.right() + 0.5 and current["index"] == 5
          and count_color(in_rect(img, current["x"], current["w"]), TAB_CURRENT_FG) > 20,
          f"{tabs[0]['text']!r}; ends at {ends:.0f}, room to {room.right():.0f}")

    def measure(s):
        return 10.0 * len(s)
    few = fit_tabs(["x"] * 30, 20, 120, measure)
    idx = [i for i, *_ in few]
    check("too many even for their numbers: the numbers alone, as many as fit, around the "
          "current one", idx == list(range(idx[0], idx[-1] + 1)) and 20 in idx
          and max(x + w for _i, _t, x, w in few) <= 120
          and all(t == str(i + 1) for i, t, *_ in few), f"{[t for _i, t, *_ in few]}")

    j.on_message(flux_state(["Alpha"], secure=True, host="example.org", bookmarked=True))
    img = paint(j.content, W, H)
    h = j.content.host.layout()
    glyph = count_color(in_rect(img, h["bookmark_x"], h["bookmark_w"] - 1), HOST_FG)
    check("a bookmarked page over https: the bookmark glyph before the host, no padlock",
          h["bookmarked"] and h["bookmark_w"] > 0 and glyph > 15 and h["mark_w"] == 0
          and h["text"] == "example.org", f"glyph px {glyph}")
    save(scratch / "content-bookmarked.png", img)

    def find_line(f):
        j.on_message(flux_state(["Alpha", "Beta"], find=f))
        img_ = paint(j.content, W, H)
        return img_, {p["role"]: p for p in j.content.tabs.layout()}

    img, parts = find_line(protocol.find("cats", 3, 12, True))
    save(scratch / "content-find-typing.png", img)
    q, label, count = parts.get("query", {}), parts.get("label", {}), parts.get("count", {})
    check("find while typing: `find  cats▏  3/12`, the query strong, the rest muted; the tabs "
          "gone", label.get("text") == "find" and q.get("text") == "cats▏"
          and count.get("text") == "3/12"
          and count_color(in_rect(img, q["x"], q["w"]), TAB_CURRENT_FG) > 20
          and count_color(in_rect(img, label["x"], label["w"]), FIND_FG) > 10
          and count_color(in_rect(img, count["x"], count["w"]), FIND_FG) > 10,
          f"{[p['text'] for p in parts.values()]}")
    img, parts = find_line(protocol.find("cats", 4, 12, False))
    check("find while stepping: no caret", parts.get("query", {}).get("text") == "cats"
          and parts.get("count", {}).get("text") == "4/12")
    img, parts = find_line(protocol.find("zzz", 0, 0, True))
    save(scratch / "content-no-matches.png", img)
    nm = parts.get("count", {})
    check("no matches: said in the error colour", nm.get("text") == protocol.NO_MATCHES
          and count_color(in_rect(img, nm["x"], nm["w"]), NO_MATCHES_FG) > 20,
          f"{nm.get('text')!r}")
    img, parts = find_line(protocol.find("cats", None, None, True))
    check("before the page has counted: no count", "count" not in parts)

    j.on_message(flux_state(["Alpha", "Beta"], loading=True, progress=40))
    img = paint(j.content, W, H)
    save(scratch / "content-loading.png", img)
    rows = [count_color(img[-1 - i:H - i], PROGRESS_FG, 6) for i in range(PROGRESS_THICK)]
    # Just above it: nothing (the text, in the same strong colour as the
    # line, sits higher up).
    above = count_color(img[-PROGRESS_THICK - 4:-PROGRESS_THICK], PROGRESS_FG, 6)
    check("loading at 40%: the load line along the bottom edge, 40% of the width, nothing just "
          "above it", all(abs(r - 0.4 * W) <= 2 for r in rows) and above == 0
          and j.content.progress == 40, f"{rows} px in the bottom rows of {W}, {above} above")
    j.on_message(flux_state(["Alpha", "Beta"], loading=False, progress=100))
    img = paint(j.content, W, H)
    check("done: the line gone", count_color(img[-PROGRESS_THICK:], PROGRESS_FG, 6) == 0)


# ── 5. the app service ──────────────────────────────────────────────────

class App:
    """A stand-in app window on the service's socket."""

    @classmethod
    async def open(cls, path: str) -> "App":
        self = cls()
        self.r, self.w = await asyncio.open_unix_connection(path)
        return self

    def send(self, msg: dict) -> None:
        self.w.write((json.dumps(msg) + "\n").encode())

    async def answer(self, timeout: float = 2.0) -> dict | None:
        try:
            line = await asyncio.wait_for(self.r.readline(), timeout)
        except asyncio.TimeoutError:
            return None
        return json.loads(line) if line else {"eof": True}

    def close(self) -> None:
        self.w.close()


async def until(pred, timeout: float = 5.0, every: float = 0.02):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(every)


class Windows:
    """Unmapped override-redirect windows on a connection of the test's
    own: real X windows for apps to announce, never shown."""

    def __init__(self) -> None:
        import xcffib
        self.conn = xcffib.connect()
        self.screen = self.conn.get_setup().roots[0]
        self.made: list[int] = []

    def make(self, x=300, y=150, w=1000, h=700, transient_for: int | None = None) -> int:
        import struct

        import xcffib.xproto as xp
        wid = self.conn.generate_id()
        s = self.screen
        self.conn.core.CreateWindow(s.root_depth, wid, s.root, x, y, w, h, 0,
                                    xp.WindowClass.InputOutput, s.root_visual,
                                    xp.CW.OverrideRedirect, [1])
        if transient_for is not None:
            self.conn.core.ChangeProperty(xp.PropMode.Replace, wid, xp.Atom.WM_TRANSIENT_FOR,
                                          xp.Atom.WINDOW, 32, 1, struct.pack("<I", transient_for))
        self.conn.core.GetInputFocus().reply()
        self.made.append(wid)
        return wid

    def destroy(self, wid: int) -> None:
        self.conn.core.DestroyWindow(wid)
        self.conn.core.GetInputFocus().reply()
        if wid in self.made:
            self.made.remove(wid)

    def close(self) -> None:
        for wid in list(self.made):
            self.conn.core.DestroyWindow(wid)
        self.conn.flush()
        self.conn.disconnect()


def keycodes_of(conn, keysym) -> list[int]:
    """The keycodes that type `keysym` (or a letter) with or without
    Shift, read from the server on a connection of the test's own: an
    oracle for Display.keycodes."""
    if isinstance(keysym, str):
        keysym = ord(keysym)
    setup = conn.get_setup()
    lo, hi = setup.min_keycode, setup.max_keycode
    r = conn.core.GetKeyboardMapping(lo, hi - lo + 1).reply()
    per, syms = r.keysyms_per_keycode, list(r.keysyms)
    return [lo + i for i in range(hi - lo + 1) if keysym in syms[i * per:i * per + 2]]


def numlock_mask(conn) -> int:
    """The modifier bit Num Lock sets, read on a connection of the
    test's own."""
    r = conn.core.GetModifierMapping().reply()
    per, codes = r.keycodes_per_modifier, list(r.keycodes)
    numlock = set(keycodes_of(conn, 0xFF7F))
    return next((1 << bit for bit in range(8)
                 if numlock & set(codes[bit * per:(bit + 1) * per])), 0)


def held_by_another(conn, wid: int, code: int, mods: int) -> bool:
    """Whether another client holds a passive grab of (code, mods) on
    `wid`: X refuses this connection the same grab (BadAccess). When it
    does not, the grab just made is let go at once."""
    import xcffib.xproto as xp
    try:
        conn.core.GrabKeyChecked(False, wid, mods, code, xp.GrabMode.Async,
                                 xp.GrabMode.Async).check()
    except xp.AccessError:
        return True
    conn.core.UngrabKey(code, wid, mods)
    conn.core.GetInputFocus().reply()
    return False


ATTACHED = {"type": "attached"}


async def test_service(scratch: Path) -> None:
    import types

    import xcffib.xproto as xp

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.service import AppService, AppWindow
    from indigoshell.backend.x11 import Display
    from flux import protocol
    from indigoshell.applink.flux import FluxWindow, TAB_CURRENT_FG

    section("5. the app service: announces both ways, focus, losses, key grabs going and coming")
    rec = Records("indigoshell.applink.service", "indigoshell.applink.flux")
    loop = asyncio.get_running_loop()
    display = Display()
    display.attach_loop(loop)

    def pump():
        while display.pump():
            pass

    loop.add_reader(display.fd, pump)
    xw = Windows()
    A, B, E, G, G2, D = (xw.make() for _ in range(6))
    C = xw.make(transient_for=A)            # a dialog of A's
    L, CTRL = keycodes_of(xw.conn, "l")[0], xp.ModMask.Control
    sock = os.path.join(runtime_dir(), f"{APP}-tbt-{os.getpid()}-apps.sock")
    service = AppService({"flux": FluxWindow}, path=sock, display=display)
    service.show_launcher = lambda mode, placement: False      # no key is pressed here
    apps: list[App] = []
    try:
        service.start()
        await until(lambda: service.listening, 2)
        check("listening on its socket, the socket ours alone (no group or other bits)",
              service.listening and os.path.exists(sock)
              and (os.stat(sock).st_mode & 0o077) == 0, oct(os.stat(sock).st_mode & 0o777))

        async def app(*msgs):
            a = await App.open(sock)
            apps.append(a)
            for m in msgs:
                a.send(m)
            return a

        a = await app(protocol.announce(A, False), flux_state(["Alpha", "Beta"]))
        got = await a.answer()
        wa = await until(lambda: service.window_for(A) is not None
                         and len(service.window_for(A).state["tabs"]) == 2 and service.window_for(A))
        check("a main Flux window: answered {type: attached}; kept as a flux window with its "
              "state; Ctrl+L, Ctrl+T, Ctrl+M grabbed on it (X refuses another client the same "
              "grab of Ctrl+L)",
              got == ATTACHED and isinstance(wa, FluxWindow) and wa.kind == "flux"
              and not wa.single and A in service.grabs
              and sorted(set(service.grabs[A].chords.values())) == ["actions", "address", "new_tab"]
              and held_by_another(xw.conn, A, L, CTRL), f"{got}")
        b = await app(protocol.announce(B, False), flux_state(["Gamma"]))
        await b.answer()
        wb = await until(lambda: service.window_for(B))

        ctx = AppContext(service)
        host = Host(display)
        ctx.attach(host)
        check("the context area hooked in: the service knows where it is", service.context is ctx)

        # The root listener's path, with the property read stood in for:
        # the real focus is the person's.
        real_get_int = display.get_int
        display.get_int = lambda wid, name: (A if name == "_NET_ACTIVE_WINDOW"
                                              else real_get_int(wid, name))
        service._on_root_property(types.SimpleNamespace(atom=display.atom("WM_NAME")))
        unmoved = service.active
        service._on_root_property(types.SimpleNamespace(atom=service._active_atom))
        display.get_int = real_get_int
        check("_NET_ACTIVE_WINDOW changing on the root (a PropertyNotify): read, and the focused "
              "app window follows; another property changes nothing",
              unmoved != A and service.active == A and service.focused is wa
              and ctx.shown is wa.content, f"active 0x{service.active:x}")
        # From here the person's real focus is not followed: moved by hand.
        service._active_atom = 0

        service.set_active(B)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        texts = [t["text"] for t in wb.content.tabs.layout()]
        check("focus on the other Flux window: its tabs; the first's content let go (no window)",
              ctx.shown is wb.content and texts == ["1 Gamma"]
              and all(w_.window is None for w_ in wa.content.walk())
              and count_color(img, TAB_CURRENT_FG) > 20, f"{texts}")
        host.damaged.clear()
        a.send(flux_state(["Alpha", "Beta", "Delta"]))
        await until(lambda: len(wa.state["tabs"]) == 3)
        quiet = list(host.damaged)
        b.send(flux_state(["Gamma", "Epsilon"]))
        await until(lambda: len(wb.state["tabs"]) == 2)
        check("a Flux window in the background changing: the bar not repainted for it; the "
              "focused one changing: repainted", quiet == [] and bool(host.damaged),
              f"{len(quiet)} then {len(host.damaged)}")

        service.set_active(D)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        check("focus on a window that is no app's: the context area empty (the bar's background)",
              service.focused is None and ctx.shown is None
              and share_of(img, hex_rgb(theme.BAR_BG)) == 1.0)
        service.set_active(C)
        check("focus on a dialog of the first Flux window's (transient for it): that window's",
              service.focused is wa and ctx.shown is wa.content)
        service.set_active(0)
        check("no window with focus: empty", ctx.shown is None)

        service.set_active(A)
        a.close()
        gone = await until(lambda: service.window_for(A) is None)
        check("the focused window's connection lost: dropped, the context area empty; its key "
              "grabs let go (X gives another client the grab of Ctrl+L now)",
              bool(gone) and ctx.shown is None and service.focused is None
              and wa.closed and wb in service.windows and A not in service.grabs
              and not held_by_another(xw.conn, A, L, CTRL))
        a2 = await app(protocol.announce(A, False), flux_state(["Alpha again"]))
        await a2.answer()
        wa2 = await until(lambda: service.window_for(A))
        check("... and back (a new connection, as after its X window was replaced): shown again, "
              "its keys grabbed again",
              wa2 is not None and wa2 is not wa and ctx.shown is wa2.content
              and wa2.content.tabs.titles == ["Alpha again"]
              and held_by_another(xw.conn, A, L, CTRL))
        a3 = await app(protocol.announce(A, False), flux_state(["Alpha third"]))
        await a3.answer()
        wa3 = await until(lambda: service.window_for(A) is not wa2 and service.window_for(A))
        a2.close()
        await until(lambda: wa2.closed)
        check("the same X window announced on a second connection, then the first one closing: "
              "the grabs stay (the newer window's)",
              wa3 is not None and service.grabs.get(A) is not None
              and service.grabs[A].window is wa3 and held_by_another(xw.conn, A, L, CTRL))

        e = await app(protocol.announce(E, True), flux_state(["Popup"]))
        got = await e.answer()
        we = await until(lambda: service.window_for(E))
        check("a single Flux window (a sign-in popup, the inspector): attached and shown, no key "
              "grabbed on it (X lets another client take Ctrl+L there)",
              got == ATTACHED and we is not None and we.single and E not in service.grabs
              and not held_by_another(xw.conn, E, L, CTRL))

        mark = len(rec.records)
        g = await app(protocol.message("announce", app="ide", wid=G))
        got = await g.answer()
        g2 = await app(protocol.message("announce", app="ide", wid=G2))
        await g2.answer()
        wg = await until(lambda: service.window_for(G))
        service.set_active(G)
        warned = [m for m in rec.at_least(logging.WARNING, mark) if "ide" in m]
        check("an app kind with no class here (\"ide\"): kept and answered, shown as nothing, "
              "said once; no keys grabbed", got == ATTACHED and type(wg) is AppWindow
              and wg.content is None and ctx.shown is None and len(warned) == 1
              and G not in service.grabs, f"{warned}")

        bad = []
        for wid in (0, display.root, "x", True, 1 << 30, display.conn.generate_id()):
            z = await app(protocol.message("announce", app="flux", wid=wid, single=False))
            bad.append(await z.answer(0.3))
        for msg in (protocol.message("announce", wid=xw.make(), single=False),
                    protocol.message("announce", app="", wid=xw.make())):
            z = await app(msg)
            bad.append(await z.answer(0.3))
        early = await app(flux_state(["Early"]), protocol.announce(xw.make(), False))
        got_early = await early.answer()
        check("announces that are no window (0, the root, not a number, too large, one of the "
              "shell's own) or name no kind: ignored, no answer; a state before the announce: "
              "ignored", bad == [None] * 8 and got_early == ATTACHED
              and not any(w_.content and w_.content.tabs.titles == ["Early"]
                          for w_ in service.windows if isinstance(w_, FluxWindow)),
              f"{bad}")

        mark = len(rec.records)
        b.w.write(b'not json\n{"no": "type"}\n[1, 2]\n')
        b.send(protocol.message("from-the-future", x=1))
        b.send(protocol.message("announce", app="flux", wid=B, single=False))
        b.send(flux_state(["Gamma", "Epsilon", "Zeta"]))
        await until(lambda: len(wb.state["tabs"]) == 3)
        said = [m for m in rec.at_least(logging.DEBUG, mark)
                if "no message" in m or "not understood" in m or "announced again" in m]
        check("lines that are no message, a type nobody knows, a second announce on one "
              "connection: each ignored with one debug line, and the line goes on",
              len(wb.state["tabs"]) == 3 and len(said) == 5
              and not rec.at_least(logging.WARNING, mark), f"{said}")

        service.set_active(B)
        service._restarting = lambda: True          # as the daemon says on a reload
        service.stop()
        del service._restarting
        notes = [await x_.answer(1) for x_ in (b, e, a3, g)]
        eofs = [await x_.answer(1) for x_ in (b, e, a3, g)]
        check("the service stopped for a reload: every app told {type: restarting}, then hears "
              "its line close; the socket file gone; the context area empty; every key grab let "
              "go (X gives another client Ctrl+L on both windows)",
              all(n_ == {"type": "restarting"} for n_ in notes)
              and all(x_ == {"eof": True} for x_ in eofs)
              and not os.path.exists(sock) and not service.windows and ctx.shown is None
              and not service.grabs and not held_by_another(xw.conn, A, L, CTRL)
              and not held_by_another(xw.conn, B, L, CTRL), f"{notes} {eofs}")
        service.start()
        await until(lambda: service.listening, 2)
        b2 = await app(protocol.announce(B, False), flux_state(["Gamma back"]))
        got = await b2.answer()
        wb2 = await until(lambda: service.window_for(B))
        check("started again: apps announce again, focus as it was (still on that window), the "
              "keys grabbed again", got == ATTACHED and wb2 is not None
              and ctx.shown is wb2.content and held_by_another(xw.conn, B, L, CTRL))
        ctx.detach()
        check("the context area detached: unhooked from the service",
              service.context is None and ctx._on_focus not in service._followers)
        expected = ("no kind 'ide'", "an app announced")
        unexpected = [m for m in rec.at_least(logging.WARNING)
                      if not any(e_ in m for e_ in expected)]
        check("no warnings or errors but the ones for the unknown kind and the announces that "
              "are none", unexpected == [], f"{unexpected[:3]}")
    finally:
        for x_ in apps:
            x_.close()
        service.stop()
        loop.remove_reader(display.fd)
        display.close()
        xw.close()


# ── 5b. the keys the shell takes ────────────────────────────────────────

class CoreRecorder:
    """The service's X connection's core requests, recorded: the key
    grabs passed on to the server, the keyboard grabs (active ones, which
    would take the person's keyboard) recorded and never sent."""

    def __init__(self, core) -> None:
        self._core, self.calls = core, []

    def __getattr__(self, name):
        return getattr(self._core, name)

    def GrabKeyChecked(self, owner_events, wid, mods, key, pointer_mode, keyboard_mode):
        self.calls.append(("grab", wid, key, mods, owner_events, pointer_mode, keyboard_mode))
        return self._core.GrabKeyChecked(owner_events, wid, mods, key, pointer_mode,
                                         keyboard_mode)

    def UngrabKey(self, key, wid, mods, is_checked=False):
        self.calls.append(("ungrab", wid, key, mods))
        return self._core.UngrabKey(key, wid, mods)

    def GrabKeyboard(self, owner_events, wid, time_, pointer_mode, keyboard_mode,
                     is_checked=True):
        import types

        import xcffib.xproto as xp
        self.calls.append(("keyboard", wid, owner_events, pointer_mode, keyboard_mode))
        return types.SimpleNamespace(
            reply=lambda: types.SimpleNamespace(status=xp.GrabStatus.Success))

    def UngrabKeyboard(self, time_, is_checked=False):
        self.calls.append(("let go",))

    def of(self, kind: str, since: int = 0) -> list:
        return [c for c in self.calls[since:] if c[0] == kind]


def test_grabs(scratch: Path) -> None:
    import types

    import xcffib.xproto as xp

    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from flux import protocol
    from indigoshell.applink.flux import FluxWindow
    from indigoshell.services.addresses import AddressMode
    from indigoshell.services.modes import Choices

    section("5b. the keys the shell takes (real unmapped windows of the test's own)")
    # The address list reads the history and the bookmarks: scratch ones.
    saved_env = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
    os.environ.update(XDG_CONFIG_HOME=str(scratch / "grabs-config"),
                      XDG_DATA_HOME=str(scratch / "grabs-data"))
    rec = Records("indigoshell.applink.service", "indigoshell.applink.flux")
    display = Display()
    real_core = display.conn.core
    core = CoreRecorder(real_core)
    display.conn.core = core
    xw = Windows()
    other = xw.conn
    A, S, N, E, F = (xw.make() for _ in range(5))
    CTRL, LOCK, SHIFT = xp.ModMask.Control, xp.ModMask.Lock, xp.ModMask.Shift
    ASYNC = xp.GrabMode.Async
    num = numlock_mask(other)
    locks = sorted({0, LOCK, num, LOCK | num})
    codes = {c: keycodes_of(other, c) for c in "ltmyo"}
    service = AppService({"flux": FluxWindow}, path="/nonexistent", display=display)
    service.running = True                  # started, as far as the keys go (no socket here)
    display.add_keymap_listener(service._keymap_changed)
    typed, shown = [], []

    class Launcher:
        """What Flux's side needs of the launcher: shows a mode, takes
        keys for it, holds the keyboard (or not)."""
        window = types.SimpleNamespace(keyboard_grabbed=True)
        modes: list = []

        def shows(self, m):
            return m in self.modes

        def type_ahead(self, m, keys):
            if not self.shows(m):
                return False
            typed.extend(k.text or hex(k.keysym) for k in keys)
            return True

        def cancel(self, m):
            self.modes = []

    launcher = Launcher()

    def show(mode, placement):
        core.calls.append(("launcher", type(mode).__name__))
        mode.host = launcher
        launcher.modes = [mode]
        shown.append(mode)
        return True

    service.show_launcher = show

    def flux(wid, announce):
        line = Line(service)
        window = FluxWindow(line, wid, announce)
        service.added(window)
        return window, line

    def key(wid, sym, state=0, release=False):
        code = (codes.get(chr(sym)) if sym < 0x100 else None) or keycodes_of(other, sym)
        ev = xp.KeyPressEvent.synthetic(code[0], 0, display.root, wid, 0, 0, 0, 0, 0, state, 1)
        ev.response_type = 3 if release else 2
        display.dispatch(ev)

    try:
        wa, la = flux(A, protocol.announce(A, False))
        wa.on_message(flux_state(["Alpha"], host="a.example"))
        grabs = core.of("grab")
        want = {(A, codes[c][0], CTRL | lock) for c in "ltm" for lock in locks}
        check("a main Flux window announced: GrabKey on its window for Ctrl+L, Ctrl+T, Ctrl+M, by "
              "the keycodes the keymap gives (read on another connection), once per combination "
              "of Caps Lock and Num Lock, owner_events off, asynchronous for both devices",
              {(w_, k, m) for _, w_, k, m, *_ in grabs} == want and len(grabs) == len(want)
              and all(c[4:] == (False, ASYNC, ASYNC) for c in grabs)
              and all(len(codes[c]) == 1 for c in "ltm"),
              f"{len(grabs)} grabs; lock masks {locks}; keycodes {codes}")
        check("X itself: another client's grab of Ctrl+L on that window refused, with Caps Lock "
              "and with Num Lock too; of Ctrl+Shift+L not (that one is not the shell's)",
              held_by_another(other, A, codes["l"][0], CTRL)
              and held_by_another(other, A, codes["l"][0], CTRL | LOCK)
              and held_by_another(other, A, codes["l"][0], CTRL | num)
              and not held_by_another(other, A, codes["l"][0], CTRL | SHIFT))
        check("events on that window are the service's (Display.on), for that window only",
              service._on_app_event in display._handlers.get(A, [])
              and all(service._on_app_event not in hs for w_, hs in display._handlers.items()
                      if w_ != A))

        n = len(core.calls)
        ws, _ = flux(S, protocol.announce(S, True))
        wn, _ = flux(N, protocol.message("announce", app="flux", wid=N))
        check("a single window's announce (a popup, the inspector), and one that says neither: no "
              "grab at all, no routing", core.calls[n:] == [] and not ws.keys and not wn.keys
              and S not in service.grabs and N not in service.grabs
              and S not in display._handlers and N not in display._handlers)

        # Another client holds Ctrl+M on E and F first.
        for wid in (E, F):
            other.core.GrabKeyChecked(False, wid, CTRL, codes["m"][0], ASYNC, ASYNC).check()
        mark, n = len(rec.records), len(core.calls)
        we, _ = flux(E, protocol.announce(E, False))
        wf, _ = flux(F, protocol.announce(F, False))
        warned = rec.at_least(logging.WARNING, mark)
        check("another client already holding Ctrl+M on a window: one warning (not again for the "
              "next such window), Ctrl+M left to the app (the lock combinations that went "
              "through let go again), Ctrl+L and Ctrl+T grabbed",
              len(warned) == 1 and "ctrl+m" in warned[0]
              and all(sorted(set(service.grabs[w_].chords.values())) == ["address", "new_tab"]
                      for w_ in (E, F))
              and not held_by_another(other, E, codes["m"][0], CTRL | LOCK)
              and held_by_another(other, E, codes["l"][0], CTRL), f"{warned}")
        for wid in (E, F):
            other.core.UngrabKey(codes["m"][0], wid, CTRL)
        other.core.GetInputFocus().reply()

        # A press, as the X server reports one: Caps Lock on, the keys
        # after it on the app's window while the shell holds the keyboard.
        n = len(core.calls)
        key(A, ord("l"), CTRL | LOCK)
        key(A, ord("l"), CTRL | LOCK, release=True)
        key(A, ord("y"), LOCK)
        key(A, ord("o"), LOCK | SHIFT)
        after = core.calls[n:]
        check("Ctrl+L pressed in it (Caps Lock on): first the keyboard held on that window "
              "(GrabKeyboard, owner_events off, asynchronous), then the launcher opened on the "
              "addresses, the page's address in the query, selected; the keyboard not given back "
              "(the launcher has it)",
              after[:2] == [("keyboard", A, False, ASYNC, ASYNC), ("launcher", "AddressMode")]
              and isinstance(shown[-1], AddressMode) and shown[-1].text == "http://a.example/"
              and shown[-1].select_text and ("let go",) not in after, f"{after[:3]}")
        check("the keys typed after it, before the launcher had the keyboard: into its query, in "
              "order, through the keymap (Caps Lock and Shift as typed); the chord's release not "
              "one of them", typed == ["Y", "o"], f"{typed}")
        key(A, ord("l"), CTRL)
        check("Ctrl+L again while that launcher is up: a key for it (its pick), not a second "
              "launcher", typed[-1] == hex(ord("l")) and len(shown) == 1)
        key(A, 0xFF1B)
        check("Escape the same way: the launcher's to answer", typed[-1] == hex(0xFF1B))

        launcher.modes = []                 # closed
        Launcher.window.keyboard_grabbed = False
        mark, n = len(rec.records), len(core.calls)
        key(A, ord("m"), CTRL | num)
        check("Ctrl+M (Num Lock on), the launcher up but without the keyboard: the actions shown, "
              "and the keyboard given back at once (never held with nothing to type into), said",
              isinstance(shown[-1], Choices) and ("let go",) in core.calls[n:]
              and any("did not take the keyboard" in m
                      for m in rec.at_least(logging.WARNING, mark)))
        Launcher.window.keyboard_grabbed = True
        launcher.modes = []
        service.show_launcher = lambda mode, placement: False
        n = len(core.calls)
        key(A, ord("t"), CTRL)
        check("Ctrl+T with no launcher to show: the keyboard held, then given back, and the "
              "browser told to ask itself: {type: prompt, action: new_tab}",
              [c[0] for c in core.calls[n:]] == ["keyboard", "let go"]
              and la.out[-1] == {"type": "prompt", "action": "new_tab"}, f"{la.out[-1:]}")
        n, sent = len(core.calls), len(la.out)
        wa.on_message(protocol.launcher("address"))
        check("{type: launcher} from the browser (a new window): the same, with no key pressed, "
              "so nothing held or given back", core.calls[n:] == []
              and la.out[sent:] == [{"type": "prompt", "action": "address"}])
        service.show_launcher = show
        n = len(core.calls)
        key(S, ord("l"), CTRL)
        check("a key event for a window with no grab (the single one): not the service's",
              core.calls[n:] == [])

        n = len(core.calls)
        real_keycodes = display.keycodes
        moved_l = codes["l"][0] + 1
        display.keycodes = lambda sym: [moved_l] if sym == ord("l") else real_keycodes(sym)
        display.dispatch(xp.MappingNotifyEvent.synthetic(xp.Mapping.Keyboard, 8, 248))
        moved = core.calls[n:]
        display.keycodes = real_keycodes
        n = len(core.calls)
        display.dispatch(xp.MappingNotifyEvent.synthetic(xp.Mapping.Pointer, 0, 0))
        pointer = core.calls[n:]
        display.dispatch(xp.MappingNotifyEvent.synthetic(xp.Mapping.Keyboard, 8, 248))
        check("the keymap changed (MappingNotify; Ctrl+L on another key now): every window's "
              "grabs let go and made again, Ctrl+L on its new key; a pointer mapping change: "
              "nothing",
              ("ungrab", A, codes["l"][0], CTRL) in moved
              and ("grab", A, moved_l, CTRL, False, ASYNC, ASYNC) in moved
              and ("grab", A, codes["l"][0], CTRL, False, ASYNC, ASYNC) not in moved
              and {c[1] for c in moved if c[0] == "grab"} == {A, E, F} and pointer == []
              and held_by_another(other, A, codes["l"][0], CTRL))

        n = len(core.calls)
        service.forget(None, wa)
        let_go = {(c[1], c[2], c[3]) for c in core.of("ungrab", n)}
        check("the window's connection gone: each of its grabs let go (UngrabKey), the routing "
              "too; X gives another client Ctrl+L on it now",
              let_go == want and A not in service.grabs and A not in display._handlers
              and not held_by_another(other, A, codes["l"][0], CTRL), f"{len(let_go)} let go")
        unexpected = [m for m in rec.at_least(logging.WARNING)
                      if "ctrl+m" not in m and "did not take the keyboard" not in m]
        check("no other warnings or errors", unexpected == [], f"{unexpected[:3]}")
    finally:
        for w_ in list(service.windows):
            service.forget(None, w_)
        display.remove_keymap_listener(service._keymap_changed)
        display.conn.core = real_core
        xw.close()
        display.close()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── 6. where the launcher drops from ────────────────────────────────────

def test_placement() -> None:
    import types

    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from indigoshell.applink.flux import FluxWindow
    from indigoshell.window import Placement

    section("6. where Flux's launcher drops from")
    display = Display()
    xw = Windows()
    P = xw.make(300, 150, 1000, 700)
    service = AppService({"flux": FluxWindow}, path="/nonexistent", display=display)
    jw = FluxWindow(Line(service), P, {"single": False})
    alone = jw.placement()

    def bar(x, y, w, h):
        return types.SimpleNamespace(window=types.SimpleNamespace(wid=1, content_root=(x, y, w, h)))

    service.context = bar(0, 0, 2560, 34)
    under_bar = jw.placement()
    service.context = bar(2560, 0, 1920, 34)
    elsewhere = jw.placement()
    service.context = types.SimpleNamespace(window=types.SimpleNamespace(wid=None,
                                                                        content_root=(0, 0, 0, 0)))
    unmade = jw.placement()
    service.context = bar(0, 0, 2560, 34)
    xw.destroy(P)
    gone = jw.placement()
    check("no top bar: centred on the window, its top at the window's top edge",
          alone == Placement(800, 150), f"{alone}")
    check("a top bar over it: centred on the window, its top at the bar's bottom edge",
          under_bar == Placement(800, 34), f"{under_bar}")
    check("the bar on another monitor, or not made: the window's top edge again",
          elsewhere == Placement(800, 150) and unmade == Placement(800, 150), f"{elsewhere}")
    check("the window gone: no placement (the launcher's own spot)", gone is None)
    xw.close()
    display.close()


# ── 7. the top bar's spec ───────────────────────────────────────────────

USER_CONFIG_CHILD = r"""
import importlib.util, json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("user_config_under_test", sys.argv[2])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from indigoshell import config_default as cd
from indigoshell.applink.context import AppContext
from indigoshell.applink.service import AppService
from indigoshell.widgets.label import Label
names = [w.name for w in module.WINDOWS]
top = next(w for w in module.WINDOWS if w.name == cd.TOP_BAR)
first = list(top.content.children())[0]
apps = [s for s in module.SERVICES if isinstance(s, AppService)]
# The documented way to add a widget.
added = [cd.top_bar(Label("mine")) if w.name == cd.TOP_BAR else w for w in module.WINDOWS]
row = next(w for w in added if w.name == cd.TOP_BAR).content
print(json.dumps({"names": names, "context": isinstance(first, AppContext),
                  "same_service": len(apps) == 1 and first.service is apps[0],
                  "kinds": sorted(apps[0].kinds) if apps else [],
                  "added": [type(c).__name__ for c in row.children()],
                  "same_order": [w.name for w in added] == names}))
"""


def test_spec(scratch: Path) -> None:
    import types

    import skia

    from indigoshell import config_default as cd, theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Monitor
    from indigoshell.applink.flux import FluxWindow, TAB_CURRENT_FG
    from indigoshell.core.paths import config_dir
    from indigoshell.widgets.label import Label
    from indigoshell.window import Anchor, Layer, Window

    section("7. the top bar's spec, as computed data")
    names = [s.name for s in cd.WINDOWS]
    top = next(s for s in cd.WINDOWS if s.name == cd.TOP_BAR)
    bar = next(s for s in cd.WINDOWS if s.name == "bar")
    check("in WINDOWS right after the bottom bar, opened at start like it",
          names.index(cd.TOP_BAR) == names.index("bar") + 1 and top.autostart, f"{names[:4]}")
    check("anchored to the top edge, full width, as tall as the bottom bar (TOP_BAR_HEIGHT), a "
          "dock that reserves its space, never focused",
          top.anchor == Anchor.TOP and top.size == (None, theme.TOP_BAR_HEIGHT) == bar.size
          and bar.size == (None, theme.BAR_HEIGHT) and top.layer == Layer.DOCK
          and top.exclusive and not top.focusable and top.margin == bar.margin)
    check("the bottom bar's look: its background, gutter, rule (none: the bottom bar draws none "
          "either) and font size; managed like it (not override-redirect, so no keep_above: "
          "the window manager stacks it, and a focused fullscreen window over it)",
          (top.background, top.padding, top.rule, top.rule_thick, top.rule_glow)
          == (bar.background, bar.padding, bar.rule, bar.rule_thick, bar.rule_glow)
          and not top.override_redirect and not top.keep_above and top.sticky == bar.sticky,
          f"rule {top.rule!r}")
    first = list(top.content.children())[0]
    check("its row: the context area first, taking the room, on the service in SERVICES",
          isinstance(first, AppContext) and first.flex == 1 and first.service is cd.APPS
          and cd.APPS in cd.SERVICES and cd.APPS.kinds == {"flux": FluxWindow})

    class FakeDisplay:
        screen = types.SimpleNamespace(root_depth=24)

        def __init__(self, monitors, size):
            self._monitors, self._size, self.props = monitors, size, {}

        def monitors(self):
            return list(self._monitors)

        def primary_monitor(self):
            return self._monitors[0]

        def geometry(self):
            return self._size

        def set_prop(self, wid, name, type_, values):
            self.props[name] = list(values)

    def struts(spec, monitors, size):
        fd = FakeDisplay(monitors, size)
        win = Window(fd, spec)
        geo = win._compute_geometry(fd.primary_monitor())
        win.content_root, win.wid = geo, 1
        win._set_struts()
        return geo, fd.props.get("_NET_WM_STRUT_PARTIAL"), fd.props.get("_NET_WM_STRUT")

    one = [Monitor(0, 0, 2560, 1440, True, "HDMI-0")]
    geo, partial, plain = struts(top, one, (2560, 1440))
    bgeo, bpartial, _ = struts(bar, one, (2560, 1440))
    th = theme.TOP_BAR_HEIGHT
    check(f"on a 2560x1440 monitor: 2560x{th} at +0+0; the strut it would set reserves the top "
          f"{th} px across the monitor (never set on a real window here)",
          geo == (0, 0, 2560, th) and partial == [0, 0, th, 0, 0, 0, 0, 0, 0, 2559, 0, 0]
          and plain == [0, 0, th, 0], f"{geo} {partial}")
    check("... the bottom bar's for comparison: 42 px at the bottom",
          bgeo == (0, 1398, 2560, 42) and bpartial == [0, 0, 0, 42, 0, 0, 0, 0, 0, 0, 0, 2559],
          f"{bgeo} {bpartial}")
    two = [Monitor(1920, 0, 2560, 1440, True, "A"), Monitor(0, 360, 1920, 1080, False, "B")]
    geo2, partial2, _ = struts(top, two, (4480, 1440))
    _, bpartial2, _ = struts(bar, two, (4480, 1440))
    check("primary on the right of a second monitor: on the primary, like the bottom bar, the "
          "strut spanning only its columns", geo2 == (1920, 0, 2560, th)
          and partial2[2] == th and partial2[8:10] == [1920, 4479]
          and bpartial2[10:12] == [1920, 4479], f"{geo2} {partial2}")

    # The whole bar painted, with a widget of the user's at the right.
    svc = AppService({"flux": FluxWindow}, path="/nonexistent")
    spec = cd.top_bar(Label("MINE", color=theme.CYAN_BRIGHT), apps=svc)
    fd = FakeDisplay(one, (2560, 1440))
    win = Window(fd, spec)
    W, H = 2560, theme.TOP_BAR_HEIGHT
    win.content_rect = skia.Rect.MakeWH(W, H)
    win.content_root, win.width, win.height = (0, 0, W, H), W, H
    spec.content.attach(win)
    jw = FluxWindow(Line(svc), 0x400001, {"single": False})
    jw.on_message(flux_state(["Alpha", "Beta"], loading=True, progress=50))
    svc.added(jw)
    svc.set_active(0x400001)
    win._layout_content()
    surface = skia.Surface(W, H)
    win.paint(surface.getCanvas(), W, H)
    img = rgb_of(surface.makeImageSnapshot())
    save(scratch / "topbar-whole.png", img)
    ctx, label, chip = list(spec.content.children())
    h = jw.content.host.layout()
    check("the whole bar off screen: Flux's tabs from the left, its host just before the user's "
          "widget, the widget at the right end, the load line under the context area only",
          ctx.shown is jw.content and jw.content.tabs.layout()[0]["x"] < 10
          and h["x"] + h["w"] <= label.rect.left() and label.rect.right() >= W - 10
          and count_color(img[:, int(label.rect.left()):], theme.CYAN_BRIGHT) > 20
          and count_color(img[:, :int(ctx.rect.left()) + 10], TAB_CURRENT_FG) > 0
          and count_color(img[-1:, int(label.rect.left()):], theme.BAR_RULE, 6) == 0
          and abs(count_color(img[-1:], theme.BAR_RULE, 6) - 0.5 * ctx.rect.width()) <= 2,
          f"context {ctx.rect.width():.0f} px, label at {label.rect.left():.0f}")
    check("the bar's background everywhere else (no rule)",
          share_of(img[:3], hex_rgb(theme.BAR_BG)) > 0.98)
    check("the agent chip last in the row, empty with no agent configured: no width and no "
          "gap (with one it is always there; agent/ui/status.py, tests/agent_chip.py)",
          type(chip).__name__ == "AgentChip" and chip.collapsed and chip.rect.width() == 0)

    path = os.path.join(config_dir(), "config.py")
    if not os.path.isfile(path):
        print("  (no user config.py on this machine; its check skipped)", flush=True)
        return
    cache = Path(path).parent / "__pycache__"
    before = sorted((p.name, p.stat().st_mtime) for p in cache.iterdir()) if cache.is_dir() else []
    child = scratch / "user_config_child.py"
    child.write_text(USER_CONFIG_CHILD)
    envs = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                XDG_CONFIG_HOME=str(scratch / "uc-config"), XDG_DATA_HOME=str(scratch / "uc-data"),
                XDG_STATE_HOME=str(scratch / "uc-state"), XDG_CACHE_HOME=str(scratch / "uc-cache"))
    p = subprocess.run([PY, "-B", str(child), str(REPO), path], capture_output=True, text=True,
                       env=envs, timeout=120)
    after = sorted((p_.name, p_.stat().st_mtime) for p_ in cache.iterdir()) if cache.is_dir() else []
    out = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() \
        else {}
    check("the user's config.py (imported by its path, scratch XDG, no bytecode): it extends "
          "WINDOWS, so the top bar is there after the bottom bar, its context on the one app "
          "service in its SERVICES",
          bool(out) and out["names"][:2] == ["bar", cd.TOP_BAR] and out["context"]
          and out["same_service"] and out["kinds"] == ["flux"] and before == after,
          f"exit {p.returncode}; {out.get('names', [])[:3]} ... {len(out.get('names', []))} "
          f"windows; {p.stderr.strip()[-200:] if p.returncode else ''}")
    check("... and a widget of its own added the documented way: the context area then it, the "
          "order of WINDOWS kept", out.get("added") == ["AppContext", "Label", "AgentChip"]
          and out.get("same_order"), f"{out.get('added')}")


# ── 8. on screen ────────────────────────────────────────────────────────

PAINTED_BG = (0x10, 0x60, 0x90)     # a page colour no other page here has
LINE_BG = (0x05, 0x03, 0x10)        # the browser's own line: theme.BAR_BG
PAGE = ("<!doctype html><title>{title}</title>"
        "<body style='margin:0;background:#1a1a2a;color:#ccc'><p>{title}</p></body>")


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/painted":
            body = ("<!doctype html><title>Painted</title><body style='margin:0;background:"
                    "#%02x%02x%02x'></body>" % PAINTED_BG).encode()
        elif path.startswith("/page/"):
            body = PAGE.format(title=path[len("/page/"):]).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Reader:
    """A connection of the test's own, for reading what the browser's
    window shows (its own composite pixmap) and its properties."""

    def __init__(self) -> None:
        import xcffib
        import xcffib.composite
        self.conn = xcffib.connect()
        self.setup = self.conn.get_setup()
        self.screen = self.setup.roots[0]
        self.composite = self.conn(xcffib.composite.key)
        self.composite.QueryVersion(0, 4).reply()

    def region(self, wid: int, x: int, y: int, w: int, h: int):
        """A rect of `wid`'s own pixels (inside the window manager's
        border), as RGB; None when it cannot be read (not viewable)."""
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

    def children(self, wid: int) -> list[int]:
        try:
            return list(self.conn.core.QueryTree(wid).reply().children)
        except Exception:
            return []

    def map_state(self, wid: int) -> int:
        try:
            return self.conn.core.GetWindowAttributes(wid).reply().map_state
        except Exception:
            return -1

    def cardinal(self, wid: int, name: str) -> int | None:
        import xcffib.xproto as xp
        atom = self.conn.core.InternAtom(False, len(name), name.encode()).reply().atom
        try:
            r = self.conn.core.GetProperty(False, wid, atom, xp.Atom.CARDINAL, 0, 1).reply()
        except Exception:
            return None
        return r.value.to_atoms()[0] if r.value_len else None

    def to_current_desktop(self, wid: int) -> tuple[int | None, int | None]:
        """When the window manager put the test's window on a workspace
        that is not shown, ask it to move that window (ours, nobody
        else's) to the current one: an EWMH _NET_WM_DESKTOP request. The
        workspaces themselves are left as they are. (desktop it was on,
        current desktop)."""
        import xcffib.xproto as xp
        current = self.cardinal(self.screen.root, "_NET_CURRENT_DESKTOP")
        mine = self.cardinal(wid, "_NET_WM_DESKTOP")
        if current is None or mine is None or mine in (current, 0xFFFFFFFF):
            return mine, current
        atom = self.conn.core.InternAtom(False, 15, b"_NET_WM_DESKTOP").reply().atom
        data = xp.ClientMessageData.synthetic([current, 1, 0, 0, 0], "I" * 5)
        event = xp.ClientMessageEvent.synthetic(32, wid, atom, data)
        self.conn.core.SendEvent(False, self.screen.root,
                                 xp.EventMask.SubstructureRedirect
                                 | xp.EventMask.SubstructureNotify, event.pack())
        self.conn.flush()
        return mine, current


async def on_screen(scratch: Path) -> None:
    import xcffib.xproto as xp

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.service import AppService
    from indigoshell.backend import gl
    from indigoshell.backend.x11 import Display
    from indigoshell.applink.flux import HOST_FG, INSECURE_FG, FluxWindow, TAB_CURRENT_FG, TAB_FG

    section("8. on screen: the real service and the real browser")
    rec = Records("indigoshell.applink.service", "indigoshell.applink.flux",
                  "indigoshell.backend.x11")
    tag = f"{APP}-tbs-{os.getpid()}"
    sock = os.path.join(runtime_dir(), f"{tag}-apps.sock")
    isock = os.path.join(runtime_dir(), f"{tag}-browser.sock")
    benv = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    benv.update({"XDG_DATA_HOME": str(scratch / "data"), "XDG_CACHE_HOME": str(scratch / "cache"),
                 "XDG_CONFIG_HOME": str(scratch / "config"),
                 "XDG_STATE_HOME": str(scratch / "state"),
                 env("BROWSER_SOCKET"): isock, env("APPS_SOCKET"): sock,
                 env("BROWSER_WIDEVINE"): "off"})
    http = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"

    loop = asyncio.get_running_loop()
    display = Display()
    display.attach_loop(loop)

    def pump():
        while display.pump():
            pass

    loop.add_reader(display.fd, pump)
    x = Reader()
    backend = gl.backend(display)
    service = AppService({"flux": FluxWindow}, path=sock, display=display)
    # No daemon and no launcher here (no key is pressed: that would take
    # input injected into the display); a launcher wanted is answered
    # `prompt`, the browser's own dialog, none in an unattended run.
    service.show_launcher = lambda mode, placement: False
    service.start()
    # The person's real focus is not followed here: the test moves it.
    service._active_atom = 0
    ctx = AppContext(service)
    ctx.attach(Host(display))
    browser = None

    def fluxes():
        return [w for w in service.windows if isinstance(w, FluxWindow)]

    def titles(w):
        return [t["title"] for t in w.state["tabs"]]

    def idle(w):
        return not any(t["loading"] for t in w.state["tabs"])

    async def shown(wid, timeout=15.0) -> bool:
        """Until the window is on screen: qtile unmaps a workspace's
        windows while another is shown. A window put on a workspace not
        shown is asked back onto the current one (see Reader)."""
        moved = x.to_current_desktop(wid)
        if moved[0] != moved[1] and None not in moved and moved[0] != 0xFFFFFFFF:
            print(f"  (test window 0x{wid:x} was put on desktop {moved[0]}, the shown one is "
                  f"{moved[1]}: asked back onto it)", flush=True)
        if x.map_state(wid) != 2:
            print(f"  (browser window 0x{wid:x} not shown yet; waiting)", flush=True)
        return bool(await until(lambda: x.map_state(wid) == 2, timeout))

    def top_rows(wid, rows=4, width=240):
        return x.region(wid, 0, 0, width, rows)

    async def rows_until(wid, colour, timeout, every=0.05):
        """The window's top rows once they are mostly `colour`; None if
        they never were within `timeout`."""
        got = []

        def ready():
            r = top_rows(wid)
            if r is not None and share_of(r, colour) > 0.9:
                got.append(r)
                return True
            return False
        await until(ready, timeout, every)
        return got[0] if got else None

    try:
        log_path = scratch / "browser.log"
        with open(log_path, "w") as err:
            browser = subprocess.Popen(
                [PY, str(REPO / "main.py"), "browser", "--unattended", f"{base}/painted",
                 f"{base}/page/Beta", f"{base}/page/Gamma"],
                cwd=REPO, env=benv, stdout=err, stderr=err)
        jw = await until(lambda: next((w for w in fluxes() if titles(w) == ["Painted", "Beta",
                                                                            "Gamma"]
                                       and idle(w)), None), 40)
        check("the browser announced its window as a main Flux window; its three tabs reached "
              "the service", jw is not None and jw.kind == "flux" and not jw.single,
              f"{[titles(w) for w in fluxes()]}")
        if jw is None:
            return
        top = jw.wid
        CTRL = xp.ModMask.Control
        chord = {c: keycodes_of(x.conn, c)[0] for c in "ltm"}
        held = {c: held_by_another(x.conn, top, code, CTRL) for c, code in chord.items()}
        check("its Ctrl+L, Ctrl+T, Ctrl+M grabbed by the service on that window: X refuses "
              "another client the same grabs", all(held.values()), f"{held}")
        await asyncio.sleep(0.5)
        check("one window, never replaced after showing; nothing of the shell's inside it",
              [w.wid for w in fluxes()] == [top] and x.children(top) == [],
              f"children {x.children(top)}")
        viewable = await shown(top)
        page = await rows_until(top, PAINTED_BG, 10)
        if page is not None:
            save(scratch / "browser-top-rows.png", page)
        check("the browser window's own pixels: the page's colour from its top edge (no line)",
              viewable and page is not None, f"viewable {viewable}")

        service.set_active(top)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        save(scratch / "context-real-browser.png", img)
        tabs = jw.content.tabs.layout()
        strong = [count_color(img[:, int(t["x"]):int(t["x"] + t["w"]) + 1], TAB_CURRENT_FG)
                  for t in tabs]
        muted = [count_color(img[:, int(t["x"]):int(t["x"] + t["w"]) + 1], TAB_FG)
                 for t in tabs]
        h = jw.content.host.layout()
        mark = count_color(img[:, int(h["x"] - h["mark_w"]):int(h["x"])], INSECURE_FG)
        hpx = count_color(img[:, int(h["x"]):int(h["x"] + h["w"]) + 1], HOST_FG)
        check("focus on it: the context area draws the browser's state (tab 1 strong, 2 and 3 "
              "muted, the host after the padlock)",
              ctx.shown is jw.content and [t["text"] for t in tabs] == ["1 Painted", "2 Beta",
                                                                        "3 Gamma"]
              and strong[0] > 20 and all(m > 20 for m in muted[1:]) and h["text"] == "127.0.0.1"
              and mark > 10 and hpx > 20, f"strong {strong}, muted {muted}")
        jw.command("tab", index=1)
        moved = await until(lambda: jw.state["current"] == 1, 5)
        check("a command to it ({type: tab, index: 1}): the browser switches, the state follows",
              bool(moved) and jw.content.tabs.current == 1)
        jw.command("tab", index=0)
        await until(lambda: jw.state["current"] == 0, 5)
        service.set_active(0)
        check("focus elsewhere: the context area empty", ctx.shown is None)

        # The shell away: nothing for three seconds, then the line.
        await shown(top)
        service.stop()
        t0 = time.monotonic()
        released = not held_by_another(x.conn, top, chord["l"], CTRL)
        await asyncio.sleep(2.0)
        early = top_rows(top)
        line = await rows_until(top, LINE_BG, 5)
        took = time.monotonic() - t0
        pushed = x.region(top, 0, 34, 240, 4)
        check("the shell stopped: its key grabs gone (another client may take Ctrl+L there); no "
              "line in the browser's window for three seconds (the page stays where it is), then "
              "its own line at the top, the page below it",
              released and early is not None and share_of(early, PAINTED_BG) > 0.95
              and line is not None
              and 2.8 <= took <= 4.5 and pushed is not None and share_of(pushed, PAINTED_BG) > 0.9,
              f"line {took:.2f} s after the stop")
        service.start()
        t0 = time.monotonic()
        back = await until(lambda: any(w.wid == top for w in fluxes()), 5)
        gone = await rows_until(top, PAINTED_BG, 5)
        regrabbed = bool(back) and held_by_another(x.conn, top, chord["l"], CTRL)
        check("the shell back: the browser announces again (its retry is every 2 s while the "
              "line shows), its keys grabbed again, and the line goes, the page at the top edge "
              "again", bool(back) and regrabbed and gone is not None,
              f"{time.monotonic() - t0:.2f} s")
        n_gl = backend is not None
        browser.send_signal(signal.SIGINT)
        code = browser.wait(20)
        dropped = await until(lambda: not fluxes(), 5)
        check("the browser quits: exit 0, its window dropped by the service", code == 0
              and bool(dropped), f"exit {code}; GL here {n_gl}")
        bad = rec.at_least(logging.WARNING)
        check("no warnings or errors from the service, Flux's side, or the display", not bad,
              f"{bad[:3]}")
    finally:
        if browser is not None and browser.poll() is None:
            browser.kill()
            browser.wait(10)
        ctx.detach()
        service.stop()
        loop.remove_reader(display.fd)
        display.close()
        http.shutdown()
        for path in (sock, isock, isock + ".lock"):
            if os.path.exists(path):
                os.unlink(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--off-screen", action="store_true", help="sections 1-7 only")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix=f"{APP}-topbar-test-"))
    print(f"scratch {scratch}", flush=True)
    # Before anything of the shell's reads it: the launcher's history.
    os.environ["XDG_STATE_HOME"] = str(scratch / "state")
    logging.basicConfig(level=logging.WARNING, filename=scratch / "test.log",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    t0 = time.monotonic()
    try:
        test_display()
        test_xlib_guard(scratch)
        test_desktop(scratch)
        test_content(scratch)
        asyncio.run(test_service(scratch))
        test_grabs(scratch)
        test_placement()
        test_spec(scratch)
        if not args.off_screen:
            asyncio.run(on_screen(scratch))
    except Exception:
        FAILED.append("aborted")
        print("\nABORTED:\n" + traceback.format_exc(), flush=True)
    keep = bool(FAILED) or args.keep
    print(f"\n{time.monotonic() - t0:.0f} s" + (f"; logs kept in {scratch}" if keep else ""))
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all passed")
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
