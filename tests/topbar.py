"""The shell's top bar and its line to the shell's own apps
(indigoshell/applink: the app bar protocol), and the shell code under it.

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
   data dir, and the app catalog finding, showing and launching it, and
   rescanning when it appears.
4. An app's bar (applink/view.py, the kinds) painted off screen on the
   bar's background and read back as pixels, from bar dicts: the tabs in
   equal slots, a box each (the current one's outline bright, its title
   strong and its number cyan in brackets; the rest muted; long titles
   cut with an ellipsis; the run around the current tab when even the
   narrowest slots do not fit), the host with the open padlock and the
   bookmark; an open input's box in the items' place (the search mark,
   the text and its caret, the count at the right end, none while
   stepping, "no matches" in the error colour), the items back after
   it; the current tab's progress lighting its outline, a comet round a
   busy tab behind, hover; the motion (a switch sliding the light, a
   close wiping the tab out before the others slide; the clock asked
   for frames only while something moves).
4b. The mouse on an app's tabs, through the top bar's own Window (no X:
   a stand-in display whose keyboard grabs are counted, never made) fed
   real X events built from their bytes, the events recorded by id:
   which tab is under x; a left and a right click selecting; a middle
   click closing (let go elsewhere: nothing); the drag threshold; a drag
   lifting the tab (the keyboard taken once, for Escape), the others
   making room, the marker, `move` before the tab it lands before (both
   ways; dropped in place: nothing); below the bar still a move, past
   TEAR_OUT_PX `detach` (never a window's only tab); Escape cancelling,
   other keys swallowed, a late Escape never closing the bar; the
   keyboard refused; the keyboard given back on every way out; hover;
   the bar's other widgets as before.
5. The app service on a socket of its own, a stand-in app on it sending
   bars: the announce and the answer both ways (the voices' names; an
   app other than Flux, the same class; announces that are no X window
   or name no app or id; lines that are no message, types it does not
   know); a bar shown and replaced, a bad one keeping the last good (one
   warning), an answer for nothing open dropped, chords grabbed again
   when they change; focus followed between two windows, a dialog of one
   and a window that is no app's (the context area empty), a window in
   the background changing without repainting the bar, a connection
   lost and back, the service stopped and started; the key grabs going
   with the connection and the service, checked with X itself.
5b. The keys the shell takes, on real unmapped windows of the test's
   own, the service's X connection wrapped in a recorder (keyboard and
   pointer grabs recorded and never sent): GrabKey for a bar's chords
   by the keycodes the keymap gives (an oracle read on another
   connection), once per combination of Caps Lock and Num Lock, and none
   for a bar with no inputs; another client's grab of one chord refused
   (the warning once, that chord left to the app); a press delivered
   (the keyboard held first, the pointer on the bar's window, the input
   open in the section, keys typed after it into its box in order, the
   chord again selecting its text, Escape closing it and giving both
   back); a menu; no top bar (fallback); `open` with no top bar; a
   keymap change re-making the grabs; the grabs released with the
   window.
6. Whether a top bar is over an app's window (real unmapped windows of
   the test's own) and where its list drops: the section's left, the
   bar's bottom edge, an item's left for a menu; the bar on another
   monitor, the window gone; a bar spanning the screen asking nothing.
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
   The browser announces its window, and the service grabs its bar's
   chords (another client's grab of them refused, so X itself says so);
   its bar reaches the service; the context area draws it; an event of
   the bar's switches its tab; the live path of its address input: the
   real Daemon, a top bar of the test's own (a small window in the
   corner over the browser's: no dock, no strut) and the real drop-down
   window, Ctrl+L delivered to the service as X reports it (the grabs
   recorded, never made), the browser answering from a history seeded
   with addresses far past 128 characters and a title past 1000, the
   list mapped flush under that bar and read back, "yo" typed, Escape;
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
    ws.attach(FakeWindow())
    follower = ws._follower                      # the X11 one (indigoshell/wm/x11.py)
    real_handler, real_refresh = follower._on_client_property, follower._refresh

    def spy_refresh():
        refreshed.append(1)
        real_refresh()

    follower._refresh = spy_refresh
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
    follower._clients_dirty = True
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


def paint(widget, w: int, h: int, *, rule: bool = False, settle: bool = True):
    """`widget` laid out in w x h on the bar's background (with `rule`,
    and the top bar's rule along the bottom edge, as its window draws it)
    and painted off screen (CPU raster), as RGB; with `settle`, whatever
    in it moves (Flux's tabs) at rest first."""
    import skia

    from indigoshell import theme
    from indigoshell.widgets.base import Size
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.clear(theme.color(theme.BAR_BG))
    if rule:
        line = skia.Paint()
        line.setColor(theme.color(theme.BAR_RULE))
        canvas.drawRect(skia.Rect.MakeXYWH(0, h - theme.BAR_RULE_THICK, w,
                                           theme.BAR_RULE_THICK), line)
    widget.measure(Size(w, h))
    widget.arrange(skia.Rect.MakeWH(w, h))
    if settle:
        for each in widget.walk():
            if hasattr(each, "settle"):
                each.settle()
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
               progress=100, bookmarked=False, find=None, ids=None) -> dict:
    """A Flux window's state (flux/bar.py)."""
    from flux import bar as fb
    ids = ids or [101 + i for i in range(len(titles))]
    tabs = [fb.tab(t, host, loading and i == current, id_=n)
            for i, (t, n) in enumerate(zip(titles, ids))]
    return fb.state(tabs, current, progress, secure, url=f"http://{host}/",
                    bookmarked=bookmarked, find=find)


def flux_bar(titles, current=0, *, single=False, **kw) -> dict:
    """The bar Flux sends for a window with these tabs (flux/bar.py):
    a main window's with its inputs and menu (Ctrl+L, Ctrl+T, Ctrl+F,
    Ctrl+M), a single one's with none."""
    from flux import bar as fb
    return fb.bar_of(flux_state(titles, current, **kw), single=single)


def announce(wid: int, app: str = "flux", window=None) -> dict:
    """An app window's announce: the app, its own id for the window (the
    X window's here), its X window."""
    from indigoshell.applink import bar as B
    return B.announce(app, wid if window is None else window, wid)


def titles_of(window) -> list[str]:
    """The tab titles of the bar an app window shows."""
    bar = window.bar or {}
    tabs = next((i for i in bar.get("start", []) if i.get("kind") == "tabs"), {})
    return [t["title"] for t in tabs.get("tabs", [])]


def app_bar(titles, current=0, *, host="127.0.0.1", secure=False, progress=None,
            busy=(), bookmarked=False, ids=None, can=("close", "move", "detach")) -> dict:
    """A bar as an app with tabs and a host sends it (applink/bar.py's
    builders): the tabs, a box each; the host after its marks."""
    from indigoshell.applink import bar as B
    ids = ids or [101 + i for i in range(len(titles))]
    tabs = [B.tab(n, t, busy=i in busy or (progress is not None and i == current),
                  progress=progress if i == current else None)
            for i, (t, n) in enumerate(zip(titles, ids))]
    spans = ([B.span(icon="bookmark")] if bookmarked else []) + (
        [] if secure else [B.span(icon="unlocked", color="error")]) + [B.span(host)]
    return B.bar([B.tabs("tabs", tabs, current=ids[current], can=can)],
                 [B.text("host", spans=spans)])


def stand_in_bar(width=1800, left=12, bottom=None):
    """The top bar as the app service sees it (its context area's window
    and rect), across a 2560 px monitor at the origin: no window of it is
    made here."""
    import types

    import skia

    from indigoshell import theme
    bottom = bottom or theme.TOP_BAR_HEIGHT
    window = types.SimpleNamespace(wid=1, x=0, y=0, width=2560, height=bottom,
                                   content_root=(0, 0, 2560, bottom))
    return types.SimpleNamespace(window=window, rect=skia.Rect.MakeXYWH(left, 0, width, bottom))


def host_layout(view) -> dict:
    """The host item as drawn: its text, where, the marks before it."""
    pieces = view.item("host").layout()
    marks = [p for p in pieces if p["icon"]]
    words = [p for p in pieces if not p["icon"]]
    w = words[-1] if words else {"text": "", "x": 0.0, "w": 0.0}
    book = next((p for p in marks if p["text"].startswith("\uf02e")), None)
    lock = next((p for p in marks if p["text"].startswith("\uf09c")), None)
    return {"text": w["text"], "x": w["x"], "w": w["w"], "mark_w": lock["w"] if lock else 0.0,
            "secure": lock is None, "bookmarked": book is not None,
            "bookmark_x": book["x"] if book else 0.0, "bookmark_w": book["w"] if book else 0.0}


# ── 4. Flux's content ───────────────────────────────────────────────────

