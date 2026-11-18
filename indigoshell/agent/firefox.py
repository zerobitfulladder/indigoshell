"""Your everyday Firefox, for the call's agent: tabs, pages, forms and
JavaScript, with your sign-ins — through an add-on of our own.

Three pieces:

- **the add-on**, `agent/extension/` ("indigoshell bridge"): reads
  pages, clicks, fills forms, presses keys, runs JavaScript (Firefox's
  userScripts API, allowed once from the add-on's options) and takes
  screenshots of tabs;
- **a native messaging host**, `agent/browser_host.py`, which Firefox
  starts for the add-on and which relays to the shell;
- **this module**: the shell's end, a private socket in
  `$XDG_RUNTIME_DIR` that the host connects to.

Firefox stays an ordinary browser: no debugging port, so websites see no
automation, and nothing but this add-on can reach the host.

Setup, once: `python -m indigoshell.agent.firefox install`, then restart
Firefox and press Allow JavaScript in the add-on's options. Arch builds
Firefox to load *unsigned* add-ons from the system extension folder, so
the add-on is linked there from this repository — no Mozilla account,
no signing — and edits here reach Firefox on its next start. That needs
root once (`sudo`), as does registering the host, which goes in the
system folder too: Firefox reads per-user hosts only from ~/.mozilla,
and creating that directory can move a Firefox whose profile lives in
~/.config/mozilla back to the old place. Two preferences in the
profile's user.js have Firefox look at the system folder on startup and
enable what it finds there.
"""

import asyncio
import base64
import json
import logging
import os
import stat
import subprocess
import sys

from ..core import paths

log = logging.getLogger(__name__)

HOST_NAME = "indigoshell"
EXTENSION_ID = "bridge@indigoshell"
HERE = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(HERE, "extension")
CALL_S = 30.0
CONNECT_WAIT_S = 4.0
READ_MAX = 12000
JS_MAX = 20000


class FirefoxError(Exception):
    pass


def socket_path() -> str:
    return os.path.join(paths.runtime_dir(), f"{HOST_NAME}-firefox.sock")


GECKO_APP_ID = "{ec8030f7-c20a-464f-9b0e-13a3a9e97384}"    # Firefox
SYSTEM_EXTENSIONS = f"/usr/lib/mozilla/extensions/{GECKO_APP_ID}"
SYSTEM_HOSTS = "/usr/lib/mozilla/native-messaging-hosts"
PREFS = {
    # Look for changes in the system scope on every start...
    "extensions.startupScanScopes": 8,
    # ...and do not disable what is there until approved: only root can
    # put an add-on in that folder.
    "extensions.autoDisableScopes": 7,
}
PREFS_MARK = "// indigoshell: the INDIGO bridge add-on (agent/firefox.py)"


def launcher_path() -> str:
    return os.path.join(paths.state_dir(), "firefox-host")


def install_host() -> str:
    """Write the small launcher Firefox runs as the host (it finds the
    shell's Python and socket). Returns its path. Run on every start of
    the bridge, so it follows the shell if the repository moves."""
    launcher = launcher_path()
    os.makedirs(os.path.dirname(launcher), exist_ok=True)
    # The socket comes from the environment when set (tests run a second
    # bridge), else the shell's.
    script = (f"#!/bin/sh\n"
              f": \"${{INDIGOSHELL_FIREFOX_SOCK:={socket_path()}}}\"\n"
              f"export INDIGOSHELL_FIREFOX_SOCK\n"
              f"exec {_sh(sys.executable)} -I "
              f"{_sh(os.path.join(HERE, 'browser_host.py'))} \"$@\"\n")
    _write_if_changed(launcher, script, 0o755)
    return launcher


def host_manifest() -> str:
    return json.dumps({
        "name": HOST_NAME,
        "description": "indigoshell: the call agent's bridge to this browser",
        "path": launcher_path(), "type": "stdio",
        "allowed_extensions": [EXTENSION_ID]}, indent=2) + "\n"


def default_profile() -> str | None:
    """The profile Firefox opens by default, from profiles.ini."""
    import configparser
    root = os.path.join(mozilla_dir(), "firefox")
    ini = configparser.ConfigParser(interpolation=None)
    if not ini.read(os.path.join(root, "profiles.ini")):
        return None
    path = next((ini[s]["Default"] for s in ini.sections()
                 if s.startswith("Install") and ini[s].get("Default")), None)
    if path is None:
        path = next((ini[s]["Path"] for s in ini.sections()
                     if ini[s].get("Default") == "1" and ini[s].get("Path")), None)
    if path is None:
        return None
    return path if os.path.isabs(path) else os.path.join(root, path)


