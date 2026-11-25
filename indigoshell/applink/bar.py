"""The app bar protocol's vocabulary: what an app says about its section
of the top bar, and what the shell says back (PROTOCOL.md beside this
file is the reference).

Pure Python, imported by the apps (Flux, a Qt process) and by the shell:
names, builders for every message and piece of one, and the checks the
shell runs on what an app sends (`bar_problems`, `answer_problems`). It
imports neither toolkit, nothing of the shell's but its own package.

Builders return plain dicts, every key the protocol names and nothing
else; an app may write its JSON by hand as well, in any language.

    bar(start, end, inputs, menus)          a whole `bar` message
    tabs(id, tabs, current, can)            the `tabs` kind, of tab(...)
    text(id, text | spans, ...)             the `text` kind, spans of span(...)
    button(id, label | icon, ...)           the `button` kind
    input_(chords, icon, query | rows, ...) an INPUT, for `inputs`
    menu(rows, chords)                      a MENU, for `menus`
    row(id, title, ...)                     a ROW, in rows and answers
    answer(input, seq, rows, ...)           the answer to a `query`
    open_input(id, chord) / open_menu(...)  `open`, with the chord the app caught
    active(bool)                            the window's focus, as the app has it
    key(keysym, text, mods) / keys(...)     a KEY, and `keys` (Wayland's type-ahead)
    event(type, **fields)                   a message from the shell

`bar_problems(msg)` lists what keeps a `bar` from being shown (empty: it
is good), as the shell checks it before it takes one: a missing or
repeated id, `current` naming no tab, `opens` naming nothing, a chord
that is none, the bounds. Kinds, keys and icon names it does not know
are no problem: a side ignores those. `keys_problems(msg)` does the
same for `keys`.
"""

APP = "app"
WINDOW = "window"
HANDLE = "handle"
X11 = "x11"

# ── message types ───────────────────────────────────────────────────────
BAR = "bar"
ANSWER = "answer"
OPEN = "open"
ACTIVE = "active"
KEYS = "keys"
CHORD = "chord"             # `open`'s: the chord the app caught (Wayland)
LAST = "last"               # `keys`'s: the window has lost the keyboard
# Shell -> app.
SELECT, CLOSE, MOVE, DETACH = "select", "close", "move", "detach"
PRESS, QUERY, SUBMIT, ACT, PICK = "press", "query", "submit", "act", "pick"
CLOSED, FALLBACK = "closed", "fallback"
EVENTS = (SELECT, CLOSE, MOVE, DETACH, PRESS, QUERY, SUBMIT, ACT, PICK, CLOSED, FALLBACK)
# Why an input or menu closed (`closed`'s `by`).
BY = ("enter", "escape", "click", "focus", "shell")
# `attached`'s `chords`: the shell takes no keys from the window, the app
# catches its bar's chords and sends `open` with the chord (Wayland).
CHORDS = "chords"
CHORDS_APP = "app"

# ── the bar ─────────────────────────────────────────────────────────────
START, END, INPUTS, MENUS = "start", "end", "inputs", "menus"
TABS, TEXT, BUTTON = "tabs", "text", "button"
KINDS = (TABS, TEXT, BUTTON)
CAN = ("close", "move", "detach")
AGENT_STATES = ("driving", "paused")

# Colours are roles; the shell maps them to its theme. `agent` is the
# agent's own colour (core/persona.py).
ROLES = ("fg", "strong", "muted", "info", "accent", "warn", "error", "ok", "agent")

# Icons by name, the shell's table (an unknown one draws nothing).
ICONS = ("bookmark", "unlocked", "search", "replace", "play", "history", "unsaved",
         "branch", "error", "warning", "info", "case", "regex", "file", "folder")

# Bounds.
ID_MAX = 128                # characters of a string id
STRING_MAX = 1000           # characters of any string but an input's text
INPUT_TEXT_MAX = 8000       # characters of an input's `text` (an address may be long)
TABS_MAX = 500              # tabs in one `tabs` item
ITEMS_MAX = 50              # other items, start and end together
ROWS_MAX = 2000             # static rows (an input's `rows`, a menu's)
ANSWER_ROWS_MAX = 200       # rows in one answer
INPUTS_MAX = 50             # inputs, and menus, each
BUTTONS_MAX = 8             # toggles in one input's box
ENTER_WAIT_S = 0.25         # Enter waits this long for the latest text's answer
OPEN_WAIT_S = 0.5           # `open` waits this long for its window's focus and its bar
OPEN_WAIT_WAYLAND_S = 2.0   # ... on Wayland, where a window is focused once its first frame shows
FORWARD_WAIT_S = 0.25       # the shell's own keys wait this long for the app's `last`
KEYS_MAX = 200              # KEYs in one `keys`

