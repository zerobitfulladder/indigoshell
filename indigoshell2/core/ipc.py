"""Control socket. Line-delimited JSON over AF_UNIX, one request per
connection, client half-closes to mark end-of-request.

  Request:  {"verb": "...", "args": {...}}
  Response: {"ok": true, "data": ...}  |  {"ok": false, "error": "..."}

Wire-compatible with v1's client on purpose, so the same muscle memory
(and any scripts) work against either daemon.
"""

import asyncio
import json
import logging
import os
import socket
from typing import TYPE_CHECKING, Callable

from .paths import socket_path

if TYPE_CHECKING:
    from .daemon import Daemon

log = logging.getLogger(__name__)

# A control request is a short JSON object; anything larger is a client
# bug or someone poking the socket.
MAX_REQUEST = 64 * 1024
READ_TIMEOUT = 5.0


class IPCServer:
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.path = socket_path()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        # A leftover socket file means a previous daemon died without
        # cleanup. We already hold the singleton lock at this point, so
        # nothing live owns it — unlinking is safe.
        if os.path.exists(self.path):
            os.unlink(self.path)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # umask around bind() rather than chmod() after it: chmod leaves
        # a window where the socket is world-writable.
        old_umask = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old_umask)
        sock.listen(8)
        sock.setblocking(False)
        self._server = await asyncio.start_unix_server(self._on_client, sock=sock)
        log.info("listening on %s", self.path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self.path)
        except OSError:
            pass
        log.debug("control socket closed")

    # ── connection handling ─────────────────────────────────────────────
    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        after: Callable[[], None] | None = None
        try:
            try:
                raw = await asyncio.wait_for(reader.read(-1), READ_TIMEOUT)
            except asyncio.TimeoutError:
                resp = {"ok": False, "error": "request timed out"}
            else:
                if len(raw) > MAX_REQUEST:
                    resp = {"ok": False, "error": "request too large"}
                else:
                    try:
                        req = json.loads(raw.decode())
                        log.debug("request %r", req)
                        resp, after = self._dispatch(req)
                    except Exception as e:
                        log.warning("request failed: %s: %s", type(e).__name__, e)
                        resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            writer.write((json.dumps(resp) + "\n").encode())
            try:
                await writer.drain()
            except OSError:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

        # Deferred until the response is on the wire and the connection
        # is closed — `reload` and `kill` tear down this very server, so
        # running them inline would drop the reply the client is waiting
        # for.
        if after is not None:
            after()

    # ── verbs ───────────────────────────────────────────────────────────
    def _dispatch(self, req: dict) -> tuple[dict, Callable[[], None] | None]:
        verb = req.get("verb")
        args = req.get("args") or {}
        d = self.daemon

        if verb == "ping":
            return {"ok": True, "data": "pong"}, None
        if verb == "reload":
            return {"ok": True}, d.reload
        if verb == "kill":
            return {"ok": True}, d.quit
        if verb == "list":
            return {"ok": True, "data": {
                "instances": d.list_instances(),
                "kinds": d.list_kinds(),
                "menus": d.list_menus(),
            }}, None
        if verb == "open":
            d.open(args["name"], args.get("params"))
            return {"ok": True}, None
        if verb == "menu":
            d.menu(args["name"])
            return {"ok": True}, None
        if verb == "close":
            return {"ok": True, "data": {"closed": d.close(args["name"])}}, None
        if verb == "toggle":
            return {"ok": True, "data": {"state": d.toggle(
                args["name"], args.get("params"))}}, None
        return {"ok": False, "error": f"unknown verb: {verb}"}, None
