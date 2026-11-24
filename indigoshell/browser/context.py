"""Jack in the shell: the top bar's content for a focused browser window,
and the launcher for its Ctrl+L, Ctrl+T and Ctrl+M.

A browser window (`browser.qt`) connects to the shell's app service
(applink/service.py) and announces itself as a "jack" window; the
service gives it a `JackWindow`, which takes the state the browser
pushes (protocol.py) into `JackContent`. While that window has focus the
top bar's context area shows it (applink/context.py); the browser
itself draws nothing of it.

What it shows is text, in the bar's vocabulary: the window's tabs from
the left as `1 Title  2 Title  3 Title`, the current one in the strong
colour and the rest muted, titles shortened with an ellipsis so the list
fits; at the right end of the context area the current page's host,
after an open padlock when its connection is not secure and a bookmark
when the page is one; a thin line along the bar's bottom edge, under the
context area, while the current tab loads (the bar has no rule of its
own there, so the line reads as the one moving thing on the bar). While
the browser's find is open its line takes the tabs' place:
`find  query▏  3/12`, the caret only while the query is typed, "no
matches" in the error colour. Nothing on it is clickable.

The agent. While the agent drives a tab (protocol `agent`), that tab's
number is drawn in the voice's colour (protocol.VOICES); while the user
has taken it over and the agent waits, in that colour dimmed a third of
the way to the bar's background. The title keeps its own colour. The window's
actions (Ctrl+M) then begin with "Let Lilac drive again" (the voice's
name) for each tab taken over, which sends the browser `resume`. The
agent's own window (its announce names the voice: protocol `agent`,
"Alone") says whose it is before its tabs: the voice's name in its
colour, "Lilac", plain like the rest.

The launcher. The shell takes Ctrl+L, Ctrl+T and Ctrl+M from every main
browser window (protocol.KEYS, grabbed by the app service: applink/
service.py); a single window (a sign-in popup, the inspector) keeps
them. The press arrives here (`on_key`) with the keyboard: it is held
past the key's release at once (`hold_keyboard`), and the shell's
launcher opens (widgets/launcher.py, `open_launcher`) on Jack's addresses
(services/addresses.py: Ctrl+L with the page's address as the query,
selected; Ctrl+T empty) or its actions (a services/modes.Choices of "Add
bookmark" and "Remove bookmark", the one that does not apply dimmed),
and takes the keyboard with a grab of its own. Keys typed between the
press and that grab still come in on the browser's window (`on_typed`)
and go to the launcher's query in the order typed, ahead of the ones
typed there since. It drops from the top bar: centred on the browser's
window, its top at the bar's bottom edge, kept on that window's monitor
(window.Placement); with no top bar over the window, from the window's
own top edge. Picking a row sends the browser `load` or `bookmark`;
Shift+Delete on a bookmark's row in the address list sends `bookmark`
"remove" with that row's address. A new window opened with no address
asks for the same with a `launcher` message. With no launcher to show
the keyboard goes back and the browser is told to ask itself (`prompt`).

Import rule: of the browser package only `protocol` and `places` (both
pure) are imported here; nothing under `browser/qt` ever is, and
nothing there imports this module.
"""

import logging
from functools import partial

import skia
import xcffib.xproto as xp

from .. import text, theme
from ..applink.service import AppWindow
from ..services.addresses import BOOKMARK, AddressMode
from ..services.modes import Choice, Choices
from ..widgets.base import Insets, KeyEvent, Size, Widget
from ..widgets.layout import Align, Row, Spacer
from ..window import Placement
from . import places, protocol

log = logging.getLogger(__name__)

