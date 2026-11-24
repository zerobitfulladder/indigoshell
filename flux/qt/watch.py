"""The Watch: the agent drives a tab while the user watches, and a key takes it back.

The user's decision: the agent works in the user's visible tab, in
their own window, while they watch. protocol.py ("Watch") has the rules
as the agent meets them; this is how the browser keeps them.

**A session.** An acting request on a tab (control.py) starts the
agent's session there, or goes on with it (`begin`). While it drives:

- The user's input to that tab is locked out. Their mouse, wheel and
  keys come from the window system, through the window's QWindow, where
  the window's event filter hands them here first (`window_event`) and
  they stop. The agent's own input never passes there: act.py sends it
  straight to the page's view (its focus proxy), not spontaneous. A
  filter on that view (`eventFilter`) stops what reaches it anyway as
  the user's: a spontaneous event, or an input method commit while the
  agent is sending none (an input method hands its text to the focused
  widget, not through the window). Modifier keys alone are swallowed
  and take nothing over: the window manager's own chords (Super+N)
  begin with one.
- An OVERLAY sits over the page: a frame FRAME_PX wide in the voice's
  colour along the page's edges, and a mark at its bottom right, the
  voice's colour on the bar's background: "Lilac is driving · press a
  key to take over". Plain widgets of the window, over the stack of
  views, never in the page: a screenshot grabs the view alone, so it
  is in none (reader.grab). A click on the locked page anywhere but
  the mark flashes the mark (its colours swapped for FLASH_MS).
- The page shows the lock in the shell's own look (effects.py, the
  user's design): it starts with the shell's panel glitch over the page;
  while it holds, Indigo's tint, the panels' raster and scan, and the
  glitch again now and then; it ends with the glitch played out over the
  live page (`_show` decides, for every way a lock starts or ends). An
  action waits for a glitch in to end (`hold_s`), and no repeat starts
  while one is in flight, so nothing it changes pops in from under the
  glitch's still image. Made per window at its first lock; where it
  cannot run (headless, no GPU scene graph, a file that fails to load)
  the frame and the mark are all there is.
- A dialog the page opens (alert, confirm, prompt, beforeunload) is
  held for the agent (`hold_dialog`): tabs.py's Page asks here before
  Qt's own dialog. Held means a nested event loop: Qt's page API is
  synchronous (QWebEnginePage.javaScriptConfirm returns the answer), so
  the page waits in it, as it waits in Qt's own modal QMessageBox, until
  `answer`, the user taking over or releasing (then it is shown to them
  as their own), or DIALOG_S unanswered (dismissed).
- A file chooser the page opens (tabs.py's Page asks `files_for`) gets
  the paths an `upload` armed for that page, once (`arm_files`), and
  nothing otherwise while the agent drives the tab; outside a session it
  is the user's, Qt's own dialog.
- Headless (protocol.py "Headless"), outside a session there is no user
  to give either to: a dialog is answered at once (`UNSEEN`: an alert
  accepted, the question before leaving a page answered "leave", a
  confirm and a prompt answered no), and a file chooser gets nothing.

Ends: QUIET_S after the last request on the tab ended (requests that
read keep it going but never start one; a held dialog holds the clock),
`release`, the tab closing, its window showing another tab, the tab
going back to the user (`return`: `drop`, which ends a pause too). A
tab moving to another window (`borrow`) keeps its session: the overlay
follows it to the window that shows it.

**Take-over.** A key the user presses in the window (not a modifier
alone), or a click on the mark, ends the session at once and PAUSES the
agent on the tab; the key and the click reach nothing (the release
after them neither). While paused, control.py refuses the agent's
requests on the tab with protocol.TOOK_OVER. The pause ends when the
user hands the tab back (`resume`: the shell's actions for the window,
"Let Lilac drive again", sent as the `resume` command) or PAUSE_S after
their last key press, click or wheel turn in that tab (the clock starts
again with each).

The shell is told through the window's state (protocol `agent`: voice
and driving or paused, per tab), and the top bar marks the tab.

The Watch never raises, focuses or moves a window, and never grabs
anything: the lock is Qt's event delivery inside this process.
"""

