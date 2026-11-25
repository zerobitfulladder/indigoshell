"""The agent's plate: the turn as it happened — what it says (rendered
Markdown, selectable) and, between its words, the steps it ran
(expandable), in order, a long one with its progress bar under it — or
the list of the hub's devices, when that is open. A picture or a file
the agent showed takes its place among them (media.py). Under its
heading, how full the chat is (context.py).

**Scrolling back.** The plate shows the newest exchange, the turn on
show (live). Ctrl+K scrolls up through it and on into the earlier ones
(`RemoteAgent.exchanges`, fetched a page at a time as the top comes
near), Ctrl+J back down; each step stops at an exchange's start, or
goes half the plate at most. Back at the turn on show, it is live again.
An earlier exchange is its date and time, your lines as you said them
(your colour, the bar a line said mid-turn has), and her reply as
Markdown in hers; one the hub started has its line as a note. At the
top: more to come (Ctrl+K), the chat's start, or the edge of what she
still holds word for word, past which only she remembers."""

import time
from typing import Callable

import skia

from ... import shapes, text, theme
from ...widgets.base import Size, Widget
from ...services import clipboard
from ..remote import RemoteAgent
from . import context
from .bars import Bars
from .choice import CHOICE_GAP, CHOICE_H, Choice
from .frame import TRACE_BLEED, Trace
from .reveal import Reveal
from .style import (ACT_ROWS_ALL, ACT_ROWS_BUSY, ACT_ROWS_IDLE, ACT_SIZE,
                    ASK_ACCENT, BODY_SIZE, CLICK_S,
                    COPIED_FPS, COPIED_S, DOTS_PER_S, FAIL_ACCENT,
                    MAX_BODY_ROWS, NAME_SIZE, OUT_ROWS,
                    OUT_ROWS_FULL, PAD_X, PAD_Y, PLATE_W, SLASH_FG,
                    STATUS_WORDS, TAG_SIZE, TOOL_FG, YOU_ACCENT, accent_of,
                    ago, clock_text, fill_of, line_h, own_accent,
                    paint_heading, paint_plate, when)
from .media import (MEDIA_KINDS, Pictures, paint_file, paint_image,
                    paint_waiting, shown_rows)
from .rows import Row, code_rows, highlight, markdown_rows, paint_line, text_rows


# ── the agent's plate ───────────────────────────────────────────────────

# The context bar's row, under the heading; an exchange's date row, and
# the space above it that sets one exchange off from the one before.
CONTEXT_H = 8
STAMP_H = line_h(TAG_SIZE) + 12


def think_line(agent, now: float) -> str:
    """The thinking row's words: that the agent thinks, never what (the
    user's rule: the content of its thoughts stays off the screen, as on
    the top bar's chip). A dot more every 1/DOTS_PER_S seconds, up to
    three."""
    dots = int((now - agent.turn_started) * DOTS_PER_S) % 4
    return "thinking" + "." * dots

