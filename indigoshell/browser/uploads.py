"""What the agent may hand a page as files: the refused places, in one list.

Pure Python (os, the shell's `core.paths`): Jack's side of the `upload`
request (browser/qt/control.py) asks `refusal` of every path before a
page gets anything, and so may anyone who wants to know the rule.

The agent uploads with the user's full access and no question asked
(the user's decision), except from where secrets live. A path is
refused when it is not a regular file (a folder, a socket, nothing at
all), lies outside the user's home and outside /tmp, sits inside one of
the places below, or is named like a private key. Every check is made
on the path as given and on what it resolves to through symlinks, so a
link in /tmp to a key, or a home whose .ssh is a link elsewhere, is
caught either way.

The places (`places()`), each under the home:

    .ssh                     SSH keys
    .gnupg                   GnuPG keys (and $GNUPGHOME)
    .local/share/keyrings    the desktop keyrings
    .password-store          pass (and $PASSWORD_STORE_DIR)
    .local/share/rbw         rbw's vault, cache and settings
    .cache/rbw
    .config/rbw
    .mozilla                 Firefox's profiles, the old place and the new
    .config/mozilla

and, where the XDG directories are set elsewhere, the same under them
($XDG_DATA_HOME/keyrings, $XDG_DATA_HOME/rbw, $XDG_CACHE_HOME/rbw,
$XDG_CONFIG_HOME/rbw, $XDG_CONFIG_HOME/mozilla); the shell's own data
directory (Jack's profile with its cookies, its history, everything
else the shell keeps there), Jack's cache, and the shell's connections
(`<config_dir>/connections`).

The names (`secret_name`): `id_*` not ending in `.pub` (id_rsa,
id_ed25519), `*.pem`, `*.key`, and `.credentials.json` (where Claude
keeps a sign-in), in any case.

Everything else under the home, and under /tmp, may go.
"""

import os
import stat

from indigoshell.core.paths import cache_dir, config_dir, data_dir

# Under the home: (relative path, what it holds).
HOME_PLACES = (
    (".ssh", "SSH keys"),
    (".gnupg", "GnuPG keys"),
    (".local/share/keyrings", "the keyrings"),
    (".password-store", "the password store"),
    (".local/share/rbw", "rbw's vault"),
    (".cache/rbw", "rbw's cache"),
    (".config/rbw", "rbw's settings"),
    (".mozilla", "Firefox's profiles"),
    (".config/mozilla", "Firefox's profiles"),
)
# Under the XDG directories, when they are set: (variable, relative, what).
XDG_PLACES = (
    ("XDG_DATA_HOME", "keyrings", "the keyrings"),
    ("XDG_DATA_HOME", "rbw", "rbw's vault"),
    ("XDG_CACHE_HOME", "rbw", "rbw's cache"),
    ("XDG_CONFIG_HOME", "rbw", "rbw's settings"),
    ("XDG_CONFIG_HOME", "mozilla", "Firefox's profiles"),
)
# Named by the environment: (variable, what).
ENV_PLACES = (("GNUPGHOME", "GnuPG keys"), ("PASSWORD_STORE_DIR", "the password store"))
OUTSIDE_HOME = ("/tmp",)        # the one place outside the home files may come from


def home() -> str:
    """The user's home (tests point this at a scratch one)."""
    return os.path.expanduser("~")


def places(home_dir: str | None = None) -> list[tuple[str, str]]:
    """Every refused place: (path, what it holds)."""
    h = home_dir or home()
    out = [(os.path.join(h, rel), what) for rel, what in HOME_PLACES]
    for var, rel, what in XDG_PLACES:
        base = os.environ.get(var)
        if base:
            out.append((os.path.join(base, rel), what))
    for var, what in ENV_PLACES:
        if os.environ.get(var):
            out.append((os.environ[var], what))
    out += [(data_dir(), "the shell's data (Jack's profile, cookies and history)"),
            (os.path.join(cache_dir(), "browser"), "Jack's cache"),
            (os.path.join(config_dir(), "connections"), "the shell's connections")]
    return out


def secret_name(name: str) -> bool:
    """Whether a file's name is a private key's (or a sign-in's)."""
    n = name.lower()
    return ((n.startswith("id_") and not n.endswith(".pub")) or n.endswith((".pem", ".key"))
            or n == ".credentials.json")


def _inside(path: str, place: str) -> bool:
    return path == place or path.startswith(place.rstrip("/") + "/")


def _forms(path: str) -> set[str]:
    """A path as given (absolute, normalized) and as resolved."""
    return {os.path.normpath(os.path.abspath(path)), os.path.realpath(path)}


def refusal(path: str, home_dir: str | None = None) -> str | None:
    """Why `path` may not go to a page, or None when it may."""
    if not isinstance(path, str) or not path.strip():
        return "a path must be a non-empty string"
    path = os.path.expanduser(path) if path.startswith("~/") else path
    if not os.path.isabs(path):
        return f"{path!r} is not an absolute path"
    try:
        st = os.stat(path)
    except OSError as e:
        return f"{path}: {e.strerror.lower() if e.strerror else 'cannot be read'}"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a file" + (" (a folder: give the files in it)"
                                          if stat.S_ISDIR(st.st_mode) else "")
    if not os.access(path, os.R_OK):
        return f"{path} cannot be read"
    forms = _forms(path)
    for form in forms:
        if secret_name(os.path.basename(form)):
            return f"{path} is named like a private key or a sign-in; it is never uploaded"
    h = home_dir or home()
    for place, what in places(h):
        if any(_inside(form, p) for form in forms for p in _forms(place)):
            return f"{path} is in {place} ({what}); nothing there is ever uploaded"
    allowed = _forms(h) | {f for root in OUTSIDE_HOME for f in _forms(root)}
    if not all(any(_inside(form, root) for root in allowed) for form in forms):
        return f"{path} is outside the home and /tmp; only files from those are uploaded"
    return None


def check(paths) -> list[str]:
    """The paths, absolute, if every one may go; ValueError naming the
    first that may not (or what is wrong with the list)."""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise ValueError("`paths` must be a list of file paths")
    out = []
    for p in paths:
        why = refusal(p)
        if why:
            raise ValueError(why)
        out.append(os.path.abspath(os.path.expanduser(p) if p.startswith("~/") else p))
    return out
