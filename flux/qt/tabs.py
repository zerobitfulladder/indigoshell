"""One tab: a page, the view that shows it, and what the shell is told about it.

A window keeps its tabs' views in a stack and shows the current one; a
tab in the background keeps loading and running (Chromium throttles it).
A tab moves to another window (the agent's `borrow` and `return`) by
its page going into a new view there (`new_view`): the same page, so
everything in it stays (measured: a half-filled form, the scroll, the
page's scripts and cookies), and the tab keeps its id.

The page's own signals are forwarded to the window as `changed` (what
the top bar and the window's line show moved), `visited` and `titled` (for the
history) and as the requests the window decides on (new windows,
fullscreen, permissions, closing). Its favicon is kept as the PNG the
shell is sent and that PNG's id (`icon`, favicon.py), for its tab in
the top bar. The icon goes with its site, not with each page: QtWebEngine
clears it as every navigation starts and gives it again only after the
load ends (measured: a null `iconChanged` at the start, the icon a few
ms after LoadSucceeded locally, after the page's own load on a real
site), which would flash the tab to its letter on every click within a
site. So a null is not taken; a page on another host drops it, and the
tab shows that host's letter until its own icon comes.

What counts as a visit: a main-frame load that succeeded, and a
same-document navigation on a page that did (a site moving with
pushState, as YouTube does between videos). A load that failed, was
stopped or is still going is none; the address and title are read once
the load is done. `visited` says `again` when the page is the one this
tab last recorded (a reload, a jump to an anchor), which moves its time
without counting another visit.

Where the page came from (`came_from`). A navigation someone starts (an
address loaded, a link clicked, back or forward) begins at an address;
what follows on its own belongs to it: HTTP redirects, the page sending
itself on by script or <meta> refresh, and the page moving itself (a
script's navigation, its address rewritten with pushState) with no key
pressed or button clicked on it since it arrived, while it loads or
within CLIENT_REDIRECT_S after. A reload stays where it began. When the
page shown is not at the address its navigation began at, `came_from`
is that address, until the next navigation someone starts. So a
bookmark that now redirects still counts for the page it leads to
(window.py).

What QtWebEngine 6.12 reports, measured (tests/browser.py has the
cases):

    navigationRequested   every main-frame navigation, before it goes,
                          and every HTTP redirect hop (RedirectNavigation,
                          the hop's address). A script's navigation while
                          the page loads (location.replace, location.href,
                          in onload) and a <meta> refresh, even a delayed
                          one after the load: RedirectNavigation too. A
                          script's navigation after the load event:
                          LinkClickedNavigation, the same as a click.
                          Same-document moves (pushState, an anchor): none.
    loadingChanged        Started with the address asked for, Succeeded
                          with the final one: one load for a whole chain
                          of HTTP and in-load script redirects (the
                          script's page commits on the way: urlChanged);
                          a <meta> refresh is a second load. A download
                          or a 204 ends Stopped with nothing shown; a load
                          superseded by another ends with no status at all.
    page.requestedUrl()   the first address through HTTP, in-load script
                          and <meta> redirects; not through a script's
                          later navigation. The request interceptor sees
                          every hop but names a hop's type as the first
                          request's (TypedNavigation), so it is not used.

The page's dialogs (alert, confirm, prompt, and the question before
leaving a page that asks, "beforeunload") and its file choosers go to
the browser's Watch first (watch.py): held for the agent while it drives
the tab (a chooser answered with an upload's files), the user's own
otherwise, as Qt shows them. Qt asks for each
with a call that returns the answer, so a dialog is a nested event loop
whichever answers it; while one is open (`in_dialog`) the page must not
be deleted, and what would delete it waits (`after_dialog`, window.py).
Qt words the beforeunload question itself (UNLOAD_MESSAGE) and asks it
with javaScriptConfirm, which is how it is told from a page's confirm().
"""

import itertools
import logging
import time

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import (QWebEngineLoadingInfo, QWebEngineNavigationRequest,
                                     QWebEnginePage)
