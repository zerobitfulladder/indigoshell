"""Shared D-Bus session connection.

Every D-Bus service in the shell (systray watcher, notification daemon,
...) shares one `MessageBus` on the daemon's loop. One connection means
one unique name on the bus, one socket to poll, and no ordering
surprises between our own services.

`session_bus()` is safe to race: the first caller starts the connect and
everyone else awaits the same future.
"""

import asyncio

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

_connecting: asyncio.Future | None = None


async def session_bus() -> MessageBus:
    global _connecting
    if _connecting is None:
        _connecting = asyncio.ensure_future(
            MessageBus(bus_type=BusType.SESSION).connect())
    try:
        return await asyncio.shield(_connecting)
    except Exception:
        # A failed connect must not poison every later attempt.
        if _connecting is not None and _connecting.done():
            _connecting = None
        raise
