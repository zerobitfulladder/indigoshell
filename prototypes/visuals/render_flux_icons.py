"""Flux's tabs with their pages' icons, at 1x, from the real widgets: the
icon glowing in the tab's spot (applink/kinds/glow.py) where its number
was, and the host at the right a fixed width.

    PYTHONPATH=../.. ../../.venv/bin/python render_flux_icons.py ICONS [OUT]

ICONS is a folder of PNGs as Flux sends them (flux/qt/favicon.py: at most
32 px a side), named by site (github.png, youtube.png, ...; flux-github.png
for the one GitHub gives Flux's dark scheme, used first when there); the
repo carries none (they are the sites'). The sheet (`flux_icons.png`, or
OUT) has, for the bloom built (glow.BLOOM) and a tighter one (TIGHT):

- the states on one bar: the current tab (brackets, full glow), a tab
  under the pointer, one the agent drives (her colour), one the user took
  over from her (her colour dimmed), one with no icon yet (its letter),
  a plain one behind;
- every icon in the folder as tabs behind, the current one among them;
- the same bar after switching to a tab with a long host: the host's box
  keeps its width (the tabs' slots too: printed), the host cut with an
  ellipsis;

then a 3x crop of the first rows, to see the pixels.
"""
import os
import sys
import time

import skia

from common import txt
from indigoshell import theme
from indigoshell.applink import bar as B
from indigoshell.applink.kinds import glow, look
from indigoshell.applink.view import BarView
from indigoshell.widgets.base import Size

SECTION_W = 1500                    # the app section on a 2560 px top bar, about
H = theme.TOP_BAR_HEIGHT
LABEL_W = 330
TIGHT = glow.Bloom(1.6, 0.7)        # the one not built: a halo that all but vanishes at 1x


class Host:
    def damage(self, widget=None, *, layout: bool = False) -> None:
        pass


def load(folder: str) -> dict:
    out = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".png"):
            with open(os.path.join(folder, name), "rb") as f:
                image = glow.decode(f.read())
            if image is not None:
                out[name[:-4]] = image
    return out


def strip(images: dict, tabs: list[dict], current: int, *, host: list, hover=None):
    """One bar's app section: `tabs` (B.tab dicts) and the host's spans,
    painted on the bar's background; and its tabs item."""
    view = BarView(emit=lambda e: None, images=dict(images))
    view.attach(Host())
    view.set_bar(B.bar([B.tabs("tabs", tabs, current=tabs[current]["id"],
                               can=["close", "move", "detach"])],
                       [B.text("host", spans=host, fixed=True)]))
    s = skia.Surface(SECTION_W, H)
    c = s.getCanvas()
    c.clear(theme.color(theme.BAR_BG))
    view.measure(Size(SECTION_W, H))
    view.arrange(skia.Rect.MakeWH(SECTION_W, H))
    item = view.item("tabs")
    item.settle()
    item.hover = hover
    view.paint(c)
    return s.makeImageSnapshot(), item


def host(name: str, *, bookmarked=False, secure=True) -> list:
    return (([B.span(icon="bookmark")] if bookmarked else [])
            + ([] if secure else [B.span(icon="unlocked", color="error")]) + [B.span(name)])


def main() -> None:
    folder = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
    out = sys.argv[2] if len(sys.argv) > 2 else "flux_icons.png"
    images = load(folder)
    pick = lambda *names: [n for n in names if n in images]     # noqa: E731
    states = [
        ("flux-github" if "flux-github" in images else "github",
         "zerobitfulladder/indigoshell", None),
        ("youtube", "lofi beats to tile windows to", None),        # hovered
        ("wikipedia", "Netrunner - Wikipedia", "driving"),
        ("amazon", "Your orders", "paused"),
        (None, "news.example.org: loading", None),                # no icon: a letter
        ("google", "flux browser qt - Search", None),
    ]
    tabs = [B.tab(101 + i, title, image=name if name in images else None,
                  letter=None if name in images else "n",
                  agent=B.agent(agent) if agent else None)
            for i, (name, title, agent) in enumerate(states)]
    every = [B.tab(201 + i, f"{n} page", image=n) for i, n in enumerate(images)]
    rows = []
    built = glow.BLOOM
    for label, bloom in (("built", built), ("tighter", TIGHT)):
        glow.BLOOM = bloom
        img, _ = strip(images, tabs, 0, host=host("github.com", bookmarked=True), hover=1)
        rows.append((f"{label} bloom (sigma {bloom.sigma}, {bloom.gain:.0%})|current, hover, "
                     "Lilac, taken over,|no icon (letter), behind", img))
        cur = list(images).index("wikipedia") if "wikipedia" in images else 0
        img, _ = strip(images, every, cur, host=host("en.wikipedia.org"))
        rows.append((f"{label}: every icon in the folder|current: wikipedia", img))
    glow.BLOOM = built
    a, ta = strip(images, tabs, 0, host=host("github.com", bookmarked=True))
    b, tb = strip(images, tabs, 5, host=host("developer.mozilla.org", secure=False))
    c, tc = strip(images, tabs, 4, host=host("a-very-long-subdomain.of.some.example.org"))
    widths = {(t["slot_x"], t["slot_w"]) for t in (*ta.layout(), *tb.layout(), *tc.layout())
              if t["index"] == 0}
    print("first slot (x, width) on the three bars:", widths)
    rows.append(("fixed host: github.com|current: the first tab", a))
    rows.append(("... switched: a longer host, insecure|slots unchanged", b))
    rows.append(("... a host longer than its box|cut with an ellipsis", c))

    gap = 10
    crop_w = (LABEL_W + SECTION_W - gap) // 3
    crops = [r[1] for r in rows[:1]] + [rows[2][1]]
    w = LABEL_W + SECTION_W + gap
    h = gap + sum(H + gap for _ in rows) + 30 + sum(H * 3 + gap for _ in crops)
    s = skia.Surface(w, h)
    cv = s.getCanvas()
    cv.clear(skia.ColorSetARGB(255, 28, 28, 34))
    y = gap
    for label, img in rows:
        lines = label.split("|")
        for i, line in enumerate(lines):
            txt(cv, line, gap, y + 12 + i * 13, 12 if i == 0 else 11,
                theme.HUD_FG_BRIGHT if i == 0 else theme.HUD_FG, bold=i == 0)
        cv.drawImage(img, LABEL_W, y)
        y += H + gap
    txt(cv, "3x: the bloom built, and the tighter one", gap, y + 18, 12, theme.HUD_FG_BRIGHT,
        bold=True)
    y += 30
    nearest = skia.SamplingOptions(skia.FilterMode.kNearest)
    for img in crops:
        cv.drawImageRect(img, skia.Rect.MakeXYWH(0, 0, crop_w, H),
                         skia.Rect.MakeXYWH(gap, y, crop_w * 3, H * 3), nearest)
        y += H * 3 + gap
    s.makeImageSnapshot().save(out, skia.kPNG)
    print("wrote", out, f"{w}x{h}")


if __name__ == "__main__":
    t = time.perf_counter()
    main()
    print(f"{time.perf_counter() - t:.2f} s")
