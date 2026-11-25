"""The shell's top bar and its line to the shell's own apps
(indigoshell/applink: the app bar protocol), and the shell code under it.

    .venv/bin/python tests/topbar.py              all of it, off screen, a few seconds

3. Flux: the repository's desktop file, its install into a scratch
   data dir, and the app catalog finding, showing and launching it, and
   rescanning when it appears.
4. An app's bar (applink/view.py, the kinds) painted off screen on the
   bar's background and read back as pixels, from bar dicts: the tabs in
   equal slots, a box each (the current one's outline bright, its title
   strong and its image glowing cyan in brackets; the rest muted, their
   images and brackets a step dimmer; a tab with no image its letter in
   the same glow; long titles cut with an ellipsis; the run around the
   current tab when even the narrowest slots do not fit); the agent's
   hold glowing in her colour, dimmed when taken over; the glow made once
   per image and colour (repaints, hover and motion make none; an image
   replaced under its id makes a new one) and inside the tabs' rect,
   bloom and a lifted tab's included (their damage); the host a fixed
   width, the tabs' slots the same width whichever tab (and host) is
   current, a longer host cut with an ellipsis; the host with the open
   padlock and the
   bookmark; an open input's box in the items' place (the search mark,
   the text and its caret, the count at the right end, none while
   stepping, "no matches" in the error colour), the items back after
   it; the current tab's progress lighting its outline, a comet round a
   busy tab behind, hover; the motion (a switch sliding the light, a
   close wiping the tab out before the others slide; the clock asked
   for frames only while something moves).
4b. The mouse on an app's tabs, through the top bar's own Window on an
   off-screen canvas (canvas.Scene: its keyboard's takers counted, no
   compositor), fed the pointer and the keys as the canvas's routers
   deliver them, the events recorded by id: which tab is under x; a left
   and a right click selecting; a middle click closing (let go
   elsewhere: nothing); the drag threshold; a drag lifting the tab (the
   keyboard taken once, for Escape), the others making room, the marker,
   `move` before the tab it lands before (both ways; dropped in place:
   nothing); below the bar still a move, past TEAR_OUT_PX `detach`
   (never a window's only tab); Escape cancelling, other keys swallowed,
   a late Escape never closing the bar; no keyboard to take; the keyboard
   given back on every way out (the compositor breaking the press's grab
   among them); hover; the bar's other widgets as before.
5. The app service on a socket of its own, stand-in apps on it sending
   bars, the window manager stood in for (its focused window, as the
   test says it): the announce and the answer both ways (the agent's
   name and colour; an app other than Flux, the same class; announces
   that name no app or id; lines that are no message, types it does not
   know); a bar shown and replaced, a bad one keeping the last good (one
   warning), images held while a bar names them (a broken one refused,
   said once), an answer for nothing open dropped; focus followed
   between two windows (the compositor's focused window's process and
   the apps' own `active`), a window of another process (the context
   area empty), none, a window in the background changing without
   repainting the bar, a connection lost and back, the service stopped
   and started.
5b. The inputs and menus the app's chords open, the canvas's keyboard
   and outside clicks stood in for (taken and given back, recorded): an
   `open` with Ctrl+L from the focused window (the keyboard taken first,
   the outside clicks caught, the input open in the section, the
   address in it selected), the keys the app forwards and the canvas's
   own after them typed into its box in order, the chord again selecting
   its text, Escape closing it and giving both back; a menu; no top bar
   (fallback); `open` with no top bar; `open` from a new focused window
   before its first bar (waited for, open as the bar comes; with a bar
   without that input, `fallback` past OPEN_WAIT_S and why, at info).
6. Where an input's list drops: the section's left, the bar's bottom
   edge, an item's left for a menu; none with no top bar.
7. The top bar's spec, as computed data: anchors, size, layer, where it
   sits on a monitor against the bottom bar, its look against the
   bottom bar's, its place in WINDOWS, a user widget added the
   documented way, the whole bar painted off screen; the user's own
   config.py (when there is one) imported by its path with scratch XDG
   directories and no bytecode written, and the top bar in what it
   exports.

The real browser and the real shell together (Flux's bar in the top
bar, its chords, its line while the shell is away) run in
tests/wayland.py --nested; tests/browser.py drives Flux's end of the
line against a stand-in shell.

Prints ok/FAIL lines, exits 1 if anything failed. Nothing here touches
the running shell: no socket, lock, state or window of its, and nothing
shows on any screen.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from indigoshell.core.naming import APP, CLI           # noqa: E402  (pure)
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

    def __init__(self) -> None:
        self.damaged: list = []

    def damage(self, widget=None, *, layout: bool = False) -> None:
        self.damaged.append((widget, layout))


class Line:
    """What an AppWindow has of its connection: the service, the app's
    process (this one), and a send that keeps what was sent."""

    def __init__(self, service) -> None:
        self.service = service
        self.pid = os.getpid()
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


def announce(window, app: str = "flux") -> dict:
    """An app window's announce: the app, its own id for the window."""
    from indigoshell.applink import bar as B
    return B.announce(app, window)


def titles_of(window) -> list[str]:
    """The tab titles of the bar an app window shows."""
    bar = window.bar or {}
    tabs = next((i for i in bar.get("start", []) if i.get("kind") == "tabs"), {})
    return [t["title"] for t in tabs.get("tabs", [])]


def app_bar(titles, current=0, *, host="127.0.0.1", secure=False, progress=None,
            busy=(), bookmarked=False, ids=None, can=("close", "move", "detach"),
            images=None, letters=None, agents=None) -> dict:
    """A bar as an app with tabs and a host sends it (applink/bar.py's
    builders): the tabs, a box each, each with its image's id or letter
    by index (`images`, `letters`) and the agent's hold (`agents`:
    index -> state); the host after its marks, fixed as Flux's."""
    from indigoshell.applink import bar as B
    ids = ids or [101 + i for i in range(len(titles))]
    images, letters, agents = images or {}, letters or {}, agents or {}
    tabs = [B.tab(n, t, busy=i in busy or (progress is not None and i == current),
                  progress=progress if i == current else None, image=images.get(i),
                  letter=letters.get(i), agent=B.agent(agents[i]) if i in agents else None)
            for i, (t, n) in enumerate(zip(titles, ids))]
    spans = ([B.span(icon="bookmark")] if bookmarked else []) + (
        [] if secure else [B.span(icon="unlocked", color="error")]) + [B.span(host)]
    return B.bar([B.tabs("tabs", tabs, current=ids[current], can=can)],
                 [B.text("host", spans=spans, fixed=True)])


