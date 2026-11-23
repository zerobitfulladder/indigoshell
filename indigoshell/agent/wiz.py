"""WiZ lights on the local network, for the call's agent.

WiZ bulbs speak JSON over UDP port 38899 on the LAN — no cloud account
and no key. `getPilot` reads what a bulb shows, `setPilot` changes it:
on or off, brightness, a white temperature, a colour, or one of the
bulbs' built-in scenes.

**Finding them.** The usual way is a broadcast `getPilot`, but a
stateful firewall (ufw, here) drops the answers: a reply to a broadcast
does not match the outgoing packet's connection, so it arrives as
unsolicited. Discovery therefore does three things at once — asks the
addresses it remembers, broadcasts, and asks every address on each
local subnet of up to /22 directly, whose replies any firewall lets
back in. Bulbs are remembered by MAC in the state file, with their last
address, model and white range, so a command to a known bulb is one
round trip.

**Names.** A bulb's name and room live in WiZ's cloud app, not on the
bulb, so the shell keeps its own:
`~/.config/indigoshell/connections/wiz/config.json`,

    {"bulbs": {"a8bb50c0ffee": {"name": "desk", "room": "bedroom"}}}

which the agent fills in with the user (`identify` blinks a bulb so
they can say which one it is). Editing it by hand works too.
"""

import asyncio
import dataclasses
import ipaddress
import json
import logging
import os
import socket
import time

from .. import theme
from ..core import state
from . import connections

log = logging.getLogger(__name__)

PORT = 38899
STATE_KEY = "agent.wiz"
SWEEP_MAX_HOSTS = 1024      # a /22; bigger networks are not swept
DISCOVER_S = 1.2
REPLY_S = 1.2
RESEND_S = 0.3

# The bulbs' built-in scenes, by id. White-only bulbs accept only the
# white ones; a bulb that cannot show a scene says so in its reply.
SCENES = {
    1: "Ocean", 2: "Romance", 3: "Sunset", 4: "Party", 5: "Fireplace",
    6: "Cozy", 7: "Forest", 8: "Pastel colors", 9: "Wake up",
    10: "Bedtime", 11: "Warm white", 12: "Daylight", 13: "Cool white",
    14: "Night light", 15: "Focus", 16: "Relax", 17: "True colors",
    18: "TV time", 19: "Plant growth", 20: "Spring", 21: "Summer",
    22: "Fall", 23: "Deep dive", 24: "Jungle", 25: "Mojito", 26: "Club",
    27: "Christmas", 28: "Halloween", 29: "Candlelight",
    30: "Golden white", 31: "Pulse", 32: "Steampunk", 33: "Diwali",
    34: "White", 35: "Alarm", 36: "Snowy sky", 1000: "Rhythm",
}

# Colour words the agent may use besides hex: the shell's own palette
# first, so "make it look like the shell" is a sentence that works.
COLORS = {
    "magenta": theme.MAGENTA_BRIGHT, "pink": theme.MAGENTA_BLOOM,
    "cyan": theme.CYAN_BRIGHT, "violet": theme.VIOLET_BRIGHT,
    "purple": theme.VIOLET, "yellow": theme.YELLOW_BRIGHT,
    "lime": theme.LIME_BRIGHT, "red": "#ff1020", "orange": "#ff6a00",
    "green": "#00ff3c", "blue": "#0a3cff", "teal": "#00c2a0",
}


class WizError(Exception):
    pass


@dataclasses.dataclass
class Bulb:
    mac: str
    ip: str
    module: str = ""                    # e.g. ESP25_SHRGB_01
    cct: tuple[int, int] = (2700, 6500)
    pilot: dict = dataclasses.field(default_factory=dict)
    name: str = ""
    room: str = ""

    @property
    def kind(self) -> str:
        m = self.module.upper()
        if "RGB" in m:
            return "color"
        if "TW" in m:
            return "white"              # tunable white
        if "DW" in m:
            return "dimmable"
        if "SOCKET" in m or "PLUG" in m:
            return "plug"
        return "unknown"

    @property
    def label(self) -> str:
        return self.name or f"bulb-{self.mac[-4:]}"

    def showing(self) -> str:
        p = self.pilot
        if not p:
            return "no answer"
        if not p.get("state"):
            return "off"
        parts = [f"on {p.get('dimming', 100)}%"]
        scene = p.get("sceneId") or 0
        if scene:
            parts.append(f"scene {SCENES.get(scene, scene)}"
                         + (f" speed {p['speed']}" if "speed" in p else ""))
        elif "temp" in p:
            parts.append(f"{p['temp']}K white")
        elif "r" in p:
            parts.append("color #{:02x}{:02x}{:02x}".format(
                p.get("r", 0), p.get("g", 0), p.get("b", 0)))
        return ", ".join(parts)

    def line(self) -> str:
        where = f" ({self.room})" if self.room else ""
        temps = (f", {self.cct[0]}-{self.cct[1]}K"
                 if self.kind in ("color", "white") else "")
        return (f"{self.label}{where}: {self.showing()} — {self.kind} bulb"
                f"{temps}, {self.ip}, mac {self.mac}")


