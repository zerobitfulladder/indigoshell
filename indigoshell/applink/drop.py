"""The drop-down: an open input's or menu's list, flush under the top bar
as if the bar grew down (PROTOCOL.md, section 4).

The bar is a dock of fixed height, so the list is a window of its own
(DROP_WINDOW, config_default's WINDOWS: "launcher", so that MOD+D's
`indigoshell toggle launcher` opens it), its top against the bar's
bottom edge and on the bar's background, with no edge between: the app
section's width for an input (the box above it its header), its own for
a menu, dropped under the item that opened it or at the section's left
edge. A submenu opens beside its row, in the same window. It takes no
keyboard and no pointer of its own: the open prompt holds both
(applink/prompt.py, service.py), and a click on a row reaches it here.

The rows are the old launcher's: up to PAGE slots, ROW_H each, a scroll
thumb when there are more. A row is a `tag` word (OPEN, SEARCH) or an
icon, its title with the characters the query matched lit, its `detail`
muted at the right, a key hint (`chord`), a check when `on`, an arrow
when it opens a submenu; one that cannot be picked is dimmed, and
picking it flashes the selection in the reject colour. The selection is
a cyan slab with a beveled border; the pointer over a row selects it,
a click picks it, the wheel moves the selection. A menu that is being
typed into shows what is typed on a line at its top.

Its spawn is the list unrolling from the bar, top down (UNROLL_S), the
window's own motion: no effect pass, so no bleed around it either.

Opened by name with nothing open (`indigoshell toggle launcher`, MOD+D)
it asks the app service for the app search (catalog.py): the top bar's
app section becomes its box, the installed apps its rows. Closed by name
(the same toggle) it closes whatever it shows.
"""

import logging
import math
import time

import skia

from .. import shapes, text, theme
from ..widgets.base import Size, Widget
from .kinds.look import CHECK, SUBMENU, glyph

log = logging.getLogger(__name__)

DROP_WINDOW = "launcher"
PAGE = 12                       # slots shown at once
QUERY_SIZE = theme.FONT_SIZE
NAME_SIZE = theme.FONT_SIZE - 2
META_SIZE = theme.FONT_SIZE - 4
ROW_H = 40
ROW_GAP = 4
ICON = 26
PAD_X = 12
GAP = 12
PAD_TOP = 4                     # under the bar's edge
PAD_BOTTOM = 8
SIDE = 0                        # the list's sides: flush with the section's
SCROLL_W = 2.0
SCROLL_GAP = 8
MENU_MIN_W = 300
MENU_MAX_W = 560
UNROLL_S = 0.11                 # the list unrolling from the bar
UNROLL_FPS = 60

BEVEL = 8
CORNERS = ("top-right", "bottom-left")
LINE_W = 1.2
_SELECTED = (theme.CYAN_BRIGHT, 0.22)
_BORDER = (theme.CYAN_BRIGHT, 0.85)
_REJECT = (theme.MAGENTA_BRIGHT, 0.75)


def rows_height(n: int) -> int:
    return n * ROW_H + max(0, n - 1) * ROW_GAP


def menu_width(rows: list[dict]) -> int:
    """A menu's own width: its widest row, within MENU_MIN_W..MENU_MAX_W."""
    widest = 0.0
    for r in rows:
        w = PAD_X + ICON + GAP + text.measure(str(r.get("title") or ""), NAME_SIZE)
        extra = [str(r.get(k)) for k in ("detail", "chord") if r.get(k)]
        if extra:
            w += GAP * 2 + text.measure("  ".join(extra), META_SIZE)
        if r.get("on") or r.get("menu") is not None:
            w += GAP + text.measure(CHECK, NAME_SIZE)
        widest = max(widest, w + PAD_X)
    return int(min(MENU_MAX_W, max(MENU_MIN_W, math.ceil(widest + SCROLL_GAP + SCROLL_W))))


