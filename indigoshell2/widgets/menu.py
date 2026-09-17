"""Chord menus — v1's `widgets/menu.py`, on Skia.

A keybinding opens a stack of chips at the bottom right; each chip is
one `Item` with its digit as the hotkey. The interaction is v1's:

  * key-down on a digit (or hovering a row) arms that row: it lights
    and pulses cyan/yellow;
  * key-up on the same digit (or a click) fires the action;
  * an unmapped key flashes every row magenta;
  * Escape disarms an armed row, otherwise backs out a stage, and from
    the first stage lets the window close.

An action that returns a `Menu` swaps the rows for that stage in the
same window. There is no title: the rows are the whole picture, as in
v1. Each row paints its own bevelled slab, so the window is transparent
around them.
"""

import asyncio
import inspect
import logging
import math

import skia

from .. import shapes, text, theme
from ..plugin import Item, Menu
from ..services import proc
from .base import Size, Widget
from .layout import Align, Column

log = logging.getLogger(__name__)

MENU_WINDOW = "menu"

_UNBOUNDED = Size(10_000.0, 10_000.0)
ROW_H = 38
ROW_GAP = 4
BEVEL = 8
CORNERS = ("top-right", "bottom-left")
PAD_X = 18
GAP = 10              # between the label and the `// N` marker
MIN_WIDTH = 160
TRACKING = 1.0
LINE_W = 1.2
MARK = "//"
DIGITS = "123456789"

PULSE_S = 0.080       # armed row alternates cyan/yellow at this rate
REJECT_S = 0.070      # unmapped key: magenta on/off/on/off
REJECT_TOGGLES = 4
CLOCK_FPS = 25

KEY_ESCAPE = 0xFF1B
_MODIFIERS = frozenset({
    0xFFE1, 0xFFE2,            # Shift
    0xFFE3, 0xFFE4,            # Control
    0xFFE9, 0xFFEA,            # Alt
    0xFFEB, 0xFFEC,            # Super
    0xFFE7, 0xFFE8,            # Meta
    0xFFE5, 0xFF7F, 0xFE03,    # Caps Lock, Num Lock, ISO_Level3_Shift
})

# The slab is the panels' plate (POPUP_BG, ~95% violet-black), so the
# chips read as the same material as the panels; the state colours are
# v1's, laid over it rather than replacing it.
_IDLE = (theme.POPUP_BG, None)
_ARMED = (theme.CYAN_BRIGHT, 0.22)
_PULSE = (theme.YELLOW_BRIGHT, 0.40)
_ACTIVE = (theme.MAGENTA_BRIGHT, 0.32)   # v1's "picked" — the choice in effect
_REJECT = (theme.MAGENTA_BRIGHT, 0.75)
_BORDER = (theme.CYAN_BRIGHT, 0.85)


def _natural(widget: Widget) -> tuple[int, int]:
    size = widget.measure(_UNBOUNDED)
    return math.ceil(size.width), math.ceil(size.height)


async def _resolve(source) -> list[Item]:
    if callable(source):
        got = source()
        if inspect.isawaitable(got):
            got = await got
    else:
        got = source
    return list(got)


