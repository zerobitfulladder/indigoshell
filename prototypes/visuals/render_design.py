"""The design reference's samples (docs/design/samples), rendered from the
real code, offline.

Every picture here is painted by the shipped widgets, shapes, effects and
theme — the bottom bar is config_default's own `bar` row, the panels are
the real panel classes, the call is `CallHost` — fed neutral, made-up
state instead of the machine's. Nothing opens a window, captures the
screen, starts a subprocess or touches a running shell: CPU raster
surfaces only, the clock is this script's (`NOW`), and XDG config/state
point at throwaway directories so no user plugin, history or state is
read.

Each sample is written at 1x (how it really looks); small things also get
a nearest-neighbour 3x crop (`*_3x.png`) for the detail. A few short GIFs
(30 fps or less, cropped tight) show the motion a still cannot.

    PYTHONPATH=../.. ../../.venv/bin/python -B render_design.py [OUT_DIR]
        [--only NAME ...] [--tokens FILE] [--list]

OUT_DIR defaults to docs/design/samples in the checkout. `--tokens FILE`
also writes the machine-readable tokens (values read from the modules,
each with its file:constant), which is how docs/design/tokens.json is
made. GIFs need ffmpeg.
"""

import argparse
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import types

# Throwaway config and state before anything of the shell is imported:
# no user plugin is loaded, no history or remembered state is read.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="design-config-")
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="design-state-")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

# readme_call owns the harness's clock: it replaces time.monotonic with
# `NOW[0]` at import. Every widget here reads that clock.
import readme_call as rc                                    # noqa: E402

NOW = rc.NOW

import skia                                                 # noqa: E402

from common import C                                        # noqa: E402
from indigoshell import shapes, text as T, theme            # noqa: E402
from indigoshell.widgets.base import Insets, Size, Widget   # noqa: E402

FPS = 30
DEFAULT_OUT = os.path.join(REPO, "docs", "design", "samples")
OUT = DEFAULT_OUT
WRITTEN: list[str] = []

SHEET_BG = "#1b1b21"        # the neutral grey the sheets sit on (not a token)
LABEL_FG = "#b8bccb"
LABEL_DIM = "#7d8194"


# ═══════════════════════════════════════════════════════════════════════
# output
# ═══════════════════════════════════════════════════════════════════════
def path(name: str) -> str:
    return os.path.join(OUT, name)


def save(img: skia.Image, name: str) -> skia.Image:
    img.save(path(name), skia.kPNG)
    WRITTEN.append(name)
    print(f"  {name:34s} {img.width()}x{img.height()}  "
          f"{os.path.getsize(path(name)) / 1024:.0f} KB")
    return img


def surface(w: float, h: float, bg: str | None = None):
    s = skia.Surface(int(math.ceil(w)), int(math.ceil(h)))
    c = s.getCanvas()
    c.clear(C(bg) if bg else skia.ColorTRANSPARENT)
    return s, c


def snap(s: skia.Surface) -> skia.Image:
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def crop(img: skia.Image, x: float, y: float, w: float, h: float) -> skia.Image:
    s, c = surface(w, h)
    c.drawImage(img, -x, -y)
    return snap(s)


def zoom(img: skia.Image, k: int = 3) -> skia.Image:
    """Nearest-neighbour enlargement: every pixel a k x k block."""
    s, c = surface(img.width() * k, img.height() * k)
    c.scale(k, k)
    c.drawImage(img, 0, 0, skia.SamplingOptions(skia.FilterMode.kNearest))
    return snap(s)


def encode_gif(name: str, frames: list[skia.Image], fps: int = FPS) -> None:
    w, h = frames[0].width(), frames[0].height()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
           "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-filter_complex",
           "[0:v]split[a][b];[a]palettegen=stats_mode=diff:max_colors=200[p];"
           "[b][p]paletteuse=dither=none:diff_mode=rectangle",
           "-loop", "0", path(name)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for img in frames:
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    WRITTEN.append(name)
    print(f"  {name:34s} {w}x{h}  {len(frames)} frames @ {fps} fps  "
          f"{os.path.getsize(path(name)) / 1024:.0f} KB")


# ═══════════════════════════════════════════════════════════════════════
# sheets: captions in a plain sans, so nothing reads as part of the look
# ═══════════════════════════════════════════════════════════════════════
_LABEL_FONTS: dict = {}


def label_font(size: float, bold: bool = False) -> skia.Font:
    key = (size, bold)
    if key not in _LABEL_FONTS:
        style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
        tf = skia.FontMgr().matchFamilyStyle("DejaVu Sans", style) \
            or skia.Typeface.MakeDefault()
        f = skia.Font(tf, size)
        f.setEdging(skia.Font.Edging.kAntiAlias)
        f.setSubpixel(True)
        _LABEL_FONTS[key] = f
    return _LABEL_FONTS[key]


def caption(c, s: str, x: float, y: float, size: float = 11,
            color: str = LABEL_FG, bold: bool = False) -> float:
    f = label_font(size, bold)
    p = skia.Paint(AntiAlias=True)
    p.setColor(C(color))
    c.drawString(s, x, y, f, p)
    return f.measureText(s)


def sheet(tiles, *, cols: int = 1, gap: int = 14, pad: int = 14,
          cap_h: int = 20, title: str | None = None) -> skia.Image:
    """tiles: [(caption, image)], laid out in `cols` columns, each caption
    above its tile, on the neutral grey."""
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    col_w = [max((r[k][1].width() for r in rows if k < len(r)), default=0)
             for k in range(cols)]
    row_h = [max(t[1].height() for t in r) + cap_h for r in rows]
    top = pad + (26 if title else 0)
    W = pad * 2 + sum(col_w) + gap * (cols - 1)
    H = top + sum(row_h) + gap * (len(rows) - 1) + pad
    s, c = surface(W, H, SHEET_BG)
    if title:
        caption(c, title, pad, pad + 14, 13, LABEL_FG, bold=True)
    y = top
    for r, rh in zip(rows, row_h):
        x = pad
        for k, (cap, img) in enumerate(r):
            if cap:
                caption(c, cap, x, y + 13, 11, LABEL_FG)
            c.drawImage(img, x, y + cap_h)
            x += col_w[k] + gap
        y += rh + gap
    return snap(s)


# ═══════════════════════════════════════════════════════════════════════
# backdrops and windows
# ═══════════════════════════════════════════════════════════════════════
def desktop(w: int, h: int) -> skia.Image:
    """A plain dark desktop to lay panels over (render_panels' stand-in:
    BASE_SHADOW with a faint BASE_GUTTER grid)."""
    from render_panels import desktop_bg
    return desktop_bg(int(w), int(h))


def city(w: int, h: int) -> skia.Image:
    """readme_call's neon city at night: something busy to frost."""
    return rc.city(int(w), int(h))


class Host:
    """What a widget tree asks of its window, off screen."""
    class spec:
        name = "design"
        holo = False
    loop = None
    wid = None
    _phase = "idle"
    x = y = 0

    def damage(self, widget=None, *, layout=False):
        pass

    def damage_rect(self, rect):
        pass

    def resize_content(self, w, h):
        pass

    def reshape(self):
        pass


def place(widget: Widget, x: float, y: float, w: float | None = None,
          h: float | None = None) -> Size:
    """Measure at the natural size (or w x h) and arrange at (x, y)."""
    m = widget.measure(Size(10_000.0, 10_000.0)) if w is None or h is None \
        else Size(w, h)
    w = m.width if w is None else w
    h = m.height if h is None else h
    widget.measure(Size(w, h))
    widget.arrange(skia.Rect.MakeXYWH(x, y, w, h))
    return Size(w, h)


def layer(paint_fn, W: int, H: int) -> skia.Image:
    """A transparent window surface with `paint_fn(canvas)` on it."""
    s, c = surface(W, H)
    paint_fn(c)
    return snap(s)


def holo_window(content: Widget, W: int, H: int, *, scan_at: float | None = None,
                rect: skia.Rect | None = None) -> skia.Image:
    """A `WindowSpec(holo=True)` window's surface: the content, the panels'
    raster over it composited SrcATop (window._paint_holo), and the scan
    band `scan_at` seconds into its pass (None: between passes)."""
    from indigoshell.widgets.holo import Projection, raster
    s, c = surface(W, H)
    content.paint(c)
    r = rect or content.rect
    lines = skia.Paint(Shader=raster(theme.POPUP_RASTER))
    lines.setBlendMode(skia.BlendMode.kSrcATop)
    c.drawRect(r, lines)
    if scan_at is not None:
        proj = Projection()
        proj.window = Host()
        proj.arrange(r)
        proj.epoch = NOW[0] - scan_at
        proj.animate(NOW[0])
        proj.paint(c)
    return snap(s)


def over(back: skia.Image, top: skia.Image, blur: skia.Path | None = None,
         sigma: float = 9.0) -> skia.Image:
    """`top` composed over `back`; with `blur`, what is behind blurred
    inside that shape first (picom's blur-behind, as render_call does)."""
    s, c = surface(back.width(), back.height())
    c.drawImage(back, 0, 0)
    if blur is not None:
        c.save()
        c.clipPath(blur, doAntiAlias=True)
        p = skia.Paint()
        p.setImageFilter(skia.ImageFilters.Blur(sigma, sigma))
        c.drawImage(back, 0, 0, skia.SamplingOptions(), p)
        c.restore()
    c.drawImage(top, 0, 0)
    return snap(s)


def effect_frame(src: skia.Image, effect, *, appear: float, t: float,
                 seed: float) -> skia.Image:
    """One frame of a window's post-process pass over its surface, as
    Window does it: kDecal source, the chain, drawn kSrc."""
    w, h = src.width(), src.height()
    shader = src.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal,
                            skia.SamplingOptions())
    shader = effect.shader(shader, w, h, t=t, appear=appear, seed=seed)
    s, c = surface(w, h)
    p = skia.Paint(Shader=shader)
    p.setBlendMode(skia.BlendMode.kSrc)
    c.drawPaint(p)
    return snap(s)


def bar_strip(row: Widget, width: int, height: int = theme.BAR_HEIGHT) -> skia.Image:
    """A dock window of the bars: BAR_BG, BAR_MARGIN either side, the row
    filling the rest (config_default's `bar` / `top_bar` specs)."""
    s, c = surface(width, height, theme.BAR_BG)
    m = theme.BAR_MARGIN
    place(row, m, 0, width - 2 * m, height)
    row.paint(c)
    return snap(s)


# ═══════════════════════════════════════════════════════════════════════
# the palette: names, values from theme.py, meanings from their usages
# ═══════════════════════════════════════════════════════════════════════
# (group, theme constant, alias(es), meaning). The meanings were read off
# every use in the code; tokens.json carries the same words.
PALETTE = [
    ("base", "BASE_BLACK", "BG, BAR_BG, NOTIF_BG",
     "every background: both bars, toasts, the cards' fill (60%), Flux's window"),
    ("base", "BASE_SHADOW", "BG_SHADOW", "a lifted black for a recessed card (rare)"),
    ("base", "BASE_GUTTER", "NOTIF_METER_DIM",
     "unlit meter cells, the low-urgency toast frame, action-button fill"),
    ("base", "BASE_SURFACE", "", "hover fill behind the INDIGO tag"),
    ("base", "BASE_MUTED", "NOTIF_APPNAME_FG",
     "the muted tone: secondary text (dates, IPs, units, key hints, tabs behind)"),
    ("magenta", "MAGENTA_DIM", "FG_MUTED",
     "unlit magenta cells: empty workspaces, empty tray/battery cells, cava at rest"),
    ("magenta", "MAGENTA_MID", "FG",
     "plain neon foreground: network name, Flux host, hovered tab title, ♫"),
    ("magenta", "MAGENTA_BRIGHT", "FG_STRONG",
     "the user, focus, emphasis: YOU, current tab, summaries, row labels"),
    ("magenta", "MAGENTA_BLOOM", "",
     "magenta lifted to white: tag pulse peak, a tab loading, the send comet"),
    ("yellow", "YELLOW_FAINT", "", "faint yellow ground: inline code bg, menu separators"),
    ("yellow", "YELLOW_DIM", "", "occupied workspaces, the turn clock, code blocks, 'queued'"),
    ("yellow", "YELLOW_MID", "", "the // key marker, the warn role, an info step's !"),
    ("yellow", "YELLOW_BRIGHT", "FG_ACCENT",
     "values, caps, attention: readouts, the cap line, armed rows, ASK"),
    ("cyan", "CYAN_FAINT", "", "defined, unused (its rgba twin backs the speedtest button)"),
    ("cyan", "CYAN_DIM", "", "quiet cyan structure: meter brackets, unlit cells, dividers"),
    ("cyan", "CYAN_MID", "MUSIC_FG",
     "data at rest: song title, lyric, KEY labels, host octet, row tags"),
    ("cyan", "CYAN_BRIGHT", "HIGHLIGHT, POPUP_BORDER",
     "interactive and info: borders, //, selection, links, clock; sent / up"),
    ("cyan", "CYAN_BLOOM", "",
     "cyan lifted to white: the lyric's beat flash, matched letters when selected"),
    ("lime", "LIME_DIM", "", "battery at full charge"),
    ("lime", "LIME_MID", "", "defined, unused"),
    ("lime", "LIME_BRIGHT", "",
     "OK / received / go: received / down, ok role, GREENLIGHT, ON, online"),
    ("violet", "VIOLET_DIM", "", "unlit violet cells (RAM, volume)"),
    ("violet", "VIOLET", "", "RAM label, a finished job's cells"),
    ("violet", "VIOLET_BRIGHT", "ICON",
     "RAM; volume; toast body; on the call, the selection and progress cells"),
    ("error", "ERROR", "RING_ACCENT, FAIL_ACCENT",
     "errors and alarms only: failed, hot, critical, SCRUB, urgent, muted, ring"),
    ("hud", "HUD_FG", "", "body text on plates: steps, details (blue-grey, not neon)"),
    ("hud", "HUD_FG_BRIGHT", "",
     "bright body text: the reply, typed text, row titles; the raster leans to it"),
    ("hud", "HUD_PLATE", "", "the active tab's plate in a panel's tab strip"),
    ("hud", "HUD_SECTION", "", "defined, unused"),
]


def her() -> str:
    """Her colour (core/persona.py): the agent's, as the hub names it."""
    from indigoshell.core import persona
    return persona.COLOR


def paint_swatch(c, x, y, w, hexv: str, name: str, alias: str, role: str,
                 alpha_note: str = "") -> None:
    sw = 46
    p = skia.Paint(AntiAlias=False)
    p.setColor(C(hexv))
    c.drawRect(skia.Rect.MakeXYWH(x, y, sw, sw), p)
    edge = skia.Paint(AntiAlias=False, Style=skia.Paint.kStroke_Style)
    edge.setColor(C("#ffffff", 0.08))
    c.drawRect(skia.Rect.MakeXYWH(x + 0.5, y + 0.5, sw - 1, sw - 1), edge)
    tx = x + sw + 10
    caption(c, name, tx, y + 12, 11.5, LABEL_FG, bold=True)
    nw = label_font(11.5, True).measureText(name)
    caption(c, hexv + alpha_note, tx + nw + 8, y + 12, 11, LABEL_DIM)
    if alias:
        caption(c, alias, tx, y + 27, 10, LABEL_DIM)
    caption(c, role, tx, y + 42, 10, LABEL_FG)


def render_palette() -> None:
    groups = []
    for g, name, alias, role in PALETTE:
        if not groups or groups[-1][0] != g:
            groups.append((g, []))
        groups[-1][1].append((getattr(theme, name), name, alias, role))
    groups.append(("the agent (core/persona.py)", [
        (her(), "LILAC", "persona.COLOR, AGENT_ACCENT",
         "her: the call's plate, chip and name, a tab she drives, Flux's frame, "
         "mark and tint")]))
    from indigoshell.agent.ui import style
    groups.append(("surfaces (computed)", [
        (theme.POPUP_BG[:7], "POPUP_BG", "#170620 @ 95% (F2)",
         "panel plate: launcher, panels, menu chips, tray rows", " F2"),
        (style.YOU_FILL[0], "YOU_FILL", f"call plate: PLATE_BASE 12% to magenta @ {style.PLATE_ALPHA:.0%}",
         "your plate on the call", ""),
        (style.AGENT_FILL[0], "AGENT_FILL", f"call plate: PLATE_BASE 12% to lilac @ {style.PLATE_ALPHA:.0%}",
         "her plate on the call", ""),
    ]))
    col_w, row_h = 470, 58
    cols = 2
    # lay groups down two columns, balancing rows
    total = sum(len(items) + 1 for _g, items in groups)
    W = 24 + cols * col_w
    H = 60 + math.ceil(total / cols + 2) * row_h
    s, c = surface(W, H, theme.BG)
    caption(c, "indigoshell palette — theme.py names, hex, what the code uses each for",
            20, 30, 14, LABEL_FG, bold=True)
    x, y = 20, 56
    col = bottom = 0
    for g, items in groups:
        need = (len(items)) * row_h + 26
        if y + need > H - 10 and col == 0:
            col, x, y = 1, 20 + col_w, 56
        caption(c, g.upper(), x, y + 12, 10.5, LABEL_DIM, bold=True)
        y += 22
        for item in items:
            hexv, name, alias, role = item[:4]
            note = item[4] if len(item) > 4 else ""
            paint_swatch(c, x, y, col_w, hexv, name, alias, role, note)
            y += row_h
        y += 4
        bottom = max(bottom, y)
    save(crop(snap(s), 0, 0, W, bottom + 10), "palette.png")


