"""Files on the user's phone, for the call's agent.

The phone runs MiXplorer, whose Servers menu can share its storage over
SFTP, FTP(S), HTTP or WebDAV. This uses SFTP: one port, everything
encrypted (the password too, which FTP and plain HTTP send in the
clear), and the server proves who it is with its host key, so files
never go to some other device that took the phone's address.

**Setting up** — once, by the user, in a terminal, so the password
never passes through the agent:

    ~/Projects/indigoshell/.venv/bin/python -m indigoshell.agent.phone setup

It asks for the address MiXplorer shows when its SFTP server starts
(sftp://192.168.1.42:2222) and the user and password set there,
connects, and pins the server's key. All of it goes to
`~/.config/indigoshell/phone.json` (0600):

    {"host": "192.168.1.42", "port": 2222, "user": "me",
     "password": "...", "host_key": "ssh-ed25519 AAAA..."}

**Finding it.** The server runs only while it is switched on in
MiXplorer, and the phone's address can change (DHCP). Each connection
tries the last address that answered (state key `agent.phone`), then
the configured one, then every host on the local subnets with the port
open; only a server holding the pinned key counts.

**Paths** are the server's: relative ones start in the folder it opens
in (MiXplorer's server root, usually internal storage), and listings
give paths that way so they can be handed straight back.

**Out of process.** SSH spends Python time on every packet; a large
video moved on the shell's one loop would hitch the bar. Each operation
runs in a child (`python -m indigoshell.agent.phone do`: a JSON request
on stdin, a JSON answer on stdout) that reads the password from
phone.json itself. Stopping the agent kills it.
"""

import asyncio
import fnmatch
import glob
import json
import logging
import os
import posixpath
import stat
import sys
import time

from ..core import paths, state
from .watch import Reporter

log = logging.getLogger(__name__)

STATE_KEY = "agent.phone"
CONNECT_S = 5.0
LOGIN_S = 15.0
KNOWN_PROBE_S = 1.5         # a TCP connect to an address it had before
PROBE_S = 1.0               # ... to any LAN host (a dozing phone is slow)
PROBE_PARALLEL = 128
LIST_DEFAULT = 60
LIST_MAX = 400
DEPTH_MAX = 5
WALK_MAX = 20000            # entries looked at by one listing
SKIP_WALK = {"Android"}     # app data: huge, and mostly unreadable
DROP_DIRS = ("Download", "/storage/emulated/0/Download", "/sdcard/Download")

SET_UP = ("~/Projects/indigoshell/.venv/bin/python -m "
          "indigoshell.agent.phone setup")
NOT_SET_UP = ("The phone isn't set up yet. The user starts the SFTP server "
              "in MiXplorer (its Servers menu) and runs, once, in a "
              f"terminal: {SET_UP}")
NOT_FOUND = ("The phone didn't answer — its SFTP server is probably off. "
             "Ask the user to start it in MiXplorer (Servers menu); the "
             "phone and this computer must be on the same Wi-Fi.")


class PhoneError(Exception):
    pass


def config_path() -> str:
    return os.path.join(paths.config_dir(), "phone.json")


def _asyncssh():
    try:
        import asyncssh
    except ImportError:
        raise PhoneError(
            "asyncssh is missing — in ~/Projects/indigoshell: uv pip "
            "install --python .venv/bin/python asyncssh") from None
    return asyncssh


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def parse_address(text: str) -> tuple[str, int | None, str | None]:
    """Host, port and user from what MiXplorer shows
    (sftp://user@192.168.1.42:2222/), or a bare host[:port]."""
    from urllib.parse import urlsplit
    text = text.strip()
    if "://" not in text:
        text = "sftp://" + text
    u = urlsplit(text)
    if not u.hostname:
        raise PhoneError(f"not an address: {text!r}")
    return u.hostname, u.port, u.username


# ── the child: talks SSH ────────────────────────────────────────────────

def _read_config(path: str) -> dict:
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise PhoneError(NOT_SET_UP) from None
    except (OSError, ValueError) as e:
        raise PhoneError(f"{path}: {e}") from None
    missing = [k for k in ("host", "port", "user", "host_key")
               if not cfg.get(k)]
    if missing:
        raise PhoneError(f"{path} lacks {', '.join(missing)} — run "
                         f"setup again: {SET_UP}")
    return cfg


