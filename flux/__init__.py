"""Flux, the shell's web browser: a program of its own beside the shell
in this checkout, on PySide6 and QtWebEngine (Chromium).

    python -m flux [url ...]          (or `indigoshell browser [url ...]`)

It owns its top-level X windows: one window holds many pages (tabs),
each page from the window's top edge. It is a single instance per
profile: a second invocation hands its URLs to the running one.
`desktop.py` installs its launcher entry, Flux (`python -m flux
--install`).

The shell integrates it, as it will its other apps: while a Flux window
has focus, the shell's top bar shows its tabs, the current page's host
and its load line, drawn with the shell's own skia text, and the shell
answers its Ctrl+L, Ctrl+T and Ctrl+M with its launcher. That half is
the shell's, not this package's: `FluxWindow` in
indigoshell/applink/flux.py, an app kind of applink/ (the shell's line
to its apps).

They talk over a unix socket, one JSON object per line
(indigoshell/applink/protocol.py for the line, `protocol.py` here for
what Flux says on it). Flux is the client: it connects, says it is a
Flux window and which X window, then pushes its state whenever it
changes; the shell answers that it has the window and may send commands
back. The shell grabs Ctrl+L, Ctrl+T and Ctrl+M on Flux's main windows
and answers them with its launcher, which lists addresses (history and
bookmarks) or the window's actions. Either side can start, stop or
restart at any time. With no shell for a few seconds Flux writes the
same information into a line at its window's top, and with none at all
those keys reach it and it asks for addresses with a dialog of its own,
so it is usable alone.

`places.py` is the module both sides read: what a typed line means, the
history (SQLite in the data dir, written by Flux, read by the shell's
launcher) and the bookmarks (bookmarks.json in the config dir,
likewise). `profiles.py` names a profile's places: the default one's,
and those of a Flux run with `--profile NAME` beside it. Flux keeps the
shell's names for its places, sockets and environment (indigoshell's
`core.naming`): its data is under the shell's data dir, as before it
had a package of its own.

Headless (`--headless`, protocol.py "Headless"): Flux with no screen,
for a machine with no display (a Raspberry Pi lending the agent its own
Flux): drawn in memory, no line to the shell, nothing that asks the
user; the control socket works as on screen.

The agent's hands. Flux's single-instance socket is also its control
socket (`protocol.py` has every request; `qt/control.py` Flux's side):
the hub's agent lists, opens, switches, reads and screenshots tabs
through it, with `control.py` as the client (pure asyncio; it starts
Flux when none runs) and `hands.py` as the tools a machine lends
(`browser_hands()`, or `browser_hands(profile=..., headless=True)` for a
machine's own headless Flux), made of indigoshell/core/hands.py's
`Hand`. `control.look()` is the call's: what the user is looking at in
Flux, as content blocks. The agent also has a Flux window of its own,
placed by the shell on a workspace the user is not looking at, where it
borrows the user's tabs and returns them (protocol.py, "Alone"); it
shows the user local files, lists downloads and uploads files, never
from the places `uploads.py` lists.

Import rule (tests/flux_imports.py checks it): of the shell, Flux
imports only its pure modules: `indigoshell.core`'s (naming, paths,
log, hands), `indigoshell.applink.protocol`, `indigoshell.fuzzy` and
`indigoshell.services.proc` (to start Flux detached). So it runs where
none of the shell's own dependencies (xcffib, skia, dbus-fast, psutil)
are installed. Nothing outside `qt/` imports PySide6, and nothing in
the shell imports `flux.qt`: the shell takes `protocol`, `places`,
`control` and `hands` from here, all pure.
"""
