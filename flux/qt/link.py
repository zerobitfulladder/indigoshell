"""A browser window's line to the shell.

The window says it is a Flux window, its own id for it and its X window
(`announce`, the app bar protocol: indigoshell/applink/PROTOCOL.md),
then sends its section of the top bar whole whenever it differs from
the last (`push`, a `bar`), and whatever else it has to say (`send`:
an `answer`, an `open`). The shell answers that it has the window
(`attached`, with the agent's name and colour, which Flux adopts:
indigoshell/core/persona.py) and sends the bar's events (a tab
clicked, an input's query, a submit, a pick, `closed`, `fallback`),
handed on as the `event` signal.

Who shows the window's state. The shell does, in its top bar, while the
window has focus and the shell has said `attached`; with no shell the
window has to say it itself (`fallback`). No shell counts only after
FALLBACK_MS without one: a shell reload is back well within that, and
the line coming and going would make the page jump for it. A shell that
said it is restarting gets RESTART_MS instead, should its start be slow.

Either side may start first or restart at any time. With no shell
listening this tries again every half second, quietly, until the
fallback shows, and every couple of seconds after; when a shell appears
it is told everything again from the start: the announce, then the bar
whole, without waiting for `attached`.

Placing (the agent's window, and a popup of it: window.py). Before the
window is first shown its link starts on its own, and `place` asks the
shell to put the window on a workspace (applink `place`, sent right
after the announce, on whichever connection comes first; `with` names
another window of Flux's by its id); `placed` carries the answer, a
workspace index or None. The window waits for it at most applink's
PLACE_WAIT_S and then shows anyway: no shell, a shell restarting, or one
too old to answer.

Off (`off`: a headless browser, protocol.py "Headless"): no line at all.
It never connects, never says the window's state itself (`fallback`
stays false: no line over the page, which would move it), and is never
asked to place a window (window.py shows it at once).
"""

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket

from indigoshell.applink import bar as B
from indigoshell.applink import protocol as link_protocol
from indigoshell.core import persona
from flux import protocol

log = logging.getLogger(__name__)

RETRY_MS = 500                  # while the shell may be just restarting
RETRY_SLOW_MS = 2000            # once the window says its state itself
FALLBACK_MS = 3000              # the shell away this long: the window says it
RESTART_MS = 10000              # ... after it said it is restarting


