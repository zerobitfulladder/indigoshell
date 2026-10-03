"""The widget reel: the bar's widgets alone, large, cut to the music.

    reel.py preview --audio TAKE.mp4 --at SECONDS     all cards, to reel-preview.mp4
    reel.py render  TAKE.json                         the take's reel, for edit.py

Each card is one widget from the bar, drawn by its real class at seven to
ten times its size — the shell draws in vectors, so it stays sharp —
driven by values chosen for the card rather than read from the machine,
floating on black and lit by its own glow, with its name in the
top-left corner.

It is cut to the music, and the music is the take's own audio, so the
beats the picture hits are the ones the viewer hears: cards change on a
beat and burst in with the panels' own scan-lock; on every beat the frame
kicks in scale and splits its colour channels for an instant, the
widget's glow flares and an echo of the widget swells out of it and
fades; through each card the camera pushes in. Nothing is added around
the widgets — no frame, no backdrop text: the light and the motion are
the widget's own. The media card's visualiser and the
system card's CPU are computed from the same seconds of audio.

The reel goes at the end of the video, after the desktop footage. The
take keeps recording for its length with the song playing, so `render`
takes its music from that tail and writes `<take>-reel.json` telling
edit.py exactly which seconds of the take's audio it was cut to.

Nothing here touches the running shell — widgets are built but never
attached, so no subprocess, D-Bus or X connection is made.
"""

import argparse
import json
import math
import random
import subprocess
from pathlib import Path

import numpy as np
import skia

import audio
import indigoshell.widgets.media as media_mod
from indigoshell import effects, text, theme
from indigoshell.effects import ColorSplit, Decode, GlitchWipe, ScanLock
from indigoshell.widgets.base import Size
from indigoshell.widgets.clock import Clock
from indigoshell.widgets.layout import Brackets, Row
from indigoshell.widgets.media import Media
from indigoshell.widgets.meters import BatteryMeter, StatMeter
from indigoshell.widgets.network import Network
from indigoshell.widgets.stdout_text import StdoutText
from indigoshell.widgets.workspaces import Workspaces

W, H = 2560, 1440
FPS = 60
OUT = Path.home() / "Videos" / "indigoshell-showcase"

CARD_MIN, CARD_MAX = 1.9, 2.8      # a card ends on the first beat in here
PUSH = 0.07                         # camera push-in across a card
KICK = 0.05                         # extra scale on a beat
KICK_S = 0.11                       # its decay constant
# The widest a widget may be drawn: at full push plus a kick it grows by
# ~12%, and it must still sit inside the frame.
MAX_DRAW_W = W / (1 + PUSH + KICK) - 160
LABEL_SIZE = 72.0
LABEL_TRACKING = 10.0
LABEL_X, LABEL_BASELINE = 120.0, 190.0
FLASH_S = 0.07                      # the white-hot frame on every cut
EXIT_S = 0.2                        # a panel flash scan-locks away this fast
BACKDROP = "#020108"
BLOOM_DOWN = 4                      # glow is blurred at 1/4 size, then scaled up
BLOOM_SIGMA = 7.0                   # at that size: ~28px at full
BLOOM_ALPHA = (0.5, 0.45)           # at rest, and the extra on a beat
ECHO_S = 0.35                       # the widget's echo after a beat
ECHO_GROW = 0.10
ECHO_ALPHA = 0.38
SCANLINE_ALPHA = 0.05               # the black reads as a screen, not a void

# The panels' spawn, scaled up for a full frame: wider bands, harder tear.
ENTER = ScanLock(duration=0.28, band_px=72.0, tear_px=70.0, split_px=34.0,
                 spark=(0.05, 0.45, 0.5))
BEAT_SPLIT = ColorSplit(duration=0.16, amount=0.012, rows=18, fps=60)
LABEL_DECODE = Decode(duration=0.45, jump=28.0, slip=13.0, split=17.0,
                      vsplit=8.0)

# What the preview shows, to match the take-2 audio it is cut to. A real
# reel takes its title and lines from the take instead — see render_take.
PREVIEW_TITLE = "A Man Without Love — Engelbert Humperdinck"
PREVIEW_LINES = ["Lonely is a man without love",
                 "Waiting inside her eyes was my tomorrow"]
