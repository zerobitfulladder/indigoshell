"""Which browser a site is told it is talking to: the User-Agent, per site.

Two identities:

    chrome     Qt's own User-Agent with its `QtWebEngine/x.y.z` token
               removed: what a Chromium of this version says. The default.
    original   Qt's User-Agent untouched.

Most sites want chrome (WhatsApp Web refuses the original: it sniffs the
string and stumbles on the QtWebEngine token). Google's sign-in refuses
chrome and lets the original through. Client hints (Sec-CH-UA,
navigator.userAgentData) stay exactly as Qt sets them for both; a
"Google Chrome" brand there is refused by Google too. Client hints are
profile-wide anyway, so an identity is the User-Agent string and
nothing else.

Mechanics. The profile's User-Agent is the default identity, so its
header and `navigator.userAgent` agree natively. For a host mapped to
the other one:
- the request header is set by `Interceptor`, by the *request's* host
  (so a page elsewhere fetching from a mapped host sends the mapped
  identity to it);
- `navigator.userAgent` / `navigator.appVersion` are replaced by a
  script injected at document creation, in the page's own world, in
  every frame, that looks at the frame's own host. Chromium does not
  run it in an about:blank frame or popup a page makes for itself, so
  on a mapped page the script also reaches into those when the page
  touches them (contentWindow, contentDocument, window.open).
  Workers still see the profile's string: no page script runs there.

The list is `<config_dir>/browser/sites.json`, made on first start:

    {"identity": {"accounts.google.com": "original",
                  "accounts.youtube.com": "original"}}

A key is an exact host, or `*.example.com` for example.com and every
host under it; an exact host beats a pattern, a longer pattern beats a
shorter one. Edits apply without a restart, to pages loaded after the
edit. A file that does not parse is reported and the rules before it
stay; a bad entry is reported and skipped.

The same file may say where the agent stays out (protocol.py, "Sites
kept from the agent"), with the same keys:

    {"identity": {...}, "agent": {"bank.example": "never", "*.health.example": "never"}}

"never" is the one policy: every agent request on a tab at that host is
refused. The table is optional and empty unless the user writes it;
Jack never adds to it. It applies from the next request after an edit.
"""

import json
import logging
import os
import re
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineUrlRequestInterceptor

from indigoshell.core.paths import config_dir

log = logging.getLogger(__name__)

CHROME, ORIGINAL = "chrome", "original"
IDENTITIES = (CHROME, ORIGINAL)
DEFAULT = CHROME
SCRIPT_NAME = "identity"

DEFAULT_SITES = {"identity": {"accounts.google.com": ORIGINAL,
                              "accounts.youtube.com": ORIGINAL}}

IDENTITY_TABLE, AGENT_TABLE = "identity", "agent"
NEVER = "never"
AGENT_POLICIES = (NEVER,)
# What each table's values may be, and what a host none names gets.
TABLES = {IDENTITY_TABLE: (IDENTITIES, DEFAULT), AGENT_TABLE: (AGENT_POLICIES, None)}

_HOST = re.compile(r"^[a-z0-9_]([a-z0-9_.-]*[a-z0-9_])?$|^\[?[0-9a-f:.]+\]?$")


def sites_path() -> Path:
    return Path(config_dir()) / "browser" / "sites.json"


def user_agents(qt_user_agent: str) -> dict[str, str]:
    return {ORIGINAL: qt_user_agent,
            CHROME: re.sub(r"QtWebEngine/\S+\s*", "", qt_user_agent)}


def _normal(host: str) -> str:
    return host.strip().lower().rstrip(".").strip("[]")


