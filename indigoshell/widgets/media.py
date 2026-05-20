import html
import random
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, Pango, PangoCairo

from .. import theme
from ..services import proc
from ..style import Style, css_color
from .base import paint
from .stdout_text import StdoutText, _lerp_hex


def _playerctl(*args, player: str | None = None):
    cmd = ["playerctl"]
    if player:
        cmd += ["--player", player]
    cmd += list(args)
    proc.fire(cmd)


class Media(StdoutText):
    """MPRIS media player display + controls via `playerctl`.

    Title rendered pixel-smooth via cairo+Pango (not a label) so the
    marquee scrolls continuously instead of stepping. Periodically a
    short "glitch sweep" passes across the visible text — chars in the
    sweep window flip to random 0/1 each frame.
    """

    def __init__(
        self,
        format: str = "{{title}}",
        placeholder: str = "♫",
        player: str | None = None,
        max_chars: int = 24,
        gap_px: int = 80,
        scroll_px_per_frame: float = 1.0,
        fps: int = 60,
        glitch_interval_range: tuple[float, float] = (3.0, 8.0),
        glitch_duration_s: float = 1.0,
        glitch_density: float = 0.25,
        placeholder_size_pt: int = 20,
        bg_color: str | None = None,
        beat_pulse: bool = False,
        show_cava_bg: bool = False,
        cava_bg_color: str | None = None,
        cava_peak_color: str | None = None,
        cava_bg_alpha: float = 0.95,
        cava_floor: int = 1000,
        cava_decay: float = 0.995,
        style: Style | None = None,
        **kwargs,
    ):
        kwargs.setdefault("on_right_click", lambda _w: _playerctl("play-pause", player=player))
        kwargs.setdefault("on_scroll_up", lambda _w: _playerctl("next", player=player))
        kwargs.setdefault("on_scroll_down", lambda _w: _playerctl("previous", player=player))
        self._player = player

        if style is None:
            style = Style(italic=True)
        elif not style.italic:
            style.italic = True

        sep = "\x1f"
        wrapped_format = f"{{{{status}}}}{sep}{format}"
        cmd = ["playerctl", "--follow", "metadata", "--format", wrapped_format]
        if player:
            cmd[1:1] = ["--player", player]

        def transform(line: str) -> str:
            status, _, rest = line.partition(sep)
            if status.strip() != "Playing":
                return ""
            return rest

        super().__init__(
            command=cmd,
            transform=transform,
            placeholder=placeholder,
            style=style,
            **kwargs,
        )
        self.max_chars = max_chars
        self.gap_px = gap_px
        self.scroll_px_per_frame = scroll_px_per_frame
        self.tick_ms = max(1, int(1000 / max(1, fps)))
        self.glitch_interval_range = glitch_interval_range
        self.glitch_duration_s = glitch_duration_s
        self.glitch_density = max(0.0, min(1.0, glitch_density))
        self.placeholder_size_pt = placeholder_size_pt
        self.bg_color = bg_color
        self.beat_pulse = beat_pulse
        # State for cairo render
        self._full_text: str = ""
        self._scroll_x: float = 0.0
        self._text_w: int = 0
        self._text_h: int = 16
        self._fg_color: str = theme.MUSIC_FG
        self._layout: Pango.Layout | None = None
        self._anim_timer: int | None = None
        # Glitch state: a short burst where random characters flip to
        # colored 0/1 each frame (no sweep — scatter).
        self._glitch_active: bool = False
        self._glitch_until: float = 0.0
        self._next_glitch_at: float = time.monotonic() + random.uniform(*glitch_interval_range)
        # Cava bg state
        self.show_cava_bg = show_cava_bg
        self.cava_bg_color = cava_bg_color or theme.MAGENTA_DIM
        self.cava_peak_color = cava_peak_color or theme.MAGENTA_MID
        self.cava_bg_alpha = cava_bg_alpha
        self.cava_floor = max(1, cava_floor)
        self.cava_decay = cava_decay
        self._cava_peak: float = float(self.cava_floor)
        self._cava_bands: tuple[int, ...] | None = None
        self._beat_intensity: float = 0.0
        self._beat_decay_timer: int | None = None
        # Activation state — cava, beat listener and anim timer only run
        # while a player reports Playing. Driven by services.music.
        self._active: bool = False
        self._music_status = None

    # ── widget build ─────────────────────────────────────────────────
    def build_widget(self):
        # Estimated width when a track is playing. When idle (showing
        # placeholder), _set_text shrinks the widget down — see below.
        self._wide_w = self.max_chars * 11
        self._narrow_w = 40  # enough for the larger ♫ glyph + a little padding
        approx_h = 26
        self._filler = Gtk.Box()
        self._filler.set_size_request(self._narrow_w, approx_h)
        return self._filler

    def build(self):
        w = super().build()
        # Fill the bar height so the cava bg spans top to bottom; the
        # base widget defaults to CENTER which would crop it to ~26px.
        w.set_valign(Gtk.Align.FILL)
        w.set_vexpand(True)
        if self._named_widget is not None and self._named_widget is not w:
            self._named_widget.set_valign(Gtk.Align.FILL)
            self._named_widget.set_vexpand(True)
        w.connect_after("draw", self._draw_text)
        return w

    def default_css(self) -> str:
        if self.bg_color is None:
            return ""
        return f"#{self.name} {{ background-color: {css_color(self.bg_color)}; }}\n"

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        super().start()
        if self.gtk_widget is None:
            return
        # Cava bg draws even when inactive — handler short-circuits on
        # bands=None — so it's safe to wire the draw signal here once.
        if self.show_cava_bg:
            self.gtk_widget.connect("draw", self._draw_cava_bg)
        from ..services.music import get_status
        self._music_status = get_status(self._player)
        # add_listener replays the current state immediately, so this
        # call is what wakes the visualizer if a player is already
        # Playing at startup.
        self._music_status.add_listener(self._on_playing_changed)

    def stop(self):
        if self._music_status is not None:
            self._music_status.remove_listener(self._on_playing_changed)
            self._music_status = None
        self._deactivate()
        super_stop = getattr(super(), "stop", None)
        if super_stop:
            super_stop()

    # ── activation gating ────────────────────────────────────────────
    def _on_playing_changed(self, playing: bool) -> None:
        if playing:
            self._activate()
        else:
            self._deactivate()

    def _activate(self) -> None:
        if self._active or self.gtk_widget is None:
            return
        self._active = True
        from ..services.beat import get_detector
        detector = get_detector()
        if self.show_cava_bg:
            detector.add_bands_listener(self._on_bands)
        if self.beat_pulse:
            detector.add_listener(self._on_beat)
        self._anim_timer = GLib.timeout_add(self.tick_ms, self._tick_anim)

    def _deactivate(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._anim_timer is not None:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None
        if self._beat_decay_timer is not None:
            GLib.source_remove(self._beat_decay_timer)
            self._beat_decay_timer = None
        from ..services.beat import get_detector
        detector = get_detector()
        if self.show_cava_bg:
            detector.remove_bands_listener(self._on_bands)
        if self.beat_pulse:
            detector.remove_listener(self._on_beat)
        # Drop visual state so the next activation starts clean and the
        # cava draw handler renders nothing in the meantime.
        self._cava_bands = None
        self._cava_peak = float(self.cava_floor)
        self._beat_intensity = 0.0
        if self.gtk_widget is not None:
            self.gtk_widget.queue_draw()

    # ── text plumbing ────────────────────────────────────────────────
    def _set_text(self, text: str) -> bool:
        # No label; we just stash the text and let the draw handler
        # render it via cairo+Pango. Reset scroll on track change.
        if text == self._full_text:
            return False
        self._full_text = text
        self._scroll_x = 0.0
        self._layout = None  # rebuilt on next draw
        # Shrink widget when idle (placeholder only) so the empty
        # space doesn't dominate the bar.
        if self._filler is not None:
            target = self._wide_w if text else self._narrow_w
            self._filler.set_size_request(target, -1)
        if self.gtk_widget is not None:
            self.gtk_widget.queue_resize()
            self.gtk_widget.queue_draw()
        return False

    def _ensure_layout(self, cr) -> Pango.Layout:
        if self._layout is not None:
            return self._layout
        layout = PangoCairo.create_layout(cr)
        # Match the bar's font + italic style + a hair bolder than default.
        desc = Pango.FontDescription.from_string(
            f"{theme.FONT} Italic Bold {theme.FONT_SIZE - 2}"
        )
        layout.set_font_description(desc)
        text = self._full_text or self.placeholder
        layout.set_text(text, -1)
        self._text_w, self._text_h = layout.get_pixel_size()
        self._layout = layout
        return layout

    # ── animation tick ───────────────────────────────────────────────
    def _tick_anim(self) -> bool:
        # Smooth horizontal scroll
        if self._text_w > 0:
            loop_w = self._text_w + self.gap_px
            self._scroll_x = (self._scroll_x + self.scroll_px_per_frame) % loop_w
        # Glitch lifecycle — short scattered burst.
        now = time.monotonic()
        if not self._glitch_active and now >= self._next_glitch_at:
            self._glitch_active = True
            self._glitch_until = now + self.glitch_duration_s
        if self._glitch_active and now >= self._glitch_until:
            self._glitch_active = False
            self._next_glitch_at = now + random.uniform(*self.glitch_interval_range)
        if self.gtk_widget is not None:
            self.gtk_widget.queue_draw()
        return True

    GLITCH_PALETTE = (
        theme.MAGENTA_BRIGHT, theme.CYAN_BRIGHT, theme.YELLOW_BRIGHT,
        theme.VIOLET_BRIGHT, theme.MAGENTA_MID, theme.CYAN_MID,
    )

    def _glitched_markup(self) -> str:
        """Pango markup: a random scatter of chars flips to colored
        0/1 each frame while a glitch burst is active."""
        base = self._full_text or self.placeholder
        if not self._full_text:
            # Placeholder: bigger font so the ♫ feels iconic.
            return (
                f"<span size='{self.placeholder_size_pt * 1000}'>"
                f"{html.escape(base)}</span>"
            )
        if not self._glitch_active:
            return html.escape(base)
        out = []
        for ch in base:
            if ch.isspace() or random.random() >= self.glitch_density:
                out.append(html.escape(ch))
                continue
            glyph = "1" if random.random() < 0.5 else "0"
            color = random.choice(self.GLITCH_PALETTE)
            out.append(f'<span color="{color}" weight="bold">{glyph}</span>')
        return "".join(out)

    # ── draw ─────────────────────────────────────────────────────────
    def _draw_text(self, w, cr) -> bool:
        alloc = w.get_allocation()
        layout = self._ensure_layout(cr)
        # Always set via markup so Pango clears the attribute list each
        # frame — set_text leaves prior <span> attrs in place, which is
        # what was leaking glitch colors after the burst ended.
        layout.set_markup(self._glitched_markup(), -1)
        self._text_w, self._text_h = layout.get_pixel_size()

        # Vertical center using ink extents (logical extents include
        # leading/descent that pushes the visible glyphs off-center).
        ink, _logical = layout.get_pixel_extents()
        y = (alloc.height - ink.height) / 2 - ink.y

        # Placeholder is rendered in MAGENTA_MID; tracks in the configured fg.
        fg = theme.MAGENTA_MID if not self._full_text else self._fg_color
        paint(cr, fg)

        if self._text_w <= alloc.width:
            x = (alloc.width - self._text_w) / 2
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, layout)
            return False

        loop_w = self._text_w + self.gap_px
        offset = -self._scroll_x
        for _ in range(2):
            cr.move_to(offset, y)
            PangoCairo.show_layout(cr, layout)
            offset += loop_w
        return False

    # ── beat / cava (unchanged) ─────────────────────────────────────
    def _on_beat(self) -> None:
        self._beat_intensity = 1.0
        if self._beat_decay_timer is None:
            self._beat_decay_timer = GLib.timeout_add(30, self._tick_beat_decay)
        if self.gtk_widget is not None:
            self.gtk_widget.queue_draw()

    def _tick_beat_decay(self) -> bool:
        self._beat_intensity *= 0.82
        done = self._beat_intensity < 0.02
        if done:
            self._beat_intensity = 0.0
            self._beat_decay_timer = None
        if self.gtk_widget is not None:
            self.gtk_widget.queue_draw()
        return not done

    def _on_bands(self, bands: tuple[int, ...]) -> None:
        self._cava_bands = bands
        frame_max = max(bands)
        if frame_max > self._cava_peak:
            self._cava_peak = float(frame_max)
        else:
            self._cava_peak = max(self.cava_floor, self._cava_peak * self.cava_decay)
        if self.gtk_widget is not None:
            self.gtk_widget.queue_draw()

    def _draw_cava_bg(self, w, cr) -> bool:
        bands = self._cava_bands
        if not bands:
            return False
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n = len(bands)
        gap = 1
        bar_w = max(1.0, (width - gap * (n - 1)) / n)
        color_hex = _lerp_hex(
            self.cava_bg_color, self.cava_peak_color, self._beat_intensity
        )
        paint(cr, color_hex, alpha=self.cava_bg_alpha)
        for i, band in enumerate(bands):
            mag = min(1.0, band / self._cava_peak)
            bh = max(1.0, mag * height)
            x = i * (bar_w + gap)
            y = height - bh
            cr.rectangle(x, y, bar_w, bh)
        cr.fill()
        return False
