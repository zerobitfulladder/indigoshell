"""The user's phone, for the call's agent: its files, and a shell on it.

The phone (Android) runs Termux with its OpenSSH server on port 8022,
and Termux:API, so one key-only SSH login reaches both its storage
(SFTP) and its commands: texts, the call log, notifications, location,
the camera, ... The user switches the server on and off with the
`uplink` home-screen widget, a Termux script (~/.shortcuts/tasks/uplink)
that starts sshd, holds a wake lock and pins a notification whose SEVER
button stops it again.

**Two ways in.**

- The file tools here (phone_files, phone_pull, phone_push), which feed
  the call's progress bars.
- `ssh phone <command>` in the agent's Bash, for everything else. Setup
  writes a `phone` host into ~/.ssh/config whose ProxyCommand is
  `proxy` below: it finds the phone and hands ssh the connected socket.
  The workspace's phone skill (workspace/.claude/skills/phone) tells
  the agent what the commands are; being Bash, each one passes auto
  mode's check like any other command.

**Setting up** — once, by the user, in a terminal, with sshd running in
Termux (`pkg install openssh`, `passwd`, `sshd`):

    .venv/bin/python -m indigoshell.agent.phone setup     # in the checkout

(Messages that tell the user to run it name this checkout and this
interpreter, worked out at run time: see `SET_UP`.) It asks for the
phone's address, makes this computer a key
(~/.config/indigoshell/connections/phone/key) and, the first time, asks
for the Termux password once to put the key on the phone, so the
password never passes through the agent; then it turns password logins
off there. It
pins the server's host key and writes
`~/.config/indigoshell/connections/phone/config.json` (0600):

    {"host": "192.168.1.42", "port": 8022, "user": "termux",
     "key": "/home/me/.config/indigoshell/connections/phone/key",
     "host_key": "ssh-ed25519 AAAA...", "root": "/storage/emulated/0"}

and the `phone` block in ~/.ssh/config, with its own known_hosts.

**Finding it.** The server runs only while the uplink is on, and the
phone's address can change (DHCP). Each connection tries the last
address that answered (state key `agent.phone`) and the configured one,
then every host on the local subnets with the port open; only a server
holding the pinned key counts. `proxy` does the same for ssh, and keeps
config.json's host current.

**Paths** are the phone's: relative ones start in `root`, internal
storage (DCIM, Download, Pictures, ...), and listings give paths that
way so they can be handed straight back.

**Out of process.** SSH spends Python time on every packet; a large
video moved on the shell's one loop would hitch the bar. Each operation
runs in a child (`python -m indigoshell.agent.phone do`: a JSON request
on stdin, a JSON answer on stdout). Stopping the agent kills it.
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

from ..core import state
from . import connections
from .watch import Reporter

log = logging.getLogger(__name__)

STATE_KEY = "agent.phone"
PORT = 8022                 # Termux's sshd: Android keeps apps off 22
USER = "termux"             # Termux takes any user name
ROOT = "/storage/emulated/0"
SSH_HOST = "phone"          # `ssh phone`
HOST_KEY_ALIAS = "indigoshell-phone"   # its known_hosts name, any address
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

# The checkout this runs from: children's PYTHONPATH, and where the user
# runs setup. Found at run time, so no one's paths live in the repo.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _shown(path: str) -> str:
    """A path as the user would type it: ~/... under their home, and
    relative to the checkout when inside it."""
    if path.startswith(REPO + os.sep):
        return os.path.relpath(path, REPO)
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


SET_UP = (f"cd {_shown(REPO)} && {_shown(sys.executable)} -m "
          "indigoshell.agent.phone setup")
NOT_SET_UP = ("The phone isn't paired yet. With Termux's SSH server running "
              "on it, the user runs, once, in a terminal: " + SET_UP)
NOT_FOUND = ("The phone didn't answer — its uplink is probably off. Ask "
             "the user to tap the uplink widget on the phone (it starts "
             "Termux's SSH server); the phone and this computer must be "
             "on the same Wi-Fi.")
REPAIR = ("If Termux was reinstalled or lost this computer's key, the user "
          "runs setup again: " + SET_UP)


class PhoneError(Exception):
    pass


def config_path() -> str:
    return connections.path("phone", "config.json")


def key_path() -> str:
    """This computer's key for the phone (and its .pub)."""
    return connections.path("phone", "key")


def known_hosts_path() -> str:
    """The phone's host key, for `ssh phone`."""
    return connections.path("phone", "known_hosts")



def _remembered() -> list[str]:
    """The address that answered last time, if any."""
    got = state.get(STATE_KEY)
    host = got.get("host") if isinstance(got, dict) else None
    return [host] if isinstance(host, str) else []


