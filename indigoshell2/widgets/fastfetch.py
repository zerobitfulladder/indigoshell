"""System identity panel, sourced from `fastfetch --format json`.

fastfetch's normal output is an ANSI-coloured logo plus `Key: value`
lines built for a terminal. Rendering that faithfully would mean an SGR
parser and a character grid; instead this asks fastfetch for JSON and
lays the fields out in the shell's own HUD vocabulary, so the panel
matches the hardware panel rather than looking like a terminal pasted
into one. The structured form also carries raw numbers, which is what
lets memory, disk and battery draw real meters instead of printing a
percentage someone else already rounded.

Two fastfetch modules are deliberately dropped. `Terminal` and `Shell`
are detected by walking the process tree, and this runs from a daemon —
they would report `python`, not the user's terminal. The login shell is
recovered from `Title.userShell`, which fastfetch reads from passwd and
is correct regardless of who spawned it.
"""

import asyncio
import json
import logging
import os
import re

from .. import theme
from ..services import proc
from .base import Size, Widget
from .hud import HudCard, key_label, meta_label, section_header, value_label
from .label import Label
from .layout import Align, Column, Row, Spacer
from .meters import BarMeter

log = logging.getLogger(__name__)

CMD = ["fastfetch", "--format", "json"]
KEY_CHARS = 10          # widest key is "PACKAGES"/"RESOLUTION" + breathing room
GiB = 1024 ** 3


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def _fmt_uptime(ms: float) -> str:
    """fastfetch reports uptime in milliseconds."""
    total = int(ms // 1000)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins or not parts:
        parts.append(f"{mins} min{'s' if mins != 1 else ''}")
    return ", ".join(parts)


class _Rows:
    """Accumulates `KEY  value` rows, skipping anything fastfetch left
    empty — a machine with no battery should show no BATTERY row, not a
    row saying nothing."""

    def __init__(self) -> None:
        self.widgets: list[Widget] = []

    def add(self, key: str, value: str | None) -> None:
        if not value:
            return
        self.widgets.append(Row(
            [key_label(key, width_chars=KEY_CHARS),
             value_label(value, align="left", color=theme.FG_STRONG)],
            spacing=10, align=Align.CENTER))

    def meter(self, key: str, pct: float, value: str,
              color=theme.CYAN_BRIGHT) -> None:
        bar = BarMeter(color=color, min_width=150, thick=6)
        bar.set_value(pct)
        self.widgets.append(Row(
            [key_label(key, width_chars=KEY_CHARS), bar, Spacer(),
             meta_label(value, color=theme.FG)],
            spacing=10, align=Align.CENTER))

    def card(self, accent: str) -> Widget | None:
        if not self.widgets:
            return None
        return HudCard(Column(self.widgets, spacing=5, align=Align.STRETCH),
                       accent=accent)


class Fastfetch(Widget):
    """Renders one `fastfetch --format json` run.

    Sampled once per panel open rather than on a clock: every field here
    is either static or slow (uptime, package count), and the panel is
    transient, so a timer would only burn a subprocess while nobody is
    looking.
    """

    def __init__(self, *, width: int = 520, **kwargs) -> None:
        super().__init__(**kwargs)
        self.width = width
        self._pending = False
        self._status = Label("querying fastfetch…", size=theme.FONT_SIZE - 3,
                             color=theme.BASE_MUTED)
        self._column = Column([self._status], spacing=10, align=Align.STRETCH)

    def children(self):
        return (self._column,)

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        self._refresh()

    def _refresh(self) -> None:
        if self._pending:
            return
        self._pending = True
        try:
            asyncio.get_running_loop().create_task(self._query())
        except RuntimeError:
            self._pending = False

    async def _query(self) -> None:
        try:
            raw = await proc.run(CMD, timeout=6.0)
            if not raw.strip():
                self._fail("fastfetch not available")
                return
            try:
                modules = json.loads(raw)
            except json.JSONDecodeError:
                self._fail("fastfetch returned no usable JSON")
                return
            self._build({m.get("type"): m.get("result") for m in modules
                         if isinstance(m, dict)})
        finally:
            self._pending = False

    def _fail(self, message: str) -> None:
        self._status.color = theme.ERROR
        self._status.set_value(message)
        self._column.replace([self._status])
        self.invalidate(layout=True)

    # ── field extraction ────────────────────────────────────────────────
    def _build(self, mods: dict) -> None:
        def get(name, *path, default=None):
            node = mods.get(name)
            for key in path:
                if not isinstance(node, dict):
                    return default
                node = node.get(key)
            return default if node is None else node

        def first(name):
            node = mods.get(name)
            return node[0] if isinstance(node, list) and node else None

        cards: list[Widget] = []

        # ── identity ────────────────────────────────────────────────────
        user = get("Title", "userName")
        host = get("Title", "hostName")
        header = section_header(f"{user}@{host}" if user and host else "SYSTEM")

        # ── system ──────────────────────────────────────────────────────
        rows = _Rows()
        arch = get("Kernel", "architecture")
        os_name = get("OS", "prettyName") or get("OS", "name")
        rows.add("OS", f"{os_name} {arch}".strip() if os_name else None)
        rows.add("HOST", get("Host", "name"))
        kernel = get("Kernel", "release")
        rows.add("KERNEL", f"{get('Kernel', 'name') or ''} {kernel}".strip()
                 if kernel else None)
        uptime = get("Uptime", "uptime")
        rows.add("UPTIME", _fmt_uptime(uptime) if uptime is not None else None)
        rows.add("PACKAGES", self._packages(mods.get("Packages")))
        shell = get("Title", "userShell")
        rows.add("SHELL", os.path.basename(shell) if shell else None)
        card = rows.card(theme.CYAN_BRIGHT)
        if card:
            cards.append(card)

        # ── desktop ─────────────────────────────────────────────────────
        rows = _Rows()
        wm, proto = get("WM", "prettyName"), get("WM", "protocolName")
        rows.add("WM", f"{wm} ({proto})" if wm and proto else wm)
        rows.add("DE", get("DE", "prettyName"))
        rows.add("DISPLAY", self._displays(mods.get("Display")))
        rows.add("THEME", get("Theme", "theme1"))
        rows.add("ICONS", get("Icons", "icons1"))
        cursor, size = get("Cursor", "theme"), get("Cursor", "size")
        rows.add("CURSOR", f"{cursor} ({size}px)" if cursor and size else cursor)
        locale = mods.get("Locale")
        rows.add("LOCALE", locale if isinstance(locale, str) else None)
        card = rows.card(theme.VIOLET_BRIGHT)
        if card:
            cards.append(card)

        # ── hardware ────────────────────────────────────────────────────
        rows = _Rows()
        rows.add("CPU", self._cpu(mods.get("CPU")))
        for gpu in (mods.get("GPU") or []):
            if isinstance(gpu, dict) and gpu.get("name"):
                vendor = gpu.get("vendor") or ""
                name = gpu["name"]
                label = name if name.startswith(vendor) else f"{vendor} {name}".strip()
                rows.add("GPU", label)
        used, total = get("Memory", "used"), get("Memory", "total")
        if used is not None and total:
            rows.meter("MEMORY", used / total * 100,
                       f"{_fmt_bytes(used)} / {_fmt_bytes(total)}",
                       color=theme.VIOLET_BRIGHT)
        disk = first("Disk")
        if isinstance(disk, dict):
            b = disk.get("bytes") or {}
            if b.get("total"):
                rows.meter("DISK", b.get("used", 0) / b["total"] * 100,
                           f"{_fmt_bytes(b.get('used', 0))} /"
                           f" {_fmt_bytes(b['total'])}")
        battery = first("Battery")
        if isinstance(battery, dict) and battery.get("capacity") is not None:
            cap = float(battery["capacity"])
            status = ", ".join(battery.get("status") or []) or "—"
            rows.meter("BATTERY", cap, f"{cap:.0f}%  {status}",
                       color=theme.LIME_BRIGHT if cap > 25 else theme.ERROR)
        card = rows.card(theme.MAGENTA_BRIGHT)
        if card:
            cards.append(card)

        # ── network ─────────────────────────────────────────────────────
        rows = _Rows()
        for iface in (mods.get("LocalIp") or []):
            if isinstance(iface, dict) and iface.get("ipv4"):
                rows.add(iface.get("name", "IP")[:KEY_CHARS].upper(),
                         iface["ipv4"])
        card = rows.card(theme.YELLOW_BRIGHT)
        if card:
            cards.append(card)

        self._column.replace([header, *cards])
        self.invalidate(layout=True)

    @staticmethod
    def _packages(node) -> str | None:
        if not isinstance(node, dict):
            return None
        total = node.get("all")
        # fastfetch keys these camelCase and scoped: `flatpakSystem`,
        # `nixUser`. Split the scope back off so the row reads the way
        # fastfetch's own output does — "792 pacman", not "pacman 792".
        managers = [f"{count} {re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name).lower()}"
                    for name, count in sorted(node.items(),
                                              key=lambda kv: -kv[1]
                                              if isinstance(kv[1], int) else 0)
                    if name != "all" and isinstance(count, int) and count]
        if total is None:
            return ", ".join(managers) or None
        return f"{total} ({', '.join(managers)})" if managers else str(total)

    @staticmethod
    def _cpu(node) -> str | None:
        if not isinstance(node, dict) or not node.get("cpu"):
            return None
        name = node["cpu"]
        logical = (node.get("cores") or {}).get("logical")
        if logical:
            name += f" ({logical})"
        max_mhz = (node.get("frequency") or {}).get("max")
        if max_mhz:
            name += f" @ {max_mhz / 1000:.2f} GHz"
        return name

    @staticmethod
    def _displays(node) -> str | None:
        if not isinstance(node, list):
            return None
        out = []
        for d in node:
            if not isinstance(d, dict):
                continue
            o = d.get("output") or {}
            w, h, hz = o.get("width"), o.get("height"), o.get("refreshRate")
            if not (w and h):
                continue
            out.append(f"{w}x{h}" + (f" @ {hz:.0f} Hz" if hz else ""))
        return ", ".join(out) or None

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        inner = self._column.measure(Size(min(self.width, avail.width),
                                          avail.height))
        return Size(max(self.width, inner.width), inner.height)

    def arrange(self, rect) -> None:
        self.rect = rect
        self._column.measure(Size(rect.width(), rect.height()))
        self._column.arrange(rect)

    def paint(self, canvas) -> None:
        self._column.paint(canvas)
