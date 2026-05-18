"""Frame-by-frame text transforms applied before render.

Each effect implements `start(target)` and `tick() -> (frame, done)`.
Host widgets drive it from a GLib timer at the effect's `interval_ms`.
If `produces_markup` is True the frame is Pango markup, otherwise plain text.
"""

import html
import random

from .. import theme


class TextEffect:
    interval_ms: int = 30
    produces_markup: bool = False

    def start(self, target: str) -> None:
        raise NotImplementedError

    def tick(self) -> tuple[str, bool]:
        raise NotImplementedError


class Scramble(TextEffect):
    """Reveals the target left-to-right. Only a short window of
    characters around the reveal head shows random glyphs; everything
    past it is rendered as spaces so total width stays constant."""

    DEFAULT_CHARSET = "01@#$%&*+=<>{}[]|/\\?!~^"
    DEFAULT_PALETTE = (
        theme.MAGENTA_BRIGHT, theme.CYAN_BRIGHT, theme.YELLOW_BRIGHT,
        theme.VIOLET_BRIGHT, theme.MAGENTA_MID, theme.CYAN_MID,
    )
    produces_markup = True

    def __init__(
        self,
        interval_ms: int = 35,
        frames_per_char: int = 2,
        scramble_window: int = 4,
        charset: str | None = None,
        palette: tuple[str, ...] | None = None,
    ):
        self.interval_ms = interval_ms
        self.frames_per_char = max(1, frames_per_char)
        self.scramble_window = max(1, scramble_window)
        self.charset = charset or self.DEFAULT_CHARSET
        self.palette = palette or self.DEFAULT_PALETTE
        self._target = ""
        self._frame = 0

    def start(self, target: str) -> None:
        self._target = target
        self._frame = 0

    def tick(self) -> tuple[str, bool]:
        self._frame += 1
        reveal = self._frame // self.frames_per_char
        out = []
        for i, ch in enumerate(self._target):
            if ch.isspace():
                out.append(ch)
                continue
            if i < reveal:
                out.append(html.escape(ch))
            elif i < reveal + self.scramble_window:
                color = random.choice(self.palette)
                glyph = html.escape(random.choice(self.charset))
                out.append(f'<span color="{color}" weight="bold">{glyph}</span>')
            else:
                out.append(" ")
        done = reveal >= len(self._target)
        return "".join(out), done


class Typewriter(TextEffect):
    """Reveals characters left-to-right, one per tick."""

    def __init__(self, interval_ms: int = 30):
        self.interval_ms = interval_ms
        self._target = ""
        self._idx = 0

    def start(self, target: str) -> None:
        self._target = target
        self._idx = 0

    def tick(self) -> tuple[str, bool]:
        self._idx += 1
        done = self._idx >= len(self._target)
        return self._target[: self._idx], done


class WordAppear(TextEffect):
    """Reveals words one per tick."""

    def __init__(self, interval_ms: int = 90):
        self.interval_ms = interval_ms
        self._words: list[str] = []
        self._idx = 0

    def start(self, target: str) -> None:
        self._words = target.split(" ")
        self._idx = 0

    def tick(self) -> tuple[str, bool]:
        self._idx += 1
        done = self._idx >= len(self._words)
        return " ".join(self._words[: self._idx]), done