# ═══════════════════════════════════════════════════════════════════════
# type
# ═══════════════════════════════════════════════════════════════════════
def render_type() -> None:
    pt = T.pt
    rows = [
        # (px size, bold, tracking, colour, sample, note)
        (theme.FONT_SIZE_XL, False, 0, theme.MAGENTA_MID, "♫",
         "28 px  FONT_SIZE_XL  the idle media glyph"),
        (pt(18), True, 0, theme.CYAN_BRIGHT, "21:47",
         "24 px bold (18 pt)  clock time"),
        (pt(14), True, 0, theme.CYAN_BRIGHT, "42%  63%  71°",
         "18.7 px bold (14 pt)  meter values  - the 'values' size"),
        (theme.FONT_SIZE_LG, True, 0, theme.CYAN_MID, "Midnight Uplink — Neon Static",
         "18 px bold  FONT_SIZE_LG  song title"),
        (pt(13), True, 0, theme.MAGENTA_BRIGHT, "INDIGO",
         "17.3 px bold (13 pt)  the INDIGO tag"),
        (theme.FONT_SIZE, False, 0, theme.FG, "NIGHTCITY-5G",
         "16 px  FONT_SIZE  names: network, tab titles, menu rows, inputs"),
        (theme.FONT_SIZE, False, 1.0, theme.FG_STRONG, "SUSPEND   POWEROFF",
         "16 px, 1 px tracking  chord-menu / tray row labels"),
        (15, True, 0, None, [("//", theme.CYAN_BRIGHT, True), (" LILAC", her(), True),
                             (" working", theme.BASE_MUTED, False)],
         "15 px bold  toast & call heading:  // NAME tag"),
        (15, False, 0, theme.HUD_FG_BRIGHT, "Uplink's clean. Three ghosts in the logs.",
         "15 px  call reply, toast body, lyric line"),
        (pt(11), False, 0, theme.BASE_MUTED, "Wed 25 Nov", "14.7 px (11 pt)  clock date"),
        (13, False, 1.0, theme.CYAN_MID, "CPU  RAM  DISK", "13 px, 1 px tracking  panel KEY labels"),
        (13, False, 0, theme.CYAN_BRIGHT, "› $ journalctl -b -p err",
         "13 px  call steps (ACT_SIZE), panel body"),
        (12, False, 0, theme.BASE_MUTED, "⏎ send   ^C stop   esc hang up",
         "12 px  call tags / key hints (TAG_SIZE), row details"),
        (pt(9), False, 0, theme.CYAN_DIM, "CPU", "12 px (9 pt)  meter labels"),
        (11, True, 0, theme.YELLOW_BRIGHT, "NETLINK", "11 px bold  panel section titles"),
        (pt(8), False, 0, theme.BASE_MUTED, [("192.168.1.", theme.BASE_MUTED, False),
                                             ("42", theme.CYAN_MID, True)],
         "10.7 px (8 pt)  the IP under the network name (host octet bold)"),
    ]
    W = 1000
    pad = 20
    y = 48
    heights = []
    for size, bold, *_ in rows:
        heights.append(max(26, math.ceil(T.line_height(size, bold)) + 10))
    H = y + sum(heights) + 160
    s, c = surface(W, H, theme.BG)
    caption(c, f"type — {theme.FONT}, regular + bold.  Sizes in px (Skia); pt x 96/72 = px.",
            pad, 28, 13, LABEL_FG, bold=True)
    small_top = None
    for (size, bold, track, color, sample, note), h in zip(rows, heights):
        if size <= T.pt(11) and small_top is None:
            small_top = y
        base = y + h / 2 - (T.font(size, bold).getMetrics().fAscent
                            + T.font(size, bold).getMetrics().fDescent) / 2
        x = pad
        if isinstance(sample, list):
            for part, col, b in sample:
                T.draw(c, part, x, base, size, C(col), b, track)
                x += T.measure(part, size, b, track)
        else:
            T.draw(c, sample, x, base, size, C(color), bold, track)
        caption(c, note, 520, y + h / 2 + 4, 11, LABEL_FG)
        y += h
    # the monospace grid
    y += 16
    caption(c, f"the grid: every glyph one advance wide ({T.advance(16) / 16:.3f} em), so values "
            "never jitter and the call selects by cell", pad, y, 11, LABEL_FG)
    y += 12
    size = theme.FONT_SIZE
    adv = T.advance(size)
    line = "0123456789 // ICE ◢◤ READY"
    gp = skia.Paint(AntiAlias=False)
    gp.setColor(C(theme.CYAN_DIM, 0.6))
    lh = T.line_height(size)
    for i in range(len(line) + 1):
        c.drawRect(skia.Rect.MakeXYWH(round(pad + i * adv), y, 1, lh), gp)
    T.draw(c, line, pad, y - T.font(size).getMetrics().fAscent, size, C(theme.HUD_FG_BRIGHT))
    caption(c, f"16 px: advance {adv:.2f} px, line height {lh:.2f} px", 520, y + lh / 2 + 4, 11,
            LABEL_FG)
    y += lh + 18
    caption(c, "caps for labels, keys, tags and headings (CPU, SUSPEND, // LILAC, GREENLIGHT); "
            "mixed or lower case for prose and states (working, on the line).",
            pad, y, 11, LABEL_FG)
    y += 18
    caption(c, "One family, two weights (regular, bold). No italic face: the call skews the "
            "regular by -0.2 for *emphasis*.", pad, y, 11, LABEL_FG)
    y += 14
    img = crop(snap(s), 0, 0, W, y + 10)
    save(img, "type.png")
    small_bottom = 48 + sum(heights)
    save(zoom(crop(img, 0, small_top, 480, small_bottom - small_top), 3), "type_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# shapes: the real geometry functions and widgets, one element per tile
# ═══════════════════════════════════════════════════════════════════════
def tile(w: int, h: int, paint_fn, bg: str = theme.BG) -> skia.Image:
    s, c = surface(w, h, bg)
    paint_fn(c)
    return snap(s)


def widget_tile(widget: Widget, pad: int = 12, bg: str = theme.BG,
                w: float | None = None, h: float | None = None) -> skia.Image:
    size = widget.measure(Size(10_000.0, 10_000.0)) if w is None else Size(w, h)
    W, H = int(math.ceil(size.width)) + 2 * pad, int(math.ceil(size.height)) + 2 * pad

    def paint(c):
        place(widget, pad, pad, size.width, size.height)
        widget.paint(c)
    return tile(W, H, paint, bg)


def stroke(color: str, width: float, alpha: float | None = None) -> skia.Paint:
    p = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style)
    p.setStrokeWidth(width)
    p.setColor(C(color, alpha))
    return p


def fill(color: str, alpha: float | None = None, aa: bool = True) -> skia.Paint:
    p = skia.Paint(AntiAlias=aa)
    p.setColor(C(color, alpha))
    return p


def bevel_tile() -> skia.Image:
    """Every bevel size in use, each as its owner strokes it."""
    from indigoshell.agent.ui import style
    from indigoshell.applink.kinds import look
    from indigoshell.widgets import menu as menu_mod, hud
    from indigoshell.agent.ui import choice as choice_mod
    W, H = 800, 116

    def paint(c):
        x, y = 14, 22
        items = [
            # (w, h, bevel, corners, stroke width, colour, alpha, fill, label)
            (120, 64, theme.POPUP_BEVEL, theme.POPUP_BEVEL_CORNERS, 2.0, theme.POPUP_BORDER,
             None, theme.POPUP_BG, "16  panels"),
            (120, 64, theme.NOTIF_BEVEL, theme.NOTIF_BEVEL_CORNERS, theme.NOTIF_BORDER_THICK,
             theme.NOTIF_FRAME_NORMAL, None, theme.NOTIF_BG, "12  toasts, call"),
            (110, 38, menu_mod.BEVEL, menu_mod.CORNERS, menu_mod.LINE_W, theme.CYAN_BRIGHT,
             0.85, theme.POPUP_BG, "8  rows, chips"),
            (100, 30, choice_mod.BEVEL, style.CORNERS, choice_mod.LINE_W, theme.LIME_BRIGHT,
             0.8, None, "7  answers"),
        ]
        for w, h, b, corners, sw, col, a, bgc, cap in items:
            path = shapes.beveled(x, y, w, h, bevel=b, corners=corners)
            if bgc:
                c.drawPath(path, fill(bgc))
            c.drawPath(shapes.beveled(x, y, w, h, bevel=b, corners=corners,
                                      inset=sw / 2), stroke(col, sw, a))
            caption(c, cap, x, y + h + 16, 10, LABEL_DIM)
            x += w + 18
        # a HudCard: its stripe follows the bottom-left cut
        from indigoshell.widgets.layout import Spacer
        card = hud.HudCard(Spacer(size=1), accent=theme.MAGENTA_BRIGHT)
        c.drawRect(skia.Rect.MakeXYWH(x - 8, y - 8, 116, 80), fill(theme.POPUP_BG))
        place(card, x, y, 100, 64)
        card.paint(c)
        caption(c, f"{hud.BEVEL}  card (on a panel)", x, y + 64 + 16, 10, LABEL_DIM)
        x += 100 + 26
        # a top-bar tab box: one cut, top right
        band = look.Band(y, y + look.BOX_H, y + look.BOX_H / 2)
        look.paint_current(c, x, 110, band)
        caption(c, f"{look.BOX_BEVEL}  tab (one cut)", x, y + 64 + 16, 10, LABEL_DIM)
    return tile(W, H, paint)


def bracket_tile() -> skia.Image:
    """Corner brackets: the bar's cluster frame, the tag's, the tab
    number's spot, and the call plate's marks (L on a square corner, a
    cut-arm-cut along a 45-degree one)."""
    from indigoshell.widgets.layout import Brackets, Spacer
    from indigoshell.widgets.systag import Systag
    from indigoshell.applink.kinds import look
    from indigoshell.agent.ui import style
    W, H = 760, 96

    def paint(c):
        x, y = 14, 18
        b = Brackets(Spacer(size=1))
        place(b, x, y, 130, 40)
        b.paint(c)
        caption(c, f"cluster: arm {b.arm}, {b.thick} px, CYAN_DIM", x, y + 58, 10, LABEL_DIM)
        x += 190
        tag = Systag("INDIGO")
        size = tag.measure(Size(500, 100))
        place(tag, x, y + 4, size.width, size.height)
        tag.paint(c)
        caption(c, "tag: arm 4, 1 px", x, y + 58, 10, LABEL_DIM)
        x += 150
        spot = skia.Rect.MakeXYWH(x, y + 10, look.NUMBER_SPOT, look.NUMBER_SPOT)
        look.brackets(c, spot, look.BRACKET_FG)
        look.paint_number(c, "1", spot, look.NUMBER_CURRENT_FG)
        caption(c, f"tab number: {look.NUMBER_SPOT} px spot, arm {look.BRACKET_ARM}", x,
                y + 58, 10, LABEL_DIM)
        x += 170
        r = skia.Rect.MakeXYWH(x, y - 4, 200, 54)
        style.paint_plate(c, r, theme.VIOLET_BRIGHT, style.AGENT_FILL)
        caption(c, f"plate marks: inset {style.BRACKET_INSET}, arm {style.BRACKET_ARM}, "
                f"{style.BRACKET_W} px", x, y + 66, 10, LABEL_DIM)
    return tile(W, H, paint)


def dots_tile() -> skia.Image:
    """The network's two square lights in each state, as the widget paints them."""
    from indigoshell.widgets.network import Network
    W, H = 760, 70
    states = [((0.0, 0.0, 0.0), "steady (quiet link): 45%"),
              ((2.0, 2.0, 0.1), "blinking, lit: 100% + 2 px glow"),
              ((2.0, 2.0, 0.3), "blinking, between: 25%"),
              (None, "no address: red, steady")]

    def paint(c):
        x = 14
        for st, cap in states:
            n = Network()
            if st is None:
                n._apply("--", "")
            else:
                n._apply("NIGHTCITY-5G", "192.168.1.42")
                n._f_up, n._f_down, n._t = st
            size = n.measure(Size(500, 100))
            place(n, x, 10, size.width, size.height)
            n.paint(c)
            caption(c, cap, x, 62, 10, LABEL_DIM)
            x += 190
    return tile(W, H, paint)


