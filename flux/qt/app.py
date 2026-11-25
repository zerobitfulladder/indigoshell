"""The browser process: start, the windows it keeps, and an orderly end.

`main()` runs once __main__ holds the single-instance lock:

1. startup.apply_flags(): Chromium's flags, Widevine settled. Must come
   before any web engine object exists.
2. The QApplication. Child widgets never become native windows
   (AA_DontCreateNativeWidgetSiblings: a web view turned native would
   be another X window inside ours). Its argv[0] and name make
   the window class `<APP>-browser`. Only the browser's own windows are
   GPU-composed, each from its creation (window.py), so a web view
   appearing in one does not make Qt replace its X window, and menus
   and tooltips stay quick to show.
3. The profile, the per-site identity, the history and bookmarks
   (flux/places.py), the control socket (control.py: later
   invocations' URLs, and the agent's requests), each the named
   profile's when there is one (flux/profiles.py).
4. The windows for the URLs given; none at all with `no_window` (the
   agent's start, flux/control.py `ensure_running`: its first
   request opens one).

`Browser` keeps the windows, most recently used first, and is what the
control socket, the windows and the downloads ask for "the window to
put this in". Its `watch` (watch.py) keeps the agent's hold on the tabs
it acts on.

The agent's own windows (protocol.py, "Alone"), one per voice, are
among them but never "the window to put this in" for the user: that is
`main_window()`, the user's most recently used main window. `move_tab`
moves a tab with its page as it is; `borrow` takes a user's tab into the
agent's window (made, and placed before it shows, when there is none),
remembering where it came from; `hand_back` sends a tab of the agent's
window to the user's (back where it came from when it can); `give_back`
does that for every borrowed tab of an agent's window that is closing.
`detach_tab` moves one into a new window of the user's: a tab torn off
the shell's top bar (protocol `detach`). From the agent's window that
makes it the user's (it leaves `borrowed` there, as any tab leaving
does); a session the agent drives on it follows it, as on a `borrow`.

Downloads are taken, no question asked, into the profile's download
folder, and recorded for the agent (`download_list`, newest first, the
last protocol.DOWNLOADS_KEPT of this run) with the tab that started
each.

Headless (`headless`, protocol.py "Headless"; startup.py settled the
platform): every window and page as on screen, drawn in memory, and
what needs a screen or a person degrades (window.py, watch.py,
control.py say how). The page effects are off, as with --software-gl.

The end: Ctrl+C or SIGTERM quits the event loop, and so does the last
window closing (not headless: a service waits for its next window); the
log says which (logs.py) before every window (and so every page) is
deleted, deferred deletions flushed, and only then the profile. The
other order makes Qt warn that the profile was released under a live
page.
"""

import logging
import os
import signal
import socket
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QSocketNotifier, Qt, QTimer,
                            QtMsgType, QUrl, qInstallMessageHandler, qVersion)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
from PySide6.QtWidgets import QApplication

from flux import places, protocol, zoom

from . import effects, logs, startup
from .bookmarks import Bookmarks
from .control import ControlServer
from .identity import Identity
from .profile import make_profile
from .tabs import to_url
from .watch import Watch
from .window import NAME, Window

log = logging.getLogger(__name__)
qt_log = logging.getLogger("flux.qt.messages")


# ── the application ─────────────────────────────────────────────────────
_LEVELS = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
           QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
           QtMsgType.QtFatalMsg: logging.CRITICAL}


def _qt_message(mode, context, message) -> None:
    level = logging.DEBUG if context.category == "js" else _LEVELS.get(mode, logging.INFO)
    qt_log.log(level, "%s", message)


def make_application() -> QApplication:
    app = QApplication.instance()
    if app is not None:
        return app
    # Not QT_WIDGETS_RHI: it made every top-level GPU-composed, and each
    # new menu, tooltip or dialog then took 250-400 ms to show. Only the
    # browser's own windows are, from their creation (window.py).
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    QCoreApplication.setApplicationName(NAME)
    QGuiApplication.setDesktopFileName(NAME)
    qInstallMessageHandler(_qt_message)
    logs.install_hooks()            # a slot's exception: logged, and the browser goes on
    app = QApplication([NAME])
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    return app