# The longest line the lyrics card fits, at its size, with the camera's
# push and kick.
LYRIC_MAX_CHARS = 40


# ── GPU ────────────────────────────────────────────────────────────────
# Rendered on the GPU through the shell's own EGL backend, with no window:
# its context parks on a 1x1 pbuffer, and every surface here is an
# offscreen render target. The scan-lock, colour split, glitch passes and
# bloom are what a GPU does in microseconds — a frame costs ~20ms there
# against ~1.4s on the CPU raster path, which remains the fallback.
_gpu = None
_tried_gpu = False


def _context():
    global _gpu, _tried_gpu
    if not _tried_gpu:
        _tried_gpu = True
        try:
            from indigoshell.backend import gl
            from indigoshell.backend.x11 import Display
            backend = gl.backend(Display())
            if backend is not None:
                backend.make_idle()
                _gpu = skia.GrDirectContexts.MakeGL(skia.GrGLInterface.MakeEGL())
                backend.context = _gpu
                name = (backend.lib.gl.glGetString(gl.GL_RENDERER) or b"?").decode()
                print(f"reel: rendering on {name}")
        except Exception as e:
            print(f"reel: no GPU ({e}); rendering on the CPU")
            _gpu = None
    return _gpu


def _surface(w: int, h: int) -> skia.Surface:
    ctx = _context()
    if ctx is not None:
        return skia.Surfaces.MakeRenderTarget(
            ctx, skia.Budgeted.kYes, skia.ImageInfo.MakeN32Premul(w, h))
    return skia.Surface(w, h)


class _Clock:
    """Media reads `time.monotonic()` for its glitch bursts; the reel
    hands it frame time instead, so a render is the same every run."""
    now = 0.0

    @classmethod
    def monotonic(cls) -> float:
        return cls.now


media_mod.time = _Clock


# ── cards ───────────────────────────────────────────────────────────────
class Card:
    """One widget, its name, and what drives it. `scale` is applied as a
    canvas transform, the largest up to `max_scale` that fits the frame.
    A card whose widget is built at size instead sets `native`."""

    label = ""
    max_scale = 8.0
    native = False
    weight = 2.0        # share of the reel; a panel flash is half a card
    leaves = False      # plays the scan-lock backwards to go

    def widget_size(self) -> tuple[float, float]:
        raise NotImplementedError

    @property
    def scale(self) -> float:
        if self.native:
            return 1.0
        return min(self.max_scale, MAX_DRAW_W / self.widget_size()[0])

    @property
    def strip_scale(self) -> float:
        """How tall the widget's bar slot is drawn, as a multiple of the
        bar — what the widget is centred in."""
        return self.scale

    def drive(self, t: float, dur: float, frame: int, beat: bool) -> None:
        """Set the widget's state for card time `t` of `dur`."""

    def paint(self, canvas: skia.Canvas) -> None:
        raise NotImplementedError


def _bar_row(widget, w: float) -> None:
    """Arrange a widget the way the bar's row does: full bar height,
    centred vertically on its own measured height."""
    m = widget.measure(Size(w, theme.BAR_HEIGHT))
    h = min(m.height, theme.BAR_HEIGHT)
    widget.arrange(skia.Rect.MakeXYWH(0, (theme.BAR_HEIGHT - h) / 2, w, h))


class MediaCard(Card):
    label = "MEDIA"
    max_scale = 7.0

    def __init__(self, bands, title: str) -> None:
        self.bands = bands
        self.w = Media(player="showcase", max_chars=26, size=theme.FONT_SIZE_LG,
                       baseline_shift=1.0, show_cava_bg=True, beat_pulse=True)
        self.w._active = True
        self.w._set_title(title)
        self.w._next_glitch = 1e9   # set on the card's own beat, below
        self.glitched = False

    def widget_size(self):
        return self.w.measure(Size(10_000, theme.BAR_HEIGHT)).width, theme.BAR_HEIGHT

    def drive(self, t, dur, frame, beat):
        row = self.bands[min(frame, len(self.bands) - 1)]
        self.w._on_bands(tuple(int(v * 50000) for v in row))
        if beat:
            self.w._on_beat()
            if t > 0.9 and not self.glitched:   # one title glitch, on a beat
                self.glitched = True
                self.w._next_glitch = _Clock.now
                self.w.glitch_duration_s = 0.6

    def paint(self, canvas):
        w, h = self.widget_size()
        self.w.arrange(skia.Rect.MakeWH(w, h))
        self.w.animate(_Clock.now)
        self.w.paint(canvas)


