"""A browser window: its tabs, its keys, and its line to the shell.

    ┌──────────────────────────────────────────────┐
    │ the current tab's view (a stack of views,    │  from the window's top
    │ one per tab)                                 │  edge: the shell's top
    │                                              │  bar shows the tabs,
    └──────────────────────────────────────────────┘  host and load line

While a shell has the window (link.py), the window shows nothing of its
own above the page: the top bar does it while the window has focus.
When none has it (none for a few seconds), a plain line at the window's
top says the same in text: the fallback line, `FallbackLine`.

Main windows hold any number of tabs. A `single` window holds one page
and takes no new tabs: the small floating window a page opens for a
sign-in dialog (`dialog`, Qt's Dialog flag so the window manager floats
it), and the inspector. Links that ask for a new tab or window become
tabs of the window they were clicked in (behind the current one when
the request says background); new tabs a single window's page asks for
go to the most recently used main window.

Where a tab opened from a link goes: right after the tab it was opened
from, and further ones from the same tab after those, in the order they
were opened, until another tab is picked. Ctrl+T and handed-over URLs
go to the end.

One X window per browser window, from the first show to the end. Qt
makes a top-level's native window GPU-composed (RHI) only when a child
that renders into a texture is in it at the moment the native window is
made; one arriving later makes Qt destroy the native window and make
another (the window manager sees a window close and another open, and
the shell has to be told the new one). A web view's own widget (a QQuickWidget)
appears only once its page starts loading, after a window is often
shown, so every window holds a hidden QQuickWidget from its
construction (`_gpu`): the native window is made GPU-composed at once,
and a web view arriving later changes nothing. Only these windows: a new
GPU-composed top-level is slow to make (measured 250-400 ms to its first
paint on an NVIDIA X11 desktop, against 4-30 ms plain), which is why it
is not the process-wide default (QT_WIDGETS_RHI). Context menus,
tooltips and dialogs stay plain; a page's <select> list is a
QQuickWidget of QtWebEngine's own and pays the GPU window's cost.

Keys (keys.py) are taken on the window's QWindow before any widget
inside, the page included, sees them; if Qt replaces that QWindow
anyway (WinIdChange) the filter moves to the new one and the shell is
told the new X window. The release of a key we took (a shortcut, a key
typed into find) never reaches the page either.

Zoom is the user's: Ctrl++ / Ctrl+- / Ctrl+0 and Ctrl+wheel (taken on
the QWindow too, a step a notch, so Chromium's own wheel zoom never
happens unseen) step the current page, kept for its site (flux/zoom.py)
and given to the site's other open pages (app.py `spread_zoom`). Each
step shows the zoom ("110%") in the host's place, in the top bar and in
the fallback line, for bar.py's ZOOM_NOTE_S, the time starting again at
each step; switching tabs ends it. A site's zoom given to a page as it
loads shows nothing.

The shell's top bar. While a shell has the window it shows the
window's section of its top bar (link.py; bar.py makes it of `state()`:
the tabs, the page's host, the inputs and the actions menu), and what
the user does there comes back as the bar's events (`on_event`), naming
the tabs by their ids: `select` (a left or right press), `close` (a
middle click; the tab is remembered for Ctrl+Shift+T as on Ctrl+W),
`move` (a tab dragged along the bar to before another: Alt+Shift+N's
move) and `detach` (one dragged off the bar: app.py `detach_tab`). A tab
the agent holds gets what the same path does with no mouse: switching
away from one it drives ends its session (the Watch, `shown`), closing
ends it with the tab, moving it changes nothing of it, and one torn out
takes its session along (as `borrow` does).

The inputs. On a main window the shell takes Ctrl+L, Ctrl+T, Ctrl+F and
Ctrl+M (the chords of the bar's `address`, `new_tab` and `find` inputs
and its `actions` menu): while a shell has the window they never get
here, and the shell's box and list answer them, asking the window: an
address input's `query` is answered from Flux's places (addresses.py,
one `Addresses` per open input), its `submit` loads the row or the text
(here, or in a new tab for `new_tab`, made when the submit comes) and
`act` takes a bookmark out; a `pick` in the actions adds or removes the
page's bookmark or hands a tab back to the agent. When those keys do get
here (no shell, a key the shell could not grab, a single window), or the
shell cannot show an input (`fallback`), the window asks itself: Ctrl+L
and Ctrl+T with a Qt dialog of its own, Ctrl+F its own find, Ctrl+M not
at all. A new window opened with no address asks the shell to open its
address input (`open`) once the shell has the window, or asks itself
when no shell answers.

Find in page, the shell's (its `find` input, vim's): each `query` is
searched in the current page as it is typed and highlighted
(smartcase), its count answered when the page has counted ("3/12", "no
matches"); the first `submit` (Enter) turns the shell's box to stepping,
and each one after it (n, N) is the next or previous match, its count
answered again; `closed` ends it, the highlight gone and the match left
selected (Enter, a click) or cleared too (Escape). The keys are the
shell's throughout: the page never sees them.

Find in page, the window's own (no shell, or `fallback`: this window has
the keyboard). Two phases, the same:

    typing      Ctrl+F; the query, as typed, searched as it changes and
                counted. The last query comes back, replaced whole by the
                first key typed. Smartcase: an uppercase letter makes the
                search case-sensitive. Enter: on to stepping, or the end
                when nothing matched. Escape: the end, highlight cleared.
    stepping    n / N: the next / previous match, scrolled to. Enter: the
                end, the match left selected. Escape: the end, cleared.
                Ctrl+F: back to typing, the query kept.

In both, the keys that scroll a page (arrows, PageUp/PageDown,
Home/End, and Space while stepping) still reach it, Ctrl/Alt shortcuts
still act, and every other plain key is taken by find, never the page.
A mouse press ends find and leaves the match selected; so does
switching tabs.

Bookmarks. A page counts as a bookmark (the state's `bookmarked`) when
its address is one, or when it was reached from one: its tab came to it
through redirects from a bookmarked address (tabs.py, `came_from`), as
an old bookmark often does. Removing "the current page's" bookmark
(Ctrl+M) takes out the one the tab came from, by its own address, and
only then, when the page's own address is a bookmark too, that one. The
bookmark is never rewritten to where it leads. Keys and clicks that
reach the page, and Back and Forward, tell its tab the user is moving
it (`note_input`): after those, wherever it goes is a new beginning.

The agent. Every event on the window's QWindow goes to the browser's
Watch first (watch.py): while the agent drives the tab the window shows,
the user's input stops there, and a key or a click on the overlay's mark
takes the tab over. The window tells the Watch which tab it shows
(`select`), which closes (`close_tab`) and when it goes (`closeEvent`);
the state says the agent's hold on each tab (`agent`), and the actions'
"Let Lilac drive again" hands a paused tab back. A tab whose page is inside a
dialog's call (a nested event loop, tabs.py) is deleted only once that
call returned, out of the window meanwhile.

The agent's own window (`agent`: protocol.py, "Alone") is a
main window made for the agent's requests. It is never raised or given
focus by Flux (shown without activating), its title says whose it is
(protocol.agent_title) and its WM_WINDOW_ROLE is protocol.AGENT_ROLE.
It is shown only once placed (`show_placed`): its native window made
but not mapped, its line to the shell started early, the shell asked to
put it on an empty workspace (link.py `place`), and shown when the shell
answers, or after applink's PLACE_WAIT_S with no answer. A popup a page
in it opens is placed on the same workspace (`opener`), and the tabs
such a popup asks for go to the agent's window. Tabs move between
windows (`release_tab`, `insert_tab`; app.py `move_tab`): a borrowed
tab's origin is kept in `borrowed`, by tab id, and when the agent's
window closes it gives those tabs back (app.py `give_back`) before
anything else.

Headless (the browser's `headless`, protocol.py "Headless"): the window
is drawn in memory, and nobody sees it. Its line to the shell is off
(link.py: no announce, no fallback line), the agent's window shows at
once with no placing, its pages are muted, and what would ask the user
is answered here at once: a permission request denied, no address
dialog, fullscreen refused (the page keeps its viewport).
"""

