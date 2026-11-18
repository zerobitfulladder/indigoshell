"""The README's pictures of the call, rendered offline by the real call
widgets on a clock the frames own.

    docs/media/call.gif          a whole call: typed, worked, answered,
                                 then the hologram's scan glitching down it
    docs/media/call-ask.png      a command that needs your ok
    docs/media/call-music.png    asked for music
    docs/media/call-status.gif   hung up: the status chip working, then
                                 calling back

The desktop behind is drawn here too — a neon city at night — and
frosted inside the plates the way picom blurs what is behind them. The
call is painted onto a transparent layer of its own, as the live window
is, so the scan tears only the call.

    cd prototypes/visuals
    PYTHONPATH=../.. ../../.venv/bin/python readme_call.py [call ask music status]
"""

import asyncio
import math
import os
import random
import subprocess
import sys
import tempfile
import time

os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp()    # never the real session

import skia  # noqa: E402

NOW = [5000.0]
time.monotonic = lambda: NOW[0]

from common import C  # noqa: E402  (also quiets logging)
from indigoshell.agent.activity import Activity, Approval, describe_tool  # noqa: E402
from indigoshell.agent.session import Agent  # noqa: E402
from indigoshell.agent.ui.call import CallHost  # noqa: E402
from indigoshell.agent.ui.holo import SCAN_EVERY_S  # noqa: E402
from indigoshell.agent.ui.status import CallStatus  # noqa: E402
from indigoshell.agent.ui.style import UNBOUNDED  # noqa: E402
from indigoshell.widgets.base import Size, Widget  # noqa: E402
from render_call import FakeWindow  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "media"))
FPS = 25
MARGIN = 40
BLUR_SIGMA = 9.0

# ── the desktop: a neon city at night ───────────────────────────────────
_CITY: dict[tuple[int, int], skia.Image] = {}


def city(w: int, h: int) -> skia.Image:
    hit = _CITY.get((w, h))
    if hit is not None:
        return hit
    rnd = random.Random(7)
    s = skia.Surface(w, h)
    c = s.getCanvas()
    sky = skia.GradientShader.MakeLinear(
        [skia.Point(0, 0), skia.Point(0, h)],
        [C("#07040f"), C("#160827"), C("#3b0d45"), C("#5a1450")],
        [0.0, 0.45, 0.8, 1.0])
    c.drawRect(skia.Rect.MakeWH(w, h), skia.Paint(Shader=sky))
    # a haze of neon low on the horizon
    for x, col in ((w * 0.2, "#ff2a6d"), (w * 0.62, "#05d9e8"),
                   (w * 0.9, "#b967ff")):
        glow = skia.Paint(AntiAlias=True)
        glow.setColor(C(col, 0.22))
        glow.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 60))
        c.drawOval(skia.Rect.MakeXYWH(x - 180, h * 0.62, 360, 220), glow)

    def towers(base, lo, hi, color, lit):
        x = -20
        p = skia.Paint(AntiAlias=False)
        while x < w:
            bw = rnd.randint(36, 110)
            bh = rnd.uniform(lo, hi) * h
            top = h - bh
            p.setColor(C(color))
            c.drawRect(skia.Rect.MakeXYWH(x, top, bw, bh), p)
            if rnd.random() < 0.3:      # an antenna
                c.drawRect(skia.Rect.MakeXYWH(x + bw / 2, top - 26, 2, 26), p)
            for wy in range(int(top) + 8, h, 7):
                for wx in range(int(x) + 5, int(x + bw) - 4, 6):
                    if rnd.random() < lit:
                        col = rnd.choice(("#05d9e8", "#ff2a6d", "#f9f871",
                                          "#b967ff", "#d1f7ff"))
                        p.setColor(C(col, rnd.uniform(0.25, 0.8)))
                        c.drawRect(skia.Rect.MakeXYWH(wx, wy, 2, 2), p)
            x += bw + rnd.randint(2, 14)

    towers(h, 0.35, 0.7, "#120a22", 0.10)
    towers(h, 0.2, 0.5, "#09050f", 0.16)
    # neon signs on the near towers
    for _ in range(9):
        r = skia.Rect.MakeXYWH(rnd.uniform(0, w - 60), rnd.uniform(h * 0.45, h - 40),
                               rnd.choice((10, 14, 46, 70)), rnd.choice((10, 34, 60)))
        col = rnd.choice(("#ff2a6d", "#05d9e8", "#b967ff", "#f9f871"))
        glow = skia.Paint(AntiAlias=True)
        glow.setColor(C(col, 0.5))
        glow.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 8))
        c.drawRect(r, glow)
        line = skia.Paint(AntiAlias=True)
        line.setStyle(skia.Paint.kStroke_Style)
        line.setStrokeWidth(2)
        line.setColor(C(col, 0.95))
        c.drawRect(r, line)
    # rain
    rain = skia.Paint(AntiAlias=True)
    rain.setStrokeWidth(1)
    for _ in range(int(w * h / 900)):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        rain.setColor(C("#d1f7ff", rnd.uniform(0.04, 0.12)))
        c.drawLine(x, y, x - 3, y + rnd.uniform(10, 22), rain)
    s.flushAndSubmit()
    img = s.makeImageSnapshot()
    _CITY[(w, h)] = img
    return img


