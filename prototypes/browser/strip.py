"""The shell's side of the browser strip, standing alone.

    strip.py [--socket PATH] [--raster] [--no-wide] [--targets] [-v]   draw until Ctrl+C
    strip.py --selftest [--scratch DIR]                     the unattended test (selftest.py)

A browser window leaves a band empty at its top and says so over a unix
socket: {"wid": <its X window>, "band": 28}. This draws into that band
through an X window whose *parent* is the browser's window, so X moves,
hides, stacks and tiles it with the browser and nothing has to follow
it by hand except its width. The strip is text: the window's tabs from
the left as `1 Title  2 Title  3 Title`, the current one in the strong
foreground and the rest muted, each title shortened with an ellipsis so
the list fits; at the right edge the current page's host, after an open
padlock when the connection is not secure; a thin line along the bottom
while the current tab loads. With --targets it also draws back /
forward / reload and turns clicks on them into
{"cmd": "back" | "forward" | "reload"}.

The strip window is as wide as the X screen and the browser's window
clips it (--no-wide: exactly as wide as the browser). Resizing a GL
child window stalls for several frames on NVIDIA; a window that never
resizes only repaints.

It never takes the keyboard. Its window does not select key events, so
a key pressed while the pointer rests over it propagates to the
browser's window, exactly as over any other part of the browser. X
delivers a key to the window under the pointer when that window is
inside the focus window; a strip that selected KeyPress would swallow
every key typed with the pointer parked over it.

The real shell now does this from its daemon (indigoshell/browser/
strip.py, which grew from this file). This prototype borrows the shell's
X connection class, GL backend, theme, text and widgets, and starts
nothing else, so it runs beside the shell that is drawing the desktop;
with the shell running it needs --socket, since the shell holds the
default one and a second listener refuses rather than take it.

Wire format: indigoshell/browser/protocol.py, one JSON object per line
both ways. In short:

    browser -> strip   {"wid": 1234, "band": 28}        first, then band on every change
                       {"tabs": [{"title", "host", "loading"}, ...], "current",
                        "progress", "secure"}           whole, whenever it changes
                       {"download": {...}}              ignored here
      the prototype    {"url", "title", "progress", "loading", "back", "forward"}
      (gauntlet.py)                                      any subset; merged, shown as one tab
    strip -> browser   {"attached": <strip window>}     drawn and mapped (or parked at band 0)
                       {"cmd": "back" | "forward" | "reload"}
    test monitor       {"monitor": true}  then events; {"stats": true}; {"frame": <browser wid>}
"""

import argparse
import asyncio
import base64
import ctypes
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import skia
import xcffib.xproto as xp

from indigoshell import text, theme
from indigoshell.backend import gl as shell_gl
from indigoshell.backend.x11 import Display, DisplayError
from indigoshell.browser import protocol
from indigoshell.core import log as shell_log
from indigoshell.core.naming import APP
from indigoshell.widgets.base import Insets, Size, Widget
from indigoshell.widgets.label import Label
from indigoshell.widgets.layout import Align, Row, Spacer

log = logging.getLogger(f"{APP}.strip")

SOCKET_ENV = protocol.SOCKET_ENV

# ── look ────────────────────────────────────────────────────────────────
# The bar's own vocabulary and nothing else: its background, its text
# size, its strong neon for the tab in front and the muted tone for the
# rest, its plain foreground for the host, its accent rule for the one
# moving thing. No plate, no frame, no separators.
BG = theme.BAR_BG
TAB_FG = theme.BASE_MUTED
TAB_CURRENT_FG = theme.FG_STRONG
HOST_FG = theme.FG
INSECURE_FG = theme.ERROR
INSECURE_MARK = ""            # an open padlock (Nerd Font: fa-unlock)
TARGET_FG = theme.FG
TARGET_HOVER = theme.FG_STRONG
TARGET_OFF = theme.FG_MUTED
PROGRESS_FG = theme.BAR_RULE
PROGRESS_THICK = theme.BAR_RULE_THICK
SIZE = theme.FONT_SIZE
PAD = Insets(0, 4, 0, 8)            # top, right, bottom, left
HOST_CAP = 0.34                     # the host takes at most this share of the row
TARGET_W = 28                       # one band-high cell per target

TARGETS = (("back", ""), ("forward", ""), ("reload", ""))

