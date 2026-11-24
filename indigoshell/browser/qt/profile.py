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
                                     QWebEngineSettings)

from indigoshell.core.paths import cache_dir, data_dir

NAME = "browser"


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
    return profile