class LyricsCard(Card):
    """Built at size: the reveal renders through an offscreen at the
    widget's own resolution, which a canvas scale would only magnify."""

    label = "LYRICS"
    native = True
    S = 5.5

    def __init__(self, lines: list[tuple[float, str]]) -> None:
        s = self.S
        self.lines = list(lines)
        self.w = StdoutText(
            ["true"], placeholder=" ", min_width_chars=8, max_width_chars=100,
            scroll_interval_ms=90, loop_scroll=False,
            reveal_effect=GlitchWipe(duration=0.8, cell=3 * s, slice_px=3 * s,
                                     seg_px=22 * s, shift=5 * s, split=3 * s,
                                     vsplit=1 * s, bar_w=2 * s, glow_px=14 * s),
            pulse_colors=(theme.CYAN_MID, theme.CYAN_BLOOM), beat_sync=True,
            italic=True, size=(theme.FONT_SIZE - 1) * s)

    @property
    def strip_scale(self):
        return self.S

    def widget_size(self):
        m = self.w.measure(Size(10_000, 10_000))
        return m.width, m.height

    def drive(self, t, dur, frame, beat):
        while self.lines and t >= self.lines[0][0]:
            self.w._on_line(self.lines.pop(0)[1])
        if beat:
            self.w._on_beat()

    def paint(self, canvas):
        w, h = self.widget_size()
        bar_h = theme.BAR_HEIGHT * self.S
        self.w.arrange(skia.Rect.MakeXYWH(0, (bar_h - h) / 2, w, h))
        self.w.animate(_Clock.now)
        self.w.paint(canvas)


class NetworkCard(Card):
    label = "NETWORK"
    max_scale = 10.0

    def __init__(self, name: str, ip: str) -> None:
        self.w = Network()
        self.w._apply(name, ip)
        self.w._f_up, self.w._f_down = 0.5, 0.5
        # (up, down) blink Hz, stepped up on successive beats: idle, a
        # download starting, then the link flat out.
        self.steps = [(0.5, 1.0), (0.5, 2.0), (1.0, 3.0), (2.0, 4.0)]

    def widget_size(self):
        return self.w.measure(Size(10_000, theme.BAR_HEIGHT)).width, theme.BAR_HEIGHT

    def drive(self, t, dur, frame, beat):
        if beat and self.steps:
            self.w._f_up, self.w._f_down = self.steps.pop(0)

    def paint(self, canvas):
        _bar_row(self.w, self.widget_size()[0])
        self.w.animate(_Clock.now)
        self.w.paint(canvas)


class SystemCard(Card):
    """The bar's CPU/RAM/TEMP cluster: CPU rides the music's low end, RAM
    climbs, TEMP sweeps its gradient from cyan into red."""

    label = "SYSTEM"
    max_scale = 8.0
    SAMPLE_HZ = 10                  # readable digits, still lively

    def __init__(self, bands) -> None:
        self.bands = bands
        self.v = {"cpu": 7.0, "ram": 41.0, "temp": 46.0}
        v = self.v
        self.meters = [
            StatMeter("CPU", lambda: v["cpu"],
                      bright_color=theme.CYAN_BRIGHT, dim_color=theme.CYAN_DIM,
                      label_color=theme.CYAN_DIM, value_color=theme.CYAN_BRIGHT),
            StatMeter("RAM", lambda: v["ram"],
                      bright_color=theme.VIOLET_BRIGHT, dim_color=theme.VIOLET_DIM,
                      label_color=theme.VIOLET, value_color=theme.VIOLET_BRIGHT),
            StatMeter("TEMP", lambda: v["temp"], value_format="{:.0f}°",
                      dim_color=theme.CYAN_DIM, label_color=theme.CYAN_DIM,
                      to_pct=lambda t: (t - 30) * (100 / 60),
                      gradient=((0.0, theme.CYAN_BRIGHT),
                                (0.5, theme.YELLOW_BRIGHT),
                                (0.8, theme.ERROR))),
        ]
        self.w = Brackets(Row(self.meters, spacing=theme.SPACING_XL))
        self.next_sample = 0.0

    def widget_size(self):
        return self.w.measure(Size(10_000, theme.BAR_HEIGHT)).width, theme.BAR_HEIGHT

    def drive(self, t, dur, frame, beat):
        x = t / dur
        low = float(self.bands[min(frame, len(self.bands) - 1)][:4].mean())
        self.v["cpu"] = 6 + 88 * low ** 1.6
        self.v["ram"] = 41 + 27 * x
        self.v["temp"] = 46 + 45 * (x * x * (3 - 2 * x))

    def paint(self, canvas):
        if _Clock.now >= self.next_sample:
            self.next_sample = _Clock.now + 1 / self.SAMPLE_HZ
            for m in self.meters:
                m.animate(_Clock.now)
        _bar_row(self.w, self.widget_size()[0])
        self.w.paint(canvas)