from PySide6.QtWebEngineWidgets import QWebEngineView

from flux import bar as flux_bar, places, protocol, zoom
from flux.qt import favicon

log = logging.getLogger(__name__)
js_log = logging.getLogger(__name__.rsplit(".", 1)[0] + ".js")

# Chromium's zoom steps.
ZOOM_STEPS = (0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75,
              2.0, 2.5, 3.0, 4.0, 5.0)

# A page that sends itself on this soon after loading, untouched, is
# redirecting (see "Where the page came from").
CLIENT_REDIRECT_S = 5.0

# A load asked for (`expect_load`) counts as begun this long before
# QtWebEngine says it began (it says so within milliseconds; a
# same-document move never does).
ASKED_S = 5.0

SEARCH = places.SEARCH

_page_ids = itertools.count(1)


def to_url(text: str) -> QUrl:
    """An address if it looks like one, otherwise a search
    (places.classify, which the shell's launcher uses too)."""
    _kind, url = places.classify(text)
    return QUrl(url)


# How Qt words the question before leaving a page that asks (its
# QWebEnginePage::javaScriptConfirm for Chromium's beforeunload dialog).
UNLOAD_MESSAGE = ("Are you sure you want to leave this page? Changes that you made may not "
                  "be saved.")


def unload_message() -> str:
    return QCoreApplication.translate("QWebEnginePage", UNLOAD_MESSAGE)


# An empty page (a new tab, a new window, before anything is loaded)
# shows the shell's background instead of Chromium's white. Only empty
# pages Flux made itself: a real page that sets no background keeps
# white under it, or its default black text would be unreadable, and so
# does a blank page a site opened and writes into (`opened_by_page`).
BLANK_BACKGROUND = QColor("#050310")    # theme.BG
PAGE_BACKGROUND = QColor("#ffffff")     # Chromium's own, for pages


def _blank(url: QUrl) -> bool:
    return url.isEmpty() or url.toString() == "about:blank"


class Page(QWebEnginePage):
    """A page whose console goes to the log at debug level, not the
    terminal. `id` names its tab to the agent (flux/qt/control.py):
    never reused in this process, and the page's, so it goes with the
    page should the page move to another tab or window. Its dialogs and
    file choosers ask `watch` first (the module docstring)."""

    def __init__(self, profile, parent=None) -> None:
        super().__init__(profile, parent)
        self.id = next(_page_ids)
        self.watch = None               # the browser's Watch, once in a window
        self.in_dialog = 0              # dialogs open on it now, the user's or held
        self.after_dialog: list = []    # run once none is
        self.from_page = False          # opened by a site (see BLANK_BACKGROUND)
        self.setBackgroundColor(BLANK_BACKGROUND)
        self.urlChanged.connect(self._background)

    def opened_by_page(self) -> None:
        """A site asked for this page (a link's new tab, a popup): it
        gets the page white from the start, whatever it shows."""
        self.from_page = True
        self.setBackgroundColor(PAGE_BACKGROUND)

    def _background(self, url: QUrl) -> None:
        empty = _blank(url) and not self.from_page
        self.setBackgroundColor(BLANK_BACKGROUND if empty else PAGE_BACKGROUND)

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:
        js_log.debug("js: %s (%s:%d)", message, source, line)

    # ── dialogs ─────────────────────────────────────────────────────────
    def _dialog(self, kind: str, origin, message: str, default: str = "") -> tuple:
        self.in_dialog += 1
        try:
            held = (self.watch.hold_dialog(self, kind, message, default)
                    if self.watch is not None else None)
            return held if held is not None else self.user_dialog(kind, origin, message, default)
        finally:
            self.in_dialog -= 1
            if not self.in_dialog and self.after_dialog:
                # Not now: this frame is still inside the page's own call.
                todo, self.after_dialog = self.after_dialog, []
                for fn in todo:
                    QTimer.singleShot(0, fn)

    def user_dialog(self, kind: str, origin, message: str, default: str) -> tuple:
        """The dialog as Qt shows it, the user's own: (accepted, text)."""
        if kind == "alert":
            super().javaScriptAlert(origin, message)
            return True, ""
        if kind == "prompt":
            ok, text = super().javaScriptPrompt(origin, message, default)
            return bool(ok), str(text or "")
        return bool(super().javaScriptConfirm(origin, message)), ""

    def javaScriptAlert(self, origin, message) -> None:
        self._dialog("alert", origin, message)

    def javaScriptConfirm(self, origin, message) -> bool:
        kind = "beforeunload" if message == unload_message() else "confirm"
        return bool(self._dialog(kind, origin, message)[0])

    def javaScriptPrompt(self, origin, message, default):
        ok, text = self._dialog("prompt", origin, message, default)
        return bool(ok), str(text) if ok else ""

    def chooseFiles(self, mode, old_files, accepted_types):
        chosen = self.watch.files_for(self, mode.name) if self.watch is not None else None
        if chosen is not None:
            return chosen               # the agent's upload, or refused while it drives
        return super().chooseFiles(mode, old_files, accepted_types)