# ── frames ──────────────────────────────────────────────────────────────
def frame(widget, W: int, H: int) -> skia.Image:
    layer = skia.Surface(W, H)
    lc = layer.getCanvas()
    lc.clear(skia.ColorTRANSPARENT)
    widget.paint(lc)
    layer.flushAndSubmit()
    out = skia.Surface(W, H)
    c = out.getCanvas()
    bg = city(W, H)
    c.drawImage(bg, 0, 0)
    c.save()
    c.clipPath(widget.blur_shape(), doAntiAlias=True)
    blur = skia.Paint()
    blur.setImageFilter(skia.ImageFilters.Blur(BLUR_SIGMA, BLUR_SIGMA))
    c.drawImage(bg, 0, 0, skia.SamplingOptions(), blur)
    c.drawImage(layer.makeImageSnapshot(), 0, 0)
    c.restore()
    out.flushAndSubmit()
    return out.makeImageSnapshot()


def walk(widget):
    yield widget
    for child in widget.children():
        yield from walk(child)


def tick(widget) -> None:
    """What the frame clock does: animate everything that asks, and
    once more when it stops asking (`Window._tick_widgets`)."""
    for w in walk(widget):
        moving = bool(w.animation_fps)
        if moving or w._animating:
            w.animate(NOW[0])
        w._animating = moving


def place(widget, x: float = MARGIN, y: float = MARGIN):
    Widget.attach(widget, FakeWindow())
    m = widget.measure(UNBOUNDED)
    widget.measure(Size(m.width, m.height))
    widget.arrange(skia.Rect.MakeXYWH(x, y, m.width, m.height))
    return widget


def encode(path: str, frames: list[skia.Image]) -> None:
    w, h = frames[0].width(), frames[0].height()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
           "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-filter_complex",
           "[0:v]split[a][b];[a]palettegen=stats_mode=diff:max_colors=256[p];"
           "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
           "-loop", "0", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for img in frames:
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    print(f"wrote {path}: {len(frames)} frames, {w}x{h}, "
          f"{os.path.getsize(path) / 1e6:.2f} MB")


def crop(img: skia.Image, w: int, h: int) -> skia.Image:
    s = skia.Surface(w, h)
    s.getCanvas().drawImage(img, 0, 0)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def save(path: str, img: skia.Image) -> None:
    img.save(path, skia.kPNG)
    print(f"wrote {path}: {img.width()}x{img.height()}")


