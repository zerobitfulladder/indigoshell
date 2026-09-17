"""Text driven by a subprocess's stdout.

Runs a long-lived command and shows its latest line, with optional
horizontal scrolling for lines wider than the widget, a per-frame text
effect (scramble/typewriter), and a colour pulse that can be driven by
music playback state.

The rendered frame is a list of `(text, colour)` runs rather than Pango
markup — `RichLabel` draws them directly.
"""

import logging
import random

import skia

from .. import effects, text as textmod, theme
from ..services import proc
from ..services.text_effects import TextEffect
from .base import Size, Widget

log = logging.getLogger(__name__)

# Beat flash decay, as v1's per-30ms factor.
BEAT_DECAY = 0.82
BEAT_DECAY_MS = 30.0
# Above this the flash also goes bold, as v1 did. Colour alone is a
# modest signal on small italic text; the weight change is what makes the
# beat legible across a room. Safe to flip per frame only because the
# shell font is monospace — advance *and* line height are identical
# between the two weights, so nothing reflows. Check that before reusing
# this trick with a proportional font.
BEAT_BOLD_AT = 0.2


class RichLabel(Widget):
    """Draws a list of (text, colour) runs on one line.

    Runs are laid out by advancing x by each run's measured width, which
    is exact for the monospace shell font and keeps per-character colour
    (the scramble effect) cheap.
    """

    def __init__(self, *, size: float | None = None, bold: bool = False,
                 italic: bool = False, color: str = theme.FG,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.size = size or theme.FONT_SIZE
        self.bold = bold
        self.italic = italic
        self.color = color
        self.runs: list[tuple[str, str | None]] = []

    def set_runs(self, runs) -> None:
        before = self.plain
        self.runs = list(runs)
        after = self.plain
        # A line of a different width is a different *size*, and only a
        # relayout re-measures it. Repainting alone drew each new lyric
        # into the slot the previous one had: a longer line was clipped
        # at that pixel boundary — mid-word — until something else in
        # the bar (a stat changing digits, the clock's minute) forced a
        # layout and the rest of it appeared. Measured, not compared as
        # strings: the reveal re-sets the same runs every frame, and a
        # scramble tick swaps glyphs of equal advance, and neither should
        # cost a whole-window repaint.
        relayout = (before != after
                    and textmod.measure(after, self.size, self.bold)
                    != textmod.measure(before, self.size, self.bold))
        self.invalidate(layout=relayout)

    @property
    def plain(self) -> str:
        return "".join(t for t, _ in self.runs)

    def measure(self, avail: Size) -> Size:
        return Size(textmod.measure(self.plain, self.size, self.bold),
                    textmod.line_height(self.size, self.bold))

    def baseline(self, size: Size) -> float:
        return -textmod.font(self.size, self.bold).getMetrics().fAscent

    def paint(self, canvas: skia.Canvas) -> None:
        baseline = textmod.baseline_in(self.rect, self.size, self.bold)
        x = self.rect.left()
        canvas.save()
        canvas.clipRect(self.rect)
        for run, color in self.runs:
            if run:
                textmod.draw(canvas, run, x, baseline, self.size,
                             theme.color(color or self.color), self.bold)
                x += textmod.measure(run, self.size, self.bold)
        canvas.restore()


class StdoutText(Widget):
    @property
    def animation_fps(self) -> int:
        """Frames only while something is actually moving.

        A lyric line sits unchanged for seconds at a time; asking for 30
        throughout meant the bar repainted for it 30 times a second to
        show the same pixels. Each of these wakes the clock again through
        `invalidate()` when it starts.
        """
        if self.effect is not None and not self._effect_done:
            return 30
        if self.reveal_effect is not None and self._reveal < 1.0:
            return 30
        if self._beat > 0.0:
            return 30
        if self.pulse_colors and not self.beat_sync:
            return 30
        if (self.max_width_chars
                and len(self._full) > self.max_width_chars):
            return 30
        return 0

    def __init__(self, command: list[str], *,
                 transform=None,
                 placeholder: str = "",
                 min_width_chars: int | None = None,
                 max_width_chars: int | None = None,
                 scroll_interval_ms: int = 220,
                 scroll_gap: str = "   •   ",
                 loop_scroll: bool = True,
                 effect: TextEffect | None = None,
                 reveal_effect=None,
                 pulse_colors: tuple[str, ...] | None = None,
                 pulse_period_ms: int = 500,
                 beat_sync: bool = False,
                 clear_when_idle: bool = False,
                 idle_player: str | None = None,
                 size: float | None = None,
                 color: str | None = None,
                 bold: bool = False,
                 italic: bool = False,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.command = list(command)
        self.transform = transform or (lambda line: line)
        self.placeholder = placeholder
        self.min_width_chars = min_width_chars
        self.max_width_chars = max_width_chars
        self.scroll_interval_ms = scroll_interval_ms
        self.scroll_gap = scroll_gap
        self.loop_scroll = loop_scroll
        self.effect = effect
        # A post-process pass replayed on every new line, at widget scale
        # — the pixel-level counterpart to `effect`, which works on
        # characters. Both can run at once; the shader sees whatever the
        # character effect has produced this frame.
        self.reveal_effect = reveal_effect
        # The reveal pass draws outside the label's rect, so damage from
        # the label alone would leave its fringes stale under a partial
        # repaint — see Widget.absorbs_damage / paint_bounds.
        self.absorbs_damage = reveal_effect is not None
        self.pulse_colors = pulse_colors
        self.pulse_period_ms = pulse_period_ms
        self.beat_sync = beat_sync
        self.clear_when_idle = clear_when_idle
        self.idle_player = idle_player

        self._base_color = color or theme.MUSIC_FG
        self._base_bold = bold
        self._label = RichLabel(size=size, bold=bold, italic=italic,
                                color=self._base_color)
        self._full = placeholder
        self._offset = 0
        self._scroll_acc = 0.0
        self._effect_acc = 0.0
        self._pulse_acc = 0.0
        self._pulse_idx = 0
        self._effect_done = True
        self._t = 0.0
        self._sub: proc.Subscription | None = None
        self._music = None
        self._playing = True
        # Beat flash: 1.0 the frame a beat lands, decaying to 0.
        self._beat = 0.0
        self._beat_subscribed = False
        # Reveal progress for `reveal_effect`: 0 at a new line, 1 when
        # the pass has finished and painting goes back to the direct path.
        self._reveal = 1.0
        self._reveal_seed = 0.0
        self._render()

    def children(self):
        return (self._label,)

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        if self._sub is None:
            self._sub = proc.subscribe(self.command, self._on_line)
        # beat_sync subscribes to the detector lazily on Playing so
        # parec and aubio don't run while idle; clear_when_idle wants the
        # same status stream to blank a stale lyric. Either feature — one
        # music subscription.
        if (self.clear_when_idle or (self.pulse_colors and self.beat_sync)) \
                and self._music is None:
            from ..services.music import get_status
            self._music = get_status(self.idle_player)
            self._music.add_listener(self._on_playing)

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        if self._music is not None:
            self._music.remove_listener(self._on_playing)
            self._music = None
        self._unsubscribe_beats()

    def _on_playing(self, playing: bool) -> None:
        self._playing = playing
        if self.pulse_colors and self.beat_sync:
            if playing:
                self._subscribe_beats()
            else:
                self._unsubscribe_beats()
        if not playing and self.clear_when_idle:
            self._set_text("")
        self.invalidate(layout=True)

    # ── beat pulse ──────────────────────────────────────────────────────
    def _subscribe_beats(self) -> None:
        if self._beat_subscribed:
            return
        from ..services.beat import get_detector
        get_detector().add_listener(self._on_beat)
        self._beat_subscribed = True

    def _unsubscribe_beats(self) -> None:
        if not self._beat_subscribed:
            return
        from ..services.beat import get_detector
        get_detector().remove_listener(self._on_beat)
        self._beat_subscribed = False
        self._beat = 0.0
        self._apply_pulse()

    def _on_beat(self) -> None:
        self._beat = 1.0
        self._apply_pulse()

    def _apply_pulse(self) -> None:
        """Restyle without re-rendering.

        The pulse only ever changes the label's colour and weight, and
        going back through `_render` would tick the text effect a second
        time per frame — the scramble would run at double speed for as
        long as a track was playing.
        """
        color = self._pulse_color() or self._base_color
        bold = self._base_bold or (self.beat_sync and self._beat > BEAT_BOLD_AT)
        if color != self._label.color or bold != self._label.bold:
            self._label.color = color
            self._label.bold = bold
            self._label.invalidate()

    def _on_line(self, line: str) -> None:
        try:
            self._set_text(self.transform(line.rstrip("\n")))
        except Exception:
            log.exception("transform failed for %s", self.command[0])

    def _set_text(self, value: str) -> None:
        if value == self._full:
            return
        self._full = value
        self._offset = 0
        self._scroll_acc = 0.0
        if self.reveal_effect is not None and value:
            self._reveal = 0.0
            # Re-rolled per line, or every reveal replays the same hash
            # sequence and the "random" slices land identically each time.
            self._reveal_seed = random.random() * 997.0
        if self.effect is not None and value:
            self.effect.start(self._visible_source())
            self._effect_done = False
            self._effect_acc = 0.0
        self._render()

    # ── text windowing ──────────────────────────────────────────────────
    def _visible_source(self) -> str:
        value = self._full or self.placeholder
        limit = self.max_width_chars
        if limit is None or len(value) <= limit:
            return self._pad(value)
        if self.loop_scroll:
            ring = value + self.scroll_gap
            start = self._offset % len(ring)
            window = (ring + ring)[start:start + limit]
        else:
            start = min(self._offset, max(0, len(value) - limit))
            window = value[start:start + limit]
        return self._pad(window)

    def _pad(self, value: str) -> str:
        if self.min_width_chars and len(value) < self.min_width_chars:
            return value.ljust(self.min_width_chars)
        return value

    def _render(self) -> None:
        # Set on the label rather than baked into the runs: the scramble
        # emits its own colours for the glyphs it is still resolving and
        # None for the settled ones, and None falls through to this — so
        # a line pulses while it is still being revealed.
        self._label.color = self._pulse_color() or self._base_color
        source = self._visible_source()
        if self.effect is not None and not self._effect_done:
            runs, done = self.effect.tick()
            self._effect_done = done
            self._label.set_runs(runs)
        else:
            self._label.set_runs([(source, None)])

    def _pulse_color(self) -> str | None:
        if not self.pulse_colors:
            return None
        if self.beat_sync:
            if len(self.pulse_colors) < 2:
                return self.pulse_colors[0]
            # Base colour at rest, accent at the instant of the beat.
            return theme.lerp(self.pulse_colors[0], self.pulse_colors[1],
                              self._beat)
        return self.pulse_colors[self._pulse_idx % len(self.pulse_colors)]

    # ── frame clock ─────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt = self.tick_dt(t) * 1000.0
        self._t = t
        dirty = False

        if self.reveal_effect is not None and self._reveal < 1.0:
            duration = max(1e-3, self.reveal_effect.duration)
            # Damage the current bounds *before* advancing: on the frame
            # the reveal completes `paint_bounds` shrinks back to the
            # rect, and whatever the previous frame threw into the bleed
            # margin would otherwise never be painted over.
            self.invalidate()
            self._reveal = min(1.0, self._reveal + (dt / 1000.0) / duration)
            dirty = True

        if self.effect is not None and not self._effect_done:
            self._effect_acc += dt
            while self._effect_acc >= self.effect.interval_ms:
                self._effect_acc -= self.effect.interval_ms
                runs, done = self.effect.tick()
                self._effect_done = done
                self._label.set_runs(runs)
                dirty = True

        if (self.max_width_chars
                and len(self._full) > self.max_width_chars
                and self._effect_done):
            self._scroll_acc += dt
            while self._scroll_acc >= self.scroll_interval_ms:
                self._scroll_acc -= self.scroll_interval_ms
                self._offset += 1
                dirty = True

        # The phase pulse and the beat pulse are alternatives, not
        # layers: with beat_sync on, the colour is a function of how long
        # ago the last beat was, and a free-running phase would fight it.
        if self.pulse_colors and not self.beat_sync:
            self._pulse_acc += dt
            while self._pulse_acc >= self.pulse_period_ms:
                self._pulse_acc -= self.pulse_period_ms
                self._pulse_idx += 1
                dirty = True
        elif self._beat:
            # v1 decayed by 0.82 every 30ms on a timer of its own. Framed
            # as a rate, the fade takes the same wall-clock time whatever
            # the window's frame rate happens to be.
            self._beat *= BEAT_DECAY ** (dt / BEAT_DECAY_MS)
            if self._beat < 0.02:
                self._beat = 0.0
            self._apply_pulse()

        if dirty and self._effect_done:
            self._render()

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        return self._label.measure(avail)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self._label.arrange(rect)

    def paint_bounds(self) -> skia.Rect:
        if self.reveal_effect is None or self._reveal >= 1.0:
            return self.rect
        bleed = self.reveal_effect.bleed(self.rect.width(), self.rect.height())
        return self.rect.makeOutset(bleed, bleed)

    def paint(self, canvas: skia.Canvas) -> None:
        if self.reveal_effect is None or self._reveal >= 1.0:
            self._label.paint(canvas)
            return
        effects.paint_through(
            canvas, self.rect, self._label.paint, (self.reveal_effect,),
            t=self._t, appear=self._reveal, seed=self._reveal_seed)