def _options(cfg: dict, *, pinned: bool = True) -> dict:
    ssh = _asyncssh()
    opts = dict(port=int(cfg["port"]), username=cfg["user"],
                connect_timeout=CONNECT_S, login_timeout=LOGIN_S,
                known_hosts=None)
    if pinned:
        key = ssh.import_public_key(cfg["host_key"])
        opts["known_hosts"] = ([key], [], [])
    if cfg.get("password"):
        # The password only: no SSH keys or agent offered first, which a
        # small server may count as failed attempts.
        opts.update(password=cfg["password"], client_keys=None,
                    agent_path=None)
    return opts


async def _reachable(hosts: list[str], port: int, wait: float) -> list[str]:
    """The hosts, in order, that take a TCP connection on port."""
    gate = asyncio.Semaphore(PROBE_PARALLEL)

    async def probe(host):
        async with gate:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), wait)
            except (OSError, asyncio.TimeoutError):
                return None
            w.close()
            return host

    return [h for h in await asyncio.gather(*map(probe, hosts)) if h]


async def _connect(cfg: dict, remembered: list[str]):
    """(connection, host) to the phone, wherever it is now."""
    ssh = _asyncssh()
    opts = _options(cfg)
    stranger = []

    async def attempt(host):
        try:
            return await ssh.connect(host, **opts)
        except ssh.HostKeyNotVerifiable:
            stranger.append(host)
        except ssh.PermissionDenied:
            raise PhoneError(
                "The phone turned down the user name or password. If they "
                f"changed in MiXplorer, the user runs setup again: {SET_UP}"
            ) from None
        except (OSError, asyncio.TimeoutError, ssh.Error) as e:
            log.debug("phone: %s: %s", host, e)
        return None

    # The addresses it had first, all at once, so a stale one costs a
    # short probe rather than an SSH timeout; then the whole LAN.
    port = int(cfg["port"])
    known = list(dict.fromkeys([*remembered, cfg["host"]]))
    for host in await _reachable(known, port, KNOWN_PROBE_S):
        conn = await attempt(host)
        if conn:
            return conn, host
    from .wiz import _networks
    lan = [h for h in _networks()[1] if h not in known]
    for host in await _reachable(lan, port, PROBE_S):
        conn = await attempt(host)
        if conn:
            return conn, host
    if stranger:
        raise PhoneError(
            f"Something answers at {', '.join(stranger)} on port "
            f"{cfg['port']}, but with a different key than the phone's. If "
            "MiXplorer's server was reset or reinstalled, the user runs "
            f"setup again: {SET_UP}")
    raise PhoneError(NOT_FOUND)


def _kind(attrs) -> str:
    if attrs.permissions is not None:
        fmt = stat.S_IFMT(attrs.permissions)
        return {stat.S_IFDIR: "d", stat.S_IFLNK: "l"}.get(fmt, "f")
    return {2: "d", 3: "l"}.get(attrs.type, "f")