import logging
import time
from functools import partial

import shiboken6
from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, Qt, QTimer
from PySide6.QtWidgets import QLabel, QWidget

from flux import protocol

from . import act, effects, keys
from .window import BACKGROUND, FONT

log = logging.getLogger(__name__)

FRAME_PX = 2
FLASH_MS = 350
MARK = "{name} is driving · press a key to take over"

T = QEvent.Type
# What counts as the user's input: stopped while the agent drives.
INPUT = frozenset({
    T.MouseButtonPress, T.MouseButtonRelease, T.MouseButtonDblClick, T.MouseMove,
    T.Wheel, T.KeyPress, T.KeyRelease, T.InputMethod, T.ContextMenu,
    T.TouchBegin, T.TouchUpdate, T.TouchEnd, T.TouchCancel,
    T.TabletPress, T.TabletMove, T.TabletRelease, T.NativeGesture,
    T.DragEnter, T.DragMove, T.DragLeave, T.Drop,
    T.Enter, T.Leave, T.HoverEnter, T.HoverMove, T.HoverLeave,
})
# What the user does to a tab: the clock of a pause starts again.
DOING = frozenset({T.KeyPress, T.MouseButtonPress, T.MouseButtonDblClick, T.Wheel,
                   T.InputMethod, T.TouchBegin})

# Headless, a dialog outside the agent's session: answered at once, as
# nobody could. Leaving is let through (a page that asks must not keep
# the agent from going on); what a page asks yes or no to is answered no.
UNSEEN = {"alert": (True, ""), "beforeunload": (True, ""), "confirm": (False, ""),
          "prompt": (False, "")}


class Held:
    """A page's dialog, held for the agent: the nested loop the page
    waits in, and the answer it gets."""

    def __init__(self, kind: str, message: str, default: str) -> None:
        self.kind, self.message, self.default = kind, message, default
        self.loop = QEventLoop()
        self.answer: tuple[bool, str] | None = None
        self.to_user = False
        self.since = time.monotonic()

    def info(self) -> dict:
        # Qt words its own text for beforeunload; the page's is not shown.
        return protocol.dialog(self.kind, "" if self.kind == "beforeunload" else self.message,
                               self.default)

    def finish(self, answer: tuple[bool, str] | None = None, *, to_user: bool = False) -> None:
        if self.answer is None and not self.to_user:
            self.answer, self.to_user = answer, to_user
        self.loop.quit()


class Session:
    """The agent's hold on one tab: driving it, or paused by the user."""

    def __init__(self, watch: "Watch", tab_id: int, voice: str) -> None:
        self.tab = tab_id
        self.voice = voice
        self.state = "driving"
        self.busy: set = set()          # the requests running on the tab
        self.acting: set = set()        # ... the acting ones among them (begin)
        self.dialog: Held | None = None
        self.notes: list[str] = []      # what happened outside a request, said by the next
        self.proxies: list = []         # the views' focus proxies filtered
        self.quiet = QTimer(watch)
        self.quiet.setSingleShot(True)
        self.quiet.setInterval(int(protocol.QUIET_S * 1000))
        self.quiet.timeout.connect(partial(watch.end, tab_id, "quiet"))
        self.pause = QTimer(watch)
        self.pause.setSingleShot(True)
        self.pause.setInterval(int(protocol.PAUSE_S * 1000))
        self.pause.timeout.connect(partial(watch.pause_over, tab_id))

    def stop(self) -> None:
        self.quiet.stop()
        self.pause.stop()
        self.quiet.deleteLater()
        self.pause.deleteLater()