class Rules:
    """host -> value, from one table of sites.json: the identity (the
    default "identity" table), or the agent's policy ("agent")."""

    def __init__(self, exact: dict[str, str] | None = None,
                 domains: dict[str, str] | None = None, default: str | None = DEFAULT) -> None:
        self.exact = dict(exact or {})
        # Longest first: the most specific pattern wins.
        self.domains = sorted((domains or {}).items(), key=lambda kv: -len(kv[0]))
        self.default = default

    def value_for(self, host: str) -> str | None:
        host = _normal(host)
        found = self.exact.get(host)
        if found:
            return found
        for domain, value in self.domains:
            if host == domain or host.endswith("." + domain):
                return value
        return self.default

    identity_for = value_for

    def __eq__(self, other) -> bool:
        return (isinstance(other, Rules) and self.exact == other.exact
                and self.domains == other.domains and self.default == other.default)

    def __repr__(self) -> str:
        return f"Rules(exact={self.exact}, domains={dict(self.domains)})"

    @classmethod
    def parse(cls, data, table_name: str = IDENTITY_TABLE) -> tuple["Rules", list[str]]:
        """Rules from one table of sites.json's parsed contents, and what
        was skipped (with the identity table, also the keys no table
        has). Raises ValueError when the shape is wrong as a whole."""
        if not isinstance(data, dict):
            raise ValueError("the file is not a JSON object")
        values, default = TABLES[table_name]
        warnings = ([f"unknown key {k!r} ignored" for k in data if k not in TABLES]
                    if table_name == IDENTITY_TABLE else [])
        table = data.get(table_name, {})
        if not isinstance(table, dict):
            raise ValueError(f'"{table_name}" is not an object of host: {table_name}')
        exact, domains = {}, {}
        for key, identity in table.items():
            if identity not in values:
                warnings.append(f"{key!r}: {table_name} {identity!r} is not one of "
                                f"{', '.join(values)}; skipped")
                continue
            pattern = key.strip().lower() if isinstance(key, str) else ""
            wild = pattern.startswith("*.")
            host = _normal(pattern[2:] if wild else pattern)
            if not host or not _HOST.match(host):
                warnings.append(f"{key!r} is not a host or *.domain; skipped")
                continue
            (domains if wild else exact)[host] = identity
        return cls(exact, domains, default), warnings


# ── the page side ───────────────────────────────────────────────────────
# Main world, document creation, every frame. Replaces the getters on
# Navigator.prototype through a Proxy of the native getter, so they keep
# their place, flags and native-looking source.
_SCRIPT = r"""
(() => {
  const EXACT = %(exact)s, DOMAINS = %(domains)s, UA = %(ua)s;
  const hostOf = (w) => {
    let h = '';
    try { h = w.location.hostname; } catch (e) { return ''; }
    if (!h) {
      try { const o = w.origin; if (o && o !== 'null') h = new URL(o).hostname; } catch (e) {}
    }
    return h.toLowerCase().replace(/\.$/, '').replace(/^\[|\]$/g, '');
  };
  const host = hostOf(self);
  let id = Object.prototype.hasOwnProperty.call(EXACT, host) ? EXACT[host] : null;
  if (id === null) {
    for (const [d, i] of DOMAINS) {
      if (host === d || host.endsWith('.' + d)) { id = i; break; }
    }
  }
  const ua = id && UA[id];
  if (!ua) return;
  const app = ua.replace(/^Mozilla\//, '');
  const swap = (proto, name, value) => {
    const d = Object.getOwnPropertyDescriptor(proto, name);
    if (d && d.get) {
      Object.defineProperty(proto, name, {...d, get: new Proxy(d.get, {apply: () => value})});
    }
  };
  const done = new WeakSet();
  const dress = (w) => {
    try {
      if (!w || done.has(w.Navigator.prototype) || hostOf(w) !== host && hostOf(w) !== '') return;
      done.add(w.Navigator.prototype);
      swap(w.Navigator.prototype, 'userAgent', ua);
      swap(w.Navigator.prototype, 'appVersion', app);
    } catch (e) {}   // cross-origin: its own document-creation script handles it
  };
  dress(self);
  const wrap = (proto, name, after) => {
    const d = proto && Object.getOwnPropertyDescriptor(proto, name);
    if (!d) return;
    const key = d.get ? 'get' : 'value';
    if (typeof d[key] !== 'function') return;
    Object.defineProperty(proto, name, {...d, [key]: new Proxy(d[key], {
      apply(target, that, args) { const r = Reflect.apply(target, that, args); after(r); return r; },
    })});
  };
  for (const C of [self.HTMLIFrameElement, self.HTMLFrameElement, self.HTMLObjectElement]) {
    if (!C) continue;
    wrap(C.prototype, 'contentWindow', dress);
    wrap(C.prototype, 'contentDocument', (doc) => dress(doc && doc.defaultView));
  }
  wrap(self, 'open', dress);
})();
"""


def script_source(rules: Rules, agents: dict[str, str]) -> str:
    """The page script for `rules`: only identities other than the
    default need replacing; the default is the profile's own."""
    wanted = {i: ua for i, ua in agents.items() if i != DEFAULT}
    return _SCRIPT % {"exact": json.dumps(rules.exact),
                      "domains": json.dumps([list(kv) for kv in rules.domains]),
                      "ua": json.dumps(wanted)}