def icon_png(kind: str) -> bytes:
    """A 32 x 32 test icon as an app sends it: "marks" a dark ring on
    nothing (dark marks with a hole: turned round, they glow), "tile" a
    white tile with a dark bar across it (a light ground: it goes black,
    the bar glows), "dark" its opposite (white on black), "colour" four
    saturated quadrants."""
    import skia
    s = skia.Surface(32, 32)
    c = s.getCanvas()
    c.clear(skia.ColorTRANSPARENT)
    p = skia.Paint(AntiAlias=True)
    if kind in ("tile", "dark"):
        ground, bar = ((skia.ColorWHITE, skia.ColorSetRGB(20, 20, 20)) if kind == "tile"
                       else (skia.ColorBLACK, skia.ColorWHITE))
        p.setColor(ground)
        c.drawRect(skia.Rect.MakeWH(32, 32), p)
        p.setColor(bar)
        c.drawRect(skia.Rect.MakeXYWH(4, 12, 24, 8), p)
    elif kind == "colour":
        for (x, y), rgb in zip(((0, 0), (16, 0), (0, 16), (16, 16)),
                               ((230, 30, 40), (40, 200, 60), (30, 90, 240), (250, 200, 20))):
            p.setColor(skia.ColorSetRGB(*rgb))
            c.drawCircle(x + 8, y + 8, 7, p)
    else:
        p.setColor(skia.ColorSetRGB(24, 24, 28))
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(8)
        c.drawCircle(16, 16, 11, p)
    return bytes(s.makeImageSnapshot().encodeToData(skia.kPNG, 100))


