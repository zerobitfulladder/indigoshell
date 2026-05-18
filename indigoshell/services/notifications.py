"""org.freedesktop.Notifications D-Bus service.

Owns the well-known bus name, decodes incoming Notify calls into a
Python-friendly shape, and dispatches them to a single callback. Also
emits the spec signals (NotificationClosed, ActionInvoked) when the UI
layer says a toast was dismissed or an action was clicked.

The UI is decoupled — this module is "just" a D-Bus adapter. The
notification stack window owns one instance and supplies the callbacks.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib


# Reasons emitted by NotificationClosed signal (spec values).
REASON_EXPIRED   = 1
REASON_DISMISSED = 2  # user dismissed
REASON_CLOSED    = 3  # CloseNotification call
REASON_UNDEFINED = 4

# Urgency hint values.
URGENCY_LOW      = 0
URGENCY_NORMAL   = 1
URGENCY_CRITICAL = 2


_INTERFACE_XML = """
<node>
  <interface name='org.freedesktop.Notifications'>
    <method name='Notify'>
      <arg type='s' name='app_name'        direction='in'/>
      <arg type='u' name='replaces_id'     direction='in'/>
      <arg type='s' name='app_icon'        direction='in'/>
      <arg type='s' name='summary'         direction='in'/>
      <arg type='s' name='body'            direction='in'/>
      <arg type='as' name='actions'        direction='in'/>
      <arg type='a{sv}' name='hints'       direction='in'/>
      <arg type='i' name='expire_timeout'  direction='in'/>
      <arg type='u' name='id'              direction='out'/>
    </method>
    <method name='CloseNotification'>
      <arg type='u' name='id' direction='in'/>
    </method>
    <method name='GetCapabilities'>
      <arg type='as' name='capabilities' direction='out'/>
    </method>
    <method name='GetServerInformation'>
      <arg type='s' name='name'         direction='out'/>
      <arg type='s' name='vendor'       direction='out'/>
      <arg type='s' name='version'      direction='out'/>
      <arg type='s' name='spec_version' direction='out'/>
    </method>
    <signal name='NotificationClosed'>
      <arg type='u' name='id'/>
      <arg type='u' name='reason'/>
    </signal>
    <signal name='ActionInvoked'>
      <arg type='u' name='id'/>
      <arg type='s' name='action_key'/>
    </signal>
  </interface>
