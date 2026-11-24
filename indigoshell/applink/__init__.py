"""The shell's line to its own apps, and the top bar they set.

An indigoshell app is a program of the shell's own that runs as a
process of its own: Flux, the browser (`indigoshell/browser`), now;
later an IDE. Each of its windows connects to the daemon over one unix
socket and says what it is (`{"type": "announce", "app": "flux", "wid":
<X window>}`), then keeps the shell told what it shows (protocol.py).

The daemon side is `service.AppService`, a SERVICES entry: it listens,
keeps one `AppWindow` per announced window (each kind's own subclass,
from the `kinds` it is given), follows the root's `_NET_ACTIVE_WINDOW`
to know which of them has focus, and grabs the keys a kind answers in
the shell (Flux's Ctrl+L, Ctrl+T, Ctrl+M) on its windows.

The top bar shows it (`context.AppContext`, the widget on the bar's
left): the focused app window's own content widget, which its kind
draws however it likes, or nothing when the window with focus is no
app's. A new kind of app brings its own `AppWindow` subclass and is
named in the service's `kinds`; nothing of Flux's has to change.

Import rule: `protocol` is pure Python, for the apps (Qt processes)
to import; `service` and `context` are the daemon's (xcffib, skia) and
never imported from an app.
"""