class MenuHost(Column):
    @property
    def animation_fps(self) -> int:
        return CLOCK_FPS if (self._armed is not None or self._reject_left) else 0

    def __init__(self) -> None:
        super().__init__([], spacing=ROW_GAP, align=Align.STRETCH)
        self._stack: list[tuple[Menu, list[Item]]] = []
        self._rows: list["_ChipRow"] = []
        self._task: asyncio.Task | None = None
        self._armed: int | None = None
        self._pulse_on = False
        self._pulse_acc = 0.0
        self._reject_on = False
        self._reject_left = 0
        self._reject_acc = 0.0

    # ── stages ──────────────────────────────────────────────────────────
    async def start(self, menu: Menu) -> None:
        self._stack = []
        await self.push(menu)

    async def push(self, menu: Menu) -> None:
        items = await _resolve(menu.items)
        self._stack.append((menu, items))
        self._rebuild()

    def back(self) -> bool:
        if len(self._stack) <= 1:
            return False
        self._stack.pop()
        self._rebuild()
        return True

    @property
    def path(self) -> str:
        return " // ".join(menu.title for menu, _ in self._stack)

    def _rebuild(self) -> None:
        self.disarm()
        self._reject_left, self._reject_on = 0, False
        items = self._stack[-1][1] if self._stack else []
        self._rows = [_ChipRow(self, i, item) for i, item in enumerate(items)]
        self.replace(list(self._rows))
        win = self.window
        if win is not None and win.wid is not None:
            win.resize_content(*_natural(self))

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        window.resize_content(*_natural(self))

    def detach(self) -> None:
        self.disarm()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().detach()

    # ── arm / pulse / reject ────────────────────────────────────────────
    def arm(self, index: int) -> None:
        if self._armed == index or not 0 <= index < len(self._rows):
            return
        prev = self._armed
        self._armed = index
        self._pulse_on = False
        self._pulse_acc = 0.0
        if prev is not None and prev < len(self._rows):
            self._rows[prev].invalidate()
        self._rows[index].invalidate()      # also wakes the clock

    def disarm(self) -> None:
        if self._armed is None:
            return
        prev, self._armed = self._armed, None
        self._pulse_on = False
        if prev < len(self._rows):
            self._rows[prev].invalidate()

    def _flash_reject(self) -> None:
        self._reject_left = REJECT_TOGGLES
        self._reject_on = True
        self._reject_acc = 0.0
        self._redraw_rows()

    def _redraw_rows(self) -> None:
        for row in self._rows:
            row.invalidate()

    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)
        if self._armed is not None:
            self._pulse_acc += dt
            while self._pulse_acc >= PULSE_S:
                self._pulse_acc -= PULSE_S
                self._pulse_on = not self._pulse_on
                self._rows[self._armed].invalidate()
        if self._reject_left:
            self._reject_acc += dt
            while self._reject_acc >= REJECT_S and self._reject_left:
                self._reject_acc -= REJECT_S
                self._reject_left -= 1
                self._reject_on = bool(self._reject_left) and not self._reject_on
                self._redraw_rows()

    # ── input ───────────────────────────────────────────────────────────
    @staticmethod
    def _index(keysym: int) -> int | None:
        return keysym - 0x31 if 0x31 <= keysym <= 0x39 else None

    def key(self, keysym: int, shift: bool = False) -> bool:
        if self._task is not None:
            return True                     # an action is in flight
        index = self._index(keysym)
        if index is not None and index < len(self._rows):
            self.arm(index)                 # repeats of a held key are no-ops
            return True
        if keysym == KEY_ESCAPE:
            if self._armed is not None:
                self.disarm()               # cancel the armed row
                return True
            return self.back()              # False from the first stage: close
        if keysym in _MODIFIERS:
            return False
        self._flash_reject()
        return True

    def key_release(self, keysym: int, shift: bool = False) -> bool:
        index = self._index(keysym)
        if index is None or self._armed != index:
            return False
        self.commit(index)
        return True

    def commit(self, index: int) -> None:
        self.disarm()
        if self._task is not None or not self._stack:
            return
        items = self._stack[-1][1]
        if not 0 <= index < len(items):
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(index, items[index]))

    async def _run(self, index: int, item: Item) -> None:
        try:
            result = item.run
            if callable(result):
                result = result()
                if inspect.isawaitable(result):
                    result = await result
            if isinstance(result, Menu):
                await self.push(result)
                return
            if result is not None:
                proc.fire([str(a) for a in result])
            _close()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("menu item %r failed", item.label)
            self._flash_reject()
        finally:
            self._task = None


def _close() -> None:
    from ..core.daemon import get_daemon
    d = get_daemon()
    if MENU_WINDOW in d.instances:
        d.close(MENU_WINDOW)


async def open_menu(daemon, menu: Menu) -> None:
    """Resolve `menu`'s first stage and show the menu window with it.

    Opening a different menu while one is up swaps the stages in place;
    opening the same one again closes it, like a toggle.
    """
    spec = daemon.kinds.get(MENU_WINDOW)
    if spec is None:
        raise KeyError(f"no {MENU_WINDOW!r} window kind is configured")
    host: MenuHost = spec.content
    if MENU_WINDOW in daemon.instances and host._stack \
            and host._stack[0][0].name == menu.name:
        daemon.close(MENU_WINDOW)
        return
    await host.start(menu)
    if MENU_WINDOW not in daemon.instances:
        daemon.open(MENU_WINDOW)


