"""The call overlay under a scripted agent: does it keep moving, and is
anything left on screen that should have been painted over?

    .venv/bin/python tests/call.py [--seeds 12] [--first 0]

Two parts. First the plain case that used to fail: a call opened for the
second time, the agent running a tool and saying nothing — the trace,
the chip and the caret must still get their frames. Then a fuzz: per
seed, one to three turns of streamed replies (fast or slow), thinking,
tool calls, typing and lines said mid-turn, with the loop blocked at random
moments and the call closed and opened again between turns. After each
turn, once every timed effect has ended, the window's surface must be
what a full repaint draws (`_live.stale`).
"""

import argparse
import asyncio
import random
import sys
import time

import _live

from indigoshell import config_default as config
from indigoshell.agent.ui.call import CallHost
from indigoshell.widgets.base import KeyEvent

RETURN = 0xFF0D
# Long enough after a turn for every timed effect to have ended: the
# reply's decode, the trace closing, the chip's two blinks and its arcs
# easing back. The scan may be mid-pass; it is drawn from its last tick,
# so a forced repaint draws the same band.
QUIET_S = 1.3

WORDS = ("the deck is preem choom jacking in copy that wifi driver resets "
         "since boot three of them all from the same chrome `iwlwifi` "
         "**firmware** flatlined twice *maybe* a power-save thing check "
         "journalctl and the ICE on that port nova delta").split()


def words(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def open_call(display, host: CallHost):
    spec, _services = config.call(host.agent)
    return _live.open_window(display, _live.offscreen(spec, content=host))


def pieces_at(host: CallHost, box: tuple) -> str:
    """Which of the call's pieces a box of pixels touches."""
    import skia
    rect = skia.Rect.MakeLTRB(*box)
    names = [name for name in ("agent_plate", "agent_chip", "you_plate",
                               "you_chip")
             if getattr(host, name).paint_bounds().intersects(rect)]
    return ", ".join(names) or "outside every piece"


# ── a re-opened call, the agent silent ──────────────────────────────────
async def silent_tool(display) -> bool:
    agent = _live.ScriptedAgent()
    host = CallHost(agent)
    win = open_call(display, host)
    moving = {"trace": host.agent_plate, "chip": host.agent_chip,
              "caret": host.you_plate.input}
    for widget in moving.values():
        _live.count_frames(widget)
    await asyncio.sleep(0.5)                    # the entrance
    ok = True
    for label in ("first open", "opened again"):
        host.you_plate.input.set_text("update the deck")
        host.key(KeyEvent(keysym=RETURN))
        agent.tool(label, "pacman -Syu --noconfirm")
        await asyncio.sleep(0.3)
        for widget in moving.values():
            widget.frames = 0
        await asyncio.sleep(1.0)                # the tool runs; nothing arrives
        rates = {name: widget.frames for name, widget in moving.items()}
        agent.result(label, "there is nothing to do")
        agent.text_start()
        agent.text("Already current, choom.")
        agent.end()
        await asyncio.sleep(QUIET_S)
        n, box = _live.stale(win, f"silent-{label.replace(' ', '-')}")
        good = (rates["trace"] >= 24 and rates["chip"] >= 24
                and rates["caret"] >= 3 and n == 0)
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: frames in a silent "
              f"second — trace {rates['trace']}, chip {rates['chip']}, "
              f"caret {rates['caret']}; "
              + (f"{n}px left over at {box}" if n else "nothing left over"),
              flush=True)
        if label == "first open":
            await asyncio.sleep(3.0)            # the call stays open a while
            await _live.reopen(win)
            await asyncio.sleep(0.5)
    win.destroy()
    return ok


# ── the fuzz ────────────────────────────────────────────────────────────
async def reply(rng, agent, *, fast: bool, stall_p: float) -> None:
    """One text block, streamed in chunks."""
    agent.text_start()
    body = []
    for _ in range(rng.randint(1, 4)):
        kind = rng.random()
        if kind < 0.2:
            body.append("- " + words(rng, rng.randint(4, 9)) + "\n- "
                        + words(rng, rng.randint(4, 9)))
        elif kind < 0.3:
            body.append("```\n" + words(rng, 5) + "\n```")
        else:
            body.append(words(rng, rng.randint(8, 40)))
    full = "\n\n".join(body)
    at = 0
    while at < len(full):
        n = rng.randint(2, 14) if fast else rng.randint(1, 5)
        agent.text(full[at:at + n])
        at += n
        if rng.random() < stall_p:
            time.sleep(rng.uniform(0.08, 0.4))      # the loop, blocked
        await asyncio.sleep(rng.uniform(0.002, 0.012) if fast
                            else rng.uniform(0.02, 0.07))


