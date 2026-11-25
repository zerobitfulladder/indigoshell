"""An open input or menu: the shell's, from the key that opened it to the
key, click or loss of focus that closes it (PROTOCOL.md, section 4).

A `Prompt` is one open INPUT or MENU of an app window's bar (or the
shell's own: MOD+D's launcher, launcher.py, `local` rows of its own).
While it is open it has the keyboard (the app service holds it:
service.py; the launcher's window grabs it), and every key typed
reaches `key` in the order typed, whichever window X reported it on.
For an app's input it shows in two places (the launcher frames the same
two as its own panel):

  - an input turns the app's section of the top bar into its box (an
    `InputBox`, kinds/input.py): the icon, the text being typed, the
    count, the toggles. The box edits the text itself (TextInput's
    keys), so every key shows at once with no round trip.
  - its list drops flush under the section (drop.py, a window of its own
    against the bar's bottom edge): an input's suggestions, a menu's
    rows; for an input it opens with the first rows there are.

Where the rows come from:

  query     the app's: the text goes out as `query` whenever it changes,
            one in flight (newer text waits, and only the newest goes
            when the answer comes); an answer for an older seq than the
            last one taken is dropped, and the latest may come again
            (find's count after a step). Enter with the latest text
            unanswered waits for its answer (ENTER_WAIT_S), then submits
            the row selected then, or the text alone.
  rows      static: filtered here with the shell's fuzzy match, the
            app's order with nothing typed (`StaticRows`).
  a menu    the same, the filter line shown at the list's top once
            something is typed; a row with `menu` opens its submenu
            beside it (Right, Return, a click), Left or Escape go back.
  local     the shell's own source (the app catalog, catalog.py): searched
            and picked in the shell, no app involved. MOD+D's launcher
            (launcher.py) is such an input, its box and list framed as a
            panel of its own instead of the bar's.

Keys, while an input is open:

    type, edit          the box's (TextInput: words, selection)
    Up/Down, Tab        the selection; also Ctrl+P/N and Ctrl+K/J
    PageUp/PageDown     a page at a time
    Return              submit the selected row (`submit`, its `row`;
                        null with none); a dimmed row flashes instead
    Shift+Return        submit the text itself (`row` null, `shift`)
    a row action chord  `act` on the selected row, when it lists that
                        action (Flux: Shift+Delete on a bookmark); with
                        `drops` the row leaves at once and the text is
                        queried again; refused with the flash elsewhere
    a toggle's chord    `press` for that toggle
    its own chord       again: the text selected whole (a `steps` input
                        stepping: back to typing)
    Escape              close (`closed`, by escape)

`keep`: Enter submits and the input stays (Shift+Enter, `shift`).
`steps`: Enter submits and turns the box to stepping: the text stays,
typing stops, `n` and `N` submit again (`shift` false / true); Enter,
Escape or a click close it. Every key is the shell's meanwhile.

Closing (`close(by)`) is one way out for all of it: `closed` to the app
(by enter, escape, click, focus or shell), the box and the list gone,
the keyboard and the pointer given back.
"""

import logging
import time

from .. import fuzzy
from ..widgets.base import KeyEvent
from ..widgets.text_input import ctrl_letter
from . import bar as B
from .kinds.input import InputBox, count_of

log = logging.getLogger(__name__)

PAGE = 12                       # rows the list shows at once (drop.py's slots)
REJECT_S = 0.28                 # a refused row's flash

_RETURN, _KP_ENTER, _ESCAPE = 0xFF0D, 0xFF8D, 0xFF1B
_UP, _DOWN, _KP_UP, _KP_DOWN = 0xFF52, 0xFF54, 0xFF97, 0xFF99
_LEFT, _RIGHT, _KP_LEFT, _KP_RIGHT = 0xFF51, 0xFF53, 0xFF96, 0xFF98
_PAGE_UP, _PAGE_DOWN, _KP_PAGE_UP, _KP_PAGE_DOWN = 0xFF55, 0xFF56, 0xFF9A, 0xFF9B
_TAB, _ISO_LEFT_TAB = 0xFF09, 0xFE20
_BACKSPACE = 0xFF08

