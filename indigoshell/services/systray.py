"""StatusNotifierItem broker — Watcher + Host for D-Bus tray icons.

What this module does (in plain English):

1. **Watcher role** — claims the well-known session-bus name
   `org.kde.StatusNotifierWatcher`. SNI-aware apps look for this name to
   register their tray items. If something else already owns the name
   (KDE plasma, snixembed, ...) we just give up the role and become a
   pure host. If nothing else owns it, we run the watcher ourselves
   from this same process, which is the common case on a minimal WM.

2. **Host role** — registers as a `StatusNotifierHost-<pid>` on the bus
   and subscribes to the watcher's add/remove signals. When a new item
   appears we connect to its bus name, fetch its current properties
   (icon, tooltip, status, menu path, ...), subscribe to its `New*`
   change signals, and store it in `self._items`.

3. **Pub/sub** — bar widgets subscribe via `.subscribe(...)` to be
   notified when items are added, removed, or change. The widget owns
   rendering; this module is a pure data broker — same pattern as
   `services/music.py` and `services/beat.py`.

4. **Click dispatch** — exposes `activate`, `secondary_activate`,
   `context_menu`, and `scroll` so the renderer can forward user input
   to the item's D-Bus methods.

What v1 does NOT do yet:
  - DBusMenu (right-click menus). For now we call `ContextMenu(x,y)` on
    the item, which works for apps that still implement the legacy
    fallback (most still do). Apps that only export DBusMenu will get
    no menu until we add a DBusMenu walker.
  - NewAttentionIcon / NeedsAttention pulsing. Status is tracked but
    the renderer treats Active and NeedsAttention identically.
  - IconThemePath honoring (apps shipping non-standard icon dirs).
"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib


# ── D-Bus constants ──────────────────────────────────────────────────
WATCHER_BUS      = "org.kde.StatusNotifierWatcher"
WATCHER_PATH     = "/StatusNotifierWatcher"
ITEM_IFACE       = "org.kde.StatusNotifierItem"
ITEM_PATH        = "/StatusNotifierItem"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DBUS_BUS         = "org.freedesktop.DBus"
DBUS_PATH        = "/org/freedesktop/DBus"
DBUS_IFACE       = "org.freedesktop.DBus"


# Watcher interface XML — only consulted when we run the watcher
# ourselves (i.e. the bus name was free when we started).
_WATCHER_XML = """
<node>
  <interface name='org.kde.StatusNotifierWatcher'>
    <method name='RegisterStatusNotifierItem'>
      <arg type='s' name='service' direction='in'/>
    </method>
    <method name='RegisterStatusNotifierHost'>
      <arg type='s' name='service' direction='in'/>
    </method>
    <property name='RegisteredStatusNotifierItems' type='as' access='read'/>
    <property name='IsStatusNotifierHostRegistered' type='b' access='read'/>
    <property name='ProtocolVersion' type='i' access='read'/>
    <signal name='StatusNotifierItemRegistered'>
      <arg type='s' name='service'/>
    </signal>
    <signal name='StatusNotifierItemUnregistered'>
      <arg type='s' name='service'/>
    </signal>
    <signal name='StatusNotifierHostRegistered'/>
    <signal name='StatusNotifierHostUnregistered'/>
  </interface>
