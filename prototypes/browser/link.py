"""A browser window's line to the shell's strip (strip.py).

The window says which X window it is and how tall a band it left empty,
then pushes what the strip shows: address, title, load progress, history.
The strip answers that it is drawn ({"attached": ...}) and sends the
commands its targets were clicked for ({"cmd": "back"}).

Either side may start first or restart at any time. With no strip
listening this tries again every couple of seconds, quietly, and the
window goes on showing its own band text; when a strip appears it is
told everything again from the start.
"""

import json
import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket

from indigoshell.core.naming import APP, env
from indigoshell.core.paths import runtime_dir

RETRY_MS = 2000
SOCKET_ENV = env("STRIP_SOCKET")


def socket_path() -> str:
    """Where the strip listens. strip.py has the same rule."""
    return os.environ.get(SOCKET_ENV) or os.path.join(
        runtime_dir(), f"{APP}-strip-{os.getuid()}.sock")


class StripLink(QObject):
    attached_changed = Signal(bool)
    command = Signal(str)

    def __init__(self, wid_of, band: int, parent=None) -> None:
        super().__init__(parent)
        self.wid_of = wid_of        # asked at connect time, once the window exists
        self.band = band
        self.state: dict = {}
        self.attached = False
        self._started = self._closing = False
        self.sock = QLocalSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.readyRead.connect(self._on_ready_read)
        # Not errorOccurred: a refused connect reports its error while the
        # socket still says Connecting, and only then drops to Unconnected.
        self.sock.stateChanged.connect(self._on_state)
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.setInterval(RETRY_MS)
        self.retry.timeout.connect(self._connect)

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._connect()

    def close(self) -> None:
        self._closing = True
        self.retry.stop()
        self.sock.abort()

    def push(self, **state) -> None:
        """Send whatever changed since the last push."""
        changed = {k: v for k, v in state.items() if self.state.get(k) != v}
        if changed:
            self.state.update(changed)
            self._send(changed)

    def set_band(self, px: int) -> None:
        if px != self.band:
            self.band = px
            self._send({"band": px})

    # ── socket ──────────────────────────────────────────────────────────
    def _connect(self) -> None:
        if self._closing or self.sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self.sock.connectToServer(socket_path())

    def _again(self) -> None:
        if not self._closing and not self.retry.isActive():
            self.retry.start()

    def _on_state(self, state) -> None:
        # Nobody listening, or the strip went away: try again later.
        if state == QLocalSocket.LocalSocketState.UnconnectedState:
            self._set_attached(False)
            self._again()

    def _on_connected(self) -> None:
        self._send({"wid": int(self.wid_of()), "band": self.band, **self.state})

    def _on_ready_read(self) -> None:
        while self.sock.canReadLine():
            try:
                msg = json.loads(bytes(self.sock.readLine()).decode(errors="replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "attached" in msg:
                self._set_attached(True)
            if "cmd" in msg:
                self.command.emit(str(msg["cmd"]))

    def _send(self, msg: dict) -> None:
        if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self.sock.write((json.dumps(msg) + "\n").encode())
            self.sock.flush()

    def _set_attached(self, value: bool) -> None:
        if value != self.attached:
            self.attached = value
            self.attached_changed.emit(value)
