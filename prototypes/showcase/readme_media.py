"""The README's pictures, from the showcase take and the real widgets.

    PYTHONPATH=../.. python readme_media.py ~/Videos/indigoshell-showcase/take-*.json

Two kinds, written to docs/media/:

  * Footage, cut from the take at its markers — the hero frame, the bar,
    the launcher in use, a panel's scan-lock opening, the panels, a chord
    menu, the notifications worked through, the tray. What a user sees,
    at the size they see it.
  * Widget loops, rendered offline by the bar's real widget classes at
    a few times their size, on the bar's own background — and without
    any of the closing reel's camera moves, glows or splits, so each one
    shows the widget as it actually looks and moves.

Animations are GIFs: GitHub plays them inline, which it does not do for
a video committed to the repo. Each is encoded with a palette of its
own, so flat UI colours stay flat.
"""

import json
import math
import random
import subprocess
import sys
from pathlib import Path

import skia

import audio
import reel
from indigoshell import theme

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "docs" / "media"
FPS_LOOP = 24                       # widget loops
FPS_CLIP = 20                       # footage


# ── footage ─────────────────────────────────────────────────────────────
def _gif(cmd_in: list[str], vf: str, out: Path, fps: int) -> None:
    graph = (f"{vf},fps={fps},split[a][b];"
             "[a]palettegen=stats_mode=diff:max_colors=160[p];"
             "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *cmd_in, "-filter_complex",
                    graph, "-loop", "0", str(out)], check=True)


def clip(video: str, start: float, dur: float, region, out: str,
         width: int | None = None) -> None:
    x, y, w, h = region
    vf = f"crop={w}:{h}:{x}:{y}"
    if width:
        vf += f",scale={width}:-1:flags=lanczos"
    _gif(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", video], vf,
         MEDIA / out, FPS_CLIP)


def still(video: str, t: float, region, out: str, width: int | None = None,
          trim: bool = False) -> skia.Image:
    if trim:
        img = reel.grab(video, t, region)
    else:
        x, y, w, h = region
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video,
             "-frames:v", "1", "-vf", f"crop={w}:{h}:{x}:{y}",
             "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
            capture_output=True, check=True).stdout
        import numpy as np
        img = skia.Image.fromarray(
            np.frombuffer(raw, np.uint8).reshape(h, w, 4).copy(),
            colorType=skia.kRGBA_8888_ColorType)
    if width:
        img = _resize(img, width)
    img.save(str(MEDIA / out), skia.kPNG)
    return img


def _resize(img: skia.Image, width: int) -> skia.Image:
    h = round(img.height() * width / img.width())
    s = skia.Surface(width, h)
    s.getCanvas().drawImageRect(img, skia.Rect.MakeWH(width, h),
                                skia.SamplingOptions(skia.CubicResampler.Mitchell()))
    return s.makeImageSnapshot()


def stack(images: list[skia.Image], out: str, *, horizontal: bool,
          gap: int = 24) -> None:
    """Side by side (or stacked) on the backdrop, bottoms aligned."""
    if horizontal:
        w = sum(i.width() for i in images) + gap * (len(images) + 1)
        h = max(i.height() for i in images) + 2 * gap
    else:
        w = max(i.width() for i in images) + 2 * gap
        h = sum(i.height() for i in images) + gap * (len(images) + 1)
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(theme.color(theme.BASE_BLACK))
    pos = gap
    for img in images:
        if horizontal:
            c.drawImage(img, pos, h - gap - img.height())
            pos += img.width() + gap
        else:
            c.drawImage(img, gap, pos)
            pos += img.height() + gap
    s.makeImageSnapshot().save(str(MEDIA / out), skia.kPNG)