def test_content(scratch: Path) -> None:
    import types

    from indigoshell import theme
    from indigoshell.applink.kinds import tabs as tabs_mod
    from indigoshell.applink.kinds.input import InputBox
    from indigoshell.applink.kinds.look import (BRACKET_FG, COMET_FPS, COMET_S, LOAD_FG,
                                                MOTION_FPS, NUMBER_CURRENT_FG, NUMBER_FG,
                                                OUTLINE, SLIDE_S, SLOT_GAP, SLOT_MIN, SLOT_W,
                                                SWITCH_S, TAB_CURRENT_FG, TAB_FG, TAB_HOVER_FG,
                                                WIPE_S, ICONS, ROLE_COLORS)
    from indigoshell.applink.kinds.tabs import fit_slots
    from indigoshell.applink.view import BarView

    HOST_FG, INSECURE_FG = theme.FG, theme.ERROR
    FIND_QUERY_FG, FIND_COUNT_FG, NO_MATCHES_FG = theme.FG_STRONG, ROLE_COLORS["info"], theme.ERROR
    section("4. an app's bar, painted off screen: its tabs, a box each, and its host")
    W, H = 1600, theme.TOP_BAR_HEIGHT

    class App:
        """An app window's section, as the shell keeps it: a BarView."""

        def __init__(self) -> None:
            self.events = []
            self.view = self.content = BarView(emit=self.events.append)

        def on_message(self, bar: dict) -> None:
            self.view.set_bar(bar)

    def flux():
        j = App()
        host = Host()
        j.content.attach(host)
        return j, host

    def in_rect(img, x, w, top=9, bottom=34):
        """Columns x..x+w, rows top..bottom: a title's or a number's band,
        clear of the box's top edge."""
        return img[top:bottom, max(0, int(x)):int(x + w) + 1]

    def rows_at(img, x, top, bottom):
        return img[top:bottom, int(x):int(x) + 1]

    j, host = flux()
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"]))
    tl = j.view.item("tabs")
    img = paint(j.content, W, H)
    save(scratch / "content-tabs.png", img)
    tabs = tl.layout()
    strong = [count_color(in_rect(img, t["x"], t["w"]), TAB_CURRENT_FG) for t in tabs]
    muted = [count_color(in_rect(img, t["x"], t["w"]), TAB_FG) for t in tabs]
    numbers = [count_color(in_rect(img, t["number_x"], t["number_w"], 12, 32), c, 30)
               for t, c in zip(tabs, (NUMBER_CURRENT_FG, NUMBER_FG, NUMBER_FG))]
    brackets = [count_color(in_rect(img, t["number_x"], t["number_w"], 12, 33), BRACKET_FG, 20)
                for t in tabs]
    slots = [(t["slot_x"], t["slot_w"]) for t in tabs]
    check("three tabs from the left, each in a slot of the same width: the current one's title "
          "strong, its number cyan in corner brackets; the others' titles and numbers muted, "
          "no brackets", [t["text"] for t in tabs] == ["1 Alpha", "2 Beta", "3 Gamma"]
          and tabs[0]["slot_x"] < 8 and all(w == SLOT_W for _x, w in slots)
          and slots[1][0] - slots[0][0] == slots[2][0] - slots[1][0] > SLOT_W
          and strong[0] > 20 and muted[0] < 5 and all(m > 20 for m in muted[1:])
          and all(v < 5 for v in strong[1:]) and all(n > 8 for n in numbers)
          and brackets[0] > 12 and brackets[1] == brackets[2] == 0,
          f"strong {strong}, muted {muted}, numbers {numbers}, brackets {brackets}")
    x0, w0 = slots[0]
    edges = [count_color(rows_at(img, x + 1, 8, 34), OUTLINE, 30) for x, _w in slots]
    dim = [int(img[8:34, int(x):int(x) + 2, 0].mean()) for x, _w in slots]
    top_ = count_color(img[5:8, int(x0 + 8):int(x0 + w0 - 12)], OUTLINE, 30)
    gap = img[8:34, int(x0 + w0) + 2:int(slots[1][0]) - 1]
    check("each tab a box of its own: the current one's outline bright, the others' dim (no "
          "bright pixel), boxes apart; nothing along the bar's bottom edge",
          edges[0] >= 24 and top_ > (w0 - 20) and edges[1] == edges[2] == 0
          and 30 < dim[1] < 120 and dim[2] == dim[1]
          and share_of(gap, hex_rgb(theme.BAR_BG), 25) > 0.9
          and count_color(img[H - 3:], OUTLINE, 30) == 0,
          f"edges {edges}, dim red {dim}, top {top_}")
    h = host_layout(j.view)
    mark = count_color(in_rect(img, h["x"] - h["mark_w"], h["mark_w"] - 1, 0, 34), INSECURE_FG)
    hpx = count_color(in_rect(img, h["x"], h["w"], 0, 34), HOST_FG)
    check("the host at the right end, after the open padlock (plain http) in the error colour",
          h["text"] == "127.0.0.1" and not h["secure"] and h["mark_w"] > 0 and mark > 10
          and hpx > 20 and h["x"] + h["w"] > W - 20 and h["bookmark_w"] == 0,
          f"{h['text']!r} ends at {h['x'] + h['w']:.0f} of {W}; padlock px {mark}, host px {hpx}")
    bright = count_color(img, LOAD_FG, 30)
    check("nothing loading: nothing bright", bright == 0 and tl.progress is None,
          f"{bright} px")

    host.damaged.clear()
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"]))
    same = list(host.damaged)
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], 1))
    check("the same bar again damages nothing; a change does", same == [] and host.damaged,
          f"{len(same)} then {len(host.damaged)} damage(s)")

    titles = [f"A rather long page title number {i} about nothing in particular"
              for i in range(1, 13)]
    j.on_message(app_bar(titles, 5))
    img = paint(j.content, 900, H)
    save(scratch / "content-many.png", img)
    tabs = tl.layout()
    room = tl.rect
    numbered = all(t["text"].startswith(str(t["index"] + 1)) for t in tabs)
    ends = max(t["slot_x"] + t["slot_w"] for t in tabs)
    inside = all(t["x"] + t["w"] <= t["slot_x"] + t["slot_w"] for t in tabs)
    current = next((t for t in tabs if t["current"]), {})
    idx = [t["index"] for t in tabs]
    check("twelve long titles in 900 px: slots of the narrowest width, the run of them that fits "
          "around the current one, each title cut with an ellipsis inside its slot, the "
          "current one still strong",
          len(tabs) == int((room.width() + SLOT_GAP) // (SLOT_MIN + SLOT_GAP))
          and idx == list(range(idx[0], idx[-1] + 1))
          and 5 in idx and numbered and inside and all("…" in t["text"] for t in tabs)
          and all(t["slot_w"] == SLOT_MIN for t in tabs) and ends <= room.right() + 0.5
          and current.get("index") == 5
          and count_color(in_rect(img, current["x"], current["w"]), TAB_CURRENT_FG) > 20,
          f"{idx}: {tabs[0]['text']!r}; ends at {ends:.0f}, room to {room.right():.0f}")

    g = SLOT_GAP
    fits = [fit_slots(3, 0, 1600), fit_slots(12, 5, 1600), fit_slots(30, 20, 1600),
            fit_slots(30, 0, 1600), fit_slots(30, 29, 1600), fit_slots(2, 1, 50)]
    first, count, w = fits[2]
    check("the slots: SLOT_W while they fit; narrower alike as more come (12 in 1600 px); "
          "SLOT_MIN at the least, then the run around the current tab (kept inside the list "
          "at either end); one, as wide as the room, when not even that fits",
          fits[0] == (0, 3, SLOT_W) and fits[1][:2] == (0, 12) and SLOT_MIN <= fits[1][2] < SLOT_W
          and 12 * fits[1][2] + 11 * g <= 1600 and w == SLOT_MIN and first <= 20 < first + count
          and count == (1600 + g) // (SLOT_MIN + g) and fits[3][0] == 0
          and fits[4][0] + fits[4][1] == 30 and fits[5] == (1, 1, 50), f"{fits}")

    j.on_message(app_bar(["Alpha"], secure=True, host="example.org", bookmarked=True))
    img = paint(j.content, W, H)
    h = host_layout(j.view)
    glyph = count_color(in_rect(img, h["bookmark_x"], h["bookmark_w"] - 1, 0, 34), HOST_FG)
    check("a bookmarked page over https: the bookmark glyph before the host, no padlock",
          h["bookmarked"] and h["bookmark_w"] > 0 and glyph > 15 and h["mark_w"] == 0
          and h["text"] == "example.org", f"glyph px {glyph}")
    save(scratch / "content-bookmarked.png", img)

    box = InputBox()
    box.show({"icon": "search", "placeholder": "find"})

    def find_line(query, count, typing=True, color=FIND_COUNT_FG):
        j.on_message(app_bar(["Alpha", "Beta"]))
        box.field.set_text(query, notify=False)
        box.set_stepping(not typing)
        box.set_count((count, color) if count else None)
        j.view.show_box(box)
        img_ = paint(j.content, W, H)
        return img_, {p["role"]: p for p in box.layout()}

    img, parts = find_line("cats", "3/12")
    save(scratch / "content-find-typing.png", img)
    q, mark_, count_ = parts.get("query", {}), parts.get("mark", {}), parts.get("count", {})
    caret_x = q.get("x", 0) + q.get("w", 0)
    check("an input open (find, typing): one lit box the section's width in the items' place, "
          "the search mark in the bracketed spot, `cats` strong with the caret after it, `3/12` "
          "small and cyan at the box's right end; the host gone meanwhile",
          mark_.get("text") == ICONS["search"] and q.get("text") == "cats" and q.get("caret")
          and count_.get("text") == "3/12"
          and count_color(in_rect(img, q["x"], q["w"]), FIND_QUERY_FG) > 20
          and count_color(in_rect(img, caret_x, 3), FIND_QUERY_FG, 30) > 8
          and count_color(in_rect(img, count_["x"], count_["w"]), FIND_COUNT_FG, 30) > 10
          and count_["x"] + count_["w"] > W - 20
          and count_color(rows_at(img, 1, 8, 34), OUTLINE, 30) >= 24
          and count_color(rows_at(img, W - 2, 16, 34), OUTLINE, 30) >= 15
          and j.view.children() == (box,),
          f"{[p['text'] for p in parts.values()]}")
    img, parts = find_line("cats", "4/12", typing=False)
    q = parts.get("query", {})
    check("stepping: the text stays, no caret", q.get("text") == "cats" and not q.get("caret")
          and count_color(in_rect(img, q["x"] + q["w"] + 1, 3), FIND_QUERY_FG, 30) == 0
          and parts.get("count", {}).get("text") == "4/12")
    img, parts = find_line("zzz", "no matches", color=NO_MATCHES_FG)
    save(scratch / "content-no-matches.png", img)
    nm = parts.get("count", {})
    check("no matches: said in the error colour", nm.get("text") == "no matches"
          and count_color(in_rect(img, nm["x"], nm["w"]), NO_MATCHES_FG) > 20,
          f"{nm.get('text')!r}")
    img, parts = find_line("cats", None)
    check("before the app has counted: no count", "count" not in parts)
    j.view.show_box(None)
    img = paint(j.content, W, H)
    check("closed: the items back as they were", j.view.children()[0] is tl
          and count_color(img, TAB_CURRENT_FG) > 20)

    j.on_message(app_bar(["Alpha", "Beta"], progress=40))
    img = paint(j.content, W, H)
    save(scratch / "content-loading.png", img)
    x0, w0 = tl.layout()[0]["slot_x"], tl.layout()[0]["slot_w"]
    left = count_color(rows_at(img, x0 + 1, 8, 34), LOAD_FG, 30)
    right = count_color(rows_at(img, x0 + w0 - 1, 16, 34), LOAD_FG, 30)
    check("the current tab loading at 40%: its outline lit round from the bottom-left corner "
          "(its left edge bright), not yet down the right; no line of its own",
          left >= 24 and right == 0 and tl.progress == 40,
          f"left edge {left}, right edge {right}")
    j.on_message(app_bar(["Alpha", "Beta"]))
    img = paint(j.content, W, H)
    check("done: nothing bright left", count_color(img, LOAD_FG, 30) == 0)

    # The rule's comet under a tab behind that loads, at a moment of its run.
    j.on_message(app_bar(["Alpha", "Beta"], busy=(1,)))
    real = tabs_mod.time
    tabs_mod.time = types.SimpleNamespace(monotonic=lambda: 1000 * COMET_S + 0.5 * COMET_S)
    try:
        img = paint(j.content, W, H)
    finally:
        tabs_mod.time = real
    save(scratch / "content-comet.png", img)
    x1, w1 = tl.layout()[1]["slot_x"], tl.layout()[1]["slot_w"]
    comet = count_color(img[:, int(x1):int(x1 + w1)], LOAD_FG, 50)
    elsewhere = count_color(img[:, :int(x1) - 4], LOAD_FG, 50)
    check("a tab behind busy: a comet going round its own box, and only there; the bar's "
          "clock asked for it (COMET_FPS) only while it is busy",
          comet > 20 and elsewhere == 0 and tl.animation_fps == COMET_FPS,
          f"under it {comet}, elsewhere {elsewhere}")
    j.on_message(app_bar(["Alpha", "Beta"]))
    paint(j.content, W, H)
    idle = tl.animation_fps

    # Motion: a switch slides the raised part; a close wipes the tab out.
    import time as time_
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], 0))
    paint(j.content, W, H)
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], 2))
    due = tl.animation_fps
    paint(j.content, W, H, settle=False)
    start = tl._front.at(time_.monotonic())
    moving = tl.animation_fps
    later = tl._front.at(time_.monotonic() + SWITCH_S)
    check("switching: the bar's clock asked for frames at once, the current tab's light sliding "
          "from the old box to the new one over SWITCH_S, then nothing asked (idle: 0)",
          due == moving == MOTION_FPS and start < 50 and later == 2 * tl.pitch and idle == 0,
          f"due {due}, moving {moving}, from {start:.0f} to {later:.0f}, idle {idle}")
    paint(j.content, W, H)
    j.on_message(app_bar(["Alpha", "Gamma"], 0, ids=[101, 103]))
    paint(j.content, W, H, settle=False)
    leaving = [(i, t) for _s, i, t, _x in tl._leaving]
    gamma = tl._glides[103]
    now = time_.monotonic()
    held = gamma.at(now + WIPE_S * 0.5)
    after = gamma.at(now + WIPE_S + SLIDE_S)
    check("a tab closed: it wipes out from its place (WIPE_S), and only then the ones after it "
          "slide over to close the gap", leaving == [(1, "Beta")] and held == 2 * tl.pitch
          and after == tl.pitch and tl.animation_fps == MOTION_FPS,
          f"leaving {leaving}, Gamma at {held:.0f} then {after:.0f}")
    paint(j.content, W, H)

    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], 0))
    paint(j.content, W, H)
    tl.set_hovered(True)
    tl.motion(tl.layout()[2]["slot_x"] + 60, 20)
    img = paint(j.content, W, H)
    save(scratch / "content-hover.png", img)
    t2 = tl.layout()[2]
    lit = count_color(in_rect(img, t2["x"], t2["w"]), TAB_HOVER_FG, 15)
    t1 = tl.layout()[1]

    def edge(t):            # how red a box's left edge is
        x = int(t["slot_x"])
        return int(img[8:34, x:x + 2, 0].mean())
    hovered_edge, plain_edge = edge(t2), edge(t1)
    tl.set_hovered(False)
    check("hover: the tab's title brighter, its outline a step brighter than the others'",
          lit > 15 and hovered_edge > plain_edge + 30,
          f"title px {lit}, edge red {hovered_edge} against {plain_edge}")


