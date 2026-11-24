"""Direction F, "rule tabs": Flux's tabs on the top bar as a rule along
the bar's bottom edge that rises around the current tab.

F1 "minimal, ours": the bottom bar's glowing magenta rule, mirrored along
the top bar's bottom edge; only the current tab rises out of it (up its
left edge, across its top with the shell's chamfer, down its right edge,
on along the bar). The other tabs stay plain text. The number sits in a
slot that gets corner brackets only on the current tab (and on a tab the
agent holds, in its voice's colour). Loading runs along the rule.

F2 "closer to the reference": a thin cyan rule, every tab a chamfered
label box with a left accent bar and a corner-bracketed number square,
the current one joined to the rule with faint hatching under its box.

A canvas sketch: the host and the agent chip are stand-ins.

    PYTHONPATH=$PWD:$PWD/prototypes/visuals python -B \
        prototypes/visuals/flux_tabs/f_rule.py OUTDIR    (or FLUX_TABS_OUT)
"""
import os
import sys

import skia

from common import C, brackets, sheet
from indigoshell import shapes, text as T, theme

OUT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FLUX_TABS_OUT")
if not OUT:
    sys.exit("usage: f_rule.py OUTDIR (or set FLUX_TABS_OUT)")

W, H = 2560, theme.TOP_BAR_HEIGHT
SIZE = theme.FONT_SIZE
NUM = 13                                    # tab numbers: small, bold
SMALL = 11                                  # small caps labels
CY = 22                                     # the tabs' text centre line
CHIP_W = 360 + 6 + 32                       # the agent chip's box + gap + square
CHIP_X = W - theme.BAR_MARGIN - CHIP_W
CTX_L = theme.BAR_MARGIN
CTX_R = CHIP_X - theme.BAR_SPACING
VOICES = {"lilac": "#c8a2c8", "indigo": "#b967ff"}
PAUSED_DIM = 0.35
SEARCH = ""                           # Nerd Font: fa-search
BOOKMARK = ""                         # Nerd Font: fa-bookmark


def agent_color(agent):
    """As browser/context.agent_color: the voice's colour driving, dimmed
    a third of the way to the bar's background while taken over."""
    if not agent:
        return None
    voice, state = agent
    c = VOICES[voice]
    return c if state == "driving" else theme.lerp(c, theme.BAR_BG, PAUSED_DIM)


def paint(color, alpha=None, stroke=None, aa=True, blur=None):
    p = skia.Paint(AntiAlias=aa)
    p.setColor(C(color, alpha))
    if stroke is not None:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(stroke)
        p.setStrokeJoin(skia.Paint.kMiter_Join)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    return p


def base(size, bold=False, cy=CY):
    m = T.font(size, bold).getMetrics()
    return cy - (m.fAscent + m.fDescent) / 2


def draw_text(c, s, x, size, color, alpha=None, bold=False, cy=CY):
    T.draw(c, s, x, base(size, bold, cy), size, C(color, alpha), bold)


def centred(c, s, rect, size, color, alpha=None, bold=True):
    w = T.measure(s, size, bold)
    draw_text(c, s, rect.centerX() - w / 2, size, color, alpha, bold, rect.centerY())


def cursor(c, x, y):
    p = skia.Path()
    for i, (dx, dy) in enumerate(((0, 0), (0, 16), (4, 12), (7, 18), (9, 17), (6, 11), (11, 11))):
        (p.moveTo if i == 0 else p.lineTo)(x + dx, y + dy)
    p.close()
    c.drawPath(p, paint("#ffffff"))
    c.drawPath(p, paint("#000000", stroke=1))


# ── layout: water-filled titles, a fixed chrome per tab ─────────────────
class Cell:
    def __init__(self, i, x, w, title):
        self.i, self.x, self.w, self.title = i, x, w, title

    @property
    def right(self):
        return self.x + self.w