def glow_px(region, rgb, k: float = 0.4) -> int:
    """Pixels of a glow in colour `rgb`: hue the same as `rgb`'s (each
    channel near `rgb` scaled by the pixel's own strength) and at least
    `k` of its brightness."""
    import numpy as np
    want = np.array(rgb, dtype=float)
    px = region.astype(float)
    level = px.max(axis=-1) / want.max()
    near = np.abs(px - want[None, None, :] * level[..., None]).max(axis=-1) <= 40
    return int((near & (level >= k)).sum())


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

    import skia

    from indigoshell import theme
    from indigoshell.applink.kinds import glow
    from indigoshell.applink.kinds import tabs as tabs_mod
    from indigoshell.applink.kinds.input import InputBox
    from indigoshell.applink.kinds.look import (BRACKET_FG, COMET_FPS, COMET_S, FIXED_W, LIFT,
                                                LOAD_FG, MOTION_FPS, OUTLINE, PAUSED_DIM,
                                                SLIDE_S, SLOT_GAP, SLOT_MIN, SLOT_W, SPOT_FG,
                                                SWITCH_S, TAB_CURRENT_FG, TAB_FG, TAB_HOVER_FG,
                                                WIPE_S, ICONS, ROLE_COLORS, blend)
    from indigoshell.core import persona
    from indigoshell.applink.kinds.tabs import fit_slots
    from indigoshell.applink.view import BarView

    HOST_FG, INSECURE_FG = theme.FG, theme.ERROR
    FIND_QUERY_FG, FIND_COUNT_FG, NO_MATCHES_FG = theme.FG_STRONG, ROLE_COLORS["info"], theme.ERROR
    section("4. an app's bar, painted off screen: its tabs, a box each, and its host")
    W, H = 1600, theme.TOP_BAR_HEIGHT

    class App:
        """An app window's section, as the shell keeps it: a BarView, and
        the images the app sent (here put straight in, decoded)."""

        def __init__(self) -> None:
            self.events = []
            self.images = {"marks": glow.decode(icon_png("marks")),
                           "tile": glow.decode(icon_png("tile"))}
            self.view = self.content = BarView(emit=self.events.append, images=self.images)

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

    def spot_of(img, t, pad=0):
        """A tab's image spot (and `pad` px round it), as pixels."""
        x, y, w = int(t["spot_x"]), int(t["spot_y"]), int(t["spot_w"])
        return img[y - pad:y + w + pad, x - pad:x + w + pad]

    cyan = hex_rgb(SPOT_FG)
    j, host = flux()
    j.on_message(app_bar(["Alpha", "Beta", "Gamma", "Delta"], images={0: "marks", 1: "marks",
                                                                     2: "tile"},
                         letters={3: "d"}))
    tl = j.view.item("tabs")
    img = paint(j.content, W, H)
    save(scratch / "content-tabs.png", img)
    tabs = tl.layout()
    strong = [count_color(in_rect(img, t["x"], t["w"]), TAB_CURRENT_FG) for t in tabs]
    muted = [count_color(in_rect(img, t["x"], t["w"]), TAB_FG) for t in tabs]
    glows = [glow_px(spot_of(img, t), cyan) for t in tabs]
    peak = [int(spot_of(img, t)[..., 2].max()) for t in tabs]
    brackets = [count_color(spot_of(img, t), BRACKET_FG, 20) for t in tabs]
    arms = [int(spot_of(img, t)[0, 0:3, 2].mean() - spot_of(img, t)[0, 8:12, 2].mean())
            for t in tabs]            # a bracket's arm along the spot's top, against its middle
    slots = [(t["slot_x"], t["slot_w"]) for t in tabs]
    check("four tabs from the left, each in a slot of the same width, no numbers: the current "
          "one's title strong, its image glowing cyan in corner brackets; the others' titles "
          "muted, their images and brackets a step dimmer; a tab with no image its letter in "
          "the same glow", [t["text"] for t in tabs] == ["Alpha", "Beta", "Gamma",
                                                                  "Delta"]
          and [t["shows"] for t in tabs] == ["image", "image", "image", "letter"]
          and tabs[0]["slot_x"] < 8 and all(w == SLOT_W for _x, w in slots)
          and slots[1][0] - slots[0][0] == slots[2][0] - slots[1][0] > SLOT_W
          and strong[0] > 20 and muted[0] < 5 and all(m > 20 for m in muted[1:])
          and all(v < 5 for v in strong[1:]) and glows[0] > 60 and glows[1] > 60
          and glows[2] > 15 and glows[3] > 10 and peak[0] > peak[1] + 40
          and brackets[0] > 12 and brackets[1] == brackets[2] == brackets[3] == 0
          and all(a > 30 for a in arms),
          f"strong {strong}, muted {muted}, glows {glows}, peak blue {peak}, "
          f"brackets {brackets}, arms {arms}")
    tile = spot_of(img, tabs[2])
    centre, ground = tile[9:11, 9:11], tile[3:5, 3:5]
    ring = spot_of(img, tabs[0])
    check("the effect: a white tile's ground goes black and the dark bar across it glows; a dark "
          "ring on nothing glows, its hole dark but for the bloom",
          int(ground.max()) < 30 and glow_px(centre, cyan, 0.3) == 4
          and glow_px(ring[3:5, 8:12], cyan, 0.8) == 8 and int(ring[10, 10, 2]) < 200,
          f"ground max {int(ground.max())}, bar {centre.tolist()}, ring {ring[3, 9].tolist()}, "
          f"hole {ring[10, 10].tolist()}")
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

    j.on_message(app_bar(["Alpha", "Beta", "Gamma"]))
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
    spotted = all(t["spot_x"] + t["spot_w"] < t["x"] for t in tabs)
    ends = max(t["slot_x"] + t["slot_w"] for t in tabs)
    inside = all(t["x"] + t["w"] <= t["slot_x"] + t["slot_w"] for t in tabs)
    current = next((t for t in tabs if t["current"]), {})
    idx = [t["index"] for t in tabs]
    check("twelve long titles in 900 px: slots of the narrowest width, the run of them that fits "
          "around the current one, each title cut with an ellipsis inside its slot, the "
          "current one still strong",
          len(tabs) == int((room.width() + SLOT_GAP) // (SLOT_MIN + SLOT_GAP))
          and idx == list(range(idx[0], idx[-1] + 1))
          and 5 in idx and spotted and inside and all("…" in t["text"] for t in tabs)
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
    leaving = [t for _s, t, _x, _g in tl._leaving]
    gamma = tl._glides[103]
    now = time_.monotonic()
    held = gamma.at(now + WIPE_S * 0.5)
    after = gamma.at(now + WIPE_S + SLIDE_S)
    check("a tab closed: it wipes out from its place (WIPE_S), and only then the ones after it "
          "slide over to close the gap", leaving == ["Beta"] and held == 2 * tl.pitch
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

    # The agent's hold: her colour, dimmed while the user has it.
    lilac = hex_rgb(persona.color())
    paused = hex_rgb(blend(persona.color(), theme.BAR_BG, PAUSED_DIM))
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], images={0: "marks", 1: "marks",
                                                             2: "marks"},
                         agents={1: "driving", 2: "paused"}))
    img = paint(j.content, W, H)
    save(scratch / "content-agent.png", img)
    tabs = tl.layout()
    held = [(glow_px(spot_of(img, t), cyan), glow_px(spot_of(img, t), lilac, 0.6),
             int(spot_of(img, t).max())) for t in tabs]
    check("the agent drives tab 2: its image glows in her colour (in brackets of it); tab 3, "
          "taken over: her colour dimmed, told apart from her own by its brightness; tab 1 "
          "still cyan", held[0][0] > 60 and held[0][1] == 0 and held[1][1] > 60
          and held[1][0] == 0 and held[2][0] == 0 and held[2][2] < held[1][2] - 40
          and glow_px(spot_of(img, tabs[2]), paused, 0.5) > 40
          and count_color(spot_of(img, tabs[1]), persona.color(), 20) > 12,
          f"(cyan, lilac, max) per tab {held}")

    # The glow is made once per image and colour, and stays inside the tabs' rect.
    made = glow.made
    for _ in range(3):
        paint(j.content, W, H)
    tl.set_hovered(True)
    tl.motion(tl.layout()[1]["slot_x"] + 60, 20)
    paint(j.content, W, H)
    tl.set_hovered(False)
    j.on_message(app_bar(["Alpha", "Beta", "Gamma"], 2, images={0: "marks", 1: "marks",
                                                                2: "marks"},
                         agents={1: "driving", 2: "paused"}))
    paint(j.content, W, H, settle=False)
    paint(j.content, W, H)
    again = glow.made - made
    j.images["marks"] = glow.decode(icon_png("marks"))     # sent again under its id
    paint(j.content, W, H)
    replaced = glow.made - made
    first = glow.of_image(j.images["tile"], SPOT_FG)
    same = glow.of_image(j.images["tile"], SPOT_FG) is first
    check("the glow made once per image and colour: three repaints, hover, a switch's motion "
          "made none; the image replaced under its id made one per colour shown (3); the same "
          "asked again is the same image", again == 0 and replaced == 3 and same,
          f"made {again} on repaints, {replaced} after the replacement")
    def drift(g, color):
        """How far a finished glow strays from its tint, premultiplied:
        the worst channel off the nearest tint x brightness, and past the
        tint at its alpha."""
        import numpy as np
        px = g.image.toarray(colorType=skia.kRGBA_8888_ColorType,
                             alphaType=skia.kPremul_AlphaType).astype(float).reshape(-1, 4)
        tint = np.array(hex_rgb(color), dtype=float)
        k = px[:, :3] @ tint / (tint @ tint)
        off = np.abs(px[:, :3] - k[:, None] * tint).max()
        over = (px[:, :3] - tint[None, :] * px[:, 3:4] / 255).max()
        return round(float(off), 2), round(float(over), 2)

    pure = {(kind, name): drift(glow.of_image(glow.decode(icon_png(kind)), color), color)
            for kind in ("dark", "tile", "colour")
            for name, color in (("cyan", SPOT_FG), ("lilac", persona.color()))}
    pure[("letter", "cyan")] = drift(glow.of_letter("w", SPOT_FG), SPOT_FG)
    check("every pixel of a glow (bloom included) is the tint times one brightness, never past "
          "the tint: white on black, black on white and a colourful icon, in cyan and in "
          "Lilac's colour, and a letter (premultiplied, within a rounding step)",
          all(off <= 1.0 and over <= 1.0 for off, over in pure.values()),
          f"(off the line, past the tint) {pure}")
    reach = glow.BLOOM.pad + LIFT
    inside = all(tl.rect.contains(skia.Rect.MakeXYWH(
        t["spot_x"] + (t["spot_w"] - glow.ICON_PX) / 2 - glow.BLOOM.pad,
        t["spot_y"] + (t["spot_w"] - glow.ICON_PX) / 2 - reach,
        glow.ICON_PX + 2 * glow.BLOOM.pad, glow.ICON_PX + glow.BLOOM.pad + reach))
        for t in tl.layout())
    g = glow.of_image(j.images["tile"], SPOT_FG)
    check("a glow reaches BLOOM.pad past its 16 px square, and with a lifted tab's LIFT still "
          "lies inside the tabs' rect (their paint bounds: what a repaint of them damages)",
          inside and g.image.width() == glow.ICON_PX + 2 * g.pad == g.image.height()
          and tl.paint_bounds() == tl.rect, f"pad {g.pad}, rect {tl.rect}")

    # The host a fixed width: the tabs' slots the same whichever tab is current.
    hosts = ["github.com", "developer.mozilla.org", "a-very-long-subdomain.of.some.example.org"]
    seen = []
    for i, name in enumerate(hosts):
        j.on_message(app_bar(["Alpha", "Beta", "Gamma"], i, host=name, secure=i != 1))
        img = paint(j.content, W, H)
        seen.append(([(t["slot_x"], t["slot_w"]) for t in tl.layout()],
                     j.view.item("host").rect.width(), host_layout(j.view), img))
    save(scratch / "content-fixed-host.png", seen[2][3])
    slots_same = seen[0][0] == seen[1][0] == seen[2][0]
    short, long_ = seen[0][2], seen[2][2]
    check("the host a fixed width (FIXED_W): switching between tabs with hosts of 10, 21 and 41 "
          "characters leaves the tabs' slots exactly where and as wide as they were; a short "
          "host against the right end, a long one cut with an ellipsis",
          slots_same and seen[0][1] == seen[1][1] == seen[2][1] == FIXED_W
          and short["text"] == "github.com" and short["x"] + short["w"] > W - 2
          and long_["text"].endswith("…") and long_["x"] + long_["w"] <= W + 0.5,
          f"slots {[s_[0][0] for s_ in seen]}, widths {[s_[1] for s_ in seen]}, "
          f"long {long_['text']!r}")


