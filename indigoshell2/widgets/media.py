"""Now playing — the MPRIS title, marquee'd over a cava visualiser.

Three things happen in one widget, which is why it isn't composed out of
`StdoutText` and a background: the title scrolls by sub-pixel offsets
rather than by whole characters, a glitch burst recolours individual
characters, and the bars behind it are drawn from the same audio the
beat pulse reads. All three want the widget's own rect and its own frame.

Ported from v1's `Media`, which drew the same picture through cairo and
Pango on top of a GTK event box. The pieces that were fighting GTK are
gone: no `valign=FILL` to keep the background from being cropped to the
label, no `set_size_request` juggling to shrink when idle, and no
markup — a glitched frame is a list of `(text, colour)` runs.

Controls are `playerctl`: click plays/pauses, scroll changes track. v1
also opened a terminal player on left click; that went with TUI support.
"""

import logging
import random
import time

import skia

from .. import shapes, text as textmod, theme
from ..services import proc
from .base import Size, Widget

log = logging.getLogger(__name__)

SEPARATOR = "\x1f"      # splits status from the title in one playerctl line

GLITCH_PALETTE = (
    theme.MAGENTA_BRIGHT, theme.CYAN_BRIGHT, theme.YELLOW_BRIGHT,
    theme.VIOLET_BRIGHT, theme.MAGENTA_MID, theme.CYAN_MID,
)

# Beat decay, as v1's per-30ms factor. Applied as `RATE ** (dt / 30)` so
# the fade takes the same wall-clock time whatever the frame rate is.
BEAT_DECAY = 0.82
BEAT_DECAY_MS = 30.0