# ── 4b. the mouse on Flux's tabs ────────────────────────────────────────

class StubDisplay:
    """What a Window needs of its display for input, no X: every
    keyboard grab and ungrab counted (never sent), the pointer's buttons
    as a test sets them, Escape and "a" as the keymap."""

    def __init__(self) -> None:
        import types
        self.grabs = self.ungrabs = self.pumps = 0
        self.refuse = False
        self.buttons = 0                # the pointer's button mask, as QueryPointer says
        self.root = 1
        self.screen = types.SimpleNamespace(root_depth=24)
        query = types.SimpleNamespace(
            QueryPointer=lambda root: types.SimpleNamespace(
                reply=lambda: types.SimpleNamespace(mask=self.buttons)))
        self.conn = types.SimpleNamespace(core=query)

    def grab_keyboard(self, wid: int) -> bool:
        if self.refuse:
            return False
        self.grabs += 1
        return True

    def ungrab_keyboard(self) -> None:
        self.ungrabs += 1

    def pump(self) -> int:
        self.pumps += 1
        return 0

    def keysym(self, keycode: int, state: int = 0) -> int:
        return 0xFF1B if keycode == 9 else 0x61

    def mod_mask(self, role: str) -> int:
        return 0

    @property
    def held(self) -> int:
        return self.grabs - self.ungrabs


def x_event(kind: str, wid: int, x: int, y: int, detail: int = 0):
    """A real xcffib event, built from its wire bytes: a button press or
    release, a motion, a key press, or a leave."""
    import struct

    import xcffib
    import xcffib.xproto as xp
    code, cls = {"press": (4, xp.ButtonPressEvent), "release": (5, xp.ButtonReleaseEvent),
                 "motion": (6, xp.MotionNotifyEvent), "key": (2, xp.KeyPressEvent),
                 "leave": (8, xp.LeaveNotifyEvent)}[kind]
    raw = struct.pack("=BBHIIIIhhhhHBB", code, detail, 0, 0, 1, wid, 0, x, y, x, y, 0, 1, 0)
    ev = cls(xcffib.MemoryUnpacker(raw))
    ev.response_type = code         # the connection sets it on events it reads
    return ev


