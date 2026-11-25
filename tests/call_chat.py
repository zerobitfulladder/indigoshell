"""The call's one chat (lilac's protocol 2): how full it is, its
compaction, and scrolling back through it.

    .venv/bin/python tests/call_chat.py

1. The bookkeeping, no window (`ScriptedAgent` plays the hub): the
   welcome asks for the newest page of the scrollback; the turn on show
   (`view.exchange`, then `done.exchange`) stays out of the earlier
   exchanges; an older page goes before them and an answer covering a
   span replaces it (what was forgotten meanwhile goes); `forgotten`
   drops; `compacted` trims to its `from` and marks the edge; `used`
   unknown right after; compacting is no turn (not busy, Ctrl+C sends no
   stop) and a line said meanwhile waits, shown as asked, until the
   hub's turn joins it; a refused `fetch` is matched by its id; `say`
   names no voice.
2. The keys, on the plate's rows: Ctrl+K up through the chat to the
   edge and no further, Ctrl+J down to live again; with Super (niri's
   Mod+Ctrl+J/K resize) or Alt nothing scrolls; Escape goes back live
   before it hangs up; Return sends and is live.
3. The window, off the screen and opened twice: scrolled back while a
   turn streams, live again, the compaction's sweep, its note: nothing
   left over (`_live.stale`).
"""

import asyncio
import sys
import time

import _live

from indigoshell import config_default as config
from indigoshell.agent.ui import context
from indigoshell.agent.ui.call import CallHost
from indigoshell.widgets.base import KeyEvent

FAILED: list[str] = []
RETURN, ESCAPE = 0xFF0D, 0xFF1B
K, J = ord("k"), ord("j")
QUIET_S = 1.3


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}  {detail}".rstrip(), flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def exchange(xid: int, line: str, reply: str, source: str = "user",
             ago: float = 3600.0) -> dict:
    return {"id": xid, "at": time.time() - ago, "source": source,
            "device": "iris" if xid % 2 else "laptop", "client": "app",
            "lines": [line], "reply": reply, "attached": 0, "shown": []}


def seeded(n: int = 40) -> _live.ScriptedAgent:
    """A hub that already keeps `n` exchanges, ids 701..."""
    agent = _live.ScriptedAgent()
    agent.history([exchange(701 + i, f"line {i}", f"reply {i} " * (i % 7 + 1),
                            ago=3600.0 * (n - i)) for i in range(n)])
    return agent


async def turn(agent, host, line: str, reply: str) -> None:
    host.you_plate.input.set_text(line)
    host.key(KeyEvent(keysym=RETURN))
    await asyncio.sleep(0)
    agent.text_start()
    agent.text(reply)
    agent.end()
    await asyncio.sleep(0)


def ctrl(sym: int, **mods) -> KeyEvent:
    return KeyEvent(keysym=sym, ctrl=True, **mods)