# ── 4b. the mouse on Flux's tabs ────────────────────────────────────────

def test_mouse(scratch: Path) -> None:
    import asyncio

    import skia

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.kinds.look import (OUTLINE, SLOT_GAP, SLOT_W, TAB_CURRENT_FG,
                                                TAB_HOVER_FG, TEAR_FG)
    from indigoshell.applink.kinds.tabs import DRAG_START_PX, TEAR_OUT_PX, landing, slot_at
    from indigoshell.applink.view import BarView
    from indigoshell.canvas import Scene
    from indigoshell.widgets.base import KeyEvent, Size, Widget
    from indigoshell.widgets.layout import Align, Row
    from indigoshell.window import Window, WindowSpec

    section("4b. the mouse on an app's tabs: the bar's window on an off-screen canvas, driven "
            "as the canvas's routers drive it (the keyboard's takers counted); events by id")
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
    scene = Scene(loop)
    scene.resize(W, H)
    scene.keyboard.available = True
    win = Window(scene, WindowSpec(name="test-top-bar", content=row), loop)
    win.create()                            # on the canvas (attached, laid out), never shown
    kb = scene.keyboard
    hold = ("hold", win)                    # the window's token while a press holds the keyboard
    counts = {"take": 0, "give": 0}
    real_take, real_give = kb.take, kb.give_back

    def take(token, handler):
        counts["take"] += token == hold
        real_take(token, handler)

    def give_back(token):
        counts["give"] += token == hold and kb.holds(token)
        real_give(token)
    kb.take, kb.give_back = take, give_back

    def held() -> int:
        """Whether the hold has the canvas's keyboard now (1) or not (0)."""
        return int(kb.holds(hold))
    dismissed = []
    win._dismiss_others = lambda: dismissed.append(1)
    closes = []
    win._request_close = lambda: closes.append(1)

    def lay_out():
        row.measure(Size(W, H))
        row.arrange(skia.Rect.MakeWH(W, H))

    def state(titles, current=0, ids=None):
        return app_bar(titles, current, host="x.org", secure=True, ids=ids)

    j.on_message = j.view.set_bar
    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    tl = j.view.item("tabs")

    def send(kind, x, y, button=0):
        """The pointer as the canvas's PointerRouter hands it on."""
        if kind in ("press", "release"):
            win.pointer_button(button, kind == "press", float(int(x)), float(int(y)))
        elif kind == "motion":
            win.pointer_motion(float(int(x)), float(int(y)))
        else:
            win.pointer_leave()

    def key(keysym: int, text: str = ""):
        """A key as the canvas's KeyboardRouter hands it on."""
        win.key_press(KeyEvent(keysym=keysym, text=text))

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
          and counts["take"] == 0 and win.holding is None and len(dismissed) == 3,
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
    still = tl.lift_layout() is None and counts["take"] == 0
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
          still and lifted is not None and lifted["to"] == 0 and counts["take"] == 1
          and over["to"] == 2 and placed == {1: 0, 2: pitch, 3: 2 * pitch}
          and mark > 4 and lift_px > 20
          and since(n) == [{"type": "move", "item": "tabs", "tab": 101, "before": 104}]
          and held() == 0
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
              {"type": "select", "item": "tabs", "tab": 104}] and held() == 0,
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
          and held() == 0, f"{since(n)}, new-window mark px {cue}")

    n = len(sent)
    send("press", mid(1), 20, 1)
    send("motion", mid(1) + 20, H + 20)
    send("release", mid(1) + 20, H + 20, 1)
    check("let go between the bar and the tear-out line: a move (here, to where it was: "
          "nothing)", since(n) == [{"type": "select", "item": "tabs", "tab": 102}]
          and held() == 0)

    j.on_message(state(["Solo"]))
    lay_out()
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(0) + 30, H + TEAR_OUT_PX + 50)
    solo = tl.lift_layout()
    send("release", mid(0) + 30, H + TEAR_OUT_PX + 50, 1)
    check("a window's only tab is never torn out: no mark, nothing sent",
          solo is not None and not solo["out"] and since(n) == [] and held() == 0,
          f"{solo} {since(n)}")

    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(3), 20)
    key(0x61, "a")                          # swallowed, the drag goes on
    going = tl.lift_layout() is not None
    key(0xFF1B)                             # Escape
    gone = tl.lift_layout() is None and win.holding is None and held() == 0
    send("motion", mid(3) + 5, 20)
    send("release", mid(3), 20, 1)
    key(0xFF1B)                             # late: the hold is over
    check("Escape while dragging puts the tab back: the keyboard given back at once, the "
          "release after it does nothing, another key in the drag is swallowed, and an Escape "
          "after the hold reaches nothing (never the window's Escape-closes)",
          going and gone and since(n) == [] and closes == [] and held() == 0,
          f"{since(n)}")

    kb.available = False                    # no keymap: no keyboard to take
    n = len(sent)
    send("press", mid(0), 20, 1)
    send("motion", mid(1) + 10, 20)
    send("release", mid(1) + 10, 20, 1)
    kb.available = True
    check("no keyboard to take: the drag works without Escape, nothing to give back",
          since(n) == [{"type": "move", "item": "tabs", "tab": 101, "before": 103}]
          and counts["give"] == counts["take"])

    # Every way out of a hold gives the keyboard back.
    j.on_message(state(["Alpha", "Beta", "Gamma", "Delta"]))
    lay_out()
    outs = {}

    def lift_at(i):
        send("press", mid(i), 20, 1)
        send("motion", mid(i) + 40, 20)
        return held() == 1 and win.holding is tl

    began = lift_at(0)
    j.on_message(state(["Beta", "Gamma", "Delta"], 0, [102, 103, 104]))
    outs["its tab closed meanwhile"] = began and held() == 0 and win.holding is None \
        and tl.drag is None
    n = len(sent)
    send("release", mid(1), 20, 1)
    outs["... and its release then does nothing"] = since(n) == []
    lay_out()

    began = lift_at(0)
    ctx = row.children()[0]
    ctx._on_focus(None)
    outs["the app window gone (its content taken off the bar)"] = began and held() == 0 \
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
    outs["an exception in its handler"] = began and held() == 0 and win.holding is None \
        and tl.drag is None
    send("release", mid(1), 20, 1)

    began = lift_at(0)
    win.pointer_lost()                      # the compositor broke the press's grab
    outs["its release never coming (the compositor broke the press's grab)"] = began \
        and held() == 0 and win.holding is None and tl.drag is None
    send("release", mid(1), 20, 1)

    began = lift_at(0)
    win._end_hold(cancel=True)              # what park, dismiss and destroy call first
    outs["the window parked, dismissed or destroyed"] = began and held() == 0 \
        and tl.drag is None
    send("release", mid(1), 20, 1)
    for name, ok in outs.items():
        check(f"the keyboard given back: {name}", ok)
    check("every take given back, none twice", counts["give"] == counts["take"] > 0,
          f"{counts['take']} taken, {counts['give']} given back")

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