_NAMED = {"delete": (0xFFFF, 0xFF9F), "backspace": (0xFF08,), "enter": (0xFF0D, 0xFF8D),
          "tab": (0xFF09, 0xFE20), "space": (0x20,), "up": (0xFF52, 0xFF97),
          "down": (0xFF54, 0xFF99), "left": (0xFF51, 0xFF96), "right": (0xFF53, 0xFF98),
          "home": (0xFF50, 0xFF95), "end": (0xFF57, 0xFF9C), "pageup": (0xFF55, 0xFF9A),
          "pagedown": (0xFF56, 0xFF9B), **{f"f{n}": (0xFFBD + n,) for n in range(1, 13)}}


def chord_matches(ev: KeyEvent, chord: str) -> bool:
    """Whether a key press is `chord` ("shift+delete", "ctrl+l", "n",
    "alt+c"): exactly those modifiers held, the key itself in either
    case. A lone letter or digit counts with or without Shift as typed:
    "n" is n, "shift+n" is N."""
    parsed = B.parse_chord(chord)
    if parsed is None:
        return False
    mods, key = parsed
    held = {m for m in ("ctrl", "shift", "alt", "super") if getattr(ev, m)}
    if key in _NAMED:
        return held == mods and ev.keysym in _NAMED[key]
    k = ev.keysym
    if not 0 < k < 0x100 or chr(k).lower() != key:
        return False
    if key.isalpha() and not ({"ctrl", "alt", "super"} & held):
        # Typed: Shift is in the letter's case (keysym "N"), and may be
        # all the modifiers there are.
        return held - {"shift"} == mods - {"shift"} and (chr(k).isupper() == ("shift" in mods))
    return held == mods


