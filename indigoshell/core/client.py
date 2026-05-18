import argparse
import json
import socket
import sys

from .paths import socket_path

VERBS = {"ping", "reload", "list", "open", "close", "toggle"}


def _send(verb: str, **args) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(socket_path())
    except (FileNotFoundError, ConnectionRefusedError):
        print("indigo: daemon is not running", file=sys.stderr)
        sys.exit(2)
    s.sendall(json.dumps({"verb": verb, "args": args}).encode())
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
    parser = argparse.ArgumentParser(prog="indigo")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("ping")
    sub.add_parser("reload")
    sub.add_parser("list")
    for v in ("open", "close", "toggle"):
        p = sub.add_parser(v)
        p.add_argument("name")
    args = parser.parse_args(argv)

    kwargs = {}
    if args.verb in ("open", "close", "toggle"):
        kwargs["name"] = args.name
    resp = _send(args.verb, **kwargs)

    if not resp.get("ok"):
        print(f"indigo: {resp.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)
    data = resp.get("data")
    if data is None:
        return
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    else:
        print(data)