import html
import itertools
import logging
import re
import time
from functools import partial

import shiboken6
from PySide6.QtCore import QByteArray, QDataStream, QEvent, QIODevice, QObject, Qt, QTimer, QUrl
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWebEngineCore import (QWebEngineNewWindowRequest, QWebEnginePage,
                                     QWebEngineScript)
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from indigoshell.applink import bar as B
from indigoshell.applink.protocol import PLACE_WAIT_S
from flux import bar as flux_bar, places, protocol
from flux.addresses import Addresses
from indigoshell.core.naming import APP

from . import keys
from .link import ShellLink
from .tabs import Tab, to_url

log = logging.getLogger(__name__)

NAME = f"{APP}-browser"

# Ctrl+Shift+T, as in Firefox: the tabs closed in the user's own windows
# are remembered, newest last, each with its back/forward history and
# where it was, and reopened one at a time. Tabs of the agent's window
# and of popups aren't the user's to reopen.
CLOSED_KEEP = 25


class ClosedTab:
    def __init__(self, window, index: int, url: QUrl, history: QByteArray) -> None:
        self.window = window
        self.index = index
        self.url = url
        self.history = history


def history_bytes(page) -> QByteArray:
    """The page's back/forward history, streamed; empty where this
    PySide cannot stream it (measured on 6.12 only; the tab then comes
    back at its address alone), so closing a tab never fails over it."""
    data = QByteArray()
    try:
        QDataStream(data, QIODevice.OpenModeFlag.WriteOnly) << page.history()
    except (TypeError, NotImplementedError) as e:
        log.debug("a closed tab's history not kept: %s", e)
        return QByteArray()
    return data

# The fallback line's look, the top bar's colours. These mirror the
# theme's values by hand: indigoshell.theme imports skia, which this
# process must not load.
BACKGROUND = "#050310"         # theme.BAR_BG
STRONG = "#ff2a6d"             # theme.FG_STRONG
MUTED = "#5a4a78"              # theme.BASE_MUTED
HOST = "#d1004f"               # theme.FG
WARN = "#ff003c"               # theme.ERROR
INFO = "#05d9e8"               # theme.CYAN_BRIGHT, the bar's `info`
FONT = "'FiraCode Nerd Font Mono', 'DejaVu Sans Mono', monospace"
LINE_HEIGHT = 28               # px, the fallback line
TITLE_CHARS = 32               # a tab's title in the line, at most

_window_ids = itertools.count(1)
BOOKMARK_MARK = ""       # the top bar's glyph (Nerd Font: fa-bookmark)