class ShellLink(QObject):
    attached_changed = Signal(bool)
    fallback_changed = Signal(bool)
    event = Signal(dict)        # the bar's events, from the shell
    down = Signal()             # a connection attempt failed, or the line dropped
    placed = Signal(object)     # the answer to `place`: a workspace index, or None

    def __init__(self, wid_of, window_id: int, parent=None, off: bool = False) -> None:
        super().__init__(parent)
        self.off = off              # headless: no line to a shell, ever
        self.wid_of = wid_of        # asked at connect time, once the window exists
        self.window_id = window_id  # Flux's own id for the window (control.py's)
        self._place: dict | None = None     # a `place` not yet answered
        self.wid: int | None = None         # the X window last announced
        self.bar: dict | None = None        # the latest bar, sent or not
        self._sent: dict | None = None      # what this connection last carried
        self.attached = False
        self.fallback = False               # the window shows its own state
        self.restarting = False             # the shell said it will be back
        self.sent_bars = 0                  # for the tests: bars actually written
        self._started = self._closing = False
        self._moving = False                # dropping the line to announce a new window
        self.sock = QLocalSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.readyRead.connect(self._on_ready_read)
        # Not errorOccurred: a refused connect reports its error while the
        # socket still says Connecting, and only then drops to Unconnected.
        self.sock.stateChanged.connect(self._on_state)
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self._connect)
        self.away = QTimer(self)
        self.away.setSingleShot(True)
        self.away.setInterval(FALLBACK_MS)
        self.away.timeout.connect(self._away_too_long)

    def start(self) -> None:
        if not self._started and not self.off:
            self._started = True
            self._update_fallback()
            self._connect()

    def close(self) -> None:
        self._closing = True
        self.retry.stop()
        self.away.stop()
        self.sock.abort()

    def push(self, bar: dict) -> None:
        """The window's bar now; sent only if it differs from the last."""
        self.bar = bar
        if self.connected() and bar != self._sent:
            self._send_bar()

    def follow(self, wid: int) -> None:
        """The window's X window is `wid` now. If the shell was told
        another, start over on a new connection at once (not at the next
        retry), so it moves to this one."""
        if self.wid is None or wid == self.wid or not self._started or self._closing:
            return
        log.debug("X window 0x%x replaced by 0x%x: announcing again", self.wid, wid)
        self.wid = None
        self.retry.stop()
        self._moving = True
        self.sock.abort()
        self._moving = False
        self._connect()

    def send(self, msg: dict) -> None:
        """A one-off message (an answer, an input to open)."""
        self._send(msg)

    def place(self, with_window: int | None = None) -> None:
        """Ask the shell to put the window, not yet shown, on a workspace
        (the one Flux's window `with_window` is on, else an empty one):
        `placed` answers. Starts the line when it has not started (the
        window is not shown yet)."""
        if self.off:
            return
        self._place = link_protocol.place(with_window)
        if self.connected():
            self._send(self._place)
        else:
            self.start()            # sent once connected, after the announce

    def placing_over(self) -> None:
        """The window is shown, answered or not: a shell that comes later
        is not asked to place it."""
        self._place = None

    def connected(self) -> bool:
        return self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState

    def coming(self) -> bool:
        """Connecting or connected, and not yet told whether it is
        attached: what the shell will say is a moment away."""
        S = QLocalSocket.LocalSocketState
        return not self.attached and self.sock.state() in (S.ConnectingState,
                                                           S.ConnectedState)

    # ── socket ──────────────────────────────────────────────────────────
    def _connect(self) -> None:
        if self._closing or self.sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self.sock.connectToServer(protocol.socket_path())

    def _again(self) -> None:
        if not self._closing and not self.retry.isActive():
            self.retry.start(RETRY_SLOW_MS if self.fallback else RETRY_MS)

    def _on_state(self, state) -> None:
        # Nobody listening, or the shell went away: try again soon.
        if state == QLocalSocket.LocalSocketState.UnconnectedState:
            self._sent = None
            self._set_attached(False)
            self._again()
            if not (self._closing or self._moving):
                self.down.emit()

    def _on_connected(self) -> None:
        log.debug("connected to the shell")
        self._sent = None
        self.wid = int(self.wid_of())
        self._send(B.announce(protocol.KIND, self.window_id, self.wid))
        if self._place is not None:
            self._send(self._place)
        if self.bar is not None:
            self._send_bar()

    def _send_bar(self) -> None:
        self._sent = self.bar
        self.sent_bars += 1
        self._send(self.bar)

    def _on_ready_read(self) -> None:
        while self.sock.canReadLine():
            msg = protocol.decode(bytes(self.sock.readLine()).decode(errors="replace"))
            if msg is None:
                log.debug("a line from the shell that is no message, ignored")
                continue
            kind = msg[protocol.TYPE]
            if kind == protocol.ATTACHED:
                # Her name and colour, as the shell knows them from the
                # hub: the marks and labels every window draws.
                if persona.adopt(msg.get("agent")):
                    log.info("the agent is %s in %s, as the shell says", persona.name(),
                             persona.color())
                self._set_attached(True)
            elif kind == protocol.RESTARTING:
                log.info("the shell is restarting; waiting up to %d s for it",
                         RESTART_MS // 1000)
                self.restarting = True
            elif kind == link_protocol.PLACED:
                if self._place is not None:
                    self._place = None
                    desktop = msg.get(link_protocol.DESKTOP)
                    if desktop is None:
                        log.info("the shell did not place the window: %s", msg.get("why") or "")
                    self.placed.emit(desktop if protocol.is_id(desktop) else None)
            elif kind in B.EVENTS:
                self.event.emit(msg)
            else:
                log.debug("message from the shell not understood: %r", kind)

    def _send(self, msg: dict) -> None:
        if self.connected():
            self.sock.write(protocol.encode(msg))
            self.sock.flush()

    def _set_attached(self, value: bool) -> None:
        if value != self.attached:
            self.attached = value
            log.debug("shell %s", "attached" if value else "detached")
            self.attached_changed.emit(value)
        self._update_fallback()

    def _update_fallback(self) -> None:
        """Whether the window says its state itself: see the module
        docstring. Called on every change of the line."""
        if self._closing:
            return
        if self.attached:
            self.away.stop()
            self.restarting = False
            self._set_fallback(False)
        elif not self.fallback and not self.away.isActive():
            self.away.start(RESTART_MS if self.restarting else FALLBACK_MS)

    def _away_too_long(self) -> None:
        if not self.attached and not self._closing:
            self.restarting = False
            self._set_fallback(True)

    def _set_fallback(self, value: bool) -> None:
        if value != self.fallback:
            self.fallback = value
            log.info("%s", "the window shows its own state (no shell shows it)" if value
                     else "the shell shows the window's state")
            self.fallback_changed.emit(value)
