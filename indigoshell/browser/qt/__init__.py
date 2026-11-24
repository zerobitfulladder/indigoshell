"""The browser process: PySide6 + QtWebEngine.

    python -m indigoshell.browser.qt [url ...]

Modules, in the order a start runs through them:

    __main__   arguments; the single-instance check (pure Python, so a
               hand-off to a running browser never loads Qt)
    logs       the log file (<state_dir>/browser.log), unhandled
               exceptions and crashes into it (pure Python)
    instance   that check: the lock, and the hand-off client
    startup    Chromium's flags, Widevine found or copied (pure Python)
    app        the QApplication, the process-wide `Browser` (windows,
               most recently used, the control socket, downloads),
               signals and shutdown
    control    the control socket: later invocations' URLs, and the
               agent's requests (tabs, open, read, screenshot, look, ...)
    reader     reading a page for the agent: reader.js in a script world
               of its own, and pictures of the view
    profile    the one persistent profile and its settings
    identity   the per-site User-Agent (sites.json)
    window     a top-level window: the tabs, the keys, the fallback line
    keys       which key does what
    tabs       one page and its view
    bookmarks  bookmarks.json, followed on disk
    link       a window's line to the shell (applink), which shows it in
               its top bar

Import rule: nothing in this package imports xcffib, skia, or a shell
module that does (`indigoshell.theme` imports skia). Only the pure
`indigoshell.core` modules (naming, paths, log), `indigoshell.fuzzy`
(through places), `indigoshell.applink.protocol`,
`indigoshell.browser.protocol`,
`indigoshell.browser.places`, and `indigoshell.browser.desktop` (for
`--install`). The X side (`browser/context.py`) never sees Qt either.
"""