class _Remote:
    """One SFTP session, with the folder the server opens in."""

    def __init__(self, sftp, home: str) -> None:
        self.sftp = sftp
        self.home = home

    def resolve(self, path: str) -> str:
        path = (path or "").strip()
        if path in ("", ".", "~"):
            return self.home
        if path.startswith("~/"):
            path = path[2:]
        full = path if path.startswith("/") else posixpath.join(self.home,
                                                                path)
        return posixpath.normpath(full).replace("//", "/")

    def shown(self, path: str) -> str:
        """A path as listings give it: relative to home when inside it."""
        if self.home != "/" and path.startswith(self.home + "/"):
            return path[len(self.home) + 1:]
        return path

    async def list(self, path="", pattern="", depth=0, newest=False,
                   limit=LIST_DEFAULT) -> dict:
        ssh = _asyncssh()
        base = self.resolve(path)
        depth = max(0, min(int(depth or 0), DEPTH_MAX))
        pat = (pattern or "").lower()
        rows, unreadable, seen, cut = [], [], 0, False
        queue = [(base, 0)]
        while queue and not cut:
            folder, level = queue.pop(0)
            try:
                names = [n async for n in self.sftp.scandir(folder)]
            except ssh.SFTPError as e:
                if folder == base:
                    raise PhoneError(f"{self.shown(folder)}: {e.reason}")
                unreadable.append(self.shown(folder))
                continue
            for n in names:
                if n.filename in (".", ".."):
                    continue
                seen += 1
                if seen > WALK_MAX:
                    cut = True
                    break
                full = posixpath.join(folder, n.filename)
                kind = _kind(n.attrs)
                if not pat or fnmatch.fnmatch(n.filename.lower(), pat):
                    rows.append([self.shown(full), n.attrs.size or 0,
                                 n.attrs.mtime or 0, kind])
                if (kind == "d" and level < depth
                        and n.filename not in SKIP_WALK):
                    queue.append((full, level + 1))
        if newest:
            rows.sort(key=lambda r: -r[2])
        else:
            rows.sort(key=lambda r: (r[3] != "d", r[0].casefold()))
        limit = max(1, min(int(limit or LIST_DEFAULT), LIST_MAX))
        return {"base": base, "home": self.home, "total": len(rows),
                "rows": rows[:limit], "walk_cut": cut,
                "unreadable": unreadable[:10]}

    async def _matches(self, path: str) -> list[str]:
        ssh = _asyncssh()
        full = self.resolve(path)
        if glob.has_magic(full):
            try:
                return list(await self.sftp.glob(full))
            except ssh.SFTPNoSuchFile:
                return []
        return [full] if await self.sftp.exists(full) else []

    async def pull(self, paths: list[str], to: str,
                   overwrite: bool = False) -> dict:
        os.makedirs(to, exist_ok=True)
        items, missing = [], []
        for p in paths:
            found = await self._matches(p)
            if not found:
                missing.append(p)
            for src in found:
                is_dir = _kind(await self.sftp.stat(src)) == "d"
                name = posixpath.basename(src.rstrip("/")) or "phone"
                local = os.path.join(to, name)
                if not overwrite:
                    local = _unique(local)
                # An existing folder as the target means "into it", so an
                # overwrite names the parent and lets the copy add the name.
                await self.sftp.get(src, to if overwrite else local,
                                    recurse=is_dir, preserve=True,
                                    progress_handler=_progress())
                _tidy(local)
                items.append([self.shown(src), local, _local_size(local),
                              is_dir])
        return {"to": to, "items": items, "missing": missing}

    async def _drop_dir(self) -> str:
        for d in DROP_DIRS:
            full = self.resolve(d)
            if await self.sftp.isdir(full):
                return full
        return self.home

    async def push(self, paths: list[str], to: str = "",
                   overwrite: bool = False) -> dict:
        folder = self.resolve(to) if to else await self._drop_dir()
        await self.sftp.makedirs(folder, exist_ok=True)
        items, there, missing = [], [], []
        for local in paths:
            if not os.path.exists(local):
                missing.append(local)
                continue
            name = os.path.basename(local.rstrip("/"))
            dst = posixpath.join(folder, name)
            if not overwrite and await self.sftp.exists(dst):
                there.append(self.shown(dst))
                continue
            is_dir = os.path.isdir(local)
            # No preserve: Android storage refuses chmod, which would
            # abort the copy part-way.
            await self.sftp.put(local, folder, recurse=is_dir,
                                progress_handler=_progress())
            items.append([local, self.shown(dst), _local_size(local),
                          is_dir])
        return {"to": self.shown(folder), "items": items, "exists": there,
                "missing": missing}


def _unique(path: str) -> str:
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    if os.path.isdir(path):
        stem, ext = path, ""
    n = 1
    while os.path.lexists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def _tidy(path: str) -> None:
    """Ordinary permissions: preserve copies the phone's mode bits, which
    on Android storage can be 0660, 0771 or nothing at all."""
    try:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                os.chmod(root, 0o755)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o644)
        elif os.path.isfile(path):
            os.chmod(path, 0o644)
    except OSError as e:
        log.debug("phone: chmod %s: %s", path, e)


def _local_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


async def _serve(request: dict) -> dict:
    """Run one request from the shell; the answer always names the host
    that answered, so the shell can remember it."""
    ssh = _asyncssh()
    host = None
    started = time.monotonic()
    try:
        cfg = _read_config(request.get("config") or config_path())
        conn, host = await _connect(cfg, request.get("hosts") or [])
        try:
            async with conn.start_sftp_client() as sftp:
                remote = _Remote(sftp, await sftp.realpath("."))
                op = request.get("op")
                if op not in ("list", "pull", "push"):
                    raise PhoneError(f"unknown operation {op!r}")
                result = await getattr(remote, op)(**request.get("args", {}))
        finally:
            conn.close()
        result["seconds"] = round(time.monotonic() - started, 2)
        return {"ok": True, "host": host, "result": result}
    except PhoneError as e:
        return {"ok": False, "host": host, "error": str(e)}
    except ssh.SFTPError as e:
        return {"ok": False, "host": host, "error": f"the phone: {e.reason}"}
    except (OSError, ssh.Error) as e:
        return {"ok": False, "host": host,
                "error": f"{type(e).__name__}: {e}"}