class BatteryCard(Card):
    """The clock with the battery underline, charging: the lit cells
    climb, then at full charge the whole bar shimmers lime."""

    label = "BATTERY"
    max_scale = 10.0

    def __init__(self) -> None:
        self.battery = BatteryMeter(cell_thick=2, gap=2, height=4, pad_y=0,
                                    brackets=False, fill=True,
                                    fake_state=(22.0, True))
        self.w = Clock(extra=self.battery)

    def widget_size(self):
        return self.w.measure(Size(10_000, theme.BAR_HEIGHT)).width, theme.BAR_HEIGHT

    def drive(self, t, dur, frame, beat):
        full_at = 0.62 * dur
        pct = 100.0 if t >= full_at else 22 + 78 * (t / full_at)
        self.battery.fake_state = (pct, True)
        self.battery._poll()

    def paint(self, canvas):
        _bar_row(self.w, self.widget_size()[0])
        self.w.animate(_Clock.now)
        self.battery.animate(_Clock.now)
        self.w.paint(canvas)


class WorkspacesCard(Card):
    """Nine workspaces: the current one steps along with the beat, then
    a window on another demands attention and its indicator rings."""

    label = "WORKSPACES"
    max_scale = 9.0

    def __init__(self) -> None:
        self.w = Workspaces()
        self.w._count = 9
        self.w._per_desktop = [2, 1, 3, 0, 1, 4, 0, 2, 1]
        self.w._urgent = [False] * 9
        self.w._current = 0
        self.rang = False

    def widget_size(self):
        return self.w.measure(Size(10_000, theme.BAR_HEIGHT)).width, theme.BAR_HEIGHT

    def drive(self, t, dur, frame, beat):
        if beat and t < 0.55 * dur:
            self.w._current = (self.w._current + 1) % self.w._count
        if not self.rang and t >= 0.55 * dur:
            self.rang = True
            w = self.w
            w._urgent[6] = True
            w._ring_idx, w._ring_acc = 0, 0.0
            w._ring_visible = theme.WORKSPACE_URGENT_RING[0][1]
            w._any_urgent = True

    def paint(self, canvas):
        _bar_row(self.w, self.widget_size()[0])
        self.w.animate(_Clock.now)
        self.w.paint(canvas)


class PanelCard(Card):
    """A panel, the launcher, a menu or the toast stack, as the take
    captured it fully open — real data, real chrome. It flashes in with
    the scan-lock and back out on the next beat; a captured frame only
    scales so far, so it is shown big but briefly."""

    weight = 1.0
    leaves = True
    MAX_H = 1000.0
    _CUBIC = skia.SamplingOptions(skia.CubicResampler.Mitchell())

    def __init__(self, label: str, image: skia.Image) -> None:
        self.label = label
        self.image = image

    def widget_size(self):
        return float(self.image.width()), float(self.image.height())

    @property
    def scale(self) -> float:
        w, h = self.widget_size()
        return min(2.0, MAX_DRAW_W / w, self.MAX_H / h)

    @property
    def strip_scale(self) -> float:
        return self.image.height() * self.scale / theme.BAR_HEIGHT

    def paint(self, canvas):
        canvas.drawImage(self.image, 0, 0, self._CUBIC)


