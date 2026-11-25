"""Zoom kept per site, as Firefox's site-specific zoom.

A zoom the user sets on a page (Ctrl++ or Ctrl+=, Ctrl+-, keypad + and
-, Ctrl+wheel: keys.py, window.py) is kept for the page's site, and
every page of that site gets it from then on, in every tab and window,
the agent's own included: a new tab's (a URL opened again, handed over,
a tab reopened) before it loads, and a tab navigated to the site as its
page arrives (a tab moving to another site takes that site's zoom, 100%
when none is kept; qt/tabs.py has when). Pages of the site already open
follow at once. Ctrl+0, or
stepping back to 100%, forgets the site's zoom. A page with no site
(about:blank, data:, a local file) is zoomed as asked and nothing is
kept. The agent's actions read the zoom wherever they map a point
(control.py), so a zoomed page is no different to them.

The site is the page's host, lowercase, a leading "www." dropped,
without its port. The host, as Firefox keys its zoom (it groups by
hostname): a zoom set on one subdomain stays there, so mail.example.com
and docs.example.com, often different apps at different sizes, keep
their own. "www." does not count, as in Flux's address list
(addresses.py): example.com and www.example.com are one site
everywhere in practice. Not the registrable domain: that takes the
public suffix list, which nothing here has, and would join what the
user zoomed apart. The port is not part of it either, as in Firefox.

The file: `<config_dir>/browser/zoom.json` (`zoom-NAME.json` for a
named profile, profiles.py), next to bookmarks.json, an object a person
can edit, each site's zoom in percent:

    {"news.example": 125, "docs.example": 90}

The browser is the only writer, and replaces the file whole. It keeps
whatever else the file holds (an entry it cannot use) when it writes,
reads it again whenever it changed on disk (a hand edit applies to the
next page), and does not write over a file it cannot parse: a typo in
a hand edit is reported, not lost.

Pure Python: no Qt.
"""

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from flux import places, profiles

log = logging.getLogger(__name__)

MIN_PERCENT, MAX_PERCENT = 25, 500      # Chromium's zoom, both ends (tabs.py ZOOM_STEPS)
SCHEMES = ("http", "https")


def site_of(url: str) -> str | None:
    """The site a page's zoom is kept for (the module docstring); None
    for a page with none."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in SCHEMES or not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") and len(host) > 4 else host


def percent_of(factor: float) -> int:
    return int(round(factor * 100))


def zoom_path(profile: str | None = None) -> Path:
    return Path(profiles.zoom_path(profile))


class ZoomBroken(Exception):
    """The file is there but is not an object: it is not written over."""


def parse(text: str) -> tuple[dict, dict[str, int], list[str]]:
    """(the file's object as it is, the usable sites' percents, what was
    skipped). Raises ValueError when the file is not a JSON object."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the file is not a JSON object of sites and their zoom")
    sites, warnings = {}, []
    for key, value in data.items():
        site = key.strip().lower() if isinstance(key, str) else ""
        if site.startswith("www."):
            site = site[4:]
        if (not site or isinstance(value, bool) or not isinstance(value, (int, float))
                or not MIN_PERCENT <= value <= MAX_PERCENT):
            warnings.append(f"{key!r}: {value!r} is no zoom for a site (a percent, "
                            f"{MIN_PERCENT} to {MAX_PERCENT}); skipped")
            continue
        sites[site] = int(round(value))
    return data, sites, warnings


class ZoomFile:
    """zoom.json: read again when it changed on disk; written whole when
    a site's zoom changes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else zoom_path()
        self.raw: dict = {}             # the file's object, entries it cannot use included
        self.sites: dict[str, int] = {}
        self.broken = False
        self._seen: tuple | None = None     # the file as last read: (mtime_ns, size), or ()

    def load(self) -> bool:
        """Read the file when it changed since it was last read; True when
        the sites changed. A missing file is no site kept; one that does
        not parse keeps the sites before it."""
        try:
            st = os.stat(self.path)
            seen = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            seen = ()
        except OSError as e:
            log.warning("could not read %s (%s); keeping the zoom as it was", self.path, e)
            return False
        if seen == self._seen:
            return False
        self._seen = seen
        if not seen:
            changed = bool(self.sites)
            self.raw, self.sites, self.broken = {}, {}, False
            return changed
        try:
            text = self.path.read_text(encoding="utf-8")
            raw, sites, warnings = parse(text)
        except OSError as e:
            self._seen = None
            log.warning("could not read %s (%s); keeping the zoom as it was", self.path, e)
            return False
        except ValueError as e:
            self.broken = True
            log.warning("%s: %s; keeping the zoom as it was, and not writing over it",
                        self.path, e)
            return False
        for w in warnings:
            log.warning("%s: %s", self.path, w)
        self.broken = False
        changed = sites != self.sites
        self.raw, self.sites = raw, sites
        return changed

    def factor(self, url: str) -> float:
        """The zoom for a page at `url`: its site's, 1.0 when none is kept
        (or it has no site)."""
        self.load()
        site = site_of(url)
        return self.sites.get(site, 100) / 100 if site else 1.0

    def set(self, url: str, factor: float) -> bool:
        """`url`'s site kept at `factor` (forgotten at 100%); False when
        nothing changed (the same already, or a page with no site).
        Raises ZoomBroken, or OSError when the file cannot be written."""
        site = site_of(url)
        if site is None:
            return False
        self.load()                     # a hand edit made since is kept
        if self.broken:
            raise ZoomBroken(f"{self.path} does not parse")
        percent = percent_of(factor)
        if self.sites.get(site, 100) == percent:
            return False
        data = {k: v for k, v in self.raw.items()
                if not (isinstance(k, str) and k.strip().lower().removeprefix("www.") == site)}
        if percent != 100:
            data[site] = percent
        places.write_whole(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        self._seen = None
        self.load()
        return True
