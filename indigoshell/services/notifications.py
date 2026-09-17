"""org.freedesktop.Notifications D-Bus service.

Owns the well-known bus name, decodes incoming Notify calls into a
Python-friendly shape, and dispatches them to a single callback. Also
emits the spec signals (NotificationClosed, ActionInvoked) when the UI
layer says a toast was dismissed or an action was clicked.

The UI is decoupled — this module is "just" a D-Bus adapter. The
notification manager owns one instance and supplies the
callbacks. Unlike the systray watcher this uses dbus-fast's high-level
`ServiceInterface`: nothing here needs the caller's identity, so the
declarative form is the tidier fit and buys introspection for free.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from dbus_fast import NameFlag, RequestNameReply, unpack_variants
from dbus_fast.service import ServiceInterface, method
from dbus_fast.service import signal as dbus_signal

from .bus import session_bus

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"

# Reasons emitted by NotificationClosed signal (spec values).
REASON_EXPIRED   = 1
REASON_DISMISSED = 2  # user dismissed
REASON_CLOSED    = 3  # CloseNotification call
REASON_UNDEFINED = 4

# Urgency hint values.
URGENCY_LOW      = 0
URGENCY_NORMAL   = 1
URGENCY_CRITICAL = 2


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
    image_data: Any = None          # (w,h,rowstride,alpha,bps,channels,bytes) or None
    image_path: str | None = None   # file path or file:// URI or None
    value: int | None = None        # progress 0-100, from "value" hint; None if absent
    raw_hints: dict[str, Any] = field(default_factory=dict)


class _Interface(ServiceInterface):
    def __init__(self, server: "NotificationServer") -> None:
        super().__init__(BUS_NAME)
        self._server = server

    @method()
    def Notify(self, app_name: 's', replaces_id: 'u', app_icon: 's',
               summary: 's', body: 's', actions: 'as', hints: 'a{sv}',
               expire_timeout: 'i') -> 'u':
        return self._server._handle_notify(
            app_name, replaces_id, app_icon, summary, body, actions,
            unpack_variants(hints), expire_timeout)

    @method()
    def CloseNotification(self, id: 'u') -> None:
        try:
            self._server.on_close_request(int(id))
        except Exception:
            log.exception("close-request handler failed")

    @method()
    def GetCapabilities(self) -> 'as':
        return list(NotificationServer.CAPABILITIES)

    @method()
    def GetServerInformation(self) -> 'ssss':
        s = NotificationServer
        return [s.SERVER_NAME, s.SERVER_VENDOR, s.SERVER_VERSION,
                s.SPEC_VERSION]

    @dbus_signal()
    def NotificationClosed(self, id: 'u', reason: 'u') -> 'uu':
        return [id, reason]

    @dbus_signal()
    def ActionInvoked(self, id: 'u', action_key: 's') -> 'us':
        return [id, action_key]


class NotificationServer:
    """D-Bus side. Calls `on_notify(notif)` for each arrival and
    `on_close_request(id)` for CloseNotification calls."""

    CAPABILITIES = ["body", "body-markup", "actions", "icon-static",
                    "persistence"]
    SERVER_NAME    = "indigoshell"
    SERVER_VENDOR  = "indigo"
    SERVER_VERSION = "0.2.0"
    SPEC_VERSION   = "1.2"

    def __init__(
        self,
        on_notify: Callable[[Notification], None],
        on_close_request: Callable[[int], None],
    ) -> None:
        self.on_notify = on_notify
        self.on_close_request = on_close_request
        self._next_id = 1
        self._iface = _Interface(self)
        self._started = False
        self.active = False

    async def start(self) -> None:
        """Export the interface and claim the well-known name. If another
        daemon (dunst, say) owns it we stay exported but inert — `active`
        says whether notifications will actually reach us."""
        if self._started:
            return
        self._started = True
        bus = await session_bus()
        bus.export(OBJECT_PATH, self._iface)
        reply = await bus.request_name(
            BUS_NAME, NameFlag.ALLOW_REPLACEMENT | NameFlag.DO_NOT_QUEUE)
        self.active = reply in (RequestNameReply.PRIMARY_OWNER,
                                RequestNameReply.ALREADY_OWNER)
        if self.active:
            log.info("notification daemon up")
        else:
            log.warning("%s is owned by another daemon; "
                        "notifications are theirs", BUS_NAME)

    # ── decode ───────────────────────────────────────────────────────
    def _handle_notify(self, app_name, replaces_id, app_icon, summary,
                       body, actions, hints, expire_timeout) -> int:
        nid = int(replaces_id) if int(replaces_id) != 0 else self._next_id
        if int(replaces_id) == 0:
            self._next_id += 1

        # actions array is flat [key, label, key, label, ...]
        action_pairs: list[tuple[str, str]] = []
        it = iter(actions)
        for key in it:
            label = next(it, key)
            action_pairs.append((str(key), str(label)))

        urgency = int(hints.get("urgency", URGENCY_NORMAL))

        # Image priority: image-data > image_data > image-path > image_path.
        # Spec uses kebab-case; older clients used snake_case.
        image_data = (hints.get("image-data") or hints.get("image_data")
                      or hints.get("icon_data"))
        image_path = hints.get("image-path") or hints.get("image_path") or None

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
            log.exception("notify handler failed")
        return nid

    # ── signals (called by the UI) ───────────────────────────────────
    def emit_closed(self, nid: int, reason: int) -> None:
        if self._started:
            self._iface.NotificationClosed(int(nid), int(reason))

    def emit_action(self, nid: int, action_key: str) -> None:
        if self._started:
            self._iface.ActionInvoked(int(nid), str(action_key))
