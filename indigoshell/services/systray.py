"""StatusNotifierItem broker — Watcher + Host for D-Bus tray icons.

Built on dbus-fast, on the daemon's own asyncio loop. What this module
does (in plain English):

1. **Watcher role** — claims the well-known session-bus name
   `org.kde.StatusNotifierWatcher`. SNI-aware apps look for this name to
   register their tray items. If something else already owns the name
   (KDE plasma, snixembed, ...) we just give up the role and become a
   pure host. If nothing else owns it, we run the watcher ourselves
   from this same process, which is the common case on a minimal WM.

2. **Host role** — registers as a `StatusNotifierHost-<pid>` on the bus
   and subscribes to the watcher's add/remove signals. When a new item
   appears we fetch its current properties (icon, tooltip, status, menu
   path, ...), subscribe to its `New*` change signals, and store it in
   `self._items`.

3. **Pub/sub** — widgets subscribe via `.subscribe(...)` to be notified
   when items are added, removed, or change. The widget owns rendering;
   this module is a pure data broker — same pattern as `services/music`.
   Everything runs on the one loop, so listeners are called directly —
   with no marshalling hop in between.

4. **Click dispatch** — exposes `activate`, `secondary_activate`,
   `context_menu`, and `scroll` so the renderer can forward user input
   to the item's D-Bus methods, plus `fetch_menu`/`menu_event` for the
   DBusMenu walker in `services/dbusmenu.py`.

The watcher interface is implemented at the raw message level rather
than through `ServiceInterface` because `RegisterStatusNotifierItem`
needs the caller's bus name — apps may register a bare object path,
meaning "use my unique name" — and the high-level API hides the sender.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from dbus_fast import (Message, MessageType, NameFlag,
                       RequestNameReply, Variant, unpack_variants)
from dbus_fast.aio import MessageBus

from .bus import session_bus

log = logging.getLogger(__name__)


# ── D-Bus constants ──────────────────────────────────────────────────
WATCHER_BUS      = "org.kde.StatusNotifierWatcher"
WATCHER_PATH     = "/StatusNotifierWatcher"
ITEM_IFACE       = "org.kde.StatusNotifierItem"
ITEM_PATH        = "/StatusNotifierItem"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECT_IFACE = "org.freedesktop.DBus.Introspectable"
DBUS_BUS         = "org.freedesktop.DBus"
DBUS_PATH        = "/org/freedesktop/DBus"
DBUS_IFACE       = "org.freedesktop.DBus"

_CALL_TIMEOUT = 2.0

# Watcher interface XML — served to Introspect callers when we run the
# watcher ourselves (i.e. the bus name was free when we started).
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

        broker.start()                # connect, claim watcher, register host
        broker.subscribe(on_add, on_remove, on_change)
        broker.activate(bus_name, x, y)      # forward clicks
    """

    def __init__(self) -> None:
        self._items: dict[str, TrayItem] = {}
        self._listeners: list[tuple[Callable, Callable, Callable]] = []
        self._bus: MessageBus | None = None
        self._starting = False
        self._is_watcher_owner = False
        self._host_name = f"org.kde.StatusNotifierHost-{os.getpid()}"
        # Tracked registrations when WE run the watcher.
        self._watcher_items: list[str] = []
        self._watcher_hosts: list[str] = []
        # Fire-and-forget tasks, held so the event loop doesn't GC them.
        self._tasks: set[asyncio.Task] = set()

    # ── public API ───────────────────────────────────────────────────
    def start(self) -> None:
        """Idempotent; connects on the running loop, in the background."""
        if self._bus is not None or self._starting:
            return
        self._starting = True
        if not self._spawn(self._connect()):
            self._starting = False

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
        self._call_item(bus_name, "Activate", "ii", [x, y])

    def secondary_activate(self, bus_name: str, x: int, y: int) -> None:
        self._call_item(bus_name, "SecondaryActivate", "ii", [x, y])

    def context_menu(self, bus_name: str, x: int, y: int) -> None:
        self._call_item(bus_name, "ContextMenu", "ii", [x, y])

    def scroll(self, bus_name: str, delta: int,
               orientation: str = "vertical") -> None:
        self._call_item(bus_name, "Scroll", "is", [delta, orientation])

    # ── menu ─────────────────────────────────────────────────────────
    async def fetch_menu(self, bus_name: str):
        """The item's DBusMenu layout as `MenuNode`s, or None if the item
        didn't advertise a menu path or the fetch failed (item died)."""
        from .dbusmenu import fetch_layout  # lazy: avoid cycle in tests
        item = self._items.get(bus_name)
        if item is None or self._bus is None or not item.menu_path:
            return None
        return await fetch_layout(self._bus, bus_name, item.menu_path)

    def menu_event(self, bus_name: str, item_id: int,
                   event_id: str = "clicked") -> None:
        from .dbusmenu import send_event
        item = self._items.get(bus_name)
        if item is None or self._bus is None or not item.menu_path:
            return
        self._spawn(send_event(self._bus, bus_name, item.menu_path,
                               item_id, event_id))

    # ── connection ───────────────────────────────────────────────────
    async def _connect(self) -> None:
        try:
            bus = await session_bus()
        except Exception:
            log.exception("session bus connect failed; no tray")
            self._starting = False
            return
        self._bus = bus
        bus.add_message_handler(self._on_message)

        try:
            await bus.request_name(self._host_name)
        except Exception:
            log.debug("host name request failed", exc_info=True)
        await self._try_acquire_watcher()

        for rule in (
            f"type='signal',sender='{WATCHER_BUS}',path='{WATCHER_PATH}',"
            f"interface='{WATCHER_BUS}'",
            f"type='signal',sender='{DBUS_BUS}',path='{DBUS_PATH}',"
            f"interface='{DBUS_IFACE}',member='NameOwnerChanged'",
        ):
            await self._add_match(rule)
        await self._register_with_watcher()
        log.info("tray broker up (%s)",
                 "watcher+host" if self._is_watcher_owner else "host only")

    async def _try_acquire_watcher(self) -> None:
        assert self._bus is not None
        try:
            reply = await self._bus.request_name(
                WATCHER_BUS, NameFlag.ALLOW_REPLACEMENT | NameFlag.DO_NOT_QUEUE)
        except Exception:
            log.debug("watcher name request failed", exc_info=True)
            return
        self._is_watcher_owner = reply in (
            RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER)

    async def _register_with_watcher(self) -> None:
        """Tell the watcher we exist (whoever it is), then snapshot the
        items it already tracks. Works unchanged when the watcher is us:
        the daemon routes both messages straight back."""
        if self._bus is None:
            return
        try:
            await asyncio.wait_for(self._bus.call(Message(
                destination=WATCHER_BUS, path=WATCHER_PATH,
                interface=WATCHER_BUS, member="RegisterStatusNotifierHost",
                signature="s", body=[self._host_name])), _CALL_TIMEOUT)
        except Exception:
            log.debug("no watcher to register with", exc_info=True)
        try:
            reply = await asyncio.wait_for(self._bus.call(Message(
                destination=WATCHER_BUS, path=WATCHER_PATH,
                interface=PROPERTIES_IFACE, member="Get", signature="ss",
                body=[WATCHER_BUS, "RegisteredStatusNotifierItems"])),
                _CALL_TIMEOUT)
        except Exception:
            return
        if reply.message_type != MessageType.METHOD_RETURN or not reply.body:
            return
        value = reply.body[0]
        for service in unpack_variants(value) or []:
            if service.startswith("/"):
                continue  # malformed; skip
            self._track_item(service, ITEM_PATH)

    # ── message dispatch ─────────────────────────────────────────────
    def _on_message(self, msg: Message):
        if msg.message_type == MessageType.METHOD_CALL:
            return self._on_method_call(msg)
        if msg.message_type == MessageType.SIGNAL:
            self._on_signal(msg)
        return None

    # ── watcher role (only active if we own the bus name) ────────────
    def _on_method_call(self, msg: Message):
        if msg.path != WATCHER_PATH or not self._is_watcher_owner:
            return None
        if msg.interface == WATCHER_BUS:
            if msg.member == "RegisterStatusNotifierItem":
                service = msg.body[0]
                # Apps pass either a bus name ("org.kde.SNItem-1234-1") or
                # an object path ("/StatusNotifierItem") meaning "use
                # sender as bus name." Normalize.
                if service.startswith("/"):
                    bus_name, path = msg.sender, service
                else:
                    bus_name, path = service, ITEM_PATH
                if bus_name not in self._watcher_items:
                    self._watcher_items.append(bus_name)
                    self._emit_watcher_signal(
                        "StatusNotifierItemRegistered", bus_name)
                # Track this item from our host side too:
                self._track_item(bus_name, path)
                return Message.new_method_return(msg)
            if msg.member == "RegisterStatusNotifierHost":
                host = msg.body[0]
                if host not in self._watcher_hosts:
                    self._watcher_hosts.append(host)
                    self._emit_watcher_signal(
                        "StatusNotifierHostRegistered", None)
                return Message.new_method_return(msg)
        if msg.interface == PROPERTIES_IFACE:
            return self._watcher_property_call(msg)
        if msg.interface == INTROSPECT_IFACE and msg.member == "Introspect":
            return Message.new_method_return(msg, "s", [_WATCHER_XML])
        return None

    def _watcher_property_call(self, msg: Message):
        props = {
            "RegisteredStatusNotifierItems": Variant("as", self._watcher_items),
            "IsStatusNotifierHostRegistered": Variant("b", bool(self._watcher_hosts)),
            "ProtocolVersion": Variant("i", 0),
        }
        if msg.member == "Get":
            _iface, name = msg.body
            value = props.get(name)
            if value is None:
                return Message.new_error(
                    msg, "org.freedesktop.DBus.Error.UnknownProperty",
                    f"Unknown property: {name}")
            return Message.new_method_return(msg, "v", [value])
        if msg.member == "GetAll":
            return Message.new_method_return(msg, "a{sv}", [props])
        return None

    def _emit_watcher_signal(self, name: str, service: str | None) -> None:
        if self._bus is None:
            return
        sig, body = ("s", [service]) if service is not None else ("", [])
        self._bus.send(Message.new_signal(
            WATCHER_PATH, WATCHER_BUS, name, sig, body))

    # ── host role (always active) ────────────────────────────────────
    def _on_signal(self, msg: Message) -> None:
        if msg.interface == WATCHER_BUS and msg.path == WATCHER_PATH:
            if msg.member == "StatusNotifierItemRegistered":
                service = msg.body[0]
                if not service.startswith("/"):
                    self._track_item(service, ITEM_PATH)
            elif msg.member == "StatusNotifierItemUnregistered":
                self._drop_item(msg.body[0])
            return
        if (msg.interface == DBUS_IFACE and msg.member == "NameOwnerChanged"
                and msg.sender == DBUS_BUS):
            self._on_name_owner_changed(*msg.body)
            return
        if msg.interface == ITEM_IFACE and msg.member.startswith("New"):
            self._on_item_changed(msg)

    def _on_name_owner_changed(self, name: str, _old: str, new: str) -> None:
        # Apps just exit rather than sending Unregister — deaths are the
        # common removal path.
        if not new and name in self._items:
            self._drop_item(name)
        if not new and name in self._watcher_items:
            self._watcher_items.remove(name)
            self._emit_watcher_signal("StatusNotifierItemUnregistered", name)
        if name == WATCHER_BUS and self._bus is not None:
            if not new:
                # The external watcher died; take over if we can.
                self._spawn(self._watcher_handover())
            elif new != self._bus.unique_name:
                # Someone else took the watcher role; be a pure host and
                # introduce ourselves to the new owner.
                self._is_watcher_owner = False
                self._spawn(self._register_with_watcher())

    async def _watcher_handover(self) -> None:
        await self._try_acquire_watcher()
        if self._is_watcher_owner:
            log.info("took over the StatusNotifierWatcher role")

    def _on_item_changed(self, msg: Message) -> None:
        # Change signals come from the app's *unique* name; items that
        # registered under a well-known name won't match — a known
        # limitation, and rare in practice.
        item = self._items.get(msg.sender)
        if item is None:
            return
        if msg.member == "NewStatus" and msg.body:
            item.status = msg.body[0]
            self._emit_changed(item)
            return
        # All other "New*" signals just mean "re-read your properties".
        self._spawn(self._refresh_item(item))

    async def _refresh_item(self, item: TrayItem) -> None:
        if await self._fetch_all_props(item):
            self._emit_changed(item)

    def _emit_changed(self, item: TrayItem) -> None:
        for _add, _rm, on_change in list(self._listeners):
            on_change(item)

    # ── per-item tracking ────────────────────────────────────────────
    def _track_item(self, bus_name: str, path: str) -> None:
        if bus_name in self._items:
            return
        item = TrayItem(bus_name=bus_name, path=path)
        self._items[bus_name] = item
        self._spawn(self._init_item(item))

    async def _init_item(self, item: TrayItem) -> None:
        await self._add_match(self._item_rule(item))
        if not await self._fetch_all_props(item):
            # Item disappeared between register and our GetAll.
            self._items.pop(item.bus_name, None)
            self._spawn(self._remove_match(self._item_rule(item)))
            return
        for on_add, _rm, _change in list(self._listeners):
            on_add(item)

    def _drop_item(self, bus_name: str) -> None:
        item = self._items.pop(bus_name, None)
        if item is None:
            return
        self._spawn(self._remove_match(self._item_rule(item)))
        for _add, on_remove, _change in list(self._listeners):
            on_remove(bus_name)

    @staticmethod
    def _item_rule(item: TrayItem) -> str:
        return (f"type='signal',sender='{item.bus_name}',"
                f"interface='{ITEM_IFACE}'")

    # ── property fetch ───────────────────────────────────────────────
    async def _fetch_all_props(self, item: TrayItem) -> bool:
        if self._bus is None:
            return False
        try:
            reply = await asyncio.wait_for(self._bus.call(Message(
                destination=item.bus_name, path=item.path,
                interface=PROPERTIES_IFACE, member="GetAll", signature="s",
                body=[ITEM_IFACE])), _CALL_TIMEOUT)
        except Exception:
            return False
        if (reply is None or reply.message_type != MessageType.METHOD_RETURN
                or not reply.body):
            return False
        self._apply_props(item, unpack_variants(reply.body[0]))
        return True

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

    # ── plumbing ─────────────────────────────────────────────────────
    def _call_item(self, bus_name: str, method: str,
                   signature: str, body: list) -> None:
        item = self._items.get(bus_name)
        if self._bus is None or item is None:
            return
        # Fire-and-forget; ignore errors (apps that don't implement the
        # method respond with a DBus error which we don't surface).
        self._spawn(self._call_quietly(Message(
            destination=bus_name, path=item.path, interface=ITEM_IFACE,
            member=method, signature=signature, body=body)))

    async def _call_quietly(self, msg: Message) -> None:
        assert self._bus is not None
        try:
            await asyncio.wait_for(self._bus.call(msg), _CALL_TIMEOUT)
        except Exception:
            pass

    async def _add_match(self, rule: str) -> None:
        await self._bus_admin("AddMatch", rule)

    async def _remove_match(self, rule: str) -> None:
        await self._bus_admin("RemoveMatch", rule)

    async def _bus_admin(self, member: str, rule: str) -> None:
        if self._bus is None:
            return
        try:
            await asyncio.wait_for(self._bus.call(Message(
                destination=DBUS_BUS, path=DBUS_PATH, interface=DBUS_IFACE,
                member=member, signature="s", body=[rule])), _CALL_TIMEOUT)
        except Exception:
            log.debug("%s failed: %s", member, rule, exc_info=True)

    def _spawn(self, coro) -> bool:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return False
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True


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
