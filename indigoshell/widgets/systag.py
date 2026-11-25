"""Identity tag — the INDIGO button.

The word is framed by four small corner brackets: an L of arms at each
corner rather than a closed border, so the frame reads as a targeting
reticle. (A full beveled box was tried instead and lost the look.)

The word is still: a resting bar draws nothing (the user's rule: idle
is still, motion marks events), and this tag's colour pulse was the one
thing that kept the bar repainting all day. `pulse=True` brings the
pulse back: a sine over `cycle_s` seconds lerping between two magentas.

The pulse is driven by the host window's frame clock rather than a timer
of its own — the widget declares `animation_fps` and the window
schedules it, so N animated widgets still cost one repaint per frame
instead of N.
"""

import math

import skia

from .. import text, theme
from .base import Insets, Widget
from .label import Label
from .layout import Box


def _lerp_hex(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return (f"#{int(ar + (br - ar) * t):02x}"
            f"{int(ag + (bg - ag) * t):02x}"
            f"{int(ab + (bb - ab) * t):02x}")


class Systag(Box):
    # With `pulse`: a colour ramp over `cycle_s` seconds, not a motion —
    # the eye cannot resolve a step of 1/12th of 2.6s.
    PULSE_FPS = 12

    @property
    def animation_fps(self) -> int:
        return self.PULSE_FPS if self.pulse else 0

    def __init__(self, value: str = "INDIGO", *,
                 size: float = 13,        # points
                 color: str | None = None,
                 pulse: bool = False,
                 pulse_colors: tuple[str, str] | None = None,
                 cycle_s: float = 2.6,
                 bracket_color: str | None = None,
                 corner_arm: float = 4.0,
                 corner_thick: float = 1.0,
                 padding: Insets | None = None,
                 **kwargs) -> None:
        self._phase = 0.0
        self.pulse = pulse
        self.color = color or theme.MAGENTA_BRIGHT     # the still word's colour
        self.cycle_s = cycle_s
        self.pulse_colors = pulse_colors or (theme.MAGENTA_MID, theme.MAGENTA_BLOOM)
        self.bracket_color = bracket_color or theme.MAGENTA_DIM
        self.corner_arm = corner_arm
        self.corner_thick = corner_thick
        self.label = Label(value, size=text.pt(size), bold=True,
                           color=self._pulse_color if pulse else self.color)
        super().__init__(
            self.label,
            padding=padding or Insets.all(4),
            hover_background=theme.BASE_SURFACE,
            **kwargs,
        )

    def _pulse_color(self) -> str:
        t = (math.sin(self._phase) + 1) / 2
        return _lerp_hex(self.pulse_colors[0], self.pulse_colors[1], t)

    def animate(self, t: float) -> None:
        self._phase = (t / self.cycle_s) * 2 * math.pi

    def paint(self, canvas: skia.Canvas) -> None:
        super().paint(canvas)      # hover fill, then the label
        r = self.rect
        x, y, w, h = r.left(), r.top(), r.width(), r.height()
        arm, t = self.corner_arm, self.corner_thick
        brackets = skia.Paint(AntiAlias=False)
        brackets.setColor(theme.color(self.bracket_color))
        for rect in (
            # top-left, top-right, bottom-left, bottom-right — each an
            # L of one horizontal and one vertical arm.
            (x, y, arm, t), (x, y, t, arm),
            (x + w - arm, y, arm, t), (x + w - t, y, t, arm),
            (x, y + h - t, arm, t), (x, y + h - arm, t, arm),
            (x + w - arm, y + h - t, arm, t), (x + w - t, y + h - arm, t, arm),
        ):
            canvas.drawRect(skia.Rect.MakeXYWH(*rect), brackets)