class SignalWake:
    """Ctrl+C and SIGTERM quit the event loop. Python handles a signal
    only when it runs, and inside app.exec() it does not; the wakeup fd
    makes the loop run a line of Python as soon as one arrives."""

    def __init__(self, app: QApplication) -> None:
        self.received: str | None = None   # the signal that ended the loop
        self.r, self.w = socket.socketpair()
        self.r.setblocking(False)
        self.w.setblocking(False)
        signal.set_wakeup_fd(self.w.fileno())
        self.notifier = QSocketNotifier(self.r.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self._drain)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda num, _frame: self._quit(app, num))

    def _drain(self) -> None:
        try:
            self.r.recv(64)
        except OSError:
            pass

    def _quit(self, app: QApplication, num: int) -> None:
        self.received = signal.Signals(num).name
        log.info("%s: quitting", self.received)
        app.quit()


# ── the process-wide state ──────────────────────────────────────────────
class Browser(QObject):
    def __init__(self, profile, *, unattended: bool = False, headless: bool = False,
                 name: str | None = None, sites: Path | None = None) -> None:
        super().__init__()
        self.profile = profile                  # the QWebEngineProfile
        self.profile_name = name                # flux/profiles.py; None: the default one
        self.unattended = unattended
        self.headless = headless                # no screen (protocol.py, "Headless")
        self.ended_by: str | None = None        # why it ends, when asked to (quit)
        self.identity = Identity(profile, sites)
        self.history = places.History(places.history_path(name))
        self.bookmarks = Bookmarks(self, places.bookmarks_path(name))
        self.bookmarks.changed.connect(self._bookmarks_changed)
        self.zooms = zoom.ZoomFile(zoom.zoom_path(name))   # the zoom kept per site
        self.watch = Watch(self)                # the agent driving tabs (watch.py)
        self.windows: list[Window] = []         # open ones, most recently used first
        # Tabs the user closed, newest last, for Ctrl+Shift+T (window.py).
        self.closed: list = []
        self._made: list[Window] = []           # every one made and not closed, for the end
        self.server: ControlServer | None = None
        self.downloads: dict[int, int | None] = {}  # id -> last percent reported
        self.download_list: list[dict] = []         # the agent's view, newest first
        self._requests: list[QWebEngineDownloadRequest] = []
        profile.downloadRequested.connect(self.on_download)
        # Which window has the focus, for the shell (window.py `report_active`).
        app = QGuiApplication.instance()
        if app is not None:
            app.focusWindowChanged.connect(self._focus_moved)

    def _focus_moved(self, _window) -> None:
        for window in list(self.windows):
            if shiboken6.isValid(window):
                window.report_active()

    def listen(self, path: str | None = None) -> None:
        """The control socket: hand-offs and the agent's requests (the
        profile's socket unless `path`)."""
        self.server = ControlServer(self, path or protocol.control_socket_path(self.profile_name))

    # ── windows ─────────────────────────────────────────────────────────
    def new_window(self, *, single: bool = False, dialog: bool = False, agent: str | None = None,
                   opener: Window | None = None) -> Window:
        window = Window(self, single=single, dialog=dialog or (self.unattended and not single),
                        agent=agent, opener=opener)
        if self.unattended:
            self._out_of_the_way(window, offset=40 * len(self.windows))
        self.windows.insert(0, window)
        self._made.append(window)
        return window

    def touch(self, window: Window) -> None:
        if window in self.windows and self.windows[0] is not window:
            self.windows.remove(window)
            self.windows.insert(0, window)

    def forget(self, window: Window) -> None:
        """A window closed: it deletes itself (WA_DeleteOnClose); one
        still pending at the end goes with the deferred deletions."""
        if window in self.windows:
            self.windows.remove(window)
        if window in self._made:
            self._made.remove(window)

    def spread_zoom(self, tab, factor: float) -> None:
        """The user zoomed `tab`'s page: every other page of its site open
        in any window follows at once (flux/zoom.py)."""
        site = zoom.site_of(tab.url().toString())
        if site is None:
            return
        for window in self.windows:
            if not shiboken6.isValid(window):
                continue
            for other in window.tabs:
                if other is not tab and zoom.site_of(other.url().toString()) == site:
                    other.set_zoom(factor)

    def user_windows(self) -> list[Window]:
        """The user's main windows, most recently used first: not a
        popup, not the agent's own."""
        return [w for w in self.windows if shiboken6.isValid(w) and not w.single and not w.agent]

    def main_window(self) -> Window:
        """The user's most recently used window that takes tabs; a new one
        if none."""
        for window in self.user_windows():
            return window
        window = self.new_window()
        window.show()
        return window

    def agent_window(self, voice) -> Window:
        """The agent's own window for `voice`; a new one when there is
        none, not yet shown: the caller gives it a tab, then
        `show_placed()` (which does nothing once it is placed or shown)."""
        vid = protocol.voice_id(voice)
        for window in self.windows:
            if shiboken6.isValid(window) and window.agent == vid:
                return window
        log.info("the agent's own window for %s: made", protocol.voice_name(vid))
        return self.new_window(agent=vid)

    # ── tabs moving between windows (protocol.py, "Alone") ──────────────
    def move_tab(self, tab, dest: Window, *, at: int | None = None, background: bool = False,
                 close_empty: bool = True) -> tuple[Window, int]:
        """`tab`, its page as it is, into `dest` (a new view there for the
        same page): (the window it left, its index there). The window left
        with no tab closes, unless `close_empty` is false (it is closing
        already)."""
        src = self.window_of(tab.page)
        index = src.release_tab(tab)
        old = tab.new_view()
        old.hide()
        old.deleteLater()               # still in the old stack; the page is the new view's
        dest.insert_tab(tab, at, background=background)
        self.watch.shown(dest)          # a session follows its tab to where it shows
        self.watch.guard(tab)
        if not src.tabs and close_empty:
            src.close()
        log.info("tab %d moved from window %d to window %d", tab.id, src.id, dest.id)
        return src, index

    def detach_tab(self, tab, *, at: tuple[int, int] | None = None) -> Window | None:
        """A tab torn off the shell's top bar (protocol `detach`): into a
        new window of the user's, its page as it is (`move_tab`), the
        size of the one it left, shown and focused like any window the
        user opens; `at`, the root point where they let go, is where its
        top left goes if the window manager lets it (qtile tiles it
        wherever its layout says). None, and nothing done, for a
        window's last tab and a single window's."""
        src = self.window_of(tab.page)
        if src is None or src.single or len(src.tabs) < 2:
            log.info("tab %d not torn out: %s", tab.id,
                     "no window holds it" if src is None else
                     "a single window's" if src.single else "its window's last tab")
            return None
        window = self.new_window()
        if not self.unattended:
            window.resize(src.size())
            if at is not None:
                window.move(*at)
        self.move_tab(tab, window)
        window.show()
        if not (self.unattended or self.headless):
            window.raise_()
            window.activateWindow()
        log.info("tab %d torn out of window %d into window %d", tab.id, src.id, window.id)
        return window

    def borrow(self, tab, voice) -> dict:
        """A user's tab into the agent's window, as its current tab; where
        it came from (kept in that window's `borrowed`)."""
        src = self.window_of(tab.page)
        dest = self.agent_window(voice)
        origin = {"window": src, "window_id": src.id, "index": src.tabs.index(tab),
                  "placeholder": None}
        if len(src.tabs) == 1:
            # A window holds a tab at least: a blank one keeps its place.
            origin["placeholder"] = src.add_tab(QUrl("about:blank"), background=True)
        self.move_tab(tab, dest)
        dest.borrowed[tab.id] = origin
        dest.show_placed()
        return origin

    def hand_back(self, tab, *, show: bool = False, close_empty: bool = True) -> tuple:
        """A tab of the agent's window to the user's: back to its window at
        its old place, or to the end of the user's most recently used
        window; in the background unless `show`. (window, "its window" |
        "another window"); LookupError when the user has no window."""
        src = self.window_of(tab.page)
        origin = src.borrowed.get(tab.id) if src is not None else None
        home = origin["window"] if origin else None
        if home is not None and not (shiboken6.isValid(home) and home in self.user_windows()):
            home = None
        to = "its window" if home is not None else "another window"
        if home is None:
            users = self.user_windows()
            if not users:
                raise LookupError("the user has no Flux window open")
            home = users[0]
        at = origin["index"] if origin and home is origin["window"] else None
        placeholder = origin.get("placeholder") if origin else None
        shown = home.current_tab()
        if shown is not None and shown is not placeholder and self.watch.paused(shown.id):
            show = False                # the user took over the tab it shows: theirs to keep
        self.watch.drop(tab.id, "handed back to the user")
        self.move_tab(tab, home, at=at, background=not show, close_empty=close_empty)
        if (placeholder is not None and shiboken6.isValid(placeholder)
                and placeholder in home.tabs and len(home.tabs) > 1
                and placeholder.url().toString() in ("", "about:blank")):
            home.close_tab(placeholder)     # the blank tab that kept its place
        return home, to

    def give_back(self, window: Window) -> None:
        """The agent's window is closing: its borrowed tabs go back to the
        user, to a new window of theirs when they have none."""
        for tab in [t for t in window.tabs if t.id in window.borrowed]:
            try:
                home, _ = self.hand_back(tab, close_empty=False)
            except LookupError:
                home = self.new_window()
                self.move_tab(tab, home, close_empty=False)
                home.show()
            log.info("tab %d given back to window %d: the agent's window closed", tab.id,
                     home.id)

    def _bookmarks_changed(self) -> None:
        for window in self.windows:
            window.refresh()

    def window_of(self, page) -> Window | None:
        for window in self.windows:
            if any(tab.page is page for tab in window.tabs):
                return window
        return None

    def open_urls(self, urls: list[str]) -> Window:
        """URLs from the command line or a later invocation: tabs at the
        end of the most recently used window, the first one shown. None
        at all: a new window, blank, asking for an address (the shell's
        launcher once the shell is there, or our own dialog)."""
        if not urls:
            window = self.new_window()
            window.add_tab(QUrl("about:blank"))
            window.show()
            window.ask_address()
            return window
        window = self.main_window()
        for i, url in enumerate(urls):
            window.add_tab(to_url(url), background=i > 0)
        window.show()
        if not (self.unattended or self.headless):  # refuses focus on purpose; no screen
            window.raise_()
            window.activateWindow()
        return window

    def open_popup(self, request, opener: Window | None = None) -> Window:
        """A page's request for a small window of its own (a sign-in
        dialog): one page, floating, the size it asked for. A popup of the
        agent's window goes to that window's workspace."""
        window = self.new_window(single=True, dialog=True, opener=opener)
        geometry = request.requestedGeometry()
        if geometry.width() > 0 and geometry.height() > 0:
            window.resize(geometry.width(), geometry.height())
            if geometry.x() or geometry.y():
                window.move(geometry.topLeft())
        tab = window.add_tab()
        tab.page.opened_by_page()
        request.openIn(tab.page)
        if opener is not None and opener.agent:
            window.show_placed(near=opener)
        else:
            window.show()
        return window

    def _out_of_the_way(self, window: Window, offset: int = 0) -> None:
        """An unattended run's window: floating in the bottom-right corner,
        never taking the keyboard."""
        window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        window.resize(1000, 660)
        window.move(area.right() - window.width() - 12 - offset,
                    area.bottom() - window.height() - 12 - offset)

    # ── downloads ───────────────────────────────────────────────────────
    def on_download(self, request: QWebEngineDownloadRequest) -> None:
        request.accept()
        path = Path(request.downloadDirectory()) / request.downloadFileName()
        log.info("download: %s (from %s)", path, request.url().toString())
        self._requests.append(request)
        self.downloads[request.id()] = None
        page = request.page()
        self.download_list.insert(0, {
            "download": int(request.id()), "name": request.downloadFileName(), "path": str(path),
            "state": "running", "percent": None, "received": 0, "total": None,
            "host": request.url().host(), "tab": getattr(page, "id", None),
            "started": time.monotonic()})
        del self.download_list[protocol.DOWNLOADS_KEPT:]
        request.receivedBytesChanged.connect(lambda: self._download_progress(request))
        request.isFinishedChanged.connect(lambda: self._download_finished(request))
        self._download_note(request, "started", 0)

    def _percent(self, request) -> int | None:
        total = request.totalBytes()
        return int(request.receivedBytes() * 100 / total) if total > 0 else None

    def _download_progress(self, request) -> None:
        percent = self._percent(request)
        last = self.downloads.get(request.id())
        # A short state: every tenth, not every chunk.
        if percent is not None and (last is None or percent // 10 > last // 10) and percent < 100:
            self._download_note(request, "progress", percent)

    def download(self, download_id: int) -> dict | None:
        """The agent's record of a download (protocol DOWNLOAD), as it is
        now; None when this run has none by that id."""
        record = next((d for d in self.download_list if d["download"] == download_id), None)
        if record is None:
            return None
        request = next((r for r in self._requests if r.id() == download_id), None)
        if request is not None and shiboken6.isValid(request):
            total = request.totalBytes()
            record.update(received=int(request.receivedBytes()),
                          total=int(total) if total > 0 else None,
                          percent=self._percent(request))
        return {k: v for k, v in record.items() if k != "started"}

    def _download_finished(self, request) -> None:
        S = QWebEngineDownloadRequest.DownloadState
        state = request.state()
        path = Path(request.downloadDirectory()) / request.downloadFileName()
        record = next((d for d in self.download_list if d["download"] == request.id()), None)
        if record is not None:
            total = request.totalBytes()
            record.update(received=int(request.receivedBytes()),
                          total=int(total) if total > 0 else None, path=str(path),
                          name=request.downloadFileName(), percent=self._percent(request))
        if state == S.DownloadCompleted:
            log.info("download done: %s (%d bytes)", path, request.receivedBytes())
            if record is not None:
                record.update(state="done", percent=100)
            self._download_note(request, "done", 100)
        elif state == S.DownloadCancelled:
            log.info("download cancelled: %s", path)
            if record is not None:
                record["state"] = "cancelled"
            self._download_note(request, "cancelled", self._percent(request))
        else:
            log.warning("download failed: %s (%s)", path, request.interruptReasonString())
            if record is not None:
                record.update(state="failed", error=request.interruptReasonString())
            self._download_note(request, "failed", self._percent(request))
        if request in self._requests:
            self._requests.remove(request)
        self.downloads.pop(request.id(), None)

    def _download_note(self, request, state: str, percent: int | None) -> None:
        self.downloads[request.id()] = percent
        window = self.window_of(request.page()) if request.page() else None
        window = window or next((w for w in self.windows if not w.single), None)
        if window is None:
            return
        name = request.downloadFileName()
        running = state in ("started", "progress")
        window.download = (f"↓ {name}" + (f" {percent}%" if percent else "")) if running else ""
        window.refresh()

    # ── the end ─────────────────────────────────────────────────────────
    def quit(self, reason: str) -> None:
        """End at the event loop's next turn (protocol.py, "Quit"): what
        was answered in this one is written by then."""
        self.ended_by = reason
        log.info("%s: quitting", reason)
        QTimer.singleShot(0, QApplication.instance().quit)

    def shutdown(self) -> None:
        """Every page gone, before the profile goes."""
        if self.server is not None:
            self.server.close()
        for window in self._made:
            if shiboken6.isValid(window):
                window.link.close()
        for window in self._made:
            if shiboken6.isValid(window):
                shiboken6.delete(window)
        self._made.clear()
        self.windows.clear()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.history.close()


def main(urls: list[str], *, unattended: bool = False, lock: int | None = None,
         no_window: bool = False, headless: bool = False, software: bool = False,
         profile: str | None = None) -> int:
    log.info("starting: pid %d, Qt %s%s%s%s%s", os.getpid(), qVersion(),
             f", profile {profile}" if profile else "",
             ", headless" if headless else ", drawn in software" if software else "",
             ", unattended" if unattended else "",
             ", no window" if no_window and not urls else "")
    widevine = startup.apply_flags(software=headless or software)
    app = make_application()
    if headless:
        app.setQuitOnLastWindowClosed(False)    # a service: SIGTERM ends it
    if headless or software:
        effects.turn_off("drawn in software", quiet=True)
    web_profile = make_profile(profile)
    log.info("profile in %s%s", web_profile.persistentStoragePath(),
             f"; Widevine {widevine}" if widevine else "")
    browser = Browser(web_profile, unattended=unattended, headless=headless, name=profile)
    browser.listen()
    if urls or not no_window:
        browser.open_urls(urls)
    wake = SignalWake(app)
    closed = []
    app.lastWindowClosed.connect(lambda: closed.append(True))

    code = app.exec()

    # Why, said before anything else can go wrong: a log that ends
    # without this line is a browser that did not end cleanly.
    why = (f"{wake.received} received" if wake.received
           else browser.ended_by if browser.ended_by
           else "its last window closed" if closed else "the event loop was left")
    log.info("ending: %s", why)
    browser.shutdown()
    shiboken6.delete(browser)
    shiboken6.delete(web_profile)
    del wake
    log.info("ended cleanly (exit code %d)", code)
    return code