def _asyncssh():
    try:
        import asyncssh
    except ImportError:
        raise PhoneError(
            f"asyncssh is missing — in {_shown(REPO)}: uv pip install "
            f"--python {_shown(sys.executable)} asyncssh") from None
    return asyncssh


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def parse_address(text: str) -> tuple[str, int | None, str | None]:
    """Host, port and user from 192.168.1.42, 192.168.1.42:8022 or
    sftp://user@192.168.1.42:8022/."""
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
    missing = [k for k in ("host", "port", "user", "key", "host_key")
               if not cfg.get(k)]
    if missing:
        raise PhoneError(f"{path} lacks {', '.join(missing)} — run "
                         f"setup again: {SET_UP}")
    if not os.path.exists(cfg["key"]):
        raise PhoneError(f"This computer's key for the phone, {cfg['key']}, "
                         f"is gone. {REPAIR}")
    return cfg


def _options(cfg: dict, *, pinned: bool = True) -> dict:
    ssh = _asyncssh()
    # This key only: no agent or default keys offered first, which the
    # server would count as failed attempts.
    opts: dict = dict(port=int(cfg["port"]), username=cfg["user"],
                      connect_timeout=CONNECT_S, login_timeout=LOGIN_S,
                      known_hosts=None, client_keys=[cfg["key"]],
                      agent_path=None)
    if pinned:
        key = ssh.import_public_key(cfg["host_key"])
        opts["known_hosts"] = ([key], [], [])
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


async def _holds_key(host: str, port: int, pinned) -> bool | None:
    """Whether the server at host shows the phone's pinned key: a key
    exchange, no login. None when it couldn't tell."""
    ssh = _asyncssh()
    algs = [a.decode() for a in pinned.sig_algorithms]
    try:
        key = await asyncio.wait_for(ssh.get_server_host_key(
            host, port, server_host_key_algs=algs), CONNECT_S)
    except (OSError, asyncio.TimeoutError, ssh.Error) as e:
        log.debug("phone: %s: %s", host, e)
        return None
    return key is not None and key.public_data == pinned.public_data


async def _locate(cfg: dict, remembered: list[str]) -> str:
    """The address of the server holding the phone's key, wherever DHCP
    put it."""
    ssh = _asyncssh()
    pinned = ssh.import_public_key(cfg["host_key"])
    port = int(cfg["port"])
    stranger = []

    async def first(hosts: list[str], wait: float) -> str | None:
        for host in await _reachable(hosts, port, wait):
            held = await _holds_key(host, port, pinned)
            if held:
                return host
            if held is False:
                stranger.append(host)
        return None

    # The addresses it had first, all at once, so a stale one costs a
    # short probe rather than an SSH timeout; then the whole LAN.
    known = list(dict.fromkeys([*remembered, cfg["host"]]))
    host = await first(known, KNOWN_PROBE_S)
    if host is None:
        from .wiz import _networks
        host = await first([h for h in _networks()[1] if h not in known],
                           PROBE_S)
    if host:
        return host
    if stranger:
        raise PhoneError(
            f"Something answers at {', '.join(stranger)} on port {port}, "
            f"but with a different key than the phone's. {REPAIR}")
    raise PhoneError(NOT_FOUND)


async def _connect(cfg: dict, remembered: list[str]):
    """(connection, host) to the phone, wherever it is now."""
    ssh = _asyncssh()
    host = await _locate(cfg, remembered)
    try:
        return await ssh.connect(host, **_options(cfg)), host
    except ssh.PermissionDenied:
        raise PhoneError(f"The phone turned down this computer's key. "
                         f"{REPAIR}") from None


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
                remote = _Remote(sftp, cfg.get("root")
                                 or await sftp.realpath("."))
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

    async def _run(self, op: str, **args) -> dict:
        if not os.path.exists(self.config):
            raise PhoneError(NOT_SET_UP)
        request = {"op": op, "args": args, "config": self.config,
                   "hosts": _remembered()}
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (REPO, env.get("PYTHONPATH")) if p)
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
        if host and [host] != _remembered():
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


# ── `ssh phone`: the proxy, and the files ssh reads ─────────────────────

BEGIN = "# >>> indigoshell phone >>>"
END = "# <<< indigoshell phone <<<"