# How far a copy has got, for the call's progress bar: each file by
# name, its bytes as they go (agent/progress.py).
_REPORT = Reporter()


def _progress():
    """asyncssh's progress_handler for one copy, file by file."""
    named: set[str] = set()

    def handler(src, _dst, done: int, total: int) -> None:
        if not _REPORT:
            return
        path = src.decode(errors="replace") if isinstance(src, bytes) \
            else str(src)
        name = posixpath.basename(path.rstrip("/")) or path
        if name not in named:
            named.add(name)
            _REPORT.send(label=name, done=done, total=total or None,
                         unit="B")
        else:
            _REPORT.send(done=done, total=total or None, unit="B")
    return handler


async def _do() -> int:
    try:
        request = json.loads(sys.stdin.read())
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"bad request: {e}"}))
        return 2
    print(json.dumps(await _serve(request)))
    _REPORT.flush()
    return 0


# ── the shell's side ────────────────────────────────────────────────────

class Phone:
    """The phone, as the agent's tools use it."""

    def __init__(self, config: str | None = None) -> None:
        self.config = config or config_path()
        # agent/progress.py's Jobs, when the call shows transfers' bars.
        self.progress = None

    def _remembered(self) -> list[str]:
        got = state.get(STATE_KEY)
        host = got.get("host") if isinstance(got, dict) else None
        return [host] if isinstance(host, str) else []

    async def _run(self, op: str, **args) -> dict:
        if not os.path.exists(self.config):
            raise PhoneError(NOT_SET_UP)
        request = {"op": op, "args": args, "config": self.config,
                   "hosts": self._remembered()}
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (root, env.get("PYTHONPATH")) if p)
        if self.progress is not None:
            env.update(self.progress.child_env())
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-W", "ignore::RuntimeWarning",
            "-m", __name__, "do", env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await proc.communicate(json.dumps(request).encode())
        except asyncio.CancelledError:
            proc.kill()
            raise
        if err.strip():
            log.debug("phone helper: %s", err.decode(errors="replace"))
        try:
            reply = json.loads(out)
        except ValueError:
            tail = err.decode(errors="replace").strip()[-400:]
            raise PhoneError(f"the phone helper failed: "
                             f"{tail or f'exit {proc.returncode}'}") from None
        host = reply.get("host")
        if host and [host] != self._remembered():
            state.set(STATE_KEY, {"host": host})
        if not reply.get("ok"):
            raise PhoneError(reply.get("error") or "the phone helper failed")
        return reply["result"]

    async def list(self, path: str = "", *, pattern: str = "",
                   depth: int = 0, newest: bool = False,
                   limit: int = LIST_DEFAULT) -> str:
        r = await self._run("list", path=path, pattern=pattern, depth=depth,
                            newest=newest, limit=limit)
        n = r["total"]
        what = (f"{n} {'match' if n == 1 else 'matches'}" if pattern
                else f"{n} {'entry' if n == 1 else 'entries'}")
        order = "newest first" if newest else "folders first, by name"
        shown = (f", showing {len(r['rows'])}"
                 if len(r["rows"]) < r["total"] else "")
        lines = [f"{r['base']} — {what}, {order}{shown}. Paths are "
                 f"relative to {r['home']}, as the other phone tools "
                 "take them."]
        for path, size, mtime, kind in r["rows"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) \
                if mtime else " " * 16
            if kind == "d":
                lines.append(f"{when}  {'-':>9}  {path}/")
            elif kind == "l":
                lines.append(f"{when}  {'link':>9}  {path}@")
            else:
                lines.append(f"{when}  {human(size):>9}  {path}")
        if r["walk_cut"]:
            lines.append(f"(stopped after {WALK_MAX} entries — narrow the "
                         "path or depth)")
        if r["unreadable"]:
            lines.append("Couldn't read: " + ", ".join(r["unreadable"]))
        return "\n".join(lines)

    async def pull(self, paths: list[str], to: str = "~/Downloads", *,
                   overwrite: bool = False) -> str:
        to = _local(to or "~/Downloads")
        r = await self._run("pull", paths=list(paths), to=to,
                            overwrite=overwrite)
        return _report("Pulled", "to", r, missing_where="on the phone")

    async def push(self, paths: list[str], to: str = "", *,
                   overwrite: bool = False) -> str:
        expanded, missing = [], []
        for p in paths:
            p = _local(p)
            hits = sorted(glob.glob(p)) if glob.has_magic(p) else (
                [p] if os.path.exists(p) else [])
            if hits:
                expanded.extend(hits)
            else:
                missing.append(p)
        if not expanded:
            raise PhoneError("Nothing to send — not found here: "
                             + ", ".join(missing))
        r = await self._run("push", paths=expanded, to=to,
                            overwrite=overwrite)
        r["missing"] = missing + r.get("missing", [])
        return _report("Sent", "to the phone's", r, missing_where="here")


