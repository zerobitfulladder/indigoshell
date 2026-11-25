"""A browser window's line to the shell.

The window says it is a Flux window and its own id for it (`announce`,
the app bar protocol: indigoshell/applink/PROTOCOL.md; the connection is
the window), whether it has the keyboard focus (`active`, sent after the
announce and whenever it changes: how the shell knows the focused
window), then sends its section of the top bar whole whenever it differs
from the last (`push`, a `bar`), and whatever else it has to say
(`send`: an `answer`, an `open`, forwarded `keys`). The shell answers
that it has the window (`attached`, with the agent's name and colour,
which Flux adopts: indigoshell/core/persona.py; from then on the window
catches its bar's chords itself) and sends the bar's events (a tab
clicked, an input's query, a submit, a pick, `closed`, `fallback`),
handed on as the `event` signal.

The pages' icons go apart from the bar (PROTOCOL.md, "Images"): `push`
is given the PNGs its tabs name, by id, and before a bar goes, each it
names that this connection's shell does not hold goes as an `image`.
The shell holds what the last bar named (and what came since), so after
each bar the connection counts as holding just what that bar names
(`held`): an icon goes once, and again only after a bar went without it,
or on a new connection.

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
PLACE_WAIT_S and then shows anyway: a shell restarting, or one too old
to answer. With no shell listening at all (a bare compositor, as cage
on a machine that lends the agent its Flux) a connection that fails
answers None at once: nothing waits for a shell that is not there. The
shell answers at once and moves the window to that workspace once it
maps.
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

    def __init__(self, window_id: int, parent=None) -> None:
        super().__init__(parent)
        self.window_id = window_id  # Flux's own id for the window (control.py's)
        self._place: dict | None = None     # a `place` not yet answered
        self.bar: dict | None = None        # the latest bar, sent or not
        self.images: dict[str, bytes] = {}  # ... the PNGs its tabs name, by id
        self._sent: dict | None = None      # what this connection last carried
        self.held: set = set()              # the images the shell holds on it
        self.sent_images = 0                # for the tests: images actually written
        self.active = False                 # the window has the keyboard focus
        self._active_sent: bool | None = None   # ... as this connection last said it
        self.attached = False
        self.fallback = False               # no shell shows the window's state
        self.restarting = False             # the shell said it will be back
        self.sent_bars = 0                  # for the tests: bars actually written
        self._started = self._closing = False
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
        if not self._started:
            self._started = True
            self._update_fallback()
            self._connect()

    def close(self) -> None:
        self._closing = True
        self.retry.stop()
        self.away.stop()
        self.sock.abort()

    def push(self, bar: dict, images: dict[str, bytes]) -> None:
        """The window's bar now, and the PNGs its tabs name by id; sent
        only if it differs from the last."""
        self.bar, self.images = bar, images
        if self.connected() and bar != self._sent:
            self._send_bar()

    def set_active(self, active: bool) -> None:
        """Whether the window (or a dialog or popup of it) has the keyboard
        focus now; said to the shell when it changes."""
        self.active = bool(active)
        if self.connected() and self._active_sent is not self.active:
            self._send_active()

    def _send_active(self) -> None:
        self._active_sent = self.active
        self._send(B.active(self.active))

    def send(self, msg: dict) -> None:
        """A one-off message (an answer, an input to open)."""
        self._send(msg)

    def place(self, with_window: int | None = None) -> None:
        """Ask the shell to put the window, not yet shown, on a workspace
        (the one Flux's window `with_window` is on, else an empty one):
        `placed` answers. Starts the line when it has not started (the
        window is not shown yet)."""
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
            self.held = set()
            self._active_sent = None
            self._set_attached(False)
            self._again()
            if not self._closing:
                if self._place is not None:
                    # Nobody listens: no shell to place the window.
                    self._place = None
                    log.info("no shell listens: the window shows where the compositor "
                             "puts it")
                    self.placed.emit(None)
                self.down.emit()

    def _on_connected(self) -> None:
        log.debug("connected to the shell")
        self._sent = None
        self.held = set()
        self._send(B.announce(protocol.KIND, self.window_id))
        self._send_active()
        if self._place is not None:
            self._send(self._place)
        if self.bar is not None:
            self._send_bar()

    def _send_bar(self) -> None:
        named = B.image_ids(self.bar)
        held = named & self.held
        for image_id in sorted(named - self.held):
            png = self.images.get(image_id)
            if png is not None:
                self.sent_images += 1
                self._send(B.image(image_id, png))
                held.add(image_id)
        self.held = held
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
            log.info("%s", "no shell shows the window's state" if value
                     else "the shell shows the window's state")
            self.fallback_changed.emit(value)