def proxy() -> int:
    """ssh's ProxyCommand, with ProxyUseFdpass: find the phone, connect
    to it and pass ssh the socket over stdout. ssh checks the host key
    and logs in itself; this only knows where the phone is now."""
    import socket
    host = None
    try:
        cfg = _read_config(config_path())
        host = asyncio.run(_locate(cfg, _remembered()))
        sock = socket.create_connection((host, int(cfg["port"])), CONNECT_S)
    except PhoneError as e:
        print(f"phone: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"phone: {host or 'finding it'}: {e}", file=sys.stderr)
        return 1
    if host != cfg["host"]:
        cfg["host"] = host
        _write_config(config_path(), cfg)
    sock.setblocking(True)
    with socket.socket(fileno=os.dup(sys.stdout.fileno())) as out:
        socket.send_fds(out, [b"\0"], [sock.fileno()])
    sock.close()
    return 0


def ssh_config_block(cfg: dict) -> str:
    """The `phone` host for ~/.ssh/config."""
    return "\n".join([
        BEGIN,
        "# `ssh phone`: Termux on the user's phone, wherever DHCP put it.",
        "# Written by `python -m indigoshell.agent.phone setup`; run that",
        "# again rather than editing here.",
        f"Host {SSH_HOST}",
        f"    HostKeyAlias {HOST_KEY_ALIAS}",
        f"    Port {cfg['port']}",
        f"    User {cfg['user']}",
        f"    IdentityFile {cfg['key']}",
        "    IdentitiesOnly yes",
        f"    UserKnownHostsFile {known_hosts_path()}",
        "    StrictHostKeyChecking yes",
        "    BatchMode yes",
        f"    ProxyCommand env PYTHONPATH={REPO} {sys.executable} "
        "-W ignore::RuntimeWarning -m indigoshell.agent.phone proxy",
        "    ProxyUseFdpass yes",
        "    ServerAliveInterval 15",
        "    ServerAliveCountMax 2",
        "    ControlMaster auto",
        "    ControlPath ~/.ssh/indigoshell-phone-%C",
        "    ControlPersist 60",
        END, ""])


def _write_ssh_files(cfg: dict) -> str:
    """The phone's known_hosts, and its block in ~/.ssh/config (whose
    path this returns)."""
    _write_private(known_hosts_path(),
                   f"{HOST_KEY_ALIAS} {cfg['host_key']}\n")
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    path = os.path.join(ssh_dir, "config")
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    block = ssh_config_block(cfg)
    start, end = text.find(BEGIN), text.find(END)
    if start != -1 and end > start:
        rest = text[end + len(END):].lstrip("\n")
        text = text[:start] + block + ("\n" + rest if rest else "")
    else:
        # At the top: ssh keeps the first value it reads for an option,
        # so a `Host *` further down can't override these.
        text = block + ("\n" + text if text else "")
    _write_private(path, text)
    return path


def follow_moves(moved: dict[str, str]) -> None:
    """After `connections.tidy()` moved the phone's files ({old: new}):
    point config.json's key, and the `phone` block in ~/.ssh/config, at
    their new places. The rest of the block stays as setup wrote it."""
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = None
    if isinstance(cfg, dict) and cfg.get("key") in moved:
        cfg["key"] = moved[cfg["key"]]
        _write_config(config_path(), cfg)
    path = os.path.join(os.path.expanduser("~/.ssh"), "config")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end < start:
        return
    lines = text[start:end].split("\n")
    for i, line in enumerate(lines):
        words = line.split()
        if (len(words) == 2 and words[0] in ("IdentityFile",
                                              "UserKnownHostsFile")
                and words[1] in moved):
            lines[i] = line.replace(words[1], moved[words[1]])
    block = "\n".join(lines)
    if block != text[start:end]:
        _write_private(path, text[:start] + block + text[end:])


# ── setup, in the user's terminal ───────────────────────────────────────

# Key logins only, from now on. Exits 3 on a server that isn't Termux's.
KEY_ONLY = r"""
d="$PREFIX/etc/ssh/sshd_config.d"
[ -n "$PREFIX" ] && [ -d "$d" ] || exit 3
f="$d/10-key-only.conf"
grep -qx 'PasswordAuthentication no' "$f" 2>/dev/null && exit 0
printf '%s\n' '# indigoshell: key logins only' 'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' > "$f" &&
sshd -t && kill -HUP "$(cat "$PREFIX/var/run/sshd.pid")"
"""
AUTHORIZE = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
             "cat >> ~/.ssh/authorized_keys && "
             "chmod 600 ~/.ssh/authorized_keys")


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)
    os.replace(tmp, path)


def _write_config(path: str, cfg: dict) -> None:
    _write_private(path, json.dumps(cfg, indent=2) + "\n")


def _ensure_key(path: str) -> str:
    """This computer's key for the phone, made the first time: its
    public half, as authorized_keys takes it."""
    ssh = _asyncssh()
    if os.path.exists(path):
        key = ssh.read_private_key(path)
    else:
        key = ssh.generate_private_key("ssh-ed25519",
                                       comment="indigoshell agent")
        _write_private(path, key.export_private_key().decode())
    pub = key.export_public_key().decode().strip()
    if not os.path.exists(path + ".pub"):
        with open(path + ".pub", "w") as f:
            f.write(pub + "\n")
    return pub


