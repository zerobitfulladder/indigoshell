"""The shell's line to its own apps, and the top bar they set.

An indigoshell app is a program of the shell's own that runs as a
process of its own: Flux, the browser (flux/), now; later a text editor.
Each of its windows connects to the daemon over one unix socket
(protocol.py), says what it is (`announce`: the app, its own id for the
window, its X window), and sends the shell its section of the top bar
whole whenever it changes (`bar`): tabs, text and buttons, and the
inputs and menus that show only when opened. PROTOCOL.md is the
reference; bar.py its vocabulary, pure, for the apps to import.

The shell owns every kind's look, motion and input: the app describes,
the shell renders, and sends the user's doings back as events naming
the app's own ids. Nothing in this package is any app's.

    service.py   AppService, a SERVICES entry: the socket, one AppWindow
                 per announced window, focus (`_NET_ACTIVE_WINDOW`), the
                 bars checked and shown, the chords of their inputs and
                 menus grabbed, the input or menu open
    context.py   AppContext, the top bar's area for the focused app
    view.py      BarView, a window's section: its items by id
    kinds/       the items (tabs, text, button) and the open input's box
    prompt.py    an open input or menu: its keys, queries, list
    drop.py      the drop-down under the bar (the "launcher" window)
    catalog.py   MOD+D's app search, the same input fed by the app catalog

Import rule: `protocol` and `bar` are pure Python, for the apps (Qt
processes) to import; everything else here is the daemon's (xcffib,
skia) and never imported from an app.
"""