class Tab(QObject):
    changed = Signal()
    visited = Signal(str, bool)         # address, again
    titled = Signal(str, str)           # address, title
    zoomed = Signal(float)              # the user zoomed the page: the factor it went to

    def __init__(self, profile, parent=None) -> None:
        super().__init__(parent)
        self.view = QWebEngineView()
        self.page = Page(profile, self.view)
        self.view.setPage(self.page)
        self.loading = False
        self.progress = 100
        self.inspector = None           # the devtools Window, once opened
        self.icon: tuple[str, bytes] | None = None  # its page's favicon: (id, PNG)
        self._icon_host = ""            # the host it came with
        self._document_ok = False       # the page shown loaded fine
        self._recorded = ""             # page_key of the last visit recorded

        # Where the page came from (the module docstring).
        self.came_from: str | None = None   # where its navigation began, when not here
        self._start = ""                # where the page shown began
        self._pending: str | None = None    # where the navigation in flight began
        self._here = ""                 # page_key of the page shown
        self._input = False             # a key or a click on it since it arrived
        self._settled: float | None = None  # when its load ended; None while loading
        self._asked: float | None = None    # when the agent asked for a load not yet ended
        # Counted for the agent's actions (control.py): main-frame
        # navigations asked for, and those plus same-document moves.
        self.loads = 0
        self.navigations = 0
        # The next name a frame's document gets (frames.py): "f<n>",
        # never given twice in the tab.
        self.next_frame = 1
        # The zoom kept per site (flux/zoom.py, the browser's): given to
        # each page of a site as it arrives. None: none kept (the window
        # gives it before the first load).
        self.zooms = None

        page = self.page
        page.titleChanged.connect(self.changed)
        page.titleChanged.connect(self._on_title)
        page.urlChanged.connect(self.changed)
        page.urlChanged.connect(self._on_url)
        page.urlChanged.connect(self._site_zoom)
        page.loadProgress.connect(self._on_progress)
        page.loadingChanged.connect(self._on_loading)
        page.iconChanged.connect(self._on_icon)
        page.urlChanged.connect(self._icon_site)
        page.navigationRequested.connect(self._on_navigation)
        page.renderProcessTerminated.connect(self._on_crash)

    def new_view(self) -> QWebEngineView:
        """The page in a new view, for the tab's new window; the old view,
        empty now, is returned for its window to drop. The page is the new
        view's child from here on: the old view's deletion must not take
        it."""
        old = self.view
        view = QWebEngineView()
        view.setPage(self.page)
        self.page.setParent(view)
        self.view = view
        return old

    # ── what the top bar shows ──────────────────────────────────────────
    def url(self) -> QUrl:
        url = self.page.url()
        return url if not url.isEmpty() else self.page.requestedUrl()

    def title(self) -> str:
        title = self.page.title()
        return title or self.url().toString()

    def host(self) -> str:
        return self.url().host()

    def secure(self) -> bool:
        return not protocol.insecure_scheme(self.url().scheme())

    def describe(self) -> dict:
        return flux_bar.tab(self.title(), self.host(), self.loading, id_=self.id,
                            image=self.icon[0] if self.icon else None)

    def _on_icon(self, icon) -> None:
        png = favicon.png_of(icon)
        if png is None:
            return                      # a navigation starting (the module docstring)
        icon_ = (favicon.id_of(png), png)
        self._icon_host = self.page.url().host()
        if icon_ != self.icon:
            self.icon = icon_
            self.changed.emit()

    def _icon_site(self, url: QUrl) -> None:
        """A page arrived: the icon stays only with the host it came with."""
        if self.icon is not None and url.host() != self._icon_host:
            self.icon = None
            self.changed.emit()

    # ── the agent's loads ───────────────────────────────────────────────
    @property
    def id(self) -> int:
        return self.page.id

    def expect_load(self) -> None:
        """A load was just asked for (by the agent): until it ends, the
        tab counts as loading, from now, not from when QtWebEngine first
        says so."""
        self._asked = time.monotonic()

    def asked(self) -> bool:
        """A load asked for that has not ended: begun, or asked for
        moments ago."""
        if self._asked is None:
            return False
        if self.loading or time.monotonic() - self._asked < ASKED_S:
            return True
        self._asked = None              # it never began (nothing to load)
        return False

    def busy(self) -> bool:
        return self.loading or self.asked()

    def _on_progress(self, percent: int) -> None:
        self.progress = percent
        self.changed.emit()

    def _on_loading(self, info) -> None:
        S = QWebEngineLoadingInfo.LoadStatus
        status = info.status()
        self.loading = status == S.LoadStartedStatus
        self._document_ok = status == S.LoadSucceededStatus
        if self.loading:
            self.progress = 0
            if self.zooms is not None and _blank(self.page.url()):
                # Nothing shown to re-zoom yet (a new tab, a reopened one):
                # the site's zoom now, ahead of its page (a page that gets a
                # renderer process of its own may still start at the zoom
                # before: QtWebEngine's zoom is the page's, given to a new
                # renderer as its page starts).
                self.set_zoom(self.zooms.factor(info.url().toString()))
        else:
            self.progress = 100
            self._asked = None
            if status == S.LoadFailedStatus:
                log.info("load failed: %s (%s)", info.url().toString(), info.errorString())
            if self._pending is not None:
                if self._document_ok or info.isErrorPage():
                    self._arrived()     # the address it was at already: no urlChanged
                else:
                    self._pending = None    # a download, a 204: the page shown stays
            if self._settled is None:
                self._settled = time.monotonic()
        if self._document_ok:
            self._record(self.page.url())
        self.changed.emit()

    # ── where the page came from ────────────────────────────────────────
    def _on_navigation(self, request) -> None:
        if not request.isMainFrame():
            return
        self.loads += 1
        self.navigations += 1
        T = QWebEngineNavigationRequest.NavigationType
        kind = request.navigationType()
        url = request.url().toString()
        goes_on = kind in (T.RedirectNavigation, T.ReloadNavigation) or (
            kind not in (T.TypedNavigation, T.BackForwardNavigation) and self._unasked())
        if not goes_on:
            self._pending = url
        elif self._pending is None:
            self._pending = self._start or url
        log.debug("navigation %s: %s (%s)", kind.name, url,
                  f"part of the one from {self._pending}" if goes_on else "a beginning")

    def _unasked(self) -> bool:
        """Whether what the page does now is its own: no key or click on
        it since it arrived, and it is loading or loaded moments ago."""
        return not self._input and (self._settled is None
                                    or time.monotonic() - self._settled < CLIENT_REDIRECT_S)

    def note_input(self) -> None:
        """A key or a click went to this tab's page, or the user went
        back or forward in it: what follows is the user's doing."""
        self._input = True

    def _arrived(self) -> None:
        """A navigation in flight shows its page."""
        if self._pending is not None:
            self._start, self._pending = self._pending, None
            self._input, self._settled = False, None
        self._place()

    def _moved(self, url: QUrl) -> None:
        """A same-document move to another page (pushState): the user's
        doing, or long after the load, a beginning of its own; otherwise
        the page settling where it is."""
        if places.page_key(url.toString()) == self._here:
            return                      # an anchor: the same page
        if not self._unasked():
            self._start, self._input = url.toString(), False
            self._settled = time.monotonic()
        self._place()

    def _place(self) -> None:
        self._here = here = places.page_key(self.page.url().toString())
        start = self._start
        came = start if start and places.page_key(start) != here else None
        if came != self.came_from:
            self.came_from = came
            if came:
                log.debug("%s reached from %s", self.page.url().toString(), came)
            self.changed.emit()         # the state's `bookmarked` may follow

    # ── history ─────────────────────────────────────────────────────────
    def _on_url(self, url: QUrl) -> None:
        if self.loading:
            # The load in flight committed its page; the load's end
            # records the visit.
            self._arrived()
            return
        # A same-document move: a load asked for that ends here.
        self._asked = None
        self.navigations += 1
        if self._document_ok:
            self._record(url)
        self._moved(url)

    def _on_title(self, title: str) -> None:
        if self._document_ok:
            self.titled.emit(self.page.url().toString(), title)

    def _record(self, url: QUrl) -> None:
        text = url.toString()
        if not places.recordable(text):
            return
        key = places.page_key(text)
        again, self._recorded = key == self._recorded, key
        self.visited.emit(text, again)

    def _on_crash(self, status, code) -> None:
        log.warning("page renderer ended (%s, code %d): %s", status.name, code,
                    self.url().toString())

    # ── zoom ────────────────────────────────────────────────────────────
    def zoom(self, step: int) -> None:
        """The user's zoom: one Chromium zoom step in (+1) or out (-1); 0
        back to 100%. Kept for the page's site (flux/zoom.py), and said
        (`zoomed`), even when it is where it was (the last step)."""
        now = self.page.zoomFactor()
        if step == 0:
            nxt = 1.0
        elif step > 0:
            nxt = next((z for z in ZOOM_STEPS if z > now + 1e-3), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < now - 1e-3), ZOOM_STEPS[0])
        self.page.setZoomFactor(nxt)
        if self.zooms is not None:
            url = self.url().toString()
            try:
                if self.zooms.set(url, nxt):
                    log.info("zoom for %s: %s", zoom.site_of(url), "forgotten (100%)"
                             if zoom.percent_of(nxt) == 100 else f"{zoom.percent_of(nxt)}%")
            except (zoom.ZoomBroken, OSError) as e:
                log.warning("zoom not kept: %s", e)
        self.zoomed.emit(nxt)

    def set_zoom(self, factor: float) -> None:
        """The page at `factor`, not the user's own step: the site's kept
        zoom given to it."""
        if abs(self.page.zoomFactor() - factor) > 1e-3:
            self.page.setZoomFactor(factor)

    def _site_zoom(self, url: QUrl) -> None:
        """A page arrived (a load committed, a redirect's page, a
        same-document move): the zoom kept for its site, 100% when none.
        QtWebEngine carries a page's zoom across its navigations and gives
        it again just before this, so what is set here holds. It reaches
        the page's renderer as the page starts, not before: a script that
        reads its size as the page loads may see the zoom before. A tab
        with nothing shown yet gets its site's zoom when its load starts
        instead (`_on_loading`), ahead of the page; one showing a page
        cannot, the page being left would be re-zoomed while the next one
        loads."""
        if self.zooms is not None:
            self.set_zoom(self.zooms.factor(url.toString()))