def test_mouse(scratch: Path) -> None:
    import asyncio

    import skia

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.kinds.look import (OUTLINE, SLOT_GAP, SLOT_W, TAB_CURRENT_FG,
                                                TAB_HOVER_FG, TEAR_FG)
    from indigoshell.applink.kinds.tabs import DRAG_START_PX, TEAR_OUT_PX, landing, slot_at
    from indigoshell.applink.view import BarView
    from indigoshell.widgets.base import Size, Widget
    from indigoshell.widgets.layout import Align, Row
    from indigoshell.window import Window, WindowSpec

    section("4b. the mouse on an app's tabs: the bar's window driven with synthetic X events "
            "(no X: the keyboard grabs counted, never made); events by id")
    W, H = 1600, theme.TOP_BAR_HEIGHT
    pitch = SLOT_W + SLOT_GAP
    check("slots by x: each gap goes with the slot before it, nothing past the last; a dragged "
          "tab lands in the slot its middle is over",
          [slot_at(x, 3, 240) for x in (0, 239, 240, pitch - 1, pitch, 2 * pitch + 239,
                                        2 * pitch + 240, 900, -1)]
          == [0, 0, 0, 0, 1, 2, None, None, None]
          and [landing(at, 3, 240) for at in (0, pitch - 121, pitch - 120, 2 * pitch - 121,
                                              2 * pitch - 120, 2000, -50)]
          == [0, 0, 1, 1, 2, 2, 0])

    import types
    sent: list = []
    service = types.SimpleNamespace(focused=None, context=None, follow=lambda cb: None,
                                    unfollow=lambda cb: None)
    j = types.SimpleNamespace(view=BarView(emit=sent.append))
    j.content = j.view
    service.focused = j
    # The bar's own widgets beside it, as they click today: one with a
    # handler, one that reads the position (as the workspaces do), one
    # that drags (as the call's plate does).
    seen: list = []

    class Spot(Widget):
        def __init__(self, width, **kw):
            super().__init__(**kw)
            self.width = width

        def measure(self, avail):
            return Size(self.width, avail.height)

        def paint(self, canvas):
            pass

    class Where(Spot):
        def hit(self, x, y):
            return self if self.rect.contains(x, y) else None

        def click(self, button, x=0.0, y=0.0):
            seen.append(("where", button, x, y))
            return True

    plain = Spot(40, on_left_click=lambda w: seen.append("left"),
                 on_middle_click=lambda w: seen.append("middle"),
                 on_scroll_up=lambda w: seen.append("up"))
    where = Where(40)
    drags = Spot(40, on_drag=lambda w, x, y, commit: seen.append(("drag", x, commit)))
    row = Row([AppContext(service), plain, where, drags], spacing=20, align=Align.CENTER)
    loop = asyncio.new_event_loop()
    display = StubDisplay()
    win = Window(display, WindowSpec(name="test-top-bar", content=row, focusable=False), loop)
    win.wid, win.x, win.y = 0x500001, 0, 0
    dismissed = []
    win._dismiss_others = lambda: dismissed.append(1)
    closes = []
    win._request_close = lambda: closes.append(1)
    row.attach(win)

    def lay_out():
        row.measure(Size(W, H))
        row.arrange(skia.Rect.MakeWH(W, H))

    def state(titles, current=0, ids=None):
        return app_bar(titles, current, host="x.org", secure=True, ids=ids)

    j.on_message = j.view.set_bar
    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    tl = j.view.item("tabs")

    def send(kind, x, y, detail=0):
        win._on_event(x_event(kind, win.wid, int(x), int(y), detail))

    def mid(i):
        return tl.rect.left() + i * pitch + SLOT_W / 2

    def since(n):
        return [m for m in sent[n:]]

    picked = [win._pick(mid(i), H / 2) is tl for i in range(4)]
    beyond = win._pick(tl.rect.left() + 4 * pitch + 10, H / 2)
    check("the bar's window finds the tab list through the context area, over each tab and "
          "not past the last", all(picked) and beyond is None
          and [tl.tab_at(mid(i)) for i in range(4)] == [0, 1, 2, 3], f"{picked}, {beyond}")

    n = len(sent)
    send("press", mid(1), 20, 1)
    send("release", mid(1), 20, 1)
    send("press", mid(2), 20, 3)
    send("release", mid(2), 20, 3)
    send("press", mid(0), 20, 1)            # the current one: nothing to switch to
    send("release", mid(0), 20, 1)
    check("a left and a right click select the tab, named by its id (no keyboard taken, open "
          "panels dismissed as a press on the bar always did); the current one sends nothing",
          since(n) == [{"type": "select", "item": "tabs", "tab": 102},
                       {"type": "select", "item": "tabs", "tab": 103}]
          and display.grabs == 0 and win.holding is None and len(dismissed) == 3,
          f"{since(n)}")

    n = len(sent)
    send("press", mid(3), 20, 2)
    send("release", mid(3) + 30, 25, 2)
    send("press", mid(3), 20, 2)
    send("release", mid(2), 20, 2)
    send("press", mid(3), 20, 2)
    send("release", mid(3), H + 30, 2)
    check("a middle click closes the tab under it, let go over the same tab; let go over "
          "another, or off the bar, nothing", since(n) == [{"type": "close", "item": "tabs", "tab": 104}],
          f"{since(n)}")

    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(0) + DRAG_START_PX - 2, 21)
    still = tl.lift_layout() is None and display.grabs == 0
    send("motion", mid(0) + DRAG_START_PX + 1, 21)
    lifted = tl.lift_layout()
    send("motion", mid(2) + 10, 22)
    over = tl.lift_layout()
    placed = {i: x for i, x in tl.arrangement()[0]}
    img = paint_window(win, W, H)
    save(scratch / "mouse-drag.png", img)
    mx = int(over["slot_x"] - SLOT_GAP / 2)
    mark = count_color(img[36:41, mx - 1:mx + 2], OUTLINE, 30)
    lift_px = count_color(img[:, int(over["x"]):int(over["x"] + SLOT_W)], TAB_CURRENT_FG, 40)
    send("release", mid(2) + 10, 22, 1)
    check("a press moved less than the threshold is still a click; past it the tab lifts (the "
          "keyboard taken once, for Escape), follows the pointer, the others close up, a marker "
          "between two boxes where it lands; let go: `move` before the tab it lands before, the "
          "keyboard given back",
          still and lifted is not None and lifted["to"] == 0 and display.grabs == 1
          and over["to"] == 2 and placed == {1: 0, 2: pitch, 3: 2 * pitch}
          and mark > 4 and lift_px > 20
          and since(n) == [{"type": "move", "item": "tabs", "tab": 101, "before": 104}]
          and display.held == 0
          and win.holding is None, f"{over}, {placed}, marker px {mark}, {since(n)}")

    j.on_message(state(["Beta", "Gamma", "Alpha", "Delta"], 2, [102, 103, 101, 104]))
    lay_out()
    n = len(sent)
    send("press", mid(2), 20, 1)
    send("motion", mid(2) - 2 * pitch, 20)
    send("release", mid(2) - 2 * pitch, 20, 1)
    send("press", mid(1), 20, 1)
    send("motion", mid(1) + 3, 22)          # under the threshold, then back
    send("release", mid(1), 20, 1)
    send("press", mid(3), 20, 1)
    send("motion", mid(3) + 40, 20)
    send("release", mid(3) + 40, 20, 1)     # dropped where it was
    check("dragged back to the front: `move` before the first; a press barely moved: only the "
          "select; dropped where it was: nothing more", since(n) == [
              {"type": "move", "item": "tabs", "tab": 101, "before": 102},
              {"type": "select", "item": "tabs", "tab": 103},
              {"type": "select", "item": "tabs", "tab": 104}] and display.held == 0,
          f"{since(n)}")

    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    n = len(sent)
    send("press", mid(1), 20, 1)
    send("motion", mid(1) + 20, H + 20)
    near = tl.lift_layout()
    send("motion", mid(1) + 20, H + TEAR_OUT_PX + 5)
    far = tl.lift_layout()
    others = sorted(x for _i, x in tl.arrangement()[0])
    img = paint_window(win, W, H)
    save(scratch / "mouse-tear-out.png", img)
    cue = count_color(img[:, int(far["x"] + SLOT_W):int(far["x"] + SLOT_W) + 120], TEAR_FG, 30)
    send("release", mid(1) + 20, H + TEAR_OUT_PX + 5, 1)
    check("dragged below the bar: within TEAR_OUT_PX still a move; past it the others close up, "
          "no marker, NEW WINDOW by the lifted tab; let go: `detach` (no coordinates: where it "
          "goes is the window manager's)", near["out"] is False and far["out"] is True
          and others == [0, pitch, 2 * pitch] and cue > 10
          and since(n) == [{"type": "select", "item": "tabs", "tab": 102},
                           {"type": "detach", "item": "tabs", "tab": 102}]
          and display.held == 0, f"{since(n)}, new-window mark px {cue}")

    n = len(sent)
    send("press", mid(1), 20, 1)
    send("motion", mid(1) + 20, H + 20)
    send("release", mid(1) + 20, H + 20, 1)
    check("let go between the bar and the tear-out line: a move (here, to where it was: "
          "nothing)", since(n) == [{"type": "select", "item": "tabs", "tab": 102}]
          and display.held == 0)

    j.on_message(state(["Solo"]))
    lay_out()
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(0) + 30, H + TEAR_OUT_PX + 50)
    solo = tl.lift_layout()
    send("release", mid(0) + 30, H + TEAR_OUT_PX + 50, 1)
    check("a window's only tab is never torn out: no mark, nothing sent",
          solo is not None and not solo["out"] and since(n) == [] and display.held == 0,
          f"{solo} {since(n)}")

    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(3), 20)
    send("key", 0, 0, 10)                   # "a": swallowed, the drag goes on
    going = tl.lift_layout() is not None
    send("key", 0, 0, 9)                    # Escape
    gone = tl.lift_layout() is None and win.holding is None and display.held == 0
    send("motion", mid(3) + 5, 20)
    send("release", mid(3), 20, 1)
    send("key", 0, 0, 9)                    # late: the hold is over
    check("Escape while dragging puts the tab back: the keyboard given back at once, the "
          "release after it does nothing, another key in the drag is swallowed, and an Escape "
          "after the hold reaches nothing (never the window's Escape-closes)",
          going and gone and since(n) == [] and closes == [] and display.held == 0,
          f"{since(n)}")

    display.refuse = True
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(1) + 10, 20)
    send("release", mid(1) + 10, 20, 1)
    display.refuse = False
    check("the keyboard refused: the drag works without Escape, nothing to give back",
          since(n) == [{"type": "move", "item": "tabs", "tab": 101, "before": 103}]
          and display.ungrabs == display.grabs)

    # Every way out of a hold gives the keyboard back.
    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    outs = {}

    def lift_at(i):
        send("press", mid(i), 20, 1)
        send("motion", mid(i) + 40, 20)
        return display.held == 1 and win.holding is tl

    began = lift_at(0)
    j.on_message(state(["Beta", "Gamma", "Delta"], 0, [102, 103, 104]))
    outs["its tab closed meanwhile"] = began and display.held == 0 and win.holding is None \
        and tl.drag is None
    n = len(sent)
    send("release", mid(1), 20, 1)
    outs["... and its release then does nothing"] = since(n) == []
    lay_out()

    began = lift_at(0)
    ctx = row.children()[0]
    ctx._on_focus(None)
    outs["the app window gone (its content taken off the bar)"] = began and display.held == 0 \
        and win.holding is None and tl.window is None
    send("motion", mid(1), 20)
    send("release", mid(1), 20, 1)
    service.focused = j
    ctx._on_focus(j)
    lay_out()

    real_motion = tl.motion

    def broken(x, y):
        raise RuntimeError("a bug in motion")
    began = lift_at(0)
    tl.motion = broken
    send("motion", mid(1), 20)
    tl.motion = real_motion
    outs["an exception in its handler"] = began and display.held == 0 and win.holding is None \
        and tl.drag is None
    send("release", mid(1), 20, 1)

    began = lift_at(0)
    display.buttons = 1 << 8                # still down
    win._check_hold()
    kept = win.holding is tl and win._hold_check is not None
    display.buttons = 0                     # up, and no release ever came
    win._check_hold()
    outs["its release never seen (the button found up)"] = began and kept \
        and display.held == 0 and win.holding is None and display.pumps >= 2
    send("release", mid(1), 20, 1)

    began = lift_at(0)
    win._end_hold(cancel=True)              # what park, dismiss and destroy call first
    outs["the window parked, dismissed or destroyed"] = began and display.held == 0 \
        and tl.drag is None and win._hold_check is None
    send("release", mid(1), 20, 1)
    for name, ok in outs.items():
        check(f"the keyboard given back: {name}", ok)
    check("every grab given back, none twice", display.grabs == display.ungrabs > 0,
          f"{display.grabs} grabs, {display.ungrabs} ungrabs")

    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    send("motion", mid(2), 20)
    hovered = tl.hover
    img = paint_window(win, W, H)
    save(scratch / "mouse-hover.png", img)
    t2 = next(t for t in tl.layout() if t["index"] == 2)
    lit = count_color(img[:, int(t2["x"]):int(t2["x"] + t2["w"])], TAB_HOVER_FG, 15)
    send("leave", 0, 0)
    check("hover: the tab under the pointer a step brighter; gone when the pointer leaves",
          hovered == 2 and lit > 10 and tl.hover is None, f"hover px {lit}")

    # The bar's other widgets, exactly as before.
    seen.clear()
    n = len(sent)
    px, wx, dx = (int(w_.rect.centerX()) for w_ in (plain, where, drags))
    send("press", px, 20, 1)
    on_press = list(seen)
    send("release", px, 20, 1)
    send("press", px, 20, 2)
    send("release", px, 20, 2)
    send("press", px, 20, 4)
    send("press", wx, 21, 3)
    send("release", wx, 21, 3)
    send("press", dx, 20, 1)
    send("motion", dx + 300, 20)
    send("release", dx + 300, 20, 1)
    check("the bar's other widgets click as before: on the press, by handler; one reading the "
          "position gets button and place; a slider drags to its commit; none holds the press "
          "or takes the keyboard", on_press == ["left"] and seen == [
              "left", "middle", "up", ("where", 3, wx, 21), ("drag", dx, False),
              ("drag", dx + 300, False), ("drag", dx + 300, True)]
          and since(n) == [] and win.holding is None and win._pressed is None,
          f"{seen}")
    loop.close()


