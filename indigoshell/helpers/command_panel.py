"""Demo command panel.

Exports `WINDOW` (a `PopupKind`) ready to drop into the daemon's `WINDOWS`
registry. The popup name is `command-panel`; bind any key in your WM to
`indigoshell toggle command-panel` to bring it up.

Acts as a live example of every panel item type — Label, Divider, Card,
Row, Action, Toggle, Submenu, Value, Meter, Embed.
"""

import psutil

from .. import theme
from ..api import toast
from ..core.daemon import get_daemon
from ..services import proc
from ..widgets import (
    Action,
    BatteryMeter,
    Card,
    Divider,
    Embed,
    Label,
    Meter,
    Panel,
    Row,
    Screen,
    Submenu,
    Toggle,
    Value,
)
from ..windows.popup import PopupKind


POPUP_NAME = "command-panel"

# Module-level state so toggles persist across opens.
_state = {"verbose": False, "auto_update": True, "dark_mode": True}


def _notify(summary: str, body: str = "", urgency: str = "normal"):
    """Return a no-arg callable that fires a desktop notification toast."""
    return toast(["notify-send", "-u", urgency, summary, body])


def _open_then_close(target: str):
    """Close this panel, then open another popup by name."""
    def go():
        d = get_daemon()
        d.close(POPUP_NAME)
        d.open(target)
    return go


def _close():
    get_daemon().close(POPUP_NAME)


# ── Screens ─────────────────────────────────────────────────────────────

_notifications = Screen("Notifications", items=[
    Label("Send a desktop notification", style="muted"),
    Action("Hello toast",  on_activate=_notify("IndigoShell", "Hello from the panel"), key="1"),
    Action("Battery info", on_activate=_notify("Battery", "Charge nominal"),           key="2"),
    Action("Critical!",
           on_activate=_notify("System", "Something happened", "critical"), key="3"),
    Divider(),
    Label("Backspace to go back", style="muted"),
])

_toggles = Screen("Toggles", items=[
    Card("Demo flags", items=[
        Toggle("Verbose logging",
               get=lambda: _state["verbose"],
               set=lambda v: _state.__setitem__("verbose", v), key="v"),
        Toggle("Auto-update",
               get=lambda: _state["auto_update"],
               set=lambda v: _state.__setitem__("auto_update", v), key="a"),
        Toggle("Dark mode",
               get=lambda: _state["dark_mode"],
               set=lambda v: _state.__setitem__("dark_mode", v), key="d"),
    ]),
    Divider(),
    Row(cells=[
        Label("Verbose:", style="muted"),
        Value(get=lambda: "on" if _state["verbose"] else "off",
              color=lambda: theme.CYAN_BRIGHT if _state["verbose"] else theme.FG_MUTED),
    ]),
    Row(cells=[
        Label("Auto-update:", style="muted"),
        Value(get=lambda: "on" if _state["auto_update"] else "off",
              color=lambda: theme.CYAN_BRIGHT if _state["auto_update"] else theme.FG_MUTED),
    ]),
])

_windows = Screen("Windows", items=[
    Label("Switch to another panel", style="muted"),
    Action("Hardware panel", on_activate=_open_then_close("hardware"),   key="h"),
    Action("Network panel",  on_activate=_open_then_close("network"),    key="n"),
    Action("Calendar",       on_activate=_open_then_close("calendar"),   key="c"),
    Action("Power menu",     on_activate=_open_then_close("power-menu"), key="p"),
])

_embed = Screen("Embedded widgets", items=[
    Label("BatteryMeter, dropped in as-is", style="muted"),
    Embed(BatteryMeter()),
    Divider(),
    Label("Click rows or use j/k + Enter", style="muted"),
])

_root = Screen("Command", items=[
    Label("DEMO PANEL", style="heading"),
    Card("Live system", items=[
        Row(cells=[
            Label("CPU "),
            Meter(get=lambda: psutil.cpu_percent(interval=None), color=theme.CYAN_BRIGHT),
            Value(get=lambda: f"{psutil.cpu_percent(interval=None):.0f}%",
                  color=theme.MAGENTA_BRIGHT),
        ]),
        Row(cells=[
            Label("RAM "),
            Meter(get=lambda: psutil.virtual_memory().percent, color=theme.LIME_BRIGHT),
            Value(get=lambda: f"{psutil.virtual_memory().percent:.0f}%",
                  color=theme.MAGENTA_BRIGHT),
        ]),
    ]),
    Divider(),
    Submenu("Notifications…", target=_notifications, key="n"),
    Submenu("Toggles…",       target=_toggles,       key="t"),
    Submenu("Other panels…",  target=_windows,       key="w"),
    Submenu("Embed demo…",    target=_embed,         key="e"),
    Divider(),
    Action("Reload shell",
           on_activate=lambda: proc.fire(["indigoshell", "reload"], detach=True),
           key="r"),
    Action("Close panel", on_activate=_close, key="q"),
])


WINDOW = PopupKind(
    name=POPUP_NAME,
    content=Panel(popup_name=POPUP_NAME, root=_root),
    corner="top-right",
    corner_margin=(theme.BAR_MARGIN + 8, theme.BAR_MARGIN + 8),
    grab=True,
    persistent=False,
    padding=18,
)
