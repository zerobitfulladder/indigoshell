"""The call overlay: talk to the agent from a corner of the screen.

Two lines of a call in the top-right corner, read like a chat: your
plate on top with your chip on its right, the agent's below with its
chip on its left — one column of frosted plates, the chips on either
side of it:

               ┌ // YOU ─────────── ⏎ send  esc hang up ┐ ┌──────┐
               │ why does my wifi keep dropping         │ │ YOU  │
               │ › follow up▏                           │ └──────┘
               └────────────────────────────────────────┘
    ┌──────┐ ┌ // INDIGO working ──────────────── 00:41 ┐
    │ ◜  ◝ │ │ › $ journalctl -b -p err | tail -n 40    │
    │ CALL │ │ · checking the wifi driver first         │
    │ ◟  ◞ │ │ ───                                      │
    └──────┘ │ Three resets since boot, all from ...    │
             └──────────────────────────────────────────┘

The frames are the toasts' and the launcher's: 2px, opaque, beveled at
the same corners, with the toast's `// NAME tag` heading. Each speaker
owns one colour throughout — frame, chip, name and a tint in the plate:
magenta for you, violet for the agent. A call to the hub's agent
(remote.py), which speaks in two voices, takes the name and colour of
the voice of the turn on show.

What the agent is *doing* (tool calls, narration between them, its
thinking) sits above a rule in small type; what it *says* sits below it,
rendered from Markdown. Click a step to expand it — the whole command
and its output — and the "earlier steps" row (or Ctrl+O) to see every
step of the turn. Drag across any of the agent's text to select it (it
goes to PRIMARY, for a middle-click paste); Ctrl+C copies the selection
to the clipboard; double-click takes a word, triple-click a whole
paragraph, command or output.

The chip's arcs say what the agent wants from you. Turning: it is
working. Blinking: it wants you — a question blinks until you answer
it; a finished answer blinks twice and settles. Still: it is waiting
on you. Only a closed call rings (see `call-status` below).

Hanging up — Escape, an outside click, the keybinding again, or the
agent's own `hang_up` — closes the overlay without stopping the work.
While a closed call is busy, `call-status` shows a small chip with the
current step; when the work is done it rings, and opening the call
answers. `CallRouter` (a SERVICES entry) opens and closes that chip,
since neither window is alive to do it while it is closed.

Keys in the call:
    type / Return       say a line; mid-task it goes in at once and the
                        agent reads it at its next step
    A / D               greenlight (allow), scrub (deny) — while it asks and
                        the line you are typing is empty
    Ctrl+C              copy the selection; with none, stop the task
    Ctrl+O              every step of the turn, or just the last few
    PageUp / PageDown   scroll a long reply (the wheel too)
    Ctrl+H              past conversations: Up/Down, Return to go back
                        to one, Escape or Ctrl+H to leave the list
    Ctrl+D              the hub's devices (a call to the hub only): which
                        are online or when last seen, their connections,
                        the hands each lends, which wake by push. Escape
                        or Ctrl+D to leave the list
    Ctrl+S              who answers your next line: Indigo or Lilac (a
                        call to the hub only; shown in your plate's
                        heading, and remembered)
    Ctrl+L              Look (a call to the hub only): your next line
                        carries what you are looking at in Flux, your
                        browser: the tab, the selection, the picture in it
                        or under the pointer. A chip over your line says
                        what; Ctrl+L again, or Escape with the line empty,
                        drops it. Ctrl+Shift+L adds a screenshot of the
                        tab. (Ctrl+L used to start a new conversation.)
    Ctrl+F              open the last picture or file the agent showed in
                        the turn (saved to ~/Downloads); a click on one
                        does the same
    Ctrl+N              a new conversation, from scratch
    Ctrl+M              next model: opus, sonnet, haiku, fable (Shift:
                        back). Switches the live conversation.
    Ctrl+T              next effort: idle, stock, boost, overclock,
                        redline (low, medium, high, xhigh, max; Shift:
                        back). Reconnects, keeping the context.
    Escape              hang up (the work carries on)

One conversation runs at a time and carries over between calls — hang
up and call again and the agent still has the context. It starts fresh
by itself after six idle hours. Past conversations are Claude Code
sessions in the call's own config dir, so they can also be opened with
`CLAUDE_CONFIG_DIR=~/.config/indigoshell/claude claude --resume` from
the workspace.
"""

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field

