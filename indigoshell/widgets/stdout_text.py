import html
import subprocess
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from ..services import proc
from ..style import Style
from .base import Widget, make_label
from ..services.text_effects import TextEffect


def _lerp_hex(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


class StdoutText(Widget):
    """Runs a subprocess and shows each stdout line as text.

    `transform` runs on every line before display; default is identity.
    """

    def __init__(
        self,
        command: list[str],
        transform: Callable[[str], str] | None = None,
        placeholder: str = "",
        min_width_chars: int | None = None,
        max_width_chars: int | None = None,
        scroll_interval_ms: int = 220,
        scroll_gap: str = "   •   ",
        loop_scroll: bool = True,
        effect: TextEffect | None = None,
        pulse_colors: tuple[str, ...] | None = None,
        pulse_period_ms: int = 500,
        beat_sync: bool = False,
        clear_when_idle: bool = False,
        idle_player: str | None = None,
        style: Style | None = None,
        **kwargs,
    ):
        super().__init__(style, **kwargs)
        self.command = command
        self.transform = transform or (lambda s: s)
        self.placeholder = placeholder
        self.min_width_chars = min_width_chars
        self.max_width_chars = max_width_chars
        self.scroll_interval_ms = scroll_interval_ms
        self.scroll_gap = scroll_gap
        self.loop_scroll = loop_scroll
        self.effect = effect
        self.pulse_colors = pulse_colors
        self.pulse_period_ms = pulse_period_ms
        self.beat_sync = beat_sync
        self.clear_when_idle = clear_when_idle
        self.idle_player = idle_player
        self.label: Gtk.Label | None = None
        self._proc: subprocess.Popen | None = None
        self._status_subscribed: bool = False
        self._beat_subscribed: bool = False
        # Assume playing until music status says otherwise — keeps
        # widgets without idle gating behaving as before.
        self._music_playing: bool = True
        self._full_text: str = ""
        self._scroll_pos: int = 0
        self._scroll_timer: int | None = None
        self._effect_timer: int | None = None
        self._pulse_timer: int | None = None
        self._pulse_phase: int = 0
        # Beat-sync flash decay: 0.0 (base) … 1.0 (peak just hit).
        self._pulse_intensity: float = 0.0
        self._decay_timer: int | None = None
        # What's currently shown — either plain text (is_markup=False) or
        # markup (is_markup=True). The pulse re-renders this on each tick.
        self._last_payload: str = ""
        self._last_is_markup: bool = False

    def build_widget(self):
        label = make_label(self.placeholder)
        if self.min_width_chars is not None:
            label.set_width_chars(self.min_width_chars)
        if self.max_width_chars is not None:
            label.set_max_width_chars(self.max_width_chars)
            label.set_single_line_mode(True)
            label.set_xalign(0.0)
        self.label = label
        return label

    def start(self):
        self._proc = proc.subscribe(
            self.command,
            self._on_line,
            on_missing=lambda: self._set_text(f"[{self.command[0]} not found]"),
            on_exit=lambda rc: GLib.idle_add(self._set_text, f"[exited {rc}]") if rc != 0 else None,
        )

        if self.pulse_colors and not self.beat_sync:
            self._pulse_timer = GLib.timeout_add(
                self.pulse_period_ms, self._tick_pulse
            )

        # beat_sync subscribes lazily on Playing so parec/aubio don't run
        # while idle. clear_when_idle wants the same status stream to
        # blank stale text. Either feature → one music subscription.
        if self.clear_when_idle or (self.pulse_colors and self.beat_sync):
            from ..services.music import get_status
            get_status(self.idle_player).add_listener(self._on_music_status)
            self._status_subscribed = True

    def _on_music_status(self, playing: bool) -> None:
        self._music_playing = playing
        if self.pulse_colors and self.beat_sync:
            from ..services.beat import get_detector
            detector = get_detector()
            if playing and not self._beat_subscribed:
                detector.add_listener(self._on_beat)
                self._beat_subscribed = True
            elif not playing and self._beat_subscribed:
                detector.remove_listener(self._on_beat)
                self._beat_subscribed = False
                self._pulse_intensity = 0.0
                if self._decay_timer is not None:
                    GLib.source_remove(self._decay_timer)
                    self._decay_timer = None
        if not playing and self.clear_when_idle:
            # Clear so a stale lyric doesn't linger past the song.
            self._set_text("")

    def _tick_pulse(self) -> bool:
        self._pulse_phase += 1
        self._render_last()
        return True

    def _on_beat(self) -> None:
        self._pulse_intensity = 1.0
        if self._decay_timer is None:
            self._decay_timer = GLib.timeout_add(30, self._tick_decay)
        self._render_last()

    def _tick_decay(self) -> bool:
        self._pulse_intensity *= 0.82
        if self._pulse_intensity < 0.02:
            self._pulse_intensity = 0.0
            self._decay_timer = None
            self._render_last()
            return False
        self._render_last()
        return True

    def _current_pulse_color(self) -> str | None:
        if not self.pulse_colors:
            return None
        if self.beat_sync and len(self.pulse_colors) >= 2:
            return _lerp_hex(
                self.pulse_colors[0], self.pulse_colors[1], self._pulse_intensity
            )
        return self.pulse_colors[self._pulse_phase % len(self.pulse_colors)]

    def _render_last(self) -> None:
        if not self.label:
            return
        self._render(self._last_payload, self._last_is_markup)

    def _render(self, payload: str, is_markup: bool) -> None:
        """Single funnel for all label updates. Stashes the payload so
        the pulse timer can re-render with the next color."""
        if not self.label:
            return
        self._last_payload = payload
        self._last_is_markup = is_markup
        pulse_color = self._current_pulse_color()
        if pulse_color is None:
            if is_markup:
                self.label.set_markup(payload)
            else:
                self.label.set_text(payload)
            return
        # Pulse: wrap as markup. Pango span attributes nest — inner
        # color spans (e.g. scramble glyphs) win over our outer wrap.
        inner = payload if is_markup else html.escape(payload)
        weight = ' weight="bold"' if self.beat_sync and self._pulse_intensity > 0.2 else ""
        self.label.set_markup(f'<span color="{pulse_color}"{weight}>{inner}</span>')

    def _on_line(self, line: str) -> None:
        # While idle, swallow the source's lines so a still-streaming
        # producer (e.g. sptlrx re-emitting the current lyric) can't
        # overwrite the cleared placeholder.
        if self.clear_when_idle and not self._music_playing:
            return
        line = line.rstrip("\n")
        try:
            text = self.transform(line)
        except Exception as e:
            text = f"[transform error: {e}]"
        GLib.idle_add(self._set_text, text)

    def _set_text(self, text: str) -> bool:
        if not self.label:
            return False
        if not text.strip():
            self._stop_effect()
            self._stop_scroll()
            self._full_text = ""
            self._render(self.placeholder, False)
            return False
        if self.effect is not None:
            # Cancel any in-flight effect/scroll, then animate to the
            # new line; settling triggers scroll if needed.
            self._stop_effect()
            self._stop_scroll()
            self._full_text = text
            self.effect.start(text)
            self._effect_timer = GLib.timeout_add(
                self.effect.interval_ms, self._tick_effect
            )
            return False
        self._settle_text(text)
        return False

    def _settle_text(self, text: str) -> None:
        """Apply text without any effect — start scroll if it overflows."""
        if not self.label:
            return
        if self.max_width_chars is None or len(text) <= self.max_width_chars:
            self._stop_scroll()
            self._full_text = text
            self._render(text, False)
            return
        if text == self._full_text and self._scroll_timer is not None:
            return
        self._full_text = text
        self._scroll_pos = 0
        self._stop_scroll()
        self._render_scroll()
        self._scroll_timer = GLib.timeout_add(self.scroll_interval_ms, self._tick_scroll)

    def _tick_effect(self) -> bool:
        if not self.label or self.effect is None:
            self._effect_timer = None
            return False
        frame, done = self.effect.tick()
        if self.effect.produces_markup:
            self._render(frame, True)
        else:
            # Clip to label width while animating to avoid layout jitter.
            if self.max_width_chars is not None and len(frame) > self.max_width_chars:
                frame = frame[: self.max_width_chars]
            self._render(frame, False)
        if done:
            self._effect_timer = None
            self._settle_text(self._full_text)
            return False
        return True

    def _stop_effect(self) -> None:
        if self._effect_timer is not None:
            GLib.source_remove(self._effect_timer)
            self._effect_timer = None

    def _tick_scroll(self) -> bool:
        if self.max_width_chars is None:
            return False
        if self.loop_scroll:
            loop = self._full_text + self.scroll_gap
            self._scroll_pos = (self._scroll_pos + 1) % len(loop)
            self._render_scroll()
            return True
        if self._scroll_pos + self.max_width_chars >= len(self._full_text):
            self._scroll_timer = None
            return False
        self._scroll_pos += 1
        self._render_scroll()
        return True

    def _render_scroll(self):
        if not self.label or self.max_width_chars is None:
            return
        if self.loop_scroll:
            loop = self._full_text + self.scroll_gap
            doubled = loop + loop
            window = doubled[self._scroll_pos : self._scroll_pos + self.max_width_chars]
        else:
            window = self._full_text[self._scroll_pos : self._scroll_pos + self.max_width_chars]
        self._render(window, False)

    def _stop_scroll(self):
        if self._scroll_timer is not None:
            GLib.source_remove(self._scroll_timer)
            self._scroll_timer = None

    def stop(self) -> None:
        self._stop_effect()
        self._stop_scroll()
        if self._pulse_timer is not None:
            GLib.source_remove(self._pulse_timer)
            self._pulse_timer = None
        if self._beat_subscribed:
            from ..services.beat import get_detector
            get_detector().remove_listener(self._on_beat)
            self._beat_subscribed = False
        if self._decay_timer is not None:
            GLib.source_remove(self._decay_timer)
            self._decay_timer = None
        if self._status_subscribed:
            from ..services.music import get_status
            get_status(self.idle_player).remove_listener(self._on_music_status)
            self._status_subscribed = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                pass
            self._proc = None
        super().stop()
