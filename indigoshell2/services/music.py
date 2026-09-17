"""Shared music-status broker.

Tracks player status via `playerctl --follow`. v1 read the pipe with
GLib.io_add_watch to avoid a thread; here the subprocess is already on
the loop, so it's just an async line reader.

Per-player singletons: `get_status()` watches every MPRIS source,
`get_status("spotify")` filters to one — but the filtering happens
*here*, not in playerctl. See `_start`.

Two sources, because neither alone answers "is my player playing":

  • `playerctl -a --follow metadata` reports every status change, and one
    blank line when the *last* player goes away.
  • `NameOwnerChanged` on the bus reports *our* player going away, which
    playerctl is silent about while any other player is still around.

Getting this wrong is not cosmetic. Everything gated on `playing` —
the cava visualiser, the parec/aubio beat pulse — keeps its subprocesses
alive for as long as this says True.
"""

import asyncio
import logging
from typing import Callable

from dbus_fast import Message

from . import proc

log = logging.getLogger(__name__)

# Field separator inside one playerctl line. A unit separator can't occur
# in a player name or a status.
SEPARATOR = "\x1f"

MPRIS_PREFIX = "org.mpris.MediaPlayer2"
DBUS_BUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"
# arg0namespace, not arg0: playerctl resolves `spotify_player` to either
# that bus name or `spotify_player.instanceNNNN`, so an exact-name match
# would miss the instance form. The prefix match catches both and the
# handler decides.
NAME_RULE = (f"type='signal',sender='{DBUS_BUS}',path='{DBUS_PATH}',"
             f"interface='{DBUS_IFACE}',member='NameOwnerChanged',"
             f"arg0namespace='{MPRIS_PREFIX}'")
_CALL_TIMEOUT = 2.0


class MusicStatus:
    def __init__(self, player: str | None = None) -> None:
        self._player = player
        self._listeners: list[Callable[[bool], None]] = []
        self._sub: proc.Subscription | None = None
        self._bus = None
        self._watching = False
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)
        if self._sub is None:
            self._start()
        # Replay current state so a new listener renders correctly
        # instead of waiting for the next status change.
        try:
            fn(self._playing)
        except Exception:
            log.exception("music listener failed on replay")

    def remove_listener(self, fn: Callable[[bool], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass
        if not self._listeners:
            self._stop()

    def set_playing(self, playing: bool) -> None:
        """External hook — drive the broker manually (tests, custom
        triggers). Also called for every playerctl line."""
        if playing == self._playing:
            return
        self._playing = playing
        for fn in list(self._listeners):
            try:
                fn(playing)
            except Exception:
                log.exception("music listener failed")

    def _start(self) -> None:
        # Deliberately not `--player NAME`, which is what v1 used and what
        # this did first. With that filter playerctl says *nothing at all*
        # when the player exits, as opposed to pausing — so `_playing`
        # stayed True forever against a player that was gone, and anything
        # gated on it (the cava visualiser, the parec/aubio beat pulse)
        # kept its subprocesses running over silence.
        #
        # `-a --follow metadata` reports every player and prints one blank
        # line when the last one goes away, which is the event we were
        # missing. The player filter therefore moves into `_on_line`.
        cmd = ["playerctl", "-a", "--follow", "metadata",
               "--format", "{{playerName}}" + SEPARATOR + "{{status}}"]
        self._sub = proc.subscribe(cmd, self._on_line)
        # Only a named player needs the bus watch — with no filter, the
        # blank line above already means "nothing is playing anywhere".
        if self._player and not self._watching:
            self._watching = True
            self._spawn(self._watch_names())

    def _stop(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        if self._watching:
            self._watching = False
            bus, self._bus = self._bus, None
            if bus is not None:
                bus.remove_message_handler(self._on_bus_message)
                self._spawn(self._unmatch(bus))

    # ── the player going away ───────────────────────────────────────────
    @staticmethod
    def _spawn(coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    async def _watch_names(self) -> None:
        from .bus import session_bus
        try:
            bus = await session_bus()
        except Exception:
            log.warning("no session bus; a player that quits without "
                        "pausing will look like it is still playing")
            self._watching = False
            return
        if not self._watching:      # stopped while connecting
            return
        self._bus = bus
        bus.add_message_handler(self._on_bus_message)
        await self._match(bus, "AddMatch")

    async def _match(self, bus, member: str) -> None:
        try:
            await asyncio.wait_for(bus.call(Message(
                destination=DBUS_BUS, path=DBUS_PATH, interface=DBUS_IFACE,
                member=member, signature="s", body=[NAME_RULE])),
                _CALL_TIMEOUT)
        except Exception:
            log.debug("%s failed for %s", member, NAME_RULE, exc_info=True)

    async def _unmatch(self, bus) -> None:
        await self._match(bus, "RemoveMatch")

    def _owns_name(self, name: str) -> bool:
        if not self._player or not name.startswith(MPRIS_PREFIX + "."):
            return False
        rest = name[len(MPRIS_PREFIX) + 1:]
        return rest == self._player or rest.startswith(self._player + ".")

    def _on_bus_message(self, msg) -> None:
        if (msg.member != "NameOwnerChanged" or msg.interface != DBUS_IFACE
                or len(msg.body) != 3):
            return
        name, _old, new_owner = msg.body
        # An empty new owner means the name was released — the player
        # exited, crashed, or was killed. Either way it is not playing.
        if not new_owner and self._owns_name(name):
            log.debug("%s left the bus", name)
            self.set_playing(False)

    def _on_line(self, line: str) -> None:
        if not line.strip():
            # No players left at all. Nothing can be playing, whichever
            # player we were watching.
            self.set_playing(False)
            return
        name, _, status = line.partition(SEPARATOR)
        # A line about somebody else's player says nothing about ours.
        # Note this means we stay "playing" if *our* player quits while
        # another is still going — which is the right answer for what
        # this gates: there is still audio to visualise.
        if self._player and name.strip() != self._player:
            return
        self.set_playing(status.strip() == "Playing")


_statuses: dict[str | None, MusicStatus] = {}


def get_status(player: str | None = None) -> MusicStatus:
    if player not in _statuses:
        _statuses[player] = MusicStatus(player)
    return _statuses[player]
