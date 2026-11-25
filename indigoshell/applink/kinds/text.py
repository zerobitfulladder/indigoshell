"""The `text` and `button` kinds (PROTOCOL.md, section 2).

`text`: plain text in one colour role, or spans, each a piece of text or
an icon in a role of its own (Flux's host: a bookmark, an open padlock in
`error`, the host). An icon before more of the run is followed by a
space's width. Given less room than it takes, it is cut at its end with
an ellipsis (an icon that does not fit whole is left out). One that
reacts (`press`, or `opens` an input or menu) is a step brighter under
the pointer and answers a left click.

`button`: a label, an icon or both, plain; lit (the strong colour) while
`on` or while what it opens shows, muted while not `enabled`, a step
brighter under the pointer. A click sends `press`, or opens what it
names.
"""

import skia

from ... import text, theme
from ...widgets.base import Size, Widget
from . import Item
from .look import SIZE, blend, color_of, glyph

HOVER_TOWARDS = "#ffffff"
HOVER_K = 0.35                      # a reacting item under the pointer: this much lighter


def _pieces(spec: dict) -> list[tuple[str, str, bool]]:
    """(characters, colour, is an icon) for each span, an icon followed
    by a space when more comes after it."""
    spans = spec.get("spans")
    if not isinstance(spans, list):
        spans = [{"text": spec.get("text") or "", "color": spec.get("color")}]
    out = []
    for i, s in enumerate(spans):
        if not isinstance(s, dict):
            continue
        if s.get("icon") is not None:
            g = glyph(s.get("icon"))
            if g:
                out.append((g + (" " if i < len(spans) - 1 else ""), color_of(s.get("color")),
                            True))
        elif isinstance(s.get("text"), str) and s["text"]:
            out.append((s["text"], color_of(s.get("color")), False))
    return out


class TextItem(Item):
    kind = "text"

    def __init__(self, item_id) -> None:
        super().__init__(item_id)
        self.pieces: list[tuple[str, str, bool]] = []
        self.drawn: list[dict] = []

    def set(self, spec: dict) -> None:
        if spec != self.spec:
            self.spec = spec
            self.pieces = _pieces(spec)
            self.invalidate(layout=True)

    @property
    def reacts(self) -> bool:
        return bool(self.spec.get("press")) or self.spec.get("opens") is not None

    def natural(self) -> float:
        return sum(text.measure(p, SIZE) for p, _c, _i in self.pieces)

    def measure(self, avail: Size) -> Size:
        return Size(min(self.natural(), avail.width), text.line_height(SIZE))

    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.reacts and self.rect.contains(x, y) else None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1 or not self.reacts:
            return False
        opens = self.spec.get("opens")
        if opens is not None:
            if self.open is not None:
                self.open(opens, self)
        else:
            self._emit("press")
        return True

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        x, right = r.left(), r.right() + 0.5
        lit = self.hovered and self.reacts
        self.drawn = []
        for chars, color, icon in self.pieces:
            w = text.measure(chars, SIZE)
            if x + w > right:
                if icon:
                    break
                chars = text.ellipsize(chars, SIZE, right - x)
                w = text.measure(chars, SIZE)
                if not chars or x + w > right:
                    break
            shown = blend(color, HOVER_TOWARDS, HOVER_K) if lit else color
            text.draw(canvas, chars, x, base, SIZE, theme.color(shown))
            self.drawn.append({"text": chars, "x": x, "w": w, "color": color, "icon": icon})
            x += w

    def layout(self) -> list[dict]:
        """Each piece as drawn (text, x, width, colour, whether an icon),
        for tests to find in pixels."""
        return list(self.drawn)


class ButtonItem(Item):
    kind = "button"

    def natural(self) -> float:
        return text.measure(self._label(), SIZE)

    def _label(self) -> str:
        icon = glyph(self.spec.get("icon")) if self.spec.get("icon") else ""
        label = self.spec.get("label") or ""
        return f"{icon} {label}" if icon and label else icon or label

    def measure(self, avail: Size) -> Size:
        return Size(min(self.natural(), avail.width), text.line_height(SIZE))

    @property
    def enabled(self) -> bool:
        return self.spec.get("enabled") is not False

    def hit(self, x: float, y: float) -> Widget | None:
        return self if self.enabled and self.rect.contains(x, y) else None

    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button != 1 or not self.enabled:
            return False
        opens = self.spec.get("opens")
        if opens is not None:
            if self.open is not None:
                self.open(opens, self)
        else:
            self._emit("press")
        return True

    def color(self) -> str:
        if not self.enabled:
            return theme.BASE_MUTED
        if self.spec.get("on") or self.lit:
            return theme.FG_STRONG
        color = color_of(self.spec.get("color"))
        return blend(color, HOVER_TOWARDS, HOVER_K) if self.hovered else color

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        label = text.ellipsize(self._label(), SIZE, r.width() + 0.5)
        text.draw(canvas, label, r.left(), text.baseline_in(r, SIZE), SIZE,
                  theme.color(self.color()))
