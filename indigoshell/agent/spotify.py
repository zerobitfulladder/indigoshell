"""Spotify for the agent, in three layers, each needing more than the last.

- **The app** (`playerctl`, MPRIS on the session bus): what is playing,
  play / pause / skip, start a track, album or playlist by URI. Nothing
  to sign in to; works with the official desktop client.

- **The catalogue** (Web API, the app's own token — the client
  credentials flow): search, and anything public. Needs `client_id` and
  `client_secret` in `~/.config/indigoshell/connections/spotify/config.json`,
  from an app registered at developer.spotify.com.

- **The account** (Web API as the user — the authorization code flow):
  playlists, the queue, liked songs, top and recent tracks, playback on
  any device. Needs a one-time sign-in, `login()`, which opens Spotify's
  own approval page and catches the reply on 127.0.0.1 — so the app's
  settings must list `REDIRECT_URI` (or the `redirect_uri` set in
  that config.json) as a redirect URI. The tokens are kept in
  `~/.local/state/indigoshell/spotify_token.json`, readable only by the
  user, and refreshed as they expire. Queueing and playback control
  through the API need Spotify Premium.

HTTP goes through `httpx2` (an Agent SDK dependency), so tokens and the
client secret never appear on a command line.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
import urllib.parse

from ..core import paths
from . import connections

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
# Where Spotify sends the browser back after the user approves. It must be
# listed under Redirect URIs in the app's settings; this one is the one
# spotify_player's app already registers, so a single Spotify app serves
# both. spotify_player only listens on it while it signs in itself.
# Override with "redirect_uri" in connections/spotify/config.json.
REDIRECT_URI = "http://127.0.0.1:8989/login"
LOGIN_TIMEOUT_S = 240
APP = "spotify"                 # the desktop client's MPRIS name

# Every user scope the Web API offers (the user asked for all of them).
SCOPES = (
    "user-read-playback-state", "user-modify-playback-state",
    "user-read-currently-playing", "app-remote-control", "streaming",
    "playlist-read-private", "playlist-read-collaborative",
    "playlist-modify-private", "playlist-modify-public",
    "user-library-read", "user-library-modify",
    "user-follow-read", "user-follow-modify",
    "user-top-read", "user-read-recently-played",
    "user-read-playback-position", "user-read-private", "user-read-email",
    "ugc-image-upload",
)

_LINK = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?"
                   r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)")


class SpotifyError(Exception):
    """Something the agent should read and act on (sign in, set up)."""


def uri(value: str) -> str | None:
    """A spotify: URI from a URI or an open.spotify.com link."""
    value = value.strip()
    if value.startswith("spotify:"):
        return value
    m = _LINK.search(value)
    return f"spotify:{m.group(1)}:{m.group(2)}" if m else None


def track_line(t: dict) -> str:
    """One line for a track, album, playlist or artist object."""
    if not t:
        return "?"
    who = ", ".join(a.get("name", "") for a in t.get("artists") or [])
    if t.get("type") == "playlist":
        who = (t.get("owner") or {}).get("display_name", "")
    album = (t.get("album") or {}).get("name")
    line = t.get("name", "?")
    if who:
        line += f" — {who}"
    if album and t.get("type") == "track":
        line += f" ({album})"
    return f"{line}  {t.get('uri', '')}".rstrip()


async def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return 1, str(e)
    return p.returncode or 0, out.decode(errors="replace").strip()


class Spotify:
    def __init__(self, *, credentials: str | None = None,
                 tokens: str | None = None) -> None:
        self.credentials = credentials or connections.path("spotify",
                                                           "config.json")
        self.tokens = tokens or os.path.join(paths.state_dir(),
                                             "spotify_token.json")
        self._app_token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    # ── the desktop app, over MPRIS ─────────────────────────────────────
    async def app_running(self) -> bool:
        _, out = await _run(["playerctl", "-l"])
        return APP in out.split()

    async def app_start(self) -> bool:
        from ..services import proc
        if await self.app_running():
            return True
        proc.launch(["spotify"], app_id="spotify")
        for _ in range(40):
            await asyncio.sleep(0.25)
            if await self.app_running():
                await asyncio.sleep(1.0)    # MPRIS answers before it plays
                return True
        return False

    async def app(self, *args: str) -> tuple[int, str]:
        return await _run(["playerctl", "-p", APP, *args])

    # ── setup ───────────────────────────────────────────────────────────
    def _creds(self) -> dict:
        try:
            with open(self.credentials) as f:
                creds = json.load(f)
            creds["client_id"], creds["client_secret"]
        except (OSError, ValueError, KeyError):
            raise SpotifyError(
                f"Spotify Web API is not set up: {self.credentials} needs "
                '{"client_id": ..., "client_secret": ...} from an app at '
                "developer.spotify.com.") from None
        return creds

    def redirect_uri(self) -> str:
        try:
            return self._creds().get("redirect_uri") or REDIRECT_URI
        except SpotifyError:
            return REDIRECT_URI

    def signed_in(self) -> bool:
        return os.path.exists(self.tokens)

    async def _token_request(self, data: dict) -> dict:
        import httpx2
        creds = self._creds()
        async with httpx2.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{ACCOUNTS}/api/token", data=data,
                auth=httpx2.BasicAuth(creds["client_id"],
                                      creds["client_secret"]))
        reply = r.json() if r.content else {}
        if r.status_code != 200 or "access_token" not in reply:
            raise SpotifyError("Spotify refused the token request: "
                               + str(reply.get("error_description")
                                     or reply.get("error") or r.status_code))
        return reply

    # ── the app's token: the catalogue ──────────────────────────────────
    async def app_token(self) -> str:
        if self._app_token and self._app_token[1] > time.time() + 30:
            return self._app_token[0]
        reply = await self._token_request({"grant_type": "client_credentials"})
        self._app_token = (reply["access_token"],
                           time.time() + reply.get("expires_in", 3600))
        return reply["access_token"]

    # ── the user's token: the account ───────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self.tokens) as f:
                return json.load(f)
        except (OSError, ValueError):
            raise SpotifyError(
                "Not signed in to Spotify. Call spotify_login: it opens "
                "Spotify's approval page in the user's browser.") from None

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.tokens), exist_ok=True)
        fd = os.open(self.tokens, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(self.tokens, 0o600)

    async def user_token(self, *, force: bool = False) -> str:
        async with self._lock:
            data = self._load()
            if not force and data.get("expires_at", 0) > time.time() + 60:
                return data["access_token"]
            reply = await self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"]})
            data["access_token"] = reply["access_token"]
            data["expires_at"] = time.time() + reply.get("expires_in", 3600)
            if reply.get("refresh_token"):         # rotated
                data["refresh_token"] = reply["refresh_token"]
            self._save(data)
            return data["access_token"]

    def user_id(self) -> str | None:
        try:
            return self._load().get("user_id")
        except SpotifyError:
            return None

    async def login(self, open_url) -> str:
        """Sign in as the user: open Spotify's approval page with
        `open_url`, catch the redirect on 127.0.0.1, keep the tokens.
        Returns who signed in."""
        import httpx2
        creds = self._creds()
        redirect = self.redirect_uri()
        target = urllib.parse.urlparse(redirect)
        if target.hostname not in ("127.0.0.1", "[::1]", "::1"):
            raise SpotifyError("the redirect URI must be on 127.0.0.1")
        state = secrets.token_urlsafe(16)
        loop = asyncio.get_running_loop()
        got: asyncio.Future = loop.create_future()

        async def handle(reader, writer):
            try:
                line = (await reader.readline()).decode(errors="replace")
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                path = line.split(" ")[1] if " " in line else "/"
                url = urllib.parse.urlparse(path)
                query = dict(urllib.parse.parse_qsl(url.query))
                ok = (url.path == target.path and query.get("state") == state
                      and "code" in query)
                if url.path == target.path and not got.done():
                    if ok:
                        got.set_result(query["code"])
                    elif query.get("state") == state:
                        got.set_exception(SpotifyError(
                            "Spotify sign-in was declined: "
                            + query.get("error", "unknown")))
                body = _PAGE.format(
                    title="Signed in" if ok else "Not signed in",
                    text=("Spotify is connected to indigoshell. You can "
                          "close this tab." if ok else
                          html.escape(query.get("error", "Unexpected request."))))
                writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; "
                              f"charset=utf-8\r\nContent-Length: "
                              f"{len(body.encode())}\r\nConnection: close"
                              "\r\n\r\n" + body).encode())
                await writer.drain()
            finally:
                writer.close()

        try:
            server = await asyncio.start_server(
                handle, target.hostname.strip("[]"), target.port or 80)
        except OSError as e:
            raise SpotifyError(f"cannot listen on {redirect}: {e}") from None
        params = {"client_id": creds["client_id"], "response_type": "code",
                  "redirect_uri": redirect, "scope": " ".join(SCOPES),
                  "state": state, "show_dialog": "true"}
        open_url(f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode(params))
        try:
            code = await asyncio.wait_for(got, LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise SpotifyError(
                "No answer from Spotify's sign-in page. If it showed "
                "'INVALID_CLIENT: Invalid redirect URI', the app's settings "
                f"at developer.spotify.com need {redirect} added under "
                "Redirect URIs.") from None
        finally:
            server.close()
            await server.wait_closed()
        reply = await self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect})
        data = {"access_token": reply["access_token"],
                "refresh_token": reply["refresh_token"],
                "expires_at": time.time() + reply.get("expires_in", 3600),
                "scope": reply.get("scope", "")}
        async with httpx2.AsyncClient(timeout=20) as client:
            r = await client.get(f"{API}/me", headers={
                "Authorization": f"Bearer {data['access_token']}"})
        me = r.json() if r.status_code == 200 else {}
        data["user_id"] = me.get("id")
        self._save(data)
        log.info("spotify: signed in as %s", me.get("id"))
        return f"{me.get('display_name') or me.get('id')} ({me.get('product', '?')})"

    # ── the Web API ─────────────────────────────────────────────────────
    async def api(self, method: str, path: str, *, params: dict | None = None,
                  body=None, user: bool = True) -> tuple[int, object]:
        """Call the Web API, as the user (default) or as the app. Returns
        (status, parsed JSON or text). Refreshes the user's token once on
        a 401."""
        import httpx2
        url = path if path.startswith("http") else API + "/" + path.lstrip("/")
        for attempt in (1, 2):
            token = await (self.user_token(force=attempt == 2) if user
                           else self.app_token())
            async with httpx2.AsyncClient(timeout=20) as client:
                r = await client.request(
                    method.upper(), url, params=params,
                    json=body if body is not None else None,
                    headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and user and attempt == 1:
                continue
            break
        if not r.content:
            return r.status_code, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>indigoshell · {title}</title><style>
body {{ background: #050310; color: #c8d0e8; display: grid;
  place-items: center; height: 100vh; margin: 0;
  font: 16px/1.6 "FiraCode Nerd Font Mono", monospace; }}
div {{ border: 2px solid #b967ff; padding: 24px 32px; background: #170620;
  clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%,
    14px 100%, 0 calc(100% - 14px)); }}
h1 {{ font-size: 20px; margin: 0 0 8px; color: #b967ff; }}
h1::before {{ content: "// "; color: #05d9e8; }}
</style></head><body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""