def fit(style, titles, order, x0, x1):
    """Cells for `order` (tab indices; None = an empty slot of the dragged
    tab's width) from x0, titles shortened to one cap so all fit in x1."""
    n = len(order)
    nat = [T.measure(titles[i], SIZE) for i in order]
    room = (x1 - x0) - n * style.CHROME - (n - 1) * style.GAP
    cap = max(nat)
    if sum(nat) > room:
        floor, lo, hi = T.measure("…", SIZE), 0.0, max(nat)
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if sum(min(w, max(floor, mid)) for w in nat) <= room else (lo, mid)
        cap = max(floor, lo)
    cells, x = [], x0
    for i, w in zip(order, nat):
        t = titles[i] if w <= cap else T.ellipsize(titles[i], SIZE, cap + 0.5)
        cw = style.CHROME + T.measure(t, SIZE)
        cells.append(Cell(i, round(x), round(cw), t))
        x += cw + style.GAP
    return cells


# ── F1: the bar's own rule, the current tab rising out of it ────────────
class F1:
    tag = "F1"
    PAD, SLOT, SLOT_GAP, GAP = 12, 20, 6, 2
    CHROME = 2 * PAD + SLOT + SLOT_GAP
    RULE_Y = H - 1          # stroke centre: the 2px rule on rows 40-41
    TOP = 5                 # stroke centre of the raised top: rows 4-5
    BEVEL = 8               # the agent chip's bevel, top-right
    THICK = theme.BAR_RULE_THICK
    RULE = theme.BAR_RULE
    BRIGHT = theme.MAGENTA_BLOOM
    GLOW_A, GLOW = 0.55, theme.BAR_RULE_GLOW    # the bottom bar's rule, mirrored
    FILL_A = 0.10           # the light rising off the rule inside the current tab

    def slot(self, cell):
        return skia.Rect.MakeXYWH(cell.x + self.PAD, CY - self.SLOT / 2, self.SLOT, self.SLOT)

    def title_x(self, cell):
        return cell.x + self.PAD + self.SLOT + self.SLOT_GAP

    # the rule
    def raised(self, l, r, closed=False, lift=0):
        """The three sides of a tab rising out of the rule at x l..r (or,
        closed, the whole tab lifted `lift` px off it)."""
        t, b, y = self.TOP, self.BEVEL, self.RULE_Y - lift
        l, r = l + self.THICK / 2, r - self.THICK / 2
        p = skia.Path()
        p.moveTo(l, y)
        p.lineTo(l, t)
        p.lineTo(r - b, t)
        p.lineTo(r, t + b)
        p.lineTo(r, y)
        if closed:
            p.close()
        return p

    def rule(self, c, raise_=None, gap=None, ticks=None):
        p = skia.Path()
        if gap:                                 # torn open where a tab left
            p.moveTo(0, self.RULE_Y)
            p.lineTo(gap[0], self.RULE_Y)
            p.moveTo(gap[1], self.RULE_Y)
        else:
            p.moveTo(0, self.RULE_Y)
        if raise_:
            l, r = raise_
            p.lineTo(l + self.THICK / 2, self.RULE_Y)
            p.addPath(self.raised(l, r))
            p.moveTo(r - self.THICK / 2, self.RULE_Y)
        p.lineTo(W, self.RULE_Y)
        if raise_ and self.FILL_A:              # the light rising off the rule, inside
            l, r = raise_
            g = skia.Paint(Shader=skia.GradientShader.MakeLinear(
                [(0, self.TOP), (0, self.RULE_Y)],
                [C(self.RULE, 0.0), C(self.RULE, self.FILL_A)]))
            c.drawPath(shapes.beveled(l, self.TOP, r - l, self.RULE_Y - self.TOP,
                                      bevel=self.BEVEL, corners=("top-right",)), g)
        c.drawPath(p, paint(self.RULE, self.GLOW_A, stroke=self.THICK, blur=self.GLOW))
        c.drawPath(p, paint(self.RULE, stroke=self.THICK))
        if gap:                                 # the torn ends fade out
            for x0, x1, a0, a1 in ((gap[0] - 24, gap[0], 0, 1), (gap[1], gap[1] + 24, 1, 0)):
                g = skia.Paint(Shader=skia.GradientShader.MakeLinear(
                    [(x0, 0), (x1, 0)], [C(theme.BAR_BG, a0), C(theme.BAR_BG, a1)]))
                c.drawRect(skia.Rect.MakeLTRB(x0, self.RULE_Y - 8, x1, H), g)
        for x in ticks or ():                   # where a dragged tab lands
            c.drawRect(skia.Rect.MakeXYWH(x - 1, self.RULE_Y - 7, 2, 7), paint(self.RULE))

    def run(self, c, x0, x1):
        """A brighter run along the rule: a tab loading."""
        r = skia.Rect.MakeLTRB(x0, self.RULE_Y - 1, x1, self.RULE_Y + 1)
        c.drawRect(r.makeOutset(0, 1), paint(self.BRIGHT, 1.0, blur=5))
        tail = skia.Paint(Shader=skia.GradientShader.MakeLinear(    # a comet, moving right
            [(x0, 0), (x1, 0)], [C(self.BRIGHT, 0.0), C(self.BRIGHT, 1.0)]))
        c.drawRect(r, tail)
        c.drawRect(skia.Rect.MakeLTRB(x1 - 10, r.top(), x1, r.bottom()), paint("#ffffff", 0.8))

    def trace(self, c, l, r, frac):
        """The current tab loading: the raised outline lit from its left
        foot, `frac` of the way round."""
        m = skia.PathMeasure(self.raised(l, r), False)
        seg, rest = skia.Path(), skia.Path()
        m.getSegment(0, m.getLength() * frac, seg, True)
        m.getSegment(m.getLength() * frac, m.getLength(), rest, True)
        c.drawPath(rest, paint(theme.BAR_BG, 0.6, stroke=self.THICK + 1))   # not yet: dim
        c.drawPath(seg, paint(self.BRIGHT, 1.0, stroke=self.THICK + 1, blur=4))
        c.drawPath(seg, paint(self.BRIGHT, stroke=self.THICK))

    def ghost(self, c, cell):
        """Hover: the shape the tab would rise into, faint, no glow."""
        p = self.raised(cell.x, cell.right)
        c.drawPath(p, paint(self.RULE, 0.35, stroke=1))

    # a tab
    def cell(self, c, cell, current=False, hover=False, agent=None):
        s = self.slot(cell)
        mark = agent_color(agent)
        if current or mark:
            brackets(c, s.left(), s.top(), s.width(), s.height(),
                     mark or theme.CYAN_MID, arm=4)
        num = mark or (theme.CYAN_BRIGHT if current else theme.BASE_MUTED)
        centred(c, str(cell.i + 1), s, NUM, num)
        fg = theme.FG_STRONG if current else theme.FG if hover else theme.BASE_MUTED
        draw_text(c, cell.title, self.title_x(cell), SIZE, fg)

    def floating(self, c, cell, dx, dy, lift):
        """A tab picked up: closed, off the rule, over what it passes."""
        c.save()
        c.translate(dx, dy)
        p = self.raised(cell.x, cell.right, closed=True, lift=lift)
        c.drawPath(p, paint(theme.BAR_BG, 0.92))
        c.drawPath(p, paint(self.RULE, 0.45, stroke=self.THICK, blur=4))
        c.drawPath(p, paint(self.RULE, stroke=self.THICK))
        c.translate(0, -lift / 2)
        self.cell(c, cell, current=True)
        c.restore()

    def find(self, c, x0, query, count):
        """The find line as one raised tab: the search mark in the
        bracketed slot, the query with its caret, the count small."""
        qx = x0 + self.PAD + self.SLOT + self.SLOT_GAP
        qw = T.measure(query + "▏", SIZE)
        cx = qx + qw + 12
        r = round(cx + T.measure(count, NUM, True) + self.PAD)
        self.rule(c, raise_=(x0, r))
        s = skia.Rect.MakeXYWH(x0 + self.PAD, CY - self.SLOT / 2, self.SLOT, self.SLOT)
        brackets(c, s.left(), s.top(), s.width(), s.height(), theme.CYAN_MID, arm=4)
        centred(c, SEARCH, s, NUM, theme.CYAN_BRIGHT, bold=False)
        draw_text(c, query, qx, SIZE, theme.FG_STRONG)
        draw_text(c, "▏", qx + T.measure(query, SIZE), SIZE, theme.FG_STRONG)
        draw_text(c, count, cx, NUM, theme.CYAN_BRIGHT, bold=True)


