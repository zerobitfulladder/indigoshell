"""The call as a hologram (widgets/holo.py), rendered offline at 1x
from the real widgets on a clock the frames own.

    gif/call_holo.gif         one full scan period of an open call
    gif/call_holo_status.gif  the hung-up status chip, same treatment
    call_holo.png             stills: mid-scan on dark, and on a bright page
"""

import math
import os
import subprocess
import sys
import time

import skia

NOW = [2000.0]
time.monotonic = lambda: NOW[0]

from common import C  # noqa: E402  (also quiets logging)
from indigoshell.agent.activity import Activity  # noqa: E402
from indigoshell.agent.ui.call import CallHost  # noqa: E402
from indigoshell.widgets.holo import SCAN_EVERY_S  # noqa: E402
from indigoshell.agent.ui.status import CallStatus  # noqa: E402
from indigoshell.agent.ui.style import UNBOUNDED  # noqa: E402
from indigoshell.widgets.base import Size, Widget  # noqa: E402
from render_call import FakeWindow, agent_in, backdrop  # noqa: E402

FPS = 30
MARGIN = 24
BLUR_SIGMA = 9.0
OUT = os.path.dirname(os.path.abspath(__file__))

HEARD = "why does my wifi keep dropping every few minutes?"
REPLY = ("Found it, choom. The card is **fine**; the drops are *power "
         "saving*. `iwlwifi` parks the radio after a few idle minutes and "
         "the AP kicks you each time.\n\n"
         "Turn it off for good:\n\n"
         "```\nsudo iw dev wlp0s20f3 set power_save off\n```\n\n"
         "Details on the [iwd page](https://wiki.archlinux.org/title/Iwd).")


def bright(w, h):
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(C("#f6f7f9"))
    p = skia.Paint()
    for i in range(0, h, 17):
        p.setColor(C("#57606a" if i % 85 else "#1f2328"))
        c.drawRect(skia.Rect.MakeXYWH(20, i + 30, 300 + (i * 37) % 380, 7), p)
    img = skia.Rect.MakeXYWH(w - 360, 40, 320, 200)
    sh = skia.GradientShader.MakeLinear(
        [skia.Point(img.left(), img.top()), skia.Point(img.right(), img.bottom())],
        [C("#4aa3ff"), C("#ffd27a"), C("#ff7a59")])
    c.drawRect(img, skia.Paint(Shader=sh))
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def shot(widget, w, h, back=backdrop):
    """As the screen composes it: the desktop, blurred inside the shapes
    (the compositor's job), and the window's own transparent surface over it —
    which is all the projection can snapshot and tear."""
    W, H = w + 2 * MARGIN, h + 2 * MARGIN
    layer = skia.Surface(W, H)
    lc = layer.getCanvas()
    lc.clear(skia.ColorTRANSPARENT)
    widget.paint(lc)
    layer.flushAndSubmit()
    surface = skia.Surface(W, H)
    c = surface.getCanvas()
    bg = back(W, H)
    c.drawImage(bg, 0, 0)
    c.save()
    c.clipPath(widget.blur_shape(), doAntiAlias=False)
    blur = skia.Paint()
    blur.setImageFilter(skia.ImageFilters.Blur(BLUR_SIGMA, BLUR_SIGMA))
    c.drawImage(bg, 0, 0, skia.SamplingOptions(), blur)
    c.drawImage(layer.makeImageSnapshot(), 0, 0)
    c.restore()
    surface.flushAndSubmit()
    return surface.makeImageSnapshot()


def placed(widget):
    Widget.attach(widget, FakeWindow())
    m = widget.measure(UNBOUNDED)
    w, h = math.ceil(m.width), math.ceil(m.height)
    widget.measure(Size(w, h))
    widget.arrange(skia.Rect.MakeXYWH(MARGIN, MARGIN, m.width, m.height))
    return widget, w, h


def at(widget, t):
    NOW[0] = 2000.0 + t
    widget.projection.animate(NOW[0])


def encode(path, frames, w, h):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
           "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-filter_complex",
           "[0:v]split[a][b];[a]palettegen=stats_mode=full[p];"
           "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
           "-loop", "0", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for img in frames:
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    print(f"wrote {path}: {len(frames)} frames, {w}x{h}")


def main():
    NOW[0] = 2000.0
    agent = agent_in()
    agent.phase, agent.heard, agent.message = "idle", HEARD, REPLY
    agent.activity = [Activity("tool", "$ iw dev wlp0s20f3 link", id=1),
                      Activity("tool", "$ journalctl -k -g iwlwifi", id=2)]
    host, w, h = placed(CallHost(agent))
    frames = []
    for i in range(int(SCAN_EVERY_S * FPS)):
        at(host, i / FPS)
        frames.append(shot(host, w, h))
    encode(f"{OUT}/gif/call_holo.gif", frames, w + 2 * MARGIN, h + 2 * MARGIN)

    # stills: the band partway down, on dark and on bright
    at(host, 1.1)
    dark_img = shot(host, w, h)
    bright_img = shot(host, w, h, bright)
    sheet = skia.Surface(dark_img.width() * 2, dark_img.height())
    c = sheet.getCanvas()
    c.drawImage(dark_img, 0, 0)
    c.drawImage(bright_img, dark_img.width(), 0)
    sheet.flushAndSubmit()
    sheet.makeImageSnapshot().save(f"{OUT}/call_holo.png", skia.kPNG)

    busy = agent_in()
    busy.phase, busy.busy, busy.turn_started = "working", True, 2000.0 - 40
    busy.activity = [Activity("tool", "$ sudo pacman -Syu --noconfirm")]
    st, sw, sh = placed(CallStatus(busy))
    frames = []
    for i in range(int(SCAN_EVERY_S * FPS)):
        at(st, i / FPS)
        st.chip.angle = (st.chip.angle + 120.0 / FPS) % 360.0
        frames.append(shot(st, sw, sh))
    encode(f"{OUT}/gif/call_holo_status.gif", frames,
           sw + 2 * MARGIN, sh + 2 * MARGIN)


if __name__ == "__main__":
    sys.exit(main())