# Core CreateWindow value-mask bits, in the order their values go.
_CW = xp.CW
_NORTHWEST = 1                      # BitGravity: keep the left side on a resize


def socket_path() -> str:
    """Where the strip listens: protocol.py's rule, which link.py repeats."""
    return protocol.socket_path()


def _quietly(cookie) -> None:
    """Wait for a checked request; a BadWindow is an expected race."""
    try:
        cookie.check()
    except xp.WindowError:
        log.debug("window already gone")


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


# ── widgets ─────────────────────────────────────────────────────────────
# Page titles and hosts are text from elsewhere: drawn through the
# shell's per-character fallback, not shaped (see text.py).

def fit_tabs(titles: list[str], current: int, width: float,
             measure=lambda s: text.measure(s, SIZE)) -> list[tuple[int, str, float, float]]:
    """Lay the tabs out in `width` px: (index, label, x, width) for each
    one drawn, x from 0.

    All as `N Title` with two spaces between when they fit. Otherwise
    each keeps its number and the longest titles are shortened first,
    all to the same cap (water-filling), with an ellipsis. When even
    `N …` each is too much, the numbers alone; and when those do not
    fit either, as many as do, around the current one.
    """
    n = len(titles)
    if n == 0 or width <= 0:
        return []
    gap = measure("  ")
    labels = [f"{i + 1} {t}" if t else str(i + 1) for i, t in enumerate(titles)]
    natural = [measure(label) for label in labels]
    room = width - gap * (n - 1)

    def place(texts, widths, first=0):
        out, x = [], 0.0
        for i, (label, w) in enumerate(zip(texts, widths), start=first):
            out.append((i, label, x, w))
            x += w + gap
        return out

    if sum(natural) <= room:
        return place(labels, natural)

    floor = [min(nat, measure(f"{i + 1} …")) for i, nat in enumerate(natural)]
    if sum(floor) <= room:
        lo, hi = 0.0, max(natural)
        for _ in range(30):
            cap = (lo + hi) / 2
            total = sum(min(nat, max(f, cap)) for nat, f in zip(natural, floor))
            lo, hi = (cap, hi) if total <= room else (lo, cap)
        widths = [min(nat, max(f, lo)) for nat, f in zip(natural, floor)]
        texts = [text.ellipsize(label, SIZE, w + 0.5) if w < nat else label
                 for label, w, nat in zip(labels, widths, natural)]
        return place(texts, [measure(t) for t in texts])

    numbers = [str(i + 1) for i in range(n)]
    sizes = [measure(s) for s in numbers]
    gap = measure(" ")
    a = b = max(0, min(current, n - 1))           # the run [a, b] that fits
    used = sizes[a]
    while True:
        grew = False
        for side in (1, -1):
            j = b + 1 if side > 0 else a - 1
            if 0 <= j < n and used + gap + sizes[j] <= width:
                used += gap + sizes[j]
                a, b = (a, j) if side > 0 else (j, b)
                grew = True
        if not grew:
            break
    out, x = [], 0.0
    for i in range(a, b + 1):
        out.append((i, numbers[i], x, sizes[i]))
        x += sizes[i] + gap
    return out


class TabList(Widget):
    """The window's tabs, numbered, from the left of the strip."""

    def __init__(self) -> None:
        super().__init__(flex=1)
        self.titles: list[str] = []
        self.current = 0
        self.drawn: list[tuple[int, str, float, float]] = []

    def set_tabs(self, titles: list[str], current: int) -> None:
        if titles != self.titles or current != self.current:
            self.titles, self.current = list(titles), current
            self.invalidate(layout=True)

    def measure(self, avail: Size) -> Size:
        return Size(0.0, text.line_height(SIZE))

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self.drawn = fit_tabs(self.titles, self.current, rect.width())

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        canvas.save()
        canvas.clipRect(r)
        for i, label, x, _w in self.drawn:
            color = TAB_CURRENT_FG if i == self.current else TAB_FG
            text.draw(canvas, label, r.left() + x, base, SIZE, theme.color(color))
        canvas.restore()

    def layout(self) -> list[dict]:
        r = self.rect
        return [{"index": i, "text": label, "x": r.left() + x, "w": w,
                 "current": i == self.current} for i, label, x, w in self.drawn]


