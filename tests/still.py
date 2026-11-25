"""A resting bar is still: the user's rule (idle is still, motion marks
events), checked on the widgets that used to move all the time.

    .venv/bin/python tests/still.py

  1. the INDIGO tag: still by default (0 frames a second), its old pulse
     only with pulse=True;
  2. the battery meter: still at rest in every steady state, solid cells
     in the state's colour; a state change (unplugged, plugged, a band
     crossed) plays the state's motion for EVENT_S and settles; a level
     change alone repaints without motion; the low-battery blink keeps
     going (an alarm).

No display, no battery needed (fake states); a second.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indigoshell import theme                       # noqa: E402
from indigoshell.widgets.meters import BatteryMeter  # noqa: E402
from indigoshell.widgets.systag import Systag        # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed += not ok


def meter(percent: float, plugged: bool) -> BatteryMeter:
    m = BatteryMeter(cells=16, fake_state=(percent, plugged))
    m.invalidations = 0
    m.invalidate = lambda layout=False: setattr(m, "invalidations", m.invalidations + 1)
    return m


def to(m: BatteryMeter, percent: float, plugged: bool) -> None:
    m.fake_state = (percent, plugged)
    m._read()


print("1. the INDIGO tag")
tag = Systag("INDIGO")
check("still by default: 0 frames a second", tag.animation_fps == 0, f"{tag.animation_fps}")
check("... in neon magenta", tag.color == theme.MAGENTA_BRIGHT, tag.color)
check("pulse=True: its pulse again", Systag("INDIGO", pulse=True).animation_fps > 0)

print("2. the battery meter")
for percent, plugged, what in ((91, True, "plugged in, charging"), (100, True, "full"),
                               (80, False, "on battery"), (35, False, "on battery, under half")):
    m = meter(percent, plugged)
    cfg = m._state_config()
    lit = m._lit_count
    solid = all(m._cell_color(i, cfg) == cfg["sweep"] for i in range(lit))
    check(f"{what}: still at rest, its level solid", m.animation_fps == 0 and solid,
          f"fps {m.animation_fps}")

m = meter(91, True)
to(m, 91, False)
check("unplugged: the state's motion plays", m.animation_fps > 0 and m.invalidations >= 1,
      f"fps {m.animation_fps}")
m._event_until = time.monotonic() - 0.01
check(f"... and settles after {BatteryMeter.EVENT_S:g} s", m.animation_fps == 0)
to(m, 85, False)
check("a level change alone: repainted, no motion", m.animation_fps == 0 and m.invalidations >= 2,
      f"fps {m.animation_fps}, {m.invalidations} repaints")
to(m, 45, False)
check("a band crossed (under half): motion again", m.animation_fps > 0)
m._event_until = time.monotonic() - 0.01
to(m, 15, False)
blink = m.animation_fps
m._event_until = time.monotonic() - 0.01
check("under 20%: the alarm's blink, and it keeps going", blink > 0 and m.animation_fps > 0,
      f"{blink} then {m.animation_fps}")
to(m, 15, True)
check("plugged in again: the charging motion plays", m.animation_fps > 0)
m._event_until = time.monotonic() - 0.01
check("... then still", m.animation_fps == 0)

print(f"\n{'all ok' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