def agent() -> Agent:
    a = Agent(name="INDIGO", model="opus", effort="medium")
    a.phase = "idle"
    return a


def bash(command: str, result: str = "", id: int = 0) -> Activity:
    return Activity("tool", describe_tool("Bash", {"command": command}),
                    detail=command, result=result, id=id)


def tool(name: str, args: dict, result: str = "", id: int = 0) -> Activity:
    return Activity("tool", describe_tool(name, args), detail=str(args),
                    result=result, id=id)


# ── the scenes ──────────────────────────────────────────────────────────
ASKED = "deck feels sluggish today. what's eating it?"
REPLY = (
    "Copy that, choom. Jacked in and found the leech: a **Discord renderer** "
    "is pinning two cores at 180% and sitting on 3.1 GiB of RAM, "
    "and has been since 14:02.\n\n"
    "Flatline it, then reopen Discord:\n\n"
    "```\npkill -f 'discord.*--type=renderer'\n```\n\n"
    "Rest of the deck is preem: no failed units, no kernel errors since "
    "boot, temps at 52°C.")


def scene_call() -> None:
    """Typed, worked, answered — then the scan glitches down the call."""
    NOW[0] = 5000.0
    a = agent()
    host = place(CallHost(a))
    W = int(host.rect.width()) + 2 * MARGIN
    H = 600
    frames = []
    t0 = NOW[0]

    def at(t: float) -> None:
        NOW[0] = t0 + t

    tallest = [0.0]

    def run(until: float, step=None) -> None:
        while NOW[0] - t0 < until:
            if step:
                step(NOW[0] - t0)
            host._on_agent("update")
            tick(host)
            tallest[0] = max(tallest[0], host._natural())
            frames.append(frame(host, W, H))
            NOW[0] += 1.0 / FPS

    # The scan's passes, lined up so one runs down the finished answer.
    end = 15.5
    host.projection.epoch = t0 + 11.0 - SCAN_EVERY_S * 2

    run(0.8)
    typing = (0.8, 2.9)

    def type_(t):
        k = int(len(ASKED) * min(1.0, (t - typing[0]) / (typing[1] - typing[0])))
        host.you_plate.input.set_text(ASKED[:k])
    run(typing[1] + 0.35, type_)

    # Return
    host.you_plate.input.set_text("")
    a.heard, a.busy, a.phase, a.turn_started = ASKED, True, "thinking", NOW[0]
    a.thought = "Checking what is hogging the CPU and memory"
    host.you_chip.kick()
    host.you_plate.comet.fire()
    run(4.0)
    steps = [
        (4.0, bash("ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -6",
                   "PID COMMAND %CPU %MEM\n4182 Discord 180 9.8", 1)),
        (4.7, bash("systemctl --failed --no-legend", "", 2)),
        (5.4, bash("journalctl -p err -b --no-pager | tail -5",
                   "-- No entries --", 3)),
        (6.1, bash("sensors | grep -i package", "Package id 0:  +52.0°C", 4)),
    ]
    for when, act in steps:
        run(when)
        a.phase, a.thought = "working", ""
        a.activity.append(act)
    run(6.9)
    # the answer streams in, a few characters at a time
    a.phase = "speaking"
    stream_from, cps = NOW[0] - t0, 95.0

    def stream(t):
        a.message = REPLY[:int((t - stream_from) * cps)]
    run(stream_from + len(REPLY) / cps + 0.1, stream)
    a.message = REPLY
    a.busy, a.phase = False, "idle"
    run(end)
    # As tall as the call got, and no taller.
    h = min(H, int(math.ceil(tallest[0])) + 2 * MARGIN)
    encode(f"{OUT}/call.gif", [crop(f, W, h) for f in frames])