# ── F2: closer to the reference ─────────────────────────────────────────
class F2(F1):
    tag = "F2"
    PAD, SLOT, SLOT_GAP, GAP = 6, 22, 6, 4
    ACC, BOX_PL, BOX_PR, CHAMFER = 2, 8, 12, 6
    CHROME = 2 * PAD + SLOT + SLOT_GAP + ACC + BOX_PL + BOX_PR
    RULE_Y = H - 0.5        # 1px rule on row 41
    TOP = 4.5
    BEVEL = 6
    THICK = 1
    BOX_Y, BOX_H = 10, 24
    RULE = theme.CYAN_MID
    BRIGHT = theme.CYAN_BLOOM
    GLOW_A, GLOW = 0.35, 3.0
    FILL_A = 0.0

    def box(self, cell):
        x = cell.x + self.PAD + self.SLOT + self.SLOT_GAP
        return skia.Rect.MakeLTRB(x, self.BOX_Y, cell.right - self.PAD, self.BOX_Y + self.BOX_H)

    def title_x(self, cell):
        return self.box(cell).left() + self.ACC + self.BOX_PL

    def cell(self, c, cell, current=False, hover=False, agent=None):
        s = self.slot(cell)
        mark = agent_color(agent)
        brackets(c, s.left(), s.top(), s.width(), s.height(),
                 mark or (theme.CYAN_MID if current else theme.CYAN_DIM), arm=4)
        centred(c, str(cell.i + 1), s, NUM,
                mark or (theme.CYAN_BRIGHT if current else theme.BASE_MUTED))
        b = self.box(cell)
        path = shapes.beveled(b.left(), b.top(), b.width(), b.height(), bevel=self.CHAMFER,
                              corners=("bottom-right",))
        c.drawPath(path, paint(theme.HUD_PLATE, 0.95) if current
                   else paint(theme.HUD_SECTION, 0.6))
        edge = shapes.beveled(b.left(), b.top(), b.width(), b.height(), bevel=self.CHAMFER,
                              corners=("bottom-right",), inset=0.5)
        c.drawPath(edge, paint(theme.CYAN_MID, 0.55, stroke=1) if current or hover
                   else paint(theme.CYAN_DIM, 0.8, stroke=1))
        acc = mark or (theme.CYAN_BRIGHT if current else
                       theme.CYAN_MID if hover else theme.CYAN_DIM)
        c.drawRect(skia.Rect.MakeXYWH(b.left(), b.top(), self.ACC, b.height()),
                   paint(acc, aa=False))
        if current:                             # faint hatch under the box
            hp = paint(theme.CYAN_MID, 0.35, stroke=1)
            y0, y1 = b.bottom() + 2, self.RULE_Y - 2
            x = b.right() - self.CHAMFER - 4
            for _ in range(7):
                c.drawLine(x, y1, x + (y1 - y0), y0, hp)
                x -= 5
        fg = theme.FG_STRONG if current else theme.FG if hover else theme.BASE_MUTED
        draw_text(c, cell.title, self.title_x(cell), SIZE, fg)

    def ghost(self, c, cell):
        pass                                    # F2's hover is the box's own brighter edge

    def find(self, c, x0, query, count):
        qw = T.measure(query + "▏", SIZE)
        cw = T.measure(count, NUM, True)
        w = round(self.CHROME + qw + 14 + cw)
        cell = Cell(0, x0, w, "")
        self.rule(c, raise_=(x0, cell.right))
        self.cell(c, cell, current=True)
        s = self.slot(cell)                     # the search mark over the number
        c.drawRect(s.makeInset(3, 3), paint(theme.BAR_BG))
        centred(c, SEARCH, s, NUM, theme.CYAN_BRIGHT, bold=False)
        qx = self.title_x(cell)
        draw_text(c, query + "▏", qx, SIZE, theme.FG_STRONG)
        draw_text(c, count, self.box(cell).right() - self.BOX_PR - cw + 4, NUM,
                  theme.CYAN_BRIGHT, bold=True)