# ── look ────────────────────────────────────────────────────────────────
# The bar's own vocabulary and nothing else: its text size, its strong
# neon for the tab in front and the muted tone for the rest, its plain
# foreground for the host, its accent colour for the one moving thing.
# No plate, no frame, no separators.
TAB_FG = theme.BASE_MUTED
TAB_CURRENT_FG = theme.FG_STRONG
HOST_FG = theme.FG
INSECURE_FG = theme.ERROR
INSECURE_MARK = "\uf09c"            # an open padlock (Nerd Font: fa-unlock)
BOOKMARK_FG = theme.FG
BOOKMARK_MARK = BOOKMARK            # Nerd Font: fa-bookmark
FIND_FG = theme.BASE_MUTED          # "find", the count
FIND_QUERY_FG = theme.FG_STRONG
NO_MATCHES_FG = theme.ERROR
PROGRESS_FG = theme.BAR_RULE
PROGRESS_THICK = theme.BAR_RULE_THICK
SIZE = theme.FONT_SIZE
PAD = Insets()                      # the bar's own gutter is around it already
HOST_CAP = 0.34                     # the host takes at most this share of the row
PAUSED_DIM = 0.35                   # a taken-over tab's number: the voice's colour this far
                                    # towards the bar's background (halfway is too near the
                                    # muted tabs' colour to tell apart)
RESUME_MARK = "\uf04b"              # Nerd Font: fa-play


def _blend(a: str, b: str, t: float) -> str:
    """#rrggbb `t` of the way from `a` to `b`."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def agent_color(agent: dict | None) -> str | None:
    """The colour of a tab's number for the agent's hold on it: its
    voice's while it drives, dimmed while the user has taken it over."""
    if not agent:
        return None
    color = protocol.voice_color(agent.get("voice"))
    return color if agent.get("state") == "driving" else _blend(color, theme.BAR_BG, PAUSED_DIM)


# ── widgets ─────────────────────────────────────────────────────────────
# Page titles and hosts are text from elsewhere: drawn through the
# shell's per-character fallback, not shaped (see text.py).

def fit_tabs(titles: list[str], current: int, width: float,
             measure=lambda s: text.measure(s, SIZE)) -> list[tuple[int, str, float, float]]:
    """Lay the tabs out in `width` px: (index, label, x, width) for each
    one drawn, x from 0.

    All as `N Title` with two spaces between when they fit. Otherwise
    each keeps its number and the longest titles are shortened first,
    all to the same cap (water-filling), with an ellipsis. When even
    `N …` each is too much, the numbers alone; and when those do not
    fit either, as many as do, around the current one.
    """
    n = len(titles)
    if n == 0 or width <= 0:
        return []
    gap = measure("  ")
    labels = [f"{i + 1} {t}" if t else str(i + 1) for i, t in enumerate(titles)]
    natural = [measure(label) for label in labels]
    room = width - gap * (n - 1)

    def place(texts, widths, first=0):
        out, x = [], 0.0
        for i, (label, w) in enumerate(zip(texts, widths), start=first):
            out.append((i, label, x, w))
            x += w + gap
        return out

    if sum(natural) <= room:
        return place(labels, natural)

    floor = [min(nat, measure(f"{i + 1} …")) for i, nat in enumerate(natural)]
    if sum(floor) <= room:
        lo, hi = 0.0, max(natural)
        for _ in range(30):
            cap = (lo + hi) / 2
            total = sum(min(nat, max(f, cap)) for nat, f in zip(natural, floor))
            lo, hi = (cap, hi) if total <= room else (lo, cap)
        widths = [min(nat, max(f, lo)) for nat, f in zip(natural, floor)]
        texts = [text.ellipsize(label, SIZE, w + 0.5) if w < nat else label
                 for label, w, nat in zip(labels, widths, natural)]
        return place(texts, [measure(t) for t in texts])

    numbers = [str(i + 1) for i in range(n)]
    sizes = [measure(s) for s in numbers]
    gap = measure(" ")
    a = b = max(0, min(current, n - 1))           # the run [a, b] that fits
    used = sizes[a]
    while True:
        grew = False
        for side in (1, -1):
            j = b + 1 if side > 0 else a - 1
            if 0 <= j < n and used + gap + sizes[j] <= width:
                used += gap + sizes[j]
                a, b = (a, j) if side > 0 else (j, b)
                grew = True
        if not grew:
            break
    out, x = [], 0.0
    for i in range(a, b + 1):
        out.append((i, numbers[i], x, sizes[i]))
        x += sizes[i] + gap
    return out