</node>
"""


@dataclass
class TrayItem:
    """One tray icon. The broker keeps these up to date; the widget
    reads them in its draw path. `bus_name` is the unique key."""
    bus_name: str
    path: str = ITEM_PATH
    id: str = ""
    title: str = ""
    status: str = "Active"          # Active | Passive | NeedsAttention
    icon_name: str = ""
    icon_pixmaps: list[tuple[int, int, bytes]] = field(default_factory=list)
    attention_icon_name: str = ""
    attention_icon_pixmaps: list[tuple[int, int, bytes]] = field(default_factory=list)
    overlay_icon_name: str = ""
    overlay_icon_pixmaps: list[tuple[int, int, bytes]] = field(default_factory=list)
    tooltip_title: str = ""
    tooltip_body: str = ""
    menu_path: str = ""
    icon_theme_path: str = ""
    item_is_menu: bool = False


class TrayBroker:
    """Singleton — see `get_broker()`. Lifecycle:

        broker.start()                # claim watcher, register host, subscribe
        broker.subscribe(on_add, on_remove, on_change)
        broker.activate(bus_name, x, y)      # forward clicks
        broker.stop()                 # release names, unsubscribe
    """

    def __init__(self) -> None:
        self._items: dict[str, TrayItem] = {}
        self._listeners: list[tuple[Callable, Callable, Callable]] = []
        self._conn: Gio.DBusConnection | None = None
        self._watcher_owner_id: int | None = None
        self._host_owner_id: int | None = None
        self._watcher_reg_id: int | None = None
        self._is_watcher_owner: bool = False
        self._sub_ids: list[int] = []
        self._host_name: str = f"org.kde.StatusNotifierHost-{os.getpid()}"
        # Per-item signal subscription ids so we can detach on unregister.
        self._item_sub_ids: dict[str, list[int]] = {}
        # Tracked items when WE run the watcher.
        self._watcher_items: list[str] = []
        self._watcher_hosts: list[str] = []

    # ── public API ───────────────────────────────────────────────────
    def start(self) -> None:
        if self._conn is not None:
            return
        self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Try to acquire the watcher name. If we get it we serve the
        # watcher interface; if it's taken we just continue as a host.
        self._watcher_owner_id = Gio.bus_own_name_on_connection(
            self._conn,
            WATCHER_BUS,
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT,
            self._on_watcher_acquired,
            self._on_watcher_lost,
        )
        # Always become a host on a unique bus name.
        self._host_owner_id = Gio.bus_own_name_on_connection(
            self._conn,
            self._host_name,
            Gio.BusNameOwnerFlags.NONE,
            None,
            None,
        )

    def stop(self) -> None:
        if self._conn is None:
            return
        for sid in self._sub_ids:
            self._conn.signal_unsubscribe(sid)
        self._sub_ids.clear()
        for sids in self._item_sub_ids.values():
            for sid in sids:
                self._conn.signal_unsubscribe(sid)
        self._item_sub_ids.clear()
        if self._watcher_reg_id is not None:
            self._conn.unregister_object(self._watcher_reg_id)
            self._watcher_reg_id = None
        if self._watcher_owner_id is not None:
            Gio.bus_unown_name(self._watcher_owner_id)
            self._watcher_owner_id = None
        if self._host_owner_id is not None:
            Gio.bus_unown_name(self._host_owner_id)
            self._host_owner_id = None
        self._items.clear()
        self._conn = None

    def items(self) -> list[TrayItem]:
        return list(self._items.values())

    def subscribe(
        self,
        on_added: Callable[[TrayItem], None],
        on_removed: Callable[[str], None],
        on_changed: Callable[[TrayItem], None],
    ) -> None:
        self._listeners.append((on_added, on_removed, on_changed))

    def unsubscribe(self, on_added, on_removed, on_changed) -> None:
        try:
            self._listeners.remove((on_added, on_removed, on_changed))
        except ValueError:
            pass

    # ── click forwarding ─────────────────────────────────────────────
    def activate(self, bus_name: str, x: int, y: int) -> None:
        self._call_item(bus_name, "Activate", GLib.Variant("(ii)", (x, y)))

    def secondary_activate(self, bus_name: str, x: int, y: int) -> None:
        self._call_item(bus_name, "SecondaryActivate", GLib.Variant("(ii)", (x, y)))

    def context_menu(self, bus_name: str, x: int, y: int) -> None:
        self._call_item(bus_name, "ContextMenu", GLib.Variant("(ii)", (x, y)))

    def scroll(self, bus_name: str, delta: int, orientation: str = "vertical") -> None:
        self._call_item(bus_name, "Scroll", GLib.Variant("(is)", (delta, orientation)))

    def build_menu(self, bus_name: str):
        """Pop the item's DBusMenu (if any) as a Gtk.Menu. Returns None
        if the item didn't advertise a menu path or the layout fetch
        failed (e.g. the item just died)."""
        from .dbusmenu import build_menu as _build_menu  # lazy import to avoid cycle on tests
        item = self._items.get(bus_name)
        if item is None or self._conn is None or not item.menu_path:
            return None
        return _build_menu(self._conn, bus_name, item.menu_path)

    def _call_item(self, bus_name: str, method: str, params: GLib.Variant) -> None:
        item = self._items.get(bus_name)
        if self._conn is None or item is None:
            return
        # Fire-and-forget; ignore errors (apps that don't implement the
        # method respond with a DBus error which we don't surface).
        self._conn.call(
            bus_name, item.path, ITEM_IFACE, method, params, None,
            Gio.DBusCallFlags.NONE, 2000, None, None,
        )

    # ── watcher role (only active if we own the bus name) ────────────
    def _on_watcher_acquired(self, conn, _name):
        self._is_watcher_owner = True
        node = Gio.DBusNodeInfo.new_for_xml(_WATCHER_XML)
        self._watcher_reg_id = conn.register_object(
            WATCHER_PATH,
            node.interfaces[0],
            self._watcher_method_call,
            self._watcher_get_property,
            None,  # no settable properties
        )
        self._become_host()

    def _on_watcher_lost(self, _conn, _name):
        # Someone else owns the watcher (e.g. plasmashell). Just be a host.
        self._is_watcher_owner = False
        self._become_host()

    def _watcher_method_call(self, _conn, sender, _path, _iface, method, params, invocation):
        if method == "RegisterStatusNotifierItem":
            service = params.unpack()[0]
            # Apps pass either a bus name ("org.kde.SNItem-1234-1") or
            # an object path ("/StatusNotifierItem") meaning "use sender
            # as bus name." Normalize.
            if service.startswith("/"):
                bus_name, path = sender, service
            else:
                bus_name, path = service, ITEM_PATH
            if bus_name not in self._watcher_items:
                self._watcher_items.append(bus_name)
                self._emit_watcher_signal("StatusNotifierItemRegistered", bus_name)
            # Track this item from our host side too:
            self._track_item(bus_name, path)
            invocation.return_value(None)
            return
        if method == "RegisterStatusNotifierHost":
            host = params.unpack()[0]
            if host not in self._watcher_hosts:
                self._watcher_hosts.append(host)
                self._emit_watcher_signal("StatusNotifierHostRegistered", None)
            invocation.return_value(None)
            return
        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method: {method}",
        )

    def _watcher_get_property(self, _conn, _sender, _path, _iface, prop):
        if prop == "RegisteredStatusNotifierItems":
            return GLib.Variant("as", self._watcher_items)
        if prop == "IsStatusNotifierHostRegistered":
            return GLib.Variant("b", bool(self._watcher_hosts))
        if prop == "ProtocolVersion":
            return GLib.Variant("i", 0)
        return None

    def _emit_watcher_signal(self, name: str, service: str | None) -> None:
        if self._conn is None:
            return
        params = GLib.Variant("(s)", (service,)) if service is not None else None
        self._conn.emit_signal(
            None, WATCHER_PATH, WATCHER_BUS, name, params,
        )

    # ── host role (always active) ────────────────────────────────────
    def _become_host(self) -> None:
        if self._conn is None:
            return
        # Subscribe to watcher signals.
        self._sub_ids.append(self._conn.signal_subscribe(
            WATCHER_BUS, WATCHER_BUS, "StatusNotifierItemRegistered",
            WATCHER_PATH, None, Gio.DBusSignalFlags.NONE,
            self._on_external_item_registered,
        ))
        self._sub_ids.append(self._conn.signal_subscribe(
            WATCHER_BUS, WATCHER_BUS, "StatusNotifierItemUnregistered",
            WATCHER_PATH, None, Gio.DBusSignalFlags.NONE,
            self._on_external_item_unregistered,
        ))
        # Track app deaths so we can drop their items even if the app
        # never sent Unregister (the common case — apps just exit).
        self._sub_ids.append(self._conn.signal_subscribe(
            DBUS_BUS, DBUS_IFACE, "NameOwnerChanged",
            DBUS_PATH, None, Gio.DBusSignalFlags.NONE,
            self._on_name_owner_changed,
        ))
        # Tell the watcher we exist (whoever it is).
        self._conn.call(
            WATCHER_BUS, WATCHER_PATH, WATCHER_BUS,
            "RegisterStatusNotifierHost",
            GLib.Variant("(s)", (self._host_name,)),
            None, Gio.DBusCallFlags.NONE, -1, None, None,
        )
        # Snapshot existing items.
        self._conn.call(
            WATCHER_BUS, WATCHER_PATH, PROPERTIES_IFACE, "Get",
            GLib.Variant("(ss)", (WATCHER_BUS, "RegisteredStatusNotifierItems")),
            None, Gio.DBusCallFlags.NONE, -1, None,
            self._on_initial_items,
        )

    def _on_initial_items(self, conn, result):
        try:
            reply = conn.call_finish(result)
        except GLib.Error:
            return
        # Get returns a variant wrapping the property value.
        wrapped = reply.unpack()[0]  # the variant's content (as)
        for service in wrapped:
            if service.startswith("/"):
                continue  # malformed; skip
            self._track_item(service, ITEM_PATH)

    def _on_external_item_registered(self, _c, _s, _p, _i, _sig, params):
        (service,) = params.unpack()
        if service.startswith("/"):
            return
        self._track_item(service, ITEM_PATH)

    def _on_external_item_unregistered(self, _c, _s, _p, _i, _sig, params):
        (service,) = params.unpack()
        self._drop_item(service)

    def _on_name_owner_changed(self, _c, _s, _p, _i, _sig, params):
        name, _old, new = params.unpack()
        if not new and name in self._items:
            self._drop_item(name)
        if not new and name in self._watcher_items:
            self._watcher_items.remove(name)
            self._emit_watcher_signal("StatusNotifierItemUnregistered", name)

    # ── per-item tracking ────────────────────────────────────────────
    def _track_item(self, bus_name: str, path: str) -> None:
        if bus_name in self._items:
            return
        item = TrayItem(bus_name=bus_name, path=path)
        self._items[bus_name] = item
        self._fetch_all_props(item)
        self._subscribe_item_signals(item)

    def _drop_item(self, bus_name: str) -> None:
        if bus_name not in self._items:
            return
        for sid in self._item_sub_ids.pop(bus_name, []):
            if self._conn is not None:
                self._conn.signal_unsubscribe(sid)
        self._items.pop(bus_name, None)
        for _add, on_remove, _change in self._listeners:
            on_remove(bus_name)

    def _subscribe_item_signals(self, item: TrayItem) -> None:
        if self._conn is None:
            return
        sids: list[int] = []
        for sig in ("NewTitle", "NewIcon", "NewAttentionIcon",
                    "NewOverlayIcon", "NewToolTip", "NewStatus"):
            sids.append(self._conn.signal_subscribe(
                item.bus_name, ITEM_IFACE, sig, item.path, None,
                Gio.DBusSignalFlags.NONE,
                self._on_item_changed,
            ))
        self._item_sub_ids[item.bus_name] = sids

    def _on_item_changed(self, _c, sender, _p, _i, signal, params):
        item = self._items.get(sender)
        if item is None:
            return
        if signal == "NewStatus":
            # Carries new value directly: signal(s).
            try:
                (new,) = params.unpack()
                item.status = new
                self._emit_changed(item)
                return
            except Exception:
                pass
        # All other "New*" signals just mean "re-read your properties".
        self._fetch_all_props(item, on_done=self._emit_changed)

    def _emit_changed(self, item: TrayItem) -> None:
        for _add, _rm, on_change in self._listeners:
            on_change(item)

    # ── property fetch ───────────────────────────────────────────────
    def _fetch_all_props(
        self,
        item: TrayItem,
        on_done: Callable[[TrayItem], None] | None = None,
    ) -> None:
        if self._conn is None:
            return

        def _done(conn, result):
            try:
                reply = conn.call_finish(result)
            except GLib.Error:
                # Item disappeared between register and our GetAll.
                return
            props = reply.unpack()[0]  # a{sv} → plain dict
            self._apply_props(item, props)
            if on_done is not None:
                on_done(item)
            else:
                # First-time fetch — announce as added.
                for on_add, _rm, _change in self._listeners:
                    on_add(item)

        self._conn.call(
            item.bus_name, item.path, PROPERTIES_IFACE, "GetAll",
            GLib.Variant("(s)", (ITEM_IFACE,)),
            None, Gio.DBusCallFlags.NONE, -1, None,
            _done,
        )

    @staticmethod
    def _apply_props(item: TrayItem, props: dict[str, Any]) -> None:
        item.id              = props.get("Id", item.id)
        item.title           = props.get("Title", item.title)
        item.status          = props.get("Status", item.status)
        item.icon_name       = props.get("IconName", "")
        item.icon_pixmaps    = _normalize_pixmaps(props.get("IconPixmap"))
        item.attention_icon_name    = props.get("AttentionIconName", "")
        item.attention_icon_pixmaps = _normalize_pixmaps(props.get("AttentionIconPixmap"))
        item.overlay_icon_name      = props.get("OverlayIconName", "")
        item.overlay_icon_pixmaps   = _normalize_pixmaps(props.get("OverlayIconPixmap"))
        tooltip = props.get("ToolTip")
        if tooltip:
            # (sa(iiay)ss): (theme-name, pixmaps, title, body)
            try:
                _name, _pm, item.tooltip_title, item.tooltip_body = tooltip
            except (TypeError, ValueError):
                pass
        item.menu_path        = props.get("Menu", "") or ""
        item.icon_theme_path  = props.get("IconThemePath", "") or ""
        item.item_is_menu     = bool(props.get("ItemIsMenu", False))


def _normalize_pixmaps(raw) -> list[tuple[int, int, bytes]]:
    """Convert raw `a(iiay)` into a sorted list of (w, h, bytes), largest first."""
    if not raw:
        return []
    out: list[tuple[int, int, bytes]] = []
    for entry in raw:
        try:
            w, h, data = entry
            out.append((int(w), int(h), bytes(data)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0] * p[1], reverse=True)
    return out


_broker: TrayBroker | None = None


def get_broker() -> TrayBroker:
    """Module-level lazy singleton — mirrors `services/music.get_status()`."""
    global _broker
    if _broker is None:
        _broker = TrayBroker()
    return _broker
