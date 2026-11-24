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

The launcher. On a main window the shell grabs Ctrl+L, Ctrl+T and
Ctrl+M (protocol.py, KEYS): while a shell has the window they never get
here, and the shell's launcher answers them; the tab Ctrl+T is for is
made when the shell sends `load`. When they do get here (no shell, a
key the shell could not grab, a single window), the window asks itself:
Ctrl+L and Ctrl+T with a Qt dialog of its own, Ctrl+M not at all. So it
does when the shell took the key but has no launcher to show
(`prompt`). A new window opened with no address asks the shell for its
launcher once the shell has the window (`launcher`), or asks itself
when no shell answers.

Find in page, the browser's own (no shell, no keyboard grab: this
window has the keyboard already). Two phases, vim's:

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
the state says the agent's hold on each tab (protocol `agent`), and the
shell's `resume` hands a paused tab back. A tab whose page is inside a
dialog's call (a nested event loop, tabs.py) is deleted only once that
call returned, out of the window meanwhile.
"""

import html
import itertools
import logging
import re
import time
from functools import partial

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWebEngineCore import (QWebEngineNewWindowRequest, QWebEnginePage,
                                     QWebEngineScript)
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from indigoshell.browser import places, protocol
from indigoshell.core.naming import APP

from . import keys
from .link import ShellLink
from .tabs import Tab, to_url

log = logging.getLogger(__name__)

NAME = f"{APP}-browser"

# The fallback line's look, the top bar's colours. These mirror the
# theme's values by hand: indigoshell.theme imports skia, which this
# process must not load.
BACKGROUND = "#050310"         # theme.BAR_BG
STRONG = "#ff2a6d"             # theme.FG_STRONG
MUTED = "#5a4a78"              # theme.BASE_MUTED
HOST = "#d1004f"               # theme.FG
WARN = "#ff003c"               # theme.ERROR
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
        tabs = state[protocol.TABS]
        found = state.get(protocol.FIND)
        if found is not None:
            self.tabs.setText(_find_html(found))
        else:
            cap = max(8, min(TITLE_CHARS, 160 // max(1, len(tabs))))
            items = []
            for i, t in enumerate(tabs):
                title = t[protocol.TITLE]
                title = title if len(title) <= cap else title[:cap - 1].rstrip() + "…"
                color = STRONG if i == state[protocol.CURRENT] else MUTED
                items.append(f'<span style="color:{color}">{i + 1} {html.escape(title)}</span>')
            self.tabs.setText("&nbsp;&nbsp;".join(items))
        current = tabs[state[protocol.CURRENT]]
        right = []
        if extra:
            right.append(f'<span style="color:{MUTED}">{html.escape(extra)}</span>')
        if state.get(protocol.BOOKMARKED):
            right.append(f'<span style="color:{HOST}">{BOOKMARK_MARK}</span>')
        if not state[protocol.SECURE]:
            right.append(f'<span style="color:{WARN}">not secure</span>')
        right.append(f'<span style="color:{HOST}">{html.escape(current[protocol.HOST])}</span>')
        if current[protocol.LOADING]:
            right.append(f'<span style="color:{MUTED}">{state[protocol.PROGRESS]}%</span>')
        self.host.setText("&nbsp;".join(right))

    def text(self) -> str:
        """What the line says, as plain text (for tests and logs)."""
        return " | ".join(filter(None, (_plain(self.tabs.text()), _plain(self.host.text()))))


def _find_html(f: dict) -> str:
    """`find  query▏  3/12`, as the top bar draws it."""
    query = html.escape(f["query"]) + (protocol.FIND_CARET if f["typing"] else "")
    count = protocol.find_count(f)
    parts = [f'<span style="color:{MUTED}">{protocol.FIND_LABEL}</span>',
             f'<span style="color:{STRONG}">{query}</span>']
    if count:
        color = WARN if count == protocol.NO_MATCHES else MUTED
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
    def __init__(self, browser, *, single: bool = False, dialog: bool = False) -> None:
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
        self._address_wanted = False    # a new window's address, until the shell says

        # Find in page.
        self.find_query = ""
        self.find_phase: str | None = None      # None, "typing", "stepping"
        self.find_fresh = False                 # the next key typed replaces the query
        self.find_result: tuple[int, int] | None = None     # (active, total) once counted
        self.find_tab: Tab | None = None
        self._find_enter = False                # Enter waits for the count
        self._find_seq = 0                      # the search whose count is wanted

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
        self.link = ShellLink(self.winId, single, self)
        self.link.attached_changed.connect(self.refresh)
        self.link.attached_changed.connect(self._on_attached)
        self.link.down.connect(self._on_shell_down)
        self.link.fallback_changed.connect(self._place_line)
        self.link.command.connect(self.on_command)

        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(0)
        self._refresh.timeout.connect(self._refresh_now)

        self.setWindowTitle(NAME)
        self.resize(1280, 800)

    # ── tabs ────────────────────────────────────────────────────────────
    def current_tab(self) -> Tab | None:
        return self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None

    def add_tab(self, url: QUrl | None = None, *, background: bool = False,
                at: int | None = None) -> Tab:
        tab = Tab(self.profile, self)
        page = tab.page
        tab.changed.connect(self.refresh)
        tab.visited.connect(partial(self._on_visit, tab))
        tab.titled.connect(self._on_title)
        page.newWindowRequested.connect(partial(self.on_new_window, tab))
        page.fullScreenRequested.connect(partial(self.on_fullscreen, tab))
        page.permissionRequested.connect(partial(self.on_permission, tab))
        page.windowCloseRequested.connect(partial(self.close_tab, tab))
        page.watch = self.browser.watch
        if self.browser.unattended:
            page.setAudioMuted(True)

        at = len(self.tabs) if at is None else max(0, min(at, len(self.tabs)))
        self.tabs.insert(at, tab)
        self.stack.addWidget(tab.view)
        if 0 <= self.current and at <= self.current:
            self.current += 1
        if url is not None:
            page.load(url)
        if not background or self.current < 0:
            self.select(at)
        self.refresh()
        return tab

    def select(self, index: int) -> None:
        if not 0 <= index < len(self.tabs):
            return
        tab = self.tabs[index]
        if self.fullscreen_tab is not None and self.fullscreen_tab is not tab:
            self.leave_fullscreen()
        if self.find_phase is not None and self.find_tab is not tab:
            self.end_find(clear=False)
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
        return False

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
        `prompt` asks for: a dialog of the window's own."""
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
                     "launcher; nothing done")

    def ask_address(self) -> None:
        """An address for a new, blank window: the shell's launcher, as
        for Ctrl+L, once the shell has the window; our own dialog when no
        shell answers. The window has just been shown, so the line to the
        shell is usually still being made: what to do waits for it."""
        if self.link.attached:
            self.link.send(protocol.launcher("address"))
        elif self.link.coming():
            self._address_wanted = True
        else:
            self.ask_myself("address")

    def _on_attached(self, attached: bool) -> None:
        if attached and self._address_wanted:
            self._address_wanted = False
            self.link.send(protocol.launcher("address"))

    def _on_shell_down(self) -> None:
        if self._address_wanted:
            self._address_wanted = False
            self.ask_myself("address")

    # ── the address dialog (when no shell asks for it) ──────────────────
    def prompt_address(self, where: str, text: str) -> None:
        if self.browser.unattended:
            log.debug("unattended: no address dialog")
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
            target = self.browser.main_window() if self.single else self
            target.add_tab(to_url(text))
            return
        if tab not in self.tabs:
            tab = self.current_tab()
        if tab is not None:
            if tab is self.find_tab:
                # Find stays open under the shell's launcher (the keys
                # went there, not here); a new page ends it.
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
        return protocol.find(self.find_query, active, total, self.find_phase == "typing")

    # ── what pages ask for ──────────────────────────────────────────────
    def on_new_window(self, opener: Tab, request) -> None:
        D = QWebEngineNewWindowRequest.DestinationType
        destination = request.destination()
        log.debug("%s asks for %s: %s", opener.host() or "a page", destination.name,
                  request.requestedUrl().toString())
        if destination == D.InNewDialog:
            self.browser.open_popup(request)
            return
        target = self.browser.main_window() if self.single else self
        at = target.related_position(opener)
        tab = target.add_tab(background=destination == D.InNewBackgroundTab, at=at)
        request.openIn(tab.page)
        if target is not self:
            target.raise_()

    def on_fullscreen(self, tab: Tab, request) -> None:
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
        if self.browser.unattended:
            log.info("permission denied (unattended run): %s %s", origin, what)
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
    def on_command(self, msg: dict) -> None:
        """A message from the shell (protocol.COMMANDS), by its type."""
        cmd, tab = msg[protocol.TYPE], self.current_tab()
        if cmd == protocol.PROMPT:
            action = msg.get("action")
            if action in protocol.ACTIONS:
                log.info("the shell cannot show its launcher; asking ourselves")
                self.ask_myself(action)
            else:
                log.debug("prompt for %r not understood", action)
            return
        if cmd == protocol.RESUME:
            index = msg.get("index")
            if isinstance(index, int) and 0 <= index < len(self.tabs):
                if not self.browser.watch.resume(self.tabs[index]):
                    log.debug("resume: tab %d is not paused", index)
            else:
                log.debug("resume for no tab: %s", msg)
            return
        if tab is None:
            return
        WA = QWebEnginePage.WebAction
        if cmd in ("back", "forward", "reload"):
            if cmd != "reload":
                tab.note_input()
            tab.page.triggerAction({"back": WA.Back, "forward": WA.Forward,
                                    "reload": WA.Reload}[cmd])
        elif cmd == "open" and isinstance(msg.get("url"), str):
            if self.single:
                self.browser.main_window().add_tab(to_url(msg["url"]))
            else:
                self.add_tab(to_url(msg["url"]))
        elif cmd == "load" and isinstance(msg.get("url"), str) and msg["url"].strip():
            self.load(msg["url"], "tab" if msg.get("where") == "tab" else "here")
        elif cmd == "bookmark" and msg.get("op") in ("add", "remove"):
            url = msg.get("url")
            if url is None or (isinstance(url, str) and url.strip()):
                self.bookmark(msg["op"], url.strip() if url else None)
            else:
                log.debug("bookmark command with a url that is no address: %s", msg)
        elif cmd == "tab" and isinstance(msg.get("index"), int):
            self.select(msg["index"])
        elif cmd == "close":
            index = msg.get("index")
            self.close_tab(index if isinstance(index, int) else tab)
        else:
            log.debug("shell command not understood: %s", msg)

    def state(self) -> dict | None:
        tab = self.current_tab()
        if tab is None:
            return None
        return protocol.state([t.describe() for t in self.tabs], self.current,
                              tab.progress if tab.loading else 100, tab.secure(),
                              url=tab.url().toString(),
                              bookmarked=self.page_bookmark(tab) is not None,
                              find=self._find_state(),
                              agent=[self.browser.watch.state_of(t.id) for t in self.tabs])

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
        self.setWindowTitle(tab.page.title() or NAME)
        self.link.push(state)
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
        self.link.close()
        self.browser.forget(self)
        super().closeEvent(event)