def mozilla_dir() -> str:
    """Where this user's Firefox keeps its files: ~/.mozilla if it
    exists, else $XDG_CONFIG_HOME/mozilla. Never create ~/.mozilla — a
    Firefox that found its profiles under ~/.config/mozilla switches back
    to ~/.mozilla as soon as that directory appears, and opens an empty
    profile there."""
    legacy = os.path.expanduser("~/.mozilla")
    if os.path.isdir(legacy):
        return legacy
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "mozilla")


def _sh(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _write_if_changed(path: str, content: str, mode: int) -> None:
    try:
        with open(path) as f:
            if f.read() == content:
                return
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class Firefox:
    """The shell's end of the bridge. A SERVICES entry: `start()` opens
    the socket the host connects to, so the add-on is reachable as soon
    as Firefox is running."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or socket_path()
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._connected: asyncio.Event | None = None
        self.version: str | None = None
        self.javascript = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            install_host()
        except OSError:
            log.exception("could not write the Firefox host manifest")
        asyncio.get_running_loop().create_task(self._serve())

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _event(self) -> asyncio.Event:
        if self._connected is None:
            self._connected = asyncio.Event()
        return self._connected

    async def _serve(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        old = os.umask(0o077)           # the socket is ours alone
        try:
            self._server = await asyncio.start_unix_server(
                self._on_host, self.path, limit=64 * 1024 * 1024)
        finally:
            os.umask(old)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def _on_host(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        if self._writer is not None:
            self._writer.close()
        self._writer = writer
        self._event().set()
        log.info("Firefox bridge connected")
        hello = asyncio.get_running_loop().create_task(self._hello())
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            hello.cancel()
            if self._writer is writer:
                self._writer = None
                self._event().clear()
                log.info("Firefox bridge disconnected")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(FirefoxError("Firefox went away"))
            writer.close()

    async def _hello(self) -> None:
        try:
            got = await self.call("hello", timeout=10)
            self.version = got.get("version")
            self.javascript = bool(got.get("javascript"))
        except FirefoxError:
            pass

    # ── requests ────────────────────────────────────────────────────────
    async def call(self, op: str, timeout: float = CALL_S, **args):
        if self._writer is None:
            try:
                await asyncio.wait_for(self._event().wait(), CONNECT_WAIT_S)
            except asyncio.TimeoutError:
                raise FirefoxError(
                    "Firefox's indigoshell bridge is not connected: Firefox "
                    "has to be running with the indigoshell bridge add-on "
                    "installed (the README's Agent section shows how)") from None
        writer = self._writer
        if writer is None:
            raise FirefoxError("Firefox went away")
        self._next += 1
        mid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            writer.write((json.dumps({"id": mid, "op": op, "args": args})
                          + "\n").encode())
            await writer.drain()
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise FirefoxError(f"Firefox did not answer {op} in {timeout:.0f}s") from None
        except ConnectionError:
            raise FirefoxError("Firefox went away") from None
        finally:
            self._pending.pop(mid, None)
        if not msg.get("ok"):
            raise FirefoxError(str(msg.get("error") or "failed"))
        return msg.get("result")

    async def page(self, tab: int, fn: str, wait: float = 0, **args):
        """Call one of page.js's helpers in a tab; `wait` seconds for any
        page the action starts loading."""
        return await self.call("page", tab=tab, fn=fn, args=args,
                               wait=int(wait * 1000))

    # ── tabs ────────────────────────────────────────────────────────────
    async def tabs(self) -> list[dict]:
        tabs = await self.call("tabs")
        tabs.sort(key=lambda t: (t["window"], t["index"]))
        for i, t in enumerate(tabs):
            t["n"] = i + 1
        return tabs

    async def tab(self, which=None) -> int:
        """A tab id: None/'active' for the one you are looking at, a
        number from the last listing, or words from its title or address."""
        tabs = await self.tabs()
        if not tabs:
            raise FirefoxError("Firefox has no tabs open")
        w = str(which).strip().lower() if which not in (None, "") else "active"
        if w in ("active", "current", "this"):
            pick = next((t for t in tabs if t["focused"]), None) \
                or next((t for t in tabs if t["active"]), tabs[0])
            return pick["id"]
        if w.isdigit() and 1 <= int(w) <= len(tabs):
            return tabs[int(w) - 1]["id"]
        hit = [t for t in tabs if w in t["title"].lower() or w in t["url"].lower()]
        if len(hit) == 1:
            return hit[0]["id"]
        if hit:
            raise FirefoxError(f"{len(hit)} tabs match {which!r}: "
                               + "; ".join(f"{t['n']} {t['title'][:40]}" for t in hit[:8]))
        raise FirefoxError(f"no tab matches {which!r}; list them with browser_tabs")

    async def where(self, tab: int) -> str:
        try:
            w = await self.call("where", tab=tab, timeout=5)
            return f"{w['title'] or '(untitled)'} — {w['url']}"
        except FirefoxError:
            return "(gone)"

    # ── pages ───────────────────────────────────────────────────────────
    async def read(self, tab: int, max_chars: int = READ_MAX,
                   selector: str | None = None) -> str:
        res = await self.page(tab, "read", maxChars=int(max_chars),
                              selector=selector or "")
        head = f"{res.get('title') or '(untitled)'} — {res.get('url')}"
        tail = []
        if res.get("cut"):
            tail.append(f"(cut at {max_chars} characters: read again with a "
                        "selector, or a larger max_chars)")
        if res.get("frames"):
            tail.append(f"({res['frames']} embedded frame(s) not read)")
        return "\n".join([head, "", res.get("text", ""), *tail]).strip()

    async def js(self, tab: int, code: str) -> str:
        out = await self.call("js", tab=tab, code=code)
        text = out if isinstance(out, str) else json.dumps(
            out, indent=1, ensure_ascii=False)
        if len(text) > JS_MAX:
            text = text[:JS_MAX] + f"\n… (cut, {len(text)} characters)"
        return text

    async def screenshot(self, tab: int) -> bytes:
        url = await self.call("shot", tab=tab)
        return base64.b64decode(url.split(",", 1)[1])


# ── setup from a terminal ───────────────────────────────────────────────
def _install() -> int:
    install_host()
    link = os.path.join(SYSTEM_EXTENSIONS, EXTENSION_ID)
    tmp = os.path.join(paths.state_dir(), f"{HOST_NAME}.json")
    with open(tmp, "w") as f:
        f.write(host_manifest())
    script = (f"install -d -m 755 {_sh(SYSTEM_EXTENSIONS)} {_sh(SYSTEM_HOSTS)} && "
              f"ln -sfn {_sh(EXTENSION_DIR)} {_sh(link)} && "
              f"install -m 644 {_sh(tmp)} {_sh(os.path.join(SYSTEM_HOSTS, HOST_NAME + '.json'))}")
    print("As root (sudo), once:\n"
          f"  link   {link} -> {EXTENSION_DIR}\n"
          f"  write  {os.path.join(SYSTEM_HOSTS, HOST_NAME + '.json')}")
    rc = subprocess.call(["sudo", "sh", "-c", script])
    os.unlink(tmp)
    if rc != 0:
        print("sudo failed; nothing else changed.")
        return rc
    profile = default_profile()
    if profile is None:
        print("No default Firefox profile found: set these in about:config:")
        for k, v in PREFS.items():
            print(f"  {k} = {v}")
    else:
        user_js = os.path.join(profile, "user.js")
        try:
            with open(user_js) as f:
                current = f.read()
        except FileNotFoundError:
            current = ""
        if PREFS_MARK not in current:
            lines = [PREFS_MARK] + [f'user_pref("{k}", {v});' for k, v in PREFS.items()]
            with open(user_js, "a") as f:
                f.write(("\n" if current and not current.endswith("\n") else "")
                        + "\n".join(lines) + "\n")
            print(f"  prefs  {user_js}: " + ", ".join(f"{k}={v}" for k, v in PREFS.items()))
    print("\nNow restart Firefox. Then about:addons → INDIGO bridge → Options →"
          " Allow JavaScript.")
    return 0


def _uninstall() -> int:
    link = os.path.join(SYSTEM_EXTENSIONS, EXTENSION_ID)
    rc = subprocess.call(["sudo", "rm", "-f", link,
                          os.path.join(SYSTEM_HOSTS, HOST_NAME + ".json")])
    profile = default_profile()
    if profile:
        user_js = os.path.join(profile, "user.js")
        try:
            with open(user_js) as f:
                lines = f.read().splitlines()
            keep = [ln for ln in lines if ln != PREFS_MARK
                    and not any(f'"{k}"' in ln for k in PREFS)]
            with open(user_js, "w") as f:
                f.write("\n".join(keep) + ("\n" if keep else ""))
        except FileNotFoundError:
            pass
        print("Removed. The two prefs keep their last values until reset in "
              "about:config: " + ", ".join(PREFS))
    return rc


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "install":
        return _install()
    if cmd == "uninstall":
        return _uninstall()
    print("usage: python -m indigoshell.agent.firefox install | uninstall")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
