"""The call overlay at 1x, from the real widgets, with faked agent state.

Each state is painted over a stand-in desktop, with the plates' outline
blurred the way picom blurs it live (`blur_shape()` clips a blurred copy
of the backdrop), so the frosted look can be judged here.

    PYTHONPATH=../.. ../../.venv/bin/python render_call.py
"""
import time

import skia

from common import C, txt
from indigoshell import theme
from indigoshell.agent.activity import Activity, Approval
from indigoshell.agent.remote import RemoteAgent
from indigoshell.widgets.base import Size, Widget
from indigoshell.agent.ui.call import CallHost
from indigoshell.agent.ui.status import CallStatus

MARGIN = 36
BLUR_SIGMA = 9.0


class FakeSpec:
    name = "call"


class FakeWindow:
    spec = FakeSpec()
    _phase = "idle"
    loop = None         # no frame clock to wake: the renderer drives time

    def damage(self, widget=None, *, layout=False):
        pass

    def damage_rect(self, rect):
        pass

    def resize_content(self, w, h):
        pass

    def reshape(self):
        pass


def backdrop(w, h):
    """Something busy to blur: a dark editor-ish desktop with neon."""
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(C("#0b0a14"))
    p = skia.Paint(AntiAlias=True)
    for i, col in enumerate((theme.CYAN_MID, theme.MAGENTA_MID,
                             theme.YELLOW_MID, theme.VIOLET_BRIGHT)):
        p.setColor(C(col, 0.85))
        c.drawRect(skia.Rect.MakeXYWH(20 + i * 170, 30 + (i % 2) * 60,
                                      120, 26), p)
    for row in range(40):
        p.setColor(C((theme.HUD_FG, theme.CYAN_BRIGHT, theme.MAGENTA_BRIGHT,
                      theme.LIME_MID)[row % 4], 0.7))
        c.drawRect(skia.Rect.MakeXYWH(30 + (row * 37) % 90, 120 + row * 16,
                                      180 + (row * 53) % 420, 7), p)
    s.flushAndSubmit()
    return s.makeImageSnapshot()


def shot(host, w, h):
    surface = skia.Surface(w + 2 * MARGIN, h + 2 * MARGIN)
    c = surface.getCanvas()
    back = backdrop(w + 2 * MARGIN, h + 2 * MARGIN)
    c.drawImage(back, 0, 0)
    host.measure(Size(w, h))
    host.arrange(skia.Rect.MakeXYWH(MARGIN, MARGIN, w, h))
    c.save()
    c.clipPath(host.blur_shape(), doAntiAlias=False)
    blur = skia.Paint()
    blur.setImageFilter(skia.ImageFilters.Blur(BLUR_SIGMA, BLUR_SIGMA))
    c.drawImage(back, 0, 0, skia.SamplingOptions(), blur)
    host.paint(c)
    c.restore()
    surface.flushAndSubmit()
    return surface.makeImageSnapshot()


def agent_in(**state) -> RemoteAgent:
    """The hub's agent as the call reads it, never connected (its line
    isn't started; the endpoints and certificates are stand-ins), with
    `state` set on it as if the hub's events had left it so. Two keys
    are the hub's own: `approval`, its open question, and `model_label`,
    the model as its `state` names it ("opus 5.5")."""
    a = RemoteAgent(endpoints=["wss://hub.invalid:443"],
                    tls={"ca": "hub-ca.pem", "cert": "device.pem",
                         "key": "device.key"})
    a._hub_ask = state.pop("approval", None)
    a._label = state.pop("model_label", "")
    for k, v in state.items():
        setattr(a, k, v)
    return a


def call(agent, typed="", spin=0.0, expand=(), select=None):
    host = CallHost(agent)
    Widget.attach(host, FakeWindow())
    if typed:
        host.you_plate.input.set_text(typed)
    host.agent_chip.angle = -90 + spin
    host.agent_plate.expanded = set(expand)
    if select:
        host.agent_plate.anchor, host.agent_plate.focus = select
    size = host.measure(Size(10_000, 10_000))
    return shot(host, int(size.width), int(size.height))


def status(agent, spin=0.0):
    widget = CallStatus(agent)
    Widget.attach(widget, FakeWindow())
    widget.chip.angle = -90 + spin
    size = widget.measure(Size(10_000, 10_000))
    return shot(widget, int(size.width), int(size.height))