# What to lift out of the take, when: (label, marker, seconds after it,
# a region generously around it — the dark around the panel is trimmed).
PANEL_GRABS = [
    ("LAUNCHER", "nvim", 1.25, 700, 260, 1160, 920),
    ("HARDWARE", "hardware", 2.2, 1880, 760, 680, 636),
    ("NETWORK PANEL", "network", 2.2, 1880, 760, 680, 636),
    ("SYSTEM PANEL", "system", 2.4, 0, 800, 640, 596),
    ("CHORD MENU", "menu_p", 1.3, 2150, 1120, 410, 276),
    ("NOTIFICATIONS", "meter", 1.0, 1860, 480, 700, 916),
]


def grab(video: str, t: float, region: tuple[int, int, int, int]) -> skia.Image:
    """One frame of the take, cropped to `region` and trimmed to what is
    lit in it."""
    x, y, w, h = region
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
         "-vf", f"crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    px = np.frombuffer(raw, np.uint8).reshape(h, w, 4)
    ys, xs = np.nonzero(px[..., :3].max(axis=2) > 28)
    if len(xs):
        pad = 6
        px = px[max(0, ys.min() - pad):ys.max() + pad,
                max(0, xs.min() - pad):xs.max() + pad]
    return skia.Image.fromarray(np.ascontiguousarray(px),
                                colorType=skia.kRGBA_8888_ColorType)


def panel_cards(video: str, markers: dict[str, float]) -> dict[str, PanelCard]:
    """`markers` in video time."""
    cards = {}
    for label, mark, after, *region in PANEL_GRABS:
        if mark in markers:
            cards[label] = PanelCard(label, grab(video, markers[mark] + after,
                                                 tuple(region)))
    return cards


def all_cards(bands, title: str, lines: list[str],
              panels: dict[str, PanelCard] | None = None) -> list[Card]:
    """In order: it opens on the music and ends on a full charge, with the
    panel flashes, when there are any, cut in between the widgets. With
    no lyrics to show, the lyrics card is left out rather than invented."""
    panels = panels or {}
    lyrics = None
    if lines:
        timed = [(0.18, lines[0])] + ([(1.0, lines[1])] if len(lines) > 1 else [])
        lyrics = LyricsCard(timed)
    order = [MediaCard(bands, title), panels.get("LAUNCHER"),
             lyrics, panels.get("HARDWARE"),
             NetworkCard("Wired", "192.168.1.100"), panels.get("NETWORK PANEL"),
             SystemCard(bands), panels.get("SYSTEM PANEL"),
             WorkspacesCard(), panels.get("CHORD MENU"),
             panels.get("NOTIFICATIONS"), BatteryCard()]
    return [c for c in order if c is not None]


def _from_take(markers: list[dict]) -> tuple[str, list[str]]:
    """The song as the take recorded it: playerctl's "artist — title"
    turned round for the media card, and the first lyric lines sptlrx
    reported once it played that fit the lyrics card."""
    music = next((m for m in markers if m["name"] == "music"), None)
    track = (music or {}).get("track", "")
    artist, _, title = track.partition(" — ")
    shown = f"{title} — {artist}" if title else track
    after = music["t"] if music else 0.0
    lines: list[str] = []
    for m in markers:
        if m["name"] == "lyric" and m["t"] > after:
            line = m.get("text", "").strip()
            if line and len(line) <= LYRIC_MAX_CHARS and line not in lines:
                lines.append(line)
        if len(lines) == 2:
            break
    return shown, lines


# ── frame ───────────────────────────────────────────────────────────────
def _ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _paint_label(canvas: skia.Canvas, name: str, t: float, seed: float) -> None:
    """`// NAME` pinned in the top-left corner — outside the camera, so it
    holds still while the widget moves — decoding in on each card."""
    x, baseline = LABEL_X, LABEL_BASELINE
    mark = "//  "
    mark_w = text.measure(mark, LABEL_SIZE, bold=True)
    name_w = text.measure(name, LABEL_SIZE, bold=True, tracking=LABEL_TRACKING)
    asc = -text.font(LABEL_SIZE, True).getMetrics().fAscent
    rect = skia.Rect.MakeXYWH(x, baseline - asc - 4, mark_w + name_w + 8,
                              text.line_height(LABEL_SIZE, True) + 8)

    def draw(c: skia.Canvas) -> None:
        text.draw(c, mark, x, baseline, LABEL_SIZE,
                  theme.color(theme.YELLOW_MID), bold=True)
        text.draw(c, name, x + mark_w, baseline, LABEL_SIZE,
                  theme.color(theme.CYAN_BRIGHT), bold=True,
                  tracking=LABEL_TRACKING)

    appear = t / LABEL_DECODE.duration
    if appear >= 1.0:
        draw(canvas)
    else:
        effects.paint_through(canvas, rect, draw, (LABEL_DECODE,),
                              t=t, appear=max(0.0, appear), seed=seed)