def parse_color(value: str) -> tuple[int, int, int]:
    v = COLORS.get(value.strip().lower(), value.strip())
    if v.startswith("#"):
        v = v[1:]
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        raise WizError(f"not a colour: {value!r} (use #rrggbb or one of "
                       f"{', '.join(COLORS)})") from None
    if len(v) != 6:
        raise WizError(f"not a colour: {value!r}")
    if r == g == b == 0:
        raise WizError("black is off: use on=false")
    return r, g, b


def parse_scene(value) -> int:
    if isinstance(value, int) or str(value).strip().isdigit():
        sid = int(value)
        if sid in SCENES:
            return sid
        raise WizError(f"no scene {sid}")
    want = str(value).strip().lower().replace("-", " ")
    for sid, name in SCENES.items():
        if name.lower() == want:
            return sid
    for sid, name in SCENES.items():
        if want in name.lower():
            return sid
    raise WizError(f"no scene called {value!r}; there are: "
                   + ", ".join(SCENES.values()))


def restore_params(pilot: dict) -> dict:
    """setPilot params that put a bulb back to what `pilot` showed."""
    if not pilot.get("state"):
        return {"state": False}
    out: dict = {"state": True, "dimming": pilot.get("dimming", 100)}
    if pilot.get("sceneId"):
        out["sceneId"] = pilot["sceneId"]
        if "speed" in pilot:
            out["speed"] = pilot["speed"]
    elif "temp" in pilot:
        out["temp"] = pilot["temp"]
    elif "r" in pilot:
        for k in ("r", "g", "b", "c", "w"):
            if k in pilot:
                out[k] = pilot[k]
    return out


class _Collect(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []
        self.event = asyncio.Event()

    def datagram_received(self, data, addr) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict):
            self.replies.append((addr[0], msg))
            self.event.set()