class Host(Widget):
    """The current page's host, plain, at the right; an open padlock
    before it when the page came over an insecure connection."""

    def __init__(self) -> None:
        super().__init__()
        self.host = ""
        self.secure = True
        self.drawn = ("", 0.0, 0.0)         # host as drawn, its x, the mark's width

    def set(self, host: str, secure: bool) -> None:
        if (host, secure) != (self.host, self.secure):
            self.host, self.secure = host, secure
            self.invalidate(layout=True)

    def _mark(self) -> str:
        return "" if self.secure else INSECURE_MARK + " "

    def measure(self, avail: Size) -> Size:
        width = text.measure(self._mark() + self.host, SIZE)
        return Size(min(width, avail.width * HOST_CAP), text.line_height(SIZE))

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        x = r.left()
        mark = self._mark()
        mark_w = text.measure(mark, SIZE) if mark else 0.0
        if mark:
            text.draw(canvas, mark, x, base, SIZE, theme.color(INSECURE_FG))
        value = text.ellipsize(self.host, SIZE, max(0.0, r.width() - mark_w) + 0.5)
        if value and text.measure(value, SIZE) <= r.width() - mark_w + 0.5:
            text.draw(canvas, value, x + mark_w, base, SIZE, theme.color(HOST_FG))
        self.drawn = (value, x + mark_w, mark_w)

    def layout(self) -> dict:
        value, x, mark_w = self.drawn
        return {"text": value, "x": x, "w": text.measure(value, SIZE) if value else 0.0,
                "mark_w": mark_w, "secure": self.secure}


class Target(Label):
    """A click target: one glyph centred in a band-high cell. Lit on
    hover, dimmed when there is nowhere to go."""

    def __init__(self, name: str, glyph: str, on_press) -> None:
        super().__init__(glyph, size=SIZE, align="center", color=self._color,
                         on_left_click=lambda _w: on_press(name))
        self.name = name
        self.enabled = True

    def _color(self) -> str:
        if not self.enabled:
            return TARGET_OFF
        return TARGET_HOVER if self.hovered else TARGET_FG

    def measure(self, avail: Size) -> Size:
        return Size(TARGET_W, text.line_height(self.size))

    def hit(self, x: float, y: float):
        return super().hit(x, y) if self.enabled else None


# ── one strip ───────────────────────────────────────────────────────────