def fit_find(find: dict, width: float,
             measure=lambda s: text.measure(s, SIZE)) -> list[tuple[str, str, float, float]]:
    """The find line in `width` px: (role, text, x, width) for "label",
    "query" (with the caret while typing) and "count" (when there is
    one). A query too long for the room loses its start, so the end,
    where typing happens, stays in view."""
    gap = measure("  ")
    caret = protocol.FIND_CARET if find.get("typing") else ""
    count = protocol.find_count(find)
    label = protocol.FIND_LABEL
    room = width - measure(label) - gap - measure(caret) - (gap + measure(count) if count else 0)
    query = find.get("query", "")
    if measure(query) > room:
        while query and measure("…" + query) > room:
            query = query[1:]
        query = "…" + query if query else ""
    out, x = [], 0.0
    for role, piece in (("label", label), ("query", query + caret), ("count", count)):
        if not piece:
            continue
        w = measure(piece)
        out.append((role, piece, x, w))
        x += w + gap
    return out


class TabList(Widget):
    """The window's tabs, numbered, from the left of the context area;
    the find line in their place while find is open."""

    def __init__(self) -> None:
        super().__init__(flex=1)
        self.titles: list[str] = []
        self.current = 0
        self.agents: list[dict | None] = []
        self.find: dict | None = None
        self.drawn: list[tuple[int, str, float, float]] = []
        self.find_drawn: list[tuple[str, str, float, float]] = []

    def set_tabs(self, titles: list[str], current: int,
                 agents: list[dict | None] | None = None) -> None:
        agents = list(agents) if agents is not None else [None] * len(titles)
        if titles != self.titles or current != self.current:
            self.titles, self.current, self.agents = list(titles), current, agents
            self.invalidate(layout=True)
        elif agents != self.agents:
            self.agents = agents
            self.invalidate()

    def set_find(self, find: dict | None) -> None:
        if find != self.find:
            self.find = dict(find) if find else None
            self.invalidate(layout=True)

    def measure(self, avail: Size) -> Size:
        return Size(0.0, text.line_height(SIZE))

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if self.find is not None:
            self.drawn, self.find_drawn = [], fit_find(self.find, rect.width())
        else:
            self.drawn = fit_tabs(self.titles, self.current, rect.width())
            self.find_drawn = []

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        canvas.save()
        canvas.clipRect(r)
        for i, label, x, _w in self.drawn:
            color = TAB_CURRENT_FG if i == self.current else TAB_FG
            mark = agent_color(self.agents[i]) if i < len(self.agents) else None
            number = str(i + 1)
            if mark is not None and label.startswith(number):
                text.draw(canvas, number, r.left() + x, base, SIZE, theme.color(mark))
                rest = label[len(number):]
                if rest:
                    text.draw(canvas, rest, r.left() + x + text.measure(number, SIZE), base,
                              SIZE, theme.color(color))
                continue
            text.draw(canvas, label, r.left() + x, base, SIZE, theme.color(color))
        for role, piece, x, _w in self.find_drawn:
            color = {"label": FIND_FG, "query": FIND_QUERY_FG}.get(
                role, NO_MATCHES_FG if piece == protocol.NO_MATCHES else FIND_FG)
            text.draw(canvas, piece, r.left() + x, base, SIZE, theme.color(color))
        canvas.restore()

    def layout(self) -> list[dict]:
        """Where each tab (or piece of the find line) was drawn, for tests
        to find in pixels."""
        r = self.rect
        if self.find_drawn:
            return [{"role": role, "text": piece, "x": r.left() + x, "w": w}
                    for role, piece, x, w in self.find_drawn]
        return [{"index": i, "text": label, "x": r.left() + x, "w": w,
                 "current": i == self.current,
                 "number_w": text.measure(str(i + 1), SIZE),
                 "agent": self.agents[i] if i < len(self.agents) else None}
                for i, label, x, w in self.drawn]


