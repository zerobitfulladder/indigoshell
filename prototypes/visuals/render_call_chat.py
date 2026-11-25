"""The call's one chat at 1x, from the real widgets: how full it is, and
paging back through it.

    PYTHONPATH=../.. ../../.venv/bin/python render_call_chat.py

`call_chat.png`, two rows:

- the meter in her heading (context.py): early, past the compact tick,
  near the cap, compacting (the sweep caught mid-run), just compacted
  (the note under the turn, the size not yet measured: nothing lit, no
  number);
- paging (Ctrl+K, scrollback.py): live; one back; three back, a turn the
  hub started (its line a note in her plate); past the oldest she holds
  (earlier is in her memory); and a line said into a running turn (your
  plate shows it, her reply goes on).
"""
import time

import skia

from common import txt
from indigoshell import theme
from indigoshell.agent.activity import Activity
from indigoshell.agent.ui.call import CallHost
from indigoshell.widgets.base import Size, Widget
from render_call import FakeWindow, agent_in, shot

NOW = time.time()
CONTEXT = {"used": 132_000, "compact_at": 200_000, "cap": 500_000,
           "window": 1_000_000, "from": None}


def exchanges():
    """Four earlier exchanges, as the hub keeps them."""
    day = 86400
    return [
        {"id": 811, "at": NOW - day - 3 * 3600, "source": "user",
         "device": "iris", "lines": ["is the pi still hot after the update"],
         "reply": "Cooled off, choom. **52 °C** and falling; the fan curve "
                  "kicked in at 60.", "attached": 0, "shown": []},
        {"id": 812, "at": NOW - 5 * 3600, "source": "hub", "device": None,
         "lines": ["job transcribe talk.mp4 ended: ok"],
         "reply": "Transcript's done: `~/Videos/talk.txt`, 41 minutes, "
                  "clean.", "attached": 0, "shown": []},
        {"id": 813, "at": NOW - 40 * 60, "source": "user",
         "device": "indigo", "lines": ["what's using my disk", "just home"],
         "reply": "Projects is the big one at **21G**. The cache is 1.2G:\n\n"
                  "- `.cache` 1.2G\n- `Videos` 3.4G\n- `Projects` 21G",
         "attached": 0, "shown": []},
        {"id": 814, "at": NOW - 12 * 60, "source": "user",
         "device": "indigo", "lines": ["clear the thumbnails then"],
         "reply": "Flatlined them. 380M back.", "attached": 0, "shown": []},
    ]


def hub(**state):
    """The call's agent as a page of scrollback and a turn on show left
    it, on the line."""
    ctx = state.pop("context", CONTEXT)
    xs = state.pop("exchanges", exchanges())
    edge = state.pop("edge", None)
    defaults = dict(phase="idle", connected=True, device="indigo",
                    model="opus", effort="medium", model_label="opus 5.5",
                    heard="and the downloads folder?", at=NOW - 3 * 60,
                    turn_started=time.monotonic() - 60,
                    activity=[Activity("tool", "$ du -sh ~/Downloads",
                                       result="2.9G")],
                    message="**2.9G** in Downloads, mostly two ISOs from "
                            "March. Want them gone?")
    defaults.update(state)
    a = agent_in(**defaults)
    a.lines = [a.heard] if a.heard else []
    a.context = ctx
    a.exchanges = xs
    a.more_back = edge is None
    a.edge = edge
    a.want_older = lambda: False        # nothing to ask: the page is here
    return a


def call(agent, back: int = 0):
    """The call, paged back `back` times (Ctrl+K)."""
    host = CallHost(agent)
    Widget.attach(host, FakeWindow())
    for _ in range(back):
        if host.pages.older():
            host.agent_plate.turn_page()
    # A still: the streamed reply as decoded, not caught mid-decode.
    host.agent_plate._build()
    reveal = host.agent_plate.reveal
    reveal.arrivals = {k: [0.0] * len(v) for k, v in reveal.arrivals.items()}
    reveal._latest = 0.0
    size = host.measure(Size(10_000, 10_000))
    return shot(host, int(size.width), int(size.height))


def meter_states():
    m = time.monotonic()
    ctx = dict(CONTEXT)
    return [
        ("61k: early", lambda: call(hub(context={**ctx, "used": 61_000}))),
        ("240k: past the tick", lambda: call(hub(context={**ctx, "used": 240_000}))),
        ("470k: near the cap", lambda: call(hub(context={**ctx, "used": 470_000}))),
        ("compacting (the sweep)", lambda: call(hub(
            context={**ctx, "used": 240_000}, compacting=True,
            phase="compacting"))),
        ("compacted: the note; not measured yet", lambda: call(hub(
            context={**ctx, "used": None, "from": 813},
            compacted={"before": 240_000, "after": None, "at": m}))),
    ]


def page_states():
    mid = dict(phase="speaking", busy=True, turn_started=time.monotonic() - 9,
               heard="only the ones from March", at=NOW - 20,
               activity=[Activity("tool", "$ ls -la ~/Downloads",
                                  result="...")],
               message="Two ISOs from March, **2.6G** between them. Clearing")
    return [
        ("live", lambda: call(hub())),
        ("^K once: 1 back", lambda: call(hub(), 1)),
        ("^K x3: a hub turn, 3 back", lambda: call(hub(), 3)),
        ("past the oldest she holds", lambda: call(hub(
            edge="compacted", exchanges=exchanges()[2:]), 3)),
        ("a line said into the turn", lambda: call(hub(**mid))),
    ]


def sheet(path, rows, title):
    gap, top, side = 24, 40, 30
    row_h = [max(img.height() for _, img in r) + top for _, r in rows]
    w = max(sum(img.width() for _, img in r) + gap * (len(r) + 1)
            for _, r in rows) + side
    h = sum(row_h) + gap + 30
    s = skia.Surface(w, h)
    c = s.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    txt(c, title, gap, 24, 14, theme.HUD_FG_BRIGHT, bold=True)
    y = 30
    for (name, r), rh in zip(rows, row_h):
        txt(c, name, 8, y + 26, 13, theme.YELLOW_BRIGHT, bold=True)
        x = gap + side
        for label, img in r:
            txt(c, label, x + 36, y + 26, 13, theme.HUD_FG_BRIGHT, bold=True)
            c.drawImage(img, x, y + top)
            x += img.width() + gap
        y += rh
    s.flushAndSubmit()
    s.makeImageSnapshot().save(path, skia.kPNG)
    print("wrote", path, f"{w}x{h}")


def main():
    sheet("call_chat.png",
          [("C", [(label, fn()) for label, fn in meter_states()]),
           ("^K", [(label, fn()) for label, fn in page_states()])],
          "the chat: how full it is, in her heading (C) · paging back, an "
          "exchange at a time on both plates (Ctrl+K / Ctrl+J)")


if __name__ == "__main__":
    main()