</node>
"""


@dataclass
class Notification:
    """Parsed Notify call. The UI builds a toast from this."""
    id: int
    app_name: str
    app_icon: str
    summary: str
    body: str
    actions: list[tuple[str, str]]  # [(key, label), ...]
    expire_timeout: int             # ms; -1 = server default, 0 = never
    urgency: int                    # 0/1/2
    image_data: Any = None          # raw GVariant tuple (w,h,rowstride,alpha,bps,channels,bytes) or None
    image_path: str | None = None   # file path or file:// URI or None
    value: int | None = None        # progress 0-100, from "value" hint; None if absent
    raw_hints: dict[str, Any] = field(default_factory=dict)


class NotificationServer:
    """D-Bus side. Calls `on_notify(notif)` for each arrival and
    `on_close(id, reason)` for CloseNotification calls (reason=CLOSED)."""

    CAPABILITIES = ["body", "body-markup", "actions", "icon-static", "persistence"]
    SERVER_NAME    = "indigoshell"
    SERVER_VENDOR  = "indigo"
    SERVER_VERSION = "0.1.0"
    SPEC_VERSION   = "1.2"

    def __init__(
        self,
        on_notify: Callable[[Notification], None],
        on_close_request: Callable[[int], None],
    ) -> None:
        self.on_notify = on_notify
        self.on_close_request = on_close_request
        self._next_id = 1
        self._node = Gio.DBusNodeInfo.new_for_xml(_INTERFACE_XML)
        self._owner_id: int | None = None
        self._registration_id: int | None = None
        self._connection: Gio.DBusConnection | None = None

    def start(self) -> None:
        if self._owner_id is not None:
            return
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            "org.freedesktop.Notifications",
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )

    def stop(self) -> None:
        if self._connection is not None and self._registration_id is not None:
            self._connection.unregister_object(self._registration_id)
        self._registration_id = None
        if self._owner_id is not None:
            Gio.bus_unown_name(self._owner_id)
        self._owner_id = None
        self._connection = None

    # ── name ownership callbacks ────────────────────────────────────
    def _on_bus_acquired(self, connection, _name):
        self._connection = connection
        self._registration_id = connection.register_object(
            "/org/freedesktop/Notifications",
            self._node.interfaces[0],
            self._method_call,
            None,
            None,
        )

    def _on_name_acquired(self, _connection, name):
        # Useful for logs; quiet by default.
        pass

    def _on_name_lost(self, _connection, name):
        # Another daemon took the name. We could re-attempt or log.
        # For now, leave silent; daemon reload will retry.
        pass

    # ── method dispatch ─────────────────────────────────────────────
    def _method_call(
        self, _connection, _sender, _path, _interface, method, params, invocation
    ):
        if method == "Notify":
            self._handle_notify(params, invocation)
        elif method == "CloseNotification":
            (nid,) = params.unpack()
            self.on_close_request(int(nid))
            invocation.return_value(None)
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (self.CAPABILITIES,)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant(
                "(ssss)",
                (self.SERVER_NAME, self.SERVER_VENDOR, self.SERVER_VERSION, self.SPEC_VERSION),
            ))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.UNKNOWN_METHOD,
                f"Unknown method {method}",
            )

    def _handle_notify(self, params, invocation):
        app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout = params.unpack()

        nid = int(replaces_id) if int(replaces_id) != 0 else self._next_id
        if int(replaces_id) == 0:
            self._next_id += 1

        # actions array is flat [key, label, key, label, ...]
        action_pairs: list[tuple[str, str]] = []
        it = iter(actions)
        for key in it:
            label = next(it, key)
            action_pairs.append((str(key), str(label)))

        # hints come back unpacked as native dict already (a{sv} → dict[str, Any])
        urgency = int(hints.get("urgency", URGENCY_NORMAL))

        # Image priority: image-data > image_data > image-path > image_path > app_icon
        # Spec uses kebab-case; older clients used snake_case.
        image_data = (
            hints.get("image-data") or hints.get("image_data") or hints.get("icon_data")
        )
        image_path = (
            hints.get("image-path") or hints.get("image_path") or None
        )

        # "value" hint (int 0-100) — apps use this for progress bars.
        value_hint = hints.get("value")
        value = int(value_hint) if value_hint is not None else None

        notif = Notification(
            id=nid,
            app_name=str(app_name),
            app_icon=str(app_icon),
            summary=str(summary),
            body=str(body),
            actions=action_pairs,
            expire_timeout=int(expire_timeout),
            urgency=urgency,
            image_data=image_data,
            image_path=str(image_path) if image_path else None,
            value=value,
            raw_hints=dict(hints),
        )

        try:
            self.on_notify(notif)
        except Exception:
            # Don't let UI exceptions break the D-Bus return.
            import traceback
            traceback.print_exc()
        invocation.return_value(GLib.Variant("(u)", (nid,)))

    # ── signals (called by the UI) ──────────────────────────────────
    def emit_closed(self, nid: int, reason: int) -> None:
        if self._connection is None:
            return
        self._connection.emit_signal(
            None,
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "NotificationClosed",
            GLib.Variant("(uu)", (int(nid), int(reason))),
        )

    def emit_action(self, nid: int, action_key: str) -> None:
        if self._connection is None:
            return
        self._connection.emit_signal(
            None,
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "ActionInvoked",
            GLib.Variant("(us)", (int(nid), str(action_key))),
        )