class FallbackLine(QWidget):
    """The window's own line about itself, at its top: the tabs, the
    host, find. Hidden while a shell shows the same in its top bar; the
    window shows it only when none does (`ShellLink.fallback`)."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("line")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(LINE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"#line {{ background: {BACKGROUND}; }}"
                           f"QLabel {{ font-family: {FONT}; font-size: 15px; }}")
        self.tabs = QLabel()
        self.tabs.setTextFormat(Qt.TextFormat.RichText)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.host = QLabel()
        self.host.setTextFormat(Qt.TextFormat.RichText)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.host)

    def show_state(self, state: dict | None, extra: str = "") -> None:
        if state is None:
            self.tabs.setText("")
            self.host.setText("")
            return
        tabs = state["tabs"]
        found = state.get("find")
        if found is not None:
            self.tabs.setText(_find_html(found))
        else:
            cap = max(8, min(TITLE_CHARS, 160 // max(1, len(tabs))))
            items = []
            for i, t in enumerate(tabs):
                title = t["title"]
                title = title if len(title) <= cap else title[:cap - 1].rstrip() + "…"
                color = STRONG if i == state["current"] else MUTED
                items.append(f'<span style="color:{color}">{i + 1} {html.escape(title)}</span>')
            self.tabs.setText("&nbsp;&nbsp;".join(items))
        current = tabs[state["current"]]
        right = []
        if extra:
            right.append(f'<span style="color:{MUTED}">{html.escape(extra)}</span>')
        if state.get("bookmarked"):
            right.append(f'<span style="color:{HOST}">{BOOKMARK_MARK}</span>')
        if not state["secure"]:
            right.append(f'<span style="color:{WARN}">not secure</span>')
        if state.get("zoom"):
            right.append(f'<span style="color:{INFO}">{html.escape(state["zoom"])}</span>')
        else:
            right.append(f'<span style="color:{HOST}">{html.escape(current["host"])}</span>')
        if current["loading"]:
            right.append(f'<span style="color:{MUTED}">{state["progress"]}%</span>')
        self.host.setText("&nbsp;".join(right))

    def text(self) -> str:
        """What the line says, as plain text (for tests and logs)."""
        return " | ".join(filter(None, (_plain(self.tabs.text()), _plain(self.host.text()))))


def _find_html(f: dict) -> str:
    """`find  query▏  3/12`, as the top bar draws it."""
    query = html.escape(f["query"]) + (flux_bar.FIND_CARET if f["typing"] else "")
    count = flux_bar.find_count(f)
    parts = [f'<span style="color:{MUTED}">{flux_bar.FIND_LABEL}</span>',
             f'<span style="color:{STRONG}">{query}</span>']
    if count:
        color = WARN if count == flux_bar.NO_MATCHES else MUTED
        parts.append(f'<span style="color:{color}">{html.escape(count)}</span>')
    return "&nbsp;&nbsp;".join(parts)


def _plain(rich: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", rich)).replace("\xa0", " ")


def _after_dialog(tab: Tab) -> None:
    """A tab whose page is inside a dialog's call: out of its window
    (whose deletion would take it), deleted once the call returned."""
    tab.view.setParent(None)
    tab.setParent(None)

    def delete() -> None:
        if shiboken6.isValid(tab.view):
            tab.view.deleteLater()
        if shiboken6.isValid(tab):
            tab.deleteLater()
    tab.page.after_dialog.append(delete)


def _key_id(event) -> int:
    """What ties a release to its press: the X keycode, or the key code
    where the platform has none (offscreen)."""
    return event.nativeScanCode() or int(event.key())


class Window(QWidget):
    def __init__(self, browser, *, single: bool = False, dialog: bool = False,
                 agent: bool = False, opener: "Window | None" = None) -> None:
        super().__init__(None, Qt.WindowType.Dialog if dialog else Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # First, before anything can make the native window: GPU-composed
        # from its creation (the module docstring). Never shown.
        self._gpu = QQuickWidget(self)
        self._gpu.setFixedSize(0, 0)
        self._gpu.hide()
        self.browser = browser
        self.profile = browser.profile
        self.single = single
        self.id = next(_window_ids)     # the agent's name for it (control.py)
        self.unfocused_at: float | None = None  # when it last lost focus
        self.tabs: list[Tab] = []
        self.current = -1
        self.fullscreen_tab: Tab | None = None
        self.prompt: QInputDialog | None = None
        self.download = ""              # the line's note about a running download
        self._related: tuple[Tab, int] | None = None
        self._key_window = None         # the QWindow our key filter sits on
        self._held_keys: set[int] = set()       # pressed, taken by us
        self._wheel = 0                 # Ctrl+wheel turned, short of a notch
        self.zoom_note: str | None = None   # the zoom shown in the host's place, for a moment
        self._address_wanted = False    # a new window's address, until the shell says
        self.agent = agent              # the agent's own window (False: the user's)
        self.opener = opener            # a popup's: the window whose page opened it
        self.borrowed: dict[int, dict] = {}     # tab id -> where it came from (app.py)
        self.ready = True               # shown, or free to be (False while being placed)
        self.placed_on: int | None = None       # the workspace the shell put it on
        self._place_timer: QTimer | None = None
        if agent or (opener is not None and opener.agent):
            # The agent's: never activated by its showing (and so never
            # by Flux, which raises or activates only the user's).
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if agent:
            self.setWindowRole(protocol.AGENT_ROLE)

        # Find in page.
        self.find_query = ""
        self.find_phase: str | None = None      # None, "typing", "stepping"
        self.find_fresh = False                 # the next key typed replaces the query
        self.find_result: tuple[int, int] | None = None     # (active, total) once counted
        self.find_tab: Tab | None = None
        self._find_enter = False                # Enter waits for the count
        self._find_seq = 0                      # the search whose count is wanted
        # The shell's find (its `find` input): the query seq whose count is
        # wanted, the tab it searches, whether Enter has turned it to
        # stepping, the last count. And an open address input's places.
        self.shell_find: dict | None = None
        self.addresses: dict[str, Addresses] = {}

        self.line = FallbackLine()
        self.line.hide()
        self.stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.line)
        layout.addWidget(self.stack)

        # The shell shows the tabs and host in its top bar when one has
        # this window; the line says them when none does.
        self.link = ShellLink(self.winId, self.id, self, off=browser.headless)
        self.link.attached_changed.connect(self.refresh)
        self.link.attached_changed.connect(self._on_attached)
        self.link.down.connect(self._on_shell_down)
        self.link.fallback_changed.connect(self._place_line)
        self.link.event.connect(self.on_event)

        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(0)
        self._refresh.timeout.connect(self._refresh_now)
        self._zoom_note = QTimer(self)
        self._zoom_note.setSingleShot(True)
        self._zoom_note.setInterval(int(flux_bar.ZOOM_NOTE_S * 1000))
        self._zoom_note.timeout.connect(self._end_zoom_note)

        self.setWindowTitle(protocol.agent_title() if agent else NAME)
        self.resize(1280, 800)

    # ── being placed (the agent's window) ───────────────────────────────
    def show_placed(self, near: "Window | None" = None) -> None:
        """Shown once the shell has put it on a workspace: an empty one,
        or `near`'s (link.py `place`); with no answer within applink's
        PLACE_WAIT_S, shown anyway, where the window manager puts it."""
        if self._place_timer is not None or self.isVisible():
            return
        if self.browser.headless:
            log.info("%s window %d shown (headless: no workspace to place it on)",
                     "the agent's" if self.agent else "a popup of the agent's", self.id)
            self.show()                 # nobody sees it: no workspace to choose
            return
        self.ready = False
        self.winId()                    # the native window, made and not mapped
        self.link.placed.connect(self._on_placed)
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.setInterval(int(PLACE_WAIT_S * 1000))
        self._place_timer.timeout.connect(partial(self._on_placed, None))
        self._place_timer.start()
        near_id = near.id if near is not None and shiboken6.isValid(near) else None
        self.link.place(near_id)

    def _on_placed(self, desktop) -> None:
        if self.ready:
            return
        self.ready = True
        if self._place_timer is not None:
            self._place_timer.stop()
        self.link.placing_over()
        self.placed_on = desktop
        log.info("%s window %d %s", "the agent's" if self.agent else "a popup of the agent's",
                 self.id, f"placed on workspace {desktop}" if desktop is not None else
                 "shown where the window manager puts it (no workspace from the shell)")
        self.show()

    # ── tabs ────────────────────────────────────────────────────────────
    def current_tab(self) -> Tab | None:
        return self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None

    def add_tab(self, url: QUrl | None = None, *, background: bool = False,
                at: int | None = None) -> Tab:
        tab = Tab(self.profile, self)
        tab.page.watch = self.browser.watch
        tab.zooms = self.browser.zooms      # before its first page
        if self.browser.unattended or self.browser.headless:
            tab.page.setAudioMuted(True)
        at = self._insert(tab, at)
        if url is not None:
            tab.page.load(url)
        if not background or self.current < 0:
            self.select(at)
        self.refresh()
        return tab

    def _insert(self, tab: Tab, at: int | None) -> int:
        """`tab` into the list at `at` (the end for None), its view into
        the stack, its signals to this window; where it went."""
        page = tab.page
        tab.slots = [(tab.changed, self.refresh), (tab.visited, partial(self._on_visit, tab)),
                     (tab.titled, self._on_title),
                     (tab.zoomed, partial(self._on_zoom, tab)),
                     (page.newWindowRequested, partial(self.on_new_window, tab)),
                     (page.fullScreenRequested, partial(self.on_fullscreen, tab)),
                     (page.permissionRequested, partial(self.on_permission, tab)),
                     (page.windowCloseRequested, partial(self.close_tab, tab))]
        for signal, slot in tab.slots:
            signal.connect(slot)
        at = len(self.tabs) if at is None else max(0, min(at, len(self.tabs)))
        self.tabs.insert(at, tab)
        self.stack.addWidget(tab.view)
        if 0 <= self.current and at <= self.current:
            self.current += 1
        return at

    def insert_tab(self, tab: Tab, at: int | None = None, *, background: bool = False) -> int:
        """A tab from another window (app.py `move_tab`, its page in a new
        view already) into this one; where it went."""
        tab.setParent(self)
        at = self._insert(tab, at)
        if not background or self.current < 0:
            self.select(at)
        self.refresh()
        return at

    def release_tab(self, tab: Tab) -> int:
        """`tab` out of this window, for another (app.py `move_tab`): its
        signals cut, out of the list (the window shows the neighbour
        close_tab would) and its view out of the stack; its index then. A
        window left with no tab is the caller's to close."""
        index = self.tabs.index(tab)
        if tab is self.find_tab:
            self.end_find(clear=False)
        if tab is self.fullscreen_tab:
            self._restore_normal()
        for signal, slot in getattr(tab, "slots", ()):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        tab.slots = []
        self.tabs.pop(index)
        self._related = None
        self.stack.removeWidget(tab.view)
        if not self.tabs:
            self.current = -1
        elif index < self.current:
            self.current -= 1
        elif index == self.current:
            self.current = -1
            self.select(min(index, len(self.tabs) - 1))
        self.borrowed.pop(tab.id, None)
        self.refresh()
        return index

    def select(self, index: int) -> None:
        if not 0 <= index < len(self.tabs):
            return
        tab = self.tabs[index]
        if self.fullscreen_tab is not None and self.fullscreen_tab is not tab:
            self.leave_fullscreen()
        if self.find_phase is not None and self.find_tab is not tab:
            self.end_find(clear=False)
        if index != self.current:
            self._end_zoom_note()       # the zoom shown was the tab left's
        self.current = index
        self._related = None
        self.stack.setCurrentWidget(tab.view)
        tab.view.setFocus()
        self.browser.watch.shown(self)
        self.refresh()

    def move_tab(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self.tabs) and 0 <= dst < len(self.tabs)) or src == dst:
            return
        current = self.current_tab()
        self.tabs.insert(dst, self.tabs.pop(src))
        self.current = self.tabs.index(current)
        self._related = None
        self.refresh()

    def close_tab(self, tab: Tab | int | None = None) -> None:
        if isinstance(tab, int):
            tab = self.tabs[tab] if 0 <= tab < len(self.tabs) else None
        elif tab is None:
            tab = self.current_tab()
        if tab is None or tab not in self.tabs:
            return
        index = self.tabs.index(tab)
        if tab is self.find_tab:
            self.end_find(clear=False)
        if tab is self.fullscreen_tab:
            self._restore_normal()
        if tab.inspector is not None and shiboken6.isValid(tab.inspector):
            tab.inspector.close()
        self._remember_closed(tab, index)
        self.tabs.pop(index)
        self._related = None
        if not self.tabs:
            self.current = -1
            self._dispose(tab)
            self.close()
            return
        if index < self.current:
            self.current -= 1
        elif index == self.current:
            # The neighbour that slid into its place, or the one before
            # it when it was the last.
            self.current = -1
            self.select(min(index, len(self.tabs) - 1))
        self._dispose(tab)
        self.refresh()

    def _remember_closed(self, tab: Tab, index: int) -> None:
        url = tab.page.url()
        if self.single or self.agent or url.isEmpty() or url.toString() == "about:blank":
            return
        closed = self.browser.closed
        closed.append(ClosedTab(self, index, url, history_bytes(tab.page)))
        del closed[:-CLOSED_KEEP]

    def reopen_tab(self) -> Tab | None:
        """The tab closed last, back where it was when it was this
        window's (else at the end), with its history, in front."""
        closed = self.browser.closed
        if not closed:
            return None
        entry = closed.pop()
        here = entry.window is self and shiboken6.isValid(self)
        tab = self.add_tab(at=min(entry.index, len(self.tabs)) if here else None)
        if not entry.history.isEmpty():
            QDataStream(entry.history, QIODevice.OpenModeFlag.ReadOnly) >> tab.page.history()
        if tab.page.history().count() == 0:
            tab.page.load(entry.url)        # no history to give back: the address
        log.info("reopened a closed tab at %d", self.tabs.index(tab))
        return tab

    def _dispose(self, tab: Tab) -> None:
        # Later, not now: this often runs inside one of the page's own
        # signals (windowCloseRequested).
        self.browser.watch.forget(tab)
        self.stack.removeWidget(tab.view)
        tab.view.hide()
        if tab.page.in_dialog:
            _after_dialog(tab)
            return
        tab.view.deleteLater()
        tab.deleteLater()

    def related_position(self, opener: Tab) -> int | None:
        """Where a tab opened from `opener` goes (None: the end)."""
        if opener not in self.tabs:
            return None
        if self._related is not None and self._related[0] is opener:
            at = self._related[1] + 1
        else:
            at = self.tabs.index(opener) + 1
        self._related = (opener, at)
        return at

    # ── history and bookmarks ───────────────────────────────────────────
    def _on_visit(self, tab: Tab, url: str, again: bool) -> None:
        self.browser.history.visit(url, tab.page.title(), again=again)

    def _on_zoom(self, tab: Tab, factor: float) -> None:
        """The user zoomed `tab`'s page (keys, Ctrl+wheel): kept for its
        site by the tab, followed by the site's other pages; the zoom
        shown in the host's place for a moment (the time starting again)."""
        self.browser.spread_zoom(tab, factor)
        if tab is self.current_tab():
            self.zoom_note = flux_bar.zoom_text(factor)
            self._zoom_note.start()
            self.refresh()

    def _end_zoom_note(self) -> None:
        self._zoom_note.stop()
        if self.zoom_note is not None:
            self.zoom_note = None
            self.refresh()

    def _on_title(self, url: str, title: str) -> None:
        self.browser.history.retitle(url, title)

    def page_bookmark(self, tab: Tab) -> dict | None:
        """The bookmark `tab`'s page counts as: the one it was reached
        from through redirects, else its own address's; None for none."""
        marks = self.browser.bookmarks
        for url in (tab.came_from, tab.url().toString()):
            entry = marks.find(url) if url else None
            if entry is not None:
                return entry
        return None

    def bookmark(self, op: str, url: str | None = None) -> bool:
        """Add a bookmark or take one out: `url`'s, or with none the
        current page's (for "remove", the bookmark it counts as)."""
        marks, tab = self.browser.bookmarks, self.current_tab()
        if url is None:
            if tab is None:
                return False
            if op == "remove":
                entry = self.page_bookmark(tab)
                return entry is not None and marks.remove(entry["url"])
            url = tab.url().toString()
        if op == "add":
            shown = next((t for t in self.tabs
                          if places.page_key(t.url().toString()) == places.page_key(url)), None)
            return marks.add(url, (shown.page.title() if shown else "") or url)
        if op == "remove":
            return marks.remove(url)
        return False

    # ── keys ────────────────────────────────────────────────────────────
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._watch_keys()
        # Its bar before the line starts: the shell gets it right after the
        # announce, ahead of anything that names it (a new window's `open`).
        self._refresh_now()
        self.link.start()
        self._follow_native()

    def event(self, event) -> bool:
        # Qt can replace a window's native window and its QWindow (it did
        # when a web view first showed in a window made plain; `_gpu`
        # prevents that). Keys come in on the new QWindow, and the shell
        # has to be told the new X window, once Qt is done replacing it.
        if event.type() == QEvent.Type.WinIdChange and hasattr(self, "link"):
            self._watch_keys()
            QTimer.singleShot(0, self, self._follow_native)
        return super().event(event)

    def _follow_native(self) -> None:
        if self.testAttribute(Qt.WidgetAttribute.WA_WState_Created) and self.isVisible():
            self._watch_keys()
            self.link.follow(int(self.winId()))

    def _watch_keys(self) -> None:
        handle = self.windowHandle()
        if handle is not None and handle is not self._key_window:
            handle.installEventFilter(self)
            self._key_window = handle

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        taken = self.browser.watch.window_event(self, event)
        if taken is not None:
            return taken                # the agent drives the tab shown (watch.py)
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            taken = self._key_press(event)
            kid = _key_id(event)
            if taken:
                self._held_keys.add(kid)
            else:
                self._held_keys.discard(kid)
                if event.key() not in keys.MODIFIER_KEYS:
                    self._note_input()
            return taken
        if kind == QEvent.Type.KeyRelease:
            kid = _key_id(event)
            if kid in self._held_keys:
                self._held_keys.discard(kid)
                return True             # its press was ours
            return False
        if kind == QEvent.Type.MouseButtonPress:
            self._note_input()
            if self.find_phase is not None:
                self.end_find(clear=False)  # and the press goes on to the page
        if kind == QEvent.Type.Wheel and keys.wheel_zooms(event.modifiers()):
            return self._wheel_zoom(event)
        return False

    def _wheel_zoom(self, event) -> bool:
        """Ctrl+wheel: the user's zoom, a step per notch (120), as Ctrl++
        and Ctrl+- (Chromium's own would zoom the page unseen by the
        site's zoom). A horizontal scroll is the page's."""
        dy = event.angleDelta().y()
        if dy == 0:
            return False
        if (self._wheel > 0) != (dy > 0):
            self._wheel = 0             # the other way: start counting again
        self._wheel += dy
        tab = self.current_tab()
        while abs(self._wheel) >= keys.WHEEL_NOTCH:
            step = 1 if self._wheel > 0 else -1
            self._wheel -= step * keys.WHEEL_NOTCH
            if tab is not None:
                tab.zoom(step)
        return True

    def swallow_release(self, event) -> None:
        """A key press taken elsewhere (the Watch's take-over): its
        release reaches nothing either."""
        self._held_keys.add(_key_id(event))

    def _note_input(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.note_input()

    def _key_press(self, event) -> bool:
        """Whether the press is ours (True) or the page's."""
        if self.find_phase is not None:
            taken = self._find_key(event)
            if taken is not None:
                return taken
        action = keys.action_for(event.key(), event.modifiers(), event.nativeScanCode())
        if (action is None and event.key() == Qt.Key.Key_Escape
                and self.fullscreen_tab is not None):
            action = "exit_fullscreen"
        if action is not None:
            log.debug("key %s", action)
            self.perform(action)
            return True
        return False

    def perform(self, action) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        page, WA = tab.page, QWebEnginePage.WebAction
        if isinstance(action, tuple):
            verb, n = action
            if self.single or n > len(self.tabs):
                return
            if verb == "tab":
                self.select(n - 1)
            else:
                self.move_tab(self.current, n - 1)
        elif action in ("next_tab", "prev_tab"):
            if len(self.tabs) > 1:
                step = 1 if action == "next_tab" else -1
                self.select((self.current + step) % len(self.tabs))
        elif action in ("address", "new_tab", "actions"):
            self.ask_myself(action)
        elif action == "close_tab":
            self.close_tab(tab)
        elif action == "find":
            self.start_find()
        elif action in ("back", "forward"):
            tab.note_input()            # a same-document step has no navigation of its own
            page.triggerAction(WA.Back if action == "back" else WA.Forward)
        elif action == "reload":
            page.triggerAction(WA.Reload)
        elif action == "reopen_tab":
            self.reopen_tab()
        elif action == "hard_reload":
            # Every resource fetched again, none from the cache.
            page.triggerAction(WA.ReloadAndBypassCache)
        elif action == "inspector":
            self.open_inspector(tab)
        elif action in ("zoom_in", "zoom_out", "zoom_reset"):
            tab.zoom({"zoom_in": 1, "zoom_out": -1, "zoom_reset": 0}[action])
        elif action == "exit_fullscreen":
            if self.fullscreen_tab is not None:
                self.fullscreen_tab.page.triggerAction(WA.ExitFullScreen)

    # ── asking for an address ───────────────────────────────────────────
    def ask_myself(self, action: str) -> None:
        """What Ctrl+L (`address`), Ctrl+T (`new_tab`) and Ctrl+M
        (`actions`) do when they reach this window, and what the shell's
        `fallback` for those asks for: a dialog of the window's own."""
        if self.find_phase is not None:
            self.end_find(clear=False)
        if action == "address":
            tab = self.current_tab()
            url = tab.url().toString() if tab is not None else ""
            self.prompt_address("here", "" if url in ("", "about:blank") else url)
        elif action == "new_tab":
            if not self.single:
                self.prompt_address("tab", "")
        else:
            log.info("the window's actions (Ctrl+M) have no list here but the shell's "
                     "top bar; nothing done")

    def ask_address(self) -> None:
        """An address for a new, blank window: the shell's address input,
        as for Ctrl+L, once the shell has the window; our own dialog when no
        shell answers. The window has just been shown, so the line to the
        shell is usually still being made: what to do waits for it."""
        if self.link.attached:
            self._refresh_now()             # the bar first: the open names its input
            self.link.send(B.open_input(flux_bar.ADDRESS))
        elif self.link.coming():
            self._address_wanted = True
        else:
            self.ask_myself("address")

    def _on_attached(self, attached: bool) -> None:
        if attached and self._address_wanted:
            self._address_wanted = False
            self._refresh_now()             # the bar first: the open names its input
            self.link.send(B.open_input(flux_bar.ADDRESS))

    def _on_shell_down(self) -> None:
        if self._address_wanted:
            self._address_wanted = False
            self.ask_myself("address")

    # ── the address dialog (when no shell asks for it) ──────────────────
    def prompt_address(self, where: str, text: str) -> None:
        if self.browser.unattended or self.browser.headless:
            log.debug("%s: no address dialog",
                      "headless" if self.browser.headless else "unattended")
            return
        if self.prompt is not None and shiboken6.isValid(self.prompt):
            self.prompt.raise_()
            self.prompt.activateWindow()
            return
        dialog = QInputDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("New tab" if where == "tab" else "Open")
        dialog.setLabelText("Address or search:")
        dialog.setTextValue(text)
        dialog.textValueSelected.connect(partial(self._open_typed, where, self.current_tab()))
        self.prompt = dialog
        dialog.open()

    def _open_typed(self, where: str, tab: Tab | None, text: str) -> None:
        if not text.strip():
            return
        self.load(text, where, tab)

    def load(self, text: str, where: str, tab: Tab | None = None) -> None:
        """An address or a search, in `tab` (the current one) or a new tab."""
        if where == "tab":
            self.tab_target().add_tab(to_url(text))
            return
        if tab not in self.tabs:
            tab = self.current_tab()
        if tab is not None:
            if tab is self.find_tab:
                # The window's own find (after a `fallback`) stays open
                # under a dialog of its own; a new page ends it.
                self.end_find(clear=False)
            tab.page.load(to_url(text))
            tab.view.setFocus()

    # ── find in page ────────────────────────────────────────────────────
    def start_find(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        if self.find_phase is None:
            self.find_phase, self.find_tab = "typing", tab
            self.find_fresh = bool(self.find_query)
            self.find_result, self._find_enter = None, False
            if self.find_query:
                self._find(self.find_query)
        elif self.find_phase == "stepping":
            self.find_phase, self.find_fresh = "typing", False
        self.refresh()

    def _find_key(self, event) -> bool | None:
        """A key press in find mode: True taken, False the page's, None
        on to the shortcuts."""
        K = Qt.Key
        key, mods = event.key(), event.modifiers()
        if keys.command_held(mods):
            return None
        if key == K.Key_Escape:
            self.end_find(clear=True)
            return True
        if key in (K.Key_Return, K.Key_Enter):
            if self.find_phase == "typing":
                self._finish_typing()
            else:
                self.end_find(clear=False)
            return True
        if key in keys.SCROLL_KEYS or key in keys.MODIFIER_KEYS:
            return False
        text = event.text()
        if self.find_phase == "typing":
            if key == K.Key_Backspace:
                self._find_type("")
                return True
            if text and text.isprintable():
                self._find_type(text)
                return True
        else:
            if key == K.Key_Space:
                return False
            if text in ("n", "N"):
                self._find(self.find_query, backward=text == "N")
                return True
        if keys.action_for(key, mods, event.nativeScanCode()) is not None:
            return None                 # F5, F12
        return True

    def _find_type(self, text: str) -> None:
        """A character typed into the query ("" is Backspace)."""
        query = "" if self.find_fresh else self.find_query
        self.find_fresh = False
        self.find_query = query + text if text else query[:-1]
        self.find_result, self._find_enter = None, False
        self._find(self.find_query)
        self.refresh()

    def _find(self, query: str, backward: bool = False) -> None:
        tab = self.find_tab
        if tab is None:
            return
        F = QWebEnginePage.FindFlag
        flags = F(0)
        if any(c.isupper() for c in query):
            flags |= F.FindCaseSensitively
        if backward:
            flags |= F.FindBackward
        # Each search answers through its own callback: a count for a
        # query typed over (or a step taken since) arrives late, and must
        # not be taken for the current one.
        self._find_seq += 1
        tab.page.findText(query, flags, partial(self._on_found, tab, self._find_seq))

    def _finish_typing(self) -> None:
        if not self.find_query:
            self.end_find(clear=True)
        elif self.find_result is None:
            self._find_enter = True     # decided when the page has counted
        elif self.find_result[1] == 0:
            self.end_find(clear=True)   # nothing to step through
        else:
            self.find_phase = "stepping"
            self.refresh()

    def _on_found(self, tab: Tab, seq: int, result) -> None:
        if seq != self._find_seq or tab is not self.find_tab or self.find_phase is None:
            return
        self.find_result = (result.activeMatch(), result.numberOfMatches())
        if self._find_enter:
            self._find_enter = False
            self._finish_typing()
        self.refresh()

    def end_find(self, *, clear: bool) -> None:
        """Out of find mode. `clear` also drops the selection the active
        match left; without it the page keeps it, as where you landed."""
        tab = self.find_tab
        self.find_phase, self.find_tab = None, None
        self.find_result, self._find_enter, self.find_fresh = None, False, False
        self._find_seq += 1
        if tab is not None and tab in self.tabs:
            # Chromium makes the active match the page's selection when
            # the search stops. Clearing it has to come after that: the
            # script goes to the frame on the same channel as the stop,
            # where Qt's Unselect action may overtake it.
            tab.page.findText("")
            if clear:
                tab.page.runJavaScript("window.getSelection().removeAllRanges()",
                                       QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self.refresh()

    def _find_state(self) -> dict | None:
        if self.find_phase is None:
            return None
        active, total = self.find_result if self.find_result else (None, None)
        return flux_bar.find(self.find_query, active, total, self.find_phase == "typing")

    # ── what pages ask for ──────────────────────────────────────────────
    def on_new_window(self, opener: Tab, request) -> None:
        D = QWebEngineNewWindowRequest.DestinationType
        destination = request.destination()
        log.debug("%s asks for %s: %s", opener.host() or "a page", destination.name,
                  request.requestedUrl().toString())
        if destination == D.InNewDialog:
            self.browser.open_popup(request, self.tab_target())
            return
        target = self.tab_target()
        at = target.related_position(opener)
        tab = target.add_tab(background=destination == D.InNewBackgroundTab, at=at)
        tab.page.opened_by_page()
        request.openIn(tab.page)
        if target is not self and not target.agent:
            target.raise_()

    def tab_target(self) -> "Window":
        """Where a new tab asked for in this window goes: here; for a
        single window, the agent's window that opened it while it is
        there, else the user's most recently used main window."""
        if not self.single:
            return self
        opener = self.opener
        if (opener is not None and shiboken6.isValid(opener) and opener.agent
                and opener in self.browser.windows):
            return opener
        return self.browser.main_window()

    def on_fullscreen(self, tab: Tab, request) -> None:
        if self.browser.headless:
            request.reject()            # no screen to fill; the page keeps its viewport
            return
        request.accept()
        if request.toggleOn():
            if tab is not self.current_tab() and tab in self.tabs:
                self.select(self.tabs.index(tab))
            self.fullscreen_tab = tab
            self._place_line()          # first: the page takes the whole screen
            self.showFullScreen()
        else:
            self._restore_normal()

    def _restore_normal(self) -> None:
        if self.fullscreen_tab is None:
            return
        self.fullscreen_tab = None
        self.showNormal()
        self._place_line()

    def leave_fullscreen(self) -> None:
        tab = self.fullscreen_tab
        if tab is not None:
            tab.page.triggerAction(QWebEnginePage.WebAction.ExitFullScreen)
            self._restore_normal()

    def on_permission(self, tab: Tab, permission) -> None:
        what = permission.permissionType().name
        origin = permission.origin().toString()
        if self.browser.unattended or self.browser.headless:
            log.info("permission denied (%s): %s %s",
                     "headless: no one to ask" if self.browser.headless else "unattended run",
                     origin, what)
            permission.deny()
            return
        # Temporary: a plain Qt question until the shell asks for it. The
        # answer is kept by the profile (PersistentPermissionsPolicy).
        box = QMessageBox(QMessageBox.Icon.Question, "Permission", f"{origin}\nwants {what}",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def answer(result: int) -> None:
            if not permission.isValid():
                return
            if result == QMessageBox.StandardButton.Yes:
                log.info("permission granted: %s %s", origin, what)
                permission.grant()
            else:
                log.info("permission denied: %s %s", origin, what)
                permission.deny()

        box.finished.connect(answer)
        box.open()

    def open_inspector(self, tab: Tab) -> None:
        if tab.inspector is None or not shiboken6.isValid(tab.inspector):
            window = self.browser.new_window(single=True)
            devtools = window.add_tab()
            tab.page.setDevToolsPage(devtools.page)
            tab.inspector = window
            window.show()
        tab.inspector.raise_()
        tab.inspector.activateWindow()

    # ── the shell ───────────────────────────────────────────────────────
    def on_event(self, msg: dict) -> None:
        """An event of the bar's, from the shell (applink/PROTOCOL.md): the
        mouse on the tabs, the inputs' queries and submits, a pick in the
        actions, an input or menu closed, or one the shell cannot show."""
        kind, name = msg.get("type"), msg.get("input")
        if kind in (B.SELECT, B.CLOSE, B.MOVE, B.DETACH):
            self.on_tab_event(kind, msg)
        elif kind == B.QUERY and isinstance(msg.get("seq"), int):
            text = str(msg.get("text") or "")
            if name == flux_bar.FIND:
                self.shell_find_query(msg["seq"], text)
            elif name in (flux_bar.ADDRESS, flux_bar.NEW_TAB):
                places_ = self.addresses.get(name)
                if places_ is None:
                    places_ = self.addresses[name] = Addresses()
                self.link.send(places_.answer(name, msg["seq"], text))
        elif kind == B.SUBMIT:
            if name == flux_bar.FIND:
                self.shell_find_step(bool(msg.get("shift")))
            elif name in (flux_bar.ADDRESS, flux_bar.NEW_TAB):
                places_ = self.addresses.get(name) or Addresses()
                url = places_.url_of(msg.get("row"), str(msg.get("text") or ""))
                if name not in self.addresses:
                    places_.close()
                if url is not None:
                    log.info("address picked: %s", url)
                    self.load(url, "tab" if name == flux_bar.NEW_TAB else "here")
        elif kind == B.ACT:
            places_ = self.addresses.get(name)
            url = places_.address_of(msg.get("row")) if places_ is not None else None
            if msg.get("action") == flux_bar.REMOVE and url:
                self.bookmark("remove", url)
                places_.remove(url)
            else:
                log.debug("an action on no row of an open address input: %s", msg)
        elif kind == B.PICK and msg.get("menu") == flux_bar.ACTIONS:
            self.on_action(msg.get("row"))
        elif kind == B.CLOSED:
            if name == flux_bar.FIND:
                self.shell_find_end(clear=msg.get("by") == "escape")
            elif name in self.addresses:
                self.addresses.pop(name).close()
        elif kind == B.FALLBACK:
            what = name or msg.get("menu")
            if what in (flux_bar.ADDRESS, flux_bar.NEW_TAB, flux_bar.ACTIONS):
                log.info("the shell cannot show %s; asking ourselves", what)
                self.ask_myself(what)
            elif what == flux_bar.FIND:
                log.info("the shell cannot show find; our own")
                self.start_find()
            else:
                log.debug("a fallback for %r not understood", what)
        else:
            log.debug("shell event not understood: %s", msg)

    def on_action(self, row) -> None:
        """A row of the window's actions picked (bar.py `actions_rows`)."""
        if row in (flux_bar.ADD_ROW, flux_bar.REMOVE_ROW):
            self.bookmark("add" if row == flux_bar.ADD_ROW else "remove")
        elif isinstance(row, str) and row.startswith(flux_bar.RESUME):
            tab = next((t for t in self.tabs if str(t.id) == row[len(flux_bar.RESUME):]), None)
            if tab is None or not self.browser.watch.resume(tab):
                log.debug("resume: %s is no paused tab here", row)
        else:
            log.debug("an action that is none: %r", row)

    def on_tab_event(self, kind: str, msg: dict) -> None:
        """`select`, `close`, `move` or `detach` for one tab, named by its
        id (the mouse on the shell's top bar): the tab the user clicked,
        wherever it is in the list now; nothing when it is gone."""
        tab_id = msg.get(protocol.TAB_ID)
        target = next((t for t in self.tabs if t.id == tab_id), None) \
            if protocol.is_id(tab_id) else None
        if target is None:
            log.debug("%s for a tab this window does not hold: %s", kind, msg)
            return
        if kind == B.SELECT:
            self.select(self.tabs.index(target))
        elif kind == B.CLOSE:
            self.close_tab(target)
        elif self.single:
            log.debug("%s not taken by a single window: %s", kind, msg)
        elif kind == B.MOVE:
            before = msg.get("before")
            others = [t for t in self.tabs if t is not target]
            to = len(others) if before is None else next(
                (i for i, t in enumerate(others) if t.id == before), None)
            if to is None:
                log.debug("move before a tab this window does not hold: %s", msg)
                return
            self.move_tab(self.tabs.index(target), min(to, len(self.tabs) - 1))
        else:
            self.browser.detach_tab(target)

    # ── the shell's find (its `find` input) ─────────────────────────────
    def shell_find_query(self, seq: int, text: str) -> None:
        """Find `text` in the current page, as typed in the shell's box:
        highlighted at once (smartcase), the count answered when the
        page has counted. Every query is answered, even with no page to
        search: the shell sends the next only then."""
        tab = self.current_tab()
        f = self.shell_find
        if f is None or f["tab"] is not tab:
            if f is not None and f["tab"] in self.tabs:
                f["tab"].page.findText("")
            f = self.shell_find = {"tab": tab, "seq": seq, "query": "", "stepping": False,
                                   "count": None}
        f.update(seq=seq, query=text, stepping=False, count=None)
        if tab is None or not text:
            if tab is not None:
                tab.page.findText("")
            self.link.send(B.answer(flux_bar.FIND, seq))
            return
        self._shell_find(tab, seq, text)

    def shell_find_step(self, backward: bool) -> None:
        """Enter in the shell's find: the first turns it to stepping (no
        move); after that, n and N: the next match, the one before."""
        f = self.shell_find
        if f is None or f["tab"] not in self.tabs or not f["query"]:
            return
        if not f["stepping"]:
            f["stepping"] = True
            return
        self._shell_find(f["tab"], f["seq"], f["query"], backward=backward)

    def _shell_find(self, tab: Tab, seq: int, query: str, backward: bool = False) -> None:
        F = QWebEnginePage.FindFlag
        flags = F(0)
        if any(c.isupper() for c in query):
            flags |= F.FindCaseSensitively
        if backward:
            flags |= F.FindBackward
        tab.page.findText(query, flags, partial(self._shell_found, tab, seq))

    def _shell_found(self, tab: Tab, seq: int, result) -> None:
        f = self.shell_find
        if f is None or f["tab"] is not tab or f["seq"] != seq:
            return
        active, total = result.activeMatch(), result.numberOfMatches()
        f["count"] = (active, total)
        count = flux_bar.find_count(flux_bar.find(f["query"], active, total, True))
        self.link.send(B.answer(flux_bar.FIND, seq, count=B.count(
            count, "error" if count == flux_bar.NO_MATCHES else None) if count else None))

    def shell_find_end(self, *, clear: bool) -> None:
        """The shell's find closed: the highlight goes; the match left
        selected (Enter, a click) or cleared too (Escape)."""
        f, self.shell_find = self.shell_find, None
        tab = f["tab"] if f is not None else None
        if tab is not None and tab in self.tabs:
            tab.page.findText("")
            if clear:
                tab.page.runJavaScript("window.getSelection().removeAllRanges()",
                                       QWebEngineScript.ScriptWorldId.ApplicationWorld)

    def state(self) -> dict | None:
        """The window as it is now (bar.py `state`): what its bar and its
        own line say."""
        tab = self.current_tab()
        if tab is None:
            return None
        return flux_bar.state([t.describe() for t in self.tabs], self.current,
                              tab.progress if tab.loading else 100, tab.secure(),
                              url=tab.url().toString(),
                              bookmarked=self.page_bookmark(tab) is not None,
                              find=self._find_state(),
                              agent=[self.browser.watch.state_of(t.id) for t in self.tabs],
                              zoom=self.zoom_note)

    def bar(self, state: dict) -> dict:
        """Its section of the shell's top bar (bar.py)."""
        return flux_bar.bar_of(state, single=self.single, agent=self.agent)

    def refresh(self, *_) -> None:
        """The shell, the line and the title, once per turn of the event
        loop however many things changed in it."""
        if shiboken6.isValid(self._refresh):
            self._refresh.start()

    def _refresh_now(self) -> None:
        state = self.state()
        if state is None:
            return
        tab = self.current_tab()
        title = tab.page.title()
        if self.agent:
            title = protocol.agent_title() + (f" · {title}" if title else "")
        self.setWindowTitle(title or NAME)
        self.link.push(self.bar(state))
        self.line.show_state(state if self.link.fallback else None, self.download)

    def _place_line(self, *_) -> None:
        """The fallback line shown when no shell shows the window's state,
        and never over a fullscreen page. Its coming and going moves the
        page, which is why the link waits a few seconds before it says
        no shell has the window."""
        show = self.link.fallback and self.fullscreen_tab is None
        if show == self.line.isHidden():
            self.line.setVisible(show)
        self.refresh()

    # ── the window ──────────────────────────────────────────────────────
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self.browser.touch(self)
            else:
                self.unfocused_at = time.monotonic()

    def closeEvent(self, event) -> None:
        # Said in the log, so a window that went can be told from one
        # that was never closed (the browser ending by itself).
        log.info("%s window closed: %s", "a single-page" if self.single else "a",
                 "by the window manager" if event.spontaneous()
                 else "its last tab closed" if not self.tabs else f"with {len(self.tabs)} tab(s)")
        if self.borrowed:
            self.browser.give_back(self)    # first: the user's tabs go home, not with it
        for tab in self.tabs:
            if tab.inspector is not None and shiboken6.isValid(tab.inspector):
                tab.inspector.close()
        self.browser.watch.window_closed(self)
        for tab in self.tabs:
            if tab.page.in_dialog:
                _after_dialog(tab)      # not deleted with the window
        if self.prompt is not None and shiboken6.isValid(self.prompt):
            self.prompt.close()
        self._address_wanted = False
        for places_ in self.addresses.values():
            places_.close()
        self.addresses.clear()
        self.link.close()
        self.browser.forget(self)
        super().closeEvent(event)