async def _pair(cfg: dict, pub: str, ask_password) -> dict:
    """Pin the phone's host key (into cfg), put this computer's key on
    the phone (asking for the Termux password only when it isn't there
    yet), turn password logins off, and look in root."""
    ssh = _asyncssh()
    host, port = cfg["host"], int(cfg["port"])
    try:
        key = await asyncio.wait_for(ssh.get_server_host_key(
            host, port, server_host_key_algs=["ssh-ed25519"]), CONNECT_S)
    except ssh.KeyExchangeFailed:
        key = None
    if key is None:
        key = await asyncio.wait_for(ssh.get_server_host_key(host, port),
                                     CONNECT_S)
    if key is None:
        raise PhoneError(f"Nothing at {host}:{port} speaks SSH.")
    cfg["host_key"] = key.export_public_key("openssh").decode().strip()
    opts = _options(cfg)
    try:
        conn = await ssh.connect(host, **opts)
    except ssh.PermissionDenied:
        password = ask_password()
        async with ssh.connect(
                host, port=port, username=cfg["user"], password=password,
                client_keys=None, agent_path=None,
                known_hosts=opts["known_hosts"], connect_timeout=CONNECT_S,
                login_timeout=LOGIN_S) as first:
            await first.run(AUTHORIZE, input=pub + "\n", check=True)
        try:
            conn = await ssh.connect(host, **opts)
        except ssh.PermissionDenied:
            raise PhoneError("The phone took the key but still turns it "
                             "down; check ~/.ssh in Termux (chmod 700, "
                             "authorized_keys 600).") from None
    async with conn:
        locked = (await conn.run(KEY_ONLY, check=False)).exit_status == 0
        async with conn.start_sftp_client() as sftp:
            root = cfg["root"]
            if not await sftp.isdir(root):
                root = await sftp.realpath(".")
            names = sorted(n for n in await sftp.listdir(root)
                           if n not in (".", ".."))
    return {"fingerprint": key.get_fingerprint(), "root": root,
            "names": names, "locked": locked}


def setup(address: str | None = None, path: str | None = None) -> int:
    import getpass
    connections.tidy()
    path = path or config_path()
    try:
        with open(path) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    try:
        if address is None:
            print("On the phone, in Termux: `pkg install openssh`, `passwd` "
                  "(a password, used once here to put this computer's key "
                  "on the phone), then `sshd` — or tap the uplink widget.")
            default = old.get("host", "")
            address = input("Phone's address"
                            + (f" [{default}]" if default else "")
                            + ": ") or default
        host, port, user = parse_address(address)
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    except PhoneError as e:
        print(e)
        return 1
    cfg = {"host": host, "port": port or PORT, "user": user or USER,
           "key": old.get("key") or key_path(),
           "root": old.get("root") or ROOT}

    def ask_password() -> str:
        return getpass.getpass("Termux password (from `passwd`; used once, "
                               "to put this computer's key on the phone): ")

    print(f"Connecting to {host}:{cfg['port']} …")
    try:
        pub = _ensure_key(cfg["key"])
        got = asyncio.run(_pair(cfg, pub, ask_password))
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    except PhoneError as e:
        print(e)
        return 1
    except _asyncssh().PermissionDenied:
        print("The phone turned down that password: set one in Termux with "
              "`passwd` and try again.")
        return 1
    except Exception as e:     # noqa: BLE001 - shown to the user as is
        print(f"Couldn't connect: {type(e).__name__}: {e}")
        return 1
    if (old.get("host_key") and old.get("port") == cfg["port"]
            and old["host_key"] != cfg["host_key"]):
        print("Note: the phone shows a different key from the one saved "
              "before.")
    cfg["root"] = got["root"]
    _write_config(path, cfg)
    ssh_config = _write_ssh_files(cfg)
    names = got["names"]
    shown = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
    print(f"Paired. The phone's key is {got['fingerprint']}; only a server "
          "with this key is trusted as the phone.\n"
          "Password logins on the phone are "
          + ("off." if got["locked"] else "still on (this isn't Termux?).")
          + f"\nFiles start in {got['root']}: {shown or '(empty)'}\n"
          f"Saved to {path}; `ssh {SSH_HOST}` is set up in {ssh_config}.")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "setup":
        return setup(argv[2] if len(argv) > 2 else None)
    if cmd == "do":
        return asyncio.run(_do())
    if cmd == "proxy":
        return proxy()
    print(f"usage: {SET_UP} [address]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
