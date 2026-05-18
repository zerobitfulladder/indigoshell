import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


class Anim:
    """A sequence of glyph frames with optional per-frame color and timing.

    `ms=0` means a static frame (no timer). `color` may be a single string
    applied to every frame, or a list whose length must equal `frames`.
    """

    def __init__(
        self,
        frames: list[str],
        ms: int = 0,
        loop: bool = True,
        color: str | list[str] | None = None,
    ):
        if not frames:
            raise ValueError("Anim requires at least one frame")
        if isinstance(color, list) and len(color) != len(frames):
            raise ValueError(
                f"Anim color list length ({len(color)}) must equal frames length ({len(frames)})"
            )
        self.frames = frames
        self.ms = ms
        self.loop = loop
        self.color = color

    def frame_color(self, idx: int) -> str | None:
        if isinstance(self.color, list):
            return self.color[idx]
        return self.color

    def render(self, label: Gtk.Label, idx: int) -> None:
        glyph = self.frames[idx]
        color = self.frame_color(idx)
        if color is None:
            label.set_text(glyph)
        else:
            label.set_markup(
                f"<span foreground='{color}'>{GLib.markup_escape_text(glyph)}</span>"
            )


class Icon:
    """Owns a `Gtk.Label` that renders a glyph, an animation, or a set of
    state-keyed animations. Drive it by calling `set_state(key)`.

    `source` may be:
      - `str`: a single static glyph.
      - `Anim`: one animation (state is irrelevant).
      - `dict[str, Anim]`: pick by state via `set_state`.
    """

    def __init__(
        self,
        source: str | Anim | dict[str, Anim],
        initial_state: str | None = None,
        css_class: str | None = None,
    ):
        if isinstance(source, str):
            source = Anim([source])
        if isinstance(source, Anim):
            self._anims: dict[str, Anim] = {"_": source}
            self._state = "_"
        else:
            if not source:
                raise ValueError("Icon dict source must be non-empty")
            self._anims = source
            state = initial_state if initial_state is not None else next(iter(source))
            if state not in source:
                raise ValueError(f"initial_state {state!r} not in anim keys")
            self._state = state

        self.label = Gtk.Label()
        self.label.set_valign(Gtk.Align.CENTER)
        self.label.set_xalign(0.5)
        self.label.set_yalign(0.5)
        if css_class:
            self.label.get_style_context().add_class(css_class)
        self._frame_idx = 0
        self._timer_id: int | None = None
        self._render()
        self._start_timer()

    @property
    def current(self) -> Anim:
        return self._anims[self._state]

    @property
    def states(self) -> list[str]:
        return list(self._anims.keys())

    def has_state(self, state: str) -> bool:
        return state in self._anims

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        if state not in self._anims:
            raise ValueError(f"unknown anim state {state!r}")
        self._state = state
        self._frame_idx = 0
        self._stop_timer()
        self._render()
        self._start_timer()

    def set_frame(self, idx: int) -> None:
        """Pin the current anim to a specific frame. Useful for state-driven
        (non-time-driven) anims where the widget picks the frame itself."""
        frames = self.current.frames
        idx = max(0, min(idx, len(frames) - 1))
        if idx == self._frame_idx:
            return
        self._frame_idx = idx
        self._render()

    def stop(self) -> None:
        self._stop_timer()

    def _render(self) -> None:
        self.current.render(self.label, self._frame_idx)

    def _start_timer(self) -> None:
        anim = self.current
        if anim.ms <= 0 or len(anim.frames) <= 1:
            return
        self._timer_id = GLib.timeout_add(anim.ms, self._tick)

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _tick(self) -> bool:
        anim = self.current
        next_idx = self._frame_idx + 1
        if next_idx >= len(anim.frames):
            if not anim.loop:
                self._timer_id = None
                return False
            next_idx = 0
        self._frame_idx = next_idx
        self._render()
        return True


IconLike = "Icon | str | Anim | dict[str, Anim]"


def to_icon(source, css_class: str | None = None) -> Icon:
    if isinstance(source, Icon):
        return source
    return Icon(source, css_class=css_class)