class Host(Widget):
    """The current page's host, plain, at the right; before it a bookmark
    when the page is one, and an open padlock when it came over an
    insecure connection."""

    def __init__(self) -> None:
        super().__init__()
        self.host = ""
        self.secure = True
        self.bookmarked = False
        # host as drawn, its x, the padlock's width, the bookmark's x and width
        self.drawn = ("", 0.0, 0.0, 0.0, 0.0)

    def set(self, host: str, secure: bool, bookmarked: bool = False) -> None:
        if (host, secure, bookmarked) != (self.host, self.secure, self.bookmarked):
            self.host, self.secure, self.bookmarked = host, secure, bookmarked
            self.invalidate(layout=True)

    def _marks(self) -> tuple[str, str]:
        return (BOOKMARK_MARK + " " if self.bookmarked else "",
                "" if self.secure else INSECURE_MARK + " ")

    def measure(self, avail: Size) -> Size:
        width = text.measure("".join(self._marks()) + self.host, SIZE)
        return Size(min(width, avail.width * HOST_CAP), text.line_height(SIZE))

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        base = text.baseline_in(r, SIZE)
        x = r.left()
        bookmark, padlock = self._marks()
        book_w = text.measure(bookmark, SIZE) if bookmark else 0.0
        if bookmark:
            text.draw(canvas, bookmark, x, base, SIZE, theme.color(BOOKMARK_FG))
        lock_w = text.measure(padlock, SIZE) if padlock else 0.0
        if padlock:
            text.draw(canvas, padlock, x + book_w, base, SIZE, theme.color(INSECURE_FG))
        marks_w = book_w + lock_w
        value = text.ellipsize(self.host, SIZE, max(0.0, r.width() - marks_w) + 0.5)
        if value and text.measure(value, SIZE) <= r.width() - marks_w + 0.5:
            text.draw(canvas, value, x + marks_w, base, SIZE, theme.color(HOST_FG))
        self.drawn = (value, x + marks_w, lock_w, x, book_w)

    def layout(self) -> dict:
        """Where the host was drawn, for tests to find in pixels."""
        value, x, lock_w, book_x, book_w = self.drawn
        return {"text": value, "x": x, "w": text.measure(value, SIZE) if value else 0.0,
                "mark_w": lock_w, "secure": self.secure, "bookmarked": self.bookmarked,
                "bookmark_x": book_x, "bookmark_w": book_w}


class Owner(Widget):
    """Whose window it is, before its tabs: the agent's name in its
    voice's colour, for the agent's own window."""

    def __init__(self, voice: str) -> None:
        super().__init__()
        self.label = protocol.voice_name(voice)
        self.color = protocol.voice_color(voice)

    def measure(self, avail: Size) -> Size:
        return Size(text.measure(self.label, SIZE), text.line_height(SIZE))

    def paint(self, canvas: skia.Canvas) -> None:
        text.draw(canvas, self.label, self.rect.left(), text.baseline_in(self.rect, SIZE), SIZE,
                  theme.color(self.color))


class JackContent(Widget):
    """Tabs and host in a row, and the progress line along the bottom
    edge: what the top bar shows for one browser window; for the agent's
    own window (`owner`, its voice), its name first."""

    def __init__(self, owner: str | None = None) -> None:
        super().__init__()
        self.tabs = TabList()
        self.host = Host()
        self.owner = Owner(owner) if owner else None
        lead = [self.owner, Spacer(size=theme.SPACING_MD)] if self.owner else []
        self.row = Row([*lead, self.tabs, Spacer(size=theme.SPACING_MD), self.host],
                       padding=PAD, align=Align.STRETCH)
        self.progress: int | None = None    # the current tab's load; None when idle

    def set_progress(self, value: int | None) -> None:
        if value != self.progress:
            self.progress = value
            self.invalidate()

    def children(self):
        return (self.row,)

    def measure(self, avail: Size) -> Size:
        self.row.measure(avail)
        return avail

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self.row.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        self.row.paint(canvas)
        if self.progress is not None:
            r = self.rect
            line = skia.Paint(AntiAlias=False)
            line.setColor(theme.color(PROGRESS_FG))
            done = round(r.width() * self.progress / 100)
            canvas.drawRect(skia.Rect.MakeXYWH(
                r.left(), r.bottom() - PROGRESS_THICK, done, PROGRESS_THICK), line)


# ── one browser window ──────────────────────────────────────────────────