def main():
    now = time.monotonic()

    class Done:
        def done(self):
            return False

    shots = [
        ("answered: on the line", call(agent_in(phase="idle"))),
        ("working, after your line", call(agent_in(
            phase="working", busy=True, turn_started=now - 41,
            heard="why does my wifi keep dropping every few minutes",
            activity=[Activity("note", "Checking the driver and the logs first."),
                      Activity("tool", "$ journalctl -b -u NetworkManager --since -1h"),
                      Activity("tool", "$ iw dev wlp0s20f3 link")]), spin=55)),
        ("thinking", call(agent_in(
            phase="thinking", busy=True, turn_started=now - 6,
            heard="is anything eating my battery right now",
            thought="Looking at powertop's top consumers before guessing."), spin=20)),
        ("markdown reply, blinking: done", call(agent_in(
            phase="idle", busy=False, ringing="done", ring_started=now,
            heard="why does my wifi keep dropping every few minutes",
            activity=[Activity("tool", "$ iw dev wlp0s20f3 link",
                               detail="iw dev wlp0s20f3 link", result="Connected"),
                      Activity("tool", "$ journalctl -k -g iwlwifi",
                               detail="journalctl -k -g iwlwifi", result="...")],
            message=("## Power saving\nCopy that. The card is **fine**; the drops "
                     "are *power saving*. `iwlwifi` parks the radio after idle "
                     "minutes and the AP drops you.\n\n- happens every ~4 min\n"
                     "- only on battery\n\nTurn it off for this connection:\n"
                     "```\nnmcli connection modify Home 802-11-wireless.powersave 2\n```")),
            typed="do it")),
        ("expanded step, text selected", call(agent_in(
            phase="idle", busy=False,
            heard="what's using my disk",
            activity=[Activity("tool", "$ du -xh -d1 ~ | sort -h | tail -n 5", id=901,
                               detail="du -xh -d1 ~ | sort -h | tail -n 5\n# biggest folders in home",
                               result="1.2G    /home/user/.cache\n3.4G    /home/user/Videos\n"
                                      "8.9G    /home/user/.local\n21G    /home/user/Projects\n35G    /home/user"),
                      Activity("tool", "$ rm -rf ~/.cache/thumbnails", id=902,
                               detail="rm -rf ~/.cache/thumbnails",
                               result="The user declined this over the call.", failed=True)],
            message="Projects is the big one at **21G**. The cache is 1.2G; clearing thumbnails needs your ok."),
            expand=(901,), select=(("m0", 0), ("m0", 24)))),
        ("asking", call(agent_in(
            phase="asking", busy=True, turn_started=now - 88,
            heard="do it",
            activity=[Activity("note", "Switching power saving off for Home.")],
            approval=Approval(tool="Bash", title="Run a command",
                              detail="nmcli connection modify Home 802-11-wireless.powersave 2",
                              future=Done())))),
        ("not signed in", call(agent_in(
            phase="error",
            error="The agent's Claude account is not signed in.",
            hint="ssh -t lilac lilac/.venv/bin/lilac login"))),
    ]
    small = [
        ("hung up, working", status(agent_in(
            phase="working", busy=True, turn_started=now - 132,
            activity=[Activity("tool", "$ sudo pacman -Syu --noconfirm")]), spin=70)),
        ("ringing back", status(agent_in(
            phase="idle", ringing="done", ring_started=now,
            message="Done. 214 packages updated, nothing needs a reboot."), spin=10)),
        ("needs your ok", status(agent_in(
            phase="asking", busy=True, turn_started=now - 20, ringing="ask",
            ring_started=now,
            approval=Approval(tool="Bash", title="Run a command",
                              detail="sudo systemctl restart bluetooth",
                              future=Done())))),
        ("missed call", status(agent_in(
            phase="idle", ringing="done", ring_started=now - 300,
            message="Done. 214 packages updated, nothing needs a reboot."))),
    ]

    gap, top = 24, 40
    cols = 3
    rows = [shots[i:i + cols] for i in range(0, len(shots), cols)] + [small]
    row_h = [max(img.height() for _, img in r) + top for r in rows]
    w = max(sum(img.width() for _, img in r) + gap * (len(r) + 1) for r in rows)
    h = sum(row_h) + gap
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    y = 0
    for r, rh in zip(rows, row_h):
        x = gap
        for label, img in r:
            txt(c, label, x + MARGIN, y + 26, 13, theme.HUD_FG_BRIGHT, bold=True)
            c.drawImage(img, x, y + top)
            x += img.width() + gap
        y += rh
    s.flushAndSubmit()
    s.makeImageSnapshot().save("call.png", skia.kPNG)
    print("wrote call.png", f"{w}x{h}")


if __name__ == "__main__":
    main()
