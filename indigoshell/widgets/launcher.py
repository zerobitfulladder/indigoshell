"""The app launcher: a query line over a fixed list of results.

Opened by a keybinding (`indigoshell toggle launcher`), typed into at
once, closed by Escape, an outside click or a launch. It is a panel like
the others — the same plate, the same scan-lock spawn, kept alive between
opens — so it differs from them only in what is inside.

    type                filter (see services/apps.py for the ranking)
    Up/Down, Tab        move the selection; also Ctrl+P/N and Ctrl+K/J
    PageUp/PageDown     a page at a time
    Return              launch the selected app
    Shift+Return        run the query itself as a command line — and
                        plain Return does that too when nothing matches
    Escape              close

The list has a fixed number of rows, so the window never resizes while
typing: a resize would re-anchor it and change the spawn effect's bleed,
and with fixed rows a keystroke repaints the list's rect and nothing else.
The rows are slots; what each one shows is read from the launcher's
results at paint time.
"""

import logging
import math
import time

import skia

from .. import shapes, text, theme
from ..services.apps import Catalog, Result
from ..services.icons import Icons
from .base import Insets, KeyEvent, Size, Widget
from .label import Label
from .layout import Align, Box, Column, Divider, Row
from .text_input import TextInput, ctrl_letter

log = logging.getLogger(__name__)

# A step under the panels' body text: a launcher is scanned rather than
# read, and the smaller face fits more rows in the same glance.
QUERY_SIZE = theme.FONT_SIZE
NAME_SIZE = theme.FONT_SIZE - 2
META_SIZE = theme.FONT_SIZE - 4
ROW_H = 40
ROW_GAP = 4
ICON = 26
PAD_X = 12
GAP = 12
SCROLL_W = 2.0
SCROLL_GAP = 8          # between the rows and the scroll thumb

# The chord menu's armed row: the same slab, tint and border, so a
# selection reads the same in both.
BEVEL = 8
CORNERS = ("top-right", "bottom-left")
LINE_W = 1.2
_SELECTED = (theme.CYAN_BRIGHT, 0.22)
_BORDER = (theme.CYAN_BRIGHT, 0.85)
_REJECT = (theme.MAGENTA_BRIGHT, 0.75)
REJECT_S = 0.28
REJECT_FPS = 25

_RETURN, _KP_ENTER, _ESCAPE = 0xFF0D, 0xFF8D, 0xFF1B
_UP, _DOWN, _KP_UP, _KP_DOWN = 0xFF52, 0xFF54, 0xFF97, 0xFF99
_PAGE_UP, _PAGE_DOWN, _KP_PAGE_UP, _KP_PAGE_DOWN = 0xFF55, 0xFF56, 0xFF9A, 0xFF9B
_TAB, _ISO_LEFT_TAB = 0xFF09, 0xFE20

_UNBOUNDED = Size(10_000.0, 10_000.0)


