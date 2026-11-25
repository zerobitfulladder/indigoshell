"""Flux in the shell, for now: a Flux window's state turned into its bar,
the bar's events into Flux's commands, and the launcher for its Ctrl+L,
Ctrl+T and Ctrl+M.

A Flux window (flux/qt) connects to the shell's app service
(applink/service.py) and announces itself as a "flux" window; the
service gives it a `FluxWindow`, which takes the state the browser
pushes (flux/protocol.py), makes the window's bar of it (flux/bar.py:
the tabs, a box each, and the host) and shows that in its `BarView`
(applink/view.py, the kinds in applink/kinds/), which the top bar's
context area shows while the window has focus (applink/context.py).
While the browser's find is open the section is its box instead: a
search mark in the bracketed spot, the query (the caret only while it is
typed), the count at its right end, "no matches" in the error colour.

The mouse on the tabs arrives as the bar's events, by tab id (`select`,
`close`, `move` with the tab it lands `before`, `detach`), turned here
into the browser's commands (flux/protocol.py: `tab`, `close`, `move`
to a position, `detach`).

The launcher. The shell takes Ctrl+L, Ctrl+T and Ctrl+M from every main
browser window (protocol.KEYS, grabbed by the app service: applink/
service.py); a single window (a sign-in popup, the inspector) keeps
them. The press arrives here (`on_key`) with the keyboard: it is held
past the key's release at once (`hold_keyboard`), and the shell's
launcher opens (widgets/launcher.py, `open_launcher`) on Flux's addresses
(services/addresses.py: Ctrl+L with the page's address as the query,
selected; Ctrl+T empty) or its actions (a services/modes.Choices of "Add
bookmark" and "Remove bookmark", the one that does not apply dimmed; and
first, for each tab the user took over from the agent, "Let Lilac drive
again"), and takes the keyboard with a grab of its own. Keys typed
between the press and that grab still come in on the browser's window
(`on_typed`) and go to the launcher's query in the order typed, ahead of
the ones typed there since. It drops from the top bar: centred on the
browser's window, its top at the bar's bottom edge, kept on that
window's monitor (window.Placement); with no top bar over the window,
from the window's own top edge. Picking a row sends the browser `load`
or `bookmark`; Shift+Delete on a bookmark's row in the address list
sends `bookmark` "remove" with that row's address. A new window opened
with no address asks for the same with a `launcher` message. With no
launcher to show the keyboard goes back and the browser is told to ask
itself (`prompt`).

Import rule: of the browser package only `protocol`, `places` and `bar`
(all pure) are imported here; nothing under `flux/qt` ever is, and
nothing there imports this module.
"""

import logging
from functools import partial

import xcffib.xproto as xp

from ..services.addresses import BOOKMARK, AddressMode
from ..services.modes import Choice, Choices
from ..widgets.base import KeyEvent
from ..window import Placement
from .kinds.input import InputBox
from .kinds.look import ICONS, ROLE_COLORS
from .service import AppWindow
from .view import BarView
from flux import bar as flux_bar, places, protocol

log = logging.getLogger(__name__)

BOOKMARK_MARK = BOOKMARK            # Nerd Font: fa-bookmark
RESUME_MARK = ICONS["play"]         # Nerd Font: fa-play


class FluxWindow(AppWindow):
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
        self.view = BarView(emit=self._on_event)
        self.content = self.view
        self.find_box = InputBox()
        self.find_box.show({"icon": "search", "placeholder": protocol.FIND_LABEL})
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
        self.view.set_bar(flux_bar.bar_of(state, single=self.single, agent=self.agent))
        find = state[protocol.FIND]
        if find is None:
            self.view.show_box(None)
            return
        field = self.find_box.field
        if field.text != find["query"]:
            field.set_text(find["query"], notify=False)
        self.find_box.set_stepping(not find["typing"])
        count = protocol.find_count(find)
        self.find_box.set_count((count, ROLE_COLORS["error" if count == protocol.NO_MATCHES
                                                     else "info"]) if count else None)
        self.view.show_box(self.find_box)

    def _on_event(self, event: dict) -> None:
        """The bar's events (the mouse on the tabs), as the browser's
        commands."""
        if self.closed:
            log.info("the browser window is gone; %s not sent", event.get("type"))
            return
        kind, tab = event.get("type"), event.get("tab")
        log.debug("browser 0x%x: %s", self.wid, event)
        if kind == "select":
            self.command("tab", tab=tab)
        elif kind == "close":
            self.command("close", tab=tab)
        elif kind == "detach":
            self.command("detach", tab=tab)
        elif kind == "move":
            ids = [t[protocol.TAB_ID] for t in self.state[protocol.TABS]]
            others = [t for t in ids if t != tab]
            before = event.get("before")
            self.command("move", tab=tab, to=others.index(before) if before in others
                         else len(others))

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
