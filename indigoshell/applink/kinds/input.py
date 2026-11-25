"""The open input's box: what an app's section turns into while one of its
inputs is open (PROTOCOL.md, section 4).

One lit box the section's width, as the current tab's: the input's icon
in the bracketed spot where a tab has its number, then the text being
typed (a `TextInput`: the caret, the selection, the shell's editing
keys), and at the right end the count (an answer's `count`, "3/12", or
the rows over the total) small and bold, its colour a role (`info` by
default; "no matches" in `error`), with the input's toggles before it
(case, regex), lit while on. While a `steps` input steps through what
it found, the text stays and the caret goes.

The box only shows and edits; what the keys mean (Enter, the list, the
row actions) is the open prompt's (applink/prompt.py).
"""

import skia

from ... import text, theme
from ...widgets.base import Size, Widget
from ...widgets.text_input import TextInput
from .look import (BOX_H, BRACKET_FG, NUMBER_CURRENT_FG, NUMBER_GAP, NUMBER_SIZE, NUMBER_SPOT,
                   SIZE, SLOT_PAD, TAB_CURRENT_FG, Band, baseline, brackets, color_of, glyph,
                   number_spot, paint_current, paint_number)

QUERY_FG = TAB_CURRENT_FG
PLACEHOLDER_FG = theme.BASE_MUTED
COUNT_ROLE = "info"
TOGGLE_ON = theme.FG_STRONG
TOGGLE_OFF = theme.BASE_MUTED
TOGGLE_GAP = 10


class InputBox(Widget):
    """The section as a text box (the module docstring)."""

    def __init__(self) -> None:
        super().__init__()
        self.field = TextInput(size=SIZE, color=QUERY_FG, caret_color=QUERY_FG,
                               placeholder_color=PLACEHOLDER_FG)
        self.icon = ""
        self.count: tuple[str, str] | None = None       # (text, colour)
        self.toggles: list[dict] = []
        self.on_toggle = None           # on_toggle(id): a toggle clicked
        self.on_click = None            # on_click(): the box clicked (not a toggle)
        self._toggle_at: list[tuple[float, float, object]] = []

    # ── what it shows ───────────────────────────────────────────────────
    def show(self, spec: dict) -> None:
        """An input's look (its icon, placeholder and toggles), its text
        not touched."""
        self.icon = glyph(spec.get("icon")) if spec.get("icon") else ""
        self.field.placeholder = spec.get("placeholder") or ""
        self.set_toggles(spec.get("buttons") or [])

    def set_toggles(self, toggles: list[dict]) -> None:
        toggles = [t for t in toggles if isinstance(t, dict)]
        if toggles != self.toggles:
            self.toggles = toggles
            self.invalidate(layout=True)

    def set_count(self, count: tuple[str, str] | None) -> None:
        if count != self.count:
            self.count = count
            self.invalidate(layout=True)

    def set_stepping(self, stepping: bool) -> None:
        """Stepping through what a `steps` input found: no caret."""
        if self.field.caret_shown == stepping:
            self.field.caret_shown = not stepping
            self.field.selected_all = False if stepping else self.field.selected_all
            self.field.invalidate()

    @property
    def stepping(self) -> bool:
        return not self.field.caret_shown

    # ── layout ──────────────────────────────────────────────────────────
    def children(self):
        return (self.field,)

    def measure(self, avail: Size) -> Size:
        return Size(avail.width, avail.height)

    def _count_w(self) -> float:
        return text.measure(self.count[0], NUMBER_SIZE, True) if self.count else 0.0

    def _toggles_w(self) -> float:
        return sum(text.measure(glyph(t.get("icon")), SIZE) + TOGGLE_GAP for t in self.toggles)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        band = Band.of(rect)
        qx = rect.left() + SLOT_PAD + NUMBER_SPOT + NUMBER_GAP
        end = rect.right() - SLOT_PAD
        if self.count:
            end -= self._count_w() + NUMBER_GAP * 2
        end -= self._toggles_w()
        self.field.measure(Size(max(0.0, end - qx), BOX_H))
        self.field.arrange(skia.Rect.MakeLTRB(qx, band.top + 2, max(qx, end), band.bottom - 2))

    # ── the mouse ───────────────────────────────────────────────────────
    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.rect.contains(x, y) else None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1:
            return False
        for left, right, tid in self._toggle_at:
            if left <= x < right:
                if self.on_toggle is not None:
                    self.on_toggle(tid)
                return True
        if self.on_click is not None:
            self.on_click()
        return True

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        band = Band.of(r)
        canvas.save()
        canvas.clipRect(r)
        paint_current(canvas, r.left(), r.width(), band)
        spot = number_spot(r.left(), band.cy)
        brackets(canvas, spot, BRACKET_FG)
        if self.icon:
            paint_number(canvas, self.icon, spot, NUMBER_CURRENT_FG, bold=False)
        self.field.paint(canvas)
        x = r.right() - SLOT_PAD
        if self.count:
            w = self._count_w()
            x -= w
            text.draw(canvas, self.count[0], x, baseline(band.cy, NUMBER_SIZE, True), NUMBER_SIZE,
                      theme.color(self.count[1]), True)
            x -= NUMBER_GAP * 2
        self._toggle_at = []
        for t in reversed(self.toggles):
            g = glyph(t.get("icon"))
            w = text.measure(g, SIZE)
            x -= w
            text.draw(canvas, g, x, baseline(band.cy, SIZE), SIZE,
                      theme.color(TOGGLE_ON if t.get("on") else TOGGLE_OFF))
            self._toggle_at.append((x - TOGGLE_GAP / 2, x + w + TOGGLE_GAP / 2, t.get("id")))
            x -= TOGGLE_GAP
        canvas.restore()

    def layout(self) -> list[dict]:
        """Where each piece is, for tests to find in pixels: the mark (the
        icon's spot), the text, the count."""
        r = self.rect
        spot = number_spot(r.left(), Band.of(r).cy)
        f = self.field
        shown = f.text or f.placeholder
        out = [{"role": "mark", "text": self.icon, "x": spot.left(), "w": spot.width()},
               {"role": "query", "text": f.text, "x": f.rect.left(),
                "w": text.measure(shown, SIZE), "caret": f.caret_shown}]
        if self.count:
            w = self._count_w()
            out.append({"role": "count", "text": self.count[0], "x": r.right() - SLOT_PAD - w,
                        "w": w, "color": self.count[1]})
        return out


def count_of(value, default: str = COUNT_ROLE) -> tuple[str, str] | None:
    """An answer's `count` ({text, color?}) as (text, colour)."""
    if isinstance(value, dict) and isinstance(value.get("text"), str) and value["text"]:
        return value["text"], color_of(value.get("color"), default)
    return None
