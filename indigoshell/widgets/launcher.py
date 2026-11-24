"""The launcher: a query line over a fixed list of results.

Opened by a keybinding (`indigoshell toggle launcher`), typed into at
once, closed by Escape, an outside click or a launch. It is a panel like
the others — the same plate, the same scan-lock spawn, kept alive between
opens — so it differs from them only in what is inside.

What it lists: the installed apps, unless the open came with a mode
(services/modes.py) that lists something else instead, for that open
only: Flux's addresses (Ctrl+L, Ctrl+T) and actions (Ctrl+M), opened by
the shell's side of Flux (applink/flux.py) through `open_launcher`.
A mode's row can lead to a further list, a step, in the same window.

    type                filter (see services/apps.py for the apps' ranking)
    Up/Down, Tab        move the selection; also Ctrl+P/N and Ctrl+K/J
    PageUp/PageDown     a page at a time
    Return, Ctrl+L      launch / pick the selected row
    Shift+Return        run the query itself as a command line — and
                        plain Return does that too when nothing matches;
                        in a mode, take the typed text itself
    Shift+Delete        take the selected row out, where the mode lets it
                        (a bookmark in Flux's addresses): the list follows
                        in place, the query stays; refused with the flash
                        anywhere else. Plain Delete edits the query.
    Escape, Ctrl+H      back one step; close from the first

The list has a fixed number of rows, so the window never resizes while
typing: a resize would re-anchor it and change the spawn effect's bleed,
and with fixed rows a keystroke repaints the list's rect and nothing else.
The rows are slots; what each one shows is read from the launcher's
results at paint time.

Type-ahead. A mode opened for a key pressed in another program's window
(the browser's Ctrl+T, which the shell grabs there) comes up while the
person goes on typing, and the keys typed before this window took the
keyboard arrive at the shell on that program's window. Whoever opened
the mode hands them over as they come (`type_ahead`); they come before
any key typed here, since the X server reports them in the order typed,
so each goes straight into the query.
"""

import logging
import math
import time

import skia

from .. import shapes, text, theme
from ..services.apps import Catalog, Result
from ..services.icons import Icons
from ..services.modes import Entry
from .base import Insets, KeyEvent, Size, Widget
from .label import Label
from .layout import Align, Box, Column, Divider, Row
from .text_input import TextInput, ctrl_letter

log = logging.getLogger(__name__)