class Launcher(Box):
    def __init__(self, *, rows: int = 12, width: int = 760,
                 terminal: tuple[str, ...] = ("kitty",),
                 icon_theme: str | None = None,
                 catalog: Catalog | None = None,
                 icons: Icons | None = None) -> None:
        self.catalog = catalog or Catalog(terminal=terminal)
        self.icons = icons or Icons(icon_theme)
        self.rows = rows
        self.results: list[Result] = []
        self.selected = 0
        self.top = 0                    # result shown in the first slot
        self._reject_until = 0.0

        self.query = TextInput(placeholder="search applications",
                               size=QUERY_SIZE, on_change=self._on_query,
                               flex=1)
        self._count = Label(self._count_text, size=META_SIZE,
                            color=theme.BASE_MUTED, align="right",
                            min_width=text.advance(META_SIZE, chars=7))
        self._list = _ResultList(self, width)
        prompt = Label("//", size=QUERY_SIZE, bold=True,
                       color=theme.YELLOW_MID)
        super().__init__(
            Column([Row([prompt, self.query, self._count], spacing=10,
                        align=Align.CENTER),
                    Divider(theme.CYAN_DIM),
                    self._list],
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
        # Every open starts from an empty query and a current catalog.
        # Done here rather than on close so the despawn still shows what
        # was typed.
        first = not self.catalog.apps
        if self.catalog.refresh() and not first:
            self.icons.refresh()        # installed apps bring icons along
        self._reject_until = 0.0
        self.query.set_text("")
        self._on_query("")              # even if the text was already empty
        super().attach(window)
        size = self.measure(_UNBOUNDED)
        window.resize_content(math.ceil(size.width), math.ceil(size.height))

    @property
    def animation_fps(self) -> int:
        return REJECT_FPS if self._reject_until else 0

    def animate(self, t: float) -> None:
        if self._reject_until and time.monotonic() >= self._reject_until:
            self._reject_until = 0.0
            self._list.invalidate()

    # ── results ─────────────────────────────────────────────────────────
    def _on_query(self, query: str) -> None:
        t0 = time.perf_counter()
        self.results = self.catalog.search(query)
        self.selected = self.top = 0
        self._list.invalidate()
        self._count.invalidate()
        log.debug("search %r: %d results in %.2fms", query,
                  len(self.results), (time.perf_counter() - t0) * 1000)

    def _count_text(self) -> str:
        return f"{len(self.results)}/{len(self.catalog.apps)}"

    def result_at(self, slot: int) -> Result | None:
        index = self.top + slot
        return self.results[index] if index < len(self.results) else None

    def move(self, delta: int, *, wrap: bool = True) -> None:
        n = len(self.results)
        if not n:
            return
        target = self.selected + delta
        target = target % n if wrap else max(0, min(target, n - 1))
        self.select(target)

    def select(self, index: int) -> None:
        if index == self.selected or not 0 <= index < len(self.results):
            return
        prev, self.selected = self.selected, index
        top = min(max(self.top, index - self.rows + 1), index)
        if top != self.top:
            self.top = top
            self._list.invalidate()
        else:
            self._list.invalidate_slot(prev - self.top)
            self._list.invalidate_slot(index - self.top)

    # ── actions ─────────────────────────────────────────────────────────
    def activate(self, index: int | None = None) -> None:
        index = self.selected if index is None else index
        if not self.results:
            self.run_query()
            return
        try:
            self.catalog.launch(self.results[index].app)
        except Exception:
            log.exception("cannot launch %s", self.results[index].app.entry.id)
            self._reject()
            return
        self._close()

    def run_query(self) -> None:
        line = self.query.text.strip()
        if not line:
            return
        try:
            self.catalog.run(line)
        except ValueError:              # unbalanced quotes
            log.warning("cannot parse command line %r", line)
            self._reject()
            return
        self._close()

    def _reject(self) -> None:
        self._reject_until = time.monotonic() + REJECT_S
        self._list.invalidate()         # also wakes the frame clock

    def _close(self) -> None:
        if self.window is None:
            return
        from ..core.daemon import get_daemon
        get_daemon().close(self.window.spec.name)

    # ── input ───────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        k, letter = ev.keysym, ctrl_letter(ev)
        if k in (_RETURN, _KP_ENTER):
            if ev.shift:
                self.run_query()
            else:
                self.activate()
        elif k in (_UP, _KP_UP, _ISO_LEFT_TAB) or letter in ("p", "k"):
            self.move(-1)
        elif k in (_DOWN, _KP_DOWN, _TAB) or letter in ("n", "j"):
            self.move(1)
        elif k in (_PAGE_UP, _KP_PAGE_UP):
            self.move(-self.rows, wrap=False)
        elif k in (_PAGE_DOWN, _KP_PAGE_DOWN):
            self.move(self.rows, wrap=False)
        elif k == _ESCAPE:
            return False                # the window closes on its own
        else:
            return self.query.key(ev)
        return True


class _ResultList(Widget):
    """`rows` slots stacked with a scroll thumb beside them."""

    def __init__(self, launcher: Launcher, width: int) -> None:
        super().__init__()
        self.launcher = launcher
        self.width = width
        self.slots = [_Slot(launcher, i) for i in range(launcher.rows)]

    def children(self):
        return self.slots

    def invalidate_slot(self, slot: int) -> None:
        if 0 <= slot < len(self.slots):
            self.slots[slot].invalidate()

    def measure(self, avail: Size) -> Size:
        n = len(self.slots)
        return Size(self.width, n * ROW_H + (n - 1) * ROW_GAP)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        w = rect.width() - SCROLL_GAP - SCROLL_W
        for i, slot in enumerate(self.slots):
            slot.arrange(skia.Rect.MakeXYWH(
                rect.left(), rect.top() + i * (ROW_H + ROW_GAP), w, ROW_H))

    def paint(self, canvas: skia.Canvas) -> None:
        clip = canvas.getLocalClipBounds()
        for slot in self.slots:
            if slot.rect.intersects(clip):
                slot.paint(canvas)
        self._paint_thumb(canvas)

    def _paint_thumb(self, canvas: skia.Canvas) -> None:
        n, rows = len(self.launcher.results), len(self.slots)
        if n <= rows:
            return
        r = self.rect
        h = max(ROW_H / 2, r.height() * rows / n)
        y = r.top() + (r.height() - h) * self.launcher.top / (n - rows)
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(theme.CYAN_MID))
        canvas.drawRect(skia.Rect.MakeXYWH(r.right() - SCROLL_W, y,
                                           SCROLL_W, h), paint)


class _Slot(Widget):
    """One visible row. Shows whichever result is scrolled into it, or —
    in the first slot, when nothing matches — the command the query
    would run."""

    def __init__(self, launcher: Launcher, index: int) -> None:
        super().__init__()
        self.launcher = launcher
        self.index = index

    @property
    def interactive(self) -> bool:
        return True

    def measure(self, avail: Size) -> Size:
        return Size(avail.width, ROW_H)

    # Hover selects, as rofi's hover-select does; a click launches and
    # the wheel moves the selection.
    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if value and self.launcher.result_at(self.index) is not None:
            self.launcher.select(self.launcher.top + self.index)

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        launcher = self.launcher
        if button == 1:
            if launcher.result_at(self.index) is not None:
                launcher.activate(launcher.top + self.index)
            elif self.index == 0:
                launcher.run_query()
        elif button in (4, 5):
            launcher.move(-1 if button == 4 else 1, wrap=False)
        else:
            return False
        return True

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        launcher = self.launcher
        result = launcher.result_at(self.index)
        if result is None:
            if self.index == 0 and not launcher.results \
                    and launcher.query.text.strip():
                self._paint_slab(canvas, selected=True)
                self._paint_run(canvas, launcher.query.text.strip())
            return
        selected = launcher.top + self.index == launcher.selected
        self._paint_slab(canvas, selected)
        self._paint_result(canvas, result, selected)

    def _paint_slab(self, canvas: skia.Canvas, selected: bool) -> None:
        if not selected:
            return
        r = self.rect
        slab = shapes.beveled(r.left(), r.top(), r.width(), r.height(),
                              bevel=BEVEL, corners=CORNERS, inset=LINE_W / 2)
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(*(_REJECT if self.launcher._reject_until
                                    else _SELECTED)))
        canvas.drawPath(slab, fill)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(LINE_W)
        stroke.setColor(theme.color(*_BORDER))
        canvas.drawPath(slab, stroke)

    def _paint_result(self, canvas: skia.Canvas, result: Result,
                      selected: bool) -> None:
        r = self.rect
        entry = result.app.entry
        icon_rect = skia.Rect.MakeXYWH(r.left() + PAD_X,
                                       r.centerY() - ICON / 2, ICON, ICON)
        icon = self.launcher.icons.get(entry.icon, ICON)
        if icon is not None:
            icon.draw(canvas, icon_rect, tint=theme.color(theme.HUD_FG))
        else:
            # No icon anywhere on disk: the name's initial holds its place.
            initial = entry.name[:1].upper()
            text.draw(canvas, initial,
                      icon_rect.centerX() - text.measure(initial, NAME_SIZE,
                                                         bold=True) / 2,
                      text.baseline_in(icon_rect, NAME_SIZE, bold=True),
                      NAME_SIZE, theme.color(theme.BASE_MUTED), bold=True)

        size = NAME_SIZE
        x = icon_rect.right() + GAP
        baseline = text.baseline_in(r, size)
        plain = theme.YELLOW_BRIGHT if selected else theme.HUD_FG_BRIGHT
        hit = theme.CYAN_BLOOM if selected else theme.CYAN_BRIGHT
        end = _draw_runs(canvas, entry.name, set(result.positions), x,
                         baseline, size, plain, hit)

        # The generic name, right-aligned, if it fits beside the name.
        meta = entry.generic_name
        room = r.right() - PAD_X - (end + GAP * 2)
        if meta and room > text.advance(META_SIZE, chars=4):
            meta = _ellipsize(meta, META_SIZE, room)
            text.draw(canvas, meta,
                      r.right() - PAD_X - text.measure(meta, META_SIZE),
                      text.baseline_in(r, META_SIZE), META_SIZE,
                      theme.color(theme.CYAN_MID if selected
                                  else theme.BASE_MUTED))

    def _paint_run(self, canvas: skia.Canvas, line: str) -> None:
        r = self.rect
        key = "RUN"
        x = r.left() + PAD_X
        text.draw(canvas, key, x, text.baseline_in(r, META_SIZE), META_SIZE,
                  theme.color(theme.CYAN_MID), bold=True, tracking=1.0)
        x += text.measure(key, META_SIZE, bold=True, tracking=1.0) + GAP
        room = r.right() - PAD_X - x
        text.draw(canvas, _ellipsize(line, NAME_SIZE, room), x,
                  text.baseline_in(r, NAME_SIZE), NAME_SIZE,
                  theme.color(theme.YELLOW_BRIGHT))


def _draw_runs(canvas: skia.Canvas, value: str, hits: set[int], x: float,
               baseline: float, size: float, plain: str, hit: str) -> float:
    """Draw `value` with the characters at `hits` bold and in `hit`.
    Returns the x where the text ends."""
    i, n = 0, len(value)
    while i < n:
        matched = i in hits
        j = i
        while j < n and (j in hits) == matched:
            j += 1
        run = value[i:j]
        text.draw(canvas, run, x, baseline, size,
                  theme.color(hit if matched else plain), bold=matched)
        x += text.measure(run, size, bold=matched)
        i = j
    return x


def _ellipsize(value: str, size: float, room: float) -> str:
    if text.measure(value, size) <= room:
        return value
    cell = text.advance(size)
    keep = max(0, int(room / cell) - 1)
    return value[:keep].rstrip() + "…"
