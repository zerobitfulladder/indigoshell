"""Flux in the shell, for now: a Flux window's state turned into its bar,
the bar's events into Flux's commands, and the answers to its address
inputs from Flux's places, until Flux says all of it itself.

A Flux window (flux/qt) connects to the shell's app service
(applink/service.py) and announces itself as a "flux" window; the
service gives it a `FluxWindow`, which takes the state the browser
pushes (flux/protocol.py), makes the window's bar of it (flux/bar.py:
the tabs, a box each, and the host; the `address` and `new_tab` inputs
and the `actions` menu) and shows that in its `BarView`
(applink/view.py, the kinds in applink/kinds/), which the top bar's
context area shows while the window has focus (applink/context.py).
While the browser's find is open the section is its box instead: a
search mark in the bracketed spot, the query (the caret only while it is
typed), the count at its right end, "no matches" in the error colour.

The bar's events turn into the browser's commands here: the mouse on
the tabs (`select`, `close`, `move` with the tab it lands `before`,
`detach`) into `tab`, `close`, `move` to a position and `detach`; a
`query` of an address input is answered at once from Flux's history and
bookmarks (services/addresses.py, the stand-in); `submit` loads the row
or the text (`load`, here for `address`, in a new tab for `new_tab`);
`act` takes a bookmark out (`bookmark` "remove" with its address);
`pick` in the actions adds or removes the page's bookmark or hands a tab
back to the agent (`resume`); `fallback` is `prompt` (the browser asks
itself). A new window with no address asks for its address input with
a `launcher` message.

Import rule: of the browser package only `protocol`, `places` and `bar`
(all pure) are imported here; nothing under `flux/qt` ever is, and
nothing there imports this module.
"""

import logging

from ..services.addresses import BOOKMARK, HISTORY, AddressMode
from . import bar as B
from .kinds.input import InputBox
from .kinds.look import ROLE_COLORS
from .service import AppWindow
from flux import bar as flux_bar, places, protocol

log = logging.getLogger(__name__)

WHERE = {flux_bar.ADDRESS: "here", flux_bar.NEW_TAB: "tab"}


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
        agent = announce.get(protocol.AGENT)
        self.agent = protocol.voice_id(agent) if isinstance(agent, str) and agent else None
        self.find_box = InputBox()
        self.find_box.show({"icon": "search", "placeholder": protocol.FIND_LABEL})
        self.modes: dict[str, AddressMode] = {}     # an open address input's places
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
            if action == flux_bar.ADDRESS:
                self.service.open_prompt(self, "input", action, pressed=False)
            else:
                log.debug("launcher for %r not understood", action)
        else:
            log.debug("browser 0x%x: message %r not understood", self.wid, kind)

    def _show(self, state: dict) -> None:
        if state == self.state:
            return
        self.state = state
        self.show_bar(flux_bar.bar_of(state, single=self.single, agent=self.agent))
        self._show_find()

    def _show_find(self) -> None:
        find = self.state[protocol.FIND]
        if self.service.prompt_of(self) is not None:
            return                      # an input of the bar's is open over it
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

    def prompt_closed(self, prompt) -> None:
        self._show_find()

    def send_event(self, event: dict) -> None:
        """The bar's events, as the browser's commands, or answered here
        (the module docstring)."""
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
        elif kind == B.QUERY:
            self._answer(event)
        elif kind == B.SUBMIT:
            self._submit(event)
        elif kind == B.ACT:
            self._act(event)
        elif kind == B.PICK:
            self._pick(event)
        elif kind == B.CLOSED:
            mode = self.modes.pop(event.get("input"), None)
            if mode is not None:
                mode.close()
        elif kind == B.FALLBACK:
            action = event.get("input") or event.get("menu")
            if action in protocol.ACTIONS:
                self.command(protocol.PROMPT, action=action)

    # ── the address inputs, answered from Flux's places ─────────────────
    def _mode(self, name: str) -> AddressMode:
        mode = self.modes.get(name)
        if mode is None:
            mode = self.modes[name] = AddressMode(on_load=lambda url: None,
                                                  on_remove=lambda url: True)
        return mode

    @staticmethod
    def _row(entry) -> dict:
        icon = "bookmark" if entry.glyph == BOOKMARK else "history" if entry.glyph == HISTORY \
            else None
        return B.row(flux_bar.TYPED if entry.key else entry.value, entry.title,
                     detail=entry.detail or None, tag=entry.key or None, icon=icon,
                     match=list(entry.positions) or None,
                     actions=[flux_bar.REMOVE] if icon == "bookmark" else None)

    def _answer(self, event: dict) -> None:
        name = event.get("input")
        mode = self._mode(name)
        entries, start = mode.search(str(event.get("text") or ""))
        rows = [self._row(e) for e in entries]
        total = int(mode.count(entries).split("/")[1])
        msg = B.answer(name, event.get("seq"), rows,
                       selected=rows[start]["id"] if rows else None, total=total)
        prompt = self.service.prompt_of(self)
        if prompt is not None and prompt.what == "input" and prompt.name == name:
            prompt.answer(msg)

    def _submit(self, event: dict) -> None:
        name, row, text = event.get("input"), event.get("row"), str(event.get("text") or "")
        if row is None or row == flux_bar.TYPED:
            kind, url = places.classify(text)
            if not kind:
                return
        else:
            url = row
        log.info("address picked: %s", url)
        self._load(WHERE.get(name, "here"), url)

    def _act(self, event: dict) -> None:
        if event.get("action") != flux_bar.REMOVE or not isinstance(event.get("row"), str):
            return
        url = event["row"]
        self.command("bookmark", op="remove", url=url)
        mode = self.modes.get(event.get("input"))
        if mode is not None:
            from ..services.modes import Entry
            mode.remove(Entry("", glyph=BOOKMARK, value=url))

    def _pick(self, event: dict) -> None:
        row = event.get("row")
        if row in (flux_bar.ADD_ROW, flux_bar.REMOVE_ROW):
            self._bookmark("add" if row == flux_bar.ADD_ROW else "remove")
        elif isinstance(row, str) and row.startswith(flux_bar.RESUME):
            ids = [t[protocol.TAB_ID] for t in self.state[protocol.TABS]]
            try:
                index = ids.index(int(row[len(flux_bar.RESUME):]))
            except ValueError:
                return
            self._resume(index)

    # ── to the browser ──────────────────────────────────────────────────
    def command(self, name: str, **args) -> None:
        """Ask the browser window to do something: protocol.COMMANDS."""
        self.send(protocol.command(name, **args))

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

    # ── teardown ────────────────────────────────────────────────────────
    def close(self) -> None:
        if self.closed:
            return
        super().close()
        for mode in self.modes.values():
            mode.close()
        self.modes.clear()
