"""The call's one chat (lilac's protocol 2): how full it is, its
compaction, and paging back through it.

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
   carries the line alone; a welcome whose turn on show the hub started
   (`view.client`) shows its line as a note, not as yours, and keeps it
   as the hub's when it ends; one asked on another device says which.
   A line said into a running turn (here, or from another screen) is
   what your plate shows now, never a row of hers, and the turn keeps
   every line for its exchange.
2. The keys, on both plates: Ctrl+K pages to the exchange before (your
   plate its lines, all of them, with its time and how far back; hers
   its reply, a hub turn's line a note), again and again to the edge
   and no further (the chat's start, or her memory); Ctrl+J back, live
   past the newest; with Super (niri's Mod+Ctrl+J/K resize) or Alt
   nothing pages; Escape goes back live before it hangs up; Return
   sends and is live. The heading's meter: lit as `used` of `cap`, its
   number, nothing lit and no number when unknown.
3. The window, opened twice: paged back while a
   turn streams and a line goes into it, live again, the compaction's
   sweep, its note: nothing left over (`_live.stale`).
"""

import asyncio
import re
import sys
import time

import _live

from indigoshell import config_default as config
from indigoshell.agent.ui import agent_plate as plate_mod, context
from indigoshell.agent.ui.agent_plate import AgentPlate
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
    check("`say` carries the line alone", say == {"type": "say", "text": "hello there"})
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

    hub = _live.ScriptedAgent()
    line = "job transcribe talk.mp4 ended: ok"
    hub._hub_turn, hub._hub_phase = 7, "speaking"
    hub._on_welcome({
        "type": "welcome", "protocol": 2, "device": hub.DEVICE,
        "agent": hub.AGENT, "state": hub._hub_state(), "ask": None,
        "unseen": None, "devices": [],
        "view": {"turn": 7, "at": time.time() - 30, "client": "hub",
                 "device": None, "line": line, "text": "Transcript's done.",
                 "blocks": [{"block": 0, "text": "Transcript's done."}],
                 "steps": [], "shown": [], "exchange": None}})
    acts = list(hub.activity)
    check("a welcome with the hub's own turn on show (`view.client`: hub): its line a "
          "note at the top, not in your plate", hub.heard == "" and hub.from_hub
          and acts and acts[0].kind == "note" and acts[0].text == line
          and hub.busy, f"heard={hub.heard!r} {[(a.kind, a.text) for a in acts]}")
    hub._feed("done", turn=7, ok=True, text="Transcript's done.", error=None,
              hint=None, notify=None, seconds=1.0, exchange=950)
    kept = next((x for x in hub.exchanges if x["id"] == 950), None)
    check("... watched to its end: kept as the hub's exchange, its line first",
          kept is not None and kept["source"] == "hub" and kept["lines"] == [line]
          and kept["device"] is None
          and kept["reply"] == "Transcript's done.", f"{kept}")
    other = _live.ScriptedAgent()
    other._on_welcome({
        "type": "welcome", "protocol": 2, "device": other.DEVICE,
        "agent": other.AGENT, "state": other._hub_state(), "ask": None,
        "unseen": None, "devices": [],
        "view": {"turn": 3, "at": time.time() - 60, "client": "app",
                 "device": "iris", "line": "is the pi hot", "text": "52 °C.",
                 "blocks": [], "steps": [], "shown": [], "exchange": 949}})
    check("a welcome with a turn asked on the phone: your line in your plate, "
          "'on iris'", other.heard == "is the pi hot" and other.heard_on == "iris"
          and not other.from_hub and not other.activity)

    mid = _live.ScriptedAgent()
    mid.dial()
    await asyncio.sleep(0)
    mid.say("check the logs")
    await asyncio.sleep(0)
    mid.text_start()
    mid.text("Reading the journal")
    mid.say("only since boot")
    await asyncio.sleep(0)
    mid.text(", since boot then.")
    parts = [k for k, _after, _said in mid.said_parts()]
    check("a line said into the running turn: your plate shows it now; her plate's "
          "parts are only hers", mid.heard == "only since boot" and mid.busy
          and all(not k.startswith("y") for k in parts)
          and mid.message == "Reading the journal, since boot then.",
          f"heard={mid.heard!r} parts={parts} message={mid.message!r}")
    mid._feed("line", text="and the kernel", device="iris", client="app",
              attached=0)
    check("... one from another screen: your plate shows it, on iris",
          mid.heard == "and the kernel" and mid.heard_on == "iris")
    mid.end()
    kept = mid.exchanges[-1]
    check("... the turn ends: its exchange keeps every line, oldest first",
          kept["lines"] == ["check the logs", "only since boot", "and the kernel"],
          f"{kept['lines']}")