def paint_window(win, w: int, h: int):
    """The window's content painted off screen, as RGB."""
    import skia

    from indigoshell import theme
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.clear(theme.color(theme.BAR_BG))
    for each in win.content.walk():
        if hasattr(each, "settle"):
            each.settle()
    win.content.paint(canvas)
    return rgb_of(surface.makeImageSnapshot())


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




async def test_service(scratch: Path) -> None:
    import types

    import xcffib.xproto as xp

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.service import AppService, AppWindow
    from indigoshell.backend.x11 import Display
    from indigoshell.applink import bar as Bv
    from indigoshell.applink.kinds.look import TAB_CURRENT_FG

    section("5. the app service: announces both ways, focus, losses, key grabs going and coming")
    rec = Records("indigoshell.applink.service", "indigoshell.applink.prompt")
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
    service = AppService(path=sock, display=display)
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

        a = await app(announce(A), flux_bar(["Alpha", "Beta"]))
        got = await a.answer()
        wa = await until(lambda: (w_ := service.window_for(A)) and len(titles_of(w_)) == 2 and w_)
        check("an app window (Flux's): answered {type: attached} with the voices' names; kept "
              "with its own id and its bar; its inputs' and menu's chords grabbed on it (Ctrl+L, "
              "Ctrl+T, Ctrl+F, Ctrl+M: X refuses another client the same grab of Ctrl+L)",
              got.get("type") == "attached" and got.get("voices", {}).get("lilac") == "Lilac"
              and isinstance(wa, AppWindow) and wa.kind == "flux" and wa.app_id == A
              and A in service.grabs
              and sorted(set(service.grabs[A].chords.values())) == ["ctrl+f", "ctrl+l", "ctrl+m",
                                                                     "ctrl+t"]
              and held_by_another(xw.conn, A, L, CTRL), f"{got}")
        b = await app(announce(B), flux_bar(["Gamma"]))
        await b.answer()
        wb = await until(lambda: (w_ := service.window_for(B)) and w_.bar and w_)

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
        texts = [t["text"] for t in wb.view.item("tabs").layout()]
        check("focus on the other Flux window: its tabs; the first's content let go (no window)",
              ctx.shown is wb.content and texts == ["1 Gamma"]
              and all(w_.window is None for w_ in wa.content.walk())
              and count_color(img, TAB_CURRENT_FG) > 20, f"{texts}")
        host.damaged.clear()
        a.send(flux_bar(["Alpha", "Beta", "Delta"]))
        await until(lambda: len(titles_of(wa)) == 3)
        quiet = list(host.damaged)
        b.send(flux_bar(["Gamma", "Epsilon"]))
        await until(lambda: len(titles_of(wb)) == 2)
        check("an app window in the background changing: the bar not repainted for it; the "
              "focused one changing: repainted", quiet == [] and bool(host.damaged),
              f"{len(quiet)} then {len(host.damaged)}")

        # A bad bar: ignored whole, the last good one kept, said once.
        mark = len(rec.records)
        bad_bar = dict(flux_bar(["Bad"]))
        bad_bar["start"] = [dict(bad_bar["start"][0], current=999)]
        b.send(bad_bar)
        b.send(bad_bar)
        b.send(Bv.answer("address", 1, [Bv.row("x", "nothing asked")]))
        b.send(flux_bar(["Gamma", "Epsilon"]))
        await asyncio.sleep(0.2)
        warned = [m for m in rec.at_least(logging.WARNING, mark) if "bar ignored" in m]
        check("a bar that breaks its shape (current naming no tab): ignored whole, the last good "
              "one kept, one warning for it (not again for the same problem); an answer for "
              "nothing open: dropped", titles_of(wb) == ["Gamma", "Epsilon"]
              and len(warned) == 1 and "current 999 names no tab" in warned[0], f"{warned}")

        # Chords follow the bar: one without find lets Ctrl+F go.
        F_ = keycodes_of(xw.conn, "f")[0]
        no_find = flux_bar(["Gamma", "Epsilon"])
        no_find = dict(no_find, inputs={k: v for k, v in no_find["inputs"].items() if k != "find"})
        b.send(no_find)
        regrabbed = await until(lambda: sorted(set(service.grabs[B].chords.values()))
                                == ["ctrl+l", "ctrl+m", "ctrl+t"], 2)
        check("a bar whose chords changed: the grabs follow (Ctrl+F let go, X gives it to another "
              "client again; Ctrl+L still the shell's)", bool(regrabbed)
              and not held_by_another(xw.conn, B, F_, CTRL) and held_by_another(xw.conn, B, L, CTRL))

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
        check("the focused window's connection lost: dropped, its bar with it, the context area "
              "empty; its key grabs let go (X gives another client the grab of Ctrl+L now)",
              bool(gone) and ctx.shown is None and service.focused is None
              and wa.closed and wb in service.windows and A not in service.grabs
              and not held_by_another(xw.conn, A, L, CTRL))
        a2 = await app(announce(A), flux_bar(["Alpha again"]))
        await a2.answer()
        wa2 = await until(lambda: (w_ := service.window_for(A)) and w_.bar and w_)
        check("... and back (a new connection, as after its X window was replaced): shown again, "
              "its keys grabbed again",
              wa2 is not None and wa2 is not wa and ctx.shown is wa2.content
              and titles_of(wa2) == ["Alpha again"]
              and held_by_another(xw.conn, A, L, CTRL))
        a3 = await app(announce(A), flux_bar(["Alpha third"]))
        await a3.answer()
        wa3 = await until(lambda: service.window_for(A) is not wa2 and service.window_for(A))
        a2.close()
        await until(lambda: wa2.closed)
        check("the same X window announced on a second connection, then the first one closing: "
              "the grabs stay (the newer window's)",
              wa3 is not None and service.grabs.get(A) is not None
              and service.grabs[A].window is wa3 and held_by_another(xw.conn, A, L, CTRL))

        e = await app(announce(E), flux_bar(["Popup"], single=True))
        got = await e.answer()
        we = await until(lambda: (w_ := service.window_for(E)) and w_.bar and w_)
        check("a window whose bar has no inputs (Flux's sign-in popup, the inspector): attached "
              "and shown, no key grabbed on it (X lets another client take Ctrl+L there)",
              got.get("type") == "attached" and we is not None and we.keys == {}
              and E not in service.grabs and not held_by_another(xw.conn, E, L, CTRL))

        mark = len(rec.records)
        g = await app(announce(G, app="ide", window="main"))
        got = await g.answer()
        g2 = await app(announce(G2, app="ide", window="second"))
        await g2.answer()
        wg = await until(lambda: service.window_for(G))
        service.set_active(G)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        check("another app (\"ide\"): the same window class, nothing of its own in the shell; "
              "kept and answered, its section empty until its bar comes; no keys grabbed",
              got.get("type") == "attached" and type(wg) is AppWindow and wg.kind == "ide"
              and wg.app_id == "main" and wg.bar is None and ctx.shown is wg.content
              and share_of(img, hex_rgb(theme.BAR_BG)) == 1.0
              and not rec.at_least(logging.WARNING, mark) and G not in service.grabs)

        bad = []
        for wid in (0, display.root, "x", True, 1 << 30, display.conn.generate_id()):
            z = await app({"type": "announce", "app": "flux", "window": 1,
                           "handle": {"x11": wid}})
            bad.append(await z.answer(0.3))
        for msg in ({"type": "announce", "window": 1, "handle": {"x11": xw.make()}},
                    {"type": "announce", "app": "", "window": 1, "handle": {"x11": xw.make()}},
                    {"type": "announce", "app": "flux", "handle": {"x11": xw.make()}},
                    {"type": "announce", "app": "flux", "window": 1, "handle": {}}):
            z = await app(msg)
            bad.append(await z.answer(0.3))
        early = await app(flux_bar(["Early"]), announce(xw.make()))
        got_early = await early.answer()
        check("announces that are no X window (0, the root, not a number, too large, one of the "
              "shell's own, none), or name no app or no id of the app's: ignored, no answer; a "
              "bar before the announce: ignored", bad == [None] * 10
              and got_early.get("type") == "attached"
              and not any(titles_of(w_) == ["Early"] for w_ in service.windows), f"{bad}")

        mark = len(rec.records)
        b.w.write(b'not json\n{"no": "type"}\n[1, 2]\n')
        b.send({"type": "from-the-future", "x": 1})
        b.send(announce(B))
        b.send(flux_bar(["Gamma", "Epsilon", "Zeta"]))
        await until(lambda: len(titles_of(wb)) == 3)
        said = [m for m in rec.at_least(logging.DEBUG, mark)
                if "no message" in m or "not understood" in m or "announced again" in m]
        check("lines that are no message, a type nobody knows, a second announce on one "
              "connection: each ignored with one debug line, and the line goes on",
              len(titles_of(wb)) == 3 and len(said) == 5
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
              and not held_by_another(xw.conn, B, L, CTRL),
              f"{notes} {eofs} sock {os.path.exists(sock)} windows {len(service.windows)} "
              f"shown {ctx.shown} grabs {list(service.grabs)}")
        service.start()
        await until(lambda: service.listening, 2)
        b2 = await app(announce(B), flux_bar(["Gamma back"]))
        got = await b2.answer()
        wb2 = await until(lambda: (w_ := service.window_for(B)) and w_.bar and w_)
        check("started again: apps announce again, focus as it was (still on that window), the "
              "keys grabbed again", got.get("type") == "attached" and wb2 is not None
              and ctx.shown is wb2.content and held_by_another(xw.conn, B, L, CTRL))
        ctx.detach()
        check("the context area detached: unhooked from the service",
              service.context is None and ctx._on_focus not in service._followers)
        expected = ("an app announced", "a bar ignored")
        unexpected = [m for m in rec.at_least(logging.WARNING)
                      if not any(e_ in m for e_ in expected)]
        check("no warnings or errors but the ones for the announces that are none and the bad "
              "bar", unexpected == [], f"{unexpected[:3]}")
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

    def GrabPointer(self, owner_events, wid, mask, pointer_mode, keyboard_mode, confine, cursor,
                    time_, is_checked=True):
        import types

        import xcffib.xproto as xp
        self.calls.append(("pointer", wid, owner_events))
        return types.SimpleNamespace(
            reply=lambda: types.SimpleNamespace(status=xp.GrabStatus.Success))

    def UngrabPointer(self, time_, is_checked=False):
        self.calls.append(("pointer let go",))

    def of(self, kind: str, since: int = 0) -> list:
        return [c for c in self.calls[since:] if c[0] == kind]