# ── 1. the bookkeeping ──────────────────────────────────────────────────
async def test_bookkeeping() -> None:
    print("1. the chat's bookkeeping, from the hub's events")
    agent = seeded()
    agent.dial()
    await asyncio.sleep(0)
    asked = [m for m in agent.sent if m["type"] == "scrollback"]
    check("the welcome asks for the newest page of the scrollback, 30 of them",
          asked == [{"type": "scrollback", "limit": 30}], f"{asked}")
    ids = [x["id"] for x in agent.exchanges]
    check("... it arrives oldest first, the newest 30, more before them",
          ids == list(range(711, 741)) and agent.more_back and agent.edge is None,
          f"{ids[:2]}..{ids[-1:]} more={agent.more_back}")

    agent.say("hello there")
    await asyncio.sleep(0)
    say = next(m for m in agent.sent if m["type"] == "say")
    check("`say` carries the text alone: no voice", say == {"type": "say",
                                                             "text": "hello there"})
    agent.text("General Kenobi.")
    agent.end()
    check("done.exchange: the turn joins the scrollback, and is the turn on show",
          agent.exchanges[-1]["id"] == 741 and agent.exchange == 741
          and agent.exchanges[-1]["lines"] == ["hello there"]
          and agent.exchanges[-1]["reply"] == "General Kenobi.")
    check("... the earlier exchanges leave it out", agent.earlier()[-1]["id"] == 740)

    agent.want_older()
    await asyncio.sleep(0)
    ids = [x["id"] for x in agent.exchanges]
    check("an older page goes before them, echoing `before`",
          ids[0] == 701 and ids[-1] == 741 and len(ids) == 41
          and agent.sent[-1] == {"type": "scrollback", "limit": 30, "before": 711},
          f"{ids[:3]} {agent.sent[-1]}")
    check("... the oldest kept: no more, the chat starts there",
          not agent.more_back and agent.edge == "start")

    agent.kept = [x for x in agent.kept if x["id"] != 735]
    agent._ask_newest()
    await asyncio.sleep(0)
    check("a page covering a span replaces it: one forgotten meanwhile goes, the "
          "older ones stay", 735 not in [x["id"] for x in agent.exchanges]
          and agent.exchanges[0]["id"] == 701, f"{len(agent.exchanges)}")
    agent._feed("forgotten", ids=[702, 703])
    check("`forgotten` drops them", {702, 703}.isdisjoint(
        x["id"] for x in agent.exchanges))

    agent.compact(before=240_000, after=None, keep=5)
    start = agent.kept[-5]["id"]
    check("`compacted`: trimmed to its `from`, the edge is her memory now",
          agent.exchanges[0]["id"] == start and not agent.more_back
          and agent.edge == "compacted", f"{agent.exchanges[0]['id']} vs {start}")
    check("... the note's words, `after` not yet measured",
          context.note(agent.compacted) == "compacted 240k")

    agent._feed("state", **{**agent._hub_state(), "context": {
        **agent.hub_context, "used": None}})
    check("`used` unknown: the bar is there, nothing lit",
          context.shown(agent) is not None and agent.context["used"] is None)

    agent._phase("compacting")
    check("compacting is no turn: not busy, compacting, the phase says so",
          not agent.busy and agent.compacting and agent.phase == "compacting")
    n = len(agent.sent)
    agent.interrupt()
    check("... Ctrl+C's stop isn't sent (the hub refuses one)", len(agent.sent) == n)
    agent.say("while you tidy")
    check("a line said meanwhile: shown as asked, waiting, compacting still",
          agent.heard == "while you tidy" and agent.busy and agent.phase == "compacting")
    agent._hub_phase = "compacting"
    agent._phase("idle")
    agent._hear()
    check("... the hub's turn after it joins it (no second turn)",
          agent.heard == "while you tidy" and agent.busy
          and agent._local_turn is False and agent.phase == "thinking")
    agent.text("Done tidying.")
    agent.end()

    agent.want_media("abc")
    agent.want_media("def")
    agent._feed("error", request="fetch", id="def", reason="not found")
    check("a refused fetch is matched by its id, not its order",
          agent.media_failed("def") == "not found" and agent.media_failed("abc") is None
          and agent._fetching == ["abc"])