def segments_tile() -> skia.Image:
    """Level bars of square cells: every rhythm in use."""
    from indigoshell.widgets.meters import BarMeter, BatteryMeter, StatMeter
    from indigoshell.widgets.volume import Volume
    from indigoshell.agent.ui import bars as call_bars
    W, H = 760, 112

    def paint(c):
        x, y = 14, 14
        m = StatMeter("CPU", lambda: 42.0, bright_color=theme.CYAN_BRIGHT,
                      dim_color=theme.CYAN_DIM, label_color=theme.CYAN_DIM,
                      value_color=theme.CYAN_BRIGHT)
        size = m.measure(Size(500, 100))
        place(m, x, y, size.width, size.height)
        m.paint(c)
        caption(c, f"gauge: {m.line_tick}/{m.line_gap} px, {m.line_thickness} tall", x, y + 70,
                10, LABEL_DIM)
        x += 130
        b = BarMeter(color=theme.YELLOW_BRIGHT, min_width=150)
        b.set_value(63)
        place(b, x, y + 10, 150, 12)
        b.paint(c)
        caption(c, "panel bar: 4/2 px, 8 tall", x, y + 70, 10, LABEL_DIM)
        x += 180
        bat = BatteryMeter(cell_thick=2, gap=2, height=4, pad_y=0, brackets=False,
                           fill=True, fake_state=(64.0, False))
        bat._poll()
        bat._event_until = 0.0
        place(bat, x, y + 14, 110, 4)
        bat.paint(c)
        caption(c, "battery: 2/2 px, 4 tall", x, y + 70, 10, LABEL_DIM)
        x += 140
        n = int((120 + call_bars.CELL_GAP) // (call_bars.CELL_W + call_bars.CELL_GAP))
        for i in range(n):
            col = call_bars.LIT if i < n * 0.55 else call_bars.UNLIT
            c.drawRect(skia.Rect.MakeXYWH(x + i * (call_bars.CELL_W + call_bars.CELL_GAP),
                                          y + 12, call_bars.CELL_W, call_bars.CELL_H),
                       fill(col, aa=False))
        caption(c, "call job: 3/2 px, 6 tall", x, y + 70, 10, LABEL_DIM)
        x += 150
        v = Volume(cap=True, cap_color=theme.YELLOW_BRIGHT)
        v._percent = 62.0
        size = v.measure(Size(500, 100))
        place(v, x, y + 4, size.width, size.height)
        v.paint(c)
        vm = Volume(cap=True, cap_color=theme.YELLOW_BRIGHT)
        vm._muted = True
        place(vm, x + 30, y + 4, size.width, size.height)
        vm.paint(c)
        caption(c, "volume, muted", x, y + 70, 10, LABEL_DIM)
        caption(c, "3 px cells, 1 gap; cap 1 px", x, y + 84, 10, LABEL_DIM)
    return tile(W, H, paint)


def chip_tile() -> skia.Image:
    from indigoshell.agent.ui.chip import Chip
    from indigoshell.agent.ui import style
    W, H = 360, 100

    def paint(c):
        x = 14
        for size, look, cap in ((style.CHIP, (theme.VIOLET_BRIGHT, "CALL", "still"), "64 px"),
                                (style.CHIP_SM, (theme.YELLOW_BRIGHT, "ASK", "still"), "44 px"),
                                (style.CHIP, (theme.MAGENTA_BRIGHT, "YOU", "still"), "64 px")):
            ch = Chip(size, lambda look=look: look, tint=look[0] if look[1] != "ASK"
                      else theme.VIOLET_BRIGHT)
            ch.angle = -90.0 if look[1] != "ASK" else -40.0
            place(ch, x, 10 + (style.CHIP - size) / 2, size, size)
            ch.paint(c)
            caption(c, cap, x, 92, 10, LABEL_DIM)
            x += size + 30
    return tile(W, H, paint)


def zoom_sheet(tiles, k: int = 3, **kw) -> skia.Image:
    return sheet([(cap, zoom(img, k)) for cap, img in tiles], **kw)


def render_shapes() -> None:
    bev, brk, dots, seg, chip = (bevel_tile(), bracket_tile(), dots_tile(),
                                 segments_tile(), chip_tile())
    save(sheet([
        ("bevels: 45-degree cuts at top-right + bottom-left (the tab box: top-right only); "
         "strokes inset by half their width", bev),
        ("corner brackets: the most framing the bottom bar allows", brk),
        ("lights are squares, never circles (network: 5 px, sent cyan over received lime)", dots),
        ("level bars: whole-pixel square cells, lit left to right / bottom up", seg),
        ("the call's chips: beveled square, hairline inset 5.5 px, two 64-degree arcs", chip),
    ], title="shapes — all drawn by the shipped functions and widgets"), "shapes.png")
    save(zoom_sheet([
        ("corner brackets: cluster, tag, tab number", crop(brk, 0, 8, 520, 54)),
        ("square lights: steady, lit, between", crop(dots, 0, 4, 560, 40)),
        ("level bars: gauge, panel bar, battery, call job, volume + cap, muted", crop(seg, 0, 8, 690, 56)),
        ("bevel corners: row (8), answer (7), card stripe (14), tab (8)", crop(bev, 286, 16, 514, 74)),
    ], title="shapes at 3x (nearest neighbour)"), "shapes_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# the bottom bar: config_default's own `bar` row, given neutral state
# ═══════════════════════════════════════════════════════════════════════
TITLE = "Midnight Uplink — Static"               # made up
LYRIC = "chrome rain on the overpass, signal holding"  # made up
NET_NAME, NET_IP = "NIGHTCITY-5G", "192.168.1.42"      # example values
BANDS = tuple(int(1000 + 9000 * (0.35 + 0.65 * abs(math.sin(i * 0.55 + 0.7))
                                 * (0.55 + 0.45 * math.cos(i * 0.21))))
              for i in range(48))


class _Items:
    """The tray broker as the indicator reads it: just its items."""
    def __init__(self, n):
        self.n = n

    def items(self):
        return list(range(self.n))


def _config():
    from indigoshell import config_default
    return config_default


def bottom_bar(*, playing: bool = True, urgent: bool = False):
    """config_default's bar row with every widget's state set by hand
    (no subprocess, no D-Bus, no sensor): (row, widgets by name)."""
    import importlib
    from indigoshell.widgets.layout import Brackets, Row
    from indigoshell.widgets.media import Media
    from indigoshell.widgets.network import Network
    from indigoshell.widgets.stdout_text import StdoutText
    from indigoshell.widgets.systag import Systag
    from indigoshell.widgets.volume import Volume
    from indigoshell.widgets.workspaces import Workspaces
    cfg = importlib.reload(_config())       # fresh widgets every call
    row = cfg.WINDOWS[0].content
    w = {}
    for child in row.children():
        for kind, name in ((Systag, "tag"), (Workspaces, "ws"), (StdoutText, "lyrics"),
                           (Brackets, "meters"), (Network, "net"), (Media, "media"),
                           (Volume, "vol"), (Row, "clock_cluster")):
            if isinstance(child, kind):
                w[name] = child
    ws = w["ws"]
    ws._count, ws._current = 6, 2
    ws._per_desktop = [2, 0, 3, 1, 0, 4]
    ws._urgent = [False, False, False, False, urgent, False]
    if urgent:
        ws._per_desktop[4] = 1
        ws._any_urgent, ws._ring_visible = True, True
    lyr = w["lyrics"]
    if playing:
        lyr._set_text(LYRIC)
        lyr._reveal = 1.0
        lyr._render()
    meters = w["meters"].child.children()
    for m, v in zip(meters, (42.0, 63.0, 71.0)):
        m.source = (lambda v=v: v)
        m._peak_pct = 0.0
        m._sample()
    net = w["net"]
    net._apply(NET_NAME, NET_IP)
    net._f_up, net._f_down, net._t = 0.0, 2.0, 0.1
    media = w["media"]
    if playing:
        media._title, media._title_w = TITLE, T.measure(TITLE, media.size, media.bold)
        media._bands, media._peak, media._active = BANDS, float(max(BANDS)), True
        media._glitching = False
    vol = w["vol"]
    vol._percent = 62.0
    clock, tray = w["clock_cluster"].children()
    w["clock"], w["tray"] = clock, tray
    clock._date.set_value("Wed 25 Nov")
    clock._time.set_value("21:47")
    bat = clock.extra
    bat.fake_state = (64.0, False)
    bat._poll()                         # a reading, no event: at rest
    bat._state, bat._event_until = bat._state_key(), 0.0
    tray._broker = _Items(3)
    return row, w


BAR_W = 1920


def render_bottom_bar() -> None:
    row, w = bottom_bar()
    img = bar_strip(row, BAR_W)
    save(img, "bar_bottom.png")
    # where things are, for the crops
    left_end = w["lyrics"].rect.right() + 16
    right_start = w["meters"].rect.left() - 12
    save(zoom(crop(img, 0, 0, left_end, img.height())), "bar_bottom_left_3x.png")
    mid = w["media"].rect.left() - 12
    save(zoom(crop(img, right_start, 0, mid - right_start, img.height())),
         "bar_bottom_right_3x.png")
    save(zoom(crop(img, mid, 0, BAR_W - mid, img.height())), "bar_bottom_end_3x.png")
    idle_row, _ = bottom_bar(playing=False)
    save(bar_strip(idle_row, BAR_W), "bar_bottom_idle.png")


def strip_of(widget: Widget, pad: int = 16, extra_w: int = 0,
             h: int = theme.BAR_HEIGHT, fill_h: bool = False) -> skia.Image:
    """One widget on a bar-height slice, centred as the bar's Row does."""
    size = widget.measure(Size(1000, h))
    W = int(math.ceil(size.width)) + 2 * pad + extra_w
    s, c = surface(W, h, theme.BAR_BG)
    wh = h if fill_h else size.height
    place(widget, pad, (h - wh) / 2, size.width, wh)
    widget.paint(c)
    return snap(s)


def render_bar_widgets() -> None:
    """Each bottom-bar widget in its states, at 1x, then the sheet at 3x."""
    tiles = []
    _, w = bottom_bar()
    tiles.append(("INDIGO tag: still, MAGENTA_BRIGHT", strip_of(w["tag"])))
    tiles.append(("workspaces: a 4-bar stack per desktop; current cyan/magenta",
                  strip_of(w["ws"])))
    _, wu = bottom_bar(urgent=True)
    tiles.append(("workspaces: desktop 5 urgent (red, ring blink)",
                  strip_of(wu["ws"])))
    tiles.append(("meters: CPU / RAM / TEMP, one bracket frame",
                  strip_of(w["meters"])))
    _, hot = bottom_bar()
    for m, v in zip(hot["meters"].child.children(), (97.0, 91.0, 86.0)):
        m.source = (lambda v=v: v)
        m._sample()
    tiles.append(("under load: TEMP steps cyan > yellow > red",
                  strip_of(hot["meters"])))
    tiles.append(("network: squares, name, IP",
                  strip_of(w["net"])))
    _, wd = bottom_bar()
    wd["net"]._apply("--", "")
    tiles.append(("network: no address", strip_of(wd["net"])))
    tiles.append(("media playing: title over cava bars",
                  strip_of(w["media"], fill_h=True)))
    beat = bottom_bar()[1]["media"]
    beat._beat = 0.9
    beat._glitching = True
    random.seed(3)
    tiles.append(("media on a beat, mid glitch burst",
                  strip_of(beat, fill_h=True)))
    idle = bottom_bar(playing=False)[1]["media"]
    tiles.append(("media idle: just the glyph", strip_of(idle, fill_h=True)))
    tiles.append(("lyric line (CYAN_MID, 15 px)", strip_of(w["lyrics"])))
    lb = bottom_bar()[1]["lyrics"]
    lb._beat = 0.8
    lb._apply_pulse()
    tiles.append(("lyric on a beat: CYAN_BLOOM, bold", strip_of(lb)))
    tiles.append(("volume: 8 cells + yellow cap line", strip_of(w["vol"])))
    vm = bottom_bar()[1]["vol"]
    vm._muted = True
    tiles.append(("volume muted: every cell red, no cap", strip_of(vm)))
    tiles.append(("clock, battery at rest (64%, cyan), tray stack",
                  strip_of(w["clock_cluster"])))
    for state, cap, event in (((40.0, False), "battery under half, at rest: yellow", None),
                              ((100.0, True), "battery full, at rest: lime", None),
                              ((15.0, False), "battery < 20%: magenta blink (an alarm)", None),
                              ((70.0, True), "plugged in: 3 s of charging sweep, then still",
                               ("sweep", 1, 6))):
        _, wb = bottom_bar()
        b = wb["clock"].extra
        b.fake_state = state
        b._poll()
        b._state = b._state_key()
        b._event_until = NOW[0] + 1.0 if event else 0.0
        if event:
            b._mode, b._dir, b._sweep_pos = event
        tiles.append((cap, strip_of(wb["clock_cluster"])))
    img = sheet(tiles, cols=2, title="bottom-bar widgets, real classes, 1x")
    save(img, "bar_widgets.png")
    save(zoom_sheet(tiles, cols=2, title="bottom-bar widgets at 3x"), "bar_widgets_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# the top bar: an app's section (BarView + kinds) and the agent chip
# ═══════════════════════════════════════════════════════════════════════
TAB_TITLES = ["Uplink status", "Netrunner field guide", "Patch notes 7.3", "Weather radar"]


def hub_agent(**state):
    """render_call's never-connected hub agent: Lilac, as the hub's
    welcome names her (core/persona.py)."""
    from render_call import agent_in
    return agent_in(**state)


class Router:
    """A CallRouter as the chip reads it: its agent and its state."""
    call = "call"

    def __init__(self, agent, state):
        self.agent, self.state = agent, state

    def follow(self, fn):
        return lambda: None


def agent_chip(agent, state: str, angle: float = -90.0):
    from indigoshell.agent.ui.status import AgentChip
    chip = AgentChip(routers=[Router(agent, state)])
    chip.attach(Host())
    chip.chip.angle = angle
    return chip


def app_view(tabs, current=0, *, host="wiki.example.org", secure=True, bookmarked=True,
             progress=None, busy=(), agents=None, hover=None):
    """An app window's section as the shell keeps it: a BarView fed the bar
    an app with tabs and a host sends (applink/bar.py's builders)."""
    from indigoshell.applink import bar as B
    from indigoshell.applink.view import BarView
    ids = [101 + i for i in range(len(tabs))]
    agents = agents or {}
    items = [B.tab(n, t, busy=i in busy, progress=progress if i == current else None,
                   agent=B.agent(*agents[i]) if i in agents else None)
             for i, (t, n) in enumerate(zip(tabs, ids))]
    spans = ([B.span(icon="bookmark")] if bookmarked else []) + (
        [] if secure else [B.span(icon="unlocked", color="error")]) + [B.span(host)]
    view = BarView(emit=lambda e: None)
    view.attach(Host())
    view.set_bar(B.bar([B.tabs("tabs", items, current=ids[current],
                               can=["close", "move", "detach"])],
                       [B.text("host", spans=spans)]))
    view._hover_index = hover
    return view


def settle(widget: Widget, hover=None) -> None:
    for each in widget.walk():
        if hasattr(each, "settle"):
            each.settle()
        if hover is not None and getattr(each, "kind", "") == "tabs":
            each.hover = hover


def top_bar_row(view, chip):
    """config_default's `top_bar` row: AppContext (the focused app's
    section, here `view`, through a stand-in app service) and the chip."""
    from indigoshell.applink.context import AppContext
    from indigoshell.widgets.layout import Align, Row
    service = types.SimpleNamespace(context=None, focused=types.SimpleNamespace(content=view),
                                    follow=lambda fn: None, unfollow=lambda fn: None)
    ctx = AppContext(service)
    ctx.attach(Host())
    return Row([ctx, chip], spacing=theme.BAR_SPACING, align=Align.CENTER)


def top_strip(view, chip, width=BAR_W, hover=None) -> skia.Image:
    row = top_bar_row(view, chip)
    s, c = surface(width, theme.TOP_BAR_HEIGHT, theme.BAR_BG)
    m = theme.BAR_MARGIN
    place(row, m, 0, width - 2 * m, theme.TOP_BAR_HEIGHT)
    settle(row, hover)
    row.paint(c)
    return snap(s)


def working_agent():
    from indigoshell.agent.activity import Activity
    return hub_agent(phase="working", busy=True, turn_started=NOW[0] - 41,
                     activity=[Activity("tool", "$ journalctl -b -p err --no-pager"),
                               Activity("tool", "$ systemctl --failed")])


def render_top_bar() -> None:
    from indigoshell.applink.kinds import look
    NOW[0] = 3000.0 + 0.35 * look.COMET_S        # the busy tab's comet mid-run
    view = app_view(TAB_TITLES, 0, busy=(2,), agents={1: ("driving",)})
    chip = agent_chip(working_agent(), "working", angle=-60)
    img = top_strip(view, chip)
    save(img, "bar_top.png")
    save(zoom(crop(img, 0, 0, 2 * (look.SLOT_W + look.SLOT_GAP) + 8, img.height())),
         "bar_top_tabs_3x.png")
    cx = chip.rect.left() - 10
    save(zoom(crop(img, cx, 0, BAR_W - cx, img.height())), "bar_top_chip_3x.png")


def render_top_states() -> None:
    """Every state a tab and the agent chip show, each on a bar slice."""
    from indigoshell.agent.activity import Approval
    from indigoshell.applink.kinds import look
    import asyncio
    W = 2 * (look.SLOT_W + look.SLOT_GAP) + 2 * theme.BAR_MARGIN
    tiles = []

    def tabs_tile(cap, *, hover=None, **kw):
        view = app_view(TAB_TITLES[:2], **kw)
        s, c = surface(W, theme.TOP_BAR_HEIGHT, theme.BAR_BG)
        place(view, theme.BAR_MARGIN, 0, W - 2 * theme.BAR_MARGIN, theme.TOP_BAR_HEIGHT)
        settle(view, hover)
        view.paint(c)
        tiles.append((cap, snap(s)))

    NOW[0] = 3000.0 + 0.35 * look.COMET_S
    tabs_tile("current (left): bright outline + faint light; behind: dim outline, muted")
    tabs_tile("pointer over the tab behind: outline 60%, title FG", hover=1)
    tabs_tile("current loading 40%: its outline lit round from bottom-left (MAGENTA_BLOOM)",
              progress=40)
    tabs_tile("tab behind loading: a comet runs round its box (1.1 s a lap)", busy=(1,))
    tabs_tile("the agent drives the current tab: number, brackets, outline in Lilac",
              agents={0: ("driving",)})
    tabs_tile("she drives the tab behind; its outline in her colour",
              agents={1: ("driving",)})
    tabs_tile("taken over by the user: her colour 35% toward the bar",
              agents={0: ("paused",), 1: ("paused",)})

    loop = asyncio.new_event_loop()
    CW = 420

    def chip_tile(cap, agent, state, angle=-90.0, t_in_ring=None):
        chip = agent_chip(agent, state, angle)
        s, c = surface(CW, theme.TOP_BAR_HEIGHT, theme.BAR_BG)
        size = chip.measure(Size(CW, theme.TOP_BAR_HEIGHT))
        place(chip, CW - theme.BAR_MARGIN - size.width,
              (theme.TOP_BAR_HEIGHT - size.height) / 2, size.width, size.height)
        chip.paint(c)
        tiles.append((cap, snap(s)))

    NOW[0] = 5000.0
    idle = hub_agent(phase="idle")
    chip_tile("agent idle: both pieces at 40%, her name", idle, "idle")
    chip_tile("working: the step in cyan, arcs turning", working_agent(), "working", -50)
    ask = hub_agent(phase="asking", busy=True, turn_started=NOW[0] - 20)
    ask._hub_ask = Approval(tool="Bash", title="Run a command",
                            detail="sudo systemctl restart bluetooth",
                            future=loop.create_future())
    ask.ring_started = NOW[0] - 0.05
    chip_tile("a question, call closed: yellow, the square blinking", ask, "calling")
    ring = hub_agent(phase="idle", ringing="done")
    ring.ring_line = "Patched. 143 packages, no reboot needed."
    ring.ring_started = NOW[0] - 0.05
    chip_tile("calling back, lit: red, both pieces blink (ring pattern)", ring, "calling")
    ring2 = hub_agent(phase="idle", ringing="done")
    ring2.ring_line = ring.ring_line
    ring2.ring_started = NOW[0] - 0.2                   # in the 100 ms gap
    chip_tile("calling back, between flashes: 30%", ring2, "calling")
    missed = hub_agent(phase="idle", ringing="done")
    missed.ring_line = ring.ring_line
    missed.ring_started = NOW[0] - 300
    chip_tile("missed call (after 40 s): still, 45%", missed, "calling")
    tabs_part, chips_part = tiles[:7], tiles[7:]
    img = sheet(tabs_part + chips_part, cols=1,
                title="top bar states: tabs (left) and the agent chip (right end)")
    save(img, "bar_top_states.png")
    save(zoom_sheet(tabs_part[:3] + chips_part[1:5], cols=1, title="top bar states at 3x"),
         "bar_top_states_3x.png")


def render_top_input() -> None:
    """An open input in the section (kinds/input.py) and its list dropping
    from the bar (drop.py's DropList), as for Flux's Ctrl+L."""
    from indigoshell.applink.drop import DropList
    from indigoshell.applink.kinds.input import InputBox
    from indigoshell.applink.kinds import look
    NOW[0] = 7000.0
    view = app_view(TAB_TITLES, 0)
    box = InputBox()
    box.show({"icon": "search", "placeholder": "go to or search"})
    box.field.set_text("netrun", notify=False)
    box.set_count(("4/38", look.color_of("info")))
    view.show_box(box)
    chip = agent_chip(hub_agent(phase="idle"), "idle")
    row = top_bar_row(view, chip)
    width = 1400
    drop = DropList()
    rows = [
        {"id": 1, "tag": "OPEN", "title": "netrun", "detail": "a host or an address"},
        {"id": 2, "tag": "SEARCH", "title": "netrun", "chord": "shift+⏎"},
        {"id": 3, "icon": "history", "title": "Netrunner field guide",
         "detail": "wiki.example.org", "match": [0, 1, 2, 3, 4, 5]},
        {"id": 4, "icon": "bookmark", "title": "Netrunning basics: ICE, daemons, exits",
         "detail": "docs.example.net", "match": [0, 1, 2, 3, 4, 5]},
        {"id": 5, "icon": "history", "title": "Run a net scan from the deck",
         "detail": "forum.example.com", "match": [0, 4, 8, 9, 10, 11], "enabled": False},
    ]
    prompt = types.SimpleNamespace(what="input", rows=rows, selected=2, top=0,
                                   reject_until=0.0, open=True, levels=[])
    s0, c0 = surface(width, 400, theme.BAR_BG)
    m = theme.BAR_MARGIN
    place(row, m, 0, width - 2 * m, theme.TOP_BAR_HEIGHT)
    settle(row)
    row.paint(c0)
    section = view.rect
    drop.show(prompt, width=int(section.width()))
    drop._opened_at = NOW[0] - 1.0
    dw, dh = drop.relayout()
    place(drop, section.left(), theme.TOP_BAR_HEIGHT, dw, dh)
    drop.paint(c0)
    H = theme.TOP_BAR_HEIGHT + dh
    full = crop(snap(s0), 0, 0, width, H)
    # the desktop below the drop window, so its edge reads
    back = desktop(width, H + 40)
    img = over(back, full)
    save(img, "topbar_input.png")
    save(zoom(crop(img, 0, 0, 640, theme.TOP_BAR_HEIGHT + 3 * 44 + 8)), "topbar_input_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# overlays above the bottom bar: toasts, the chord menu
# ═══════════════════════════════════════════════════════════════════════
def bar_tail(width: int) -> skia.Image:
    """The right end of the bottom bar, `width` px of it, for scenes that
    show where an overlay sits."""
    row, _ = bottom_bar()
    full = bar_strip(row, BAR_W)
    return crop(full, BAR_W - width, 0, width, theme.BAR_HEIGHT)


def scene_above_bar(windows, W: int, H: int) -> skia.Image:
    """A desktop W x H whose bottom is the bar, with `windows` [(layer
    image, x, y)] composed over it."""
    s, c = surface(W, H)
    c.drawImage(desktop(W, H), 0, 0)
    for img, x, y in windows:
        c.drawImage(img, x, y)
    c.drawImage(bar_tail(W), 0, H - theme.BAR_HEIGHT)
    return snap(s)


class _Manager:
    def any_hovered(self):
        return False

    def invoke_action(self, *a):
        pass

    def dismiss(self, *a):
        pass


def toast(summary, body, *, app="", urgency=1, actions=(), value=None, elapsed=0.0,
          timeout=5000, hover_action=None):
    from indigoshell.services.notifications import Notification
    from indigoshell.widgets.notification import NotificationToast
    n = Notification(id=1, app_name=app, app_icon="", summary=summary, body=body,
                     actions=list(actions), expire_timeout=timeout, urgency=urgency,
                     value=value)
    t = NotificationToast(_Manager(), n, timeout if urgency != 2 else 0)
    t._elapsed_ms = elapsed * timeout
    if hover_action is not None:
        t._buttons[hover_action].hovered = True
    return t


TOASTS = [
    # oldest first, as the manager stacks them (newest nearest the bar)
    dict(summary="1 new message", body="from: fixer // re: the job", app="mail",
         urgency=0, elapsed=0.75),
    dict(summary="PATCHING", body="143 packages · 61% through the stack", app="deck",
         value=61, elapsed=0.35),
    dict(summary="◢◤ UPLINK HOT", body="ICE down · port 8022 open · a "
         "netrunner rides your wire", app="phone",
         actions=[("default", ""), ("sever", "SEVER"), ("trace", "TRACE")], elapsed=0.15,
         hover_action=0),
    dict(summary="◢◤ DECK FLATLINED", body="the uplink dropped mid-run. "
         "jack back in to pick up where it died.", app="uplink", urgency=2,
         actions=[("default", ""), ("retry", "RECONNECT")]),
]


def render_toasts() -> None:
    layers = []
    for spec in TOASTS:
        t = toast(**spec)
        size = t.measure(Size(theme.NOTIF_WIDTH, 10_000.0))
        w, h = int(size.width), int(math.ceil(size.height))
        place(t, 0, 0, w, h)
        layers.append((t, w, h))
    W = theme.NOTIF_WIDTH + 2 * theme.NOTIF_OFFSET_X + 120
    total = sum(h for _t, _w, h in layers) + theme.NOTIF_GAP * (len(layers) - 1)
    H = total + theme.NOTIF_OFFSET_Y + 40
    windows = []
    bottom = theme.NOTIF_OFFSET_Y
    for t, w, h in reversed(layers):
        y = H - bottom - h
        x = W - theme.NOTIF_OFFSET_X - w
        img = holo_window(t, w, h)
        windows.append((img, x, y))
        bottom += h + theme.NOTIF_GAP
    img = scene_above_bar(windows, W, H)
    save(img, "toasts.png")
    # 3x: the heading, a bevel corner and the timer trace of the meter toast
    t, w, h = layers[1]
    y1 = H - theme.NOTIF_OFFSET_Y - layers[3][2] - layers[2][2] - h - 2 * theme.NOTIF_GAP
    save(zoom(crop(img, W - theme.NOTIF_OFFSET_X - w - 4, y1 - 4, 300, h + 8)), "toast_3x.png")


def menu_host(items, *, armed=None, pulse=False, reject=False):
    from indigoshell.plugin import Menu
    from indigoshell.widgets.menu import MenuHost
    host = MenuHost()
    host._stack = [(Menu("power", "POWER", items), list(items))]
    host._rebuild()
    host._armed, host._pulse_on = armed, pulse
    host._reject_on = reject
    return host


def render_menu() -> None:
    from indigoshell.plugin import Item
    items = [Item("SUSPEND"), Item("POWEROFF"), Item("REBOOT"), Item("LOGOUT", hint="qtile")]
    prof = [Item("POWER-SAVER"), Item("BALANCED", active=True), Item("PERFORMANCE")]
    states = [
        ("idle", menu_host(prof)),
        ("armed (digit down / hover): cyan 22%", menu_host(items, armed=1)),
        ("armed, other beat: yellow 40% (80 ms)", menu_host(items, armed=1, pulse=True)),
        ("unmapped key: rows flash magenta 75%", menu_host(items, reject=True)),
    ]
    tiles = []
    for cap, host in states:
        size = host.measure(Size(10_000.0, 10_000.0))
        w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
        m = 16
        place(host, m, m, w, h)
        win = holo_window(host, w + 2 * m, h + 2 * m)
        tiles.append((cap, over(desktop(w + 2 * m, h + 2 * m), win)))
    img = sheet(tiles, cols=2, title="chord menu: MenuHost rows, holo raster")
    save(img, "menu.png")
    save(zoom(crop(tiles[2][1], 8, 8, tiles[2][1].width() - 16, 3 * 42 + 8)), "menu_3x.png")
    # in place: bottom right above the bar
    host = menu_host(prof)
    size = host.measure(Size(10_000.0, 10_000.0))
    w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
    W, H = 520, h + theme.BAR_HEIGHT + theme.BAR_MARGIN + 6 + 60
    place(host, 0, 0, w, h)
    win = holo_window(host, w, h)
    x = W - (theme.BAR_MARGIN + 8) - w
    y = H - (theme.BAR_HEIGHT + theme.BAR_MARGIN + 6) - h
    save(scene_above_bar([(win, x, y)], W, H), "menu_in_place.png")


# ═══════════════════════════════════════════════════════════════════════
# panels
# ═══════════════════════════════════════════════════════════════════════
def panel_box(content):
    """config_default's panel plate round `content`."""
    from indigoshell.widgets.layout import Box
    return Box(content, padding=Insets.all(20), background=theme.POPUP_BG,
               border=theme.POPUP_BORDER, border_width=2.0, bevel=theme.POPUP_BEVEL,
               bevel_corners=theme.POPUP_BEVEL_CORNERS)


def wave(n, base, amp, k=0.37, seed=1):
    rnd = random.Random(seed)
    return [max(0.0, min(100.0, base + amp * math.sin(i * k) + rnd.uniform(-amp, amp) * 0.4))
            for i in range(n)]


def hardware_panel():
    from indigoshell.widgets import hardware_panel as hp
    from indigoshell.widgets.hardware_panel import HardwarePanel
    p = HardwarePanel(width=560)
    p._disk_bar.set_value(66)
    p._disk_value.set_value("312.4 GB / 476.9 GB  66%")
    p._cpu_value.set_value("42%")
    p._cpu_graph.set_samples(wave(60, 38, 18, seed=2))
    p._ram_value.set_value("63%")
    p._ram_total.set_value("20.1 GB / 31.9 GB")
    p._ram_graph.set_samples(wave(60, 62, 3, k=0.11, seed=3))
    p._gpu_name.set_value("GPU 0")
    p._gpu_util_bar.set_value(23)
    p._gpu_util.set_value("23%")
    p._gpu_mem_bar.set_value(31)
    p._gpu_mem.set_value("2511 / 8188 MiB")
    p._gpu_temp_bar.set_value(hp.temp_pct(58))
    p._gpu_temp.set_value("58°C")
    p._apply_state("hybrid", "balanced")
    return p


def network_panel(tab=0):
    from indigoshell.widgets.network_panel import NetworkPanel, _IfaceCard
    p = NetworkPanel(width=560)
    p._firewall = ("active", "firewalld")
    p._state.set_value("active")
    p._service.set_value("firewalld")
    wl = _IfaceCard("wlan0")
    wl.update("192.168.1.42", NET_NAME, 18.4 * 1024, 2.31 * 1024 * 1024)
    en = _IfaceCard("enp3s0")
    en.update("10.0.0.7", None, 0.0, 512.0)
    p._iface_column.replace([wl, en])
    if tab:
        p._switch(1)
        p._server.set_value("Night City Exchange (12 km)")
        p._server.color = theme.FG
        p._ping.set_value("14")
        p._ping_unit.set_value("ms")
        p._down_meter.set_value(72)
        p._up_meter.set_value(38)
        p._down_value.set_value("412.7")
        p._down_unit.set_value("Mbit/s")
        p._up_value.set_value("96.3")
        p._up_unit.set_value("Mbit/s")
        p._button.set_label("CANCEL")
    return p


def render_panels() -> None:
    m = 28
    shots = []
    for name, content, size in (
            ("panel_hardware.png", hardware_panel(), (600, 520)),
            ("panel_network.png", network_panel(0), None),
            ("panel_network_speedtest.png", network_panel(1), None)):
        box = panel_box(content)
        if size is None:
            nat = box.measure(Size(600, 10_000.0))
            size = (600, int(math.ceil(nat.height)))
        w, h = size
        place(box, m, m, w, h)
        win = holo_window(box, w + 2 * m, h + 2 * m)
        img = over(desktop(w + 2 * m, h + 2 * m), win)
        save(img, name)
        shots.append(img)
    # 3x: a card's stripe, key, value and segmented bar
    save(zoom(crop(shots[0], m, m, 330, 150)), "panel_card_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# the launcher (MOD+D)
# ═══════════════════════════════════════════════════════════════════════
APPS = [  # made-up apps: (name, generic name, keywords)
    ("Deckterm", "Terminal", ("shell", "console")),
    ("Flux", "Web Browser", ("web", "internet")),
    ("Icebreaker", "Network Scanner", ("net", "scan")),
    ("Ledger", "Spreadsheet", ("table",)),
    ("Netwatch", "System Monitor", ("process", "cpu")),
    ("Neon Notes", "Text Editor", ("text", "write")),
    ("Overpass", "Map Viewer", ("map",)),
    ("Patchbay", "Audio Mixer", ("sound", "volume")),
    ("Relay", "Chat Client", ("message",)),
    ("Shard Vault", "Password Manager", ("secret", "key")),
    ("Skyline", "Image Viewer", ("photo", "picture")),
    ("Static", "Music Player", ("audio", "song")),
    ("Synth Lab", "Sequencer", ("midi",)),
    ("Tracer", "Debugger", ("gdb",)),
    ("Uplink", "File Transfer", ("sync", "ssh")),
]


def launcher(keys=(), history=()):
    from indigoshell.applink.launcher import Launcher
    from indigoshell.services.apps import App, Catalog
    from indigoshell.services.desktop_entry import DesktopEntry
    from indigoshell.widgets.base import KeyEvent

    class Catalog_(Catalog):
        def refresh(self):
            return False

    class Icons:
        def get(self, name, size):
            return None             # no icon theme: each app's initial, the real fallback

        def refresh(self):
            pass

    cat = Catalog_()
    cat.apps = [App(DesktopEntry(id=f"{n.lower().replace(' ', '-')}.desktop", path="",
                                 name=n, generic_name=g, keywords=k, exec=n.lower()))
                for n, g, k in APPS]
    for name in history:
        cat.history.record(f"{name.lower().replace(' ', '-')}.desktop")
    l = Launcher(rows=12, width=760, catalog=cat, icons=Icons())
    l.attach(Host())
    for k in keys:
        l.key(KeyEvent(keysym=ord(k), text=k) if isinstance(k, str) else KeyEvent(keysym=k))
    return l


def render_launcher() -> None:
    DOWN = 0xFF54
    m = 48
    shots = []
    for name, keys, hist in (("launcher.png", list("net") + [DOWN], ("Netwatch", "Deckterm")),
                             ("launcher_empty.png", [], ("Flux", "Flux", "Deckterm"))):
        l = launcher(keys, hist)
        size = l.measure(Size(10_000.0, 10_000.0))
        w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
        place(l, m, m, w, h)
        win = holo_window(l, w + 2 * m, h + 2 * m)
        img = over(desktop(w + 2 * m, h + 2 * m), win)
        save(img, name)
        shots.append(img)
    save(zoom(crop(shots[0], m, m, 420, 220)), "launcher_3x.png")


# ═══════════════════════════════════════════════════════════════════════
# the call (MOD+A): CallHost over a frosted city, as readme_call frames it
# ═══════════════════════════════════════════════════════════════════════
def call_agent(**state):
    a = hub_agent(**state)
    a.model, a.effort, a._label = "opus", "medium", "opus 5.5"
    # How full her chat is, as the hub's state says (the heading's meter).
    a.context = {"used": 132_000, "compact_at": 200_000, "cap": 500_000,
                 "window": 1_000_000, "from": None}
    return a


def call_shot(agent, *, typed="", spin=0.0, expand=(), select=None,
              scan_at: float | None = None, nudge: bool = False) -> tuple:
    """(image, host): the call laid out at its natural size, its widgets
    ticked once on this script's clock, framed over the city."""
    from indigoshell.agent.ui.call import CallHost
    from indigoshell.widgets.holo import SCAN_EVERY_S
    shot_t = NOW[0]
    NOW[0] = shot_t - 3.0           # the reply arrived 3 s ago: decoded, settled
    host = rc.place(CallHost(agent))
    if typed:
        host.you_plate.input.set_text(typed)
    host.agent_plate.expanded = set(expand)
    if select:
        host.agent_plate.anchor, host.agent_plate.focus = select
    host._on_agent("update")
    rc.tick(host)
    NOW[0] = shot_t
    host._on_agent("update")
    if nudge:
        host._nudge_t0 = NOW[0] - 0.05
    host.projection.epoch = NOW[0] - (scan_at if scan_at is not None else SCAN_EVERY_S - 0.5)
    rc.tick(host)
    host.agent_chip.angle = -90 + spin
    W = int(host.rect.width()) + 2 * rc.MARGIN
    H = int(host._natural()) + 2 * rc.MARGIN
    return rc.frame(host, W, H), host


REPLY = (
    "## Uplink is clean\n"
    "Jacked in and walked the logs. The **wifi card** is fine; the drops are *power "
    "saving*: the driver parks the radio after a few idle minutes and the access "
    "point kicks you.\n\n"
    "- three resets since boot, all on battery\n"
    "- no ICE trips, no failed units\n\n"
    "Flatline the power saving for this link:\n\n"
    "```\nnmcli connection modify NIGHTCITY-5G 802-11-wireless.powersave 2\n```")


def render_call() -> None:
    from indigoshell.agent.activity import Activity, Approval, describe_tool
    import asyncio

    def bash(cmd, result="", id=0, failed=False):
        return Activity("tool", describe_tool("Bash", {"command": cmd}), detail=cmd,
                        result=result, id=id, failed=failed)

    random.seed(5)
    # working: steps above the rule, the reply arriving below it
    NOW[0] = 11000.0
    a = call_agent(phase="working", busy=True, turn_started=NOW[0] - 41,
                   heard="why does my wifi keep dropping every few minutes?")
    a.activity.extend([Activity("note", "Checking the driver and the logs first."),
                       bash("journalctl -b -u NetworkManager --since -1h | tail -n 40", "...", 1),
                       bash("iw dev wlan0 link", "Connected to 00:00:5e:00:53:01", 2)])
    a.message = "Three resets since boot, all from the driver's power saving"
    working, _ = call_shot(a, spin=55)
    save(working, "call_working.png")

    # answered: the reply in Markdown; the chip's two-blink nudge, lit
    NOW[0] = 12000.0
    b = call_agent(phase="idle", busy=False, turn_started=NOW[0] - 60,
                   heard="why does my wifi keep dropping every few minutes?")
    b.activity.extend([bash("iw dev wlan0 link", "Connected", 3),
                       bash("journalctl -k -g iwlwifi", "...", 4)])
    b.message = REPLY
    answered, _ = call_shot(b, typed="do it", nudge=True)
    save(answered, "call_answer.png")

    # asking: GREENLIGHT / SCRUB
    NOW[0] = 13000.0
    c = call_agent(phase="asking", busy=True, turn_started=NOW[0] - 9,
                   heard="patch the deck. everything.")
    c.activity.extend([bash("checkupdates | wc -l", "143", 5),
                       bash("checkupdates | grep -E '^(linux|mesa)'", "linux ...", 6)])
    c.message = ("143 packages waiting, a new kernel among them. That's root work, "
                 "so the ICE wants your say-so.")
    loop = asyncio.new_event_loop()
    c._hub_ask = Approval(tool="Bash", title="Run a command",
                          detail="sudo pacman -Syu --noconfirm", future=loop.create_future())
    c.ring_started = NOW[0] - 0.05
    ask, host_c = call_shot(c)
    save(ask, "call_ask.png")
    # 3x: the agent's chip and heading, and the answers
    chip = host_c.agent_chip.rect
    x0 = chip.left() - 6
    save(zoom(crop(ask, x0, chip.top() - 6, 520, 90)), "call_chip_heading_3x.png")
    keys = host_c.agent_plate.choices
    kx, ky = keys[0].rect.left() - 10, keys[0].rect.top() - 10
    save(zoom(crop(ask, kx, ky, keys[1].rect.right() - kx + 14, 50)), "call_answers_3x.png")
    # the scan band passing down an open call
    NOW[0] = 14000.0
    scanned, _ = call_shot(b, scan_at=1.15)
    save(scanned, "call_scan.png")


def render_call_chips() -> None:
    """The chip's looks: who it is (fill tint) and what it says (frame,
    arcs, label)."""
    from indigoshell.agent.ui.chip import Chip
    from indigoshell.agent.ui import style
    looks = [
        ("YOU", theme.MAGENTA_BRIGHT, (theme.MAGENTA_BRIGHT, "YOU", "still"), -90),
        ("idle / waiting on you: still", her(), (her(), "CALL", "still"), -90),
        ("working: arcs turn (200 deg/s)", her(), (her(), "CALL", "working"), -35),
        ("asking: yellow, blinking", her(), (theme.YELLOW_BRIGHT, "ASK", "blink"), -90),
        ("line down: red, DROP", her(), (theme.ERROR, "DROP", "still"), -90),
        ("missed: dimmed 45%", her(), (theme.ERROR, "CALL", "dim"), -90),
    ]
    NOW[0] = 15000.0
    tiles = []
    for cap, tint, look, angle in looks:
        ch = Chip(style.CHIP, lambda look=look: look, lambda: NOW[0] - 0.05, tint=tint)
        ch.angle = angle
        img = widget_tile(ch, pad=14, bg=theme.BG)
        tiles.append((cap, img))
    img = sheet(tiles, cols=3, title="the call's chip: tint = whose, frame/arcs/label = state")
    save(img, "call_chips.png")
    save(zoom_sheet(tiles[1:4], cols=3, title="call chips at 3x"), "call_chips_3x.png")


def render_progress() -> None:
    """Progress on the call: a job's segmented bar under its step, a
    pipeline's stages, the hung-up plate's one line."""
    import render_call_progress as rcp
    from indigoshell.agent.activity import Activity
    from indigoshell.agent.ui.status import CallStatus
    NOW[0] = 16000.0
    a = rcp.agent_in(phase="working", busy=True, turn_started=NOW[0] - 64,
                     heard="pull the field recordings and transcribe them")
    a.model, a.effort, a._label = "opus", "high", "opus 5.5"
    a.activity = [Activity("tool", "$ rsync -a deck:/recordings/ ~/field/", tool_id="t1",
                           tool="Bash", summary="212.0 MB in 0:41"),
                  Activity("tool", "$ transcribe ~/field/*.opus", tool_id="t2", tool="Bash")]
    rcp.job(a, "t2", label="transcribe", stages=("audio", "model", "transcribe", "write"),
            stage="transcribe", done=252.0, total=588.0, unit="s", text="9:48 long",
            rate=14.2, age=40)
    rcp.job(a, "", label="night-city-dataset.tar", done=812e6, total=1.32e9, unit="B",
            rate=9.4e6, age=90)
    img, _ = call_shot(a, spin=30)
    save(img, "progress_call.png")
    # the corner plate's compact line (CallStatus), on the city
    s = rcp.agent_in(phase="working", busy=True, turn_started=NOW[0] - 23,
                     activity=[Activity("tool", "$ rsync ...", tool_id="t1", tool="Bash")])
    rcp.job(s, "t1", label="night-city-dataset.tar", done=22.4e6, total=51.0e6, unit="B",
            rate=3.1e6)
    st = rc.place(CallStatus(s))
    st.projection.epoch = NOW[0] - 6.5
    rc.tick(st)
    st.chip.angle = -40
    W = int(st.rect.width()) + 2 * rc.MARGIN
    H = int(st.rect.height()) + 2 * rc.MARGIN
    save(rc.frame(st, W, H), "progress_status.png")


# ═══════════════════════════════════════════════════════════════════════
# motion: still strips, and short GIFs on this script's clock
# ═══════════════════════════════════════════════════════════════════════
def frames_at(fps: int, seconds: float):
    return [i / fps for i in range(int(round(seconds * fps)))]


def spawn_source():
    """The PROFILE chord menu as its window's surface (holo), with the
    spawn's bleed round it (ScanLock.bleed: tear + split + 2 px)."""
    from indigoshell.plugin import Item
    cfg = _config()
    bleed = int(math.ceil(cfg.PANEL_SPAWN.bleed(0, 0)))
    host = menu_host([Item("POWER-SAVER"), Item("BALANCED", active=True),
                      Item("PERFORMANCE"), Item("LOGOUT", hint="qtile")])
    size = host.measure(Size(10_000.0, 10_000.0))
    w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
    place(host, bleed, bleed, w, h)
    W, H = w + 2 * bleed, h + 2 * bleed
    return holo_window(host, W, H), cfg.PANEL_SPAWN, W, H


def render_spawn() -> None:
    src, fx, W, H = spawn_source()
    back = desktop(W, H)
    seed = 0.37 * 997
    steps = [0.08, 0.2, 0.32, 0.45, 0.6, 0.8, 1.0]
    tiles = []
    for a in steps:
        frame = src if a >= 0.999 else effect_frame(src, fx, appear=a, t=2.0 + a * fx.duration,
                                                    seed=seed)
        tiles.append((f"appear {a:.2f}  (t = {a * fx.duration * 1000:.0f} ms)", over(back, frame)))
    save(sheet(tiles, cols=4, title=f"panel spawn (ScanLock, PANEL_SPAWN): {fx.duration * 1000:.0f}"
               f" ms, {fx.band_px:.0f} px bands, tear {fx.tear_px:.0f} px, split "
               f"{fx.split_px:.0f} px; despawn plays it backwards"), "motion_spawn_strip.png")
    # the GIF: open, hold, close, pause
    fps, frames = 30, []
    n = int(round(fx.duration * fps))
    seq = ([(i / n, i / fps) for i in range(n + 1)] + [(1.0, 0.0)] * int(0.7 * fps)
           + [(1 - i / n, i / fps) for i in range(n + 1)] + [(0.0, 0.0)] * int(0.45 * fps))
    for appear, t in seq:
        if appear >= 0.999:
            frame = src
        elif appear <= 0.001:
            frame = None
        else:
            frame = effect_frame(src, fx, appear=appear, t=1.0 + t, seed=seed)
        frames.append(over(back, frame) if frame is not None else back)
    encode_gif("motion_spawn.gif", frames, fps)


def render_lyrics_gif() -> None:
    """The lyric line's GlitchWipe reveal (0.8 s), in place on the bar."""
    row, w = bottom_bar()
    lyr = w["lyrics"]
    bar_strip(row, BAR_W)                   # lays the row out
    x0 = w["ws"].rect.left() - 8
    x1 = lyr.rect.left() + T.measure(LYRIC, lyr._label.size) + 40
    fps, frames = 30, []
    t0 = 20000.0
    NOW[0] = t0
    lyr._full = ""
    lyr._set_text(LYRIC)
    lyr._reveal_seed = 211.0
    lyr._last_animate_t = None
    lyr.animate(t0)
    for t in frames_at(fps, 1.5):
        NOW[0] = t0 + t
        if t > 0:
            lyr.animate(NOW[0])
        s, c = surface(BAR_W, theme.BAR_HEIGHT, theme.BAR_BG)
        row.paint(c)
        frames.append(crop(snap(s), x0, 0, x1 - x0, theme.BAR_HEIGHT))
    encode_gif("motion_lyrics_reveal.gif", frames, fps)
    # still strip of the same reveal
    tiles = [(f"{k * 100:.0f}% of 0.8 s", frames[int(k * 0.8 * fps)]) for k in
             (0.15, 0.35, 0.55, 0.75, 1.0)]
    save(sheet(tiles, cols=1, title="lyric reveal (GlitchWipe): cursor bar sweeps, cells, torn "
               "glyphs, clean"), "motion_lyrics_strip.png")


def render_tabs_gif() -> None:
    """A tab switch (light slides, 110 ms), a close (wipe 80 ms, then the
    rest slide 110 ms), and a busy tab's comet."""
    from indigoshell.applink import bar as B
    titles = TAB_TITLES[:4]
    W = 4 * 246 + 2 * theme.BAR_MARGIN
    view = app_view(titles, 0, host="")
    place(view, theme.BAR_MARGIN, 0, W - 2 * theme.BAR_MARGIN, theme.TOP_BAR_HEIGHT)
    settle(view)

    def spec(cur, ids, busy=()):
        tabs = [B.tab(i, titles[i - 101], busy=i in busy) for i in ids]
        return B.bar([B.tabs("tabs", tabs, current=cur, can=["close", "move", "detach"])], [])

    fps, frames = 30, []
    t0 = 30000.0
    events = {0.0: spec(101, [101, 102, 103, 104]),
              0.5: spec(103, [101, 102, 103, 104]),
              1.2: spec(103, [101, 103, 104]),
              1.9: spec(103, [101, 103, 104], busy=(104,))}
    for t in frames_at(fps, 3.2):
        NOW[0] = t0 + t
        for at in sorted(events):
            if at <= t < at + 1 / fps:
                view.set_bar(events[at])
        s, c = surface(W, theme.TOP_BAR_HEIGHT, theme.BAR_BG)
        place(view, theme.BAR_MARGIN, 0, W - 2 * theme.BAR_MARGIN, theme.TOP_BAR_HEIGHT)
        view.paint(c)
        frames.append(snap(s))
    encode_gif("motion_tabs.gif", frames, fps)


def render_chip_gif() -> None:
    """The top bar's agent chip: working (arcs turn), a question (yellow,
    the square blinks), a call back (red, both blink in the ring
    pattern)."""
    from indigoshell.agent.activity import Approval
    import asyncio
    loop = asyncio.new_event_loop()
    t0 = 40000.0
    NOW[0] = t0
    a = working_agent()
    chip = agent_chip(a, "working")
    router = chip.routers[0]
    CW = 440
    fps, frames = 30, []
    for t in frames_at(fps, 4.2):
        NOW[0] = t0 + t
        if abs(t - 1.4) < 1e-6:
            a.busy, a.phase = True, "asking"
            a._hub_ask = Approval(tool="Bash", title="Run a command",
                                  detail="sudo systemctl restart bluetooth",
                                  future=loop.create_future())
            a.ring_started = NOW[0]
            router.state = "calling"
            chip._on_router()
        if abs(t - 2.6) < 1e-6:
            a._hub_ask = None
            a.busy, a.phase, a.ringing = False, "idle", "done"
            a.ring_line = "Patched. 143 packages, no reboot needed."
            a.ring_started = NOW[0]
            router.state = "calling"
            chip._on_router()
        rc.tick(chip)
        s, c = surface(CW, theme.TOP_BAR_HEIGHT, theme.BAR_BG)
        size = chip.measure(Size(CW, theme.TOP_BAR_HEIGHT))
        place(chip, CW - theme.BAR_MARGIN - size.width,
              (theme.TOP_BAR_HEIGHT - size.height) / 2, size.width, size.height)
        chip.paint(c)
        frames.append(snap(s))
    encode_gif("motion_agent_chip.gif", frames, fps)


def render_scan_gif() -> None:
    """The holo scan: one pass of the band down a panel window (every 7 s,
    170 px/s), shown at 24 fps."""
    from indigoshell.plugin import Item
    from indigoshell.widgets.holo import SCAN_HEAD_PX, SCAN_SPEED, SCAN_WAKE_PX
    host = menu_host([Item("SUSPEND"), Item("POWEROFF"), Item("REBOOT"),
                      Item("LOGOUT", hint="qtile")])
    size = host.measure(Size(10_000.0, 10_000.0))
    w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
    m = 16
    place(host, m, m, w, h)
    W, H = w + 2 * m, h + 2 * m
    back = desktop(W, H)
    span = (h + SCAN_WAKE_PX + 5 * SCAN_HEAD_PX) / SCAN_SPEED + 0.3
    fps, frames = 24, []
    NOW[0] = 50000.0
    for t in frames_at(fps, span):
        frames.append(over(back, holo_window(host, W, H, scan_at=t)))
    encode_gif("motion_holo_scan.gif", frames, fps)


def render_menu_gif() -> None:
    """A chord menu taking keys: a digit held (armed, pulsing cyan/yellow
    every 80 ms), let go; then a key it has no row for (every row flashes
    magenta, on/off twice at 70 ms)."""
    from indigoshell.plugin import Item
    from indigoshell.widgets.base import KeyEvent
    host = menu_host([Item("SUSPEND"), Item("POWEROFF"), Item("REBOOT"),
                      Item("LOGOUT", hint="qtile")])
    size = host.measure(Size(10_000.0, 10_000.0))
    w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
    m = 12
    place(host, m, m, w, h)
    W, H = w + 2 * m, h + 2 * m
    back = desktop(W, H)
    fps, frames = 30, []
    t0 = 60000.0
    host._last_animate_t = None
    for t in frames_at(fps, 2.2):
        NOW[0] = t0 + t
        if abs(t - 0.3) < 1e-6:
            host.key(KeyEvent(keysym=0x32))          # "2" down: arms POWEROFF
        if abs(t - 1.1) < 1e-6:
            host.disarm()                            # let go (no action fired here)
        if abs(t - 1.4) < 1e-6:
            host.key(KeyEvent(keysym=ord("x"), text="x"))
        if host.animation_fps:
            host.animate(NOW[0])
        else:
            host._last_animate_t = None
        frames.append(over(back, holo_window(host, W, H)))
    encode_gif("motion_menu_keys.gif", frames, fps)


# ═══════════════════════════════════════════════════════════════════════
# the tray: its panel rows and an app's menu as the shell draws it
# ═══════════════════════════════════════════════════════════════════════
def render_tray() -> None:
    from indigoshell.services.dbusmenu import MenuNode
    from indigoshell.widgets.layout import Align, Column
    from indigoshell.widgets.systray import TrayMenu, _TrayRow, _menu_box
    names = ["Patchbay", "Relay", "Shard Vault"]
    rows = [_TrayRow(None, types.SimpleNamespace(title=n, tooltip_title="", id=n))
            for n in names]
    col = Column(rows, spacing=4, align=Align.STRETCH)
    size = col.measure(Size(10_000.0, 10_000.0))
    w, h = int(math.ceil(size.width)), int(math.ceil(size.height))
    nodes = [MenuNode(1, "Show _window"), MenuNode(2, "Mute notifications",
                                                   toggle_type="checkmark", toggle_state=1),
             MenuNode(3, separator=True),
             MenuNode(4, "Status", has_submenu=True,
                      children=[MenuNode(5, "Online"), MenuNode(6, "Away")]),
             MenuNode(7, "Sync now", enabled=False), MenuNode(8, separator=True),
             MenuNode(9, "Quit")]
    menu = TrayMenu("relay", nodes)
    menu._expanded = {4}
    menu.replace(menu._build_rows())
    box = _menu_box(menu)
    msize = box.measure(Size(10_000.0, 10_000.0))
    mw, mh = int(math.ceil(msize.width)), int(math.ceil(msize.height))
    m = 16
    W, H = m + mw + 10 + w + m, m + max(h, mh) + m
    place(box, m, m, mw, mh)
    menu.children()[0].hovered = True
    place(col, m + mw + 10, m, w, h)
    img = over(desktop(W, H), holo_window(_Both(box, col), W, H,
                                          rect=skia.Rect.MakeWH(W, H)))
    save(img, "tray.png")


class _Both(Widget):
    def __init__(self, *ws):
        super().__init__()
        self.ws = ws

    def paint(self, canvas):
        for w in self.ws:
            w.paint(canvas)


# ═══════════════════════════════════════════════════════════════════════
# Flux's Watch: a STAND-IN (the real overlay is Qt Quick on the GPU)
# ═══════════════════════════════════════════════════════════════════════
def fake_page(w: int, h: int) -> skia.Image:
    """A neutral light web page: headings, text lines, a picture."""
    s, c = surface(w, h, "#f4f5f8")
    p = skia.Paint(AntiAlias=False)
    p.setColor(C("#1f2328"))
    c.drawRect(skia.Rect.MakeXYWH(32, 30, 300, 16), p)
    rnd = random.Random(9)
    y = 70
    for _block in range(5):
        for _ in range(rnd.randint(2, 4)):
            p.setColor(C("#57606a"))
            c.drawRect(skia.Rect.MakeXYWH(32, y, rnd.randint(380, w - 300), 6), p)
            y += 15
        y += 14
    img = skia.Rect.MakeXYWH(w - 240, 70, 200, 130)
    sh = skia.GradientShader.MakeLinear([skia.Point(img.left(), img.top()),
                                         skia.Point(img.right(), img.bottom())],
                                        [C("#4aa3ff"), C("#ffd27a"), C("#ff7a59")])
    c.drawRect(img, skia.Paint(Shader=sh))
    return snap(s)


def render_flux_standin() -> None:
    """What the user sees while the agent drives a tab: a 2 px frame in her
    colour, the mark, and over the page her tint, the panels' raster and
    scan; the lock starts with the panel spawn glitch. Built
    from the shell's own originals (holo.raster, Projection, ScanLock with
    PANEL_SPAWN) and flux/qt/effects.py's constants, in Skia, because the
    real thing is a QQuickWidget that only runs on a display."""
    from flux.qt import effects as ffx
    from flux.qt.watch import FRAME_PX, MARK
    from indigoshell.widgets.holo import Projection, raster
    W, H = 760, 300
    page = fake_page(W, H)
    from indigoshell.core import persona
    name, col_her = persona.NAME, persona.COLOR

    def projected():
        s, c = surface(W, H)
        c.drawImage(page, 0, 0)
        c.drawRect(skia.Rect.MakeWH(W, H), fill(col_her, ffx.PLATE_TINT, aa=False))
        lines = skia.Paint(Shader=raster(ffx.RASTER, ffx.RASTER_ALPHA))
        c.drawRect(skia.Rect.MakeWH(W, H), lines)
        return s, c

    def chrome(img):
        s, c = surface(W, H)
        c.drawImage(img, 0, 0)
        f = FRAME_PX
        col = fill(col_her, aa=False)
        for r in ((0, 0, W, f), (0, H - f, W, f), (0, 0, f, H), (W - f, 0, f, H)):
            c.drawRect(skia.Rect.MakeXYWH(*r), col)
        mark = MARK.format(name=name)
        mw = T.measure(mark, 15) + 16
        mh = T.line_height(15) + 6
        r = skia.Rect.MakeXYWH(W - f - mw, H - f - mh, mw, mh)
        c.drawRect(r, fill(ffx.BACKGROUND, aa=False))
        T.draw(c, mark, r.left() + 8, T.baseline_in(r, 15), 15, C(col_her))
        return snap(s)

    # holding: tint + raster, the scan band partway down
    s, c = projected()
    proj = Projection()
    proj.window = Host()
    proj.arrange(skia.Rect.MakeWH(W, H))
    NOW[0] = 70000.0
    proj.epoch = NOW[0] - 0.9
    proj.animate(NOW[0])
    proj.paint(c)
    holding = chrome(snap(s))
    # the lock starting: the spawn glitch over the projected page, on Flux's bg
    src = snap(projected()[0])
    fx = _config().PANEL_SPAWN
    g = effect_frame(src, fx, appear=0.42, t=1.0, seed=0.37 * 997)
    s3, c3 = surface(W, H, ffx.BACKGROUND)
    c3.drawImage(g, 0, 0)
    glitch = chrome(snap(s3))
    save(sheet([("the page, user in control (no overlay)", page),
                ("lock starts: the panel spawn glitch over the page (0.22 s)", glitch),
                ("lock holds: her tint 12%, raster, scan band; frame + mark in her "
                 "colour", holding)], cols=1,
               title="Flux while the agent drives a tab — Skia stand-in built from the "
               "shell's originals, not a capture of Flux"), "flux_watch_standin.png")


# ═══════════════════════════════════════════════════════════════════════
# overview: one screen, both bars and the overlays (a composite)
# ═══════════════════════════════════════════════════════════════════════
def render_overview() -> None:
    from indigoshell.applink.kinds import look
    W, H = 1600, 900
    NOW[0] = 3000.0 + 0.35 * look.COMET_S
    view = app_view(TAB_TITLES, 0, busy=(2,), agents={1: ("driving",)})
    chip = agent_chip(working_agent(), "working", angle=-60)
    top = top_strip(view, chip, width=W)
    row, _ = bottom_bar()
    bottom = bar_strip(row, W)
    back = city(W, H)
    s, c = surface(W, H)
    c.drawImage(back, 0, 0)
    # the call, top right
    NOW[0] = 12000.0
    from indigoshell.agent.activity import Activity, describe_tool
    b = call_agent(phase="idle", busy=False, turn_started=NOW[0] - 60,
                   heard="why does my wifi keep dropping every few minutes?")
    b.activity.extend([Activity("tool", describe_tool("Bash", {"command": "iw dev wlan0 link"}),
                                id=3)])
    b.message = REPLY
    from indigoshell.agent.ui.call import CallHost
    from indigoshell.widgets.holo import SCAN_EVERY_S
    NOW[0] -= 3
    host = CallHost(b)
    Widget.attach(host, rc.FakeWindow())
    m = host.measure(Size(10_000.0, 10_000.0))
    # where config_default's call() puts it: top-right, 16 px under the top bar
    spec, _ = _config().call(b)
    cx = W - spec.margin.right - m.width
    cy = spec.margin.top
    host.measure(Size(m.width, m.height))
    host.arrange(skia.Rect.MakeXYWH(cx, cy, m.width, m.height))
    host._on_agent("update")
    rc.tick(host)
    NOW[0] += 3
    host._on_agent("update")
    host.projection.epoch = NOW[0] - (SCAN_EVERY_S - 0.5)
    rc.tick(host)
    lay = layer(host.paint, W, H)
    c.drawImage(top, 0, 0)
    c.drawImage(bottom, 0, H - theme.BAR_HEIGHT)
    back = snap(s)                  # the bars are under the overlay: blurred with the rest
    c.save()
    c.clipPath(host.blur_shape(), doAntiAlias=True)
    bp = skia.Paint()
    bp.setImageFilter(skia.ImageFilters.Blur(9.0, 9.0))
    c.drawImage(back, 0, 0, skia.SamplingOptions(), bp)
    c.restore()
    c.drawImage(lay, 0, 0)
    # a toast, bottom right above the bar
    t = toast(**TOASTS[2])
    ts = t.measure(Size(theme.NOTIF_WIDTH, 10_000.0))
    tw_, th = int(ts.width), int(math.ceil(ts.height))
    place(t, 0, 0, tw_, th)
    c.drawImage(holo_window(t, tw_, th), W - theme.NOTIF_OFFSET_X - tw_,
                H - theme.NOTIF_OFFSET_Y - th)
    save(snap(s), "overview.png")


# ═══════════════════════════════════════════════════════════════════════
# tokens.json: values read from the modules, each with its file:constant
# ═══════════════════════════════════════════════════════════════════════
def _rel(mod) -> str:
    """A module's (or a class's module's) file, relative to the checkout."""
    if isinstance(mod, type):
        return os.path.relpath(sys.modules[mod.__module__].__file__, REPO)
    return os.path.relpath(mod.__file__, REPO)


def tok(mod, name: str, unit: str | None = None, note: str | None = None, **extra) -> dict:
    """One token: the module's constant's value and where it lives."""
    value = getattr(mod, name)
    if isinstance(value, tuple):
        value = list(value)
    out = {"value": value}
    if unit:
        out["unit"] = unit
    owner = f"{mod.__name__}." if isinstance(mod, type) else ""
    out["source"] = f"{_rel(mod)}:{owner}{name}"
    if note:
        out["note"] = note
    out.update(extra)
    return out


def lit(value, source: str, unit: str | None = None, note: str | None = None) -> dict:
    """A value written inline in the code (no constant of its own)."""
    out = {"value": value}
    if unit:
        out["unit"] = unit
    out["source"] = source + " (inline)"
    if note:
        out["note"] = note
    return out


def color_tok(hexv: str, source: str, role: str = "", aliases=(), alpha=None) -> dict:
    out = {"hex": hexv[:7]}
    a = alpha
    if len(hexv) == 9:
        a = round(int(hexv[7:9], 16) / 255, 3)
    if a is not None:
        out["alpha"] = a
    out["source"] = source
    if aliases:
        out["aliases"] = list(aliases)
    if role:
        out["role"] = role
    return out


def effect_params(obj, source: str, note: str = "") -> dict:
    def clean(v):
        if isinstance(v, (tuple, list)):
            return [clean(x) for x in v]
        return round(v, 4) if isinstance(v, float) else v
    out = {k: clean(v) for k, v in vars(obj).items() if not k.startswith("_")}
    out = {"params": out, "class": type(obj).__name__, "source": source}
    if note:
        out["note"] = note
    return out


def build_tokens() -> dict:
    from indigoshell import effects as fx_mod
    from indigoshell.agent import remote
    from indigoshell.agent.ui import bars, choice, frame, reveal, status, style
    from indigoshell.applink import drop
    from indigoshell.applink.kinds import input as input_mod, look
    from indigoshell.core import persona
    from indigoshell.services import text_effects
    from indigoshell.widgets import (holo, hud, media as media_mod, menu as menu_mod, meters,
                                     notification as notif_mod, stdout_text,
                                     systray as tray_mod, tabs as tabs_mod, text_input,
                                     workspaces as ws_mod)
    from flux.qt import effects as flux_fx, watch as flux_watch
    cfg = _config()
    th = theme
    thf = _rel(theme)

    palette = {}
    for g, name, alias, role in PALETTE:
        aliases = [a.strip() for a in alias.split(",") if a.strip()]
        palette[name] = color_tok(getattr(th, name), f"{thf}:{name}", role, aliases)
        palette[name]["group"] = g

    semantic_names = ["BG", "BG_SHADOW", "FG", "FG_MUTED", "FG_STRONG", "FG_ACCENT", "ICON",
                      "HIGHLIGHT", "HUD_FG", "HUD_FG_BRIGHT", "HUD_PLATE", "HUD_SECTION",
                      "BAR_BG", "BAR_RULE", "MUSIC_FG", "STATUS_FG",
                      "WORKSPACE_CURRENT_FG", "WORKSPACE_OCCUPIED_FG", "WORKSPACE_EMPTY_FG",
                      "WORKSPACE_URGENT_FG", "NOTIF_BG", "NOTIF_FRAME_LOW", "NOTIF_FRAME_NORMAL",
                      "NOTIF_FRAME_CRITICAL", "NOTIF_BODY_FG_NORMAL", "NOTIF_BODY_FG_CRITICAL",
                      "NOTIF_SEPARATOR_FG", "NOTIF_SUMMARY_FG", "NOTIF_APPNAME_FG",
                      "NOTIF_ACTION_FG", "NOTIF_ACTION_BG", "NOTIF_TIMER_BORDER_FG",
                      "NOTIF_METER_DIM", "POPUP_BG", "POPUP_BORDER", "POPUP_RASTER"]
    notes = {"BAR_RULE": "defined; the default config's bars draw no rule (WindowSpec.rule is None)",
             "STATUS_FG": "defined, unused", "ICON": "defined, unused in code",
             "HUD_SECTION": "defined, unused",
             "POPUP_BG": "the panels' plate, ~95% violet-black"}
    semantic = {}
    for n in semantic_names:
        semantic[n] = color_tok(getattr(th, n), f"{thf}:{n}", notes.get(n, ""))

    agent_tokens = {
        "color": color_tok(persona.COLOR, f"{_rel(persona)}:COLOR",
                           "the agent's colour until the hub's welcome names hers "
                           "(welcome.agent): every reply, mark and plate of hers",
                           [persona.NAME]),
        "name": tok(persona, "NAME")}

    def fill_tok(name, accent, role):
        hexv, a = style.plate_fill(accent)
        return color_tok(hexv, f"{_rel(style)}:plate_fill({name})", role, alpha=a)

    call_colors = {
        "AGENT_ACCENT": color_tok(style.AGENT_ACCENT, f"{_rel(style)}:AGENT_ACCENT",
                                  "her colour (= persona.COLOR); the call reads the hub's "
                                  "welcome.agent at run time"),
        "YOU_ACCENT": color_tok(style.YOU_ACCENT, f"{_rel(style)}:YOU_ACCENT", "you"),
        "PLATE_BASE": color_tok(style.PLATE_BASE, f"{_rel(style)}:PLATE_BASE",
                                "a plate's base before its tint", alpha=style.PLATE_ALPHA),
        "PLATE_TINT": tok(style, "PLATE_TINT", note="share the base is pulled toward the "
                          "speaker's colour"),
        "PLATE_ALPHA": tok(style, "PLATE_ALPHA"),
        "YOU_FILL": fill_tok("YOU_ACCENT", style.YOU_ACCENT, "your plate"),
        "AGENT_FILL": fill_tok("AGENT_ACCENT", style.AGENT_ACCENT, "her plate"),
        "SLASH_FG": color_tok(style.SLASH_FG, f"{_rel(style)}:SLASH_FG", "the // before a name"),
        "ASK_ACCENT": color_tok(style.ASK_ACCENT, f"{_rel(style)}:ASK_ACCENT", "a question"),
        "FAIL_ACCENT": color_tok(style.FAIL_ACCENT, f"{_rel(style)}:FAIL_ACCENT", "line down, "
                                 "a failed step"),
        "RING_ACCENT": color_tok(style.RING_ACCENT, f"{_rel(style)}:RING_ACCENT",
                                 "a hung-up call ringing back"),
        "TOOL_FG": color_tok(style.TOOL_FG, f"{_rel(style)}:TOOL_FG", "a step (tool call)"),
        "CODE_FG": color_tok(style.CODE_FG, f"{_rel(style)}:CODE_FG", "inline code"),
        "LINK_FG": color_tok(style.LINK_FG, f"{_rel(style)}:LINK_FG", "links"),
        "SELECT_BG": color_tok(style.SELECT_BG[0], f"{_rel(style)}:SELECT_BG",
                               "selected text on the call", alpha=style.SELECT_BG[1]),
        "GREENLIGHT": lit(th.LIME_BRIGHT, "indigoshell/agent/ui/agent_plate.py:AgentPlate.choices",
                          note="LIME_BRIGHT"),
        "SCRUB": lit(th.ERROR, "indigoshell/agent/ui/agent_plate.py:AgentPlate.choices",
                     note="ERROR"),
    }

    def overlay(mod, name, role):
        spec = getattr(mod, name)
        return color_tok(spec[0], f"{_rel(mod)}:{name}", role,
                         alpha=spec[1] if spec[1] is not None else None)

    rows = {
        "menu_idle": overlay(menu_mod, "_IDLE", "chip slab (POPUP_BG)"),
        "menu_armed": overlay(menu_mod, "_ARMED", "armed row tint"),
        "menu_pulse": overlay(menu_mod, "_PULSE", "armed row's other beat"),
        "menu_active": overlay(menu_mod, "_ACTIVE", "the choice in effect"),
        "menu_reject": overlay(menu_mod, "_REJECT", "unmapped key flash"),
        "menu_border": overlay(menu_mod, "_BORDER", "chip outline"),
        "drop_selected": overlay(drop, "_SELECTED", "selected row slab (drop-down, launcher)"),
        "drop_border": overlay(drop, "_BORDER", "selected row outline"),
        "drop_reject": overlay(drop, "_REJECT", "a row that cannot be picked, flashed"),
        "text_selection": overlay(text_input, "SELECTION", "selected text in an input"),
        "tray_row_border": lit([th.CYAN_BRIGHT, 0.85], "indigoshell/widgets/systray.py:"
                               "_TrayRow.paint"),
        "tray_menu_bg": color_tok(tray_mod._MENU_BG, f"{_rel(tray_mod)}:_MENU_BG",
                                  "an app's tray menu"),
        "tray_menu_hover": lit([th.CYAN_BRIGHT, 0.18], "indigoshell/widgets/systray.py:"
                               "_MenuItemRow.paint"),
    }
    from indigoshell.widgets import network_panel as net_panel
    directions = {
        "sent_up": color_tok(net_panel.UP_FG, f"{_rel(net_panel)}:UP_FG",
                             "sent / up everywhere: the bar's upper light, the panel's "
                             "up rate, the speedtest's UP", ["CYAN_BRIGHT"]),
        "received_down": color_tok(net_panel.DOWN_FG, f"{_rel(net_panel)}:DOWN_FG",
                                   "received / down everywhere: the bar's lower light, the "
                                   "panel's down rate, the speedtest's DN", ["LIME_BRIGHT"]),
    }
    heat = {
        "ramp": {"value": [[share, next(n for _, n, _, _ in PALETTE
                                         if getattr(th, n) == col)]
                           for share, col in th.HEAT_RAMP],
                 "source": f"{thf}:HEAT_RAMP",
                 "note": "steps by share of the scale: the bar's TEMP, the hardware panel's "
                         "disk and GPU temperature; red = hot"},
        "temp_scale": {"value": [th.TEMP_MIN_C, th.TEMP_MAX_C], "unit": "degC",
                       "source": f"{thf}:TEMP_MIN_C, TEMP_MAX_C",
                       "note": "empty to full; so yellow from 60 degC, red from 78 degC"},
    }
    top_bar = {k: tok(look, k) for k in ("TAB_FG", "TAB_CURRENT_FG", "TAB_HOVER_FG", "NUMBER_FG",
                                          "NUMBER_CURRENT_FG", "BRACKET_FG", "OUTLINE", "LOAD_FG",
                                          "TEAR_FG", "OUTLINE_DIM_A", "OUTLINE_HOVER_A",
                                          "OUTLINE_GLOW", "OUTLINE_GLOW_A", "FILL_A",
                                          "LOAD_DIM_A", "COMET_LEN", "PAUSED_DIM", "TEAR_A")}
    top_bar["ROLE_COLORS"] = tok(look, "ROLE_COLORS", note="colour roles an app may name; "
                                 "'agent' gives her colour")
    top_bar["QUERY_FG"] = tok(input_mod, "QUERY_FG")
    top_bar["TOGGLE_ON"] = tok(input_mod, "TOGGLE_ON")
    top_bar["TOGGLE_OFF"] = tok(input_mod, "TOGGLE_OFF")

    pt = T.pt
    typography = {
        "family": tok(th, "FONT", note="FiraCode Nerd Font Mono: monospace, regular + bold, "
                      "no italic face (the call skews regular by -0.2)"),
        "fallbacks": tok(th, "FONT_FALLBACKS"),
        "px_per_pt": tok(T, "PX_PER_PT", note="sizes are Skia pixels; a size quoted in points "
                         "is pt x 96/72"),
        "advance_em": lit(round(T.advance(th.FONT_SIZE) / th.FONT_SIZE, 4),
                          "indigoshell/text.py:advance",
                          note="every glyph one cell wide; measured at FONT_SIZE "
                               f"({T.advance(th.FONT_SIZE):.2f} px at {th.FONT_SIZE} px)"),
        "sizes": {
            "FONT_SIZE": tok(th, "FONT_SIZE", "px", "names: network, tabs, menu/tray rows, inputs"),
            "FONT_SIZE_LG": tok(th, "FONT_SIZE_LG", "px", "song title (bold), input default"),
            "FONT_SIZE_ICON": tok(th, "FONT_SIZE_ICON", "px", "defined, unused"),
            "FONT_SIZE_XL": tok(th, "FONT_SIZE_XL", "px", "the idle media glyph"),
            "clock_time": lit(pt(18), "indigoshell/widgets/clock.py:Clock(time_size=18pt)", "px",
                              "bold, CYAN_BRIGHT"),
            "clock_date": lit(pt(11), "indigoshell/widgets/clock.py:Clock(date_size=11pt)", "px",
                              "BASE_MUTED"),
            "meter_value": lit(pt(14), "indigoshell/widgets/meters.py:StatMeter(value_size=14pt)",
                               "px", "bold: the 'values' size the user keeps"),
            "meter_label": lit(pt(9), "indigoshell/widgets/meters.py:StatMeter(label_size=9pt)",
                               "px"),
            "systag": lit(pt(13), "indigoshell/widgets/systag.py:Systag(size=13pt)", "px", "bold"),
            "network_ip": lit(pt(8), "indigoshell/widgets/network.py:Network(ip_size=8pt)", "px",
                              "host octet bold, CYAN_MID"),
            "lyric": lit(th.FONT_SIZE - 1, "indigoshell/config_default.py:lyrics(size)", "px"),
            "toast_heading": tok(notif_mod, "_HEAD_SIZE", "px", "bold"),
            "toast_body": tok(notif_mod, "_BODY_SIZE", "px"),
            "toast_action": tok(notif_mod, "_ACTION_SIZE", "px"),
            "call_name": tok(style, "NAME_SIZE", "px", "the // NAME heading, bold"),
            "call_body": tok(style, "BODY_SIZE", "px", "the reply"),
            "call_act": tok(style, "ACT_SIZE", "px", "steps, answers"),
            "call_tag": tok(style, "TAG_SIZE", "px", "key hints, model / effort"),
            "panel_key": tok(hud, "KEY_SIZE", "px", "KEY labels, panel body; tracking 1"),
            "panel_section": lit(th.FONT_SIZE - 5, "indigoshell/widgets/hud.py:section_header",
                                 "px", "bold, YELLOW_BRIGHT title + BASE_MUTED subtitle"),
            "tab_number": tok(look, "NUMBER_SIZE", "px", "bold"),
            "tab_tag": tok(look, "TAG_SIZE", "px", "bold: NEW WINDOW"),
            "row_name": tok(drop, "NAME_SIZE", "px", "drop-down / launcher row title"),
            "row_meta": tok(drop, "META_SIZE", "px", "row tag (bold, tracking 1), detail, chord"),
            "agent_box_name": tok(status, "BOX_NAME_SIZE", "px"),
            "agent_box_detail": tok(status, "BOX_DETAIL_SIZE", "px"),
            "tray_menu": tok(tray_mod, "_MENU_FONT_SIZE", "px"),
        },
        "line_height": {str(sz): round(T.line_height(sz), 2) for sz in
                        (10, 11, 12, 13, 14, 15, 16, 18, round(pt(14), 2), 24, 28)},
        "line_height_note": "descent - ascent of the font at that px size (text.line_height)",
        "tracking": {
            "menu_row": tok(menu_mod, "TRACKING", "px"),
            "panel_key": tok(hud, "KEY_TRACKING", "px"),
            "choice": tok(choice, "TRACKING", "px"),
            "speedtest_button": lit(1.5, "indigoshell/widgets/network_panel.py:_Button", "px"),
            "iface_name": lit(2.0, "indigoshell/widgets/network_panel.py:_IfaceCard", "px"),
            "tab_strip": lit(1.0, "indigoshell/widgets/tabs.py:TabBar.paint", "px"),
        },
        "italic_skew": lit(-0.2, "indigoshell/agent/ui/style.py:font_for"),
    }

    spacing = {k: tok(th, k, "px") for k in ("SPACING_SM", "SPACING_MD", "SPACING_LG",
                                              "SPACING_XL", "BAR_SPACING", "BAR_MARGIN",
                                              "NOTIF_PADDING_X", "NOTIF_PADDING_Y", "NOTIF_GAP",
                                              "NOTIF_OFFSET_X", "NOTIF_OFFSET_Y", "POPUP_GAP")}
    spacing.update({
        "panel_padding": lit(20, "indigoshell/config_default.py:WINDOWS (Box padding)", "px"),
        "card_padding": lit([10, 14, 10, 16], "indigoshell/widgets/hud.py:HudCard",
                            "px", "top, right, bottom, left"),
        "call_pad_x": tok(style, "PAD_X", "px"), "call_pad_y": tok(style, "PAD_Y", "px"),
        "call_plate_chip_gap": tok(style, "GAP", "px"),
        "call_row_gap": tok(style, "ROW_GAP", "px"),
        "menu_row_gap": tok(menu_mod, "ROW_GAP", "px"), "menu_pad_x": tok(menu_mod, "PAD_X", "px"),
        "menu_label_marker_gap": tok(menu_mod, "GAP", "px"),
        "row_gap": tok(drop, "ROW_GAP", "px"), "row_pad_x": tok(drop, "PAD_X", "px"),
        "slot_gap": tok(look, "SLOT_GAP", "px"), "slot_pad": tok(look, "SLOT_PAD", "px"),
        "number_gap": tok(look, "NUMBER_GAP", "px"),
        "agent_box_gap": tok(status, "BOX_GAP", "px"),
        "brackets_padding": lit([8, 4], "indigoshell/widgets/layout.py:Brackets", "px",
                                "horizontal, vertical"),
    })

    geometry = {
        "bar_height": tok(th, "BAR_HEIGHT", "px"),
        "top_bar_height": tok(th, "TOP_BAR_HEIGHT", "px"),
        "bevel": {
            "corners": tok(th, "POPUP_BEVEL_CORNERS", note="the 45-degree cuts; every bevel "
                           "in the shell uses these two, except the top bar's tab boxes "
                           "(top-right only)"),
            "panel": tok(th, "POPUP_BEVEL", "px"), "toast": tok(th, "NOTIF_BEVEL", "px"),
            "call_plate": tok(style, "BEVEL", "px"), "card": tok(hud, "BEVEL", "px"),
            "menu_row": tok(menu_mod, "BEVEL", "px"), "drop_row": tok(drop, "BEVEL", "px"),
            "tray_row": lit(tray_mod._TrayRow.BEVEL, "indigoshell/widgets/systray.py:"
                            "_TrayRow.BEVEL", "px"),
            "answer": tok(choice, "BEVEL", "px"), "tab_box": tok(look, "BOX_BEVEL", "px",
                                                              "top-right only"),
            "agent_box_and_chip": tok(status, "BAR_BEVEL", "px"),
            "look_chip": lit(5, "indigoshell/agent/ui/you_plate.py:_paint_chip", "px"),
            "inset_rule": "a path d px inside a bevel keeps parallel cuts with bevel - d(2 - sqrt 2)"
                          " (style.bevel_at)",
        },
        "stroke": {
            "panel_border": lit(2.0, "indigoshell/config_default.py:WINDOWS (border_width)", "px"),
            "toast_frame": tok(th, "NOTIF_BORDER_THICK", "px"),
            "call_frame": tok(style, "FRAME_W", "px"),
            "menu_row": tok(menu_mod, "LINE_W", "px"), "drop_row": tok(drop, "LINE_W", "px"),
            "answer": tok(choice, "LINE_W", "px"), "tab_box": tok(look, "BOX_THICK", "px"),
            "plate_brackets": tok(style, "BRACKET_W", "px"),
            "chip_hairline": lit(1.0, "indigoshell/agent/ui/chip.py:Chip.paint", "px"),
            "tray_menu_border": lit(1.5, "indigoshell/widgets/systray.py:_menu_box", "px"),
            "card_stripe": tok(hud, "STRIPE", "px", "follows the bottom-left cut"),
            "rule": lit(1, "indigoshell/widgets/layout.py:Divider", "px"),
            "inset_rule": "strokes sit inside their shape: inset by half the width "
                          "(shapes.beveled(inset=)); the call adds 0.5 px (style.frame_inset)",
        },
        "brackets": {
            "cluster": {"arm": lit(6, "indigoshell/widgets/layout.py:Brackets", "px"),
                        "thick": lit(1, "indigoshell/widgets/layout.py:Brackets", "px"),
                        "color": "CYAN_DIM"},
            "systag": {"arm": lit(4.0, "indigoshell/widgets/systag.py:Systag", "px"),
                       "thick": lit(1.0, "indigoshell/widgets/systag.py:Systag", "px"),
                       "color": "MAGENTA_DIM"},
            "tab_number": {"arm": tok(look, "BRACKET_ARM", "px"),
                           "spot": tok(look, "NUMBER_SPOT", "px"), "color": "CYAN_MID"},
            "call_plate": {"inset": tok(style, "BRACKET_INSET", "px"),
                           "arm": tok(style, "BRACKET_ARM", "px"),
                           "width": tok(style, "BRACKET_W", "px"),
                           "alpha": tok(style, "BRACKET_ALPHA"),
                           "note": "an L on a square corner; on a 45-degree corner an arm, "
                                   "the cut, an arm (half arm each side)"},
            "agent_box": {"inset": tok(status, "BOX_MARK_D", "px"),
                          "arm": tok(status, "BOX_MARK_ARM", "px")},
        },
        "segments": {
            "gauge": {"tick": lit(2, "indigoshell/widgets/meters.py:StatMeter(line_tick)", "px"),
                      "gap": lit(2, "indigoshell/widgets/meters.py:StatMeter(line_gap)", "px"),
                      "thick": lit(3, "indigoshell/widgets/meters.py:StatMeter(line_thickness)",
                                   "px"),
                      "width": lit(70, "indigoshell/widgets/meters.py:StatMeter(line_width)", "px")},
            "panel_bar": {"tick": lit(4, "indigoshell/widgets/meters.py:BarMeter", "px"),
                          "gap": lit(2, "indigoshell/widgets/meters.py:BarMeter", "px"),
                          "thick": lit(8, "indigoshell/widgets/meters.py:BarMeter", "px"),
                          "dim_alpha": lit(0.6, "indigoshell/widgets/meters.py:BarMeter.paint")},
            "toast_meter": {k: tok(th, k, "px" if k != "NOTIF_METER_SEGMENTS" else None)
                            for k in ("NOTIF_METER_SEGMENTS", "NOTIF_METER_GAP",
                                      "NOTIF_METER_THICK", "NOTIF_METER_INSET_Y")},
            "battery": {"cell": lit(2, "indigoshell/config_default.py:clock (BatteryMeter)", "px"),
                        "gap": lit(2, "indigoshell/config_default.py:clock (BatteryMeter)", "px"),
                        "height": lit(4, "indigoshell/config_default.py:clock (BatteryMeter)",
                                      "px")},
            "call_job": {"cell": tok(bars, "CELL_W", "px"), "gap": tok(bars, "CELL_GAP", "px"),
                         "height": tok(bars, "CELL_H", "px")},
            "volume": {"cells": lit(8, "indigoshell/widgets/volume.py:Volume", None),
                       "cell": lit(3, "indigoshell/widgets/volume.py:Volume", "px"),
                       "gap": lit(1, "indigoshell/widgets/volume.py:Volume", "px"),
                       "width": lit(18, "indigoshell/widgets/volume.py:Volume", "px"),
                       "cap_line": lit(1, "indigoshell/widgets/volume.py:Volume.paint", "px",
                                       "YELLOW_BRIGHT (config), one cell-row above the level, "
                                       "2 px overhang each side")},
            "workspaces": {"bars": tok(ws_mod, "BARS"), "gap_ratio": tok(ws_mod, "GAP_RATIO"),
                           "cell": lit(22, "indigoshell/widgets/workspaces.py:Workspaces(size)",
                                       "px"),
                           "bar_width": lit("0.75 x cell", "indigoshell/widgets/workspaces.py:"
                                            "_paint_bars")},
            "systray": {"lit": lit("CYAN_BRIGHT", "indigoshell/widgets/systray.py:Systray.paint",
                                   note="one cell per registered app, over MAGENTA_DIM; info, "
                                        "not an error"),
                        "bars": lit(5, "indigoshell/widgets/systray.py:Systray", None),
                        "gap": lit(2, "indigoshell/widgets/systray.py:Systray", "px"),
                        "width": lit(7, "indigoshell/config_default.py:clock_cluster", "px")},
            "rule": "cells are whole pixels: fixed tick width, the count derived from the "
                    "span (meters._ticks)",
        },
        "dots": {
            "light": tok(shapes, "LIGHT", "px",
                         "every dot is this square: the network's lights, the firewall state, "
                         "radio items, the call's device/session marks and list bullets, a "
                         "line graph's newest sample (shapes.light / text_light); filled = "
                         "on or current, a 1 px outline = off; beside text it sits centred "
                         "in the cell, half the cap height above the baseline"),
            "size": lit(5, "indigoshell/widgets/network.py:Network(dot_size)", "px"),
            "gap_to_text": lit(6, "indigoshell/widgets/network.py:Network(dot_gap)", "px"),
            "stack_gap": lit(3, "indigoshell/widgets/network.py:Network(dot_stack_gap)", "px"),
            "shape": lit("square", "indigoshell/widgets/network.py:Network(dot_shape)",
                         note="squares, never circles (the user's rule)"),
        },
        "chip": {
            "size": tok(style, "CHIP", "px"), "size_small": tok(style, "CHIP_SM", "px"),
            "hairline_inset": tok(style, "CHIP_INSET", "px"),
            "hairline_alpha": tok(style, "CHIP_INSET_ALPHA"),
            "arc_sweep": lit(64.0, "indigoshell/agent/ui/chip.py:Chip.paint", "deg",
                             "two arcs, 180 degrees apart"),
            "arc_radius": lit("0.38 x size (>= 56 px) else 0.41", "indigoshell/agent/ui/chip.py:"
                              "Chip.paint"),
            "arc_width": lit("max(2, 0.05 x size)", "indigoshell/agent/ui/chip.py:Chip.paint"),
            "label": lit("max(8, round(0.17 x size)) bold, tracking 0.6 at >= 56 px",
                         "indigoshell/agent/ui/chip.py:Chip.paint"),
            "bar_chip": {"size": tok(status, "BAR_H", "px"),
                         "arc_r": tok(status, "CHIP_ARC_R", "px"),
                         "arc_w": tok(status, "CHIP_ARC_W", "px")},
        },
        "sizes": {
            "call_plate_w": tok(style, "PLATE_W", "px"), "status_plate_w": tok(style, "STATUS_W", "px"),
            "toast_w": tok(th, "NOTIF_WIDTH", "px"),
            "tab_slot_w": tok(look, "SLOT_W", "px"), "tab_slot_min": tok(look, "SLOT_MIN", "px"),
            "tab_box_h": tok(look, "BOX_H", "px"),
            "agent_box_w": tok(status, "BOX_W", "px"),
            "menu_row_h": tok(menu_mod, "ROW_H", "px"), "menu_min_w": tok(menu_mod, "MIN_WIDTH", "px"),
            "row_h": tok(drop, "ROW_H", "px"), "row_icon": tok(drop, "ICON", "px"),
            "drop_page": tok(drop, "PAGE", None, "rows shown at once"),
            "scroll_thumb_w": tok(drop, "SCROLL_W", "px"),
            "answer_h": tok(choice, "CHOICE_H", "px"), "answer_pad_x": tok(choice, "PAD_X", "px"),
            "launcher": lit({"width": 760, "rows": 12}, "indigoshell/config_default.py:WINDOWS "
                            "(Launcher)"),
            "panels": lit({"system": [600, 580], "hardware": [600, 520], "network": [600, 470]},
                          "indigoshell/config_default.py:WINDOWS", "px"),
            "caret": {"width": tok(text_input, "CARET_W", "px"),
                      "pad_y": tok(text_input, "CARET_PAD_Y", "px")},
            "tab_strip": {k: tok(tabs_mod, k, "px") for k in ("TAB_HEIGHT", "TAB_PAD_X",
                                                               "TAB_GAP", "UNDER_THICK")},
        },
        "placement": {
            "call": lit({"anchor": "top-right", "top": th.TOP_BAR_HEIGHT + 16,
                         "right": th.BAR_MARGIN + 12},
                        "indigoshell/config_default.py:call", "px",
                        "16 px under the top bar (TOP_BAR_HEIGHT + 16), never over it"),
            "panels": lit({"anchor": "bottom-left (system) / bottom-right", "side": th.BAR_MARGIN,
                           "bottom": th.BAR_HEIGHT + th.POPUP_GAP},
                          "indigoshell/config_default.py:WINDOWS", "px"),
            "menu": lit({"anchor": "bottom-right", "right": th.BAR_MARGIN + 8,
                         "bottom": th.BAR_HEIGHT + th.BAR_MARGIN + 6},
                        "indigoshell/config_default.py:WINDOWS (MENU_WINDOW)", "px"),
            "toasts": lit({"anchor": "bottom-right", "right": th.NOTIF_OFFSET_X,
                           "bottom": th.NOTIF_OFFSET_Y, "gap": th.NOTIF_GAP,
                           "order": "newest nearest the bar"},
                          "indigoshell/theme.py:NOTIF_OFFSET_*", "px"),
            "launcher": lit({"anchor": "centred"}, "indigoshell/config_default.py:WINDOWS"),
            "drop_down": lit({"anchor": "flush under the top bar, on BAR_BG, no edge"},
                             "indigoshell/applink/drop.py"),
        },
    }

    spin = {k: v for k, v in style.SPIN.items()}
    motion = {
        "rule": "idle is still: nothing animates without an event; motion marks events "
                "(music playing, traffic, the agent working, a ring) and alarms",
        "durations": {
            "panel_spawn": lit(cfg.PANEL_SPAWN.duration, "indigoshell/config_default.py:"
                               "PANEL_SPAWN", "s", "despawn plays it backwards"),
            "lyric_reveal": lit(cfg.lyrics.reveal_effect.duration,
                                "indigoshell/config_default.py:lyrics (GlitchWipe)", "s"),
            "reply_chunk_reveal": tok(reveal, "HOLD", "s"),
            "drop_unroll": tok(drop, "UNROLL_S", "s"),
            "tab_switch": tok(look, "SWITCH_S", "s"), "tab_slide": tok(look, "SLIDE_S", "s"),
            "tab_close_wipe": tok(look, "WIPE_S", "s", "then the rest slide SLIDE_S"),
            "tab_load_comet_lap": tok(look, "COMET_S", "s"),
            "turn_trace_lap": tok(frame, "TRACE_LAP_S", "s"),
            "turn_trace_finish": tok(frame, "TRACE_FINISH_S", "s"),
            "send_comet": tok(frame, "COMET_S", "s"),
            "send_comet_tail": tok(frame, "COMET_TAIL_PX", "px"),
            "trace_head": tok(frame, "TRACE_HEAD_PX", "px", "YELLOW_BRIGHT, 3 px glow"),
            "chip_kick": tok(style, "KICK_S", "s", "a half-turn on send"),
            "answer_nudge": tok(style, "NUDGE_S", "s", "two blinks"),
            "ring_then_missed": tok(style, "RING_FOR_S", "s"),
            "hangup_close": tok(style, "HANGUP_CLOSE_S", "s"),
            "copied_tag": tok(style, "COPIED_S", "s"),
            "menu_pulse": tok(menu_mod, "PULSE_S", "s", "armed row alternates cyan / yellow"),
            "menu_reject": tok(menu_mod, "REJECT_S", "s"),
            "menu_reject_toggles": tok(menu_mod, "REJECT_TOGGLES"),
            "caret_blink_half": tok(text_input, "BLINK_S", "s"),
            "caret_blink_for": tok(text_input, "BLINK_FOR_S", "s", "then solid (the call: forever)"),
            "battery_event": tok(meters.BatteryMeter, "EVENT_S", "s",
                                 "a state change plays its motion this long, then still"),
            "battery_poll": tok(meters.BatteryMeter, "POLL_S", "s"),
            "battery_low_blink": lit(150, "indigoshell/widgets/meters.py:"
                                     "BatteryMeter._state_config", "ms", "< 20%: an alarm, "
                                     "keeps going"),
            "battery_motion_steps": lit({"charging_sweep": 70, "full_shimmer": 33,
                                         "under_half_sweep": 90, "over_half_sweep": 110},
                                        "indigoshell/widgets/meters.py:"
                                        "BatteryMeter._state_config", "ms",
                                        "only for EVENT_S after a change"),
            "toast_timeout_normal": tok(th, "NOTIF_TIMEOUT_NORMAL_MS", "ms",
                                        "the frame's timer trace runs round it clockwise"),
            "toast_timeout_low": tok(th, "NOTIF_TIMEOUT_LOW_MS", "ms"),
            "toast_timeout_critical": tok(th, "NOTIF_TIMEOUT_CRITICAL", "ms", "0: until clicked"),
            "media_marquee": lit(60.0, "indigoshell/widgets/media.py:Media(scroll_px_per_sec)",
                                 "px/s", "gap 80 px between copies"),
            "media_glitch_burst": lit({"every_s": [3.0, 8.0], "lasts_s": 1.0, "density": 0.25},
                                      "indigoshell/widgets/media.py:Media", None,
                                      "while playing only: characters become 0/1 in "
                                      "GLITCH_PALETTE"),
            "beat_decay": tok(media_mod, "BEAT_DECAY", None, "per 30 ms (BEAT_DECAY_MS)"),
            "lyric_beat_bold_at": tok(stdout_text, "BEAT_BOLD_AT"),
            "flux_glitch_repeat": tok(flux_fx, "REPEAT_S", "s"),
            "flux_mark_flash": tok(flux_watch, "FLASH_MS", "ms"),
        },
        "patterns": {
            "ring": tok(th, "WORKSPACE_URGENT_RING", "ms",
                        "(ms, visible) frames, looped: urgent workspace, the call's blink"),
            "chip_spin": lit(spin, "indigoshell/agent/ui/style.py:SPIN", "deg/s"),
            "chip_settle_rate": tok(style, "SETTLE_RATE", "1/s"),
            "chip_strength": {"lit": tok(status, "LIT"), "unlit": tok(status, "UNLIT"),
                              "missed": tok(status, "MISSED"), "idle": tok(status, "IDLE_DIM"),
                              "tint": tok(status, "CHIP_TINT"),
                              "flash_tint": tok(status, "CHIP_FLASH_TINT")},
            "network_blink": lit({"floor_Bps": 2048.0, "ceil_Bps": 20e6, "min_hz": 0.5,
                                  "max_hz": 4.0, "quantum_hz": 0.25, "steady_alpha": 0.45,
                                  "on_alpha": 1.0, "off_alpha": 0.25, "glow_sigma": 2.0,
                                  "glow_alpha": 0.55, "sample_s": 1.0},
                                 "indigoshell/widgets/network.py:Network", None,
                                 "square wave, log-mapped from byte rate"),
            "progress_sweep": {"step_s": tok(bars, "SWEEP_STEP_S", "s"),
                               "cells": tok(bars, "SWEEP_CELLS"),
                               "busy_cells": tok(bars, "BUSY_CELLS"),
                               "ease_per_s": tok(bars, "EASE_PER_S", "1/s")},
            "thinking_dots_per_s": tok(style, "DOTS_PER_S"),
        },
        "easing": {
            "ease_out_cubic": lit("1 - (1 - x)^3", "indigoshell/applink/kinds/look.py:Glide; "
                                  "agent/ui/frame.py:_ease_out; applink/drop.py:reveal; "
                                  "agent/ui/chip.py:kick"),
            "exponential": lit("v += (target - v)(1 - e^(-dt x rate))",
                               "indigoshell/agent/ui/bars.py:Bars.fill (rate 9/s); "
                               "chip.py settle (SETTLE_RATE)"),
            "effects_appear": lit("linear in wall-clock time (appear = elapsed / duration); "
                                  "the shaders shape it with smoothstep",
                                  "indigoshell/window.py:_advance_phase"),
        },
        "fps": {
            "scan": tok(holo, "SCAN_FPS"), "chip": tok(style, "CHIP_FPS"),
            "blink": tok(style, "BLINK_FPS"), "tabs": tok(look, "MOTION_FPS"),
            "tab_comet": tok(look, "COMET_FPS"), "reveal": tok(reveal, "REVEAL_FPS"),
            "trace": tok(frame, "TRACE_FPS"), "menu": tok(menu_mod, "CLOCK_FPS"),
            "drop_unroll": tok(drop, "UNROLL_FPS"), "progress": tok(bars, "CELL_FPS"),
            "effects": lit(60, "indigoshell/effects.py:Effect.fps"),
        },
    }

    lyric_fx = cfg.lyrics.reveal_effect
    effects = {
        "panel_spawn": effect_params(cfg.PANEL_SPAWN, "indigoshell/config_default.py:PANEL_SPAWN",
                                     "ScanLock: bands snap in out of order with a sideways tear "
                                     "and an RGB split; per-row lock = smoothstep(thr, thr + 0.18,"
                                     " appear); fade in smoothstep(0, 0.12, appear); jitter "
                                     "re-rolled 28/s; seed re-rolled per spawn. band_px/tear_px/split_px "
                                     "override rows/tear (left at their defaults)"),
        "lyric_reveal": effect_params(lyric_fx, "indigoshell/config_default.py:lyrics",
                                      "GlitchWipe: spark = CYAN_BLOOM, cursor = MAGENTA_BRIGHT "
                                      "(as 0..1 rgb)"),
        "reply_reveal": effect_params(reveal.Reveal().wipe, "indigoshell/agent/ui/reveal.py:"
                                      "Reveal.wipe", "per streamed chunk; spark CYAN_BRIGHT, "
                                      "cursor VIOLET_BRIGHT, its own bar off; a 2 px lead bar "
                                      "in her colour with a 22 px glow (BAR_GLOW_PX)"),
        "holo": {
            "raster": {k: tok(holo, k) for k in ("RASTER_PITCH", "RASTER_ALPHA", "RASTER_LIFT")},
            "raster_note": "1 px line every RASTER_PITCH px on the window grid, its colour "
                           "lerped RASTER_LIFT toward HUD_FG_BRIGHT; on holo windows "
                           "POPUP_RASTER (cyan), composited SrcATop; on the call, per plate "
                           "in its frame colour",
            "scan": {k: tok(holo, k) for k in ("SCAN_EVERY_S", "SCAN_SPEED", "SCAN_HEAD_PX",
                                                "SCAN_WAKE_PX", "SCAN_SHIFT_PX", "SCAN_SPLIT_PX",
                                                "SCAN_RATE", "SCAN_FPS")},
            "scan_note": "tears in 9 px bands and 3 px slices of 24-120 px chunks; whole-pixel "
                         "offsets; 10% of pieces drop to 25% at the band",
        },
        "decode_unused": effect_params(fx_mod.Decode(), "indigoshell/effects.py:Decode",
                                       "available, not in the shipped config"),
        "color_split_unused": effect_params(fx_mod.ColorSplit(), "indigoshell/effects.py:ColorSplit",
                                            "available, not in the shipped config"),
        "aberration_off": effect_params(fx_mod.Aberration(), "indigoshell/effects.py:Aberration",
                                        "the always-on half of the braindance shader; off"),
        "scramble_unused": {"params": {
            "interval_ms": 35, "frames_per_char": 2, "scramble_window": 4,
            "charset": text_effects.Scramble.DEFAULT_CHARSET,
            "palette": list(text_effects.Scramble.DEFAULT_PALETTE)},
            "class": "Scramble", "source": "indigoshell/services/text_effects.py:Scramble",
            "note": "a character-swap reveal; available, not in the shipped config"},
        "media_glitch_palette": tok(media_mod, "GLITCH_PALETTE"),
        "flux_page": {k: tok(flux_fx, k) for k in ("PLATE_TINT", "RASTER", "BACKGROUND",
                                                    "SPAWN_S", "BAND_PX", "TEAR_PX", "SPLIT_PX")},
        "flux_page_tint": "her colour (core/persona.py) at PLATE_TINT",
        "flux_watch_frame": tok(flux_watch, "FRAME_PX", "px", "in her colour"),
    }

    wording = {
        "efforts": tok(remote, "EFFORT_NAMES", note="what the call calls effort levels"),
        "status_words": tok(style, "STATUS_WORDS"),
        "answers": ["GREENLIGHT", "SCRUB"],
        "mark": tok(flux_watch, "MARK"),
        "drop_tags": ["OPEN", "SEARCH", "RUN"],
        "tear_tag": tok(look, "TEAR_TAG"),
    }

    return {
        "meta": {
            "what": "indigoshell's design tokens, read from the code by "
                    "prototypes/visuals/render_design.py --tokens; see docs/design/README.md",
            "units": "px are Skia pixels at 96 DPI; points convert as pt x 96/72",
            "colors": "hex #RRGGBB; 'alpha' 0..1 where the code applies one",
            "inline": "a source ending '(inline)' is a value written in place in that "
                      "function, not a named constant",
        },
        "color": {"palette": palette, "semantic": semantic, "agent": agent_tokens,
                  "call": call_colors, "rows": rows, "top_bar": top_bar,
                  "directions": directions, "heat": heat},
        "typography": typography,
        "spacing": spacing,
        "geometry": geometry,
        "motion": motion,
        "effects": effects,
        "wording": wording,
    }


def write_tokens(path_: str) -> None:
    data = build_tokens()
    with open(path_, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  wrote {os.path.relpath(path_, REPO)}  {os.path.getsize(path_) / 1024:.0f} KB")


# ═══════════════════════════════════════════════════════════════════════
SAMPLES = {
    "palette": render_palette, "type": render_type, "shapes": render_shapes,
    "bar": render_bottom_bar, "bar_widgets": render_bar_widgets,
    "top_bar": render_top_bar, "top_states": render_top_states, "top_input": render_top_input,
    "toasts": render_toasts, "menu": render_menu, "panels": render_panels,
    "launcher": render_launcher, "call": render_call, "call_chips": render_call_chips,
    "progress": render_progress, "tray": render_tray, "flux": render_flux_standin,
    "overview": render_overview,
    "spawn": render_spawn, "lyrics_gif": render_lyrics_gif, "tabs_gif": render_tabs_gif,
    "chip_gif": render_chip_gif, "scan_gif": render_scan_gif, "menu_gif": render_menu_gif,
}


def main(argv=None) -> int:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", nargs="?", default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--only", nargs="*", metavar="NAME", help="render only these samples")
    ap.add_argument("--tokens", metavar="FILE", help="also write the tokens JSON here")
    ap.add_argument("--list", action="store_true", help="list the sample names and exit")
    args = ap.parse_args(argv)
    if args.list:
        print(" ".join(SAMPLES))
        return 0
    OUT = os.path.abspath(args.out)
    os.makedirs(OUT, exist_ok=True)
    names = args.only if args.only is not None else list(SAMPLES)
    unknown = [n for n in names if n not in SAMPLES]
    if unknown:
        ap.error(f"unknown sample(s): {', '.join(unknown)}; --list shows them")
    print(f"rendering into {OUT}")
    for name in names:
        random.seed(1)
        SAMPLES[name]()
    if args.tokens:
        write_tokens(os.path.abspath(args.tokens))
    total = sum(os.path.getsize(path(n)) for n in WRITTEN)
    print(f"{len(WRITTEN)} files, {total / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