def scene_ask() -> None:
    NOW[0] = 7000.0
    a = agent()
    a.heard = "patch the deck. everything."
    a.busy, a.phase, a.turn_started = True, "working", NOW[0] - 9
    a.activity.extend([
        bash("checkupdates | wc -l", "143", 1),
        bash("checkupdates | grep -E '^(linux|nvidia)'",
             "linux 7.2.10.arch1-1 -> 7.2.11.arch1-1", 2),
    ])
    a.message = ("143 packages waiting, a new kernel among them. "
                 "That's root work, so the ICE wants your say-so.")
    loop = asyncio.new_event_loop()
    a.approval = Approval(tool="Bash", title="Run a command",
                          detail="sudo pacman -Syu --noconfirm",
                          suggestions=[], future=loop.create_future())
    a.ring_started = NOW[0] - 0.05      # the blink's lit half
    host = place(CallHost(a))
    host._on_agent("update")
    host.projection.epoch = NOW[0] - 4.0        # between passes
    tick(host)
    W, H = int(host.rect.width()) + 2 * MARGIN, int(host._natural()) + 2 * MARGIN
    save(f"{OUT}/call-ask.png", frame(host, W, H))


def scene_music() -> None:
    NOW[0] = 9000.0
    a = agent()
    a.heard = "put on something that sounds like rain on neon"
    a.activity.extend([
        tool("mcp__shell__spotify_search",
             {"query": "vangelis blade runner blues"}, "", 1),
        tool("mcp__shell__spotify_play", {"uri": ""}, "", 2),
        tool("mcp__shell__spotify_search",
             {"query": "vangelis tears in rain"}, "", 3),
        tool("mcp__shell__spotify_queue", {}, "", 4),
    ])
    a.message = ("Jacking it in. **Blade Runner Blues** by Vangelis is on "
                 "the deck, with *Tears in Rain* queued up behind it.\n\n"
                 "Lights down, choom. Rain's on me.")
    host = place(CallHost(a))
    host._on_agent("update")
    host.projection.epoch = NOW[0] - 4.0
    tick(host)
    W, H = int(host.rect.width()) + 2 * MARGIN, int(host._natural()) + 2 * MARGIN
    save(f"{OUT}/call-music.png", frame(host, W, H))


def scene_status() -> None:
    """Hung up: the chip works through the update, then calls back."""
    NOW[0] = 11000.0
    a = agent()
    a.heard = "patch the deck. everything."
    a.busy, a.phase, a.turn_started = True, "working", NOW[0] - 74
    a.activity.append(bash("sudo pacman -Syu --noconfirm", "", 1))
    st = place(CallStatus(a), MARGIN, MARGIN)
    W = int(st.rect.width()) + 2 * MARGIN
    H = int(st.rect.height()) + 2 * MARGIN
    st.projection.epoch = NOW[0] - SCAN_EVERY_S + 1.2
    frames = []
    t0 = NOW[0]
    acts = [(1.6, bash("pacman -Q linux", "linux 7.2.11.arch1-1", 2)),
            (2.6, bash("mkinitcpio -P", "==> Image generation successful", 3))]
    while NOW[0] - t0 < 7.5:
        t = NOW[0] - t0
        if acts and t >= acts[0][0]:
            a.activity.append(acts.pop(0)[1])
            st._on_agent("update")
        if a.busy and t >= 3.6:
            a.message = ("All 143 in, kernel 7.2.11 included. Reboot when "
                         "you're ready, choom.")
            a.busy, a.phase = False, "idle"
            a.ringing, a.ring_started = "done", NOW[0]
            st._on_agent("update")
        tick(st)
        frames.append(frame(st, W, H))
        NOW[0] += 1.0 / FPS
    encode(f"{OUT}/call-status.gif", frames)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    which = set(sys.argv[1:]) or {"call", "ask", "music", "status"}
    if "ask" in which:
        scene_ask()
    if "music" in which:
        scene_music()
    if "status" in which:
        scene_status()
    if "call" in which:
        scene_call()
    return 0


if __name__ == "__main__":
    sys.exit(main())