# ── rows ────────────────────────────────────────────────────────────────
class _ChipRow(Widget):
    """`SUSPEND        // 1`: label right-aligned against the key marker,
    as v1 laid it out. A hint, when an item has one, sits muted at the
    left edge where the label leaves room."""

    def __init__(self, host: MenuHost, index: int, item: Item) -> None:
        super().__init__()
        self.host = host
        self.index = index
        self.item = item

    @property
    def interactive(self) -> bool:
        return True

    @property
    def digit(self) -> str:
        return DIGITS[self.index] if self.index < len(DIGITS) else "?"

    @property
    def armed(self) -> bool:
        return self.host._armed == self.index

    def measure(self, avail: Size) -> Size:
        w = (2 * PAD_X + GAP
             + text.measure(self.item.label, theme.FONT_SIZE, tracking=TRACKING)
             + text.measure(f"{MARK} {self.digit}", theme.FONT_SIZE, bold=True))
        if self.item.hint:
            w += GAP * 2 + text.measure(self.item.hint, theme.FONT_SIZE - 4)
        return Size(max(w, MIN_WIDTH), ROW_H)

    def _overlay(self) -> tuple[str, float] | None:
        host = self.host
        if host._reject_on:
            return _REJECT
        if self.armed:
            return _PULSE if host._pulse_on else _ARMED
        if self.item.active:
            return _ACTIVE
        return None

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        x, y, w, h = r.left(), r.top(), r.width(), r.height()
        slab = shapes.beveled(x, y, w, h, bevel=BEVEL, corners=CORNERS,
                              inset=LINE_W / 2)
        fill = skia.Paint(AntiAlias=True)
        fill.setColor(theme.color(*_IDLE))
        canvas.drawPath(slab, fill)
        overlay = self._overlay()
        if overlay is not None:
            tint = skia.Paint(AntiAlias=True)
            tint.setColor(theme.color(*overlay))
            canvas.drawPath(slab, tint)
        stroke = skia.Paint(AntiAlias=True)
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(LINE_W)
        stroke.setColor(theme.color(*_BORDER))
        canvas.drawPath(shapes.beveled(x, y, w, h, bevel=BEVEL, corners=CORNERS,
                                       inset=LINE_W / 2), stroke)

        baseline = text.baseline_in(r, theme.FONT_SIZE)
        # `// N` at the right: yellow marker, cyan bold digit — both
        # yellow-bright while armed, as v1's `.armed` class did.
        digit_w = text.measure(self.digit, theme.FONT_SIZE, bold=True)
        mark_w = text.measure(MARK + " ", theme.FONT_SIZE, bold=True)
        kx = x + w - PAD_X - digit_w - mark_w
        mark_color = theme.FG_ACCENT if self.armed else theme.YELLOW_MID
        digit_color = theme.FG_ACCENT if self.armed else theme.HIGHLIGHT
        text.draw(canvas, MARK + " ", kx, baseline, theme.FONT_SIZE,
                  theme.color(mark_color), bold=True)
        text.draw(canvas, self.digit, kx + mark_w, baseline, theme.FONT_SIZE,
                  theme.color(digit_color), bold=True)
        label = self.item.label
        label_w = text.measure(label, theme.FONT_SIZE, tracking=TRACKING)
        label_color = theme.FG_ACCENT if self.armed else theme.FG_STRONG
        text.draw(canvas, label, kx - GAP - label_w, baseline, theme.FONT_SIZE,
                  theme.color(label_color), tracking=TRACKING)
        if self.item.hint:
            hs = theme.FONT_SIZE - 4
            text.draw(canvas, self.item.hint, x + PAD_X, text.baseline_in(r, hs),
                      hs, theme.color(theme.BASE_MUTED))

    # Hover arms, leaving disarms; a click fires — v1's mouse path.
    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        if value:
            self.host.arm(self.index)
        elif self.host._armed == self.index:
            self.host.disarm()

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1:
            return False
        self.host.commit(self.index)
        return True