# ── 2. the keys ─────────────────────────────────────────────────────────
async def test_keys() -> None:
    print("2. Ctrl+K and Ctrl+J on the plate")
    agent = seeded(12)
    agent.dial()
    host = CallHost(agent)
    from indigoshell.widgets.base import Widget
    Widget.attach(host, _NoWindow())
    await asyncio.sleep(0)
    await turn(agent, host, "the newest", "Newest reply.")
    plate = host.agent_plate
    plate._build()
    check("live at first", plate.back is None)
    host.key(ctrl(K, super=True))
    host.key(ctrl(K, alt=True))
    check("with Super (niri's Mod+Ctrl+K) or Alt: no scroll", plate.back is None)
    host.key(ctrl(K))
    plate._build()
    check("Ctrl+K: back, into the exchanges before the turn on show",
          plate.back is not None and any(r.key.startswith("x") for r in plate._built))
    steps = 0
    while steps < 100:
        before = plate.back
        host.key(ctrl(K))
        plate._build()
        steps += 1
        if plate.back == before:
            break
    top = plate._built[1:3]
    check("Ctrl+K again and again: up to the top, then it stays (the chat's start)",
          steps < 100 and any(r.kind == "edge" and "starts" in r.label for r in top),
          f"{steps} steps, {[r.label for r in top if r.kind == 'edge']}")
    downs = 0
    while plate.back is not None and downs < 100:
        host.key(ctrl(J))
        downs += 1
    check("Ctrl+J down again: live once it reaches the turn on show",
          plate.back is None and downs < 100, f"{downs} steps")
    host.key(ctrl(K))
    host.key(KeyEvent(keysym=ESCAPE))
    check("Escape while back: live again, the call stays open", plate.back is None)
    host.key(ctrl(K))
    host.you_plate.input.set_text("a new line")
    host.key(KeyEvent(keysym=RETURN))
    check("Return sends and is live", plate.back is None and agent.heard == "a new line")
    agent.text("ok")
    agent.end()

    a2 = seeded(3)
    a2.compact(keep=2)
    a2.dial()
    await asyncio.sleep(0)
    h2 = CallHost(a2)
    Widget.attach(h2, _NoWindow())
    for _ in range(10):
        h2.key(ctrl(K))
    h2.agent_plate._build()
    edge = [r.label for r in h2.agent_plate._built if r.kind == "edge"]
    check("compacted away: the top says earlier is in her memory, and Ctrl+K stops there",
          edge and "memory" in edge[0] and a2.edge == "compacted", f"{edge}")


class _NoWindow:
    """What a widget asks of its window, off any screen."""

    class spec:
        name = "test-call"

    _phase = "idle"
    loop = None

    def damage(self, widget=None, *, layout=False):
        pass

    def damage_rect(self, rect):
        pass

    def resize_content(self, w, h):
        pass

    def reshape(self):
        pass


# ── 3. the window ───────────────────────────────────────────────────────
async def test_window(display) -> None:
    print("3. the window, off the screen, opened twice")
    agent = seeded(20)
    host = CallHost(agent)
    spec, _services = config.call(agent)
    win = _live.open_window(display, _live.offscreen(spec, content=host))
    await asyncio.sleep(0.5)
    for label in ("first open", "opened again"):
        host.you_plate.input.set_text("scroll while you talk")
        host.key(KeyEvent(keysym=RETURN))
        agent.text_start()
        for i in range(3):
            host.key(ctrl(K))
            agent.text(f"part {i} of a reply that keeps coming " * 3)
            await asyncio.sleep(0.15)
        agent.end()
        await asyncio.sleep(QUIET_S)
        n_back, box_back = _live.stale(win, f"chat-back-{label.replace(' ', '-')}")
        while host.agent_plate.back is not None:
            host.key(ctrl(J))
        agent.compact(keep=4)
        await asyncio.sleep(QUIET_S)
        n_live, box_live = _live.stale(win, f"chat-live-{label.replace(' ', '-')}")
        check(f"{label}: scrolled back while a reply streamed, then live after a "
              "compaction: nothing left over",
              n_back == 0 and n_live == 0 and host.agent_plate.back is None,
              f"back {n_back}px {box_back}, live {n_live}px {box_live}")
        if label == "first open":
            await _live.reopen(win)
            await asyncio.sleep(0.5)
    agent._phase("compacting")
    sweep = host.agent_plate.animation_fps
    await asyncio.sleep(0.6)
    agent._phase("idle")
    await asyncio.sleep(QUIET_S)
    n, box = _live.stale(win, "chat-sweep")
    check("a compaction's sweep asks frames while it runs; once it ends nothing is "
          "left over", sweep >= context.SWEEP_FPS and n == 0, f"{sweep} fps, {n}px {box}")
    win.destroy()


async def main() -> int:
    await test_bookkeeping()
    await test_keys()
    await test_window(_live.connect())
    print("FAILED" if FAILED else "all passed",
          f"({len(FAILED)}: {FAILED})" if FAILED else "")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