def _scanlines() -> skia.Image:
    surface = skia.Surface(W, H)
    c = surface.getCanvas()
    c.clear(skia.ColorSetARGB(0, 0, 0, 0))
    line = skia.Paint(Color=theme.color(theme.BASE_BLACK, SCANLINE_ALPHA * 4))
    for y in range(0, H, 4):
        c.drawRect(skia.Rect.MakeXYWH(0, y, W, 2), line)
    return surface.makeImageSnapshot()


_SCANLINES = None
_LINEAR = skia.SamplingOptions(skia.FilterMode.kLinear)


def _bloom(layer: skia.Image, strength: float) -> skia.Image:
    """The widget's own light: its pixels blurred at a quarter size."""
    small = _surface(W // BLOOM_DOWN, H // BLOOM_DOWN)
    c = small.getCanvas()
    c.clear(skia.ColorSetARGB(0, 0, 0, 0))
    c.scale(1 / BLOOM_DOWN, 1 / BLOOM_DOWN)
    blur = skia.Paint(ImageFilter=skia.ImageFilters.Blur(
        BLOOM_SIGMA * BLOOM_DOWN, BLOOM_SIGMA * BLOOM_DOWN))
    blur.setAlphaf(min(1.0, strength))
    c.drawImage(layer, 0, 0, _LINEAR, blur)
    small.flushAndSubmit()
    return small.makeImageSnapshot()


def render_card(card: Card, t: float, dur: float, kick: float,
                since_beat: float | None, seed: float) -> skia.Image:
    global _SCANLINES
    if _SCANLINES is None:
        _SCANLINES = _scanlines()

    ww, _ = card.widget_size()
    slot_h = theme.BAR_HEIGHT * card.strip_scale
    draw_w = ww * card.scale
    x0 = (W - draw_w) / 2
    y0 = (H - slot_h) / 2
    cx, cy = W / 2, y0 + slot_h / 2

    # The widget alone, on transparency, through the camera: a push
    # through the card and a kick on every beat.
    zoom = 1.0 + PUSH * _ease(t / dur) + KICK * kick
    layer_surface = _surface(W, H)
    lc = layer_surface.getCanvas()
    lc.clear(skia.ColorSetARGB(0, 0, 0, 0))
    lc.translate(cx, cy)
    lc.scale(zoom, zoom)
    lc.translate(-cx, -cy)
    lc.translate(x0, y0)
    if card.scale != 1.0:
        lc.scale(card.scale, card.scale)
    card.paint(lc)
    layer_surface.flushAndSubmit()
    layer = layer_surface.makeImageSnapshot()

    surface = _surface(W, H)
    canvas = surface.getCanvas()
    canvas.clear(theme.color(BACKDROP))
    add = skia.Paint()
    add.setBlendMode(skia.BlendMode.kPlus)

    # Glow first, under everything: flares with the beat.
    glow = _bloom(layer, BLOOM_ALPHA[0] + BLOOM_ALPHA[1] * kick)
    canvas.save()
    canvas.scale(BLOOM_DOWN, BLOOM_DOWN)
    canvas.drawImage(glow, 0, 0, _LINEAR, add)
    canvas.restore()

    # The echo: the widget swelling out of itself after a beat.
    if since_beat is not None and since_beat < ECHO_S:
        e = since_beat / ECHO_S
        grow = 1 + ECHO_GROW * (1 - (1 - e) ** 3)
        echo = skia.Paint()
        echo.setBlendMode(skia.BlendMode.kPlus)
        echo.setAlphaf(ECHO_ALPHA * (1 - e) ** 2)
        canvas.save()
        canvas.translate(cx, cy)
        canvas.scale(grow, grow)
        canvas.translate(-cx, -cy)
        canvas.drawImage(layer, 0, 0, _LINEAR, echo)
        canvas.restore()

    canvas.drawImage(layer, 0, 0)
    canvas.drawImage(_SCANLINES, 0, 0)

    _paint_label(canvas, card.label, t, seed)
    if t < FLASH_S:
        flash = skia.Paint(Color=theme.color(theme.CYAN_BLOOM, 0.22 * (1 - t / FLASH_S)))
        flash.setBlendMode(skia.BlendMode.kPlus)
        canvas.drawRect(skia.Rect.MakeWH(W, H), flash)
    surface.flushAndSubmit()
    frame = surface.makeImageSnapshot()

    # Post: the entrance (and, for a flash, the exit) and the beat's
    # colour split, chained.
    passes = []
    if t < ENTER.duration:
        passes.append((ENTER, t / ENTER.duration))
    elif card.leaves and dur - t < EXIT_S:
        passes.append((ENTER, max(0.0, (dur - t) / EXIT_S)))
    if since_beat is not None and since_beat < BEAT_SPLIT.duration:
        passes.append((BEAT_SPLIT, since_beat / BEAT_SPLIT.duration))
    if not passes:
        return frame
    shader = frame.makeShader(skia.TileMode.kDecal, skia.TileMode.kDecal,
                              skia.SamplingOptions())
    for effect, appear in passes:
        shader = effect.shader(shader, W, H, t=t, appear=appear, seed=seed)
    out = _surface(W, H)
    paint = skia.Paint(Shader=shader)
    paint.setBlendMode(skia.BlendMode.kSrc)
    out.getCanvas().clear(theme.color(BACKDROP))
    out.getCanvas().drawPaint(paint)
    out.flushAndSubmit()
    return out.makeImageSnapshot()


# ── the reel ────────────────────────────────────────────────────────────
def cut_points(beats: list[float], count: int, fill: float | None = None,
               weights: list[float] | None = None) -> list[float]:
    """Card boundaries. Free-running, each card ends on the first beat
    CARD_MIN..CARD_MAX after it began, or mid-range if none falls there.
    With `fill`, the cards share that many seconds evenly instead, each
    cut moved to the nearest beat, and the last ends exactly at `fill` —
    so a reel spans all the music recorded for it."""
    if fill is not None:
        weights = weights or [1.0] * count
        bounds, acc, total = [0.0], 0.0, sum(weights)
        for w in weights[:-1]:
            acc += w
            share = fill * w / total
            # On the even grid — but never under 85% of the card's share
            # after where the last card really ended, so a cut snapped
            # late cannot starve the next card: a flash has to stay on
            # long enough to read.
            target = max(fill * acc / total, bounds[-1] + 0.85 * share)
            near = min(beats, key=lambda b: abs(b - target), default=target)
            short = near - bounds[-1] < 0.85 * share
            bounds.append(near if abs(near - target) <= 0.3 and not short
                          else target)
        return bounds + [fill]
    bounds, start = [0.0], 0.0
    for _ in range(count):
        end = next((b for b in beats if CARD_MIN <= b - start <= CARD_MAX),
                   start + (CARD_MIN + CARD_MAX) / 2)
        bounds.append(end)
        start = end
    return bounds


def render(cards: list[Card], beats: list[float], video: Path,
           audio_src: str, audio_at: float, stills: Path | None,
           fill: float | None = None) -> float:
    """Render the cards to `video`, with `audio_src` from `audio_at`
    under them. Returns the reel's length."""
    bounds = cut_points(beats, len(cards), fill, [c.weight for c in cards])
    total = bounds[-1]
    frames = int(total * FPS)
    print(f"reel: {len(cards)} cards, cuts at {[round(b, 2) for b in bounds]}, "
          f"{frames} frames")
    enc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-ss", f"{audio_at:.3f}", "-t", f"{total:.3f}", "-i", audio_src,
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium",
         "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-shortest", str(video)], stdin=subprocess.PIPE)
    assert enc.stdin is not None
    beat_i = 0
    still_at = {}
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        for frac in (0.06, 0.4, 0.85):
            still_at[round((a + (b - a) * frac) * FPS)] = f"{i}_{cards[i].label}_{frac}"
    for f in range(frames):
        T = f / FPS
        _Clock.now = T
        card_i = next(i for i in range(len(cards)) if T < bounds[i + 1])
        card, start = cards[card_i], bounds[card_i]
        dur = bounds[card_i + 1] - start
        beat = False
        while beat_i < len(beats) and beats[beat_i] <= T:
            beat, beat_i = True, beat_i + 1
        last = max((b for b in beats if b <= T), default=None)
        since = None if last is None else T - last
        kick = 0.0 if since is None else math.exp(-since / KICK_S)
        card.drive(T - start, dur, f, beat)
        img = render_card(card, T - start, dur, kick, since,
                          seed=card_i * 97.0 + 13.0)
        pixels = img.toarray(colorType=skia.kBGRA_8888_ColorType)
        enc.stdin.write(pixels.tobytes())
        if stills is not None and f in still_at:
            skia.Image.fromarray(pixels, colorType=skia.kBGRA_8888_ColorType).save(
                str(stills / f"{still_at[f]}.png"), skia.kPNG)
    enc.stdin.close()
    enc.wait()
    print(f"reel: {video}")
    return total


