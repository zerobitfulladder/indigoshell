"""Frame-by-frame text transforms applied before render.

Each effect implements `start(target)` and `tick() -> (runs, done)`.
Host widgets drive it from their frame clock at the effect's
`interval_ms`.

v1 returned Pango markup for the coloured variants; there's no markup
here, so a frame is a list of `(text, color|None)` runs that a RichLabel
draws directly. Same effect, one less string format to escape.
"""

import html  # noqa: F401  (kept out of the runs path deliberately)
import random

from .. import theme

Run = tuple[str, str | None]


class TextEffect:
    interval_ms: int = 30

    def start(self, target: str) -> None:
        raise NotImplementedError

    def tick(self) -> tuple[list[Run], bool]:
        raise NotImplementedError


class Scramble(TextEffect):
    """Reveals the target left-to-right. Only a short window of
    characters around the reveal head shows random glyphs; everything
    past it renders as spaces so the total width stays constant."""

    DEFAULT_CHARSET = "01@#$%&*+=<>{}[]|/\\?!~^"
    DEFAULT_PALETTE = (
        theme.MAGENTA_BRIGHT, theme.CYAN_BRIGHT, theme.YELLOW_BRIGHT,
        theme.VIOLET_BRIGHT, theme.MAGENTA_MID, theme.CYAN_MID,
    )

    def __init__(self, interval_ms: int = 35, frames_per_char: int = 2,
                 scramble_window: int = 4, charset: str | None = None,
                 palette: tuple[str, ...] | None = None) -> None:
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

    def tick(self) -> tuple[list[Run], bool]:
        self._frame += 1
        reveal = self._frame // self.frames_per_char
        runs: list[Run] = []
        for i, ch in enumerate(self._target):
            if ch.isspace():
                runs.append((ch, None))
            elif i < reveal:
                runs.append((ch, None))
            elif i < reveal + self.scramble_window:
                runs.append((random.choice(self.charset),
                             random.choice(self.palette)))
            else:
                runs.append((" ", None))
        return runs, reveal >= len(self._target)


class Typewriter(TextEffect):
    """Reveals characters left-to-right, one per tick."""

    def __init__(self, interval_ms: int = 30) -> None:
        self.interval_ms = interval_ms
        self._target = ""
        self._idx = 0

    def start(self, target: str) -> None:
        self._target = target
        self._idx = 0

    def tick(self) -> tuple[list[Run], bool]:
        self._idx += 1
        return [(self._target[:self._idx], None)], self._idx >= len(self._target)


class WordAppear(TextEffect):
    """Reveals words one per tick."""

    def __init__(self, interval_ms: int = 90) -> None:
        self.interval_ms = interval_ms
        self._words: list[str] = []
        self._idx = 0

    def start(self, target: str) -> None:
        self._words = target.split(" ")
        self._idx = 0

    def tick(self) -> tuple[list[Run], bool]:
        self._idx += 1
        return ([(" ".join(self._words[:self._idx]), None)],
                self._idx >= len(self._words))