# ── 2. the keys ─────────────────────────────────────────────────────────
def shown(host) -> tuple:
    """What the plates show: your lines, your heading's sign and time,
    her plate's text rows."""
    you, her = host.you_plate, host.agent_plate
    her._build()
    page = host.pages.page()
    rows = [r.src[r.start:r.end] for r in her._built
            if r.kind in ("line", "act") and r.src]
    notes = [r.label for r in her._built if r.kind == "note"]
    back = host.pages.count()
    return you._said(), back, page, rows, notes


async def test_keys() -> None:
    print("2. Ctrl+K and Ctrl+J: both plates, an exchange at a time")
    agent = seeded(12)
    # The turn played below becomes the newest: these are the two before.
    agent.kept[-2]["source"] = "hub"
    agent.kept[-2]["lines"] = ["job resize ended: ok", "and the thumbnails"]
    agent.kept[-1]["lines"] = ["first line", "said into it", "and this"]
    agent.dial()
    host = CallHost(agent)
    from indigoshell.widgets.base import Widget
    Widget.attach(host, _NoWindow())
    await asyncio.sleep(0)
    await turn(agent, host, "the newest", "Newest reply.")
    lines, back, page, rows, _ = shown(host)
    check("live at first: your plate the line on show, hers its reply",
          page is None and lines == ["the newest"] and "Newest reply." in rows)
    host.key(ctrl(K, super=True))
    host.key(ctrl(K, alt=True))
    check("with Super (niri's Mod+Ctrl+K) or Alt: no page turn", host.pages.back is None)
    host.key(ctrl(K))
    lines, back, page, rows, _ = shown(host)
    want = agent.kept[-2]                   # before the turn just played
    check("Ctrl+K: the exchange before on both plates, all its lines oldest first, "
          "1 back", page is not None and page["id"] == want["id"] and back == 1
          and lines == ["first line", "said into it", "and this"]
          and any(want["reply"].split()[0] in r for r in rows), f"{lines} {back} {rows[:2]}")
    host.key(ctrl(K))
    lines, back, page, rows, _ = shown(host)
    check("Ctrl+K again: a hub exchange looks as a live hub turn does: its line a note "
          "in her plate, only your line in yours", back == 2
          and lines == ["and the thumbnails"]
          and any("job resize ended" in r for r in rows), f"{lines} {rows[:2]}")
    host.key(ctrl(J))
    host.key(ctrl(J))
    check("Ctrl+J twice: live again", host.pages.back is None)
    steps = 0
    while steps < 40 and not host.pages.page() == "edge":
        host.key(ctrl(K))
        steps += 1
    _lines, _back, page, _rows, notes = shown(host)
    host.key(ctrl(K))
    check("Ctrl+K to the oldest and one more: nothing older, the chat starts here; "
          "and no further", page == "edge" and host.pages.page() == "edge"
          and notes and "starts" in notes[0] and _lines == [], f"{steps} steps {notes}")
    host.key(KeyEvent(keysym=ESCAPE))
    check("Escape while paged back: live again, the call stays open",
          host.pages.back is None)
    host.key(ctrl(K))
    host.you_plate.input.set_text("a new line")
    host.key(KeyEvent(keysym=RETURN))
    check("Return sends and is live", host.pages.back is None and agent.heard == "a new line")
    agent.text("ok")
    agent.end()

    a2 = seeded(3)
    a2.compact(keep=2)
    a2.dial()
    await asyncio.sleep(0)
    h2 = CallHost(a2)
    Widget.attach(h2, _NoWindow())
    for _ in range(4):
        h2.key(ctrl(K))
    _lines, _back, page, _rows, notes = shown(h2)
    check("compacted away: past the oldest she holds, a note that earlier is in her "
          "memory, and Ctrl+K stops there", page == "edge" and notes
          and "memory" in notes[0] and a2.edge == "compacted", f"{notes}")

    import skia
    from indigoshell.agent.ui.context import METER_W
    a3 = _live.ScriptedAgent()
    a3.dial()
    surface = skia.Surface(700, 40)

    def meter() -> tuple[int, int, str]:
        surface.getCanvas().clear(skia.ColorTRANSPARENT)
        drawn: list[str] = []
        real = plate_mod.text.draw

        def spy(canvas, value, *args, **kw):
            drawn.append(value)
            return real(canvas, value, *args, **kw)
        plate_mod.context.text.draw = spy
        try:
            plate = AgentPlate(a3)
            plate.arrange(skia.Rect.MakeXYWH(0, 0, 680, 200))
            plate._paint_header(surface.getCanvas(), skia.Rect.MakeXYWH(16, 8, 648, 24),
                                "#c8a2c8")
        finally:
            plate_mod.context.text.draw = real
        px = surface.makeImageSnapshot().toarray()
        lilac = ((abs(px[..., 0].astype(int) - 0xc8) < 40)
                 & (abs(px[..., 1].astype(int) - 0xa2) < 40) & (px[..., 3] > 60))
        return int(lilac.sum()), len(drawn), " ".join(drawn)

    a3.context = {**a3.context, "used": 400_000}
    full, n_full, words = meter()
    a3.context = {**a3.context, "used": 50_000}
    low, _n, _w = meter()
    a3.context = {**a3.context, "used": None}
    unknown, _n, words_unknown = meter()
    check("her heading's meter: more lit at 400k than 50k, the number drawn ('400k'); "
          "unknown: nothing lit, no number", full > low > unknown
          and "400k" in words and not re.search(r"\d+k\b", words_unknown),
          f"lit {full}/{low}/{unknown} px; {words!r} / {words_unknown!r}; {METER_W}px")


