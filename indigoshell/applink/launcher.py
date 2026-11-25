"""MOD+D's launcher: the app search in a panel of its own, made of the
same parts as the top bar's inputs.

Opened by a keybinding (`indigoshell toggle launcher`), typed into at
once, closed by Escape, an outside click or a launch. It is a panel like
the others — the same plate, the same scan-lock spawn, kept alive between
opens, centred where rofi was — so it differs from them only in what is
inside: a query line over a fixed list of rows.

Inside it is an app's input in all but its frame: a `Prompt`
(prompt.py: the keys, the selection, the filtering) fed by the app
catalog (catalog.py: `AppSearch`, fuzzy over the installed apps, launch
history lifting the ones used), its rows painted by the drop-down's
`paint_row` (drop.py), its text edited by the same `TextInput`. Only the
frame is the launcher's own: the `//` prompt, the query, the count, a
rule, and PAGE slots of rows that never resize the window.

    type                filter (see services/apps.py for the ranking)
    Up/Down, Tab        move the selection; also Ctrl+P/N and Ctrl+K/J
    PageUp/PageDown     a page at a time
    Return, Ctrl+L      launch the selected app; with none matching, run
                        the query as a command line (the RUN row)
    Shift+Return        run the query as a command line whatever is listed
    Shift+Delete        refused with the flash (nothing here to take out)
    Escape, Ctrl+H      close

The list has a fixed number of rows, so the window never resizes while
typing: a resize would re-anchor it and change the spawn effect's bleed,
and with fixed rows a keystroke repaints the list and the count.
"""

import logging
import math
import time

import skia

from .. import text, theme
from ..widgets.base import Insets, KeyEvent, Size, Widget
from ..widgets.label import Label
from ..widgets.layout import Align, Box, Column, Divider, Row
from ..widgets.text_input import TextInput, ctrl_letter
from .catalog import AppSearch
from .drop import ROW_GAP, ROW_H, SCROLL_GAP, SCROLL_W, paint_row, paint_thumb, rows_height
from .prompt import Prompt

log = logging.getLogger(__name__)

LAUNCHER_WINDOW = "launcher"
QUERY_SIZE = theme.FONT_SIZE
META_SIZE = theme.FONT_SIZE - 4
REJECT_FPS = 25

_RETURN, _ESCAPE = 0xFF0D, 0xFF1B
_DELETE, _KP_DELETE = 0xFFFF, 0xFF9F
_UNBOUNDED = Size(10_000.0, 10_000.0)


class QueryLine(Row):
    """The launcher's query line: `//`, the text, the count. The same
    shape as the bar's input box (kinds/input.py) for the prompt that
    drives it: `field`, `show`, `set_count`, `set_stepping`."""

    def __init__(self) -> None:
        self.field = TextInput(size=QUERY_SIZE, flex=1)
        self.count: tuple[str, str] | None = None
        self.on_toggle = None
        self._count = Label(lambda: self.count[0] if self.count else "", size=META_SIZE,
                            color=theme.BASE_MUTED, align="right",
                            min_width=text.advance(META_SIZE, chars=7))
        prompt = Label("//", size=QUERY_SIZE, bold=True, color=theme.YELLOW_MID)
        super().__init__([prompt, self.field, self._count], spacing=10, align=Align.CENTER)

    def show(self, spec: dict) -> None:
        self.field.placeholder = spec.get("placeholder") or ""

    def set_count(self, count) -> None:
        if count != self.count:
            self.count = count
            self._count.invalidate()

    def set_stepping(self, stepping: bool) -> None:
        pass

    @property
    def stepping(self) -> bool:
        return False