# ── the request side ────────────────────────────────────────────────────
class Interceptor(QWebEngineUrlRequestInterceptor):
    """Sets the User-Agent header of requests to mapped hosts.

    Keep a Python reference to this object for as long as the profile
    uses it: handed to setUrlRequestInterceptor with no other reference,
    PySide drops the Python half and Chromium is never asked again (the
    header silently stays the profile's)."""

    def __init__(self, identity: "Identity") -> None:
        super().__init__(identity)
        self.identity = identity

    def interceptRequest(self, info) -> None:
        name = self.identity.rules.identity_for(info.requestUrl().host())
        if name != DEFAULT:
            info.setHttpHeader(b"User-Agent", self.identity.agents[name].encode())


# ── the file ────────────────────────────────────────────────────────────
class Identity(QObject):
    """The per-site identity of one profile, kept in step with sites.json."""

    changed = Signal()          # rules reloaded from the file
    DEBOUNCE_MS = 150

    def __init__(self, profile, path: Path | None = None) -> None:
        super().__init__(profile)
        self.profile = profile
        self.path = Path(path) if path else sites_path()
        self.agents = user_agents(profile.httpUserAgent())
        self.rules = Rules()
        self.agent_rules = Rules(default=None)      # where the agent stays out
        self.reloads = 0            # successful loads, for whoever wants to wait on one
        self._text = None           # what the last read found, parsed or not

        profile.setHttpUserAgent(self.agents[DEFAULT])
        self.interceptor = Interceptor(self)
        profile.setUrlRequestInterceptor(self.interceptor)

        self._ensure_file()
        self._load()
        self._install_script()

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._soon)
        self.watcher.directoryChanged.connect(self._soon)
        self._watch()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.DEBOUNCE_MS)
        self.timer.timeout.connect(self._reload)

    def _ensure_file(self) -> None:
        if self.path.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(DEFAULT_SITES, indent=2) + "\n")
            log.info("wrote %s", self.path)
        except OSError as e:
            log.warning("could not write %s: %s", self.path, e)

    def _watch(self) -> None:
        # The directory too: an editor that saves by writing a new file
        # and renaming it over the old one ends the watch on the file.
        for p in (str(self.path.parent), str(self.path)):
            if os.path.exists(p) and p not in self.watcher.files() + self.watcher.directories():
                self.watcher.addPath(p)

    def _soon(self, *_) -> None:
        self.timer.start()

    def _reload(self) -> None:
        self._watch()
        if self._load():
            self._install_script()
            self.changed.emit()

    def _load(self) -> bool:
        """Read the file; True when it gave new rules."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            if self._text is not None:
                log.warning("%s is gone; keeping the rules it had", self.path)
                self._text = None
            return False
        except OSError as e:
            log.warning("could not read %s (%s); keeping the rules", self.path, e)
            return False
        if text == self._text:
            return False
        self._text = text
        try:
            data = json.loads(text)
            rules, warnings = Rules.parse(data)
        except ValueError as e:
            log.warning("%s: %s; keeping the rules before it", self.path, e)
            return False
        try:
            agent, more = Rules.parse(data, AGENT_TABLE)
        except ValueError as e:
            agent, more = self.agent_rules, [f"{e}; keeping the agent's rules before it"]
        for w in warnings + more:
            log.warning("%s: %s", self.path, w)
        self.reloads += 1
        if agent != self.agent_rules:
            self.agent_rules = agent
            log.info("sites kept from the agent: %s", self.describe(agent) or "none")
        if rules == self.rules:
            return False
        self.rules = rules
        log.info("site identities: %s", self.describe())
        return True

    def describe(self, rules: Rules | None = None) -> str:
        rules = rules or self.rules
        entries = [f"{h} {i}" for h, i in rules.exact.items()]
        entries += [f"*.{d} {i}" for d, i in rules.domains]
        if rules is not self.rules:
            return ", ".join(entries)
        return ", ".join(entries) or f"none (everything {DEFAULT})"

    def agent_policy(self, host: str) -> str | None:
        """The agent's policy for a host: "never", or None."""
        return self.agent_rules.value_for(host) if host else None

    def _install_script(self) -> None:
        scripts = self.profile.scripts()
        for old in scripts.find(SCRIPT_NAME):
            scripts.remove(old)
        script = QWebEngineScript()
        script.setName(SCRIPT_NAME)
        script.setSourceCode(script_source(self.rules, self.agents))
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        scripts.insert(script)
