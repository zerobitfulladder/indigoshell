"""com.canonical.dbusmenu walker.

Most StatusNotifier apps (Steam, Discord, Telegram, nm-applet, blueman,
udiskie, ...) don't implement `Activate`/`ContextMenu` on the item.
They expose a menu instead, exported via this separate spec. The
Item's `Menu` property is a D-Bus object path on the same bus name.

v1 turned that layout into a `Gtk.Menu`; here there is no toolkit, so
the walker returns plain `MenuNode` data and `widgets/systray.TrayMenu`
owns the rendering. The fetch is two awaits (AboutToShow, GetLayout) on
the daemon's loop — no sync round-trips blocking a frame.

Limitations, unchanged from v1's first cut:
  - `LayoutUpdated` / `ItemsPropertiesUpdated` signals are ignored
    while the menu is open. If the app mutates the layout mid-display
    you'll see the snapshot. Closing and reopening shows the new state.
  - `accessible-desc`, `shortcut` properties are read but unused.
  - Icons inside menu items aren't rendered.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from dbus_fast import Message, MessageType, Variant, unpack_variants
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

DBUSMENU_IFACE = "com.canonical.dbusmenu"
_GET_LAYOUT_TIMEOUT = 1.5
_ABOUT_TO_SHOW_TIMEOUT = 0.5
_EVENT_TIMEOUT = 1.0


@dataclass
class MenuNode:
    """One menu entry. `children` is non-empty for submenus."""
    id: int
    label: str = ""
    enabled: bool = True
    separator: bool = False
    toggle_type: str = ""       # "" | "checkmark" | "radio"
    toggle_state: int = 0       # 0 off, 1 on, -1 indeterminate
    has_submenu: bool = False
    children: list["MenuNode"] = field(default_factory=list)


async def fetch_layout(bus: MessageBus, bus_name: str,
                       menu_path: str) -> list[MenuNode] | None:
    """Fetch the layout and return its top-level nodes. Returns None if
    the menu couldn't be retrieved (item gone, bad path, ...)."""
    if not menu_path or menu_path == "/":
        return None
    try:
        # Tell the app we're about to display the menu so it can refresh
        # dynamic items (e.g. nm-applet's network list). Errors here are
        # advisory — proceed even if AboutToShow fails.
        await asyncio.wait_for(bus.call(Message(
            destination=bus_name, path=menu_path, interface=DBUSMENU_IFACE,
            member="AboutToShow", signature="i", body=[0])),
            _ABOUT_TO_SHOW_TIMEOUT)
    except Exception:
        pass
    try:
        reply = await asyncio.wait_for(bus.call(Message(
            destination=bus_name, path=menu_path, interface=DBUSMENU_IFACE,
            member="GetLayout", signature="iias", body=[0, -1, []])),
            _GET_LAYOUT_TIMEOUT)
    except Exception:
        return None
    if (reply is None or reply.message_type != MessageType.METHOD_RETURN
            or len(reply.body) < 2):
        return None
    # (u(ia{sv}av)) — the root node's children are the menu entries.
    root = unpack_variants(reply.body[1])
    _root_id, _root_props, children = root
    nodes = [n for n in (_build_node(c) for c in children) if n is not None]
    return nodes or None


def _build_node(node) -> MenuNode | None:
    item_id, props, children = node
    if not props.get("visible", True):
        return None
    if props.get("type") == "separator":
        return MenuNode(id=item_id, separator=True)
    out = MenuNode(
        id=item_id,
        label=props.get("label", "") or "",
        enabled=bool(props.get("enabled", True)),
        toggle_type=props.get("toggle-type", "") or "",
        toggle_state=int(props.get("toggle-state", 0) or 0),
    )
    out.children = [n for n in (_build_node(c) for c in children)
                    if n is not None]
    out.has_submenu = (bool(out.children)
                       or props.get("children-display") == "submenu")
    return out


async def send_event(bus: MessageBus, bus_name: str, menu_path: str,
                     item_id: int, event_id: str) -> None:
    """Tell the app an event happened on a menu item. We pass int(0) for
    the data payload — the spec lets apps ignore it for `clicked`."""
    try:
        await asyncio.wait_for(bus.call(Message(
            destination=bus_name, path=menu_path, interface=DBUSMENU_IFACE,
            member="Event", signature="isvu",
            body=[item_id, event_id, Variant("i", 0), int(time.time())])),
            _EVENT_TIMEOUT)
    except Exception:
        log.debug("dbusmenu event failed", exc_info=True)