class JackWindow(AppWindow):
    """One browser window: what it shows in the top bar, and its
    launcher."""

    kind = protocol.KIND

    def __init__(self, link, wid: int, announce: dict) -> None:
        super().__init__(link, wid, announce)
        single = announce.get(protocol.SINGLE)
        if not isinstance(single, bool):
            log.debug("browser 0x%x announced no `single`; taken as one (its keys left to it)",
                      wid)
            single = True
        self.single = single
        # Only a main window's keys are the shell's (protocol.py).
        self.keys = {} if single else dict(protocol.KEYS)
        agent = announce.get(protocol.AGENT)
        self.agent = protocol.voice_id(agent) if isinstance(agent, str) and agent else None
        self.content = JackContent(owner=self.agent)
        self.request = None             # the launcher mode last opened for this window
        self.state = protocol.state([], 0, 100, True, url="", bookmarked=False, find=None)

    # ── messages from the browser ───────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        if self.closed:
            return
        kind = msg[protocol.TYPE]
        if kind == protocol.STATE:
            problems = protocol.state_problems(msg)
            if problems:
                log.warning("state from browser 0x%x ignored: %s", self.wid,
                            "; ".join(problems))
                return
            state = {k: msg[k] for k in (protocol.TYPE, *protocol.STATE_KEYS)}
            state[protocol.AGENT] = protocol.agents_of(msg)
            self._show(state)
        elif kind == protocol.DOWNLOAD:
            log.debug("download in browser 0x%x: %s %s %s", self.wid, msg.get("name"),
                      msg.get("state"), msg.get("percent"))
        elif kind == protocol.LAUNCHER:
            action = msg.get("action")
            if action in protocol.ACTIONS:
                self._open(action, pressed=False)
            else:
                log.debug("launcher for %r not understood", action)
        else:
            log.debug("browser 0x%x: message %r not understood", self.wid, kind)

    def _show(self, state: dict) -> None:
        if state == self.state:
            return
        self.state = state
        tabs, current = state[protocol.TABS], state[protocol.CURRENT]
        self.content.tabs.set_tabs([str(t[protocol.TITLE]) for t in tabs], current,
                                   protocol.agents_of(state))
        here = tabs[current] if tabs else {protocol.HOST: "", protocol.LOADING: False}
        self.content.host.set(str(here[protocol.HOST]), bool(state[protocol.SECURE]),
                              bool(state[protocol.BOOKMARKED]))
        self.content.tabs.set_find(state[protocol.FIND])
        progress = state[protocol.PROGRESS]
        self.content.set_progress(progress if here[protocol.LOADING] and progress < 100
                                  else None)

    # ── to the browser ──────────────────────────────────────────────────
    def command(self, name: str, **args) -> None:
        """Ask the browser window to do something: protocol.COMMANDS."""
        self.send(protocol.command(name, **args))

    # ── the launcher, for the browser ───────────────────────────────────
    def on_key(self, name: str, key: KeyEvent) -> None:
        """Ctrl+L, Ctrl+T or Ctrl+M pressed in the window; the same again
        while its launcher is up (typed before the launcher had the
        keyboard) is a key for the launcher like any other."""
        if self._showing():
            self.on_typed(key)
        else:
            self._open(name, pressed=True)

    def on_typed(self, key: KeyEvent) -> None:
        """A key typed after the chord, before the launcher had the
        keyboard: into its query, in order."""
        mode = self.request
        if mode is None or not mode.type_ahead([key]):
            log.debug("a key typed into browser 0x%x with no launcher for it; dropped",
                      self.wid)

    def _showing(self) -> bool:
        mode = self.request
        host = getattr(mode, "host", None)
        return host is not None and host.shows(mode)

    def _open(self, action: str, *, pressed: bool) -> None:
        """The launcher for `action`; `pressed`: opened for its key, the
        keyboard the shell's since the press."""
        if pressed:
            # First: a passive grab ends with the chord's key, and every
            # key typed after that, until the launcher takes the keyboard,
            # would go to the page.
            self.service.hold_keyboard(self)
        # From here on every way out must give the keyboard back unless
        # the launcher took it: an exception left unhandled here would
        # leave the whole desktop's keyboard with the shell.
        mode = None
        try:
            mode = self._mode(action)
            self.request = mode
            shown = self._show_launcher(mode, self.placement())
        except Exception:
            log.exception("the launcher failed to open for browser 0x%x", self.wid)
            shown = False
        if not shown:
            self.request = None
            if pressed:
                self.service.release_keyboard()     # before anything else can fail
            if mode is not None:
                mode.close()
            log.info("no launcher to show %s in for browser 0x%x; it asks itself",
                     action, self.wid)
            self.command(protocol.PROMPT, action=action)
            return
        if pressed and not self._launcher_has_keyboard(mode):
            # Never left holding the keyboard with nothing to type into.
            log.warning("the launcher did not take the keyboard; keys go to browser "
                        "0x%x again", self.wid)
            self.service.release_keyboard()

    def _mode(self, action: str):
        if action == "actions":
            return self.actions()
        where = "tab" if action == "new_tab" else "here"
        url = str(self.state.get(protocol.URL) or "")
        text = url if where == "here" and url != "about:blank" else ""
        return AddressMode(text=text, on_load=partial(self._load, where),
                           on_remove=self._remove_bookmark)

    def _show_launcher(self, mode, placement) -> bool:
        """How a mode is shown: the service's `show_launcher` when it has
        one (tests put a recorder there), else the daemon's launcher."""
        show = getattr(self.service, "show_launcher", None) or show_launcher
        return show(mode, placement)

    @staticmethod
    def _launcher_has_keyboard(mode) -> bool:
        window = getattr(getattr(mode, "host", None), "window", None)
        return bool(getattr(window, "keyboard_grabbed", False))

    def actions(self) -> Choices:
        """The window's actions, for its current page; first, while the
        user has taken a tab over from the agent, handing it back."""
        url = str(self.state.get(protocol.URL) or "")
        marked = bool(self.state.get(protocol.BOOKMARKED))
        page = places.recordable(url)
        host = places.host_of(url)
        tabs = self.state.get(protocol.TABS) or []
        resume = [Choice(f"Let {protocol.voice_name(a.get('voice'))} drive again",
                         partial(self._resume, i), glyph=RESUME_MARK,
                         detail=" · ".join(filter(None, (f"tab {i + 1}",
                                                       str(tabs[i].get(protocol.HOST) or "")
                                                       if i < len(tabs) else ""))))
                  for i, a in enumerate(protocol.agents_of(self.state))
                  if a and a.get("state") == "paused"]
        return Choices([
            *resume,
            Choice("Add bookmark", partial(self._bookmark, "add"),
                   enabled=page and not marked, detail=host, glyph=BOOKMARK_MARK),
            Choice("Remove bookmark", partial(self._bookmark, "remove"),
                   enabled=page and marked, detail=host, glyph=BOOKMARK_MARK),
        ], placeholder="actions")

    def _load(self, where: str, url: str) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; %s not opened", url)
            return
        self.command("load", url=url, where=where)

    def _bookmark(self, op: str) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; no bookmark %s", op)
            return
        self.command("bookmark", op=op)

    def _resume(self, index: int) -> None:
        if self.closed:
            log.info("the browser window that asked is gone; tab %d not handed back", index + 1)
            return
        self.command(protocol.RESUME, index=index)

    def _remove_bookmark(self, url: str) -> bool:
        """Shift+Delete on a bookmark in the address list: the browser
        that asked takes it out of the file. False when it is gone."""
        if self.closed:
            log.info("the browser window that asked is gone; bookmark %s not removed", url)
            return False
        self.command("bookmark", op="remove", url=url)
        return True

    def placement(self) -> Placement | None:
        """Where the launcher drops from: centred on the browser's window,
        its top at the top bar's bottom edge when the bar is over that
        window, else at the window's own top edge. qtile does not
        reparent, so the window's root position is one
        TranslateCoordinates away; None when it is gone (or no X window:
        an offscreen test browser)."""
        d = self.service.display
        if d is None:
            return None
        try:
            at = d.conn.core.TranslateCoordinates(self.wid, d.root, 0, 0).reply()
            width = d.conn.core.GetGeometry(self.wid).reply().width
        except (xp.WindowError, xp.DrawableError):
            return None
        finally:
            d.wake()
        x = at.dst_x + width // 2
        bar = self.service.bar_bottom(x)
        return Placement(x, at.dst_y if bar is None else bar)

    # ── teardown ────────────────────────────────────────────────────────
    def close(self) -> None:
        if self.closed:
            return
        super().close()
        if self.request is not None:
            self.request.cancel()       # its launcher closes, if it still shows it
            self.request = None


def show_launcher(mode, placement) -> bool:
    """The daemon's launcher, on `mode`, at `placement`."""
    from ..core.daemon import get_daemon
    from ..widgets.launcher import open_launcher
    return open_launcher(get_daemon(), mode, placement=placement)
