import json
import os
import socket
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from .paths import socket_path

if TYPE_CHECKING:
    from .daemon import Daemon


class IPCServer:
    """Line-delimited JSON over a Unix socket. One request, one response, close.

    Request:  {"verb": "...", "args": {...}}
    Response: {"ok": true, "data": ...}  or  {"ok": false, "error": "..."}
    """

    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.path = socket_path()
        self._sock: socket.socket | None = None
        self._accept_src: int | None = None
        self._client_srcs: set[int] = set()

    def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(8)
        s.setblocking(False)
        self._sock = s
        self._accept_src = GLib.io_add_watch(s.fileno(), GLib.IO_IN, self._on_accept)

    def stop(self) -> None:
        if self._accept_src is not None:
            GLib.source_remove(self._accept_src)
            self._accept_src = None
        for src in list(self._client_srcs):
            GLib.source_remove(src)
        self._client_srcs.clear()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    # ── socket plumbing ──────────────────────────────────────────────────
    def _on_accept(self, _fd: int, _cond: int) -> bool:
        assert self._sock is not None
        try:
            client, _ = self._sock.accept()
        except BlockingIOError:
            return True
        client.setblocking(False)
        src = GLib.io_add_watch(
            client.fileno(),
            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            lambda f, c: self._on_client(f, c, client),
        )
        self._client_srcs.add(src)
        return True

    def _on_client(self, _fd: int, cond: int, client: socket.socket) -> bool:
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            client.close()
            return False
        try:
            data = client.recv(65536)
        except BlockingIOError:
            return True
        if not data:
            client.close()
            return False

        try:
            req = json.loads(data.decode())
            resp = self._dispatch(req)
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        try:
            client.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            client.close()
        return False

    # ── verbs ────────────────────────────────────────────────────────────
    def _dispatch(self, req: dict) -> dict:
        verb = req.get("verb")
        args = req.get("args") or {}
        d = self.daemon

        if verb == "ping":
            return {"ok": True, "data": "pong"}
        if verb == "reload":
            GLib.idle_add(d.reload)
            return {"ok": True}
        if verb == "list":
            return {"ok": True, "data": {"instances": d.list_instances(), "kinds": d.list_kinds()}}
        if verb == "open":
            d.open(args["name"], args.get("params"))
            return {"ok": True}
        if verb == "close":
            ok = d.close(args["name"])
            return {"ok": True, "data": {"closed": ok}}
        if verb == "toggle":
            state = d.toggle(args["name"], args.get("params"))
            return {"ok": True, "data": {"state": state}}
        return {"ok": False, "error": f"unknown verb: {verb}"}
