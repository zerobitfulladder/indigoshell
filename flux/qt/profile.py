"""The browser's persistent profile.

Storage in $XDG_DATA_HOME/<APP>/browser, cache in
$XDG_CACHE_HOME/<APP>/browser, storage name "browser", cookies always
kept: exactly what the prototype (prototypes/browser/gauntlet.py) made,
so sign-ins made there carry over. Permissions a site was granted are
stored on disk with it. A named profile (`--profile NAME`,
flux/profiles.py) is the same in browser-NAME, storage name
"browser-NAME": one per browser process.

Qt: QWebEngineProfileBuilder (6.9) makes the profile with its storage
settled before it exists. Without it (6.8, the Pi's Debian) the profile
is made by its storage name and the same settings are set on it at
once, before any page uses it, which is when the docs ask for them.

Every page of every window lives in this profile. It has to outlive
them: deleting it under a live page makes Qt complain ("Release of
profile requested but WebEnginePage still not deleted"), so app.py
deletes the windows first.

Accept-Language: QtWebEngine sends none unless told, and then sends the
value exactly as given, with no weights. Flux tells it what Chrome would
send for the locale Flux runs in (`accept_language`): the locale's
languages (QLocale().uiLanguages(), which follows $LANGUAGE and $LANG)
as language or language-region tags, then English unless English is
already there, weighted as Chrome weights them:
en_US gives "en-US,en;q=0.9", tr_TR "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7".
Pages see the same list unweighted in navigator.languages.
"""

import re

from PySide6 import QtWebEngineCore
from PySide6.QtCore import QLocale
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript, QWebEngineSettings

from flux import profiles

NAME = profiles.BASE            # the default profile's storage name
Builder = getattr(QtWebEngineCore, "QWebEngineProfileBuilder", None)    # Qt 6.9 on

# Pages' scrollbars in the shell's colours: the bar's background for the
# track, the launcher's thumb cyan for the thumb (theme.BAR_BG,
# CYAN_MID, CYAN_BRIGHT; copied, as theme imports skia). Square, no
# arrow buttons. Chromium draws a page's scrollbars itself, white on a
# page that doesn't ask for dark, so only the page's CSS can restyle
# them: a sheet every document adopts as it is created, in every frame,
# from an isolated world the page's scripts can't see. A page that
# styles its own scrollbars later in its own sheets keeps theirs.
TRACK = "#050310"
THUMB = "#05a9c4"
THUMB_HOVER = "#05d9e8"
SCROLLBAR_CSS = f"""
::-webkit-scrollbar {{ width: 10px; height: 10px; background: {TRACK}; }}
::-webkit-scrollbar-track {{ background: {TRACK}; }}
::-webkit-scrollbar-thumb {{ background: {THUMB}; border: 3px solid {TRACK}; }}
::-webkit-scrollbar-thumb:hover {{ background: {THUMB_HOVER}; border-width: 2px; }}
::-webkit-scrollbar-corner {{ background: {TRACK}; }}
::-webkit-scrollbar-button {{ display: none; }}
"""
SCROLLBAR_SCRIPT = "indigoshell-scrollbars"
# Its own world: Flux's reader uses 3, identity the main world.
SCROLLBAR_WORLD = 2


def scrollbar_source() -> str:
    """Adopt the sheet as the document is created (no element in the
    page); where adopting fails, a <style> once there is an element."""
    css = SCROLLBAR_CSS.replace("\\", "\\\\").replace("`", "\\`")
    return f"""(() => {{
  const css = `{css}`;
  try {{
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    return;
  }} catch (e) {{}}
  const put = () => {{
    const style = document.createElement('style');
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  }};
  if (document.documentElement) put();
  else document.addEventListener('DOMContentLoaded', put, {{once: true}});
}})();"""


def install_scrollbars(profile: QWebEngineProfile) -> None:
    scripts = profile.scripts()
    for old in scripts.find(SCROLLBAR_SCRIPT):
        scripts.remove(old)
    script = QWebEngineScript()
    script.setName(SCROLLBAR_SCRIPT)
    script.setSourceCode(scrollbar_source())
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(SCROLLBAR_WORLD)
    script.setRunsOnSubFrames(True)
    scripts.insert(script)


# A language with an optional region (tr, tr-TR, es-419): what Chrome
# sends. Qt's list also has script forms (tr-Latn-TR) and "C".
_TAG = re.compile(r"[A-Za-z]{2,3}(-([A-Za-z]{2}|[0-9]{3}))?")
ENGLISH = ("en-US", "en")


def accept_language(languages: list[str]) -> str:
    """The Accept-Language header Chrome sends for a locale whose
    languages are `languages` (QLocale.uiLanguages()): the plain tags in
    order, English last unless there, the first unweighted and each next
    one 0.1 lower, down to 0.1."""
    tags = []
    for tag in languages:
        if _TAG.fullmatch(tag) and tag not in tags:
            tags.append(tag)
    if not any(t.split("-")[0].lower() == "en" for t in tags):
        tags += [t for t in ENGLISH if t not in tags]
    return ",".join(t if i == 0 else f"{t};q=0.{max(10 - i, 1)}" for i, t in enumerate(tags))


def storage_path(name: str | None = None) -> str:
    return profiles.storage_path(name)


def cache_path(name: str | None = None) -> str:
    return profiles.cache_path(name)


def make_profile(name: str | None = None) -> QWebEngineProfile:
    """The profile `name` (None: the default one), its storage settled."""
    cookies = QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    permissions = QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk
    if Builder is not None:
        builder = Builder()
        builder.setPersistentStoragePath(storage_path(name))
        builder.setCachePath(cache_path(name))
        builder.setPersistentCookiesPolicy(cookies)
        builder.setPersistentPermissionsPolicy(permissions)
        profile = builder.createProfile(profiles.stem(name))
    else:
        profile = QWebEngineProfile(profiles.stem(name))
        profile.setPersistentStoragePath(storage_path(name))
        profile.setCachePath(cache_path(name))
        profile.setPersistentCookiesPolicy(cookies)
        profile.setPersistentPermissionsPolicy(permissions)
    profile.setHttpAcceptLanguage(accept_language(QLocale().uiLanguages()))

    settings = profile.settings()
    A = QWebEngineSettings.WebAttribute
    for attribute in (A.FullScreenSupportEnabled, A.PdfViewerEnabled, A.PluginsEnabled,
                      A.JavascriptCanOpenWindows):
        settings.setAttribute(attribute, True)
    # The user wants scrolling to land at once, not glide (startup.py
    # also turns Chromium's own smooth scrolling off).
    settings.setAttribute(A.ScrollAnimatorEnabled, False)
    install_scrollbars(profile)
    return profile
