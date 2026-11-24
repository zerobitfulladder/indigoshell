"""The shell's web browser, Jack: a QtWebEngine process, and what the
shell shows of it in its top bar.

Two halves, two processes, two toolkits:

- `browser.qt` is the browser itself, its own process on PySide6 and
  QtWebEngine (Chromium). It owns the top-level X windows: one window
  holds many pages (tabs), each page from the window's top edge. Run it
  as `python -m indigoshell.browser.qt [url ...]`. It is a single
  instance: a second invocation hands its URLs to the running one.

- `context.py` is the shell's: while a browser window has focus, the
  shell's top bar shows its tabs, the current page's host and its load
  line, drawn with the shell's own skia text (`JackWindow`, an app kind
  of applink/, the shell's line to its own apps). It also opens the
  shell's launcher for the browser's Ctrl+L, Ctrl+T and Ctrl+M.

`indigoshell browser [url ...]` runs the browser from the shell's CLI,
and `desktop.py` installs its launcher entry, Jack.

They talk over a unix socket, one JSON object per line
(applink/protocol.py for the line, `protocol.py` here for what Jack
says on it). The browser is the client: it connects, says it is a Jack
window and which X window, then pushes its state whenever it changes;
the shell answers that it has the window and may send commands back.
The shell grabs Ctrl+L, Ctrl+T and Ctrl+M on the browser's main windows
and answers them with its launcher, which lists addresses (history and
bookmarks) or the window's actions. Either side can start, stop or
restart at any time. With no shell for a few seconds the browser writes
the same information into a line at its window's top, and with none at
all those keys reach it and it asks for addresses with a dialog of its
own, so it is usable alone.

`places.py` is the other module both halves use: what a typed line
means, the history (SQLite in the data dir, written by the browser,
read by the launcher) and the bookmarks (bookmarks.json in the config
dir, likewise).

The agent's hands. The browser's single-instance socket is also its
control socket (`protocol.py` has every request; `qt/control.py` the
browser's side): the hub's agent lists, opens, switches, reads and
screenshots tabs through it, with `control.py` as the client (pure
asyncio; it starts the browser when none runs) and `hands.py` as the
tools the laptop lends (`browser_hands()`). `control.look()` is the
call's: what the user is looking at in the browser, as content blocks.

The two halves must stay apart: nothing under `browser/qt/` imports
xcffib, skia or a shell module that does (only the pure ones: `core/`'s,
`fuzzy`, `applink.protocol`, and `protocol`, `places` and `desktop`
here), and nothing the daemon imports pulls in PySide6: `context.py`
takes only `protocol` and `places` from this package, `control.py` only
`protocol` (and `desktop` and `services.proc` to start the browser),
and `hands.py` `control`, `protocol` and the agent's `hands`.
"""