MODIFIERS = ("ctrl", "shift", "alt", "super")
# Keys a chord may end in besides one letter or digit (a row action's,
# the find's n): the shell has the keyboard while an input is open.
NAMED_KEYS = ("delete", "backspace", "enter", "tab", "space", "up", "down", "left",
              "right", "home", "end", "pageup", "pagedown", *(f"f{n}" for n in range(1, 13)))


def message(type_: str, **fields) -> dict:
    return {"type": type_, **fields}


def is_id(value) -> bool:
    """An id: a string of 1..ID_MAX characters, or an int (not a bool)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and 0 < len(value) <= ID_MAX


def parse_chord(chord, *, grab: bool = False) -> tuple[frozenset, str] | None:
    """A chord like "ctrl+shift+p" as (modifiers, key), lower-cased; None
    when it is none. With `grab`, only one letter or digit may end it
    (the keys the shell takes from an app's window); otherwise a named
    key may too ("shift+delete")."""
    if not isinstance(chord, str) or not chord or len(chord) > 64:
        return None
    *mods, key = chord.lower().split("+")
    if len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        return None
    if len(key) == 1 and key.isascii() and key.isalnum():
        return frozenset(mods), key
    if not grab and key in NAMED_KEYS:
        return frozenset(mods), key
    return None


def role_ok(role) -> bool:
    return role is None or (isinstance(role, str) and role in ROLES)


# ── builders ────────────────────────────────────────────────────────────
def _some(**fields) -> dict:
    """The fields that say something (None and False left out)."""
    return {k: v for k, v in fields.items() if v is not None and v is not False}


def bar(start=(), end=(), inputs: dict | None = None, menus: dict | None = None) -> dict:
    return message(BAR, **{START: list(start), END: list(end), INPUTS: dict(inputs or {}),
                           MENUS: dict(menus or {})})


def tab(id_, title: str, *, busy: bool = False, progress: int | None = None,
        badge: dict | None = None, agent: dict | None = None) -> dict:
    return {"id": id_, "title": title, **_some(busy=busy, progress=progress, badge=badge,
                                                agent=agent)}


def tabs(id_, tabs_: list[dict], current=None, can=()) -> dict:
    return {"kind": TABS, "id": id_, "tabs": list(tabs_), "current": current, "can": list(can)}


def badge(icon: str, color: str | None = None) -> dict:
    return _some(icon=icon, color=color)


def agent(state: str) -> dict:
    return {"state": state}


def span(text: str | None = None, *, icon: str | None = None, color: str | None = None) -> dict:
    return _some(text=text, icon=icon, color=color)


def text(id_, text_: str | None = None, *, color: str | None = None, spans: list | None = None,
         press: bool = False, opens=None) -> dict:
    return {"kind": TEXT, "id": id_, **_some(text=text_, color=color, spans=spans, press=press,
                                              opens=opens)}


def button(id_, *, label: str | None = None, icon: str | None = None, color: str | None = None,
           on: bool = False, enabled: bool = True, opens=None) -> dict:
    return {"kind": BUTTON, "id": id_, **_some(label=label, icon=icon, color=color, on=on,
                                                opens=opens),
            **({} if enabled else {"enabled": False})}


def input_(*, chords=(), icon: str | None = None, placeholder: str | None = None,
           text: str | None = None, select: bool = False, query: bool = False,
           rows: list | None = None, keep: bool = False, steps: bool = False,
           buttons: list | None = None, actions: dict | None = None) -> dict:
    return _some(chords=list(chords) or None, icon=icon, placeholder=placeholder, text=text,
                 select=select, query=query, rows=rows, keep=keep, steps=steps,
                 buttons=buttons, actions=actions)


def toggle(id_, icon: str, *, on: bool = False, chord: str | None = None) -> dict:
    """One of an input's `buttons`: a toggle in its box."""
    return {"id": id_, "icon": icon, "on": bool(on), **_some(chord=chord)}


def action(chord: str, *, drops: bool = False) -> dict:
    """One of an input's `actions`: what a row may do besides Enter."""
    return {"chord": chord, **_some(drops=drops)}


def menu(rows: list, *, chords=()) -> dict:
    return {"rows": list(rows), **_some(chords=list(chords) or None)}


def row(id_, title: str, *, detail: str | None = None, tag: str | None = None,
        icon: str | None = None, chord: str | None = None, enabled: bool = True,
        on: bool = False, menu: str | None = None, actions: list | None = None,
        match: list | None = None) -> dict:
    return {"id": id_, "title": title,
            **_some(detail=detail or None, tag=tag or None, icon=icon or None, chord=chord,
                    on=on, menu=menu, actions=actions or None, match=list(match) if match
                    else None),
            **({} if enabled else {"enabled": False})}


def answer(input_id, seq: int, rows: list | None = None, *, selected=None,
           total: int | None = None, count: dict | None = None) -> dict:
    return message(ANSWER, input=input_id, seq=seq,
                   **_some(rows=rows, selected=selected, total=total, count=count))


def count(text_: str, color: str | None = None) -> dict:
    return _some(text=text_, color=color)


def open_input(input_id, chord: str | None = None) -> dict:
    """`open`; `chord`: the chord of the bar the app caught itself
    (`attached`'s `chords: "app"`), which is a key pressed."""
    return message(OPEN, input=input_id, **_some(chord=chord))


def open_menu(menu_id, chord: str | None = None) -> dict:
    return message(OPEN, menu=menu_id, **_some(chord=chord))


def active(value: bool) -> dict:
    """The window (or a dialog or popup of it) has the keyboard focus."""
    return message(ACTIVE, active=bool(value))


def key(keysym: int, text_: str = "", mods=()) -> dict:
    """A KEY: one press as typed (keysym, text, the modifiers held)."""
    mods = [m for m in MODIFIERS if m in set(mods)]
    return {"keysym": int(keysym), **_some(text=text_ or None, mods=mods or None)}


def keys(keys_: list, *, last: bool = False) -> dict:
    """`keys`: what the window got after a caught chord, before the shell
    had the keyboard; `last` once it has lost the keyboard."""
    return message(KEYS, keys=list(keys_), **_some(last=last))


def announce(app: str, window, x11: int | None = None) -> dict:
    """The first message on a connection: the app, its own id for the
    window, the window's platform handle."""
    return message("announce", **{APP: app, WINDOW: window,
                                  HANDLE: {X11: int(x11)} if x11 else {}})


event = message


# ── checks ──────────────────────────────────────────────────────────────
def _string(value, limit: int = STRING_MAX) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _bool(value) -> bool:
    return value is None or isinstance(value, bool)


class _Problems(list):
    def add(self, where: str, what: str) -> None:
        if len(self) < 20:
            self.append(f"{where}: {what}")


def _check_spans(p: _Problems, where: str, spans) -> None:
    if not isinstance(spans, list) or len(spans) > 20:
        p.add(where, "spans is not a list of at most 20")
        return
    for i, s in enumerate(spans):
        if not isinstance(s, dict) or not (_string(s.get("text"))
                                           or isinstance(s.get("icon"), str)):
            p.add(f"{where} span {i}", "neither text nor icon")
        elif not role_ok(s.get("color")):
            p.add(f"{where} span {i}", f"colour {s.get('color')!r} is no role")


def _check_tabs(p: _Problems, where: str, item: dict) -> None:
    tabs_ = item.get("tabs")
    if not isinstance(tabs_, list) or len(tabs_) > TABS_MAX:
        p.add(where, f"tabs is not a list of at most {TABS_MAX}")
        return
    ids = []
    for i, t in enumerate(tabs_):
        w = f"{where} tab {i}"
        if not isinstance(t, dict) or not is_id(t.get("id")):
            p.add(w, "no id")
            continue
        ids.append(t["id"])
        if not _string(t.get("title")):
            p.add(w, "title is not a string")
        if not _bool(t.get("busy")):
            p.add(w, "busy is not a bool")
        pr = t.get("progress")
        if pr is not None and (isinstance(pr, bool) or not isinstance(pr, (int, float))
                               or not 0 <= pr <= 100):
            p.add(w, f"progress {pr!r} outside 0..100")
        b = t.get("badge")
        if b is not None and not (isinstance(b, dict) and isinstance(b.get("icon"), str)
                                  and role_ok(b.get("color"))):
            p.add(w, f"badge {b!r}")
        a = t.get("agent")
        if a is not None and not (isinstance(a, dict) and a.get("state") in AGENT_STATES):
            p.add(w, f"agent {a!r}")
    if len(set(map(repr, ids))) != len(ids):
        p.add(where, "tab ids repeat")
    current = item.get("current")
    if current is not None and current not in ids:
        p.add(where, f"current {current!r} names no tab")
    can = item.get("can", [])
    if not isinstance(can, list) or not all(isinstance(c, str) for c in can):
        p.add(where, "can is not a list of strings")


def _check_rows(p: _Problems, where: str, rows, limit: int, menus: dict | None,
                actions: dict | None) -> None:
    if not isinstance(rows, list) or len(rows) > limit:
        p.add(where, f"rows is not a list of at most {limit}")
        return
    ids = []
    for i, r in enumerate(rows):
        w = f"{where} row {i}"
        if not isinstance(r, dict) or not is_id(r.get("id")):
            p.add(w, "no id")
            continue
        ids.append(r["id"])
        if not _string(r.get("title")):
            p.add(w, "title is not a string")
        for key in ("detail", "tag", "icon", "chord"):
            if r.get(key) is not None and not _string(r[key]):
                p.add(w, f"{key} is not a string")
        if r.get("chord") is not None and parse_chord(r["chord"]) is None:
            p.add(w, f"chord {r['chord']!r} is none")
        for key in ("enabled", "on"):
            if not _bool(r.get(key)):
                p.add(w, f"{key} is not a bool")
        sub = r.get("menu")
        if sub is not None and (menus is None or sub not in menus):
            p.add(w, f"menu {sub!r} names no menu")
        acts = r.get("actions")
        if acts is not None and not (isinstance(acts, list) and all(isinstance(a, str)
                                                                     for a in acts)):
            p.add(w, "actions is not a list of names")
        elif acts and actions is not None and any(a not in actions for a in acts):
            p.add(w, f"actions {acts!r} name an action the input has not")
        m = r.get("match")
        if m is not None and not (isinstance(m, list) and len(m) <= STRING_MAX and all(
                isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in m)):
            p.add(w, "match is not a list of character indices")
    if len(set(map(repr, ids))) != len(ids):
        p.add(where, "row ids repeat")


def _check_chords(p: _Problems, where: str, chords, taken: dict) -> None:
    if chords is None:
        return
    if not isinstance(chords, list) or len(chords) > 8:
        p.add(where, "chords is not a list of at most 8")
        return
    for c in chords:
        parsed = parse_chord(c, grab=True)
        if parsed is None:
            p.add(where, f"chord {c!r} is none (modifiers, then one letter or digit)")
        elif parsed in taken:
            p.add(where, f"chord {c!r} also opens {taken[parsed]}")
        else:
            taken[parsed] = where


def bar_problems(msg) -> list[str]:
    """What is wrong with a `bar` message; empty when it can be shown."""
    p = _Problems()
    if not isinstance(msg, dict):
        return ["not an object"]
    inputs, menus = msg.get(INPUTS, {}), msg.get(MENUS, {})
    if not isinstance(inputs, dict) or len(inputs) > INPUTS_MAX:
        p.add("inputs", f"not an object of at most {INPUTS_MAX}")
        inputs = {}
    if not isinstance(menus, dict) or len(menus) > INPUTS_MAX:
        p.add("menus", f"not an object of at most {INPUTS_MAX}")
        menus = {}
    openable = set(inputs) | set(menus)
    seen, others = [], 0
    for run in (START, END):
        items = msg.get(run, [])
        if not isinstance(items, list):
            p.add(run, "not a list")
            continue
        for i, item in enumerate(items):
            where = f"{run} {i}"
            if not isinstance(item, dict) or not is_id(item.get("id")):
                p.add(where, "no id")
                continue
            seen.append(item["id"])
            kind = item.get("kind")
            if kind == TABS:
                _check_tabs(p, where, item)
                continue
            others += 1
            if kind not in (TEXT, BUTTON):
                continue                # a kind the shell does not know: skipped
            for key in ("text", "label", "icon"):
                if item.get(key) is not None and not _string(item[key]):
                    p.add(where, f"{key} is not a string")
            if not role_ok(item.get("color")):
                p.add(where, f"colour {item.get('color')!r} is no role")
            if item.get("spans") is not None:
                _check_spans(p, where, item["spans"])
            for key in ("press", "on", "enabled"):
                if not _bool(item.get(key)):
                    p.add(where, f"{key} is not a bool")
            opens = item.get("opens")
            if opens is not None and opens not in openable:
                p.add(where, f"opens {opens!r}, which names no input or menu")
    if len(set(map(repr, seen))) != len(seen):
        p.add("items", "ids repeat")
    if others > ITEMS_MAX:
        p.add("items", f"more than {ITEMS_MAX} besides tabs")
    taken: dict = {}
    for name, spec in inputs.items():
        where = f"input {name!r}"
        if not is_id(name) or not isinstance(spec, dict):
            p.add(where, "not an object under an id")
            continue
        _check_chords(p, where, spec.get("chords"), taken)
        for key in ("icon", "placeholder"):
            if spec.get(key) is not None and not _string(spec[key]):
                p.add(where, f"{key} is not a string")
        if spec.get("text") is not None and not _string(spec["text"], INPUT_TEXT_MAX):
            p.add(where, f"text is not a string of at most {INPUT_TEXT_MAX}")
        for key in ("select", "query", "keep", "steps"):
            if not _bool(spec.get(key)):
                p.add(where, f"{key} is not a bool")
        actions = spec.get("actions")
        if actions is not None:
            if not isinstance(actions, dict) or not all(
                    isinstance(a, dict) and parse_chord(a.get("chord")) is not None
                    for a in actions.values()):
                p.add(where, "actions is not {name: {chord, drops?}}")
                actions = None
        if spec.get("query") and spec.get("rows") is not None:
            p.add(where, "both query and rows")
        if spec.get("rows") is not None:
            _check_rows(p, where, spec["rows"], ROWS_MAX, None, actions or {})
        buttons = spec.get("buttons")
        if buttons is not None:
            if not isinstance(buttons, list) or len(buttons) > BUTTONS_MAX:
                p.add(where, f"buttons is not a list of at most {BUTTONS_MAX}")
            else:
                bids = [b.get("id") for b in buttons if isinstance(b, dict)]
                if len(bids) != len(buttons) or not all(map(is_id, bids)) \
                        or len(set(map(repr, bids))) != len(bids):
                    p.add(where, "buttons need ids of their own")
                for b in buttons:
                    if isinstance(b, dict) and b.get("chord") is not None \
                            and parse_chord(b["chord"]) is None:
                        p.add(where, f"button chord {b['chord']!r} is none")
    for name, spec in menus.items():
        where = f"menu {name!r}"
        if not is_id(name) or not isinstance(spec, dict):
            p.add(where, "not an object under an id")
            continue
        _check_chords(p, where, spec.get("chords"), taken)
        _check_rows(p, where, spec.get("rows"), ROWS_MAX, menus, None)
    return list(p)


def answer_problems(msg) -> list[str]:
    """What is wrong with an `answer`; empty when it can be shown."""
    p = _Problems()
    if not isinstance(msg, dict) or not is_id(msg.get("input")):
        return ["no input"]
    seq = msg.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        p.add("seq", f"{seq!r} is not a count")
    if msg.get("rows") is not None:
        _check_rows(p, "answer", msg["rows"], ANSWER_ROWS_MAX, None, None)
    total = msg.get("total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        p.add("total", f"{total!r} is not a count")
    c = msg.get("count")
    if c is not None and not (isinstance(c, dict) and _string(c.get("text"), 64)
                              and role_ok(c.get("color"))):
        p.add("count", f"{c!r} is not {{text, color?}}")
    if msg.get("selected") is not None and not is_id(msg["selected"]):
        p.add("selected", "not an id")
    return list(p)


def keys_problems(msg) -> list[str]:
    """What is wrong with a `keys`; empty when every KEY can be typed."""
    p = _Problems()
    if not isinstance(msg, dict):
        return ["not a message"]
    keys_ = msg.get("keys")
    if not isinstance(keys_, list):
        return ["keys: not a list"]
    if len(keys_) > KEYS_MAX:
        p.add("keys", f"{len(keys_)} keys, more than {KEYS_MAX}")
    for i, k in enumerate(keys_[:KEYS_MAX]):
        where = f"keys[{i}]"
        if not isinstance(k, dict):
            p.add(where, "not a key")
            continue
        sym = k.get("keysym")
        if isinstance(sym, bool) or not isinstance(sym, int) or not 0 < sym < 1 << 29:
            p.add(where, f"keysym {sym!r} is none")
        if k.get("text") is not None and not _string(k["text"], 32):
            p.add(where, "text: not a short string")
        mods = k.get("mods")
        if mods is not None and not (isinstance(mods, list)
                                     and all(m in MODIFIERS for m in mods)):
            p.add(where, f"mods {mods!r}: not of {', '.join(MODIFIERS)}")
    if not _bool(msg.get("last")):
        p.add("last", "not a bool")
    return list(p)


def chords(bar_: dict) -> dict[str, tuple[str, object]]:
    """The chords a good bar's inputs and menus take, as written, each
    with what it opens: ("input" | "menu", id)."""
    out = {}
    for what, key in (("input", INPUTS), ("menu", MENUS)):
        for name, spec in (bar_.get(key) or {}).items():
            for c in spec.get("chords") or ():
                out[c.lower()] = (what, name)
    return out