# ── the bar around the tabs: host and agent chip stand-ins ──────────────
def host(c, name, bookmarked=False):
    """The current page's host, plain, at the right of the context area
    (a stand-in for browser/context.Host). Its left x."""
    label = (BOOKMARK + " " if bookmarked else "") + name
    x = CTX_R - T.measure(label, SIZE)
    draw_text(c, label, x, SIZE, theme.FG, cy=H / 2)
    return x


def agent_chip(c):
    """A stand-in for the idle AgentChip: its box and square, dimmed."""
    k = 0.4
    y = (H - 32) / 2
    for x, w in ((CHIP_X, 360), (CHIP_X + 366, 32)):
        p = shapes.beveled(x, y, w, 32, bevel=8, corners=("top-right", "bottom-left"))
        c.drawPath(p, paint("#170620", 0.88 * k))
        c.drawPath(shapes.beveled(x, y, w, 32, bevel=8, corners=("top-right", "bottom-left"),
                                  inset=1), paint(theme.VIOLET_BRIGHT, k, stroke=2))
    T.draw(c, "LILAC", CHIP_X + 10, base(14, True, H / 2), 14,
           C(theme.VIOLET_BRIGHT, k), True)


# ── scenarios ───────────────────────────────────────────────────────────
S1_TITLES = ["DuckDuckGo — Privacy, simplified.",
             "skia-python — Python binding to Skia Graphics Library",
             "Xorg - ArchWiki"]