def _analyse(audio_src: str, at: float, span: float):
    pcm = audio.samples(audio_src, at, span)
    return audio.beats(pcm), audio.bands(pcm, FPS, 20)


def preview(audio_src: str, at: float, stills: Path | None) -> None:
    random.seed(2077)
    beats, bands = _analyse(audio_src, at, 6 * CARD_MAX + 0.5)
    OUT.mkdir(parents=True, exist_ok=True)
    render(all_cards(bands, PREVIEW_TITLE, PREVIEW_LINES), beats,
           OUT / "reel-preview.mp4", audio_src, at, stills)


def render_take(take: Path, stills: Path | None, cut: float | None = None,
                span: float | None = None) -> None:
    """The reel for a take, cut to the music the take recorded after its
    last frame of desktop — see take.py's REEL_SECONDS.

    `cut` (seconds into the video) moves where the desktop footage ends
    and the reel begins, when the take's own `reel` marker is not the
    best frame to leave on; `span` fixes the reel's length."""
    random.seed(2077)
    meta = json.loads(take.read_text())
    video = meta["video"]
    marks = {m["name"]: m["t"] for m in meta["markers"]}
    length = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video], capture_output=True, text=True,
        check=True).stdout)
    offset = marks["stop"] - length            # recorder start-up delay
    at = cut if cut is not None else marks["reel"] - offset
    beats, bands = _analyse(video, at, length - at)
    title, lines = _from_take(meta["markers"])
    print(f"reel: title {title!r}, lyrics {lines!r}")
    panels = panel_cards(video, {k: v - offset for k, v in marks.items()})
    print(f"reel: panel flashes {list(panels)}")
    out = take.with_name(take.stem + "-reel.mp4")
    # Every second of music recorded for the reel is used: the second
    # chorus lands in it rather than after it.
    if span is None:
        span = round(length - at - 0.3, 2)
    total = render(all_cards(bands, title, lines, panels), beats, out, video,
                   at, stills, fill=span)
    take.with_name(take.stem + "-reel.json").write_text(json.dumps(
        {"video": str(out), "audio_at": round(at, 3), "duration": round(total, 3)},
        indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview")
    p.add_argument("--audio", required=True, help="a take's video, for its music")
    p.add_argument("--at", type=float, required=True, help="seconds into it")
    p.add_argument("--stills", type=Path, help="also save a few frames here")
    r = sub.add_parser("render")
    r.add_argument("take", type=Path, help="the take's .json")
    r.add_argument("--stills", type=Path)
    r.add_argument("--at", type=float, help="cut to the reel here (video s)")
    r.add_argument("--length", type=float, help="the reel's length (s)")
    args = parser.parse_args()
    if args.stills:
        args.stills.mkdir(parents=True, exist_ok=True)
    if args.cmd == "preview":
        preview(args.audio, args.at, args.stills)
    else:
        render_take(args.take, args.stills, args.at, args.length)