import skia

from ... import theme
from ...core import state
from ...widgets.base import KeyEvent, Size, Widget
from ...widgets.text_input import ctrl_letter
from ..session import Agent
from .agent_plate import AgentPlate
from .chip import Chip
from ...widgets.holo import Projection
from .style import (CHIP, GAP, HANGUP_CLOSE_S, HISTORY_ROWS, MAX_BODY_ROWS,
                    NUDGE_S, PLATE_W, ROW_GAP, SPIN, UNBOUNDED, YOU_ACCENT,
                    accent_of, own_accent, plate_path)
from .you_plate import YouPlate


log = logging.getLogger(__name__)

CALL_WINDOW = "call"
DRAFT_KEY = "agent.draft"       # your unsent line, between calls
LOOK_NOTE_S = 4.0               # "Flux isn't open" stays this long
LOOK_WAIT_S = 4.0               # a line waits this long for its Look


@dataclass
class _Look:
    """What the next line will carry (Ctrl+L): Flux's answer, once in."""

    screenshot: bool
    blocks: list | None = None      # None while Flux is asked
    note: str = ""                  # why there is nothing
    task: asyncio.Task | None = field(default=None, repr=False)

    def chip(self) -> tuple[str, str]:
        if self.blocks is None:
            return "looking…", theme.BASE_MUTED
        if not self.blocks:
            return self.note or "nothing to look at", theme.YELLOW_DIM
        return look_label(self.blocks), theme.CYAN_BRIGHT


def look_label(blocks: list) -> str:
    """The chip's words for a Look: the tab's title, and what else."""
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    head = texts[0].splitlines()[0] if texts and texts[0] else ""
    m = re.search(r'"(.*)" at (\S*)\s*$', head)
    title = (m.group(1) or m.group(2)) if m else "Flux's tab"
    words = f"looking at: {title}"
    if any(t.startswith("The tab as shown") for t in texts):
        words += " + screenshot"
    if any(t.startswith("The image") for t in texts):
        words += " + image"
    return words

_RETURN, _KP_ENTER, _ESCAPE = 0xFF0D, 0xFF8D, 0xFF1B
_UP, _DOWN, _KP_UP, _KP_DOWN = 0xFF52, 0xFF54, 0xFF97, 0xFF99
_PAGE_UP, _PAGE_DOWN, _KP_PAGE_UP, _KP_PAGE_DOWN = 0xFF55, 0xFF56, 0xFF9A, 0xFF9B


# ── the call window's content ───────────────────────────────────────────
# Kept clear at the bottom of the screen when the call takes its height:
# the bar, and a margin above it.
BOTTOM_CLEAR = theme.BAR_HEIGHT + 24