class Slots(Widget):
    """`rows` slots stacked, the prompt's rows in them from its scroll
    position, a scroll thumb beside them. The pointer over a row selects
    it, a click picks it, the wheel moves the selection."""

    def __init__(self, launcher: "Launcher", rows: int, width: int) -> None:
        super().__init__()
        self.launcher = launcher
        self.rows = rows
        self.width = width
        self._hover: int | None = None

    def measure(self, avail: Size) -> Size:
        return Size(self.width, rows_height(self.rows))

    def slot_rect(self, k: int) -> skia.Rect:
        r = self.rect
        return skia.Rect.MakeXYWH(r.left(), r.top() + k * (ROW_H + ROW_GAP),
                                  r.width() - SCROLL_GAP - SCROLL_W, ROW_H)

    def paint(self, canvas: skia.Canvas) -> None:
        p = self.launcher.prompt
        if p is None:
            return
        clip = canvas.getLocalClipBounds()
        reject = time.monotonic() < p.reject_until
        for k in range(self.rows):
            i = p.top + k
            if i >= len(p.rows):
                break
            rect = self.slot_rect(k)
            if rect.intersects(clip):
                paint_row(canvas, rect, p.rows[i], selected=i == p.selected,
                          reject=reject and i == p.selected, icons=self.launcher.icons)
        r = self.rect
        paint_thumb(canvas, skia.Rect.MakeXYWH(r.right() - SCROLL_W, r.top(), SCROLL_W,
                                               r.height()), len(p.rows), self.rows, p.top)

    # ── the mouse ───────────────────────────────────────────────────────
    def _slot_at(self, y: float) -> int | None:
        k = int((y - self.rect.top()) // (ROW_H + ROW_GAP))
        return k if 0 <= k < self.rows else None

    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.rect.contains(x, y) else None

    def motion(self, x: float, y: float) -> None:
        p, k = self.launcher.prompt, self._slot_at(y)
        if k == self._hover or p is None:
            return
        self._hover = k
        if k is not None and p.top + k < len(p.rows):
            p.select(p.top + k)

    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if not value:
            self._hover = None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        p = self.launcher.prompt
        if p is None:
            return False
        if button in (4, 5):
            p.scroll(-1 if button == 4 else 1)
            return True
        k = self._slot_at(y)
        if button == 1 and k is not None and p.top + k < len(p.rows):
            p.click_row(p.top + k)
            return True
        return False


class Launcher(Box):
    """MOD+D's app search in a panel (the module docstring). `terminal`
    wraps the apps whose desktop entry says Terminal=true; `icon_theme`
    names the theme their icons come from."""

    def __init__(self, *, rows: int = 12, width: int = 760,
                 terminal: tuple[str, ...] = ("kitty",), icon_theme: str | None = None,
                 catalog=None, icons=None) -> None:
        from ..services.apps import Catalog
        from ..services.icons import Icons
        self.catalog = catalog or Catalog(terminal=terminal)
        self.icons = icons or Icons(icon_theme)
        self.rows = rows
        self.prompt: Prompt | None = None
        self._open = False              # attached and not dismissed: keys still count
        self.query = QueryLine()
        self._list = Slots(self, rows, width)
        super().__init__(
            Column([self.query, Divider(theme.CYAN_DIM), self._list],
                   spacing=10, align=Align.STRETCH),
            padding=Insets.all(20),
            background=theme.POPUP_BG,
            border=theme.POPUP_BORDER,
            border_width=2.0,
            bevel=theme.POPUP_BEVEL,
            bevel_corners=theme.POPUP_BEVEL_CORNERS,
        )

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        # Every open starts afresh: an empty query, a current catalog.
        # Done here rather than on close so the despawn still shows what
        # was typed.
        self.begin()
        super().attach(window)
        self._open = True
        size = self.measure(_UNBOUNDED)
        window.resize_content(math.ceil(size.width), math.ceil(size.height))

    def begin(self) -> None:
        """A fresh search: the app catalog's rows for an empty query."""
        source = AppSearch(self.catalog, self.icons)
        self.query.field.set_text("", notify=False)
        self.prompt = Prompt(self, None, "input", "apps", source.spec, local=source,
                             box=self.query)
        self.prompt.start()

    def warm(self, gpu) -> int:
        """Pay at start what the first open would: the catalog read, the
        first page's icons loaded (an SVG parsed and recorded), and its
        rows drawn once on the GPU, small and thrown away, so the shared
        context has their programs and its caches for the icons' paths
        before the first frame needs them. `gpu`: a GpuScratch
        (canvas.py). Returns how many rows were drawn."""
        source = AppSearch(self.catalog, self.icons)
        rows, _ = source.search("")
        rows = rows[:self.rows]
        if not rows:
            return 0
        width = int(self._list.width - SCROLL_GAP - SCROLL_W)
        if not gpu.make_current():
            return 0
        surface = gpu.offscreen(width, ROW_H)
        canvas = surface.getCanvas()
        rect = skia.Rect.MakeWH(width, ROW_H)
        for i, row in enumerate(rows):
            canvas.clear(skia.ColorTRANSPARENT)
            paint_row(canvas, rect, row, selected=i == 0, icons=self.icons)
            # Off the whole-pixel grid too, as a slot's rows may sit.
            canvas.save()
            canvas.translate(0.5, 0.5)
            paint_row(canvas, rect, row, selected=False, icons=self.icons)
            canvas.restore()
        gpu.finish()
        return len(rows)

    def on_dismiss(self) -> None:
        """The window is closing: keys for it no longer count. (Not on
        detach: a window still despawning is destroyed, and detaches this
        shared widget, after a reopen gave it to another window.)"""
        self._open = False
        if self.prompt is not None and self.prompt.open:
            self.prompt.close("shell")

    @property
    def animation_fps(self) -> int:
        p = self.prompt
        return REJECT_FPS if p is not None and p.reject_until > time.monotonic() - 0.05 else 0

    def animate(self, t: float) -> None:
        self._list.invalidate()

    # ── what the prompt asks of its host ───────────────────────────────
    def changed(self, prompt) -> None:
        self._list.invalidate()

    def closed(self, prompt) -> None:
        """Launched, run, or closed: the window goes."""
        if self._open:
            self._close()

    def next_seq(self, prompt) -> int:
        return 0

    def later(self, delay: float, callback):
        loop = getattr(self.window, "loop", None)
        return loop.call_later(delay, callback) if loop is not None else None

    def _close(self) -> None:
        if self.window is None:
            return
        from ..core.daemon import get_daemon
        get_daemon().close(self.window.spec.name)

    # ── input ───────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        p = self.prompt
        if p is None or not p.open:
            return False
        k, letter = ev.keysym, ctrl_letter(ev)
        if k == _ESCAPE:
            return False                # the window closes, as for any grabbing panel
        if letter == "h":
            self._close()
            return True
        if letter == "l":
            ev = KeyEvent(keysym=_RETURN, shift=ev.shift)
        elif k in (_DELETE, _KP_DELETE) and ev.shift and not (ev.ctrl or ev.alt):
            p.flash()                   # nothing here to take out
            return True
        p.key(ev)
        return True
