"""The browser process: PySide6 + QtWebEngine.

    python -m flux.qt [url ...]

Modules, in the order a start runs through them:

    __main__   arguments; the single-instance check (pure Python, so a
               hand-off to a running browser never loads Qt)
    logs       the log file (<state_dir>/browser.log), unhandled
               exceptions and crashes into it (pure Python)
    instance   that check: the lock, and the hand-off client
    startup    the platform (Wayland's, --software-gl), Chromium's flags,
               Widevine found or copied (pure Python)
    app        the QApplication, the process-wide `Browser` (windows,
               most recently used, the control socket, downloads),
               signals and shutdown
    control    the control socket: later invocations' URLs, and the
               agent's requests (tabs, open, read, screenshot, look, ...)
    reader     reading a page for the agent: reader.js in a script world
               of its own, and pictures of the view
    frames     the page's frames, read and acted in like the page
               (QWebEngineFrame, Qt 6.8)
    script     the agent's own JavaScript: its world, its guard (guard.js)
    profile    the persistent profile (the default one, or --profile's)
               and its settings
    identity   the per-site User-Agent (sites.json)
    window     a top-level window: the tabs, the keys, the fallback line
    watch      the agent driving a tab: the lock, the overlay, dialogs
    effects    the lock shown on the page in the shell's look: its glitch,
               a tint, its scanlines (a Qt Quick overlay, shaders/*.qsb)
    keys       which key does what
    tabs       one page and its view
    bookmarks  bookmarks.json, followed on disk
    link       a window's line to the shell (applink), which shows it in
               its top bar

Import rule: nothing in this package imports skia, or a shell
module that does (`indigoshell.theme` imports skia). Only the pure
`indigoshell.core` modules (naming, paths, log, persona),
`indigoshell.fuzzy` (through places), `indigoshell.applink.protocol` and
`indigoshell.applink.bar` (the app bar protocol's vocabulary),
`flux.protocol`, `flux.profiles`, `flux.places`, `flux.bar` and
`flux.addresses` (its section of the top bar, its address answers),
`flux.uploads` (the places uploads never come from), and `flux.desktop`
(for `--install`). The shell never sees Qt either.
"""