def test_grabs(scratch: Path) -> None:
    import xcffib.xproto as xp

    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from indigoshell.applink.service import AppWindow

    section("5b. the keys the shell takes (real unmapped windows of the test's own)")
    # The address list reads the history and the bookmarks: scratch ones.
    saved_env = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
    os.environ.update(XDG_CONFIG_HOME=str(scratch / "grabs-config"),
                      XDG_DATA_HOME=str(scratch / "grabs-data"))
    rec = Records("indigoshell.applink.service", "indigoshell.applink.prompt",
                  "indigoshell.applink.prompt")
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
    codes = {c: keycodes_of(other, c) for c in "ltfmyo"}
    service = AppService(path="/nonexistent", display=display)
    service.running = True                  # started, as far as the keys go (no socket here)
    display.add_keymap_listener(service._keymap_changed)
    drops = []
    service.context = stand_in_bar()
    service.show_drop = lambda prompt, place: drops.append((prompt.name, place))
    service.hide_drop = lambda: drops.append("hide")

    def flux(wid, single=False, bar=True):
        line = Line(service)
        window = AppWindow(line, wid, announce(wid))
        service.added(window)
        if bar:                     # its bar: its chords
            window.on_message(flux_bar(["Alpha"], host="a.example", single=single))
        return window, line

    def key(wid, sym, state=0, release=False):
        code = (codes.get(chr(sym)) if sym < 0x100 else None) or keycodes_of(other, sym)
        ev = xp.KeyPressEvent.synthetic(code[0], 0, display.root, wid, 0, 0, 0, 0, 0, state, 1)
        ev.response_type = 3 if release else 2
        display.dispatch(ev)

    try:
        wa, la = flux(A)
        grabs = core.of("grab")
        want = {(A, codes[c][0], CTRL | lock) for c in "ltfm" for lock in locks}
        check("a Flux window's bar: GrabKey on its window for the chords of its inputs and menu "
              "(Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M), by the keycodes the keymap gives (read on "
              "another connection), once per combination of Caps Lock and Num Lock, owner_events "
              "off, asynchronous for both devices",
              {(w_, k, m) for _, w_, k, m, *_ in grabs} == want and len(grabs) == len(want)
              and all(c[4:] == (False, ASYNC, ASYNC) for c in grabs)
              and all(len(codes[c]) == 1 for c in "ltfm"),
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
        ws, _ = flux(S, single=True)
        wn, _ = flux(N, bar=False)
        check("a bar with no inputs (Flux's popup, the inspector), and a window with no bar yet: "
              "no grab at all, no routing", core.calls[n:] == [] and not ws.keys and not wn.keys
              and S not in service.grabs and N not in service.grabs
              and S not in display._handlers and N not in display._handlers)

        # Another client holds Ctrl+M on E and F first.
        for wid in (E, F):
            other.core.GrabKeyChecked(False, wid, CTRL, codes["m"][0], ASYNC, ASYNC).check()
        mark, n = len(rec.records), len(core.calls)
        we, _ = flux(E)
        wf, _ = flux(F)
        warned = rec.at_least(logging.WARNING, mark)
        check("another client already holding Ctrl+M on a window: one warning (not again for the "
              "next such window), Ctrl+M left to the app (the lock combinations that went "
              "through let go again), Ctrl+L and Ctrl+T grabbed",
              len(warned) == 1 and "ctrl+m" in warned[0]
              and all(sorted(set(service.grabs[w_].chords.values()))
                      == ["ctrl+f", "ctrl+l", "ctrl+t"] for w_ in (E, F))
              and not held_by_another(other, E, codes["m"][0], CTRL | LOCK)
              and held_by_another(other, E, codes["l"][0], CTRL), f"{warned}")
        for wid in (E, F):
            other.core.UngrabKey(codes["m"][0], wid, CTRL)
        other.core.GetInputFocus().reply()

        # A press, as the X server reports one: Caps Lock on, the keys
        # after it on the app's window while the shell holds the keyboard.
        n = len(core.calls)
        service.focused = wa
        key(A, ord("l"), CTRL | LOCK)
        key(A, ord("l"), CTRL | LOCK, release=True)
        key(A, ord("y"), LOCK)
        key(A, ord("o"), LOCK | SHIFT)
        after = core.calls[n:]
        p = service.prompt
        check("Ctrl+L pressed in it (Caps Lock on): first the keyboard held on that window "
              "(GrabKeyboard, owner_events off, asynchronous), then the pointer on the bar's "
              "window for clicks elsewhere; the address input open in the bar, the page's "
              "address in it, selected; the keyboard not given back (the input has it)",
              after[:2] == [("keyboard", A, False, ASYNC, ASYNC), ("pointer", 1, True)]
              and p is not None and p.name == "address" and wa.view.box is p.box
              and ("let go",) not in after, f"{after[:3]}")
        check("the keys typed after it, on the app's window while the shell holds the keyboard: "
              "into the box, in order, through the keymap (Caps Lock and Shift as typed; the "
              "first replacing the selected address); the chord's release not one of them",
              p.box.field.text == "Yo", f"{p.box.field.text!r}")
        key(A, ord("l"), CTRL)
        check("Ctrl+L again while it is open: a key for it (the text selected whole), not a "
              "second input", service.prompt is p and p.box.field.selected_all)
        n = len(core.calls)
        key(A, 0xFF1B)
        check("Escape the same way: it closes; the keyboard and the pointer given back",
              service.prompt is None and ("let go",) in core.calls[n:]
              and ("pointer let go",) in core.calls[n:] and wa.view.box is None)

        n = len(core.calls)
        key(A, ord("m"), CTRL | num)
        m = service.prompt
        check("Ctrl+M (Num Lock on): the actions menu, the keyboard held for it",
              m is not None and m.what == "menu" and m.name == "actions"
              and ("keyboard", A, False, ASYNC, ASYNC) in core.calls[n:]
              and ("let go",) not in core.calls[n:])
        key(A, 0xFF1B)
        service.context = None
        n = len(core.calls)
        key(A, ord("t"), CTRL)
        check("Ctrl+T with no top bar to show it in: the keyboard held, then given back, and the "
              "app told to do without: {type: fallback, input: new_tab}",
              [c[0] for c in core.calls[n:]] == ["keyboard", "let go"]
              and la.out[-1] == {"type": "fallback", "input": "new_tab"}, f"{la.out[-1]}")
        n, sent = len(core.calls), len(la.out)
        wa.on_message({"type": "open", "input": "address"})
        check("{type: open} from the app (a new window), focused, with no top bar: the same, "
              "with no key pressed, so nothing held or given back", core.calls[n:] == []
              and la.out[sent:] == [{"type": "fallback", "input": "address"}])
        service.context = stand_in_bar()
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
        unexpected = [m for m in rec.at_least(logging.WARNING) if "ctrl+m" not in m]
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


# ── 6. where the list drops from ────────────────────────────────────────

def test_placement() -> None:
    import types

    from indigoshell.applink.service import AppService
    from indigoshell.backend.x11 import Display
    from indigoshell.applink.service import AppWindow

    section("6. whether a top bar is over an app's window, and where its list drops")
    display = Display()
    xw = Windows()
    P = xw.make(300, 150, 1000, 700)
    service = AppService(path="/nonexistent", display=display)
    jw = AppWindow(Line(service), P, announce(P))
    alone = service._cannot_show(jw)
    service.context = stand_in_bar(width=1800, left=12)
    service.context.window.content_root = (0, 0, 2000, 42)    # less than the whole screen
    under = service._cannot_show(jw)
    menu = types.SimpleNamespace(anchor=None)
    item = types.SimpleNamespace(anchor=types.SimpleNamespace(
        rect=__import__("skia").Rect.MakeXYWH(300, 0, 60, 34)))
    places = (service.section(), service._drop_place(menu), service._drop_place(item))
    service.context.window.content_root = (2560, 0, 1920, 34)
    elsewhere = service._cannot_show(jw)
    service.context = stand_in_bar()
    sw = display.geometry()[0]
    service.context.window.content_root = (0, 0, sw, 42)
    spans = service._cannot_show(jw)
    service.context.window.content_root = (0, 0, 2000, 42)
    xw.destroy(P)
    gone = service._cannot_show(jw)
    service.context.window.content_root = (0, 0, sw, 42)
    gone_spanned = service._cannot_show(jw)
    check("no top bar: it cannot show (the app does without)", alone == "no top bar", f"{alone}")
    check("a top bar over the window: it can; the list drops at the section's left, under the "
          "bar's bottom edge, the section's width; a menu opened from an item, under the item",
          under is None and places[0] == (12, 42, 1800) and places[1] == (12, 42, 1800)
          and places[2] == (300, 42, 1800), f"{under} {places}")
    check("the bar on another monitor, or the window gone: it cannot",
          elsewhere == "the top bar is on another monitor" and gone == "the window is gone",
          f"{elsewhere} {gone}")
    check("a bar spanning the whole screen is over every window: no round trip asked (the "
          "window, even gone, is not looked at)", spans is None and gone_spanned is None)
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
    from indigoshell.applink.kinds.look import TAB_CURRENT_FG
    from indigoshell.applink.service import AppWindow
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
          and top.rule is None
          and not top.override_redirect and not top.keep_above and top.sticky == bar.sticky,
          f"rule {top.rule!r}")
    first = list(top.content.children())[0]
    check("its row: the context area first, taking the room, on the service in SERVICES",
          isinstance(first, AppContext) and first.flex == 1 and first.service is cd.APPS
          and cd.APPS in cd.SERVICES and isinstance(cd.APPS, AppService))

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
    svc = AppService(path="/nonexistent")
    spec = cd.top_bar(Label("MINE", color=theme.CYAN_BRIGHT), apps=svc)
    fd = FakeDisplay(one, (2560, 1440))
    win = Window(fd, spec)
    W, H = 2560, theme.TOP_BAR_HEIGHT
    win.content_rect = skia.Rect.MakeWH(W, H)
    win.content_root, win.width, win.height = (0, 0, W, H), W, H
    spec.content.attach(win)
    jw = AppWindow(Line(svc), 0x400001, announce(0x400001))
    jw.on_message(flux_bar(["Alpha", "Beta"], loading=True, progress=50))
    svc.added(jw)
    svc.set_active(0x400001)
    win._layout_content()
    surface = skia.Surface(W, H)
    win.paint(surface.getCanvas(), W, H)
    img = rgb_of(surface.makeImageSnapshot())
    save(scratch / "topbar-whole.png", img)
    ctx, label, chip = list(spec.content.children())
    h = host_layout(jw.view)
    first_slot = jw.view.item("tabs").layout()[0]
    sx, sw = int(first_slot["slot_x"]), int(first_slot["slot_w"])
    rule_row = img[-1:]
    check("the whole bar off screen: Flux's tabs from the left, a box each, the current one's "
          "outline lit by the load, its host just before the user's widget, the widget at the "
          "right end; nothing along the bottom edge",
          ctx.shown is jw.content and sx < 10
          and h["x"] + h["w"] <= label.rect.left() and label.rect.right() >= W - 10
          and count_color(img[:, int(label.rect.left()):], theme.CYAN_BRIGHT) > 20
          and count_color(img[9:34, :int(ctx.rect.left()) + sw], TAB_CURRENT_FG) > 20
          and count_color(img[8:34, sx + 1:sx + 2], theme.MAGENTA_BLOOM, 30) >= 20
          and count_color(rule_row, theme.BAR_RULE, 30) == 0,
          f"context {ctx.rect.width():.0f} px, label at {label.rect.left():.0f}")
    check("nothing along the bar's bottom edge (no rule): the background there, all of it",
          share_of(img[-2:], hex_rgb(theme.BAR_BG), 16) == 1.0)
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
          and out["same_service"] and before == after,
          f"exit {p.returncode}; {out.get('names', [])[:3]} ... {len(out.get('names', []))} "
          f"windows; {p.stderr.strip()[-200:] if p.returncode else ''}")
    check("... and a widget of its own added the documented way: the context area then it, the "
          "order of WINDOWS kept", out.get("added") == ["AppContext", "Label", "AgentChip"]
          and out.get("same_order"), f"{out.get('added')}")


