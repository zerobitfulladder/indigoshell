"""User-facing helpers for config files.

These return click-handlers (or plain callables) that talk to the running
daemon. Use them in widget event slots:

    from indigoshell.api import toggle, open_window, close_window

    widget_media = Media(
        ...,
        on_left_click=toggle("spotify-player"),
    )

Handlers are tagged with `_indigo_popup_name` so the daemon can discover
which bar widget anchors each popup — this lets `indigoshell open <name>`
from the CLI position the popup as if the bar widget had been clicked.
"""

from .core.daemon import get_daemon


def toggle(name: str):
    def handler(source):
        gtk_widget = getattr(source, "gtk_widget", None)
        get_daemon().toggle(name, anchor=gtk_widget)
    handler._indigo_popup_name = name
    return handler


def open_window(name: str):
    def handler(source):
        gtk_widget = getattr(source, "gtk_widget", None)
        get_daemon().open(name, anchor=gtk_widget)
    handler._indigo_popup_name = name
    return handler


def close_window(name: str):
    def handler(_source):
        get_daemon().close(name)
    handler._indigo_popup_name = name
    return handler


def toast(command: list[str], *, cols: int = 80, rows: int = 20, linger_ms: int | None = None):
    """Click/menu handler that spawns a top-right Toast popup running
    `command`; auto-closes after the command exits via a perimeter-trace
    animation. Use in `MenuItem(..., toast([...]))` or widget on_* slots."""
    def handler(_source=None):
        get_daemon().toast(command, cols=cols, rows=rows, linger_ms=linger_ms)
    return handler