class Media(Widget):
    # 30 while a track is playing — the visualiser really does change
    # every frame — and nothing at all otherwise, when this is a static
    # ♫ and has no reason to cost the bar a repaint. v1 ran at 60; the
    # marquee is measured in px/second here, so it moves at the same
    # speed either way.
    @property
    def animation_fps(self) -> int:
        return 30 if self._active else 0

    def __init__(self, *, player: str | None = None,
                 format: str = "{{title}}",
                 placeholder: str = "♫",
                 max_chars: int = 26,
                 gap_px: int = 80,
                 scroll_px_per_sec: float = 60.0,
                 glitch_interval_range: tuple[float, float] = (3.0, 8.0),
                 glitch_duration_s: float = 1.0,
                 glitch_density: float = 0.25,
                 size: float | None = None,
                 baseline_shift: float = 1.0,
                 bold: bool = True,
                 color: str | None = None,
                 placeholder_size: float | None = None,
                 placeholder_color: str | None = None,
                 show_cava_bg: bool = True,
                 cava_bg_color: str | None = None,
                 cava_peak_color: str | None = None,
                 cava_bg_alpha: float = 0.95,
                 cava_gap: int = 1,
                 cava_floor: int = 1000,
                 cava_decay: float = 0.995,
                 beat_pulse: bool = True,
                 plate: bool = False,
                 plate_fill: str | None = None,
                 plate_fill_alpha: float = 0.5,
                 plate_border: str = theme.CYAN_DIM,
                 plate_accent: str = theme.CYAN_BRIGHT,
                 plate_bevel: int = 8,
                 plate_corners: tuple[str, ...] = ("top-right", "bottom-left"),
                 plate_accent_width: int = 2,
                 plate_inset_y: int = 5,
                 plate_bottom: bool = True,
                 tag: str = "",
                 tag_size: float = 7,
                 tag_color: str = theme.CYAN_MID,
                 **kwargs) -> None:
        kwargs.setdefault("on_left_click", lambda _w: self.command("play-pause"))
        kwargs.setdefault("on_scroll_up", lambda _w: self.command("next"))
        kwargs.setdefault("on_scroll_down", lambda _w: self.command("previous"))
        super().__init__(**kwargs)
        self.player = player
        self.format = format
        self.placeholder = placeholder
        self.max_chars = max_chars
        self.gap_px = gap_px
        self.scroll_px_per_sec = scroll_px_per_sec
        self.glitch_interval_range = glitch_interval_range
        self.glitch_duration_s = glitch_duration_s
        self.glitch_density = max(0.0, min(1.0, glitch_density))
        self.size = size or theme.FONT_SIZE_LG
        self.baseline_shift = baseline_shift
        self.bold = bold
        self.color = color or theme.MUSIC_FG
        # v1 asked Pango for 20pt (~27px at 96 DPI); v2's size tokens
        # are already pixels, and XL is that size.
        self.placeholder_size = placeholder_size or theme.FONT_SIZE_XL
        self.placeholder_color = placeholder_color or theme.MAGENTA_MID
        self.show_cava_bg = show_cava_bg
        self.cava_bg_color = cava_bg_color or theme.MAGENTA_DIM
        self.cava_peak_color = cava_peak_color or theme.MAGENTA_MID
        self.cava_bg_alpha = cava_bg_alpha
        self.cava_gap = max(0, cava_gap)
        self.cava_floor = max(1, cava_floor)
        self.cava_decay = cava_decay
        self.beat_pulse = beat_pulse
        # HUD plate: a bevelled card (top-right / bottom-left cuts) around
        # the widget while a track plays, the visualiser clipped inside
        # it, an accent stripe down the left edge and a small tracked tag
        # above the title. `plate_fill=None` leaves the bar showing
        # through; the bars keep their own colour either way.
        self.plate = plate
        self.plate_fill = plate_fill
        self.plate_fill_alpha = plate_fill_alpha
        self.plate_border = plate_border
        self.plate_accent = plate_accent
        self.plate_bevel = plate_bevel
        # ("top-right",) alone makes a chip: one cut corner and the
        # accent stripe running the full height of the left edge.
        self.plate_corners = plate_corners
        self.plate_accent_width = plate_accent_width
        self.plate_inset_y = plate_inset_y
        # plate_bottom=False opens the plate at the bottom: it runs down
        # to the widget's edge with no bottom stroke, a tab rising from
        # the bar's floor rather than a box floating inside it.
        self.plate_bottom = plate_bottom
        self.tag = tag
        self.tag_size = tag_size
        self.tag_color = tag_color

        self._title = ""
        self._title_w = 0.0
        self._scroll = 0.0
        self._sub: proc.Subscription | None = None
        self._music = None
        # cava, the beat detector and the marquee only run between a
        # Playing and the next Paused — see `_on_playing`.
        self._active = False
        self._bands: tuple[int, ...] | None = None
        self._peak = float(self.cava_floor)
        self._beat = 0.0
        self._glitching = False
        self._glitch_until = 0.0
        self._next_glitch = time.monotonic() + random.uniform(
            *glitch_interval_range)

    # ── controls ────────────────────────────────────────────────────────
    def command(self, action: str) -> None:
        cmd = ["playerctl"]
        if self.player:
            cmd += ["--player", self.player]
        cmd.append(action)
        proc.fire(cmd)

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        if self._sub is None:
            cmd = ["playerctl", "--follow", "metadata", "--format",
                   f"{{{{status}}}}{SEPARATOR}{self.format}"]
            if self.player:
                cmd[1:1] = ["--player", self.player]
            self._sub = proc.subscribe(cmd, self._on_line)
        if self._music is None:
            from ..services.music import get_status
            self._music = get_status(self.player)
            self._music.add_listener(self._on_playing)

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        if self._music is not None:
            self._music.remove_listener(self._on_playing)
            self._music = None
        self._deactivate()
        super().detach()

    # ── activation ──────────────────────────────────────────────────────
    def _on_playing(self, playing: bool) -> None:
        if playing:
            self._activate()
        else:
            self._deactivate()
            # The metadata stream keeps emitting the paused track, and
            # `_on_line` already filters it out, but a title that was
            # showing when the pause arrived has to go too.
            self._set_title("")

    def _activate(self) -> None:
        if self._active:
            return
        self._active = True
        # Wakes the window's clock, which stopped while this was idle.
        self.invalidate()
        from ..services.beat import get_detector
        detector = get_detector()
        if self.show_cava_bg:
            detector.add_bands_listener(self._on_bands)
        if self.beat_pulse:
            detector.add_listener(self._on_beat)

    def _deactivate(self) -> None:
        if not self._active:
            return
        self._active = False
        from ..services.beat import get_detector
        detector = get_detector()
        if self.show_cava_bg:
            detector.remove_bands_listener(self._on_bands)
        if self.beat_pulse:
            detector.remove_listener(self._on_beat)
        # Drop the visual state so the next track starts from a clean
        # gain and nothing stale is left painted behind the placeholder.
        self._bands = None
        self._peak = float(self.cava_floor)
        self._beat = 0.0
        self.invalidate()

    # ── audio ───────────────────────────────────────────────────────────
    def _on_bands(self, bands: tuple[int, ...]) -> None:
        # Deliberately no invalidate(): the bar redraws on its own frame
        # clock, and repainting per band frame would drive it at cava's
        # rate on top of its own.
        self._bands = bands
        frame_max = max(bands)
        if frame_max > self._peak:
            self._peak = float(frame_max)
        else:
            self._peak = max(self.cava_floor, self._peak * self.cava_decay)

    def _on_beat(self) -> None:
        self._beat = 1.0

    # ── title ───────────────────────────────────────────────────────────
    def _on_line(self, line: str) -> None:
        status, _, rest = line.partition(SEPARATOR)
        self._set_title("" if status.strip() != "Playing" else rest.strip())

    def _set_title(self, title: str) -> None:
        if title == self._title:
            return
        self._title = title
        self._title_w = textmod.measure(title, self.size, self.bold)
        self._scroll = 0.0
        # The widget collapses to the placeholder's width when idle, so
        # the whole bar has to re-measure, not just repaint.
        self.invalidate(layout=True)

    # ── frame ───────────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)
        if not self._title:
            return

        if self._title_w > self.rect.width():
            loop = self._title_w + self.gap_px
            self._scroll = (self._scroll + dt * self.scroll_px_per_sec) % loop

        now = time.monotonic()
        if not self._glitching and now >= self._next_glitch:
            self._glitching = True
            self._glitch_until = now + self.glitch_duration_s
        elif self._glitching and now >= self._glitch_until:
            self._glitching = False
            self._next_glitch = now + random.uniform(*self.glitch_interval_range)

        if self._beat:
            self._beat *= BEAT_DECAY ** (dt * 1000.0 / BEAT_DECAY_MS)
            if self._beat < 0.02:
                self._beat = 0.0

    # ── layout ──────────────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        if self._title:
            width = textmod.advance(self.size, self.bold, self.max_chars)
        else:
            # Just the glyph plus breathing room — an idle player should
            # not hold a track's worth of empty bar open.
            width = textmod.measure(
                self.placeholder, self.placeholder_size) + 20
        # Full bar height, so the visualiser runs edge to edge rather
        # than being boxed to the height of a line of text.
        return Size(width, avail.height)

    # ── paint ───────────────────────────────────────────────────────────
    def paint(self, canvas: skia.Canvas) -> None:
        canvas.save()
        canvas.clipRect(self.rect)
        if self.plate:
            self._paint_plate(canvas)
        else:
            self._paint_bands(canvas)
            self._paint_title(canvas)
        canvas.restore()

    def _paint_plate(self, canvas: skia.Canvas) -> None:
        r = self.rect
        py = r.top() + self.plate_inset_y
        ph = r.height() - self.plate_inset_y - (
            self.plate_inset_y if self.plate_bottom else 0)
        corners = self.plate_corners
        path = shapes.beveled(r.left(), py, r.width(), ph,
                              bevel=self.plate_bevel, corners=corners)
        if self.plate_fill:
            fill = skia.Paint(AntiAlias=True)
            fill.setColor(theme.color(self.plate_fill, self.plate_fill_alpha))
            canvas.drawPath(path, fill)
        canvas.save()
        canvas.clipPath(path, True)
        self._paint_bands(canvas)
        canvas.restore()
        stroke = skia.Paint(AntiAlias=True)
        stroke.setColor(theme.color(self.plate_border))
        stroke.setStyle(skia.Paint.kStroke_Style)
        stroke.setStrokeWidth(1.0)
        if self.plate_bottom:
            outline = shapes.beveled(r.left(), py, r.width(), ph,
                                     bevel=self.plate_bevel, corners=corners,
                                     inset=0.5)
        else:
            # Open at the bottom: up the left, across the top with its
            # cuts, down the right, and no bottom edge.
            x0, x1 = r.left() + 0.5, r.right() - 0.5
            y0, y1 = py + 0.5, r.bottom()
            b = self.plate_bevel
            outline = skia.Path()
            outline.moveTo(x0, y1)
            if "top-left" in corners:
                outline.lineTo(x0, y0 + b)
                outline.lineTo(x0 + b, y0)
            else:
                outline.lineTo(x0, y0)
            if "top-right" in corners:
                outline.lineTo(x1 - b, y0)
                outline.lineTo(x1, y0 + b)
            else:
                outline.lineTo(x1, y0)
            outline.lineTo(x1, y1)
        canvas.drawPath(outline, stroke)
        # Accent stripe down the left edge, stopping short only where a
        # bottom-left cut would otherwise slice it.
        band = skia.Paint(AntiAlias=False)
        band.setColor(theme.color(self.plate_accent))
        stripe_h = ph - (self.plate_bevel if "bottom-left" in corners else 0)
        canvas.drawRect(skia.Rect.MakeXYWH(r.left(), py, self.plate_accent_width,
                                           stripe_h), band)
        if self.tag:
            textmod.draw(canvas, self.tag, r.left() + 8, py + self.tag_size + 2,
                         self.tag_size, theme.color(self.tag_color), False, 1.5)
            # The title sits in the band under the tag, centred on its ink.
            top = py + self.tag_size + 4
            height = ph - (self.tag_size + 4) - 2
        else:
            top, height = py, ph
        # Idle, the plate stays up around the placeholder glyph at the
        # widget's narrow idle width — same chip, less of it.
        if self._title:
            m = textmod.font(self.size, self.bold).getMetrics()
            baseline = (top + height / 2 - (m.fTop + m.fBottom) / 2
                        + self.baseline_shift)
        else:
            baseline = self._placeholder_baseline(top, height)
        canvas.save()
        canvas.clipPath(path, True)
        self._paint_title(canvas, baseline)
        canvas.restore()

    def _paint_bands(self, canvas: skia.Canvas) -> None:
        bands = self._bands
        if not bands:
            return
        r = self.rect
        n = len(bands)
        gap = self.cava_gap
        bar_w = max(1.0, (r.width() - gap * (n - 1)) / n)
        paint = skia.Paint(AntiAlias=False)
        paint.setColor(theme.color(
            theme.lerp(self.cava_bg_color, self.cava_peak_color, self._beat),
            self.cava_bg_alpha))
        for i, band in enumerate(bands):
            height = max(1.0, min(1.0, band / self._peak) * r.height())
            canvas.drawRect(
                skia.Rect.MakeXYWH(r.left() + i * (bar_w + gap),
                                   r.bottom() - height, bar_w, height),
                paint)

    def _baseline(self, size: float, bold: bool) -> float:
        """Vertical centre on the font's ink band, not on its em box.

        `text.baseline_in` centres ascent+descent. That reserves room for
        accents Latin text never uses, so the visible glyphs sit a couple
        of pixels high — which is what v1's `get_pixel_extents` comment
        was about when it took the trouble to centre on ink instead.

        v1 measured the ink of the *string*, which centres each title
        exactly but moves the baseline by up to 2px between titles, and
        would bounce mid-glitch as letters flip to 0/1 and the descenders
        vanish. `fTop`/`fBottom` is the same idea taken from the font
        rather than the string: the widest ink any glyph at this size can
        reach, so it is fixed for the life of the widget and nothing
        dances. `baseline_shift` is the taste knob on top, positive down.
        """
        m = textmod.font(size, bold).getMetrics()
        return (self.rect.centerY() - (m.fTop + m.fBottom) / 2
                + self.baseline_shift)

    def _placeholder_baseline(self, top: float, height: float) -> float:
        """Centre the placeholder on its own ink, not the font's band.

        `_baseline` centres on the font-wide top/bottom because titles
        change and must not bounce. The placeholder is one fixed glyph,
        and ♫ has no descender, so the band's allowance for one left it
        sitting low; its measured bounds put it dead centre.
        """
        bounds = skia.Rect()
        textmod.font(self.placeholder_size).measureText(
            self.placeholder, skia.TextEncoding.kUTF8, bounds)
        return top + height / 2 - (bounds.top() + bounds.bottom()) / 2

    def _paint_title(self, canvas: skia.Canvas,
                     baseline: float | None = None) -> None:
        r = self.rect
        if not self._title:
            size, color = self.placeholder_size, self.placeholder_color
            runs = ((self.placeholder, None),)
            width = textmod.measure(self.placeholder, size)
            bold = False
        else:
            size, color, bold = self.size, self.color, self.bold
            runs = self._runs()
            width = self._title_w

        if baseline is None:
            baseline = (self._baseline(size, bold) if self._title
                        else self._placeholder_baseline(r.top(), r.height()))
        if width <= r.width():
            self._draw_runs(canvas, runs, r.left() + (r.width() - width) / 2,
                            baseline, size, bold, color)
            return
        # Two copies a gap apart, scrolled left: as the first leaves the
        # rect the second is already in it, so the loop has no seam.
        x = r.left() - self._scroll
        for _ in range(2):
            self._draw_runs(canvas, runs, x, baseline, size, bold, color)
            x += width + self.gap_px

    def _runs(self) -> tuple[tuple[str, str | None], ...]:
        """The title, or a scattered burst of coloured 0/1 over it.

        One glyph in, one glyph out — in a monospace font that keeps the
        measured width identical, so a burst can't make the marquee jump.
        """
        if not self._glitching:
            return ((self._title, None),)
        out = []
        for ch in self._title:
            if ch.isspace() or random.random() >= self.glitch_density:
                out.append((ch, None))
            else:
                out.append(("1" if random.random() < 0.5 else "0",
                            random.choice(GLITCH_PALETTE)))
        return tuple(out)

    def _draw_runs(self, canvas: skia.Canvas, runs, x: float, baseline: float,
                   size: float, bold: bool, base: str) -> None:
        for run, color in runs:
            if not run:
                continue
            textmod.draw(canvas, run, x, baseline, size,
                         theme.color(color or base), bold)
            x += textmod.measure(run, size, bold)