async def turn(rng, host, agent, *, fast, stall_p, typing) -> None:
    host.you_plate.input.set_text(words(rng, rng.randint(2, 14)))
    host.key(KeyEvent(keysym=RETURN))
    await asyncio.sleep(rng.uniform(0.0, 0.3))
    if rng.random() < 0.6:
        agent.think_start()
        for _ in range(rng.randint(0, 6)):
            agent.think(words(rng, 3) + " ")
            await asyncio.sleep(rng.uniform(0.01, 0.08))
    for k in range(rng.randint(0, 3)):
        if rng.random() < 0.5:
            await reply(rng, agent, fast=fast, stall_p=stall_p)
        tool_id = f"t{time.monotonic_ns()}"
        agent.tool(tool_id, f"journalctl -b -p err | tail -n {k}")
        if rng.random() < stall_p * 3:
            time.sleep(rng.uniform(0.1, 0.5))       # a tool run in-process
        await asyncio.sleep(rng.uniform(0.02, 0.4))
        agent.result(tool_id, words(rng, 30))
        await asyncio.sleep(rng.uniform(0.0, 0.1))
    if typing and rng.random() < 0.5:
        for ch in "ok and":
            host.key(KeyEvent(keysym=ord(ch), text=ch))
            await asyncio.sleep(rng.uniform(0.01, 0.06))
        if rng.random() < 0.5:
            host.key(KeyEvent(keysym=RETURN))       # folded into the turn
    await reply(rng, agent, fast=fast, stall_p=stall_p)
    how = rng.random()
    if how < 0.35:
        time.sleep(rng.uniform(0.05, 0.5))          # a stall, then the end
    elif how < 0.6:
        await asyncio.sleep(rng.uniform(0.0, 0.5))
    agent.end()
    if rng.random() < stall_p * 4:
        time.sleep(rng.uniform(0.1, 0.6))           # ...or right after it
    if typing and rng.random() < 0.4:
        for ch in "hm":
            await asyncio.sleep(rng.uniform(0.0, 0.2))
            host.key(KeyEvent(keysym=ord(ch), text=ch))


async def settle(agent) -> None:
    while agent.busy:               # a line said as it ended started another
        await reply(random.Random(1), agent, fast=True, stall_p=0.0)
        agent.end()
    await asyncio.sleep(QUIET_S)


async def fuzz(display, seed: int) -> bool:
    rng = random.Random(seed)
    agent = _live.ScriptedAgent()
    host = CallHost(agent)
    win = open_call(display, host)
    await asyncio.sleep(0.5)
    fast = rng.random() < 0.7
    stall_p = rng.choice((0.0, 0.01, 0.03))
    typing = rng.random() < 0.5
    turns = rng.randint(1, 3)
    found = []
    for t in range(turns):
        if t and rng.random() < 0.6:
            await _live.reopen(win)
            await asyncio.sleep(0.5)
        await turn(rng, host, agent, fast=fast, stall_p=stall_p,
                   typing=typing)
        await settle(agent)
        n, box = _live.stale(win, f"seed{seed}-turn{t}")
        if n:
            found.append(f"turn {t}: {n}px left over at {box} "
                         f"({pieces_at(host, box)})")
    print(f"  {'FAIL' if found else 'ok  '} seed {seed}: "
          f"{'fast' if fast else 'slow'} stream, stalls {stall_p}, "
          f"{'typing, ' if typing else ''}{turns} turn(s)", flush=True)
    for line in found:
        print("       " + line, flush=True)
    win.destroy()
    display.pump()
    await asyncio.sleep(0.05)
    return not found


async def main(args) -> int:
    display = _live.connect()
    print("a call opened again, the agent running a tool in silence")
    ok = await silent_tool(display)
    print(f"fuzz, seeds {args.first}..{args.first + args.seeds - 1}")
    bad = 0
    for seed in range(args.first, args.first + args.seeds):
        bad += not await fuzz(display, seed)
    print("FAILED" if bad or not ok else "all passed",
          f"({bad} of {args.seeds} seeds left something on screen)"
          if bad else "")
    return 1 if bad or not ok else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--first", type=int, default=0)
    sys.exit(asyncio.run(main(parser.parse_args())))