def _local(path: str) -> str:
    path = os.path.expanduser(path.strip())
    return os.path.normpath(path if os.path.isabs(path)
                            else os.path.join(os.path.expanduser("~"), path))


def _report(verb: str, prep: str, r: dict, *, missing_where: str) -> str:
    items = r["items"]
    total = sum(i[2] for i in items)
    secs = max(r.get("seconds", 0), 0.01)
    lines = []
    if items:
        lines.append(f"{verb} {len(items)} item{'s' * (len(items) != 1)} "
                     f"{prep} {r['to']} — {human(total)} in {secs:.1f} s "
                     f"({human(total / secs)}/s):")
        for src, dst, size, is_dir in items:
            slash = "/" if is_dir else ""
            lines.append(f"  {src}{slash} → {dst}{slash}  ({human(size)})")
    else:
        lines.append(f"Nothing {verb.lower()}.")
    if r.get("missing"):
        lines.append(f"Not found {missing_where}: " + ", ".join(r["missing"]))
    if r.get("exists"):
        lines.append("Already on the phone, left alone (overwrite: true "
                     "replaces): " + ", ".join(r["exists"]))
    return "\n".join(lines)


# ── setup, in the user's terminal ───────────────────────────────────────

async def _first_contact(cfg: dict) -> tuple[str, str, str, list[str]]:
    """Connect without a pinned key: the key, its fingerprint, the folder
    the server opens in, and a few names there."""
    ssh = _asyncssh()
    async with ssh.connect(cfg["host"], **_options(cfg, pinned=False)) as conn:
        key = conn.get_server_host_key()
        async with conn.start_sftp_client() as sftp:
            home = await sftp.realpath(".")
            names = sorted(n for n in await sftp.listdir(home)
                           if n not in (".", ".."))
    return (key.export_public_key("openssh").decode().strip(),
            key.get_fingerprint(), home, names)


def _write_config(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def setup(path: str | None = None) -> int:
    import getpass
    path = path or config_path()
    try:
        old = _read_config(path) if os.path.exists(path) else {}
    except PhoneError:
        old = {}
    print("Start the SFTP server in MiXplorer (its Servers menu). It shows "
          "an address like sftp://192.168.1.42:2222.")
    try:
        default = f"{old['host']}:{old['port']}" if old else ""
        text = input(f"Address{f' [{default}]' if default else ''}: ") \
            or default
        host, port, user = parse_address(text)
        if port is None:
            port = int(input("Port [2222]: ") or 2222)
        user = input(f"User [{user or old.get('user', '')}]: ") \
            or user or old.get("user", "")
        hint = " (Enter keeps the saved one)" if old.get("password") else ""
        password = getpass.getpass(f"Password{hint}: ") \
            or old.get("password", "")
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    except (PhoneError, ValueError) as e:
        print(e)
        return 1
    cfg = {"host": host, "port": port, "user": user, "password": password}
    print(f"Connecting to {host}:{port} …")
    try:
        key, fingerprint, home, names = asyncio.run(_first_contact(cfg))
    except PhoneError as e:
        print(e)
        return 1
    except _asyncssh().PermissionDenied:
        print("The phone turned down that user or password — check them "
              "in MiXplorer's SFTP server settings.")
        return 1
    except Exception as e:     # noqa: BLE001 - shown to the user as is
        print(f"Couldn't connect: {type(e).__name__}: {e}")
        return 1
    if old.get("host_key") and old["host_key"] != key:
        print("Note: this is a different key from the one saved before.")
    cfg["host_key"] = key
    _write_config(path, cfg)
    shown = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
    print(f"Connected. The phone's key is {fingerprint}; only a server "
          f"with this key will be trusted as the phone.\n"
          f"It opens in {home}: {shown or '(empty)'}\n"
          f"Saved to {path}.")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "setup":
        return setup()
    if cmd == "do":
        return asyncio.run(_do())
    print(f"usage: {SET_UP}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
