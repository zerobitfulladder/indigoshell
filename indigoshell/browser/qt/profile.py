"""The browser's one persistent profile.

Storage in $XDG_DATA_HOME/<APP>/browser, cache in
$XDG_CACHE_HOME/<APP>/browser, storage name "browser", cookies always
kept: exactly what the prototype (prototypes/browser/gauntlet.py) made,
so sign-ins made there carry over. Permissions a site was granted are
stored on disk with it.

Every page of every window lives in this profile. It has to outlive
them: deleting it under a live page makes Qt complain ("Release of
profile requested but WebEnginePage still not deleted"), so app.py
deletes the windows first.
"""

import os

from PySide6.QtWebEngineCore import (QWebEngineProfile, QWebEngineProfileBuilder,
                                     QWebEngineScript, QWebEngineSettings)

from indigoshell.core.paths import cache_dir, data_dir

NAME = "browser"

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


def storage_path() -> str:
    return os.path.join(data_dir(), NAME)


def cache_path() -> str:
    return os.path.join(cache_dir(), NAME)


def make_profile() -> QWebEngineProfile:
    builder = QWebEngineProfileBuilder()
    builder.setPersistentStoragePath(storage_path())
    builder.setCachePath(cache_path())
    builder.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    builder.setPersistentPermissionsPolicy(
        QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk)
    profile = builder.createProfile(NAME)

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