class Follower:
    """The window manager's WindowFollower, stood in for: the test says
    which window has the focus (`service.set_focus`)."""

    ready = True

    def close(self) -> None:
        pass


async def test_service(scratch: Path) -> None:
    import types

    from indigoshell import theme
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.service import AppService, AppWindow
    from indigoshell.applink import bar as Bv
    from indigoshell.applink.kinds.look import TAB_CURRENT_FG

    section("5. the app service: announces both ways, focus, losses, the service stopped and "
            "started")
    rec = Records("indigoshell.applink.service", "indigoshell.applink.prompt")
    # The apps' process (this one's, for every stand-in app) and another.
    ours = types.SimpleNamespace(id=11, pid=os.getpid(), app=f"{APP}-browser")
    kitty = types.SimpleNamespace(id=12, pid=1, app="kitty")
    sock = os.path.join(runtime_dir(), f"{APP}-tbt-{os.getpid()}-apps.sock")
    service = AppService(path=sock, windows=Follower())
    apps: list[App] = []

    def window_of(app_id, kind: str = "flux"):
        """The newest app window announced with that id."""
        return next((w_ for w_ in reversed(service.windows)
                     if w_.app_id == app_id and w_.kind == kind), None)

    async def focus(target: App, window) -> bool:
        """`target`'s window has the keyboard focus: it says it is active and
        every other app window says it is not, as real apps do."""
        for x_ in apps:
            if not x_.w.is_closing():
                x_.send(Bv.active(x_ is target))
        return bool(await until(lambda: service.focused is window, 2))

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

        a = await app(announce(1), flux_bar(["Alpha", "Beta"]))
        got = await a.answer()
        wa = await until(lambda: (w_ := window_of(1)) and len(titles_of(w_)) == 2 and w_)
        check("an app window (Flux's): answered {type: attached} with the agent's name and "
              "colour, nothing else; kept with its own id, its process and its bar",
              got == {"type": "attached", "agent": {"name": "Lilac", "color": "#c8a2c8"}}
              and isinstance(wa, AppWindow) and wa.kind == "flux" and wa.app_id == 1
              and wa.link.pid == os.getpid(), f"{got}")
        b = await app(announce(2), flux_bar(["Gamma"]))
        await b.answer()
        wb = await until(lambda: (w_ := window_of(2)) and w_.bar and w_)

        ctx = AppContext(service)
        host = Host()
        ctx.attach(host)
        check("the context area hooked in: the service knows where it is", service.context is ctx)

        service.set_focus(ours)
        neither = service.focused
        focused = await focus(a, wa)
        check("the compositor's focused window is of the apps' process: the window of it that "
              "says it is active is focused (none while neither has said), the context area "
              "showing it", neither is None and focused and ctx.shown is wa.content,
              f"{service.focused and service.focused.label}")

        await focus(b, wb)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        texts = [t["text"] for t in wb.view.item("tabs").layout()]
        check("focus on the other Flux window: its tabs; the first's content let go (no window)",
              ctx.shown is wb.content and texts == ["Gamma"]
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

        # Images: held while a bar names them, each good bar letting go
        # of the rest; a broken one said, never held.
        def with_images(*names):
            bar_ = json.loads(json.dumps(flux_bar(["Gamma", "Epsilon"])))
            for t, name in zip(bar_["start"][0]["tabs"], names):
                if name:
                    t["image"] = name
            return bar_

        def shown_images():
            """Each tab's image as the window's tabs item finds it."""
            tl_ = wb.view.item("tabs")
            return [i is not None and tl_.image_of(i) is not None for i in tl_.images]

        mark = len(rec.records)
        b.send(Bv.image("i1", icon_png("marks")))
        b.send(Bv.image("i2", icon_png("tile")))
        b.send(with_images("i1", "i1"))
        both = await until(lambda: shown_images() == [True, True] and set(wb.images))
        shows = shown_images()
        b.send(Bv.image("bad", b"GIF89a" + bytes(40)))
        b.send({"type": "image", "id": "garbled", "png": Bv.image("g", Bv.PNG_SIGNATURE
                + icon_png("marks")[8:40])["png"]})
        b.send(with_images(None, "i1"))
        await until(lambda: shown_images() == [False, True])
        after = set(wb.images)
        b.send(flux_bar(["Gamma", "Epsilon"]))
        await until(lambda: not wb.images)
        warned = [m for m in rec.at_least(logging.WARNING, mark) if "image ignored" in m]
        check("images: two sent before the bar naming one of them on both tabs, held and shown "
              "(the other let go with that bar); a GIF and a PNG that does not decode "
              "refused, said once each; a bar going without the first tab's image keeps the "
              "second's; one naming none lets go of all",
              both == {"i1"} and shows == [True, True] and after == {"i1"}
              and not wb.images and len(warned) == 2 and "png is no PNG" in warned[0]
              and "does not decode" in warned[1],
              f"held {both} -> {after} -> {set(wb.images)}; shows {shows}; {warned}")

        service.set_focus(kitty)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        check("focus on a window of another process (no app's): the context area empty (the "
              "bar's background)", service.focused is None and ctx.shown is None
              and share_of(img, hex_rgb(theme.BAR_BG)) == 1.0)
        service.set_focus(None)
        check("no window with focus: empty", service.focused is None and ctx.shown is None)

        service.set_focus(ours)
        await focus(a, wa)
        a.close()
        gone = await until(lambda: window_of(1) is None)
        check("the focused window's connection lost: dropped, its bar with it, the context area "
              "empty (the other window says it is not active)",
              bool(gone) and ctx.shown is None and service.focused is None
              and wa.closed and wb in service.windows)
        a2 = await app(announce(1), Bv.active(True), flux_bar(["Alpha again"]))
        await a2.answer()
        wa2 = await until(lambda: (w_ := window_of(1)) and w_.bar and w_)
        check("... and back on a new connection (Flux started again), saying it is active: "
              "shown again",
              wa2 is not None and wa2 is not wa and ctx.shown is wa2.content
              and titles_of(wa2) == ["Alpha again"])

        e = await app(announce(5), flux_bar(["Popup"], single=True))
        got = await e.answer()
        we = await until(lambda: (w_ := window_of(5)) and w_.bar and w_)
        check("a window whose bar has no inputs (Flux's sign-in popup, the inspector): attached, "
              "its bar kept (no chords in it)",
              got.get("type") == "attached" and we is not None and Bv.chords(we.bar) == {})

        mark = len(rec.records)
        g = await app(announce("main", app="ide"))
        got = await g.answer()
        g2 = await app(announce("second", app="ide"))
        await g2.answer()
        wg = await until(lambda: window_of("main", "ide"))
        await focus(g, wg)
        img = paint(ctx, 1600, theme.TOP_BAR_HEIGHT)
        check("another app (\"ide\"): the same window class, nothing of its own in the shell; "
              "kept and answered, its section empty until its bar comes",
              got.get("type") == "attached" and type(wg) is AppWindow and wg.kind == "ide"
              and wg.app_id == "main" and wg.bar is None and ctx.shown is wg.content
              and share_of(img, hex_rgb(theme.BAR_BG)) == 1.0
              and not rec.at_least(logging.WARNING, mark))

        bad = []
        for msg in ({"type": "announce", "window": 1},
                    {"type": "announce", "app": "", "window": 1},
                    {"type": "announce", "app": 3, "window": 1},
                    {"type": "announce", "app": "flux"},
                    {"type": "announce", "app": "flux", "window": True},
                    {"type": "announce", "app": "flux", "window": "x" * (Bv.ID_MAX + 1)}):
            z = await app(msg)
            bad.append(await z.answer(0.3))
        early = await app(flux_bar(["Early"]), announce(9))
        got_early = await early.answer()
        check("announces that name no app or no id of the app's (none, empty, not a string; "
              "none, a bool, too long): ignored, no answer; a bar before the announce: ignored",
              bad == [None] * 6 and got_early.get("type") == "attached"
              and not any(titles_of(w_) == ["Early"] for w_ in service.windows), f"{bad}")

        mark = len(rec.records)
        b.w.write(b'not json\n{"no": "type"}\n[1, 2]\n')
        b.send({"type": "from-the-future", "x": 1})
        b.send(announce(2))
        b.send(flux_bar(["Gamma", "Epsilon", "Zeta"]))
        await until(lambda: len(titles_of(wb)) == 3)
        said = [m for m in rec.at_least(logging.DEBUG, mark)
                if "no message" in m or "not understood" in m or "announced again" in m]
        check("lines that are no message, a type nobody knows, a second announce on one "
              "connection: each ignored with one debug line, and the line goes on",
              len(titles_of(wb)) == 3 and len(said) == 5
              and not rec.at_least(logging.WARNING, mark), f"{said}")

        await focus(b, wb)
        service._restarting = lambda: True          # as the daemon says on a reload
        service.stop()
        del service._restarting
        notes = [await x_.answer(1) for x_ in (b, e, a2, g)]
        eofs = [await x_.answer(1) for x_ in (b, e, a2, g)]
        check("the service stopped for a reload: every app told {type: restarting}, then hears "
              "its line close; the socket file gone; the context area empty",
              all(n_ == {"type": "restarting"} for n_ in notes)
              and all(x_ == {"eof": True} for x_ in eofs)
              and not os.path.exists(sock) and not service.windows and ctx.shown is None,
              f"{notes} {eofs} sock {os.path.exists(sock)} windows {len(service.windows)} "
              f"shown {ctx.shown}")
        service.start()
        await until(lambda: service.listening, 2)
        b2 = await app(announce(2), Bv.active(True), flux_bar(["Gamma back"]))
        got = await b2.answer()
        wb2 = await until(lambda: (w_ := window_of(2)) and w_.bar and w_)
        check("started again: the app announces again and says it is active (the compositor's "
              "focus as it was): shown again", got.get("type") == "attached"
              and wb2 is not None and ctx.shown is wb2.content)
        ctx.detach()
        check("the context area detached: unhooked from the service",
              service.context is None and ctx._on_focus not in service._followers)
        expected = ("an app announced", "a bar ignored", "an image ignored")
        unexpected = [m for m in rec.at_least(logging.WARNING)
                      if not any(e_ in m for e_ in expected)]
        check("no warnings or errors but the ones for the announces that are none, the bad "
              "bar and the broken images", unexpected == [], f"{unexpected[:3]}")
    finally:
        for x_ in apps:
            x_.close()
        service.stop()


# ── 5b. the inputs and menus the app's chords open ─────────────────────

def test_prompts(scratch: Path) -> None:
    from indigoshell.applink import bar as Bv
    from indigoshell.applink.service import AppService, AppWindow
    from indigoshell.widgets.base import KeyEvent

    section("5b. the inputs and menus the app's chords open (the canvas's keyboard and outside "
            "clicks stood in for)")
    # The address list reads the history and the bookmarks: scratch ones.
    saved_env = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
    os.environ.update(XDG_CONFIG_HOME=str(scratch / "prompts-config"),
                      XDG_DATA_HOME=str(scratch / "prompts-data"))
    rec = Records("indigoshell.applink.service", "indigoshell.applink.prompt")
    service = AppService(path="/nonexistent")
    service.running = True                  # started, as far as the prompts go (no socket here)
    calls: list[str] = []
    service.hold_keyboard = lambda: calls.append("keyboard") or True
    service.release_keyboard = lambda: calls.append("let go")
    service._catch_outside = lambda: calls.append("outside")
    service._release_outside = lambda: calls.append("outside let go")
    drops = []
    service.context = stand_in_bar()
    service.show_drop = lambda prompt, place: drops.append((prompt.name, place))
    service.hide_drop = lambda: drops.append("hide")

    def flux(window_id, single=False, bar=True):
        line = Line(service)
        window = AppWindow(line, announce(window_id))
        service.added(window)
        if bar:                     # its bar: its inputs and menu
            window.on_message(flux_bar(["Alpha"], host="a.example", single=single))
        return window, line

    def key(sym, text="", **mods):
        """A key the canvas gets while the service has its keyboard."""
        service.key_press(KeyEvent(keysym=sym, text=text, **mods))

    try:
        wa, la = flux(1)
        wa.on_message(Bv.active(True))
        focused = service.focused is wa
        n = len(calls)
        wa.on_message(Bv.open_input("address", "ctrl+l"))
        p = service.prompt
        address = p.box.field.text if p is not None and p.box is not None else None
        check("Ctrl+L caught by the focused app window (`open` with its chord): first the "
              "keyboard taken, then the outside clicks caught; the address input open in the "
              "bar, the page's address in it, selected; the keyboard not given back (the input "
              "has it)", focused and calls[n:] == ["keyboard", "outside"]
              and p is not None and p.name == "address" and wa.view.box is p.box
              and bool(address) and p.box.field.selected_all, f"{calls[n:]} {address!r}")
        wa.on_message(Bv.keys([Bv.key(ord("y"), "y")]))
        key(ord("o"), "o")                  # the canvas's own, before the app's `last`
        waited = p.box.field.text
        wa.on_message(Bv.keys([], last=True))
        check("the keys the app's window got after the chord (forwarded, `keys`) and the "
              "canvas's own after them: into the box in the order typed (the first replacing "
              "the selected address; the canvas's waiting for the app's `last`)",
              waited == "y" and p.box.field.text == "yo", f"{waited!r} {p.box.field.text!r}")
        key(ord("l"), ctrl=True)
        check("Ctrl+L again while it is open: a key for it (the text selected whole), not a "
              "second input", service.prompt is p and p.box.field.selected_all)
        n = len(calls)
        key(0xFF1B)
        check("Escape: it closes; the keyboard and the outside clicks given back, the items "
              "back in the section",
              service.prompt is None and "let go" in calls[n:]
              and "outside let go" in calls[n:] and wa.view.box is None, f"{calls[n:]}")

        n = len(calls)
        wa.on_message(Bv.open_menu("actions", "ctrl+m"))
        m = service.prompt
        check("Ctrl+M caught: the actions menu, the keyboard taken for it",
              m is not None and m.what == "menu" and m.name == "actions"
              and "keyboard" in calls[n:] and "let go" not in calls[n:])
        wa.on_message(Bv.keys([], last=True))   # the window lost the keyboard: nothing forwarded
        key(0xFF1B)
        closed = service.prompt is None
        service.context = None
        n = len(calls)
        wa.on_message(Bv.open_input("new_tab", "ctrl+t"))
        check("Escape closing the menu (once the app's `last` came); Ctrl+T caught with no "
              "top bar to show it in: the keyboard taken, then given back, and the app told to "
              "do without: {type: fallback, input: new_tab}",
              closed and calls[n:] == ["keyboard", "let go"] and service.prompt is None
              and la.out[-1] == {"type": "fallback", "input": "new_tab"},
              f"{calls[n:]} {la.out[-1]}")
        n, sent = len(calls), len(la.out)
        wa.on_message({"type": "open", "input": "address"})
        check("{type: open} from the app (a new window), focused, with no top bar: the same, "
              "with no key pressed, so nothing taken or given back", calls[n:] == []
              and la.out[sent:] == [{"type": "fallback", "input": "address"}])
        service.context = stand_in_bar()

        # A new window's `open` before its first bar, the window focused as
        # the service hears of it (the window manager focuses a window as it
        # maps).
        wg, lg = flux(7, bar=False)
        n = len(calls)
        wg.on_message(Bv.active(True))
        wg.on_message({"type": "open", "input": "address"})
        early = service.prompt
        wg.on_message(flux_bar(["Alpha"], host="a.example"))
        p = service.prompt
        check("{type: open} from a new window before its first bar, the window focused: waited "
              "for; the address input open as its bar comes, the keyboard taken for it, nothing "
              "said to the app", early is None and p is not None and p.owner is wg
              and p.name == "address" and "keyboard" in calls[n:]
              and not [m for m in lg.out if m.get("type") == "fallback"],
              f"{p and p.name}; to the app {[m.get('type') for m in lg.out]}")
        key(0xFF1B)
        wh, lh = flux(8, bar=False)
        mark = len(rec.records)
        wh.on_message(Bv.active(True))
        wh.on_message({"type": "open", "input": "address"})
        wh.on_message(flux_bar(["Alpha"], host="a.example", single=True))
        waited = service._open_wait is not None and service.prompt is None
        service._open_late()        # OPEN_WAIT_S past (no loop here: its timer, by hand)
        said = rec.count("still waiting for that input in its bar", logging.INFO, mark)
        check("... a bar that comes without that input (a single window's): waited for until "
              "OPEN_WAIT_S, then `fallback` to the app, and why, at info", waited
              and [m for m in lh.out if m.get("type") == "fallback"]
              == [{"type": "fallback", "input": "address"}] and said == 1,
              f"to the app {lh.out[-1:]}, said {said}")
        unexpected = rec.at_least(logging.WARNING)
        check("no warnings or errors", unexpected == [], f"{unexpected[:3]}")
    finally:
        for w_ in list(service.windows):
            service.forget(None, w_)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── 6. where the list drops from ────────────────────────────────────────

def test_placement() -> None:
    import types

    import skia

    from indigoshell.applink.service import AppService, AppWindow

    section("6. where an input's list drops")
    service = AppService(path="/nonexistent")
    jw = AppWindow(Line(service), announce(1))
    jw.on_message(flux_bar(["Alpha"]))
    alone = service.section()
    opened = service.open_prompt(jw, "menu", "actions", pressed=False)
    service.context = stand_in_bar(width=1800, left=12)
    menu = types.SimpleNamespace(anchor=None)
    item = types.SimpleNamespace(anchor=types.SimpleNamespace(
        rect=skia.Rect.MakeXYWH(300, 0, 60, 34)))
    places = (service.section(), service._drop_place(menu), service._drop_place(item))
    check("no top bar: no section, an input or menu cannot show (the app told to do without)",
          alone is None and opened is False
          and jw.link.out[-1] == {"type": "fallback", "menu": "actions"}, f"{alone} {opened}")
    check("a top bar: the list drops at the section's left, under the bar's bottom edge, the "
          "section's width; a menu opened from an item, under the item",
          places[0] == (12, 42, 1800) and places[1] == (12, 42, 1800)
          and places[2] == (300, 42, 1800), f"{places}")


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
    import skia

    from indigoshell import config_default as cd, theme
    from indigoshell.applink import bar as Bv
    from indigoshell.applink.context import AppContext
    from indigoshell.applink.kinds.look import TAB_CURRENT_FG
    from indigoshell.applink.service import AppService, AppWindow
    from indigoshell.canvas import Scene
    from indigoshell.core.paths import config_dir
    from indigoshell.widgets.label import Label
    from indigoshell.window import Anchor, Layer, Monitor, Window

    section("7. the top bar's spec, as computed data")
    names = [s.name for s in cd.WINDOWS]
    top = next(s for s in cd.WINDOWS if s.name == cd.TOP_BAR)
    bar = next(s for s in cd.WINDOWS if s.name == "bar")
    check("in WINDOWS right after the bottom bar, opened at start like it",
          names.index(cd.TOP_BAR) == names.index("bar") + 1 and top.autostart, f"{names[:4]}")
    check("anchored to the top edge, full width, as tall as the bottom bar (TOP_BAR_HEIGHT), a "
          "dock that reserves its space, never taking the keyboard",
          top.anchor == Anchor.TOP and top.size == (None, theme.TOP_BAR_HEIGHT) == bar.size
          and bar.size == (None, theme.BAR_HEIGHT) and top.layer == Layer.DOCK
          and top.exclusive and not top.grab_keyboard and top.margin == bar.margin)
    check("the bottom bar's look: its background, gutter, rule (none: the bottom bar draws none "
          "either) and font size",
          (top.background, top.padding, top.rule, top.rule_thick, top.rule_glow)
          == (bar.background, bar.padding, bar.rule, bar.rule_thick, bar.rule_glow)
          and top.rule is None, f"rule {top.rule!r}")
    first = list(top.content.children())[0]
    check("its row: the context area first, taking the room, on the service in SERVICES",
          isinstance(first, AppContext) and first.flex == 1 and first.service is cd.APPS
          and cd.APPS in cd.SERVICES and isinstance(cd.APPS, AppService))

    scene = Scene(None)

    def place(spec, mon):
        """Where a window of `spec` sits on monitor `mon` (made nowhere)."""
        return Window(scene, spec)._compute_geometry(mon)

    one = Monitor(0, 0, 2560, 1440, True, "HDMI-0")
    th = theme.TOP_BAR_HEIGHT
    geo, bgeo = place(top, one), place(bar, one)
    check(f"on a 2560x1440 monitor: 2560x{th} at +0+0; the bottom bar for comparison: 42 px at "
          "the bottom", geo == (0, 0, 2560, th) and bgeo == (0, 1398, 2560, 42), f"{geo} {bgeo}")
    right = Monitor(1920, 0, 2560, 1440, True, "A")
    geo2, bgeo2 = place(top, right), place(bar, right)
    check("on a monitor right of another: across that one, like the bottom bar",
          geo2 == (1920, 0, 2560, th) and bgeo2 == (1920, 1398, 2560, 42), f"{geo2} {bgeo2}")

    # The whole bar painted, with a widget of the user's at the right.
    svc = AppService(path="/nonexistent")
    spec = cd.top_bar(Label("MINE", color=theme.CYAN_BRIGHT), apps=svc)
    win = Window(scene, spec)
    W, H = 2560, theme.TOP_BAR_HEIGHT
    win.content_rect = skia.Rect.MakeWH(W, H)
    win.content_root, win.width, win.height = (0, 0, W, H), W, H
    spec.content.attach(win)
    jw = AppWindow(Line(svc), announce(1))
    jw.on_message(flux_bar(["Alpha", "Beta"], loading=True, progress=50))
    svc.added(jw)
    jw.on_message(Bv.active(True))
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
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
        test_desktop(scratch)
        test_content(scratch)
        test_mouse(scratch)
        asyncio.run(test_service(scratch))
        test_prompts(scratch)
        test_placement()
        test_spec(scratch)
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