def thumbnail(reel_video: str, at: float, out: str) -> None:
    """A frame of the reel with a play mark, linking to the video."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", reel_video,
         "-frames:v", "1", "-vf", "scale=1280:720:flags=lanczos",
         "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    import numpy as np
    frame = skia.Image.fromarray(np.frombuffer(raw, np.uint8).reshape(720, 1280, 4).copy(),
                                 colorType=skia.kRGBA_8888_ColorType)
    s = skia.Surface(1280, 720)
    c = s.getCanvas()
    c.drawImage(frame, 0, 0)
    # The panels' plate, holding a play triangle.
    from indigoshell import shapes
    pw, ph = 220, 150
    px, py = (1280 - pw) / 2, (720 - ph) / 2 + 150
    plate = shapes.beveled(px, py, pw, ph, bevel=theme.POPUP_BEVEL,
                           corners=theme.POPUP_BEVEL_CORNERS)
    c.drawPath(plate, skia.Paint(AntiAlias=True, Color=theme.color(theme.POPUP_BG)))
    border = skia.Paint(AntiAlias=True, Color=theme.color(theme.POPUP_BORDER),
                        Style=skia.Paint.kStroke_Style, StrokeWidth=3)
    c.drawPath(plate, border)
    tri = skia.Path()
    cx, cy = px + pw / 2 + 8, py + ph / 2
    tri.moveTo(cx - 28, cy - 36)
    tri.lineTo(cx + 36, cy)
    tri.lineTo(cx - 28, cy + 36)
    tri.close()
    c.drawPath(tri, skia.Paint(AntiAlias=True, Color=theme.color(theme.YELLOW_BRIGHT)))
    s.makeImageSnapshot().save(str(MEDIA / out), skia.kPNG)


# ── widget loops ────────────────────────────────────────────────────────
class _Lyrics(reel.LyricsCard):
    S = 2.6

    def __init__(self, lines) -> None:
        super().__init__(lines)
        # Sized for its widest line, not for whatever it shows at t=0
        # (the placeholder), or every later line is clipped.
        widest = 0.0
        for _, line in lines:
            probe = reel.LyricsCard.__new__(_Lyrics)
            reel.LyricsCard.__init__(probe, [])
            probe.w._on_line(line)
            widest = max(widest, reel.LyricsCard.widget_size(probe)[0])
        self.widest = widest

    def widget_size(self):
        w, h = super().widget_size()
        return max(w, self.widest), h


class _System(reel.SystemCard):
    """Loops cleanly: RAM and TEMP swing and come back, CPU rides the music."""

    def drive(self, t, dur, frame, beat):
        phase = math.sin(2 * math.pi * t / dur)
        low = float(self.bands[min(frame, len(self.bands) - 1)][:4].mean())
        self.v["cpu"] = 6 + 88 * low ** 1.6
        self.v["ram"] = 52 + 12 * phase
        self.v["temp"] = 62 + 26 * phase


def widget_loop(card: reel.Card, scale: float, seconds: float, beats, out: str,
                pad: int = 18) -> None:
    w, h = card.widget_size()
    native = card.native
    draw_w = w if native else w * scale
    slot_h = theme.BAR_HEIGHT * (card.S if native else scale)
    W, H = int(draw_w + 2 * pad) // 2 * 2, int(slot_h + 2 * pad) // 2 * 2
    frames = int(seconds * FPS_LOOP)
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
         "-s", f"{W}x{H}", "-r", str(FPS_LOOP), "-i", "-",
         "-filter_complex",
         "split[a][b];[a]palettegen=stats_mode=diff:max_colors=128[p];"
         "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
         "-loop", "0", str(MEDIA / out)], stdin=subprocess.PIPE)
    assert enc.stdin is not None
    beat_i = 0
    for f in range(frames):
        t = f / FPS_LOOP
        reel._Clock.now = t
        beat = False
        while beat_i < len(beats) and beats[beat_i] <= t:
            beat, beat_i = True, beat_i + 1
        card.drive(t, seconds, f * 60 // FPS_LOOP, beat)
        s = skia.Surface(W, H)
        c = s.getCanvas()
        c.clear(theme.color(theme.BAR_BG))
        c.translate(pad, pad)
        if not native:
            c.scale(scale, scale)
        card.paint(c)
        enc.stdin.write(s.makeImageSnapshot().toarray(
            colorType=skia.kBGRA_8888_ColorType).tobytes())
    enc.stdin.close()
    enc.wait()


# ── all of it ───────────────────────────────────────────────────────────
def main(take: Path) -> None:
    random.seed(2077)
    MEDIA.mkdir(parents=True, exist_ok=True)
    meta = json.loads(take.read_text())
    video = meta["video"]
    length = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video], capture_output=True, text=True,
        check=True).stdout)
    raw = {m["name"]: m["t"] for m in meta["markers"] if m["name"] != "lyric"}
    offset = raw["stop"] - length
    at = {k: v - offset for k, v in raw.items()}      # video time

    print("footage…")
    still(video, at["hardware"] + 3.0, (0, 0, 2560, 1440), "hero.png", width=1600)
    bar_t = at["meter"] + 0.5
    left = still(video, bar_t, (0, 1398, 1280, 42), "bar-left.png")
    right = still(video, bar_t, (1280, 1398, 1280, 42), "bar-right.png")
    stack([left, right], "bar.png", horizontal=False, gap=6)
    for name in ("bar-left.png", "bar-right.png"):
        (MEDIA / name).unlink()
    clip(video, at["nvim"] + 0.45, 1.5, (840, 360, 880, 720), "launcher.gif",
         width=560)
    clip(video, at["hardware"] - 0.25, 1.9, (1880, 760, 680, 636),
         "panel-open.gif", width=480)
    panels = [reel.grab(video, at["hardware"] + 2.2, (1880, 760, 680, 636)),
              reel.grab(video, at["network"] + 2.2, (1880, 760, 680, 636)),
              reel.grab(video, at["system"] + 2.4, (0, 800, 640, 596))]
    stack(panels, "panels.png", horizontal=True)
    clip(video, at["menu_p"], 2.3, (2120, 1100, 440, 296), "menu.gif")
    clip(video, at["hover"] - 1.2, 6.6, (2030, 1000, 530, 396),
         "notifications.gif")
    still(video, at["tray"] + 1.6, (2100, 1000, 460, 396), "tray.png",
          trim=True, width=340)

    print("widget loops…")
    pcm = audio.samples(video, at["music"] + 9.0, 8.0)
    beats, bands = audio.beats(pcm), audio.bands(pcm, 60, 20)
    title = "Resist and Disorder — Rezodrone, The Cartesian Duelists"
    net = reel.NetworkCard("Wired", "192.168.1.100")
    net.steps = []
    net.w._f_up, net.w._f_down = 1.0, 3.0
    loops = [
        (reel.MediaCard(bands, title), 2.6, 5.0, "widget-media.gif"),
        (_Lyrics([(0.25, "Rise up! Resist and disorder"),
                  (2.6, "Nowhere to run, it's all undone")]), 1.0, 5.0,
         "widget-lyrics.gif"),
        (net, 4.0, 2.0, "widget-network.gif"),
        (_System(bands), 2.6, 4.0, "widget-meters.gif"),
        (reel.WorkspacesCard(), 4.0, 4.0, "widget-workspaces.gif"),
        (reel.BatteryCard(), 3.2, 4.0, "widget-battery.gif"),
    ]
    for card, scale, seconds, out in loops:
        widget_loop(card, scale, seconds, beats, out)

    print("thumbnail…")
    reel_meta = json.loads(take.with_name(take.stem + "-reel.json").read_text())
    thumbnail(reel_meta["video"], 1.0, "showcase.png")

    for f in sorted(MEDIA.iterdir()):
        print(f"  {f.name:24} {f.stat().st_size / 1024:7.0f} KB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} TAKE.json")
    main(Path(sys.argv[1]).expanduser())
