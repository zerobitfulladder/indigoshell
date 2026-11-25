"""Flux, the shell's web browser: a program of its own beside the shell
in this checkout, on PySide6 and QtWebEngine (Chromium).

    python -m flux [url ...]          (or `indigoshell browser [url ...]`)

It owns its top-level windows (X11's, or Wayland's under niri, where Qt
runs on its Wayland platform: qt/startup.py): one window holds many
pages (tabs), each page from the window's top edge. It is a single instance per
profile: a second invocation hands its URLs to the running one.
`desktop.py` installs its desktop entry, Flux (`python -m flux
--install`), which the launcher finds.

The shell integrates it, as it will its other apps: Flux is an
indigoshell app (indigoshell/applink/PROTOCOL.md, the app bar protocol).
Each window connects to the shell's app socket, announces itself (the
app, its own id, its X window; on Wayland no handle, and it says
whether it has the focus) and sends its section of the shell's top
bar whole whenever it changes: `bar.py` makes it of the window's state
(its tabs, the page's host, the `address`, `new_tab` and `find` inputs,
the `actions` menu). The shell draws it with its own look, takes the
inputs' and menu's chords (Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M) from the
window (on Wayland the window catches them and hands them over, with
what is typed before the shell has the keyboard), and sends back what
the user does, by id: a tab selected,
closed, moved or torn out, an input's query (answered from Flux's places
by `addresses.py`, or with find's count), a submit, a pick, an input
closed. Nothing of Flux's is in the shell. Either side can start, stop
or restart at any time. With no shell for a few seconds Flux writes the
same information into a line at its window's top, and with none at all
(or one that cannot show an input: `fallback`) those keys reach it and
it asks for addresses with a dialog of its own and finds over the page
itself, so it is usable alone.

`places.py` is the module both halves read: what a typed line means,
the history (SQLite in the data dir) and the bookmarks (bookmarks.json
in the config dir), written by Flux's windows and read for its address
answers. `profiles.py` names a profile's places: the default one's,
and those of a Flux run with `--profile NAME` beside it. Flux keeps the
shell's names for its places, sockets and environment (indigoshell's
`core.naming`): its data is under the shell's data dir, as before it
had a package of its own.

Flux always runs on a screen. Unattended (`--unattended`, protocol.py
"Unattended"): the agent's own Flux on a machine that lends it (the
hub's, shown on a bare compositor, cage, with no shell): what would ask
a person is answered at once, its pages are muted, and it outlives its
last window; the control socket works as anywhere.

The agent's hands. Flux's single-instance socket is also its control
socket (`protocol.py` has every request; `qt/control.py` Flux's side):
the hub's agent lists, opens, switches, reads and screenshots tabs
through it, with `control.py` as the client (pure asyncio; it starts
Flux when none runs) and `hands.py` as the tools a machine lends
(`browser_hands(where=...)`, or `browser_hands(profile=..., where=...,
unattended=True, start_env=...)` for a machine's own unattended Flux),
made of indigoshell/core/hands.py's
`Hand`. `control.look()` is the call's: what the user is looking at in
Flux, as content blocks. The agent also has a Flux window of its own,
placed by the shell on a workspace the user is not looking at, where it
borrows the user's tabs and returns them (protocol.py, "Alone"); it
shows the user local files, lists downloads and uploads files, never
from the places `uploads.py` lists.

Import rule (tests/flux_imports.py checks it): of the shell, Flux
imports only its pure modules: `indigoshell.core`'s (naming, paths,
log, hands, persona), `indigoshell.applink.protocol` and
`indigoshell.applink.bar`, `indigoshell.fuzzy` and
`indigoshell.services.proc` (to start Flux detached). So it runs where
none of the shell's own dependencies (xcffib, skia, dbus-fast, psutil)
are installed. Nothing outside `qt/` imports PySide6, and nothing in
the shell imports `flux.qt`: the shell takes `control` and `hands`
from here (the agent's), both pure, and nothing else of Flux's.
"""