LAUNCHER_WINDOW = "launcher"
APPS_PLACEHOLDER = "search applications"

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
_DELETE, _KP_DELETE = 0xFFFF, 0xFF9F

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
        self.results: list = []         # Result (apps) or Entry (a mode)
        self.selected = 0
        self.top = 0                    # result shown in the first slot
        self._reject_until = 0.0
        # The mode the next open shows (None: the apps), and the steps
        # shown now, each with the query it was left with.
        self.pending = None
        self.steps: list = []
        self._left_queries: list[str] = []
        self._open = False              # attached and not dismissed: keys still count

        self.query = TextInput(placeholder=APPS_PLACEHOLDER,
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

    @property
    def mode(self):
        """The mode on screen; None while the apps are listed."""
        return self.steps[-1] if self.steps else None

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        # Every open starts afresh: the mode it was opened with (the apps
        # when none), an empty query or the mode's own, a current catalog.
        # Done here rather than on close so the despawn still shows what
        # was typed.
        mode, self.pending = self.pending, None
        self._begin(mode)
        super().attach(window)
        self._open = True
        size = self.measure(_UNBOUNDED)
        window.resize_content(math.ceil(size.width), math.ceil(size.height))

    def begin(self, mode) -> None:
        """Show `mode` (None: the apps) in the open launcher, in place of
        whatever it shows."""
        self._begin(mode)

    def _begin(self, mode) -> None:
        self._end_steps()
        if mode is None:
            first = not self.catalog.apps
            if self.catalog.refresh() and not first:
                self.icons.refresh()    # installed apps bring icons along
        else:
            mode.host = self
            self.steps = [mode]
        self._reject_until = 0.0
        self._show_step()

    def _end_steps(self) -> None:
        for mode in self.steps:
            try:
                mode.close()
            except Exception:
                log.exception("closing launcher mode %s failed", type(mode).__name__)
            mode.host = None
        self.steps, self._left_queries = [], []

    def _show_step(self, query: str | None = None) -> None:
        """Put the current step on screen: its placeholder, its query
        (`query` when coming back to it), its rows."""
        mode = self.mode
        self.query.placeholder = mode.placeholder if mode is not None else APPS_PLACEHOLDER
        start = query if query is not None else (mode.text if mode is not None else "")
        self.query.set_text(start, notify=False)
        if mode is not None and query is None and mode.select_text:
            self.query.select_all()
        self._on_query(self.query.text)

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
        mode = self.mode
        if mode is None:
            self.results, start = self.catalog.search(query), 0
        else:
            try:
                self.results, start = mode.search(query)
            except Exception:
                log.exception("launcher mode %s failed to search", type(mode).__name__)
                self.results, start = [], 0
        start = max(0, min(start, len(self.results) - 1))
        self.selected = start
        self.top = max(0, start - self.rows + 1)
        self._list.invalidate()
        self._count.invalidate()
        log.debug("search %r: %d results in %.2fms", query,
                  len(self.results), (time.perf_counter() - t0) * 1000)

    def _count_text(self) -> str:
        if self.mode is not None:
            return self.mode.count(self.results)
        return f"{len(self.results)}/{len(self.catalog.apps)}"

    def result_at(self, slot: int):
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
        if self.mode is not None:
            if self.results:
                self._pick(self.results[index])
            return
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
        if self.mode is not None:
            self._pick(None)
            return
        try:
            self.catalog.run(line)
        except ValueError:              # unbalanced quotes
            log.warning("cannot parse command line %r", line)
            self._reject()
            return
        self._close()

    def _pick(self, entry: Entry | None) -> None:
        """Return in a mode: on `entry`, or (None) on the typed text."""
        if entry is not None and not entry.enabled:
            self._reject()
            return
        mode = self.mode
        try:
            result = mode.pick(entry, self.query.text)
        except Exception:
            log.exception("launcher mode %s failed to pick", type(mode).__name__)
            self._reject()
            return
        if result is False:
            self._reject()
        elif result is True or result is None:
            self._close()
        else:
            self._push(result)

    def remove_selected(self) -> None:
        """Shift+Delete: the mode takes the selected row out of its list.
        The rows below move up into its place, the selection stays at
        that place, the query as it was. Refused (the flash) by a mode
        that does not take that row out, and in the apps."""
        mode, index = self.mode, self.selected
        remove = getattr(mode, "remove", None)
        if remove is None or not 0 <= index < len(self.results):
            self._reject()
            return
        try:
            done = remove(self.results[index])
        except Exception:
            log.exception("launcher mode %s failed to remove a row", type(mode).__name__)
            done = False
        if not done:
            self._reject()
            return
        top = self.top
        self._reject_until = 0.0
        self._on_query(self.query.text)
        n = len(self.results)
        self.selected = max(0, min(index, n - 1))
        top = min(top, max(0, n - self.rows))
        self.top = max(0, min(max(top, self.selected - self.rows + 1), self.selected))
        self._list.invalidate()

    def _push(self, mode) -> None:
        """A row led to a further list: the next step."""
        self._left_queries.append(self.query.text)
        mode.host = self
        self.steps.append(mode)
        self._reject_until = 0.0
        self._show_step()

    def back(self) -> bool:
        """One step back; False from the first (the caller closes)."""
        if len(self.steps) <= 1:
            return False
        mode = self.steps.pop()
        mode.close()
        mode.host = None
        self._reject_until = 0.0
        self._show_step(self._left_queries.pop())
        return True

    def _reject(self) -> None:
        self._reject_until = time.monotonic() + REJECT_S
        self._list.invalidate()         # also wakes the frame clock

    def _close(self) -> None:
        if self.window is None:
            return
        from ..core.daemon import get_daemon
        get_daemon().close(self.window.spec.name)

    # ── type-ahead ──────────────────────────────────────────────────────
    def shows(self, mode) -> bool:
        """Whether `mode` is one of the steps on screen, the launcher open
        and not on its way out."""
        return self._open and mode in self.steps

    def type_ahead(self, mode, keys: list[KeyEvent]) -> bool:
        """Keys typed for `mode` before this window had the keyboard, as
        if typed here. False (dropped) when it no longer shows `mode`."""
        if not self.shows(mode):
            log.debug("type-ahead for a mode not shown; dropped")
            return False
        for ev in keys:
            if not self._open:
                break                   # an Escape among them closed it
            if not self.key(ev) and ev.keysym == _ESCAPE:
                self._close()           # what the window does for a live Escape
        return True

    def on_dismiss(self) -> None:
        """The window is closing: keys for it no longer count. (Not on
        detach: a window still despawning is destroyed, and detaches this
        shared widget, after a reopen gave it to another window.)"""
        self._open = False

    def cancel(self, mode) -> None:
        """Whoever asked for `mode` gave up on it."""
        if self.shows(mode) and self.window is not None:
            self._close()

    # ── input ───────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        k, letter = ev.keysym, ctrl_letter(ev)
        if k in (_RETURN, _KP_ENTER) or letter == "l":
            if ev.shift:
                self.run_query()
            else:
                self.activate()
        elif letter == "h":
            if not self.back():
                self._close()
        elif k in (_UP, _KP_UP, _ISO_LEFT_TAB) or letter in ("p", "k"):
            self.move(-1)
        elif k in (_DOWN, _KP_DOWN, _TAB) or letter in ("n", "j"):
            self.move(1)
        elif k in (_PAGE_UP, _KP_PAGE_UP):
            self.move(-self.rows, wrap=False)
        elif k in (_PAGE_DOWN, _KP_PAGE_DOWN):
            self.move(self.rows, wrap=False)
        elif k in (_DELETE, _KP_DELETE) and ev.shift and not (ev.ctrl or ev.alt):
            self.remove_selected()      # before the query, where Delete edits
        elif k == _ESCAPE:
            return self.back()          # False from the first step: the window closes
        else:
            return self.query.key(ev)
        return True


def open_launcher(daemon, mode=None, *, name: str = LAUNCHER_WINDOW,
                  placement=None) -> bool:
    """Open the launcher on `mode` (None: the apps), at `placement`
    (window.Placement; None: where its spec puts it). Open already, it
    shows `mode` in place and moves. False when no launcher window is
    configured."""
    spec = daemon.kinds.get(name)
    launcher = getattr(spec, "content", None)
    if not isinstance(launcher, Launcher):
        return False
    if name in daemon.instances:
        launcher.begin(mode)
    else:
        launcher.pending = mode
    daemon.open(name, placement=placement)
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
            if (self.index == 0 and launcher.mode is None and not launcher.results
                    and launcher.query.text.strip()):
                self._paint_slab(canvas, selected=True)
                self._paint_run(canvas, launcher.query.text.strip())
            return
        selected = launcher.top + self.index == launcher.selected
        self._paint_slab(canvas, selected)
        if isinstance(result, Entry):
            self._paint_entry(canvas, result, selected)
        else:
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
        self._paint_meta(canvas, entry.generic_name, end, selected)

    def _paint_meta(self, canvas: skia.Canvas, meta: str, end: float,
                    selected: bool, dim: bool = False) -> None:
        r = self.rect
        room = r.right() - PAD_X - (end + GAP * 2)
        if meta and room > text.advance(META_SIZE, chars=4):
            meta = _ellipsize(meta, META_SIZE, room)
            color = theme.BASE_MUTED if dim or not selected else theme.CYAN_MID
            text.draw(canvas, meta,
                      r.right() - PAD_X - text.measure(meta, META_SIZE),
                      text.baseline_in(r, META_SIZE), META_SIZE, theme.color(color))

    def _paint_entry(self, canvas: skia.Canvas, entry: Entry, selected: bool) -> None:
        """A mode's row: a key word (OPEN, SEARCH) in the RUN row's style,
        or a glyph where an app's icon goes; the title, its matched
        characters lit; the detail muted at the right. A row that cannot
        be picked is drawn dimmed throughout."""
        r = self.rect
        dim = not entry.enabled
        if entry.key:
            x = r.left() + PAD_X
            text.draw(canvas, entry.key, x, text.baseline_in(r, META_SIZE), META_SIZE,
                      theme.color(theme.CYAN_MID), bold=True, tracking=1.0)
            x += text.measure(entry.key, META_SIZE, bold=True, tracking=1.0) + GAP
            room = r.right() - PAD_X - x
            text.draw(canvas, _ellipsize(entry.title, NAME_SIZE, room), x,
                      text.baseline_in(r, NAME_SIZE), NAME_SIZE,
                      theme.color(theme.YELLOW_BRIGHT if selected else theme.HUD_FG_BRIGHT))
            return
        icon_rect = skia.Rect.MakeXYWH(r.left() + PAD_X, r.centerY() - ICON / 2, ICON, ICON)
        if entry.glyph:
            text.draw(canvas, entry.glyph,
                      icon_rect.centerX() - text.measure(entry.glyph, NAME_SIZE) / 2,
                      text.baseline_in(icon_rect, NAME_SIZE), NAME_SIZE,
                      theme.color(theme.BASE_MUTED if dim else theme.HUD_FG))
        x = icon_rect.right() + GAP
        baseline = text.baseline_in(r, NAME_SIZE)
        if dim:
            plain = hit = theme.BASE_MUTED
        else:
            plain = theme.YELLOW_BRIGHT if selected else theme.HUD_FG_BRIGHT
            hit = theme.CYAN_BLOOM if selected else theme.CYAN_BRIGHT
        # The title has the room the detail does not need, up to half.
        detail_w = min(text.measure(entry.detail, META_SIZE), r.width() / 2) \
            if entry.detail else 0.0
        room = r.right() - PAD_X - x - (detail_w + GAP * 2 if detail_w else 0.0)
        title = _ellipsize(entry.title, NAME_SIZE, room)
        positions = set(entry.positions) if title == entry.title else set()
        end = _draw_runs(canvas, title, positions, x, baseline, NAME_SIZE, plain, hit)
        self._paint_meta(canvas, entry.detail, end, selected, dim)

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