class Strip:
    """One browser window's strip: the X child window, what it shows,
    and the connection the browser announced itself on. Also the "window"
    its widgets report damage to."""

    def __init__(self, server: "Server", writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.display = server.display
        self.writer = writer
        self.parent: int | None = None
        self.wid: int | None = None
        self.band = 0
        self.width = 0                  # the browser's: what the strip lays out to
        self.child_w = 0                # our window's: the same, or more with --wide
        self.mapped = False
        self.closed = False
        self.parent_gone = False
        self.path = "?"                 # "gl" or "raster"
        self.depth = 0
        self.cmap = None
        self.gc = None
        self.gl = None
        self.gl_surface = None
        self.surface: skia.Surface | None = None
        self.paints = 0
        self.draw_ms = self.present_ms = 0.0
        self._dirty = False
        self._paint_queued = False
        self._relayout = True
        self._hovered = None
        self._attached_sent = False
        # What is shown: protocol.py's state. A prototype browser's older
        # state is kept in `old` and shown through this as one tab.
        self.state = protocol.state([], 0, 100, True)
        self.old = {"url": "", "title": "", "progress": 100, "loading": False,
                    "back": False, "forward": False}

        self.tabs = TabList()
        self.host = Host()
        # Text only unless asked: the targets are an opt-in (--targets).
        self.targets = ({name: Target(name, glyph, self.press) for name, glyph in TARGETS}
                        if server.targets else {})
        tail = [Spacer(size=theme.SPACING_SM), *self.targets.values()] if self.targets else []
        self.content = Row(
            [self.tabs, Spacer(size=theme.SPACING_MD), self.host, *tail],
            padding=PAD, align=Align.STRETCH)
        self.content.attach(self)

    # ── messages from the browser ───────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        if self.closed:
            return
        if self.parent is None:
            if "wid" not in msg:
                return
            self._merge(msg)
            self.create(int(msg["wid"]), int(msg.get("band", 0)))
            return
        if "band" in msg:
            self.set_band(int(msg["band"]))
        self._merge(msg)

    def _merge(self, msg: dict) -> None:
        if protocol.is_state(msg):
            problems = protocol.state_problems(msg)
            if problems:
                log.warning("state from 0x%x ignored: %s", self.parent or 0, "; ".join(problems))
                return
            self._show({k: msg[k] for k in protocol.STATE_KEYS})
        elif any(k in msg for k in protocol.OLD_STATE_KEYS):
            self._merge_old(msg)

    def _merge_old(self, msg: dict) -> None:
        """The prototype's {"url", "title", ...}: merged, shown as one tab."""
        changed = False
        for key in self.old:
            if key in msg and msg[key] != self.old[key]:
                self.old[key] = msg[key]
                changed = True
        if not changed:
            return
        o = self.old
        url = str(o["url"] or "")
        scheme = urlsplit(url).scheme if url else ""
        self._show(protocol.state(
            [protocol.tab(str(o["title"] or url), host_of(url), bool(o["loading"]))], 0,
            max(0, min(100, int(o["progress"]))), not protocol.insecure_scheme(scheme)))
        for name in ("back", "forward"):
            if name in self.targets:
                self.targets[name].enabled = bool(o[name])

    def _show(self, state: dict) -> None:
        if state == self.state:
            return
        self.state = state
        tabs, current = state["tabs"], state["current"]
        self.tabs.set_tabs([str(t["title"]) for t in tabs], current)
        here = tabs[current] if tabs else {"host": ""}
        self.host.set(str(here["host"]), bool(state["secure"]))
        self.damage(layout=True)

    def loading(self) -> tuple[bool, int]:
        """Whether the current tab loads, and how far it is."""
        s = self.state
        tabs = s["tabs"]
        busy = bool(tabs) and bool(tabs[s["current"]]["loading"])
        return busy, max(0, min(100, int(s["progress"])))

    def send(self, msg: dict) -> None:
        if self.closed or self.writer.is_closing():
            return
        self.writer.write((json.dumps(msg) + "\n").encode())

    def press(self, name: str) -> None:
        log.info("%s on 0x%x", name, self.parent)
        self.send({"cmd": name})
        self.server.emit({"ev": "cmd", "parent": self.parent, "cmd": name})

    # ── the X window ────────────────────────────────────────────────────
    def create(self, parent: int, band: int) -> None:
        conn = self.display.conn
        try:
            # Structure events first, then the size: a resize landing
            # between the two would otherwise be missed for good.
            conn.core.ChangeWindowAttributesChecked(
                parent, _CW.EventMask, [xp.EventMask.StructureNotify]).check()
            geo = conn.core.GetGeometry(parent).reply()
            attrs = conn.core.GetWindowAttributes(parent).reply()
        except Exception as e:
            log.warning("browser announced 0x%x, which is no window (%s)", parent, e)
            self.send({"error": "no such window"})
            self.display.wake()
            return
        self.display.wake()
        self.parent, self.band, self.width = parent, band, geo.width
        self.display.on(parent, self._on_parent_event)
        self._create_child(geo.depth, attrs.visual)

    def _create_child(self, parent_depth: int, parent_visual: int) -> None:
        conn = self.display.conn
        backend = None if self.server.raster else shell_gl.backend(self.display)
        if backend is not None:
            self.server.guard_xlib(backend)
        self.child_w = self._child_width()
        w, h = self.child_w, max(1, self.band)
        events = (xp.EventMask.Exposure | xp.EventMask.ButtonPress
                  | xp.EventMask.ButtonRelease | xp.EventMask.PointerMotion
                  | xp.EventMask.LeaveWindow)       # and no key or focus events
        bg = theme.color(BG)
        self.wid = conn.generate_id()
        if backend is not None:
            # The shell's GL path: EGL's depth-32 visual, which is not the
            # parent's. A child of another depth must name its border
            # pixel and colormap or CreateWindow is a BadMatch. Composite
            # then redirects it and paints it into the parent with Src:
            # alpha is dropped, so it is drawn opaque.
            self.depth, visual = 32, backend.visual_id
            self.cmap = conn.generate_id()
            conn.core.CreateColormap(xp.ColormapAlloc._None, self.cmap,
                                     self.display.root, visual)
            conn.core.CreateWindow(
                32, self.wid, self.parent, 0, 0, w, h, 0,
                xp.WindowClass.InputOutput, visual,
                _CW.BackPixel | _CW.BorderPixel | _CW.BitGravity | _CW.EventMask
                | _CW.Colormap | _CW.Cursor,
                [bg, 0, _NORTHWEST, events, self.cmap, self.server.cursor])
            conn.core.GetInputFocus().reply()      # EGL asks over its own connection
            self.display.wake()
            try:
                self.gl_surface = backend.create(self.wid, w, h)
            except Exception:
                log.exception("GL surface failed; using raster")
                self.gl_surface = None
            if self.gl_surface is None:
                conn.core.DestroyWindow(self.wid)
                conn.core.FreeColormap(self.cmap)
                self.cmap = None
                backend = None
                self.wid = conn.generate_id()
            else:
                self.gl, self.path = backend, "gl"
        if backend is None:
            # Depth and visual copied from the parent: no conversion
            # anywhere, PutImage writes straight into the parent's pixels.
            self.depth, self.path = parent_depth, "raster"
            conn.core.CreateWindow(
                0, self.wid, self.parent, 0, 0, w, h, 0,
                xp.WindowClass.InputOutput, 0,
                _CW.BackPixel | _CW.BitGravity | _CW.EventMask | _CW.Cursor,
                [bg & 0xFFFFFF, _NORTHWEST, events, self.server.cursor])
            self.gc = conn.generate_id()
            conn.core.CreateGC(self.gc, self.wid, 0, [])
        conn.core.ChangeProperty(xp.PropMode.Replace, self.wid, xp.Atom.WM_NAME,
                                 xp.Atom.STRING, 8, len(b"strip"), b"strip")
        self.display.on(self.wid, self._on_event)
        self.surface = self._make_surface()
        log.info("strip 0x%x in browser 0x%x: %dx%d, %s, depth %d (parent depth %d, "
                 "visual 0x%x)%s", self.wid, self.parent, w, h, self.path, self.depth,
                 parent_depth, parent_visual,
                 f", GPU {self.gl.renderer}" if self.gl else "")
        self.server.emit({
            "ev": "created", "parent": self.parent, "child": self.wid, "path": self.path,
            "depth": self.depth, "parent_depth": parent_depth,
            "parent_visual": parent_visual,
            "renderer": self.gl.renderer if self.gl else None})
        if self.band > 0:
            self._map()
        else:
            self._announce()
        self.display.flush()

    def _child_width(self) -> int:
        """--wide: as wide as the X screen from the start, clipped by the
        browser's window, so a resize is a repaint and never a new window
        size: no GL buffers to reallocate, and no band left uncovered while
        the browser grows. Only a browser wider than the screen grows it."""
        if not self.server.wide:
            return self.width
        return max(self.width, self.child_w, self.display.screen.width_in_pixels)

    def _make_surface(self) -> skia.Surface:
        w, h = max(1, self.child_w), max(1, self.band)
        if self.gl is not None:
            return self.gl.offscreen(w, h)
        return skia.Surface(w, h)

    def _map(self) -> None:
        self.display.conn.core.MapWindow(self.wid)
        self.mapped = True
        self.damage(layout=True)
        self.server.emit({"ev": "map", "parent": self.parent, "mapped": True})

    def _announce(self) -> None:
        if not self._attached_sent:
            self._attached_sent = True
            self.send({"attached": self.wid})
            self.server.emit({"ev": "attached", "parent": self.parent, "child": self.wid})

    def set_band(self, band: int) -> None:
        if self.wid is None or band == self.band:
            return
        conn = self.display.conn
        if band <= 0:
            # Fullscreen. The band is the browser's again; nothing to draw.
            conn.core.UnmapWindow(self.wid)
            self.mapped = False
            self.band = 0
            self.display.flush()
            self.server.emit({"ev": "map", "parent": self.parent, "mapped": False})
            return
        self.band = band
        self._resize()
        if not self.mapped:
            self._map()
        else:
            self.damage()
        self.display.flush()

    def _resize(self) -> None:
        """Take on `self.width` x `self.band`: the window, the GL buffer
        and the offscreen surface. The caller asks for the frame."""
        conn = self.display.conn
        self.child_w = self._child_width()
        conn.core.ConfigureWindow(self.wid, xp.ConfigWindow.Width | xp.ConfigWindow.Height,
                                  [max(1, self.child_w), max(1, self.band)])
        if self.gl_surface is not None:
            # Same reason as at creation: EGL sizes its buffers from what
            # the server says over its own connection.
            conn.core.GetInputFocus().reply()
            self.display.wake()
            self.gl.make_current(self.gl_surface)
            self.gl_surface.resize(max(1, self.child_w), max(1, self.band))
        self.surface = self._make_surface()
        self._relayout = True

    def _on_parent_event(self, ev) -> None:
        if isinstance(ev, xp.ConfigureNotifyEvent) and ev.window == self.parent:
            if ev.width != self.width:
                # Painted here and now, not queued: every millisecond
                # between the browser growing and this frame is a sliver
                # of band with nothing of ours in it.
                t_event = time.monotonic()
                old, self.width = self.width, ev.width
                if self._child_width() != self.child_w:
                    self._resize()
                else:
                    self._relayout = True
                t_resized = time.monotonic()
                self._dirty = True
                self._paint_now()
                self.server.emit({"ev": "configure", "parent": self.parent,
                                  "width": self.width, "old": old, "t_event": t_event,
                                  "t_resized": t_resized, "t_presented": time.monotonic(),
                                  "draw_ms": self.draw_ms, "present_ms": self.present_ms})
        elif isinstance(ev, xp.DestroyNotifyEvent) and ev.window == self.parent:
            # X took our window with it.
            self.parent_gone = True
            self.close("browser window destroyed")

    def _on_event(self, ev) -> None:
        if isinstance(ev, xp.ExposeEvent):
            if ev.count == 0:
                self.damage()
        elif isinstance(ev, xp.ButtonPressEvent):
            if ev.detail == 1:
                widget = self.content.hit(ev.event_x, ev.event_y)
                if widget is not None:
                    widget.click(1, ev.event_x, ev.event_y)
        elif isinstance(ev, xp.MotionNotifyEvent):
            self._hover(self.content.hit(ev.event_x, ev.event_y))
        elif isinstance(ev, xp.LeaveNotifyEvent):
            self._hover(None)
        elif isinstance(ev, (xp.KeyPressEvent, xp.FocusInEvent)):
            # Never selected. Counted so the test can show none arrive.
            self.server.keys += 1

    def _hover(self, widget) -> None:
        if widget is self._hovered:
            return
        if self._hovered is not None:
            self._hovered.set_hovered(False)
        self._hovered = widget
        if widget is not None:
            widget.set_hovered(True)

    # ── painting: only when something changed ───────────────────────────
    def damage(self, widget=None, *, layout: bool = False) -> None:
        """Coalesced: a burst of state changes is one frame. The whole
        strip is repainted; at 28 rows a damage region would save nothing."""
        self._relayout = self._relayout or layout
        self._dirty = True
        if not self._paint_queued and not self.closed:
            self._paint_queued = True
            asyncio.get_running_loop().call_soon(self._paint_now)

    def _paint_now(self) -> None:
        self._paint_queued = False
        if (not self._dirty or self.closed or self.wid is None or not self.mapped
                or self.surface is None):
            return
        self._dirty = False
        t0 = time.monotonic()
        w, h = max(1, self.width), max(1, self.band)
        if self._relayout:
            self._relayout = False
            self.content.measure(Size(w, h))
            self.content.arrange(skia.Rect.MakeWH(w, h))
        canvas = self.surface.getCanvas()
        canvas.clear(theme.color(BG))
        self.content.paint(canvas)
        busy, progress = self.loading()
        if busy and progress < 100:
            line = skia.Paint(AntiAlias=False)
            line.setColor(theme.color(PROGRESS_FG))
            done = round(w * progress / 100)
            canvas.drawRect(skia.Rect.MakeXYWH(0, h - PROGRESS_THICK, done, PROGRESS_THICK),
                            line)
        self.surface.flushAndSubmit()
        frame = self.surface.makeImageSnapshot()
        t1 = time.monotonic()
        if self.gl_surface is not None:
            self.gl.make_current(self.gl_surface)
            fb = self.gl_surface.skia_surface.getCanvas()
            copy = skia.Paint()
            copy.setBlendMode(skia.BlendMode.kSrc)
            fb.drawImage(frame, 0, 0, skia.SamplingOptions(), copy)
            self.gl_surface.swap()
        else:
            self._put(frame.tobytes(), max(1, self.child_w), h)
        self.display.flush()
        self.draw_ms, self.present_ms = (t1 - t0) * 1000, (time.monotonic() - t1) * 1000
        self.paints += 1
        self._announce()

    def _put(self, pixels: bytes, w: int, h: int) -> None:
        conn = self.display.conn
        stride = w * 4
        rows = max(1, 60000 // stride)
        for y0 in range(0, h, rows):
            n = min(rows, h - y0)
            chunk = pixels[y0 * stride:(y0 + n) * stride]
            conn.core.PutImage(xp.ImageFormat.ZPixmap, self.wid, self.gc, w, n, 0, y0, 0,
                               self.depth, len(chunk), chunk)

    def frame_png(self) -> bytes | None:
        """The last frame, for the self-test to compare pixels against."""
        if self.surface is None:
            return None
        if self.gl_surface is not None:
            self.gl.make_current(self.gl_surface)
        image = self.surface.makeImageSnapshot().makeRasterImage()
        if self.child_w > self.width:       # only what the browser's window shows
            image = image.makeSubset(skia.IRect.MakeWH(self.width, max(1, self.band)))
        return bytes(image.encodeToData())

    def target_rects(self) -> dict:
        return {name: [t.rect.left(), t.rect.top(), t.rect.width(), t.rect.height()]
                for name, t in self.targets.items()}

    # ── teardown ────────────────────────────────────────────────────────
    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        d, conn = self.display, self.display.conn
        if self.gl_surface is not None:
            # A surface whose window X already destroyed is not made
            # current again: park the context, then let the surface go.
            if self.parent_gone:
                self.gl.make_idle()
            else:
                self.gl.make_current(self.gl_surface)
            self.surface = None
            self.gl_surface.destroy()
            self.gl_surface = None
        self.surface = None
        # A hang-up usually comes with the browser's window closing, and
        # that window may be gone already, ours with it, before this runs.
        # Checked, so the BadWindow that race earns is answered here and
        # does not land in the event queue as an error.
        if self.wid is not None:
            d.off(self.wid)
            if not self.parent_gone:
                _quietly(conn.core.DestroyWindowChecked(self.wid))
        if self.parent is not None:
            d.off(self.parent)
            if not self.parent_gone:
                _quietly(conn.core.ChangeWindowAttributesChecked(
                    self.parent, _CW.EventMask, [0]))
        if self.gc is not None:
            conn.core.FreeGC(self.gc)
        if self.cmap is not None:
            conn.core.FreeColormap(self.cmap)
        d.flush()
        self.content.detach()
        if not self.writer.is_closing():
            self.writer.close()
        if self.wid is not None:
            log.info("strip 0x%x gone: %s", self.wid, reason)
        self.server.forget(self, reason)


# ── the server ──────────────────────────────────────────────────────────

class _XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(_XErrorEvent))


class _ErrorCount(logging.Handler):
    """Counts the X errors the shell's event pump logs."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record) -> None:
        if "X protocol error" in record.getMessage():
            self.count += 1


class Server:
    def __init__(self, path: str, raster: bool, targets: bool = False,
                 wide: bool = True) -> None:
        self.path = path
        self.raster = raster
        self.targets = targets
        self.wide = wide
        self.strips: list[Strip] = []
        self.monitors: set[asyncio.StreamWriter] = set()
        self.keys = 0
        self.xlib_errors = 0
        self._xlib_handler = None
        self.errors = _ErrorCount()
        logging.getLogger("indigoshell.backend.x11").addHandler(self.errors)
        self.display = Display()
        self.cursor = self._left_ptr()

    def _left_ptr(self) -> int:
        """The plain arrow. Without a cursor of its own the strip would
        wear whatever the browser last set on its window: a text beam
        after the pointer crossed a paragraph on its way up."""
        conn = self.display.conn
        font, cursor = conn.generate_id(), conn.generate_id()
        conn.core.OpenFont(font, len(b"cursor"), b"cursor")
        conn.core.CreateGlyphCursor(cursor, font, font, 68, 69,
                                    0, 0, 0, 0xFFFF, 0xFFFF, 0xFFFF)
        conn.core.CloseFont(font)
        return cursor

    def guard_xlib(self, backend) -> None:
        """Keep an X error on EGL's own Xlib connection from killing us.

        The shell destroys its windows itself, in order. Here the parent
        is someone else's and can vanish between two of our calls, taking
        our window with it; Xlib's default handler answers the BadWindow
        or BadDrawable that EGL then provokes by exiting the process.
        """
        if self._xlib_handler is not None:
            return

        def on_error(_dpy, event) -> int:
            self.xlib_errors += 1
            e = event.contents
            log.warning("Xlib error %d on request %d.%d (resource 0x%x)",
                        e.error_code, e.request_code, e.minor_code, e.resourceid)
            return 0

        self._xlib_handler = _XErrorHandler(on_error)
        backend.lib.x11.XSetErrorHandler.restype = ctypes.c_void_p
        backend.lib.x11.XSetErrorHandler(self._xlib_handler)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.display.attach_loop(loop)
        loop.add_reader(self.display.fd, self._pump)
        self._claim_socket()
        server = await asyncio.start_unix_server(self._client, path=self.path)
        log.info("listening on %s (%s)", self.path,
                 "raster" if self.raster else "GL if the backend comes up")
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            server.close()
            for strip in list(self.strips):
                strip.close("strip process exiting")
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            loop.remove_reader(self.display.fd)
            self.display.close()

    def _claim_socket(self) -> None:
        if not os.path.exists(self.path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(self.path)
        except OSError:
            os.unlink(self.path)         # stale: its owner is gone
            return
        finally:
            probe.close()
        raise SystemExit(f"a strip is already listening on {self.path}: the shell's "
                         f"daemon draws the strip itself now (browser/strip.py). "
                         f"Give this one a socket of its own with --socket PATH.")

    def _pump(self) -> None:
        try:
            self.display.pump()
        except DisplayError:
            log.exception("X connection lost")
            asyncio.get_running_loop().stop()

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        strip = None
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("monitor"):
                    self.monitors.add(writer)
                elif msg.get("stats"):
                    self._reply(writer, self.stats())
                elif "frame" in msg:
                    self._reply(writer, self._frame(int(msg["frame"])))
                else:
                    if strip is None:
                        strip = Strip(self, writer)
                        self.strips.append(strip)
                    strip.on_message(msg)
                    self.display.pump()
        except (ConnectionError, OSError):
            pass
        finally:
            self.monitors.discard(writer)
            if strip is not None:
                strip.close("browser hung up")
            elif not writer.is_closing():
                writer.close()

    def forget(self, strip: Strip, reason: str) -> None:
        if strip in self.strips:
            self.strips.remove(strip)
        self.emit({"ev": "detached", "parent": strip.parent, "child": strip.wid,
                   "reason": reason})

    # ── what the self-test reads ────────────────────────────────────────
    def emit(self, event: dict) -> None:
        event.setdefault("t", time.monotonic())
        for writer in list(self.monitors):
            self._reply(writer, event)

    @staticmethod
    def _reply(writer: asyncio.StreamWriter, msg: dict) -> None:
        if not writer.is_closing():
            writer.write((json.dumps(msg) + "\n").encode())

    def stats(self) -> dict:
        return {"ev": "stats", "pid": os.getpid(), "x_errors": self.errors.count,
                "xlib_errors": self.xlib_errors, "keys": self.keys,
                "resource_base": self.display.setup.resource_id_base,
                "strips": [{"parent": s.parent, "child": s.wid, "mapped": s.mapped,
                            "width": s.width, "child_width": s.child_w, "band": s.band,
                            "path": s.path,
                            "paints": s.paints} for s in self.strips]}

    def _frame(self, parent: int) -> dict:
        for s in self.strips:
            if s.parent == parent:
                png = s.frame_png()
                return {"ev": "frame", "parent": parent, "child": s.wid,
                        "width": s.width, "band": s.band, "mapped": s.mapped,
                        "targets": s.target_rects(), "paints": s.paints,
                        "tabs": s.tabs.layout(), "host": s.host.layout(),
                        "png": base64.b64encode(png).decode() if png else None}
        return {"ev": "frame", "parent": parent, "child": None}


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        import selftest
        return selftest.main([a for a in sys.argv[1:] if a != "--selftest"])
    parser = argparse.ArgumentParser(usage=__doc__.split("\n\n")[1])
    parser.add_argument("--socket", default=None)
    parser.add_argument("--raster", action="store_true",
                        help="skip the GL backend and draw with PutImage")
    parser.add_argument("--targets", action="store_true",
                        help="also draw back / forward / reload click targets")
    parser.add_argument("--wide", action=argparse.BooleanOptionalAction, default=True,
                        help="make the strip window screen-wide and let the browser clip it "
                             "(the default; --no-wide sizes it to the browser)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    shell_log.setup("DEBUG" if args.verbose else "INFO")
    server = Server(args.socket or socket_path(), args.raster, args.targets, args.wide)
    asyncio.run(server.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