class CallHost(Widget):
    """Both lines of the call, right-aligned, and the keys that drive it.

    `agent` is the desktop's `Agent` or a `RemoteAgent` (agent/remote.py),
    which reads the same; each call keeps its unsent line under its own
    `draft_key`."""

    def __init__(self, agent: Agent, *, draft_key: str = DRAFT_KEY) -> None:
        super().__init__()
        self.agent = agent
        self.draft_key = draft_key
        self.agent_plate = AgentPlate(agent)
        self.agent_plate.relayout = self._fit
        self.agent_plate.link_clicked = self._open_link
        self.agent_plate.media_clicked = self._open_media
        self.agent_plate.touched = agent.acknowledge
        self.you_plate = YouPlate(agent)
        self.agent_chip = Chip(CHIP, self._agent_look, self._blink_since,
                               tint=lambda: own_accent(agent))
        self.you_chip = Chip(CHIP, lambda: (YOU_ACCENT, "YOU", "still"),
                             tint=YOU_ACCENT)
        # The plates and chips are one hologram; its scan glitches down
        # all four at once (widgets/holo.py).
        self.projection = Projection()
        self._unsub = None
        self._was_busy = False
        self._nudge_t0 = 0.0
        # The window is as tall as the screen allows for the whole call;
        # the plates grow inside it. See _fit for why it never resizes.
        self._avail_h = 0
        self._natural_h = 0.0
        self._you_key: tuple | None = None
        self._chip_look: tuple | None = None
        self._close_handle: asyncio.TimerHandle | None = None
        self._look: _Look | None = None
        self._look_xid: int | None = None   # focused as the call opened

    def children(self):
        # The projection last: it paints over the surfaces it scans.
        return [self.agent_plate, self.agent_chip, self.you_plate,
                self.you_chip, self.projection]

    def _nudging(self) -> bool:
        return time.monotonic() - self._nudge_t0 < NUDGE_S

    def _blink_since(self) -> float:
        a = self.agent
        return a.ring_started if a.approval is not None or a.ringing \
            else self._nudge_t0

    def _agent_look(self):
        a = self.agent
        accent = accent_of(a)
        label = "DROP" if a.phase == "error" else "CALL"
        if a.approval is not None:
            return accent, "ASK", "blink"
        if a.ringing in ("done", "error") or self._nudging():
            return accent, label, "blink"
        if a.phase == "error":
            return accent, "DROP", "still"
        if a.phase == "dialing" or (a.busy and a.phase in SPIN):
            return accent, "CALL", a.phase
        return accent, "CALL", "still"

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        self.agent_plate.reset()
        # What you were typing when the call closed is still there — kept
        # in the state file too, so a reload of the shell keeps it.
        field_ = self.you_plate.input
        if not field_.text:
            draft = state.get(self.draft_key)
            if isinstance(draft, str) and draft:
                field_.set_text(draft)
        self._was_busy = self.agent.busy
        self._nudge_t0 = 0.0
        # The window that had focus as the call opened: Look asks Flux
        # about that one. The call takes no focus (it grabs the keyboard),
        # so this is still the window the user was in.
        try:
            d = window.display
            self._look_xid = d.get_int(d.root, "_NET_ACTIVE_WINDOW") or None
        except Exception:
            self._look_xid = None
        self._unsub = self.agent.subscribe(self._on_agent)
        self.agent.set_call_open(True)
        mon = window.display.primary_monitor()
        self._avail_h = max(2 * CHIP + ROW_GAP,
                            mon.height - window.spec.margin.top - BOTTOM_CLEAR)
        self._natural_h = self._natural()
        window.resize_content(math.ceil(self._width()), self._avail_h)

    def detach(self) -> None:
        if self._close_handle is not None:
            self._close_handle.cancel()
            self._close_handle = None
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        state.set(self.draft_key, self.you_plate.input.text)
        self._drop_look()                   # about what was on screen then
        super().detach()
        self.agent.set_call_open(False)

    # ── agent events ────────────────────────────────────────────────────
    def _on_agent(self, event: str) -> None:
        if self.window is None:
            return
        if event == "hangup":
            self._schedule_close()
        if self._was_busy and not self.agent.busy:
            # The answer is in: a nudge, not a ring, and the turn clock
            # on the frame sweeps closed.
            self._nudge_t0 = time.monotonic()
            self.agent_plate.trace.turn_ended(self.agent)
            self.agent_chip.invalidate()
        self._was_busy = self.agent.busy
        self._fit()

    def _fit(self) -> None:
        """Repaint, laying the plates out again if their height changed
        (a new wrapped line, a step, the approval).

        The window itself never resizes while the call is open. It used
        to follow the plates, and the first frame after every resize cost
        ~120ms while the GPU driver rebuilt the window's buffers — and on
        the shell's one event loop that froze everything, the bar's
        animations included, at every new line of a reply and every tool
        call. Now it takes the full height once, when the call opens, and
        the blur cut (`blur_shape`) follows the plates instead."""
        win = self.window
        if win is None or win._phase in ("exit", "gone"):
            return
        natural = self._natural()
        if natural != self._natural_h:
            self._natural_h = natural
            self._place()
        # Repaint what changed, not the window: it is the screen's height,
        # and repainting all of it on every change of a streaming reply
        # cost ~30ms a frame. The agent's plate changes with nearly every
        # event; your plate and the chips only when what they show does
        # (they animate on their own clocks otherwise).
        self.agent_plate.invalidate()
        a = self.agent
        you_key = (a.busy, a.heard, getattr(a, "heard_on", None),
                   getattr(a, "voice", None), tuple(a.queued),
                   a.approval is None)
        if you_key != self._you_key:
            self._you_key = you_key
            self.you_plate.invalidate()
        look = self._agent_look()
        if look != self._chip_look:
            self._chip_look = look
            self.agent_chip.invalidate()

    def _place(self) -> None:
        """Plates grew or shrank: place everything again, re-cut the
        window to it, and repaint whatever is no longer where it was.

        A taller line of yours moves the agent's plate and chip down
        with nothing about them changed, so nothing else would repaint
        them in their new place. Where a piece used to be is repainted
        too, although the new cut hides it: it is still in the window's
        surface, and the despawn plays over that surface uncut.
        """
        pieces = (self.agent_plate, self.agent_chip, self.you_plate,
                  self.you_chip)
        before = [w.paint_bounds() for w in pieces]
        self.arrange(self.rect)
        for piece, was in zip(pieces, before):
            if piece.paint_bounds() != was:
                self.window.damage_rect(was)
                piece.invalidate()
        # A taller call can put a scan that had run off the bottom back
        # on it.
        self.projection.invalidate()
        self.window.reshape()

    def _open_link(self, url: str) -> None:
        """A link clicked in a reply: hang up, so you land on it, and open
        it in its usual app, brought forward on its workspace. The agent
        keeps working if it was."""
        from ...core.daemon import get_daemon
        from ..tools import open_target
        if self.window is not None:
            get_daemon().close(self.window.spec.name)
        asyncio.get_running_loop().create_task(
            open_target(url, self.agent.workspace))

    def _open_media(self, sid: str) -> None:
        """A picture or file the agent showed, clicked (or Ctrl+F): save
        it to ~/Downloads and open it, as a clicked link would be."""
        save = getattr(self.agent, "save_media", None)
        if save is None:
            return

        async def go() -> None:
            name = (getattr(self.agent, "shown", {}).get(sid) or {}).get(
                "name", "it")
            self.agent_plate.flash("saving…")
            try:
                path = await save(sid)
            except Exception as e:
                log.warning("couldn't save %s: %s", sid, e)
                self.agent_plate.flash(f"couldn't get {name}: {e}",
                                       theme.ERROR)
                return
            log.info("saved %s to %s", sid, path)
            self._open_link(path)

        asyncio.get_running_loop().create_task(go())

    # ── Look ────────────────────────────────────────────────────────────
    def _show_look(self) -> None:
        self.you_plate.look_chip = self._look.chip() if self._look else None
        self._fit()

    def _drop_look(self) -> None:
        look, self._look = self._look, None
        if look is not None and look.task is not None \
                and not look.task.done():
            look.task.cancel()
        if self.you_plate.look_chip is not None:
            self._show_look()

    def _toggle_look(self, screenshot: bool) -> None:
        """Ctrl+L (Shift: with a screenshot): the next line carries what
        you are looking at in Flux; again, the chip goes."""
        if self._look is not None and self._look.screenshot == screenshot:
            self._drop_look()
            return
        self._drop_look()
        look = self._look = _Look(screenshot)
        look.task = asyncio.get_running_loop().create_task(
            self._ask_flux(look))
        self._show_look()

    async def _ask_flux(self, look: _Look) -> None:
        try:
            from ...browser.control import look as flux_look
        except ImportError:
            blocks, note = [], "Flux isn't here"
        else:
            try:
                blocks = await flux_look(screenshot=look.screenshot,
                                         image=True, xid=self._look_xid)
            except Exception:
                log.exception("look failed")
                blocks = []
            note = "Flux isn't open"
        look.blocks, look.note = list(blocks or []), note
        if self._look is not look:
            return                          # dropped meanwhile
        self._show_look()
        if not look.blocks:
            # Nothing to carry: the note says why, then goes.
            def gone() -> None:
                if self._look is look:
                    self._drop_look()
            asyncio.get_running_loop().call_later(LOOK_NOTE_S, gone)

    def _say(self, line: str) -> None:
        """Say a line, with the Look if there is one (waiting a moment
        for Flux's answer if it isn't in yet)."""
        look, self._look = self._look, None
        if look is not None:
            self.you_plate.look_chip = None
        if look is None:
            self.agent.say(line)
            return
        if look.blocks is not None:
            self._say_with(line, look.blocks)
            return

        async def later() -> None:
            try:
                await asyncio.wait_for(asyncio.shield(look.task),
                                       LOOK_WAIT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._say_with(line, look.blocks or [])
            self._fit()

        asyncio.get_running_loop().create_task(later())

    def _say_with(self, line: str, blocks: list) -> None:
        if blocks:
            self.agent.say(line, attach=blocks)
        else:
            self.agent.say(line)

    def _schedule_close(self) -> None:
        if self._close_handle is not None:
            self._close_handle.cancel()
        loop = asyncio.get_running_loop()
        self._close_handle = loop.call_later(HANGUP_CLOSE_S, self._auto_close)

    def _auto_close(self) -> None:
        self._close_handle = None
        # Someone typing a reply has not been hung up on.
        if self.window is None or self.you_plate.input.text.strip():
            return
        from ...core.daemon import get_daemon
        get_daemon().close(self.window.spec.name)

    # ── layout ──────────────────────────────────────────────────────────
    def _width(self) -> float:
        return CHIP + GAP + PLATE_W + GAP + CHIP

    def _natural(self) -> float:
        """How tall the plates want to be."""
        top = max(self.you_plate.measure(UNBOUNDED).height, CHIP)
        bottom = max(self.agent_plate.measure(UNBOUNDED).height, CHIP)
        return top + ROW_GAP + bottom

    def measure(self, avail: Size) -> Size:
        return Size(self._width(), self._avail_h or self._natural())

    def arrange(self, rect: skia.Rect) -> None:
        # One column of plates; your chip right of yours, the agent's
        # chip left of its own.
        self.rect = rect
        x0, y = rect.left(), rect.top()
        px = x0 + CHIP + GAP
        yh = self.you_plate.measure(UNBOUNDED).height
        self.you_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, yh))
        self.you_chip.arrange(skia.Rect.MakeXYWH(
            px + PLATE_W + GAP, y, CHIP, CHIP))
        y += max(yh, CHIP) + ROW_GAP
        ah = self.agent_plate.measure(UNBOUNDED).height
        self.agent_chip.arrange(skia.Rect.MakeXYWH(x0, y, CHIP, CHIP))
        self.agent_plate.arrange(skia.Rect.MakeXYWH(px, y, PLATE_W, ah))
        # Down the call as it stands, not the screen-tall window.
        self.projection.arrange(skia.Rect.MakeLTRB(
            rect.left(), rect.top(), rect.right(), y + max(ah, CHIP)))

    def blur_shape(self) -> skia.Path:
        path = skia.Path()
        for w in (self.agent_plate, self.you_plate):
            path.addPath(plate_path(w.rect))
        for chip in (self.agent_chip, self.you_chip):
            path.addPath(chip.path())
        return path

    def paint(self, canvas: skia.Canvas) -> None:
        clip = canvas.getLocalClipBounds()
        for child in self.children():
            if child.rect.intersects(clip):
                child.paint(canvas)

    def hit(self, x: float, y: float) -> Widget | None:
        if self.agent_plate.rect.contains(x, y):
            return self.agent_plate.hit(x, y)     # or an answer button
        if self.rect.contains(x, y):
            self.agent.acknowledge()    # a click anywhere on the call
        return None

    # ── input ───────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        a, field_, plate = self.agent, self.you_plate.input, self.agent_plate
        k, letter = ev.keysym, ctrl_letter(ev)
        if k == _ESCAPE:
            if plate.history is not None:   # back out of the list first
                plate.close_history()
                return True
            if plate.show_devices:
                plate.close_devices()
                return True
            if self._look is not None and not field_.text:
                self._drop_look()           # the chip goes first
                return True
            return False                    # the window closes: hang up
        a.acknowledge()                     # any key: you are back
        if self._close_handle is not None:  # typing keeps the line open
            self._close_handle.cancel()
            self._close_handle = None
        if plate.history is not None:
            # The past-conversations list owns the keys while it is up.
            if k in (_UP, _KP_UP) or letter in ("p", "k"):
                plate.hist_move(-1)
            elif k in (_DOWN, _KP_DOWN) or letter == "j":
                plate.hist_move(1)
            elif k in (_RETURN, _KP_ENTER):
                plate.hist_pick()
            elif k in (_PAGE_UP, _KP_PAGE_UP):
                plate.hist_move(-HISTORY_ROWS)
            elif k in (_PAGE_DOWN, _KP_PAGE_DOWN):
                plate.hist_move(HISTORY_ROWS)
            elif letter == "h":
                plate.close_history()
            elif letter == "n":
                if a.new_conversation():
                    plate.reset()
                    self._fit()
            return True
        if letter == "c":
            if not plate.copy("clipboard") and not ev.shift:
                a.interrupt()
            return True
        if letter == "o":
            plate.toggle_all()
            return True
        if letter == "m":                   # model; with Shift, backwards
            a.cycle_model(-1 if ev.shift else 1)
            return True
        if letter == "t":                   # effort ("think")
            a.cycle_effort(-1 if ev.shift else 1)
            return True
        if letter == "h":
            if not a.busy:
                plate.open_history()
            return True
        if letter == "d" and plate.has_devices():
            plate.toggle_devices()
            return True
        if letter == "s" and hasattr(a, "switch_voice"):
            a.switch_voice()                # who answers the next line
            return True
        if letter == "l" and getattr(a, "attachments", False):
            self._toggle_look(screenshot=ev.shift)
            return True
        if letter == "f" and hasattr(a, "save_media"):
            sid = plate.last_shown()
            if sid is not None:
                self._open_media(sid)
            return True
        if letter == "n":
            if a.new_conversation():
                plate.reset()
                self._fit()
            return True
        if a.approval is not None and not field_.text and ev.text:
            choice = {"a": "allow", "d": "deny"}.get(
                ev.text.lower())
            if choice is not None:
                a.answer(choice)
                return True
        if k in (_RETURN, _KP_ENTER):
            line = field_.text.strip()
            if line:
                field_.clear()
                plate.reset()
                self.you_chip.kick()
                self.you_plate.comet.fire()
                self.you_plate.invalidate()
                self._say(line)
                self._fit()
            return True
        if k in (_PAGE_UP, _KP_PAGE_UP):
            plate.scroll_by(MAX_BODY_ROWS // 2)
            return True
        if k in (_PAGE_DOWN, _KP_PAGE_DOWN):
            plate.scroll_by(-(MAX_BODY_ROWS // 2))
            return True
        consumed = field_.key(ev)
        if consumed:
            self._fit()     # the hint line changes with an empty field
        return consumed