# ── the overlay ─────────────────────────────────────────────────────────
class Overlay(QObject):
    """The frame and the mark over a window's page while the agent drives
    its current tab: widgets of the window, over its stack of views."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.edges = [QWidget(window) for _ in range(4)]
        for edge in self.edges:
            edge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            edge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            edge.hide()
        self.mark = QLabel(window)
        self.mark.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mark.hide()
        self.color = protocol.voice_color(None)
        self.fx = None                  # the window's page effects, kept over the page with it
        self.flashing = False
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(FLASH_MS)
        self._flash.timeout.connect(partial(self._paint, False))
        window.stack.installEventFilter(self)

    def widgets(self) -> list:
        return [*self.edges, self.mark]

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (T.Resize, T.Move, T.Show) and self.mark.isVisible():
            self.place()
        return False

    def show_for(self, voice: str) -> None:
        self.color = protocol.voice_color(voice)
        self.mark.setText(MARK.format(name=protocol.voice_name(voice)))
        self._paint(self.flashing)
        self.place()
        for w in self.widgets():
            w.show()
            w.raise_()

    def hide(self) -> None:
        for w in self.widgets():
            w.hide()

    def shown(self) -> bool:
        return self.mark.isVisible()

    def place(self) -> None:
        r = self.window.stack.geometry()
        f = FRAME_PX
        top, bottom, left, right = self.edges
        top.setGeometry(r.x(), r.y(), r.width(), f)
        bottom.setGeometry(r.x(), r.y() + r.height() - f, r.width(), f)
        left.setGeometry(r.x(), r.y(), f, r.height())
        right.setGeometry(r.x() + r.width() - f, r.y(), f, r.height())
        self.mark.adjustSize()
        self.mark.move(r.x() + r.width() - f - self.mark.width(),
                       r.y() + r.height() - f - self.mark.height())
        if self.fx is not None and shiboken6.isValid(self.fx) and not self.fx.dead:
            # Inside the frame, the mark kept out: both stay as they are.
            self.fx.place(r.adjusted(f, f, -f, -f), self.mark.geometry())

    def on_mark(self, pos: QPoint) -> bool:
        return self.mark.isVisible() and self.mark.geometry().contains(pos)

    def flash(self) -> None:
        self._paint(True)
        self._flash.start()

    def _paint(self, inverted: bool) -> None:
        self.flashing = inverted
        fg, bg = (BACKGROUND, self.color) if inverted else (self.color, BACKGROUND)
        for edge in self.edges:
            edge.setStyleSheet(f"background: {self.color};")
        self.mark.setStyleSheet(f"QLabel {{ background: {bg}; color: {fg}; font-family: {FONT};"
                                " font-size: 15px; padding: 3px 8px; }")


# ── the watch ───────────────────────────────────────────────────────────
class Watch(QObject):
    """The agent's sessions, one per tab it drives or is paused on."""

    def __init__(self, browser) -> None:
        super().__init__(browser)
        self.browser = browser
        self.sessions: dict[int, Session] = {}
        self.inject = act.Injector()
        self.overlays: dict[int, Overlay] = {}      # window id -> its overlay
        self.effects: dict[int, effects.PageFx] = {}    # window id -> its page effects
        self._locked: dict[int, int | None] = {}    # window id -> the tab its effects lock
        self._release_after: set[int] = set()       # windows whose next mouse release is ours
        self._armed: dict[int, list[str]] = {}      # page id -> the next chooser's answer
        self._took: dict[int, dict] = {}            # page id -> what that chooser took

    # ── what it says ────────────────────────────────────────────────────
    def driving(self, tab_id: int) -> bool:
        s = self.sessions.get(tab_id)
        return s is not None and s.state == "driving"

    def paused(self, tab_id: int) -> bool:
        s = self.sessions.get(tab_id)
        return s is not None and s.state == "paused"

    def dialog_of(self, tab_id: int) -> Held | None:
        s = self.sessions.get(tab_id)
        return s.dialog if s is not None else None

    def about(self, tab_id: int) -> dict:
        """TAB's agent, voice and dialog (protocol.py)."""
        s = self.sessions.get(tab_id)
        if s is None:
            return {"agent": None, "voice": None, "dialog": None}
        return {"agent": s.state, "voice": s.voice,
                "dialog": s.dialog.info() if s.dialog is not None else None}

    def state_of(self, tab_id: int) -> dict | None:
        """The tab's entry in the shell's state."""
        s = self.sessions.get(tab_id)
        return protocol.agent(s.voice, s.state) if s is not None else None

    def notes(self, tab_id: int) -> list[str]:
        """What happened on the tab since its notes were last taken."""
        s = self.sessions.get(tab_id)
        if s is None:
            return []
        out, s.notes = s.notes, []
        return out

    # ── where ───────────────────────────────────────────────────────────
    def _where(self, tab_id: int) -> tuple:
        for window in self.browser.windows:
            if not shiboken6.isValid(window):
                continue
            for tab in window.tabs:
                if tab.id == tab_id:
                    return window, tab
        return None, None

    def _refresh(self, window) -> None:
        if window is not None and shiboken6.isValid(window):
            self._show(window)
            window.refresh()

    # ── sessions ────────────────────────────────────────────────────────
    def begin(self, job, window, tab, voice: str | None) -> None:
        """An acting request on `tab`: the session starts, or goes on (in
        the voice of the request, when it names one). control.py has
        refused a paused tab already."""
        s = self.sessions.get(tab.id)
        if s is None:
            s = self.sessions[tab.id] = Session(self, tab.id, protocol.voice_id(voice))
            log.info("agent: %s drives tab %d", protocol.voice_name(s.voice), tab.id)
        elif voice is not None and protocol.voice_id(voice) != s.voice:
            s.voice = protocol.voice_id(voice)
        if job not in s.acting:
            s.acting.add(job)
            job.cleanups.append(partial(self._acted, tab.id, job))
        self.guard(tab)
        self.touch(job, tab.id)
        self._refresh(window)

    def _acted(self, tab_id: int, job) -> None:
        s = self.sessions.get(tab_id)
        if s is not None:
            s.acting.discard(job)

    def acting_on(self, tab_id) -> bool:
        """One of the agent's actions is in flight on the tab (effects.py
        starts no repeat glitch meanwhile)."""
        s = self.sessions.get(tab_id)
        return s is not None and bool(s.acting)

    def hold_s(self, tab) -> float:
        """How long an acting request on `tab` waits before it acts: what
        is left of a glitch in playing over it (effects.py), whose still
        image would hide what the action changes until it ends."""
        window, _ = self._where(tab.id)
        fx = self.effects.get(window.id) if window is not None else None
        if fx is None or not shiboken6.isValid(fx) or fx.dead or fx.tab_id != tab.id:
            return 0.0
        return fx.hold_s()

    def guard(self, tab) -> None:
        """The filter on the tab's view, as it is now: the view's focus
        proxy can be made again (a page that moved to another renderer)."""
        s = self.sessions.get(tab.id)
        if s is None or s.state != "driving":
            return
        s.proxies = [p for p in s.proxies if shiboken6.isValid(p)]
        proxy = tab.view.focusProxy()
        if proxy is not None and all(p is not proxy for p in s.proxies):
            proxy.installEventFilter(self)
            s.proxies.append(proxy)

    def _unguard(self, s: Session) -> None:
        for p in s.proxies:
            if shiboken6.isValid(p):
                p.removeEventFilter(self)
        s.proxies = []

    def touch(self, job, tab_id) -> None:
        """A request running on the tab: while it does, the session does
        not run out (`job.cleanups` says when it is over)."""
        s = self.sessions.get(tab_id) if protocol.is_id(tab_id) else None
        if s is None or s.state != "driving" or job in s.busy:
            return
        s.busy.add(job)
        s.quiet.stop()
        job.cleanups.append(partial(self._done, tab_id, job))

    def _done(self, tab_id: int, job) -> None:
        s = self.sessions.get(tab_id)
        if s is not None:
            s.busy.discard(job)
            self._arm(s)

    def _arm(self, s: Session) -> None:
        if self.sessions.get(s.tab) is not s:
            return                      # over: its timers go with it
        if s.state == "driving" and not s.busy and s.dialog is None:
            s.quiet.start()
        else:
            s.quiet.stop()

    def end(self, tab_id: int, why: str) -> None:
        """The agent's session on the tab is over (not a take-over)."""
        s = self.sessions.get(tab_id)
        if s is None or s.state != "driving":
            return
        del self.sessions[tab_id]
        self._unguard(s)
        s.stop()
        self._let_go(s, to_user=True)
        log.info("agent: the session on tab %d ended (%s)", tab_id, why)
        self._refresh(self._where(tab_id)[0])

    def release(self, tab_id: int | None = None) -> list[int]:
        """The agent lets go: of one tab, or of every tab it drives."""
        ids = [tab_id] if tab_id is not None else list(self.sessions)
        out = [t for t in ids if self.driving(t)]
        for t in out:
            self.end(t, "released")
        return out

    def take_over(self, window, tab, why: str) -> None:
        s = self.sessions.get(tab.id)
        if s is None or s.state != "driving":
            return
        s.state = "paused"
        s.quiet.stop()
        s.pause.start()
        self._unguard(s)
        self._let_go(s, to_user=True)
        log.info("the user took over tab %d (%s); %s is paused there", tab.id, why,
                 protocol.voice_name(s.voice))
        self._refresh(window)

    def pause_over(self, tab_id: int) -> None:
        s = self.sessions.get(tab_id)
        if s is None or s.state != "paused":
            return
        del self.sessions[tab_id]
        s.stop()
        log.info("tab %d: no input from the user for %g s; the agent may act there again",
                 tab_id, protocol.PAUSE_S)
        self._refresh(self._where(tab_id)[0])

    def resume(self, tab) -> bool:
        """The user hands a paused tab back."""
        s = self.sessions.get(tab.id)
        if s is None or s.state != "paused":
            return False
        del self.sessions[tab.id]
        s.stop()
        log.info("the user handed tab %d back to %s", tab.id, protocol.voice_name(s.voice))
        self._refresh(self._where(tab.id)[0])
        return True

    def drop(self, tab_id: int, why: str) -> None:
        """The agent's hold on the tab goes, driving or paused: the tab is
        the user's again (returned to them)."""
        s = self.sessions.pop(tab_id, None)
        if s is None:
            return
        self._unguard(s)
        s.stop()
        self._let_go(s, to_user=True)
        log.info("agent: the %s session on tab %d ended (%s)",
                 "paused" if s.state == "paused" else "driving", tab_id, why)
        self._refresh(self._where(tab_id)[0])

    def forget(self, tab) -> None:
        """The tab is closing: its session goes, a held dialog dismissed."""
        s = self.sessions.pop(tab.id, None)
        if s is None:
            return
        self._unguard(s)
        s.stop()
        self._let_go(s, (False, ""))
        log.info("agent: tab %d closed; its %s session with it", tab.id,
                 "paused" if s.state == "paused" else "driving")

    def shown(self, window) -> None:
        """The window shows another tab: a session on one it no longer
        shows ends; the overlay follows."""
        current = window.current_tab()
        for tab in list(window.tabs):
            if tab is not current and self.driving(tab.id):
                self.end(tab.id, "its window shows another tab")
        self._show(window)

    def window_closed(self, window) -> None:
        for tab in window.tabs:
            self.forget(tab)
        self.overlays.pop(window.id, None)
        fx = self.effects.pop(window.id, None)
        if fx is not None and shiboken6.isValid(fx):
            fx.close()
        self._locked.pop(window.id, None)
        self._release_after.discard(window.id)

    def _show(self, window) -> None:
        """The one place that decides what the window shows of the agent:
        the overlay, and the page effects (`_effects`)."""
        tab = window.current_tab()
        s = self.sessions.get(tab.id) if tab is not None else None
        overlay = self.overlays.get(window.id)
        if overlay is not None and not shiboken6.isValid(overlay.mark):
            self.overlays.pop(window.id, None)
            overlay = None
        if s is None or s.state != "driving":
            if overlay is not None:
                overlay.hide()
            self._effects(window, tab, None)
            return
        if overlay is None:
            overlay = self.overlays[window.id] = Overlay(window)
        overlay.fx = self._fx(window)
        overlay.show_for(s.voice)       # places the effects too
        self._effects(window, tab, tab)

    def _fx(self, window) -> "effects.PageFx | None":
        """The window's page effects, made at its first lock (None where
        they cannot run: effects.make says why, once)."""
        fx = self.effects.get(window.id)
        if fx is not None and (not shiboken6.isValid(fx) or fx.dead):
            self.effects.pop(window.id, None)
            fx = None
        if fx is None:
            fx = effects.make(window, self.acting_on)
            if fx is not None:
                self.effects[window.id] = fx
        return fx

    def _effects(self, window, shown, locked) -> None:
        """The page effects follow the lock on the tab the window shows:
        a lock starts (a first action, a borrowed tab arriving locked):
        the glitch in; it ends with its tab still shown (quiet, release,
        return, the user's take-over): the glitch out; its tab no longer
        shown (closed, switched away, moved off): gone at once. A paused
        tab is no lock: nothing."""
        was = self._locked.get(window.id)
        now = locked.id if locked is not None else None
        self._locked[window.id] = now
        fx = self.effects.get(window.id)
        if fx is None or not shiboken6.isValid(fx) or fx.dead:
            return
        if now is not None:
            if now != was or fx.tab_id != now or fx.phase in ("off", "out"):
                fx.lock(locked)
        elif was is not None and shown is not None and shown.id == was:
            fx.unlock()
        elif fx.tab_id is not None and (shown is None or shown.id != fx.tab_id):
            fx.vanish()

    # ── the user's input ────────────────────────────────────────────────
    def window_event(self, window, event) -> bool | None:
        """An event on the window's QWindow (window.py's filter, before
        anything else): True to stop it, None to leave it to the window."""
        kind = event.type()
        if kind not in INPUT:
            return None
        if kind == T.MouseButtonRelease and window.id in self._release_after:
            self._release_after.discard(window.id)
            return True
        tab = window.current_tab()
        s = self.sessions.get(tab.id) if tab is not None else None
        if s is None:
            return None
        if s.state == "paused":
            if kind in DOING:
                s.pause.start()         # the user is at it: the pause goes on
            return None
        if kind == T.KeyPress:
            if event.key() in keys.MODIFIER_KEYS:
                return True
            window.swallow_release(event)
            self.take_over(window, tab, "a key")
            return True
        if kind in (T.MouseButtonPress, T.MouseButtonDblClick):
            overlay = self.overlays.get(window.id)
            self._release_after.add(window.id)
            if overlay is not None and overlay.on_mark(event.position().toPoint()):
                self.take_over(window, tab, "a click on the mark")
            elif overlay is not None:
                overlay.flash()
            return True
        return True

    def eventFilter(self, obj, event) -> bool:
        """The view of a tab the agent drives: what reaches it as the
        user's (spontaneous, or an input method's text the agent is not
        sending) stops here. Only installed while it drives."""
        kind = event.type()
        if kind not in INPUT:
            return False
        users = event.spontaneous() or (kind == T.InputMethod and not self.inject.sending)
        if not users:
            return False
        s = next((s for s in self.sessions.values() if any(p is obj for p in s.proxies)), None)
        if s is None or s.state != "driving":
            return False
        window, tab = self._where(s.tab)
        if window is None:
            return True
        typed = (kind == T.KeyPress and event.key() not in keys.MODIFIER_KEYS) or (
            kind == T.InputMethod and bool(event.commitString()))
        if typed:
            self.take_over(window, tab, "a key")
        elif kind in (T.MouseButtonPress, T.MouseButtonDblClick):
            overlay = self.overlays.get(window.id)
            if overlay is not None:
                overlay.flash()
        return True

    # ── dialogs and file choosers ───────────────────────────────────────
    def hold_dialog(self, page, kind: str, message: str, default: str) -> tuple | None:
        """The page's dialog, held for the agent while it drives the tab:
        (accepted, text) once answered; None when it is the user's (no
        session, or given to them meanwhile). Headless, with no session,
        answered at once (UNSEEN)."""
        s = self.sessions.get(page.id)
        if s is None or s.state != "driving" or s.dialog is not None:
            if self.browser.headless:
                log.info("tab %d: %s %s dialog answered at once (headless, no one to ask)",
                         page.id, "an" if kind[0] in "aeiou" else "a", kind)
                return UNSEEN.get(kind, (False, ""))
            return None
        held = Held(kind, message, default)
        s.dialog = held
        s.quiet.stop()
        log.info("tab %d: a %s dialog held for the agent", page.id, kind)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(partial(self._unanswered, page.id, held))
        timer.start(int(protocol.DIALOG_S * 1000))
        self._refresh(self._where(page.id)[0])
        try:
            held.loop.exec()
        finally:
            timer.stop()
            if s.dialog is held:
                s.dialog = None
            if self.sessions.get(page.id) is s:
                self._arm(s)
        if held.to_user:
            log.info("tab %d: the %s dialog goes to the user", page.id, kind)
            return None
        return held.answer if held.answer is not None else (False, "")

    def _unanswered(self, tab_id: int, held: Held) -> None:
        s = self.sessions.get(tab_id)
        if s is not None and s.dialog is held:
            s.notes.append(f"the page's {held.kind} dialog was dismissed: unanswered for "
                           f"{protocol.DIALOG_S:g} s")
        log.info("tab %d: the %s dialog dismissed, unanswered for %g s", tab_id, held.kind,
                 protocol.DIALOG_S)
        if s is not None and s.dialog is held:
            self._let_go(s, (False, ""))
        else:
            held.finish((False, ""))

    def answer(self, tab_id: int, accept: bool, text: str | None) -> bool:
        s = self.sessions.get(tab_id)
        held = s.dialog if s is not None else None
        if held is None:
            return False
        if text is None:
            text = held.default if held.kind == "prompt" else ""
        log.info("tab %d: the agent %s the %s dialog", tab_id,
                 "accepted" if accept else "dismissed", held.kind)
        self._let_go(s, (bool(accept), str(text)))
        return True

    def _let_go(self, s: Session, answer: tuple | None = None, *, to_user: bool = False) -> None:
        """The session's held dialog answered (or given to the user): no
        longer the agent's at once, though the page's call returns only
        once the nested loop it waits in has."""
        held, s.dialog = s.dialog, None
        if held is not None:
            held.finish(answer, to_user=to_user)
            self._arm(s)

    def arm_files(self, tab_id: int, paths: list[str]) -> None:
        """The next file chooser the tab's page opens gets `paths`
        (`upload`), once."""
        self._armed[tab_id] = list(paths)
        self._took.pop(tab_id, None)

    def disarm_files(self, tab_id: int) -> dict | None:
        """The upload is over: what the chooser took, or None when the
        page opened none."""
        self._armed.pop(tab_id, None)
        return self._took.pop(tab_id, None)

    def took_files(self, tab_id: int) -> dict | None:
        return self._took.get(tab_id)

    def files_for(self, page, mode: str) -> list[str] | None:
        """A file chooser the page opens, in `mode` (a FileSelectionMode's
        name): the paths armed for it; none while the agent drives the tab
        otherwise (refused, said in the next answer; Chromium then empties
        the field, as it does for the user's Cancel: Qt names no files the
        field held to give back); None when it is the user's (Qt's own
        dialog), and nothing headless, where there is no user to ask."""
        paths = self._armed.pop(page.id, None)
        if paths is not None:
            if mode not in ("FileSelectOpen", "FileSelectOpenMultiple"):
                self._took[page.id] = {"error": "the page asked for a folder or a place to save, "
                                                "not files; it got none"}
            elif mode == "FileSelectOpen" and len(paths) > 1:
                self._took[page.id] = {"error": f"the page's chooser takes one file; "
                                                f"{len(paths)} were given and it got none"}
            else:
                self._took[page.id] = {"paths": list(paths)}
                log.info("tab %d: the page's file chooser answered with %d file(s) (upload)",
                         page.id, len(paths))
                return list(paths)
            log.info("tab %d: the page's file chooser got nothing (%s)", page.id, mode)
            return []
        s = self.sessions.get(page.id)
        if s is None or s.state != "driving":
            if self.browser.headless:
                log.info("tab %d: a file chooser got nothing (headless, no one to ask)",
                         page.id)
                return []
            return None
        s.notes.append(protocol.FILES_REFUSED)
        log.info("tab %d: a file chooser refused (the agent drives it)", page.id)
        return []

