"""Audio menu: pick the default output or input device.

Two stages — OUTPUT or INPUT, then the devices of that kind with the
current default lit. Labels are PulseAudio's descriptions ("Built-in
Audio Analog Stereo") rather than its names, which are unreadable.
"""

import json

from ..plugin import Item, Menu
from ..services import proc


async def _devices(kind: str) -> list[Item]:
    """`kind` is "sink" or "source"."""
    default = (await proc.run(["pactl", f"get-default-{kind}"])).strip()
    items: list[Item] = []
    raw = await proc.run(["pactl", "--format=json", "list", f"{kind}s"])
    try:
        entries = json.loads(raw) if raw.strip() else []
    except ValueError:
        entries = []
    if entries:
        for entry in entries:
            name = entry.get("name", "")
            # Monitors of sinks are sources too, but never the one wanted.
            if kind == "source" and name.endswith(".monitor"):
                continue
            label = (entry.get("description") or name).upper()
            items.append(Item(label, ["pactl", f"set-default-{kind}", name],
                              active=(name == default)))
    else:
        # Older pactl without --format=json: fall back to the short list.
        for line in (await proc.run(["pactl", "list", "short", f"{kind}s"])).splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[1]
            if kind == "source" and name.endswith(".monitor"):
                continue
            items.append(Item(name.upper(), ["pactl", f"set-default-{kind}", name],
                              active=(name == default)))
    return items or [Item("NO DEVICES")]


MENUS = [
    Menu("audio", "AUDIO", [
        Item("OUTPUT", lambda: Menu("output", "OUTPUT", lambda: _devices("sink"))),
        Item("INPUT", lambda: Menu("input", "INPUT", lambda: _devices("source"))),
    ]),
]