class AgentPlate(Widget):
    """What the agent is doing and what it says, selectable.

    Measure, paint and pointer events all read the same rows, built from
    the agent's state and kept until it changes (`_build`), so the three
    always agree on where everything is.
    """

    def __init__(self, agent: RemoteAgent, width: int = PLATE_W) -> None:
        super().__init__(on_drag=self._on_drag)
        self.agent = agent
        self.width = width
        self.scroll = 0             # reply rows scrolled up from the tail
        self.show_all = False
        self.expanded: set[int] = set()
        self.full_output: set[int] = set()
        self.anchor: tuple[str, int] | None = None
        self.focus: tuple[str, int] | None = None
        self.relayout: Callable[[], None] = lambda: None
        self.touched: Callable[[], None] = lambda: None
        self.link_clicked: Callable[[str], None] = lambda url: None
        # A picture or file the agent showed, clicked: its id.
        self.media_clicked: Callable[[str], None] = lambda sid: None
        self.pictures = Pictures()
        # Scrolled back (Ctrl+K): the first row shown, as the exchange it
        # is in ("live": the turn on show) and how far into it; None: live.
        self.back: tuple | None = None
        self._past: dict = {}       # an earlier exchange's rows, by its id
        # The hub's devices, while that list is open (Ctrl+D; a call to
        # the hub only). Read live from the agent, which keeps it.
        self.show_devices = False
        self.reveal = Reveal()      # how reply text arrives
        self._built: list[Row] = []
        self._built_key: tuple | None = None
        self._body: list[Row] = []  # the whole turn, before scrolling
        self.trace = Trace()        # the turn clock on the frame
        self.bars = Bars()          # long commands' progress
        # The answers to a question, placed in its keys row (`_layout`).
        pick = lambda verdict: self.agent.answer(verdict)    # noqa: E731
        self.choices = (
            Choice("GREENLIGHT", "allow", theme.LIME_BRIGHT, pick),
            Choice("SCRUB", "deny", theme.ERROR, pick),
        )
        self._copied_until = 0.0
        self._flash = ("copied", None)     # the heading's brief tag
        self._dragging = False
        self._press: tuple[float, float] = (0.0, 0.0)
        self._press_pos: tuple[str, int] | None = None
        self._moved = False
        self._clicks: tuple[float, float, float, int] = (0.0, 0.0, 0.0, 0)

    def reset(self) -> None:
        self.back = None
        self.show_devices = False
        self.scroll = 0
        self.show_all = False
        self.expanded.clear()
        self.full_output.clear()
        self.anchor = self.focus = None

    @property
    def inner_w(self) -> float:
        return self.width - 2 * PAD_X

    @property
    def animation_fps(self) -> int:
        # Everything here that moves is drawn from the time: the trace
        # round the frame (and with it the turn clock and the thinking
        # dots, for as long as a turn runs), the reply decoding, and the
        # "copied" tag waiting to come down; and progress bars filling.
        return max(self.trace.fps(self.agent), self.reveal.fps(),
                   COPIED_FPS if self._copied() else 0,
                   context.fps(self.agent),
                   self.bars.fps([r.job for r in self._built
                                  if r.kind == "job"]))

    def _copied(self) -> bool:
        return time.monotonic() < self._copied_until

    # ── building rows ───────────────────────────────────────────────────
    def _build_key(self) -> tuple:
        """Everything the rows depend on. Paint runs at up to 30fps while
        the agent works and asked for the rows several times a frame;
        rebuilding them (Markdown, wrapping) each time was most of the
        CPU a streaming reply cost. What changes per frame without
        changing the rows — the clock, the dots, a thought's text —
        is read live at paint time instead."""
        a = self.agent
        acts = a.activity
        devices = self._devices() if self.show_devices else None
        ctx = context.shown(a)
        return (a.message, a.phase, a.busy, bool(a.thought), id(a.approval),
                None if ctx is None else tuple(ctx.values()), a.compacting,
                id(a.compacted), self.back, a.back_rev, a.back_loading,
                a.more_back, a.edge, a.exchange, a.at, a.heard,
                a.error, a.hint, len(acts), id(acts[-1]) if acts else None,
                sum(1 for x in acts if x.result is not None),
                self.show_all, tuple(sorted(self.expanded)),
                tuple(sorted(self.full_output)), self.scroll, self.width,
                a.jobs.shape(), getattr(a, "media_rev", 0),
                # The colour the reply's Markdown is drawn in: hers.
                own_accent(a),
                None if devices is None else (id(devices), len(devices)))

    def _build(self) -> list[Row]:
        key = self._build_key()
        if key != self._built_key:
            self._built = self._build_rows()
            self._built_key = key
        return self._built

    def _build_rows(self) -> list[Row]:
        a, w = self.agent, self.inner_w
        rows = [Row("header", line_h(NAME_SIZE, True) + 6)]
        if context.shown(a) is not None:
            rows.append(Row("context", CONTEXT_H))
        if self.show_devices:
            self._body = []
            return rows + self._device_rows()

        if self.back is not None:
            body = self._back_window(w)
        else:
            body = self._window(self._live_body(w))
        rows += body

        # A question or a failure comes after the turn, never scrolled
        # out of sight with it.
        if a.approval is not None:
            ap = a.approval
            if body:
                rows.append(Row("rule", 11))
            rows += self._text_rows("ap-t", ap.title, BODY_SIZE, w,
                                    theme.HUD_FG_BRIGHT)[:2]
            rows += self._code_rows("ap-d", ap.detail, w, ASK_ACCENT)[:6]
            rows.append(Row("keys", CHOICE_H + 12))
        elif a.phase == "error" and a.error:
            if body:
                rows.append(Row("rule", 11))
            rows += self._text_rows("err", a.error, BODY_SIZE, w, FAIL_ACCENT)
            if a.hint:
                rows.append(Row("gap", 4))
                rows += self._code_rows("hint", a.hint, w, ASK_ACCENT)
        elif not body:
            hint = "dialing…" if a.phase in ("dialing", "offline") \
                else "go ahead"
            rows.append(Row("note", line_h(BODY_SIZE) + 3, label=hint,
                            color=theme.BASE_MUTED))
        return rows

    def _live_body(self, w: float) -> list[Row]:
        """The turn on show: its rows, thinking at the end, and the note a
        compaction since it began leaves."""
        a = self.agent
        body = self._turn_rows(w)
        if a.busy and a.phase == "thinking":
            body.append(Row("think", line_h(ACT_SIZE) + 3, size=ACT_SIZE))
        done = a.compacted
        if done and done["at"] > a.turn_started and not a.compacting:
            if body:
                body.append(Row("gap", 6))
            body.append(Row("note", line_h(ACT_SIZE) + 3, size=ACT_SIZE,
                            x=text.advance(ACT_SIZE) * 2,
                            label=context.note(done), color=theme.BASE_MUTED))
        return body

    def _turn_rows(self, w: float) -> list[Row]:
        """The turn in the order it happened: each block of text the agent
        wrote, and between them the runs of steps it took."""
        a = self.agent
        parts = a.said_parts()
        items: list = []            # a part, or a run of steps (a list)
        i = 0
        for act in a.activity:
            # A part goes before the first step that came after it.
            while i < len(parts) and parts[i][1] < act.id:
                items.append(parts[i])
                i += 1
            if items and isinstance(items[-1], list):
                items[-1].append(act)
            else:
                items.append([act])
        items += parts[i:]
        # Long commands from before this turn, or that no step claimed,
        # still running in the background: their bars come last.
        loose = a.loose_jobs()

        rows: list[Row] = []
        for n, item in enumerate(items):
            if rows:
                rows.append(Row("gap", 6))
            if isinstance(item, list):
                rows += self._step_rows(item, w,
                                        current=a.busy and n == len(items) - 1)
            elif item[0].startswith("y"):       # a line typed mid-turn
                key, _after, said = item
                rows += text_rows(key, said, BODY_SIZE, w, YOU_ACCENT,
                                  x=text.advance(ACT_SIZE) * 1.5,
                                  bar=YOU_ACCENT)
            elif item[0].startswith("s"):       # a picture or a file
                rows += shown_rows(a, self.pictures, item[2], w)
            else:
                key, _after, said = item
                rows += markdown_rows(said, w, key, own_accent(a))
        if loose:
            if rows:
                rows.append(Row("gap", 6))
            for job in loose:
                rows += self._job_rows(job, loose=True)
        self.reveal.track(a, rows)
        if a.phase == "speaking" and items and not isinstance(items[-1], list):
            for row in reversed(rows):
                if row.kind == "line" and row.key.startswith("m"):
                    row.cursor = True
                    break
        return rows

    # ── scrolling ───────────────────────────────────────────────────────
    @staticmethod
    def _budget() -> float:
        """How tall the turn may be before it scrolls: MAX_BODY_ROWS
        lines of reply. Steps are shorter lines, so more of them fit."""
        return MAX_BODY_ROWS * (line_h(BODY_SIZE) + 3)

    def _fit_back(self, body: list[Row], end: int) -> int:
        """The first row shown when the window ends at `end`."""
        budget, start, acc = self._budget(), end, 0.0
        while start > 0 and acc + body[start - 1].h <= budget:
            start -= 1
            acc += body[start].h
        return start

    def _fit_forward(self, body: list[Row], start: int) -> int:
        """Where the window ends when it starts at `start`."""
        budget, end, acc = self._budget(), start, 0.0
        while end < len(body) and acc + body[end].h <= budget:
            acc += body[end].h
            end += 1
        return max(end, start + 1)

    def _window(self, body: list[Row]) -> list[Row]:
        """The newest rows of the turn that fit, or older ones while it is
        scrolled back (`scroll` counts rows up from the end)."""
        self._body = body
        n = len(body)
        if sum(r.h for r in body) <= self._budget():
            self.scroll = 0
            return body
        self.scroll = max(0, min(self.scroll, n - self._fit_forward(body, 0)))
        end = n - self.scroll
        start = self._fit_back(body, end)
        shown = body[start:end]
        if start:
            shown.insert(0, Row("more", line_h(ACT_SIZE) + 2, size=ACT_SIZE,
                                label=f"↑ {start} more — PageUp"))
        if self.scroll:
            shown.append(Row("more", line_h(ACT_SIZE) + 2, size=ACT_SIZE,
                             label=f"↓ {self.scroll} more — PageDown"))
        return shown

    def _toggle_step(self, act, which: set[int]) -> None:
        """Open or close a step's command or its full output, keeping the
        turn where it was on screen: the rows a step opens push what is
        below it down, not the step up, and the step's own row stays in
        view however much it opens."""
        self._build()
        n0, end0 = len(self._body), len(self._body) - self.scroll
        which ^= {act.id}
        self._build()
        body = self._body
        i = next((k for k, r in enumerate(body)
                  if r.kind == "act" and r.act is act), None)
        if i is not None:
            end = max(i + 1, min(len(body), end0 + len(body) - n0))
            if self._fit_back(body, end) > i:
                end = self._fit_forward(body, i)
            self.scroll = len(body) - end
        self.relayout()

    _text_rows = staticmethod(text_rows)
    _code_rows = staticmethod(code_rows)

    def _step_rows(self, acts: list, w: float, current: bool) -> list[Row]:
        """A run of steps between two blocks of text: its last few, or
        all of them after Ctrl+O. The run still going shows a few more."""
        keep = ACT_ROWS_ALL if self.show_all else (
            ACT_ROWS_BUSY if current else ACT_ROWS_IDLE)
        # A step with a bar running stays in sight, however many came
        # after it (a download left going in the background).
        a = self.agent
        shown = [x for x in acts[:-keep] if a.job_of(x) is not None] \
            + acts[-keep:]
        hidden = len(acts) - len(shown)
        rows: list[Row] = []
        lh = line_h(ACT_SIZE) + 3
        if hidden and not self.show_all:
            rows.append(Row("toggle", lh, size=ACT_SIZE,
                            label=f"↑ {hidden} earlier step{'' if hidden == 1 else 's'}   ^O"))
        elif self.show_all and len(acts) > ACT_ROWS_IDLE:
            rows.append(Row("toggle", lh, size=ACT_SIZE,
                            label="↓ fewer steps   ^O"
                            + (f"   ({hidden} not shown)" if hidden else "")))
        cell = text.advance(ACT_SIZE)
        for act in shown:
            rows.append(Row("act", lh, x=cell * 2, size=ACT_SIZE,
                            key=f"a{act.id}", src=act.text, start=0,
                            end=len(act.text), act=act))
            job = a.job_of(act)
            if job is not None:
                rows += self._job_rows(job)
            if act.id not in self.expanded:
                continue
            rows += self._code_rows(f"d{act.id}", act.detail or act.text, w,
                                    theme.CYAN_DIM, color=theme.CYAN_BLOOM)
            if act.kind != "tool":
                continue
            if act.result is None:
                rows.append(Row("note", lh, x=cell * 1.5, size=ACT_SIZE,
                                label="running…", color=theme.BASE_MUTED))
                continue
            lines = self._code_rows(f"o{act.id}", act.result or "(no output)",
                                    w, FAIL_ACCENT if act.failed
                                    else theme.BASE_MUTED, color=theme.HUD_FG)
            full = act.id in self.full_output
            cap = OUT_ROWS_FULL if full else OUT_ROWS
            if len(lines) > cap:
                more = len(lines) - cap
                lines = lines[:cap]
                lines.append(Row("outmore", lh, x=cell * 1.5, size=ACT_SIZE,
                                 act=act, color=theme.BASE_MUTED,
                                 label=f"… {more} more lines"
                                 + ("" if full else " — click")))
            rows += lines
        return rows

    def _job_rows(self, job, loose: bool = False) -> list[Row]:
        """A job's bar, and the line of its stages and status if it has
        one. Indented under its step; one with no step gets a mark of
        its own where a step's would be."""
        lh = line_h(ACT_SIZE) + 3
        x = text.advance(ACT_SIZE) * 2
        rows = [Row("job", lh, x=x, size=ACT_SIZE, job=job,
                    label="⟳" if loose else "")]
        if job.stages or job.stage or job.text:
            rows.append(Row("jobinfo", lh, x=x, size=ACT_SIZE, job=job))
        return rows

    # ── scrolling back ──────────────────────────────────────────────────
    def _combined(self, w: float) -> tuple[list[Row], dict]:
        """Every exchange here, oldest first, the turn on show last, under
        the row that says what lies above them; and where each starts
        (by exchange id, the turn on show's as "live")."""
        a = self.agent
        rows = [self._edge_row()]
        tops: dict = {}
        for x in a.earlier():
            tops[x["id"]] = len(rows)
            rows += self._past_rows(x, w)
        tops["live"] = len(rows)
        rows.append(self._stamp(a.at, a.heard_on))
        for i, line in enumerate([a.heard] if a.heard else []):
            rows += self._you_rows(f"h{i}_", line, w)
        rows += self._live_body(w)
        return rows, tops

    def _edge_row(self) -> Row:
        """What is above the oldest exchange here."""
        a = self.agent
        if a.back_loading:
            label = "loading earlier…"
        elif a.more_back:
            label = "↑ earlier — ^K"
        elif a.edge == "compacted":
            label = f"earlier is in {a.name.capitalize()}'s memory now: ask her"
        else:
            label = "the chat starts here"
        return Row("edge", line_h(ACT_SIZE) + 10, size=ACT_SIZE, label=label,
                   color=theme.BASE_MUTED)

    def _stamp(self, at: float | None, device: str | None = None) -> Row:
        """An exchange's first row: when it began, and the device it was
        asked on when that is another one."""
        words = when(at)
        if device and device != getattr(self.agent, "device", None):
            words += f"  on {device}" if words else f"on {device}"
        return Row("stamp", STAMP_H, size=TAG_SIZE, label=words)

    def _you_rows(self, key: str, line: str, w: float) -> list[Row]:
        """A line of yours, as one said mid-turn shows: your colour, a bar."""
        return text_rows(key, line, BODY_SIZE, w, YOU_ACCENT,
                         x=text.advance(ACT_SIZE) * 1.5, bar=YOU_ACCENT)

    def _note_rows(self, key: str, value: str, w: float) -> list[Row]:
        """A line that is nobody's words: the hub's, an announcement."""
        cell = text.advance(ACT_SIZE)
        rows = text_rows(key, value, ACT_SIZE, w, theme.HUD_FG, x=cell * 2)
        if rows:
            rows[0].bullet, rows[0].bullet_x = "·", 0.0
        return rows

    def _past_rows(self, x: dict, w: float) -> list[Row]:
        """An earlier exchange: its date, your lines, her reply, what she
        showed (as files: their bytes come when one is opened). Kept, as
        an exchange never changes."""
        a = self.agent
        key = (x["id"], w, own_accent(a), x["at"] is None or when(x["at"]))
        got = self._past.get(x["id"])
        if got is not None and got[0] == key:
            return got[1]
        k = f"x{x['id']}"
        rows = [self._stamp(x["at"], x["device"])]
        lines = list(x["lines"])
        if x["source"] == "hub" and lines:
            rows += self._note_rows(f"{k}n_", lines.pop(0), w)
        if x["source"] == "announcement":
            lines = []
        for i, line in enumerate(lines):
            rows += self._you_rows(f"{k}y{i}_", line, w)
        if x["reply"].strip():
            if x["source"] == "announcement":
                rows += self._note_rows(f"{k}a_", x["reply"].strip(), w)
            else:
                rows += markdown_rows(x["reply"].strip(), w, f"{k}m",
                                      own_accent(a))
        for item in x["shown"]:
            sid = str(item.get("id") or "")
            if sid:
                a.shown.setdefault(sid, {
                    "id": sid, "kind": str(item.get("kind") or "file"),
                    "name": str(item.get("name") or sid),
                    "mime": str(item.get("mime") or ""),
                    "size": item.get("size"),
                    "caption": str(item.get("caption") or "")})
                rows.append(Row("file", line_h(BODY_SIZE) + 3, label=sid))
        self._past[x["id"]] = (key, rows)
        return rows

    def _back_window(self, w: float) -> list[Row]:
        """The rows from where the plate is scrolled back to, as many as
        fit, and one saying the newer ones are below. Near the top, the
        next page of earlier exchanges is asked for."""
        body, tops = self._combined(w)
        self._past = {k: v for k, v in self._past.items() if k in tops}
        start = self._back_start(body, tops)
        end = self._fit_forward(body, start)
        if sum(r.h for r in body[:start]) < self._budget():
            self.agent.want_older()
        return body[start:end] + [Row("more", line_h(ACT_SIZE) + 2,
                                      size=ACT_SIZE, label="↓ newer — ^J")]

    def _back_start(self, body: list[Row], tops: dict) -> int:
        """Where the plate is scrolled back to, as an index of `body`: the
        anchor's exchange (or, forgotten, the one after it) and how far
        into it."""
        key, offset = self.back
        top = tops.get(key)
        if top is None:
            later = [t for k, t in tops.items()
                     if k != "live" and isinstance(key, int) and k > key]
            top, offset = (min(later) if later else tops["live"]), 0
        return max(0, min(len(body) - 1, top + offset))

    def _anchor(self, tops: dict, index: int) -> tuple:
        """`index` of the rows as (the exchange it is in, how far in). The
        edge row above them all counts as -1 into the first exchange, so
        a page that arrives above it pushes nothing down."""
        above = [(t, k) for k, t in tops.items() if t <= index]
        top, key = max(above, key=lambda tk: tk[0]) if above \
            else min(((t, k) for k, t in tops.items()), key=lambda tk: tk[0])
        return key, index - top

    def _step(self, body: list[Row], starts: list[int], s: int,
              up: bool) -> int:
        """From index `s`, to the next exchange's start that way, or half
        the plate's height when that is further."""
        half = self._budget() / 2
        if up:
            stop = max((t for t in starts if t < s), default=0)
            acc, i = 0.0, s
            while i > stop and acc + body[i - 1].h <= half:
                i -= 1
                acc += body[i].h
            return i if i < s else max(stop, s - 1)
        stop = min((t for t in starts if t > s), default=len(body))
        acc, i = 0.0, s
        while i < stop and acc + body[i].h <= half:
            acc += body[i].h
            i += 1
        return i if i > s else min(stop, s + 1)

    def older(self) -> None:
        """Ctrl+K: up through the turn on show, then into the exchanges
        before it; at the top, as far as there is."""
        a = self.agent
        if self.back is None and self._live_top() > 0:
            self.scroll_by(MAX_BODY_ROWS // 2)  # the turn's own start first
            return
        body, tops = self._combined(self.inner_w)
        s = tops["live"] if self.back is None else self._back_start(body, tops)
        if s <= 0:
            a.want_older()
            self.invalidate()
            return
        self.anchor = self.focus = None
        self.back = self._anchor(tops, self._step(body, sorted(
            {0, *tops.values()}), s, up=True))
        self.relayout()

    def newer(self) -> None:
        """Ctrl+J: back down; reaching the turn on show, live again."""
        if self.back is None:
            self.scroll_by(-(MAX_BODY_ROWS // 2))
            return
        body, tops = self._combined(self.inner_w)
        s = self._step(body, sorted({0, *tops.values()}),
                       self._back_start(body, tops), up=False)
        self.anchor = self.focus = None
        if s >= tops["live"]:
            self.back, self.scroll = None, 0
        else:
            self.back = self._anchor(tops, s)
        self.relayout()

    def _live_top(self) -> int:
        """The first row of the turn on show the plate shows, live."""
        body = self._body
        if sum(r.h for r in body) <= self._budget():
            return 0
        return self._fit_back(body, len(body) - self.scroll)

    # ── the hub's devices ───────────────────────────────────────────────
    def has_devices(self) -> bool:
        """Whether the agent keeps a device list (the hub's does)."""
        return getattr(self.agent, "devices", None) is not None

    def _devices(self) -> list:
        return getattr(self.agent, "devices", None) or []

    def _device_rows(self) -> list[Row]:
        """Two lines a device: whether it is online, and what it has
        connected and lends."""
        lh = line_h(BODY_SIZE) + 6
        devices = self._devices()
        rows: list[Row] = []
        if not devices:
            rows.append(Row("note", lh, label="no devices heard of yet"
                            if getattr(self.agent, "connected", False)
                            else "not on the line", color=theme.BASE_MUTED))
        cell = text.advance(ACT_SIZE)
        for i in range(len(devices)):
            if i:
                rows.append(Row("gap", 4))
            rows.append(Row("device", lh, label=str(i)))
            rows.append(Row("devinfo", line_h(ACT_SIZE) + 3, x=cell * 2,
                            size=ACT_SIZE, label=str(i)))
        rows.append(Row("note", line_h(ACT_SIZE) + 8, size=ACT_SIZE,
                        label="^D or esc close list",
                        color=theme.BASE_MUTED))
        return rows

    def toggle_devices(self) -> None:
        self.show_devices = not self.show_devices
        if self.show_devices:
            self.anchor = self.focus = None
        self.relayout()

    def close_devices(self) -> None:
        self.show_devices = False
        self.relayout()

    def children(self):
        return self.choices

    def _layout(self) -> list[tuple[Row, float]]:
        y = self.rect.top() + PAD_Y
        out = []
        keys = None
        for row in self._build():
            out.append((row, y))
            if row.kind == "keys":
                keys = y
            y += row.h
        # The answer buttons follow their row, so hover and clicks find
        # them wherever it is; with no question they are nowhere.
        if keys is None:
            for c in self.choices:
                c.hide()
        else:
            x = self.rect.left() + PAD_X
            for c in self.choices:
                x = c.place(x, keys + 8) + CHOICE_GAP
        return out

    def measure(self, avail: Size) -> Size:
        return Size(self.width, sum(r.h for r in self._build()) + 2 * PAD_Y)

    def scroll_by(self, rows: int) -> None:
        self.scroll = max(0, self.scroll + rows)
        self.invalidate()

    def toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.relayout()

    # ── selection ───────────────────────────────────────────────────────
    def _order(self, laid) -> dict[str, int]:
        order: dict[str, int] = {}
        for i, (row, _y) in enumerate(laid):
            if row.selectable and row.key not in order:
                order[row.key] = i
        return order

    def _pos_at(self, x: float, y: float, laid=None) -> tuple[str, int] | None:
        laid = laid if laid is not None else self._layout()
        best, best_d = None, 1e9
        for row, top in laid:
            if not row.selectable:
                continue
            d = 0.0 if top <= y < top + row.h else min(abs(y - top),
                                                       abs(y - top - row.h))
            if d < best_d:
                best, best_d = row, d
        if best is None:
            return None
        left = self.rect.left() + PAD_X + best.x
        col = round((x - left) / text.advance(best.size))
        return best.key, max(best.start, min(best.end, best.start + col))

    def _link_at(self, x: float, y: float, laid) -> str | None:
        """The address of the link under the pointer, if any."""
        row = self._row_at(y, laid)
        if row is None or row.kind != "line" or not row.spans:
            return None
        left = self.rect.left() + PAD_X + row.x
        offset = row.start + int((x - left) // text.advance(row.size))
        if not row.start <= offset < row.end:
            return None
        for span in row.spans:
            if span.style == "link" and span.url \
                    and span.start <= offset < span.end:
                return span.url
        return None

    def _row_at(self, y: float, laid) -> Row | None:
        for row, top in laid:
            if top <= y < top + row.h:
                return row
        return None

    def _range(self, order):
        if self.anchor is None or self.focus is None:
            return None
        if self.anchor[0] not in order or self.focus[0] not in order:
            return None
        a = (order[self.anchor[0]], self.anchor[1])
        b = (order[self.focus[0]], self.focus[1])
        return (a, b) if a <= b else (b, a)

    def _selected_in(self, row: Row, order, rng) -> tuple[int, int] | None:
        if rng is None or not row.selectable:
            return None
        (lk, lo), (hk, hi) = rng
        k = order[row.key]
        if k < lk or k > hk:
            return None
        s = row.start if k > lk else max(row.start, lo)
        e = row.end if k < hk else min(row.end, hi)
        return (s, e) if s < e else None

    def selected_text(self) -> str:
        laid = self._layout()
        order = self._order(laid)
        rng = self._range(order)
        if rng is None:
            return ""
        spans: dict[str, list[int]] = {}
        sources: dict[str, str] = {}
        for row, _y in laid:
            sel = self._selected_in(row, order, rng)
            if sel is None:
                continue
            got = spans.setdefault(row.key, [sel[0], sel[1]])
            got[0], got[1] = min(got[0], sel[0]), max(got[1], sel[1])
            sources[row.key] = row.src
        return text.unpad("\n".join(sources[k][s:e]
                                    for k, (s, e) in spans.items()))

    def flash(self, value: str, color: str | None = None) -> None:
        """A word in the heading for a moment: "copied", "saved"..."""
        self._flash = (value, color)
        self._copied_until = time.monotonic() + COPIED_S * 2
        self.invalidate()

    def last_shown(self) -> str | None:
        """The id of the last thing the agent showed in the turn."""
        ids = [said for key, _after, said in self.agent.said_parts()
               if key.startswith("s")]
        return ids[-1] if ids else None

    def copy(self, selection: str = "clipboard") -> bool:
        value = self.selected_text()
        if not value or not clipboard.copy(value, selection):
            return False
        if selection == "clipboard":
            self._copied_until = time.monotonic() + COPIED_S
            self._flash = ("copied", None)
            self.invalidate()
        return True

    def _select_around(self, pos: tuple[str, int], laid, whole: bool) -> None:
        key, off = pos
        src = next((r.src for r, _y in laid if r.key == key), "")
        if whole:
            self.anchor, self.focus = (key, 0), (key, len(src))
            return
        s = e = off
        while s > 0 and not src[s - 1].isspace():
            s -= 1
        while e < len(src) and not src[e].isspace():
            e += 1
        self.anchor, self.focus = (key, s), (key, e)

    def _on_drag(self, _w, x: float, y: float, commit: bool) -> None:
        if not self._dragging:                      # the press
            self._dragging = True
            self._moved = False
            self._press = (x, y)
            self._press_pos = self._pos_at(x, y)
            self.touched()
            if not commit:
                return
        if not commit:                              # motion while held
            px, py = self._press
            if abs(x - px) + abs(y - py) > 3:
                self._moved = True
            if self._moved and self._press_pos is not None:
                self.anchor = self._press_pos
                self.focus = self._pos_at(x, y)
                self.invalidate()
            return
        self._dragging = False                      # the release
        if self._moved:
            if self._press_pos is not None:
                self.focus = self._pos_at(x, y)
                self.invalidate()
            self.copy("primary")
            return
        self._click_at(x, y)

    def _click_at(self, x: float, y: float) -> None:
        now = time.monotonic()
        t0, x0, y0, n = self._clicks
        n = n + 1 if (now - t0 < CLICK_S and abs(x - x0) + abs(y - y0) < 6) \
            else 1
        self._clicks = (now, x, y, n)
        laid = self._layout()
        if n >= 2:
            pos = self._pos_at(x, y, laid)
            if pos is not None:
                self._select_around(pos, laid, whole=n >= 3)
                self.copy("primary")
                self.invalidate()
            return
        had = self.anchor is not None
        self.anchor = self.focus = None
        url = self._link_at(x, y, laid)
        if url:
            self.link_clicked(url)
            return
        row = self._row_at(y, laid)
        if row is not None and row.kind in MEDIA_KINDS:
            self.media_clicked(row.label)
            return
        if row is not None and row.kind == "act" and row.act is not None:
            self._toggle_step(row.act, self.expanded)
        elif row is not None and row.kind == "toggle":
            self.toggle_all()
        elif row is not None and row.kind == "outmore" and row.act is not None:
            self._toggle_step(row.act, self.full_output)
        elif had:
            self.invalidate()

    # ── input ───────────────────────────────────────────────────────────
    def click(self, button: int, x: float = 0.0, y: float = 0.0) -> bool:
        if button in (4, 5):
            self.scroll_by(2 if button == 4 else -2)
            return True
        return False

    # ── paint ───────────────────────────────────────────────────────────
    def paint_bounds(self) -> skia.Rect:
        return self.rect.makeOutset(TRACE_BLEED, TRACE_BLEED)

    def paint(self, canvas: skia.Canvas) -> None:
        a = self.agent
        accent = accent_of(a)
        paint_plate(canvas, self.rect, accent, fill_of(a))
        self.trace.paint(canvas, self.rect, a, accent)
        laid = self._layout()
        order = self._order(laid)
        rng = self._range(order)
        left = self.rect.left() + PAD_X
        right = self.rect.right() - PAD_X
        clip = canvas.getLocalClipBounds()
        for row, top in laid:
            if top > clip.bottom() or top + row.h < clip.top():
                continue
            box = skia.Rect.MakeLTRB(left, top, right, top + row.h)
            sel = self._selected_in(row, order, rng)
            kind = row.kind
            if kind == "header":
                self._paint_header(canvas, box, accent)
            elif kind == "context":
                context.paint(canvas, box, a, own_accent(a))
            elif kind == "stamp":
                self._paint_stamp(canvas, box, row)
            elif kind == "act":
                self._paint_act(canvas, box, row, sel)
            elif kind == "job":
                if row.label:
                    self._glyph(canvas, box, row.label, theme.BASE_MUTED)
                self.bars.paint_row(canvas, box, row.job, left + row.x,
                                    own_accent(a))
            elif kind == "jobinfo":
                self.bars.paint_info(canvas, box, row.job, left + row.x,
                                     own_accent(a))
            elif kind == "think":
                tail = think_line(a, time.monotonic())
                self._glyph(canvas, box, "∴", own_accent(a))
                x = left + text.advance(ACT_SIZE) * 2
                text.draw(canvas, text.ellipsize(tail, ACT_SIZE, right - x),
                          x, text.baseline_in(box, ACT_SIZE), ACT_SIZE,
                          theme.color(own_accent(a)))
            elif kind == "rule":
                p = skia.Paint(AntiAlias=False)
                p.setColor(theme.color(accent, 0.35))
                canvas.drawRect(skia.Rect.MakeXYWH(left, top + row.h / 2 - 1,
                                                   36, 1.5), p)
            elif kind == "hr":
                p = skia.Paint(AntiAlias=False)
                p.setColor(theme.color(theme.BASE_MUTED, 0.6))
                canvas.drawRect(skia.Rect.MakeXYWH(left, top + row.h / 2,
                                                   right - left, 1), p)
            elif kind == "line":
                self._paint_line(canvas, box, row, sel, accent)
            elif kind in ("note", "more", "toggle", "outmore", "edge"):
                color = row.color if kind in ("note", "outmore", "edge") \
                    else theme.BASE_MUTED
                text.draw(canvas, row.label, left + row.x,
                          text.baseline_in(box, row.size), row.size,
                          theme.color(color))
            elif kind == "keys":
                self._paint_keys(canvas)
            elif kind == "image":
                paint_image(canvas, box, row, self.pictures, a)
            elif kind == "file":
                paint_file(canvas, box, row, a, own_accent(a))
            elif kind == "media":
                paint_waiting(canvas, box, row, a)
            elif kind == "device":
                self._paint_device(canvas, box, int(row.label), accent)
            elif kind == "devinfo":
                self._paint_devinfo(canvas, box, row)

    def _paint_stamp(self, canvas, box, row: Row) -> None:
        """An exchange's date, muted, on the lower part of its row: the
        upper part is the space that sets it off from the one before."""
        if not row.label:
            return
        lower = skia.Rect.MakeLTRB(box.left(), box.bottom() - line_h(TAG_SIZE)
                                   - 2, box.right(), box.bottom())
        base = text.baseline_in(lower, TAG_SIZE)
        text.draw(canvas, "//", box.left(), base, TAG_SIZE,
                  theme.color(SLASH_FG, 0.6), bold=True)
        text.draw(canvas, row.label, box.left() + text.measure("// ", TAG_SIZE,
                                                               bold=True),
                  base, TAG_SIZE, theme.color(theme.BASE_MUTED))

    def _device(self, index: int) -> dict | None:
        devices = self._devices()
        return devices[index] if 0 <= index < len(devices) else None

    def _paint_device(self, canvas, box, index: int, accent) -> None:
        """■ laptop  this one                                  online"""
        d = self._device(index)
        if d is None:
            return
        base = text.baseline_in(box, BODY_SIZE)
        online = d["online"]
        if online:
            when, when_color = "online", theme.LIME_BRIGHT
        elif d["last_seen"]:
            when, when_color = f"seen {ago(d['last_seen'])}", theme.BASE_MUTED
        else:
            when, when_color = "never seen", theme.BASE_MUTED
        shapes.text_light(canvas, box.left(), base, BODY_SIZE,
                          theme.color(theme.LIME_BRIGHT if online
                                      else theme.BASE_MUTED), on=online)
        x = box.left() + text.advance(BODY_SIZE) * 2
        name = d["name"]
        text.draw(canvas, name, x, base, BODY_SIZE,
                  theme.color(theme.HUD_FG_BRIGHT if online
                              else theme.HUD_FG), bold=True)
        if name == getattr(self.agent, "device", None):
            x += text.measure(name + "  ", BODY_SIZE, bold=True)
            text.draw(canvas, "this one", x, text.baseline_in(box, ACT_SIZE),
                      ACT_SIZE, theme.color(accent))
        tw = text.measure(when, ACT_SIZE)
        text.draw(canvas, when, box.right() - tw,
                  text.baseline_in(box, ACT_SIZE), ACT_SIZE,
                  theme.color(when_color))

    def _paint_devinfo(self, canvas, box, row: Row) -> None:
        """Its connections, its hands, and whether it wakes by push."""
        d = self._device(int(row.label))
        if d is None:
            return
        parts = []
        if d["clients"]:
            parts.append("connected: " + ", ".join(d["clients"]))
        parts.append("hands: " + ", ".join(d["hands"]) if d["hands"]
                     else "no hands")
        if d["wakes"]:
            parts.append("wakes by push")
        x = box.left() + row.x
        text.draw(canvas, text.ellipsize("  ·  ".join(parts), ACT_SIZE,
                                         box.right() - x),
                  x, text.baseline_in(box, ACT_SIZE), ACT_SIZE,
                  theme.color(theme.HUD_FG))

    def _paint_header(self, canvas, box, accent) -> None:
        a = self.agent
        if self.show_devices:
            paint_heading(canvas, box, NAME_SIZE,
                          (("DEVICES", accent, True),
                           ("on the hub", theme.BASE_MUTED, False)))
            return
        if self._copied():
            tag = (self._flash[0], self._flash[1] or accent, True)
        else:
            tag = (STATUS_WORDS.get(a.phase, a.phase),
                   ASK_ACCENT if a.phase == "asking" else theme.BASE_MUTED,
                   False)
        paint_heading(canvas, box, NAME_SIZE, ((a.name, accent, True), tag))
        tag_base = text.baseline_in(box, TAG_SIZE)
        # Right side: the model and effort (Ctrl+M / Ctrl+T), and the turn
        # clock before them while it works.
        setting = f"{a.model_label()} · {a.effort_label()}"
        x = box.right() - text.measure(setting, TAG_SIZE)
        text.draw(canvas, setting, x, tag_base, TAG_SIZE,
                  theme.color(theme.BASE_MUTED))
        if a.busy:
            clock = clock_text(time.monotonic() - a.turn_started)
            w = text.measure(clock, TAG_SIZE)
            text.draw(canvas, clock, x - 14 - w, tag_base, TAG_SIZE,
                      theme.color(theme.YELLOW_DIM))

    def _glyph(self, canvas, box, glyph: str, color: str) -> None:
        text.draw(canvas, glyph, box.left(), text.baseline_in(box, ACT_SIZE),
                  ACT_SIZE, theme.color(color), bold=True)

    def _paint_act(self, canvas, box, row: Row, sel) -> None:
        act, busy = row.act, self.agent.busy
        open_ = act.id in self.expanded
        if act.kind == "tool" and act.failed:
            glyph, color = "×", FAIL_ACCENT
        elif act.kind == "tool":
            glyph, color = ("⌄" if open_ else "›"), TOOL_FG
        elif act.kind == "info":
            glyph, color = "!", theme.YELLOW_MID
        else:
            glyph, color = ("⌄" if open_ else "·"), theme.HUD_FG
        live = busy or open_ or act.failed
        self._glyph(canvas, box, glyph, color if live else theme.BASE_MUTED)
        fg = color if live else (theme.CYAN_MID if act.kind == "tool"
                                 else theme.BASE_MUTED)
        x = box.left() + row.x
        right = box.right()
        if act.summary:
            # What its progress came to, at the end of its line.
            sw = text.measure(act.summary, ACT_SIZE)
            text.draw(canvas, act.summary, right - sw,
                      text.baseline_in(box, ACT_SIZE), ACT_SIZE,
                      theme.color(theme.BASE_MUTED))
            right -= sw + 16
        shown = text.ellipsize(act.text, ACT_SIZE, right - x)
        if sel is not None:
            self._highlight(canvas, box, row, sel, len(shown))
        text.draw(canvas, shown, x, text.baseline_in(box, ACT_SIZE),
                  ACT_SIZE, theme.color(fg))

    def _highlight(self, *args) -> None:
        highlight(*args)

    def _paint_line(self, canvas, box, row: Row, sel, accent) -> None:
        if row.key.startswith("m"):         # the reply: through the reveal
            self.reveal.paint(canvas, box, row, sel, accent)
        else:
            paint_line(canvas, box, row, sel, accent)

    def _paint_keys(self, canvas) -> None:
        for c in self.choices:
            c.paint(canvas)