S2_TITLES = ["Skia API Reference — SkCanvas", "YouTube", "Inbox (3) — mail",
             "Re: picom vsync tearing on NVIDIA — Arch Linux Forums",
             "XCB — The X protocol C-language Binding", "asyncio — Python 3.13 docs",
             "Synthwave mix 2026 — retro drive — YouTube", "Qt WebEngine Overview",
             "dbus-fast · PyPI", "EWMH — freedesktop.org", "Wikipedia — Cyberpunk 2077"]
S3_TITLES = ["Hacker News", "Flights Berlin → Lisbon — compare fares",
             "Calendar — week 48", "Checkout — order summary", "Weather — Berlin"]
S5_TITLES = ["Hacker News", "skia-python — Python binding to Skia Graphics Library",
             "Xorg - ArchWiki", "Inbox (3) — mail", "Weather — Berlin"]


def strip(style, *, titles=(), current=0, host_name="", bookmarked=False, hover=None,
          agents=None, loading=(), progress=None, drag=None, tear=False, find=None,
          height=H):
    s = skia.Surface(W, height)
    c = s.getCanvas()
    c.clear(C(theme.BAR_BG))
    if height > H:                              # the browser's page under the bar
        c.drawRect(skia.Rect.MakeLTRB(0, H, W, height), paint("#14131b"))
        for i, (x, w) in enumerate(((40, 520), (40, 760), (40, 640), (900, 380))):
            c.drawRect(skia.Rect.MakeXYWH(x, H + 18 + i * 16, w, 6), paint("#ffffff", 0.05))
    agent_chip(c)
    hx = host(c, host_name, bookmarked) if host_name else CTX_R
    x1 = hx - theme.SPACING_MD
    if find:
        style.find(c, CTX_L, *find)
        s.flushAndSubmit()
        return s.makeImageSnapshot()
    agents = agents or {}
    n = len(titles)
    if drag is not None:                        # (tab, slot before which it drops)
        tab, before = drag
        order = [i for i in range(n) if i != tab]
        order.insert(order.index(before) if before is not None else len(order), tab)
        cells = fit(style, titles, order, CTX_L, x1)
        slot = next(cl for cl in cells if cl.i == tab)
        style.rule(c, ticks=(slot.x + 1, slot.right - 1))
        for cl in cells:
            if cl.i != tab:
                style.cell(c, cl, agent=agents.get(cl.i))
        style.floating(c, slot, 14, 0, 4)
        cursor(c, slot.x + 14 + 60, 18)
    elif tear:                                  # the tab dragged 60px under the bar
        cells = fit(style, titles, [i for i in range(n) if i != current], CTX_L, x1)
        gone = fit(style, titles, list(range(n)), CTX_L, x1)[current]
        px, py = 980, H + 60
        fx = px - 60 - gone.x
        style.rule(c, gap=(gone.x + fx, gone.right + fx))
        for cl in cells:
            style.cell(c, cl, agent=agents.get(cl.i))
        style.floating(c, gone, fx, py - CY - 2, 0)
        tag = "NEW WINDOW"
        T.draw(c, tag, gone.right + fx + 10, base(SMALL, True, py), SMALL,
               C(theme.CYAN_BRIGHT), True)
        cursor(c, px, py)
    else:
        cells = fit(style, titles, list(range(n)), CTX_L, x1)
        cur = cells[current]
        style.rule(c, raise_=(cur.x, cur.right))
        if hover is not None and hover != current:
            style.ghost(c, cells[hover])
        for i in loading:
            cl = cells[i]
            a = cl.x + cl.w * 0.35
            style.run(c, a, a + cl.w * 0.35)
        if progress is not None:
            style.trace(c, cur.x, cur.right, progress)
        for cl in cells:
            style.cell(c, cl, current=cl.i == current, hover=cl.i == hover,
                       agent=agents.get(cl.i))
        if hover is not None:
            h = cells[hover]
            cursor(c, style.title_x(h) + 40, 16)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


