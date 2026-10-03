"""The launcher, rendered from the real widget against this machine's apps.

Four states side by side at 1x — the scale it is seen at: an empty query
(recent apps first), a query with its matched letters lit, a selection
moved down the list, and a query that matches nothing. Every state is
reached by feeding real KeyEvents through `Launcher.key`, so the sheet
also exercises the input path.

Launch history goes to a throwaway state dir, never the real one.

    PYTHONPATH=../.. python render_launcher.py
"""
import os
import tempfile

os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="launcher-render-")

import skia                                             # noqa: E402

from common import C, txt                               # noqa: E402
from indigoshell import theme                           # noqa: E402
from indigoshell.widgets.base import KeyEvent, Size     # noqa: E402
from indigoshell.widgets.launcher import Launcher       # noqa: E402

MARGIN = 28
DOWN = 0xFF54


class FakeSpec:
    name = "launcher"


class FakeWindow:
    """What a widget asks of its host: damage, a resize, the spec."""
    spec = FakeSpec()

    def __init__(self):
        self.size = (0, 0)

    def damage(self, widget=None, *, layout=False):
        pass

    def resize_content(self, w, h):
        self.size = (w, h)


def type_into(launcher, keys):
    for k in keys:
        if isinstance(k, str):
            launcher.key(KeyEvent(keysym=ord(k), text=k))
        else:
            launcher.key(KeyEvent(keysym=k))


def snapshot(launcher, window):
    w, h = window.size
    s = skia.Surface(w + 2 * MARGIN, h + 2 * MARGIN)
    c = s.getCanvas()
    c.clear(C(theme.BASE_BLACK))
    launcher.measure(Size(w, h))
    launcher.arrange(skia.Rect.MakeXYWH(MARGIN, MARGIN, w, h))
    launcher.paint(c)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def configured():
    """A fresh launcher at the size the shipped config gives it."""
    from indigoshell.config_default import LAUNCHER, WINDOWS
    shipped = next(w for w in WINDOWS if w.name == LAUNCHER).content
    assert isinstance(shipped, Launcher)
    return Launcher(rows=shipped.rows, width=shipped._list.width)


def state(keys, history=()):
    launcher = configured()
    window = FakeWindow()
    launcher.attach(window)
    for name in history:
        app = next(a for a in launcher.catalog.apps if a.entry.name == name)
        launcher.catalog.history.record(app.entry.id)
    launcher.attach(window)             # reopen: picks the history up
    type_into(launcher, keys)
    return snapshot(launcher, window)


def main():
    used = ["Firefox", "Firefox", "Firefox", "kitty", "kitty", "Neovim"]
    shots = [
        ("empty: recent first", state([], used)),
        ("'fi': matches lit", state(list("fi"), used)),
        ("'set' + Down x2", state(list("set") + [DOWN, DOWN], used)),
        ("no match: runs it", state(list("htop -t"), used)),
    ]
    gap, top = 24, 40
    w = sum(img.width() for _, img in shots) + gap * (len(shots) + 1)
    h = max(img.height() for _, img in shots) + top + gap
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    x = gap
    for label, img in shots:
        txt(c, label, x + MARGIN, 26, 13, theme.HUD_FG_BRIGHT, bold=True)
        c.drawImage(img, x, top)
        x += img.width() + gap
    s.flushAndSubmit()
    s.makeImageSnapshot().save("launcher.png", skia.kPNG)
    print("wrote launcher.png", f"{w}x{h}")


if __name__ == "__main__":
    main()