# ── 8. on screen ────────────────────────────────────────────────────────

# Pages in the test browser's history, as a real one has them: addresses
# far past 128 characters (a video's with its playlist, a search's past
# 1000), a title past 1000. (An answer holding one such row was refused
# whole by the shell: no list ever showed.)
LIVE_PAGES = [
    ("https://www.youtube.com/", "YouTube"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
     "&index=4&pp=iAQB8AUB&t=42s&ab_channel=SomeChannelName", "Yodeling at dawn - YouTube"),
    ("https://search.example/search?q=" + "yodel+" * 200 + "&src=1", "yodel - Search"),
    ("https://example.org/long", "Yo " + "very " * 300 + "long title"),
    ("https://news.example/", "News"),
]

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
    from indigoshell.applink import bar as Bv
    from indigoshell.applink.kinds.look import TAB_CURRENT_FG, TAB_FG
    HOST_FG, INSECURE_FG = theme.FG, theme.ERROR

    section("8. on screen: the real service and the real browser")
    rec = Records("indigoshell.applink.service", "indigoshell.applink.prompt",
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
    service = AppService(path=sock, display=display)
    # No daemon and no top bar here (no key is pressed: that would take
    # input injected into the display); an input wanted is answered
    # `fallback`, the browser's own dialog, none in an unattended run.
    service.start()
    # The person's real focus is not followed here: the test moves it.
    service._active_atom = 0
    ctx = AppContext(service)
    ctx.attach(Host(display))
    browser = None

    def fluxes():
        return [w for w in service.windows if w.kind == "flux" and w.bar]

    def titles(w):
        return titles_of(w)

    def idle(w):
        return not any(t.get("busy") for t in w.bar["start"][0]["tabs"])

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
        from flux import places
        hist = places.History(scratch / "data" / APP / "browser-history.sqlite")
        for i, (page_url, page_title) in enumerate(LIVE_PAGES):
            for _ in range(len(LIVE_PAGES) - i):
                hist.visit(page_url, page_title)
        hist.close()
        log_path = scratch / "browser.log"
        with open(log_path, "w") as err:
            browser = subprocess.Popen(
                [PY, str(REPO / "main.py"), "browser", "--unattended", f"{base}/painted",
                 f"{base}/page/Beta", f"{base}/page/Gamma"],
                cwd=REPO, env=benv, stdout=err, stderr=err)
        jw = await until(lambda: next((w for w in fluxes() if titles(w) == ["Painted", "Beta",
                                                                            "Gamma"]
                                       and idle(w)), None), 40)
        check("the browser announced its window as a Flux window, its bar a main window's "
              "(its chords); its three tabs reached the service", jw is not None
              and jw.kind == "flux" and "ctrl+l" in jw.keys,
              f"{[titles(w) for w in fluxes()]}")
        if jw is None:
            return
        top = jw.wid
        CTRL = xp.ModMask.Control
        chord = {c: keycodes_of(x.conn, c)[0] for c in "ltfm"}
        held = {c: held_by_another(x.conn, top, code, CTRL) for c, code in chord.items()}
        check("its Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M grabbed by the service on that window: X "
              "refuses another client the same grabs", all(held.values()), f"{held}")
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
        tabs = jw.view.item("tabs").layout()
        strong = [count_color(img[:, int(t["x"]):int(t["x"] + t["w"]) + 1], TAB_CURRENT_FG)
                  for t in tabs]
        muted = [count_color(img[:, int(t["x"]):int(t["x"] + t["w"]) + 1], TAB_FG)
                 for t in tabs]
        h = host_layout(jw.view)
        mark = count_color(img[:, int(h["x"] - h["mark_w"]):int(h["x"])], INSECURE_FG)
        hpx = count_color(img[:, int(h["x"]):int(h["x"] + h["w"]) + 1], HOST_FG)
        check("focus on it: the context area draws the browser's state (tab 1 strong, 2 and 3 "
              "muted, the host after the padlock)",
              ctx.shown is jw.content and [t["text"] for t in tabs] == ["1 Painted", "2 Beta",
                                                                        "3 Gamma"]
              and strong[0] > 20 and all(m > 20 for m in muted[1:]) and h["text"] == "127.0.0.1"
              and mark > 10 and hpx > 20, f"strong {strong}, muted {muted}")
        ids = [t["id"] for t in jw.bar["start"][0]["tabs"]]
        jw.send_event(Bv.event("select", item="tabs", tab=ids[1]))
        moved = await until(lambda: jw.bar["start"][0]["current"] == ids[1], 5)
        check("an event of the bar's to it ({type: select, item: tabs, tab: the second tab's "
              "id}): the browser switches, its bar follows", bool(moved) and jw.view.item("tabs").current == 1)
        jw.send_event(Bv.event("select", item="tabs", tab=ids[0]))
        await until(lambda: jw.bar["start"][0]["current"] == ids[0], 5)
        await live_input(scratch, service, display, x, jw, loop)
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


async def live_input(scratch: Path, service, display, x, jw, loop) -> None:
    """Ctrl+L on the real browser's window, the live path whole: the real
    Daemon opening the real drop-down window under a top bar of the
    test's own (a small window in the corner over the browser's: no dock,
    no strut), the browser answering from its history over the socket.
    Keys are delivered to the service's handler as X reports them under
    its grab; the keyboard and pointer grabs are recorded, never made."""
    import xcffib.xproto as xp

    from indigoshell import config_default, theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.drop import DROP_WINDOW
    from indigoshell.core import daemon as daemon_mod
    from indigoshell.widgets.base import Insets
    from indigoshell.widgets.layout import Align, Row
    from indigoshell.window import Layer, Placement, WindowSpec

    print("  the live path: Ctrl+L, the browser's answers, the list under a bar", flush=True)
    top = jw.wid
    core = x.conn.core
    at = core.TranslateCoordinates(top, x.screen.root, 0, 0).reply()
    geo = core.GetGeometry(top).reply()
    bw = min(geo.width + 40, 700)
    bar = WindowSpec(name="test-top-bar", layer=Layer.OVERLAY, size=(bw, theme.TOP_BAR_HEIGHT),
                     override_redirect=True, focusable=False, background=theme.BAR_BG,
                     padding=Insets.xy(theme.BAR_MARGIN, 0),
                     content=Row([AppContext(service)], spacing=theme.BAR_SPACING,
                                 align=Align.CENTER))
    drop = next(s_ for s_ in config_default.WINDOWS if s_.name == DROP_WINDOW)
    grabs: list = []
    saved = {k: getattr(display, k) for k in ("grab_keyboard", "ungrab_keyboard",
                                              "grab_pointer", "ungrab_pointer")}
    display.grab_keyboard = lambda wid: grabs.append(("keyboard", wid)) or True
    display.ungrab_keyboard = lambda: grabs.append("keyboard let go")
    display.grab_pointer = lambda wid: grabs.append(("pointer", wid)) or True
    display.ungrab_pointer = lambda: grabs.append("pointer let go")
    d = daemon_mod.Daemon({"windows": [bar, drop]})
    d.display, d.loop = display, loop
    said: list[dict] = []
    real_send = jw.send

    def send(msg):
        said.append(msg)
        real_send(msg)
    jw.send = send

    def press(sym, state=0):
        ev = xp.KeyPressEvent.synthetic(display.keycodes(sym)[0], 0, display.root, top,
                                        0, 0, 0, 0, 0, state, 1)
        ev.response_type = 2
        display.dispatch(ev)

    try:
        d.open("test-top-bar", placement=Placement(at.dst_x + geo.width // 2,
                                                   max(0, at.dst_y - theme.TOP_BAR_HEIGHT - 8)))
        service.set_active(top)
        press(ord("l"), xp.ModMask.Control)
        p = service.prompt
        opened = p is not None and p.owner is jw and jw.view.box is p.box
        shown = await until(lambda: DROP_WINDOW in d.instances
                            and d.instances[DROP_WINDOW].mapped and p.rows, 5)
        win = d.instances.get(DROP_WINDOW)
        left, bottom, width = service.section() or (0, 0, 0)
        queries = [m for m in said if m.get("type") == "query"]
        check("Ctrl+L on the real browser's window: the address input open in the section, the "
              "page's address queried and answered by the browser, the list's window mapped "
              "flush under the bar at the section's left and width",
              opened and bool(shown) and queries and queries[0]["seq"] == 1
              and queries[0]["text"] == jw.bar["inputs"]["address"]["text"]
              and win is not None and win.content_root[:3] == (left, bottom, width),
              f"opened {opened}, shown {bool(shown)}, queries {len(queries)}, "
              f"{win.content_root if win else None} vs {(left, bottom, width)}")
        await asyncio.sleep(0.3)
        img = x.region(win.wid, 0, 0, win.width, win.height) if win is not None else None
        if img is not None:
            save(scratch / "live-drop.png", img)
        cyan = count_color(img, theme.CYAN_BRIGHT, 90) if img is not None else 0
        check("... its own pixels: the rows drawn, the selection's cyan slab",
              img is not None and cyan > 100, f"cyan px {cyan}")
        for c in "yo":
            press(ord(c))
        typed = await until(lambda: p.text == "yo" and p.taken >= 2 and p.inflight is None
                            and any(r.get("tag") == "SEARCH" for r in p.rows), 5)
        titles_ = [r["title"] for r in p.rows]
        selected = p.selected_row()["title"] if p.rows else None
        check("\"yo\" typed after it: into the box (the address replaced), each answer taken "
              "in turn (one query in flight): the typed row first, pages with addresses far "
              "past 128 characters and a title past 1000 (cut) among the rows, YouTube's front "
              "page selected", bool(typed) and titles_[0] == "yo" and selected == "YouTube"
              and "Yodeling at dawn - YouTube" in titles_ and "yodel - Search" in titles_
              and any(t.startswith("Yo very") and len(t) <= 300 for t in titles_),
              f"{p.text!r} {[t[:30] for t in titles_]} selected {selected!r}")
        await asyncio.sleep(0.2)
        long_row = next((r for r in p.rows if r["title"] == "yodel - Search"), None)
        img = x.region(win.wid, 0, 0, win.width, win.height)
        if img is not None:
            save(scratch / "live-drop-yo.png", img)
        check("... the list's window grown to the rows (its own pixels: the rows drawn)",
              win.content_root[3] > 52 * 3 and img is not None
              and count_color(img, theme.HUD_FG_BRIGHT, 40) > 200 and long_row is not None
              and len(long_row["id"]) <= 128, f"{win.content_root}")
        press(0xFF1B)
        parked = await until(lambda: DROP_WINDOW in d._parked, 3)
        check("Escape: closed (`closed` by escape to the browser), the list parked, the box "
              "gone, the keyboard and pointer given back", bool(parked)
              and service.prompt is None and jw.view.box is None
              and said[-1] == {"type": "closed", "input": "address", "by": "escape"}
              and "keyboard let go" in grabs and "pointer let go" in grabs, f"{said[-1:]}")
    finally:
        jw.send = real_send
        for k, v in saved.items():
            setattr(display, k, v)
        for w in list(d.instances.values()) + list(d._parked.values()) + list(d._dying):
            w.destroy()
        d.instances.clear()
        d._parked.clear()
        daemon_mod._DAEMON = None


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
        test_mouse(scratch)
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