SCENARIOS = [
    ("S1-few", "S1 few|3 tabs, current 2", dict(titles=S1_TITLES, current=1, host_name="github.com")),
    ("S2-many", "S2 many|11 tabs, current 7, 9 loading",
     dict(titles=S2_TITLES, current=6, loading=(8,), host_name="www.youtube.com",
          bookmarked=True)),
    ("S2b-current-loading", "S2b|current tab loading 60%",
     dict(titles=S2_TITLES, current=6, progress=0.6, host_name="www.youtube.com",
          bookmarked=True)),
    ("S3-agent", "S3 agent|Indigo drives 2, 4 taken over",
     dict(titles=S3_TITLES, current=3, host_name="shop.example.com",
          agents={1: ("indigo", "driving"), 3: ("lilac", "paused")})),
    ("S4-hover", "S4 hover|S1, pointer over 3",
     dict(titles=S1_TITLES, current=1, hover=2, host_name="github.com")),
    ("S5-drag", "S5 drag|tab 2 between 4 and 5",
     dict(titles=S5_TITLES, current=1, drag=(1, 4), host_name="github.com")),
    ("S6-tear-out", "S6 tear out|tab 2, 60px under the bar",
     dict(titles=S5_TITLES, current=1, tear=True, host_name="github.com", height=H + 92)),
    ("S7-find", "S7 find|\"shader\" 3/12",
     dict(find=("shader", "3/12"), host_name="github.com")),
]


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for style in (F1(), F2()):
        rows = []
        for slug, label, kw in SCENARIOS:
            img = strip(style, **kw)
            img.save(os.path.join(OUT, f"{style.tag}-{slug}.png"), skia.kPNG)
            rows.append((label, img))
        sheet(rows, os.path.join(OUT, f"{style.tag}-sheet.png"), scale=1, label_w=240,
              title=f"{style.tag} rule tabs, 1x")
