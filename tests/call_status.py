"""The hung-up call's status chip: working, ringing, missed — on its
first open and on its second.

    .venv/bin/python tests/call_status.py

While the agent works in silence the trace and the chip must keep their
frames; when the work is done the chip rings, then goes dim as the plate
says "missed call"; and nothing may be left on screen from any of it
(`_live.stale`).
"""

import asyncio
import sys

import _live

from indigoshell.agent.ui import status
from indigoshell.agent.ui.status import CallStatus

status.RING_FOR_S = 1.2         # a ring that goes "missed" within the test


async def main() -> int:
    display = _live.connect()
    agent = _live.ScriptedAgent(voice="indigo")
    agent.dial()
    widget = CallStatus(agent)
    _live.count_frames(widget)
    _live.count_frames(widget.chip)
    spec = _live.status_spec(widget)
    win = None
    bad = 0
    for label in ("first open", "opened again"):
        agent.say("update the deck")    # the call is closed: it works on
        if win is None:
            win = _live.open_window(display, _live.offscreen(spec))
        else:
            win.unpark()
            win.show()
            display.pump()
        await asyncio.sleep(0.5)
        widget.frames = widget.chip.frames = 0
        for i in range(3):
            agent.tool(f"{label}{i}", f"pacman -Q linux-{i}")
            await asyncio.sleep(0.4)            # nothing arrives meanwhile
            agent.result(f"{label}{i}", "ok")
        trace, chip = widget.frames / 1.2, widget.chip.frames / 1.2
        agent.text_start()
        agent.text("Done, choom. Kernel is current.")
        agent.end()                             # the call is closed: it rings
        await asyncio.sleep(0.6)
        ringing = (agent.ringing, widget._look()[2])
        await asyncio.sleep(1.6)
        missed = widget._look()[2]
        n, box = _live.stale(win, f"status-{label.replace(' ', '-')}")
        good = (trace > 24 and chip > 24 and ringing == ("done", "blink")
                and missed == "dim" and n == 0)
        bad += not good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: trace {trace:.0f}/s, "
              f"chip {chip:.0f}/s while working; rings ({ringing[1]}), then "
              f"{missed}; "
              + (f"{n}px left over at {box}" if n else "nothing left over"),
              flush=True)
        agent.acknowledge()
        done = asyncio.Event()
        win.dismiss(done.set)
        await done.wait()
        win.park()
        await asyncio.sleep(0.3)
    win.destroy()
    print("FAILED" if bad else "all passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
