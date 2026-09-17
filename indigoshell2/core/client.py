import argparse
import json
import socket
import sys

from .naming import CLI
from .paths import socket_path

VERBS = {"ping", "reload", "kill", "list", "open", "close", "toggle", "menu"}


def _fail(message: str, code: int) -> None:
    # Client-side diagnostics go straight to stderr, not through logging:
    # this is a one-shot CLI talking to a user, not the daemon.
    print(f"{CLI}: {message}", file=sys.stderr)
    sys.exit(code)


def _send(verb: str, **args) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(socket_path())
    except (FileNotFoundError, ConnectionRefusedError):
        _fail("daemon is not running", 2)
    s.sendall(json.dumps({"verb": verb, "args": args}).encode())
    # Half-close so the server's read-to-EOF terminates. The request
    # framing is "one request per connection, sender closes write side".
    s.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    s.close()
    return json.loads(b"".join(chunks).decode() or "{}")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog=CLI)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("ping", help="check the daemon is alive")
    sub.add_parser("reload", help="restart the daemon in place")
    sub.add_parser("kill", help="shut the daemon down")
    sub.add_parser("list", help="list window kinds and open instances")
    for v in ("open", "close", "toggle"):
        p = sub.add_parser(v)
        p.add_argument("name")
    sub.add_parser("menu", help="open a plugin menu (power, audio, ...)") \
        .add_argument("name")
    args = parser.parse_args(argv)

    kwargs = {}
    if args.verb in ("open", "close", "toggle", "menu"):
        kwargs["name"] = args.name
    resp = _send(args.verb, **kwargs)

    if not resp.get("ok"):
        _fail(resp.get("error", "unknown error"), 1)
    data = resp.get("data")
    if data is None:
        return
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    else:
        print(data)
