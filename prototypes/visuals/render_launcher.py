"""MOD+D's app search, rendered from the real widgets against this
machine's apps: the top bar's app section as its box, the drop-down's
rows under it (indigoshell/applink: catalog.py, prompt.py, drop.py).

Four states stacked at 1x, the scale it is seen at: an empty box
(recent apps first), a query with its matched letters lit, a selection
moved down the list, and a query that matches nothing (RUN it). Every
state is reached by feeding real KeyEvents through the open input's
`key`, so the sheet also exercises the input path.

Launch history goes to a throwaway state dir, never the real one.

    PYTHONPATH=../.. python render_launcher.py
"""
import os
import tempfile
import types

os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="launcher-render-")

import skia                                             # noqa: E402

from common import C, txt                               # noqa: E402
from indigoshell import theme                           # noqa: E402
from indigoshell.applink.catalog import AppSearch       # noqa: E402
from indigoshell.applink.drop import DropList           # noqa: E402
from indigoshell.applink.prompt import Prompt           # noqa: E402
from indigoshell.widgets.base import KeyEvent, Size     # noqa: E402

DOWN = 0xFF54
WIDTH = 1100                    # the app section's, at a 1600 px screen
BAR_H = theme.TOP_BAR_HEIGHT


class Host:
    """What an open input asks of the app service."""

    def changed(self, prompt):
        pass

    def closed(self, prompt):
        pass

    def next_seq(self, prompt):
        return 0

    def later(self, delay, fn):
        return types.SimpleNamespace(cancel=lambda: None)


def state(keys, history=()):
    drop = DropList()
    for name in history:
        app = next(a for a in drop.catalog.apps or drop.catalog.refresh() and drop.catalog.apps
                   if a.entry.name == name)
        drop.catalog.history.record(app.entry.id)
    source = AppSearch(drop.catalog, drop.icons)
    prompt = Prompt(Host(), None, "input", "apps", source.spec, local=source)
    prompt.start()
    for k in keys:
        prompt.key(KeyEvent(keysym=ord(k), text=k) if isinstance(k, str) else KeyEvent(keysym=k))
    drop.show(prompt, WIDTH)
    w, h = drop.relayout()
    s = skia.Surface(WIDTH, BAR_H + h)
    c = s.getCanvas()
    c.clear(C(theme.BAR_BG))
    prompt.box.measure(Size(WIDTH, BAR_H))
    prompt.box.arrange(skia.Rect.MakeWH(WIDTH, BAR_H))
    prompt.box.paint(c)
    c.translate(0, BAR_H)
    drop.arrange(skia.Rect.MakeWH(w, h))
    drop.paint(c)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def main():
    used = ["Firefox", "Firefox", "Firefox", "kitty", "kitty", "Neovim"]
    shots = [
        ("empty: recent first", state([], used)),
        ("'fi': matches lit", state(list("fi"), used)),
        ("'set' + Down x2", state(list("set") + [DOWN, DOWN], used)),
        ("no match: runs it", state(list("htop -t"), used)),
    ]
    gap, top = 24, 30
    w = max(img.width() for _, img in shots) + 2 * gap
    h = sum(img.height() + top for _, img in shots) + gap * (len(shots) + 1)
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    y = gap
    for label, img in shots:
        txt(c, label, gap, y + 18, 13, theme.HUD_FG_BRIGHT, bold=True)
        c.drawImage(img, gap, y + top)
        y += img.height() + top + gap
    s.flushAndSubmit()
    s.makeImageSnapshot().save("launcher.png", skia.kPNG)
    print("wrote launcher.png", f"{w}x{h}")


if __name__ == "__main__":
    main()
