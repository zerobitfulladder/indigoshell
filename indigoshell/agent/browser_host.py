"""The native messaging host for the indigoshell bridge extension.

Firefox starts this when the extension connects, and talks to it over
stdin/stdout: each message a 4-byte native-order length, then that many
bytes of JSON. It relays both ways to the shell's socket
($XDG_RUNTIME_DIR/indigoshell-firefox.sock), one JSON object per line.

It never exits on its own while Firefox keeps it: if the shell is not
running yet, or restarts, it reconnects every few seconds, so the
extension's side of the link stays up throughout. Standard library only
— Firefox runs it with whatever environment it has.
"""

import json
import os
import socket
import struct
import sys
import threading
import time

SOCK = os.environ.get("INDIGOSHELL_FIREFOX_SOCK") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "indigoshell-firefox.sock")
RETRY_S = 2.0

_out = threading.Lock()
_conn: socket.socket | None = None
_conn_lock = threading.Lock()


def to_firefox(data: bytes) -> None:
    with _out:
        sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
        sys.stdout.buffer.flush()


def from_firefox() -> None:
    """Firefox -> shell. Ends the process when Firefox lets go."""
    stdin = sys.stdin.buffer
    while True:
        head = stdin.read(4)
        if len(head) < 4:
            os._exit(0)
        data = stdin.read(struct.unpack("=I", head)[0])
        with _conn_lock:
            conn = _conn
        if conn is None:
            continue            # no shell: nothing asked, nothing to answer
        try:
            conn.sendall(data.replace(b"\n", b" ") + b"\n")
        except OSError:
            pass


def to_shell() -> None:
    """Shell -> Firefox, reconnecting whenever the shell goes away."""
    global _conn
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCK)
        except OSError:
            time.sleep(RETRY_S)
            continue
        with _conn_lock:
            _conn = s
        try:
            for line in s.makefile("rb"):
                line = line.strip()
                if line:
                    json.loads(line)        # only well-formed JSON passes
                    to_firefox(line)
        except (OSError, ValueError):
            pass
        finally:
            with _conn_lock:
                _conn = None
            s.close()
        time.sleep(RETRY_S / 4)


def main() -> None:
    threading.Thread(target=to_shell, daemon=True).start()
    from_firefox()


if __name__ == "__main__":
    main()
