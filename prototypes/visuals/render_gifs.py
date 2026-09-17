"""Animated previews of the panel transitions, 30fps, looping.

Per effect: open -> hold -> close -> pause, timed exactly as the window
would drive it (appear by wall clock over the effect's duration). Frames
are piped raw into ffmpeg, which builds a palette from the whole clip.

    PYTHONPATH=../.. python render_gifs.py
"""
import re
import subprocess

import skia

from common import C, fill_rect, txt
from indigoshell import theme
from render_panels import EFFECTS, desktop_bg, run_effect, snapshot_panel

FPS = 30
HOLD = 0.7        # seconds open at rest
PAUSE = 0.45      # seconds closed before the loop restarts


def timeline(duration):
    """(t, appear) per frame for one open/hold/close/pause cycle."""
    frames = []
    n_open = int(round(duration * FPS)) + 1
    for i in range(n_open):
        frames.append((i / FPS, min(1.0, i / FPS / duration)))
    for i in range(int(HOLD * FPS)):
        frames.append((frames[-1][0] + 1 / FPS, 1.0))
    t0 = frames[-1][0] + 1 / FPS
    for i in range(n_open):
        frames.append((t0 + i / FPS, max(0.0, 1.0 - i / FPS / duration)))
    for i in range(int(PAUSE * FPS)):
        frames.append((frames[-1][0] + 1 / FPS, 0.0))
    return frames


def frame_image(panel, effect, t, appear, bg):
    w, h = panel.width(), panel.height()
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.drawImage(bg, 0, 0)
    if appear >= 0.999:
        c.drawImage(panel, 0, 0)
    elif appear > 0.001:
        c.drawImage(run_effect(panel, effect, appear, t), 0, 0)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def encode(path, frames_iter, w, h):
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(FPS),
           "-i", "-",
           "-filter_complex",
           "[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];"
           "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
           "-loop", "0", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = 0
    for img in frames_iter:
        p.stdin.write(img.tobytes())
        n += 1
    p.stdin.close()
    p.wait()
    print(f"wrote {path}: {n} frames, {w}x{h}, {n / FPS:.2f}s loop")


def slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.split("|")[0].lower()).strip("_")


if __name__ == "__main__":
    panel = snapshot_panel()
    w, h = panel.width(), panel.height()
    bg = desktop_bg(w, h)

    # one gif per effect
    for label, fx in EFFECTS:
        frames = (frame_image(panel, fx, t, a, bg) for t, a in timeline(fx.duration))
        encode(f"gif/{slug(label)}.gif", frames, w, h)

    # all six on one shared timeline: open together, close together
    longest = max(fx.duration for _, fx in EFFECTS)
    t_close = longest + HOLD
    t_end = t_close + longest + PAUSE
    cols, lab_h = 3, 22
    gw, gh = cols * w, ((len(EFFECTS) + cols - 1) // cols) * (h + lab_h)

    def grid_frames():
        n = int(round(t_end * FPS))
        for i in range(n):
            t = i / FPS
            s = skia.Surface(gw, gh)
            c = s.getCanvas()
            c.clear(skia.ColorSetARGB(255, 28, 28, 34))
            for k, (label, fx) in enumerate(EFFECTS):
                if t < t_close:
                    appear = min(1.0, t / fx.duration)
                else:
                    appear = max(0.0, 1.0 - (t - t_close) / fx.duration)
                x = (k % cols) * w
                y = (k // cols) * (h + lab_h)
                txt(c, label.split("|")[0], x + 8, y + 15, 12,
                    theme.HUD_FG_BRIGHT, bold=True)
                c.drawImage(frame_image(panel, fx, t, appear, bg), x, y + lab_h)
            s.flushAndSubmit()
            yield s.makeImageSnapshot()
    encode("gif/panel_all.gif", grid_frames(), gw, gh)