class Column:
    """One list in the window: where it is, what it shows."""

    def __init__(self, x: float, y: float, w: float, rows: list[dict], selected: int, top: int,
                 filter_: str | None = None, level: int | None = None) -> None:
        self.x, self.y, self.w = x, y, w
        self.rows, self.selected, self.top = rows, selected, top
        self.filter = filter_
        self.level = level

    @property
    def first_y(self) -> float:
        return self.y + (ROW_H + ROW_GAP if self.filter else 0)

    @property
    def shown(self) -> int:
        return min(PAGE, max(0, len(self.rows) - self.top))

    @property
    def bottom(self) -> float:
        return self.first_y + rows_height(self.shown)


class DropList(Widget):
    """The drop-down window's content: the open prompt's list. `terminal`
    wraps the apps whose desktop entry says Terminal=true (MOD+D's app
    search); `icon_theme` names the theme their icons come from."""

    def __init__(self, *, terminal: tuple[str, ...] = ("kitty",),
                 icon_theme: str | None = None) -> None:
        super().__init__()
        self.prompt = None
        self.service = None             # the app service (it sets this when it starts)
        self.width = 400                # an input's: the section's
        self.columns: list[Column] = []
        self.terminal = terminal
        self.icon_theme = icon_theme
        self._catalog = None
        self._icons = None
        self._opened_at = 0.0
        self._hover: tuple[int, int] | None = None

    # ── the app search's catalog and icons, made on first use ───────────
    @property
    def catalog(self):
        if self._catalog is None:
            from ..services.apps import Catalog
            self._catalog = Catalog(terminal=self.terminal)
        return self._catalog

    @property
    def icons(self):
        if self._icons is None:
            from ..services.icons import Icons
            self._icons = Icons(self.icon_theme)
        return self._icons

    # ── what it shows ───────────────────────────────────────────────────
    def show(self, prompt, width: int | None = None) -> None:
        self.prompt = prompt
        if width is not None:
            self.width = int(width)
        self.relayout()

    def relayout(self) -> tuple[int, int]:
        """The columns for the prompt as it is now; the size they need."""
        p = self.prompt
        cols: list[Column] = []
        if p is None:
            self.columns = cols
            return self.width, 1
        if p.what == "menu":
            x, y = 0.0, float(PAD_TOP)
            for i, lv in enumerate(p.levels):
                w = menu_width(lv.source.rows)
                if i:
                    prev = cols[-1]
                    y = prev.first_y + lv.anchor_y * (ROW_H + ROW_GAP)
                cols.append(Column(x, y, w, lv.rows, lv.selected, lv.top,
                                   filter_=lv.filter or None, level=i))
                x += w
            width = int(x)
        else:
            cols.append(Column(SIDE, PAD_TOP, self.width - 2 * SIDE, p.rows, p.selected, p.top))
            width = self.width
        self.columns = cols
        height = int(max((c.bottom for c in cols), default=PAD_TOP) + PAD_BOTTOM)
        return width, max(1, height)

    def size(self) -> tuple[int, int]:
        return self.relayout()

    def begin(self) -> None:
        """The window is (re)opened: the list unrolls from the bar."""
        self._opened_at = time.monotonic()
        self._hover = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        self.begin()
        if (self.prompt is None or not self.prompt.open) and self.service is not None:
            # Opened by name with nothing to show (MOD+D): the app search.
            self.service.open_apps(self, window)
        w, h = self.relayout()
        window.resize_content(w, h)

    def on_dismiss(self) -> None:
        """Closed by name (the toggle) while it shows something: that
        closes too."""
        p = self.prompt
        if p is not None and p.open:
            p.close("shell")

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        w, h = self.relayout()
        return Size(w, h)

    @property
    def animation_fps(self) -> int:
        return UNROLL_FPS if time.monotonic() - self._opened_at < UNROLL_S else 0

    def animate(self, t: float) -> None:
        self.invalidate()

    def reveal(self) -> float:
        k = (time.monotonic() - self._opened_at) / UNROLL_S
        return 1.0 if k >= 1 else 1 - (1 - max(0.0, k)) ** 3

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        if self.prompt is None:
            return
        canvas.save()
        canvas.clipRect(skia.Rect.MakeLTRB(r.left(), r.top(), r.right(),
                                           r.top() + r.height() * self.reveal()))
        p = self.prompt
        reject = time.monotonic() < getattr(p, "reject_until", 0.0)
        for col in self.columns:
            if col.filter:
                self._paint_filter(canvas, col)
            for k in range(col.shown):
                i = col.top + k
                rect = self.row_rect(col, k)
                selected = i == col.selected
                self._paint_row(canvas, rect, col.rows[i], selected, reject and selected)
            self._paint_thumb(canvas, col)
        canvas.restore()

    def row_rect(self, col: Column, k: int) -> skia.Rect:
        r = self.rect
        return skia.Rect.MakeXYWH(r.left() + col.x + PAD_X / 2,
                                  r.top() + col.first_y + k * (ROW_H + ROW_GAP),
                                  col.w - PAD_X - SCROLL_GAP - SCROLL_W, ROW_H)

    def _paint_filter(self, canvas: skia.Canvas, col: Column) -> None:
        r = self.rect
        rect = skia.Rect.MakeXYWH(r.left() + col.x + PAD_X / 2, r.top() + col.y, col.w - PAD_X,
                                  ROW_H)
        x = rect.left() + PAD_X
        text.draw(canvas, "//", x, text.baseline_in(rect, QUERY_SIZE, True), QUERY_SIZE,
                  theme.color(theme.YELLOW_MID), True)
        x += text.measure("//", QUERY_SIZE, True) + GAP
        text.draw(canvas, col.filter + "▏", x, text.baseline_in(rect, QUERY_SIZE),
                  QUERY_SIZE, theme.color(theme.HUD_FG_BRIGHT))

    def _paint_thumb(self, canvas: skia.Canvas, col: Column) -> None:
        n = len(col.rows)
        if n <= PAGE:
            return
        r = self.rect
        h_all = rows_height(PAGE)
        h = max(ROW_H / 2, h_all * PAGE / n)
        y = r.top() + col.first_y + (h_all - h) * col.top / (n - PAGE)
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(theme.CYAN_MID))
        canvas.drawRect(skia.Rect.MakeXYWH(r.left() + col.x + col.w - PAD_X / 2 - SCROLL_W, y,
                                           SCROLL_W, h), paint)

    def _paint_row(self, canvas: skia.Canvas, r: skia.Rect, row: dict, selected: bool,
                   reject: bool) -> None:
        """A row: a tag word or an icon, the title with its matched
        characters lit, the detail and key hint muted at the right, a
        check or an arrow at the end; dimmed throughout when it cannot be
        picked."""
        if selected:
            slab = shapes.beveled(r.left(), r.top(), r.width(), r.height(), bevel=BEVEL,
                                  corners=CORNERS, inset=LINE_W / 2)
            fill = skia.Paint(AntiAlias=True)
            fill.setColor(theme.color(*(_REJECT if reject else _SELECTED)))
            canvas.drawPath(slab, fill)
            stroke = skia.Paint(AntiAlias=True)
            stroke.setStyle(skia.Paint.kStroke_Style)
            stroke.setStrokeWidth(LINE_W)
            stroke.setColor(theme.color(*_BORDER))
            canvas.drawPath(slab, stroke)
        dim = row.get("enabled") is False
        title = str(row.get("title") or "")
        if dim:
            plain = hit = theme.BASE_MUTED
        else:
            plain = theme.YELLOW_BRIGHT if selected else theme.HUD_FG_BRIGHT
            hit = theme.CYAN_BLOOM if selected else theme.CYAN_BRIGHT
        # The right end: an arrow or a check, the key hint, the detail.
        right = r.right() - PAD_X
        end_mark = SUBMENU if row.get("menu") is not None else CHECK if row.get("on") else ""
        if end_mark:
            w = text.measure(end_mark, META_SIZE)
            right -= w
            text.draw(canvas, end_mark, right, text.baseline_in(r, META_SIZE), META_SIZE,
                      theme.color(theme.BASE_MUTED if dim else theme.CYAN_MID))
            right -= GAP
        meta = "  ".join(str(row[k]) for k in ("detail", "chord") if row.get(k))
        tag = str(row.get("tag") or "")
        x = r.left() + PAD_X
        if tag:
            text.draw(canvas, tag, x, text.baseline_in(r, META_SIZE), META_SIZE,
                      theme.color(theme.CYAN_MID), bold=True, tracking=1.0)
            x += text.measure(tag, META_SIZE, bold=True, tracking=1.0) + GAP
        else:
            icon_rect = skia.Rect.MakeXYWH(x, r.centerY() - ICON / 2, ICON, ICON)
            image = self.icons.get(row["_icon"], ICON) if row.get("_icon") else None
            if image is not None:
                image.draw(canvas, icon_rect, tint=theme.color(theme.HUD_FG))
            elif row.get("_initial"):
                ch = row["_initial"]
                text.draw(canvas, ch, icon_rect.centerX() - text.measure(ch, NAME_SIZE, True) / 2,
                          text.baseline_in(icon_rect, NAME_SIZE, bold=True), NAME_SIZE,
                          theme.color(theme.BASE_MUTED), bold=True)
            else:
                g = glyph(row.get("icon")) if row.get("icon") else ""
                if g:
                    text.draw(canvas, g, icon_rect.centerX() - text.measure(g, NAME_SIZE) / 2,
                              text.baseline_in(icon_rect, NAME_SIZE), NAME_SIZE,
                              theme.color(theme.BASE_MUTED if dim else theme.HUD_FG))
            x = icon_rect.right() + GAP
        # The title has the room the meta does not need, up to half.
        meta_w = min(text.measure(meta, META_SIZE), r.width() / 2) if meta else 0.0
        room = right - x - (meta_w + GAP * 2 if meta_w else 0.0)
        shown = _ellipsize(title, NAME_SIZE, room)
        positions = set(row.get("match") or ()) if shown == title else set()
        end = _draw_runs(canvas, shown, positions, x, text.baseline_in(r, NAME_SIZE), NAME_SIZE,
                         plain, hit)
        room = right - (end + GAP * 2)
        if meta and room > text.advance(META_SIZE, chars=4):
            meta = _ellipsize(meta, META_SIZE, room)
            color = theme.BASE_MUTED if dim or not selected else theme.CYAN_MID
            text.draw(canvas, meta, right - text.measure(meta, META_SIZE),
                      text.baseline_in(r, META_SIZE), META_SIZE, theme.color(color))

    # ── the mouse ───────────────────────────────────────────────────────
    def row_at(self, x: float, y: float) -> tuple[int, int] | None:
        """(column, row index) under window-local (x, y)."""
        r = self.rect
        for c, col in enumerate(self.columns):
            if not r.left() + col.x <= x < r.left() + col.x + col.w:
                continue
            for k in range(col.shown):
                rect = self.row_rect(col, k)
                if rect.top() - ROW_GAP / 2 <= y < rect.bottom() + ROW_GAP / 2:
                    return c, col.top + k
        return None

    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.prompt is not None and self.rect.contains(x, y) else None

    def motion(self, x: float, y: float) -> None:
        at = self.row_at(x, y)
        if at is None or at == self._hover or self.prompt is None:
            self._hover = at
            return
        self._hover = at
        c, i = at
        if self.prompt.what == "menu" and c != len(self.prompt.levels) - 1:
            return                      # a level behind: hover shows, a click goes back
        self.prompt.select(i)

    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if not value:
            self._hover = None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        p = self.prompt
        if p is None:
            return False
        if button in (4, 5):
            p.scroll(-1 if button == 4 else 1)
            return True
        if button != 1:
            return False
        at = self.row_at(x, y)
        if at is not None:
            c, i = at
            p.click_row(i, self.columns[c].level)
        return True


def _draw_runs(canvas: skia.Canvas, value: str, hits: set[int], x: float, baseline: float,
               size: float, plain: str, hit: str) -> float:
    """Draw `value` with the characters at `hits` bold and in `hit`; the x
    where it ends."""
    i, n = 0, len(value)
    while i < n:
        matched = i in hits
        j = i
        while j < n and (j in hits) == matched:
            j += 1
        run = value[i:j]
        text.draw(canvas, run, x, baseline, size, theme.color(hit if matched else plain),
                  bold=matched)
        x += text.measure(run, size, bold=matched)
        i = j
    return x


def _ellipsize(value: str, size: float, room: float) -> str:
    if text.measure(value, size) <= room:
        return value
    return text.ellipsize(value, size, max(0.0, room)) if room > 0 else ""
