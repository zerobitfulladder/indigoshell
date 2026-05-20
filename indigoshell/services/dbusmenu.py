"""com.canonical.dbusmenu walker.

Most StatusNotifier apps (Steam, Discord, Telegram, nm-applet, blueman,
udiskie, ...) don't implement `Activate`/`ContextMenu` on the item.
They expose a menu instead, exported via this separate spec. The
Item's `Menu` property is a D-Bus object path on the same bus name.

This module turns that menu into a `Gtk.Menu` you can `popup_at_pointer`
right now. The build is synchronous (one round-trip for AboutToShow, one
for GetLayout) — works fine in practice and saves the deferred-popup
choreography.

Limitations of this first cut:
  - `LayoutUpdated` / `ItemsPropertiesUpdated` signals are ignored
    while the menu is open. If the app mutates the layout mid-display
    you'll see the snapshot. Closing and reopening shows the new state.
  - `accessible-desc`, `shortcut` properties are read but unused.
  - Icons inside menu items aren't rendered yet (GTK 3 makes that
    semi-deprecated anyway). Easy to add later.
"""

import time
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk

DBUSMENU_IFACE = "com.canonical.dbusmenu"
_GET_LAYOUT_TIMEOUT_MS = 1500
_ABOUT_TO_SHOW_TIMEOUT_MS = 500


def build_menu(
    conn: Gio.DBusConnection,
    bus_name: str,
    menu_path: str,
) -> Gtk.Menu | None:
    """Fetch the layout and turn it into a Gtk.Menu. Returns None if
    the menu couldn't be retrieved (item gone, bad path, ...)."""
    if not menu_path or menu_path == "/":
        return None
    try:
        # Tell the app we're about to display the menu so it can refresh
        # dynamic items (e.g. nm-applet's network list). Errors here are
        # advisory — proceed even if AboutToShow fails.
        conn.call_sync(
            bus_name, menu_path, DBUSMENU_IFACE, "AboutToShow",
            GLib.Variant("(i)", (0,)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE, _ABOUT_TO_SHOW_TIMEOUT_MS, None,
        )
    except GLib.Error:
        pass
    try:
        reply = conn.call_sync(
            bus_name, menu_path, DBUSMENU_IFACE, "GetLayout",
            GLib.Variant("(iias)", (0, -1, [])),
            GLib.VariantType.new("(u(ia{sv}av))"),
            Gio.DBusCallFlags.NONE, _GET_LAYOUT_TIMEOUT_MS, None,
        )
    except GLib.Error:
        return None
    _revision, root = reply.unpack()
    _root_id, _root_props, children = root
    if not children:
        return None
    menu = Gtk.Menu()
    for child in children:
        item = _build_item(child, conn, bus_name, menu_path)
        if item is not None:
            menu.append(item)
    menu.show_all()
    return menu


def _build_item(
    node: tuple[int, dict[str, Any], list],
    conn: Gio.DBusConnection,
    bus_name: str,
    menu_path: str,
) -> Gtk.MenuItem | None:
    item_id, props, children = node
    if not props.get("visible", True):
        return None
    if props.get("type") == "separator":
        return Gtk.SeparatorMenuItem()

    # Mnemonic underscores: spec uses `_` as the accelerator marker.
    # Gtk.MenuItem.new_with_mnemonic understands the same convention,
    # so pass the raw label through unchanged.
    label = props.get("label", "") or ""

    toggle = props.get("toggle-type", "")
    state  = int(props.get("toggle-state", 0) or 0)
    if toggle == "checkmark":
        gtk_item = Gtk.CheckMenuItem.new_with_mnemonic(label)
        gtk_item.set_active(state == 1)
        gtk_item.set_inconsistent(state == -1)
    elif toggle == "radio":
        gtk_item = Gtk.RadioMenuItem.new_with_mnemonic([], label)
        gtk_item.set_active(state == 1)
    else:
        gtk_item = Gtk.MenuItem.new_with_mnemonic(label)

    gtk_item.set_sensitive(bool(props.get("enabled", True)))

    has_submenu = (
        bool(children)
        or props.get("children-display") == "submenu"
    )
    if has_submenu and children:
        sub = Gtk.Menu()
        for child in children:
            child_item = _build_item(child, conn, bus_name, menu_path)
            if child_item is not None:
                sub.append(child_item)
        sub.show_all()
        gtk_item.set_submenu(sub)
    else:
        gtk_item.connect(
            "activate",
            lambda _w, _id=item_id: _send_event(conn, bus_name, menu_path, _id, "clicked"),
        )
    return gtk_item


def _send_event(
    conn: Gio.DBusConnection,
    bus_name: str,
    menu_path: str,
    item_id: int,
    event_id: str,
) -> None:
    """Tell the app an event happened on a menu item. We pass int(0) for
    the data payload — the spec lets apps ignore it for `clicked`."""
    try:
        conn.call(
            bus_name, menu_path, DBUSMENU_IFACE, "Event",
            GLib.Variant("(isvu)", (
                item_id, event_id,
                GLib.Variant("i", 0),
                int(time.time()),
            )),
            None, Gio.DBusCallFlags.NONE, 1000, None, None,
        )
    except GLib.Error:
        pass