class StaticRows:
    """Rows filtered with the shell's fuzzy match, as the old launcher's
    actions were: the given order with nothing typed; best first with
    something typed, a prefix of the title counting extra. The selection
    starts on the first row that can be picked."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = [r for r in rows if isinstance(r, dict)]
        self.texts = [fuzzy.Text(str(r.get("title") or "")) for r in self.rows]

    @property
    def total(self) -> int:
        return len(self.rows)

    def search(self, query: str) -> tuple[list[dict], int]:
        folded, _ = fuzzy.fold(query.strip())
        found = []
        for i, (row, t) in enumerate(zip(self.rows, self.texts)):
            if not folded:
                found.append((0.0, i, row, ()))
                continue
            hit = fuzzy.match(folded, t)
            if hit is not None:
                bonus = 2.0 if t.folded.startswith(folded) else 0.0
                found.append((hit.score + bonus, i, row, hit.positions))
        found.sort(key=lambda f: (-f[0], f[1]))
        out = [{**row, "match": list(pos)} if pos else row for _s, _i, row, pos in found]
        first = next((i for i, r in enumerate(out) if r.get("enabled") is not False), 0)
        return out, first


class Level:
    """One menu shown: its rows as filtered, the selection, the scroll."""

    def __init__(self, name, spec: dict, anchor_y: int = 0) -> None:
        self.name = name
        self.source = StaticRows(spec.get("rows") or [])
        self.rows: list[dict] = []
        self.selected = 0
        self.top = 0
        self.anchor_y = anchor_y        # the row of the level before that opened it
        self.filter = ""


class Prompt:
    """One open input or menu (the module docstring).

    `owner` is the AppWindow whose bar it is (None for the shell's own);
    `host` the app service, which holds the keyboard and the pointer for
    it, shows its list and closes it on focus lost (service.py);
    `local` a source of the shell's own: `search(text) -> (rows,
    selected)`, `pick(row, text)`, `total`, `count(rows)`. `box` the
    input's box when it is not the bar's (MOD+D's launcher: launcher.py).
    """

    def __init__(self, host, owner, what: str, name, spec: dict, *, local=None,
                 anchor=None, chord: str | None = None, box=None) -> None:
        self.host = host
        self.owner = owner
        self.what = what                # "input" | "menu"
        self.name = name
        self.spec = spec
        self.local = local
        self.anchor = anchor            # the item it drops under (a menu's), or None
        self.chord = chord              # the chord that opened it
        self.open = True
        # An input's box: the bar's (kinds/input.py), or one of the same
        # shape framed otherwise (the launcher's query line).
        self.box = (box or InputBox()) if what == "input" else None
        self.levels: list[Level] = []
        # The list (an input's).
        self.rows: list[dict] = []
        self.selected = 0
        self.top = 0
        self.total: int | None = None
        self.count: tuple[str, str] | None = None
        self.answered = False           # rows have come (the list may open)
        self.static: StaticRows | None = None
        # Queries: the last seq sent, the one in flight, the last taken.
        self.sent = self.inflight = None
        self.taken = -1
        self.dirty = False              # text changed since the last query sent
        self.enter: tuple[bool, float] | None = None    # Enter waiting: (shift, by when)
        self._enter_timer = None
        self.stepping = False
        self.reject_until = 0.0
        if what == "input":
            self.box.show(spec)
            self.box.on_toggle = self._toggle
            self.box.field.on_change = self._changed
            start = spec.get("text") or ""
            self.box.field.set_text(start, notify=False)
            if start and spec.get("select"):
                self.box.field.select_all()
            if spec.get("rows") is not None:
                self.static = StaticRows(spec["rows"])
        else:
            self.levels = [Level(name, spec)]
            self._filter_menu()

    def start(self) -> None:
        """Shown: the first rows (an input's first query out, its static
        rows filtered)."""
        if self.what == "input" and self.open:
            self._update(self.text)

    # ── what the list shows ─────────────────────────────────────────────
    @property
    def level(self) -> Level | None:
        return self.levels[-1] if self.levels else None

    def list_rows(self) -> list[dict]:
        return self.level.rows if self.what == "menu" else self.rows

    def wants_list(self) -> bool:
        """Whether the drop-down shows: a menu always; an input once it
        has rows (and not while stepping through a find)."""
        if self.what == "menu":
            return True
        return bool(self.rows) and not self.stepping

    def _changed(self, text: str) -> None:
        self._update(text)

    def _update(self, text: str) -> None:
        """The text changed (or the input opened with it)."""
        if self.local is not None:
            rows, start = self.local.search(text)
            self._take_rows(rows, start, total=self.local.total)
            self.count = self.local.count(rows) if hasattr(self.local, "count") else None
            self._show_count()
        elif self.static is not None:
            rows, start = self.static.search(text)
            self._take_rows(rows, start, total=self.static.total)
            self._show_count()
        elif self.spec.get("query"):
            self.dirty = True
            self._query()
        self.host.changed(self)

    def _query(self) -> None:
        """The newest text out, when none is in flight."""
        if not self.open or self.inflight is not None or not self.dirty:
            return
        self.dirty = False
        seq = self.host.next_seq(self)
        self.sent = self.inflight = seq
        log.debug("input %r: query seq %d sent (%d characters)", self.name, seq, len(self.text))
        self._send(B.QUERY, input=self.name, seq=seq, text=self.text)

    @property
    def text(self) -> str:
        return self.box.field.text if self.box is not None else ""

    def answer(self, msg: dict) -> None:
        """The app's answer to a query (checked already)."""
        if not self.open:
            return
        seq = msg.get("seq")
        if self.sent is None or seq > self.sent:
            log.debug("input %r: answer seq %s dropped: no query of that seq was sent (the "
                      "last is %s)", self.name, seq, self.sent)
            return
        if seq < self.taken:
            log.debug("input %r: answer seq %s dropped: older than the one taken (%s)",
                      self.name, seq, self.taken)
            return
        log.debug("input %r: answer seq %s taken: %d row(s)%s", self.name, seq,
                  len(msg.get("rows") or ()), ", a count" if msg.get("count") else "")
        self.taken = seq
        if seq == self.inflight:
            self.inflight = None
        rows = [r for r in msg.get("rows") or () if isinstance(r, dict)]
        ids = [r.get("id") for r in rows]
        want = msg.get("selected")
        self._take_rows(rows, ids.index(want) if want in ids else 0, total=msg.get("total"))
        self.count = count_of(msg.get("count"))
        self._show_count()
        self._query()                   # newer text, waiting for this answer
        if self.enter is not None and self.inflight is None and not self.dirty:
            shift, _by = self.enter
            self._end_wait()
            self._submit(shift)
        self.host.changed(self)

    def _take_rows(self, rows: list[dict], selected: int, *, total=None) -> None:
        self.rows = rows
        self.answered = True
        self.total = total if isinstance(total, int) and not isinstance(total, bool) else None
        self.selected = max(0, min(selected, len(rows) - 1)) if rows else 0
        self.top = max(0, min(self.top, max(0, len(rows) - PAGE)))
        self.top = max(0, min(max(self.top, self.selected - PAGE + 1), self.selected))

    def count_text(self) -> tuple[str, str] | None:
        """The box's count: the answer's, else the rows without a tag
        over the total."""
        if self.count is not None:
            return self.count
        if self.total is not None:
            from .kinds.input import COUNT_ROLE
            from .kinds.look import color_of
            listed = sum(1 for r in self.rows if not r.get("tag"))
            return f"{listed}/{self.total}", color_of(COUNT_ROLE)
        return None

    def _show_count(self) -> None:
        if self.box is not None:
            self.box.set_count(self.count_text())

    # ── keys ────────────────────────────────────────────────────────────
    def key(self, ev: KeyEvent) -> bool:
        """A key typed while it is open. Every key is taken (none may
        reach the window underneath); True when it did something."""
        if not self.open:
            return False
        try:
            if self.what == "menu":
                return self._menu_key(ev)
            if self.stepping:
                return self._step_key(ev)
            return self._input_key(ev)
        finally:
            if self.open:
                self.host.changed(self)

    def _input_key(self, ev: KeyEvent) -> bool:
        k, letter = ev.keysym, ctrl_letter(ev)
        if self.enter is not None:
            return True                 # waiting on Enter's answer: nothing else counts
        if k == _ESCAPE:
            self.close("escape")
        elif k in (_RETURN, _KP_ENTER):
            self._enter(ev.shift)
        elif self.chord and chord_matches(ev, self.chord):
            self.box.field.select_all()
        elif k in (_UP, _KP_UP, _ISO_LEFT_TAB) or letter in ("p", "k"):
            self.move(-1)
        elif k in (_DOWN, _KP_DOWN, _TAB) or letter in ("n", "j"):
            self.move(1)
        elif k in (_PAGE_UP, _KP_PAGE_UP):
            self.move(-PAGE, wrap=False)
        elif k in (_PAGE_DOWN, _KP_PAGE_DOWN):
            self.move(PAGE, wrap=False)
        elif self._row_action(ev) or self._toggle_chord(ev):
            pass
        else:
            return self.box.field.key(ev)
        return True

    def _step_key(self, ev: KeyEvent) -> bool:
        k = ev.keysym
        if k == _ESCAPE:
            self.close("escape")
        elif k in (_RETURN, _KP_ENTER):
            self.close("enter")
        elif self.chord and chord_matches(ev, self.chord):
            self.stepping = False       # back to typing, the text kept
            self.box.set_stepping(False)
        elif not (ev.ctrl or ev.alt or ev.super) and k in (ord("n"), ord("N")):
            self._send(B.SUBMIT, input=self.name, text=self.text, row=None,
                       shift=k == ord("N"))
        elif not self._toggle_chord(ev):
            return False
        return True

    def _menu_key(self, ev: KeyEvent) -> bool:
        k, letter = ev.keysym, ctrl_letter(ev)
        lv = self.level
        if k == _ESCAPE or k in (_LEFT, _KP_LEFT) or letter == "h":
            if len(self.levels) > 1:
                self.levels.pop()
            else:
                self.close("escape")
        elif k in (_RETURN, _KP_ENTER):
            self.pick()
        elif k in (_RIGHT, _KP_RIGHT):
            row = self.selected_row()
            if row is not None and row.get("menu") is not None:
                self._submenu(row)
        elif k in (_UP, _KP_UP, _ISO_LEFT_TAB) or letter in ("p", "k"):
            self.move(-1)
        elif k in (_DOWN, _KP_DOWN, _TAB) or letter in ("n", "j"):
            self.move(1)
        elif k in (_PAGE_UP, _KP_PAGE_UP):
            self.move(-PAGE, wrap=False)
        elif k in (_PAGE_DOWN, _KP_PAGE_DOWN):
            self.move(PAGE, wrap=False)
        elif k == _BACKSPACE:
            if lv.filter:
                lv.filter = lv.filter[:-1]
                self._filter_menu()
        elif ev.text and not (ev.ctrl or ev.alt or ev.super) and ev.text.isprintable():
            lv.filter += ev.text
            self._filter_menu()
        else:
            return False
        return True

    def _filter_menu(self) -> None:
        lv = self.level
        lv.rows, start = lv.source.search(lv.filter)
        lv.selected, lv.top = start, max(0, start - PAGE + 1)

    # ── the list ────────────────────────────────────────────────────────
    def _sel(self) -> tuple[list[dict], int, int]:
        if self.what == "menu":
            return self.level.rows, self.level.selected, self.level.top
        return self.rows, self.selected, self.top

    def _set_sel(self, index: int, top: int) -> None:
        if self.what == "menu":
            self.level.selected, self.level.top = index, top
        else:
            self.selected, self.top = index, top

    def selected_row(self) -> dict | None:
        rows, i, _top = self._sel()
        return rows[i] if 0 <= i < len(rows) else None

    def move(self, delta: int, *, wrap: bool = True) -> None:
        rows, i, _top = self._sel()
        if rows:
            n = len(rows)
            self.select((i + delta) % n if wrap else max(0, min(i + delta, n - 1)))

    def select(self, index: int) -> None:
        rows, _i, top = self._sel()
        if 0 <= index < len(rows):
            self._set_sel(index, min(max(top, index - PAGE + 1), index))
            self.host.changed(self)

    def scroll(self, delta: int) -> None:
        self.move(delta, wrap=False)

    def flash(self) -> None:
        """A refused row: the selection flashes the reject colour."""
        self.reject_until = time.monotonic() + REJECT_S
        self.host.changed(self)

    def click_row(self, index: int, level: int | None = None) -> None:
        """A click on a row of the list (of a menu's `level`)."""
        if self.what == "menu" and level is not None and level < len(self.levels) - 1:
            del self.levels[level + 1:]
        self.select(index)
        if self.what == "menu":
            self.pick()
        else:
            self._enter(False)

    # ── doing ───────────────────────────────────────────────────────────
    def _enter(self, shift: bool) -> None:
        if self.spec.get("query") and self.local is None and (self.inflight is not None
                                                              or self.dirty):
            # The latest text unanswered: wait for its answer, a moment.
            self.enter = (shift, time.monotonic() + B.ENTER_WAIT_S)
            self._enter_timer = self.host.later(B.ENTER_WAIT_S, self._enter_late)
            return
        self._submit(shift)

    def _enter_late(self) -> None:
        self._enter_timer = None
        if self.enter is None or not self.open:
            return
        shift, _by = self.enter
        self.enter = None
        log.debug("Enter waited %.2f s for an answer; the text alone", B.ENTER_WAIT_S)
        self._submit(shift, text_only=True)

    def _end_wait(self) -> None:
        self.enter = None
        if self._enter_timer is not None:
            self._enter_timer.cancel()
            self._enter_timer = None

    def _submit(self, shift: bool, *, text_only: bool = False) -> None:
        row = None if shift or text_only else self.selected_row()
        if row is not None and row.get("enabled") is False:
            self.flash()
            return
        if self.local is not None:
            done = self.local.pick(row, self.text, shift=shift)
            if done is False:
                self.flash()
            elif self.open:
                self.close("enter")
            return
        if row is None and not shift and not text_only and self.static is not None \
                and self.static.rows:
            self.flash()                # static rows: Enter picks one, nothing typed is taken
            return
        self._send(B.SUBMIT, input=self.name, text=self.text,
                   row=row.get("id") if row else None, shift=bool(shift))
        if self.spec.get("steps") and not shift:
            self.stepping = True
            self.box.set_stepping(True)
            self.host.changed(self)
        elif not self.spec.get("keep"):
            self.close("enter")

    def pick(self) -> None:
        """Return (or a click) on a menu's selected row."""
        row = self.selected_row()
        if row is None:
            return
        if row.get("enabled") is False:
            self.flash()
            return
        if row.get("menu") is not None:
            self._submenu(row)
            return
        self._send(B.PICK, menu=self.level.name, row=row.get("id"))
        self.close("enter")

    def _submenu(self, row: dict) -> None:
        menus = (getattr(self.owner, "bar", None) or {}).get(B.MENUS) or {}
        spec = menus.get(row.get("menu"))
        if not isinstance(spec, dict):
            self.flash()
            return
        lv = self.level
        self.levels.append(Level(row["menu"], spec, anchor_y=lv.selected - lv.top))
        self._filter_menu()

    def _row_action(self, ev: KeyEvent) -> bool:
        """A row action's chord on the selected row: `act`. True when the
        key is one of the input's action chords at all."""
        actions = self.spec.get("actions") or {}
        name = next((a for a, spec in actions.items()
                     if isinstance(spec, dict) and chord_matches(ev, spec.get("chord", ""))), None)
        if name is None:
            return False
        row = self.selected_row()
        if row is None or name not in (row.get("actions") or ()):
            self.flash()
            return True
        self._send(B.ACT, input=self.name, action=name, row=row.get("id"), text=self.text)
        if actions[name].get("drops"):
            index = self.selected
            self.rows = [r for r in self.rows if r is not row]
            self.selected = max(0, min(index, len(self.rows) - 1))
            self.top = max(0, min(self.top, max(0, len(self.rows) - PAGE)))
            self.dirty = True
            self._query()               # the answer is the truth: it may come back as history
        return True

    def _toggle_chord(self, ev: KeyEvent) -> bool:
        for t in self.spec.get("buttons") or ():
            if isinstance(t, dict) and t.get("chord") and chord_matches(ev, t["chord"]):
                self._toggle(t.get("id"))
                return True
        return False

    def _toggle(self, tid) -> None:
        self._send(B.PRESS, item=tid, input=self.name)

    def refresh_spec(self, spec: dict) -> None:
        """The app sent a new bar while this is open: its toggles and
        placeholder follow; the text and the list stay."""
        self.spec = spec
        if self.box is not None:
            self.box.show(spec)

    # ── to the app, and out ─────────────────────────────────────────────
    def _send(self, type_: str, **fields) -> None:
        if self.owner is not None:
            self.owner.send_event(B.event(type_, **fields))

    def close(self, by: str) -> None:
        """Gone from the screen, `by` enter, escape, click, focus or shell:
        the app told (`closed`), everything given back."""
        if not self.open:
            return
        self.open = False
        self._end_wait()
        key = "input" if self.what == "input" else "menu"
        self._send(B.CLOSED, **{key: self.name}, by=by)
        if self.local is not None and hasattr(self.local, "close"):
            try:
                self.local.close()
            except Exception:
                log.exception("closing a local source failed")
        self.host.closed(self)