def _networks() -> tuple[set[str], list[str]]:
    """Broadcast addresses, and the hosts of every small local subnet."""
    import psutil
    up = {n for n, s in psutil.net_if_stats().items() if s.isup}
    bcast, hosts = {"255.255.255.255"}, []
    for name, addrs in psutil.net_if_addrs().items():
        if name not in up:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            ip = ipaddress.IPv4Address(a.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            net = ipaddress.IPv4Network(f"{a.address}/{a.netmask}",
                                        strict=False)
            if a.broadcast:
                bcast.add(a.broadcast)
            if net.num_addresses <= SWEEP_MAX_HOSTS:
                hosts.extend(str(h) for h in net.hosts() if h != ip)
    return bcast, hosts


class Wiz:
    """The lights: discovery, reading and setting, names."""

    def __init__(self, names_path: str | None = None) -> None:
        self.names_path = names_path or connections.path("wiz",
                                                         "config.json")

    # ── names and memory ────────────────────────────────────────────────
    def _names(self) -> dict:
        try:
            with open(self.names_path) as f:
                data = json.load(f)
            return data.get("bulbs", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def name(self, mac: str, name: str, room: str | None) -> None:
        try:
            with open(self.names_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        bulbs = data.setdefault("bulbs", {})
        entry = bulbs.setdefault(mac, {})
        entry["name"] = name
        if room is not None:
            entry["room"] = room
        os.makedirs(os.path.dirname(self.names_path), exist_ok=True)
        tmp = self.names_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.names_path)

    def remembered(self) -> list[str]:
        """The MACs of every bulb seen before."""
        return list(self._known())

    def _known(self) -> dict:
        got = state.get(STATE_KEY)
        return dict(got) if isinstance(got, dict) else {}

    def _remember(self, bulbs: list[Bulb]) -> None:
        known = self._known()
        for b in bulbs:
            known[b.mac] = {"ip": b.ip, "module": b.module,
                            "cct": list(b.cct)}
        state.set(STATE_KEY, known)     # a fresh dict, so it is written

    # ── the wire ────────────────────────────────────────────────────────
    async def _ask(self, sends: list[tuple[str, dict]], *,
                   wait: float, resend: list[tuple[str, dict]] | None = None,
                   expect: set[str] | None = None) -> list[tuple[str, dict]]:
        """Send each (address, message), resending `resend` every
        RESEND_S, and collect replies for `wait` seconds — or until every
        address in `expect` has answered."""
        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            _Collect, local_addr=("0.0.0.0", 0), allow_broadcast=True)
        try:
            def send(batch):
                for ip, msg in batch:
                    try:
                        transport.sendto(json.dumps(msg).encode(), (ip, PORT))
                    except OSError:
                        pass
            send(sends)
            end = time.monotonic() + wait
            while (left := end - time.monotonic()) > 0:
                if expect is not None and expect <= {ip for ip, _ in proto.replies}:
                    break
                proto.event.clear()
                try:
                    await asyncio.wait_for(proto.event.wait(),
                                           min(RESEND_S, left))
                except asyncio.TimeoutError:
                    if resend:
                        send(resend)
            return proto.replies
        finally:
            transport.close()

    @staticmethod
    def _result(replies, ip: str, method: str) -> dict | None:
        for rip, msg in replies:
            if rip == ip and msg.get("method") == method:
                if "result" in msg:
                    return msg["result"]
                if "error" in msg:
                    return {"error": msg["error"]}
        return None

    # ── finding ─────────────────────────────────────────────────────────
    async def discover(self, *, sweep: bool = True) -> list[Bulb]:
        """Every bulb that answers, named, with what it shows now.
        Without `sweep`, only the bulbs it remembers — done as soon as
        they have all answered."""
        pilot = {"method": "getPilot", "params": {}}
        known = self._known()
        remembered = [(v["ip"], pilot) for v in known.values()
                      if isinstance(v, dict) and v.get("ip")]
        if sweep:
            bcast, hosts = _networks()
            first = remembered + [(b, pilot) for b in bcast] \
                + [(h, pilot) for h in hosts]
            replies = await self._ask(
                first, wait=DISCOVER_S,
                resend=remembered + [(b, pilot) for b in bcast])
        elif remembered:
            replies = await self._ask(remembered, wait=REPLY_S,
                                      resend=remembered,
                                      expect={ip for ip, _ in remembered})
        else:
            return []
        found: dict[str, Bulb] = {}
        for ip, msg in replies:
            res = msg.get("result")
            if msg.get("method") != "getPilot" or not isinstance(res, dict):
                continue
            mac = res.get("mac")
            if not mac or mac in found:
                continue
            k = known.get(mac) or {}
            found[mac] = Bulb(mac=mac, ip=ip, module=k.get("module", ""),
                              cct=tuple(k.get("cct") or (2700, 6500)),
                              pilot=res)
        # What a bulb is never changes: ask a new one once, remember it.
        new = [b for b in found.values() if not b.module]
        if new:
            sends = [(b.ip, {"method": m, "params": {}}) for b in new
                     for m in ("getSystemConfig", "getModelConfig")]
            more = await self._ask(sends, wait=REPLY_S, resend=sends)
            for b in new:
                sysc = self._result(more, b.ip, "getSystemConfig") or {}
                model = self._result(more, b.ip, "getModelConfig") or {}
                b.module = sysc.get("moduleName", "") or ""
                rng = model.get("cctRange")
                if isinstance(rng, list) and len(rng) >= 2:
                    b.cct = (int(rng[0]), int(rng[-1]))
                elif b.kind == "color":
                    b.cct = (2200, 6500)
        names = self._names()
        for b in found.values():
            entry = names.get(b.mac) or {}
            b.name, b.room = entry.get("name", ""), entry.get("room", "")
        bulbs = sorted(found.values(), key=lambda b: (b.room, b.label))
        if bulbs:
            self._remember(bulbs)
        return bulbs

    def resolve(self, target: str, bulbs: list[Bulb]) -> list[Bulb]:
        """`target`: 'all', or names, rooms, MACs (or their last digits)
        and addresses, comma-separated."""
        target = (target or "all").strip().lower()
        if target in ("all", "*", "everything", "every"):
            return list(bulbs)
        out: list[Bulb] = []
        for token in (t.strip() for t in target.split(",")):
            if not token:
                continue
            hit = [b for b in bulbs if token in (b.name.lower(), b.ip,
                                                  b.mac.lower(), b.label.lower())
                   or (len(token) >= 4 and b.mac.lower().endswith(token))]
            hit = hit or [b for b in bulbs if b.room.lower() == token]
            if not hit:
                # Named, but not among the bulbs that answered: off at the
                # wall, or off the network — not a name it doesn't know.
                for mac, entry in self._names().items():
                    if token in (str(entry.get("name", "")).lower(),
                                 str(entry.get("room", "")).lower()):
                        raise WizError(
                            f"{token!r} did not answer — switched off at the "
                            "wall, or off the network? These answered: "
                            + "; ".join(b.label for b in bulbs))
                raise WizError(
                    f"no light called {token!r}; there are: "
                    + "; ".join(b.label + (f" ({b.room})" if b.room else "")
                                for b in bulbs))
            out.extend(b for b in hit if b not in out)
        return out

    # ── setting ─────────────────────────────────────────────────────────
    async def apply(self, plans: list[tuple[Bulb, dict]]) -> list[tuple[Bulb, str | None]]:
        """Send each bulb its setPilot params; then read back what it
        shows. Returns (bulb, error or None) for each."""
        sends = [(b.ip, {"method": "setPilot", "params": p}) for b, p in plans]
        replies = await self._ask(sends, wait=REPLY_S, resend=sends,
                                  expect={b.ip for b, _ in plans})
        out = []
        for b, _p in plans:
            res = self._result(replies, b.ip, "setPilot")
            if res is None:
                out.append((b, "no answer"))
            elif "error" in res:
                err = res["error"]
                out.append((b, err.get("message", str(err))
                            if isinstance(err, dict) else str(err)))
            elif res.get("success") is False:
                out.append((b, "refused"))
            else:
                out.append((b, None))
        await self.refresh([b for b, _ in plans])
        return out

    async def refresh(self, bulbs: list[Bulb]) -> None:
        sends = [(b.ip, {"method": "getPilot", "params": {}}) for b in bulbs]
        replies = await self._ask(sends, wait=REPLY_S, resend=sends,
                                  expect={b.ip for b in bulbs})
        for b in bulbs:
            res = self._result(replies, b.ip, "getPilot")
            if res and "error" not in res:
                b.pilot = res

    def plan(self, bulb: Bulb, *, on=None, brightness=None, temp=None,
             color=None, scene=None, speed=None) -> dict:
        """setPilot params for one bulb, fitted to what it can do."""
        if on is False or brightness == 0:
            return {"state": False}
        p: dict = {"state": True}
        if brightness is not None:
            p["dimming"] = max(10, min(100, int(brightness)))
        if scene is not None:
            p["sceneId"] = parse_scene(scene)
            if speed is not None:
                p["speed"] = max(10, min(200, int(speed)))
        elif color is not None:
            if bulb.kind != "color":
                raise WizError(f"{bulb.label} is a {bulb.kind} bulb and has "
                               "no colours; give it a temp instead")
            p["r"], p["g"], p["b"] = parse_color(color)
            p["c"] = p["w"] = 0
        elif temp is not None:
            if bulb.kind not in ("color", "white"):
                raise WizError(f"{bulb.label} is a {bulb.kind} bulb and has "
                               "no white temperatures")
            lo, hi = bulb.cct
            p["temp"] = max(lo, min(hi, int(temp)))
        return p

    async def identify(self, bulbs: list[Bulb]) -> None:
        """Blink each bulb twice, then put it back as it was."""
        await self.refresh(bulbs)
        before = {b.mac: restore_params(b.pilot) for b in bulbs}
        bright = [(b, {"state": True, "dimming": 100,
                       **({"r": 5, "g": 217, "b": 232, "c": 0, "w": 0}
                          if b.kind == "color" else
                          {"temp": b.cct[1]} if b.kind == "white" else {})})
                  for b in bulbs]
        dark = [(b, {"state": False}) for b in bulbs]
        for step in (bright, dark, bright, dark, bright):
            sends = [(b.ip, {"method": "setPilot", "params": p})
                     for b, p in step]
            await self._ask(sends, wait=0.35, resend=sends,
                            expect={b.ip for b in bulbs})
        restore = [(b.ip, {"method": "setPilot", "params": before[b.mac]})
                   for b in bulbs]
        await self._ask(restore, wait=REPLY_S, resend=restore,
                        expect={b.ip for b in bulbs})