class _NoWindow:
    """What a widget asks of its window, stood in for."""

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
async def test_window(canvas) -> None:
    print("3. the window, opened twice")
    agent = seeded(20)
    host = CallHost(agent)
    spec, _services = config.call(agent)
    win = _live.open_window(canvas, spec, content=host)
    await asyncio.sleep(0.5)
    for label in ("first open", "opened again"):
        host.you_plate.input.set_text("page back while you talk")
        host.key(KeyEvent(keysym=RETURN))
        agent.text_start()
        for i in range(3):
            host.key(ctrl(K))
            agent.text(f"part {i} of a reply that keeps coming " * 3)
            await asyncio.sleep(0.15)
        agent.end()
        await asyncio.sleep(QUIET_S)            # every timed effect over
        n_back, box_back = _live.stale(win, f"chat-back-{label.replace(' ', '-')}")
        while host.pages.back is not None:
            host.key(ctrl(J))
        host.you_plate.input.set_text("now the next")
        host.key(KeyEvent(keysym=RETURN))
        agent.text_start()
        agent.text("Starting on it")
        await asyncio.sleep(0.05)
        host.you_plate.input.set_text("and one more thing")
        host.key(KeyEvent(keysym=RETURN))       # into the running turn
        await asyncio.sleep(0.05)
        agent.text(" and that thing too.")
        agent.end()
        agent.compact(keep=4)
        await asyncio.sleep(QUIET_S)
        n_live, box_live = _live.stale(win, f"chat-live-{label.replace(' ', '-')}")
        check(f"{label}: paged back while a reply streamed; live, a line said into "
              "the turn (your plate switched), a compaction: nothing left over",
              n_back == 0 and n_live == 0 and host.pages.back is None
              and agent.exchanges[-1]["lines"][-1] == "and one more thing",
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
    await test_window(_live.canvas())
    print("FAILED" if FAILED else "all passed",
          f"({len(FAILED)}: {FAILED})" if FAILED else "")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
