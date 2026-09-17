"""Network panel — live interface telemetry and a speedtest harness.

Two tabs in the shared HUD language:

  • NETWORK    — firewall state, then one card per UP interface: name,
                 SSID (wifi only), IPv4 and live up/down rates.
  • SPEEDTEST  — a `speedtest-cli` run, stream-parsed out of its stdout,
                 driving two segmented meters and the result readouts.

Ported from v1's GTK version. Three things change with the loop and the
canvas. The tab strip is `TabBar` and the pages are a slot rather than a
`Gtk.Stack`. The one-shot queries (`iw`, `systemctl`) and the speedtest
itself were a blocking `check_output` and a `GLib.io_add_watch` on a
non-blocking fd; here they are coroutines. And the interface cards are
built once and updated in place — v1 tore down and rebuilt every card on
every 250ms tick, which is affordable with GTK widgets you are about to
`show_all()` anyway and pure waste when the rows are already yours.
"""

import asyncio
import logging
import math
import os
import re
import signal
import socket
import time

import psutil
import skia

from .. import text, theme
from ..services import proc
from .base import Insets, Size, Widget
from .hud import HudCard, key_label, meta_label, section_header
from .label import Label
from .layout import Align, Box, Column, Row, Spacer
from .meters import BarMeter
from .tabs import TabBar

log = logging.getLogger(__name__)

BODY = theme.FONT_SIZE - 3
IFACE_SIZE = theme.FONT_SIZE

# v1 refreshed rates at 4Hz. The panel is transient — it only exists
# while you are looking at it — so the clock runs while it is open and
# stops with it, and the rate sample is gated to v1's cadence on top.
RATE_INTERVAL = 0.25

# speedtest-cli's download/upload phases run ~10-15s on a typical link.
# The dot stream it emits isn't a percentage, so the meter is animated
# 0->95% on a fixed schedule and snapped to 100% when the result line
# for that phase is parsed.
PHASE_DURATION = 12.0

# Order matters: the first service that reports active is the one
# reported. v1 only ever asked about firewalld, so a host running ufw or
# plain nftables showed "inactive" while being perfectly well firewalled.
FIREWALLS = ("firewalld", "ufw", "nftables", "iptables")

SERVER_CHARS = 30       # elide beyond this so a long ISP name can't push
                        # the ping readout off the card

TAB_GAP_BELOW = 14      # breathing room between the tab rule and the page

# Big-but-finite box for "what's your natural size", as in systray.py.
_UNBOUNDED = Size(10_000.0, 10_000.0)


def fmt_rate(bps: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.0f} {unit}/s" if unit == "B" else f"{bps:.1f} {unit}/s"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def _is_wifi(iface: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{iface}/wireless")


def _elide(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


# ── button ──────────────────────────────────────────────────────────────
class _Button(Box):
    """`// RUN TEST` in a beveled cyan frame.

    The only button in the shell that isn't a bar widget, so it is built
    from `Box` — background, border, bevel and hover are all already
    there; the click handler is what makes it hit-testable.
    """

    def __init__(self, label: str, on_click, **kwargs) -> None:
        self._label = Label(label, size=BODY, bold=True,
                            color=theme.HIGHLIGHT, tracking=1.5)
        super().__init__(
            Row([Label("//", size=BODY, bold=True, color=theme.YELLOW_MID),
                 self._label], spacing=8, align=Align.CENTER),
            padding=Insets.xy(18, 7),
            background="#0a203075",          # v1: rgba(10, 32, 48, 0.46)
            hover_background="#05d9e833",    # v1: rgba(5, 217, 232, 0.20)
            border=theme.HIGHLIGHT, border_width=1.2,
            bevel=8, bevel_corners=("top-right", "bottom-left"),
            on_left_click=on_click, **kwargs)

    def set_label(self, value: str) -> None:
        self._label.set_value(value)

    def set_hovered(self, value: bool) -> None:
        super().set_hovered(value)
        # v1 lit the border as well as the fill on hover.
        self.border = theme.YELLOW_BRIGHT if value else theme.HIGHLIGHT


# ── readouts ────────────────────────────────────────────────────────────
def _value_pair(unit_chars: int, color: str) -> tuple[Row, Label, Label]:
    """A right-aligned number against a muted unit in a fixed cell.

    Two labels rather than one string so the unit stays muted — v1's
    `.result-unit` — and so the number's column doesn't shift as digits
    come and go mid-test.
    """
    number = Label("—", size=BODY, bold=True, color=color, align="right",
                   min_width=text.advance(BODY, True, 7))
    unit = Label("", size=BODY, color=theme.BASE_MUTED,
                 min_width=text.advance(BODY, False, unit_chars))
    return Row([number, unit], spacing=5, align=Align.BASELINE), number, unit


class _IfaceCard(HudCard):
    """One interface, built once and updated in place."""

    def __init__(self, iface: str) -> None:
        self.iface = iface
        self._name = Label(iface, size=IFACE_SIZE, bold=True,
                           color=theme.CYAN_BRIGHT, tracking=2.0)
        self._ssid = Label("", size=BODY, bold=True,
                           color=theme.YELLOW_BRIGHT, align="right")
        self._ip = Label("—", size=BODY, color=theme.FG)
        self._down = Label("↓ —", size=BODY, bold=True,
                           color=theme.MAGENTA_BRIGHT)
        self._up = Label("— ↑", size=BODY, bold=True,
                         color=theme.YELLOW_BRIGHT, align="right")
        super().__init__(Column([
            Row([self._name, Spacer(), self._ssid], spacing=10,
                align=Align.BASELINE),
            Row([key_label("ADDR"), self._ip], spacing=8,
                align=Align.BASELINE),
            Row([self._down, Spacer(), self._up], spacing=24,
                align=Align.BASELINE),
        ], spacing=6, align=Align.STRETCH), accent=theme.HIGHLIGHT)

    def update(self, ip: str, ssid: str | None, up: float, down: float) -> None:
        self._ip.set_value(ip or "—")
        self._ssid.set_value(ssid or "")
        self._down.set_value(f"↓ {fmt_rate(down)}")
        self._up.set_value(f"{fmt_rate(up)} ↑")


class _Pages(Widget):
    """Holds one visible page; the rest keep their last arrangement.

    Every page stays in `children()`, not just the active one — that is
    the walk `attach()` uses, and a widget whose `window` was never set
    has an `invalidate()` that silently does nothing.
    """

    def __init__(self, pages, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pages = list(pages)
        self.active = 0

    def children(self):
        return tuple(self.pages)

    def set_active(self, index: int) -> None:
        self.active = index

    def hit(self, x: float, y: float) -> Widget | None:
        if not self.pages or not self.rect.contains(x, y):
            return None
        return self.pages[self.active].hit(x, y)

    def measure(self, avail: Size) -> Size:
        if not self.pages:
            return Size(avail.width, 0.0)
        return Size(avail.width, self.pages[self.active].measure(avail).height)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        if not self.pages:
            return
        page = self.pages[self.active]
        natural = page.measure(Size(rect.width(), rect.height()))
        page.arrange(skia.Rect.MakeXYWH(rect.left(), rect.top(),
                                        rect.width(), natural.height))

    def paint(self, canvas: skia.Canvas) -> None:
        if self.pages:
            self.pages[self.active].paint(canvas)


# ── panel ───────────────────────────────────────────────────────────────
class NetworkPanel(Widget):
    animation_fps = 12

    def __init__(self, *, width: int = 560, **kwargs) -> None:
        super().__init__(**kwargs)
        self.width = width
        self._counters: dict[str, tuple[int, int, float]] = {}
        self._cards: dict[str, _IfaceCard] = {}
        self._keys: tuple[str, ...] = ()
        self._ssids: dict[str, str | None] = {}
        self._rate_acc = 0.0
        self._firewall = ("unknown", "")
        self._firewall_pending = False
        self._firewall_acc = 0.0

        # ── network page ────────────────────────────────────────────────
        self._dot = Label("●", size=BODY, bold=True,
                          color=lambda: self._firewall_color())
        self._state = Label("unknown", size=BODY, bold=True, tracking=1.0,
                            color=lambda: self._firewall_color())
        self._service = meta_label()
        self._iface_column = Column([], spacing=10, align=Align.STRETCH)
        network = Column([
            section_header("NETLINK", "LIVE INTERFACE TELEMETRY"),
            Row([key_label("FIREWALL"), self._dot, self._state,
                 self._service, Spacer()], spacing=8, align=Align.BASELINE),
            self._iface_column,
        ], spacing=12, align=Align.STRETCH)

        # ── speedtest page ──────────────────────────────────────────────
        self._server = Label("idle", size=BODY, color=theme.BASE_MUTED)
        ping_row, self._ping, self._ping_unit = _value_pair(
            3, theme.YELLOW_BRIGHT)
        self._down_meter = BarMeter(color=theme.MAGENTA_BRIGHT,
                                    dim_color=theme.NOTIF_METER_DIM,
                                    tick=5, gap=theme.NOTIF_METER_GAP,
                                    thick=theme.NOTIF_METER_THICK,
                                    min_width=200, flex=1)
        self._up_meter = BarMeter(color=theme.YELLOW_BRIGHT,
                                  dim_color=theme.NOTIF_METER_DIM,
                                  tick=5, gap=theme.NOTIF_METER_GAP,
                                  thick=theme.NOTIF_METER_THICK,
                                  min_width=200, flex=1)
        down_row, self._down_value, self._down_unit = _value_pair(
            8, theme.MAGENTA_BRIGHT)
        up_row, self._up_value, self._up_unit = _value_pair(
            8, theme.YELLOW_BRIGHT)
        self._button = _Button("RUN TEST", self._toggle_speedtest)
        speedtest = Column([
            section_header("THROUGHPUT", "SECURE SPEEDTEST ROUTINE"),
            HudCard(Column([
                Row([key_label("SERVER"), self._server, Spacer(),
                     key_label("PING"), ping_row], spacing=8,
                    align=Align.BASELINE),
                self._speed_row("DN", "↓", theme.MAGENTA_BRIGHT,
                                self._down_meter, down_row),
                self._speed_row("UP", "↑", theme.YELLOW_BRIGHT,
                                self._up_meter, up_row),
                Row([Spacer(), self._button], spacing=0, align=Align.CENTER),
            ], spacing=10, align=Align.STRETCH), accent=theme.MAGENTA_BRIGHT),
        ], spacing=12, align=Align.STRETCH)

        self._pages = _Pages([network, speedtest])
        self._bar = TabBar(["NETWORK", "SPEEDTEST"], self._switch)
        # The gap is explicit rather than the Column's spacing because
        # the trailing Spacer must stay flush: TabBar already reserves
        # its own rule gap, and a page opens on a bare section header
        # rather than a padded card, so without this the heading sits
        # right on the rule.
        self._column = Column(
            [self._bar, Spacer(size=TAB_GAP_BELOW), self._pages, Spacer()],
            spacing=0, align=Align.STRETCH)

        # ── speedtest state ─────────────────────────────────────────────
        self._st_task: asyncio.Task | None = None
        self._st_proc: asyncio.subprocess.Process | None = None
        self._st_buf = ""
        self._st_phase = "idle"
        self._st_server_set = False
        self._st_target: str | None = None
        self._st_started = 0.0
        self._st_canceled = False

    def _speed_row(self, tag: str, arrow: str, color: str,
                   meter: BarMeter, value: Row) -> Widget:
        return Row([key_label(tag, bold=True),
                    Label(arrow, size=BODY, bold=True, color=color),
                    meter, value],
                   spacing=10, align=Align.CENTER)

    def children(self):
        return (self._column,)

    def _switch(self, index: int) -> None:
        # TabBar.click() has already moved its own highlight, but this is
        # also the programmatic entry point, so it sets both. set_active
        # is a no-op when the index already matches.
        self._bar.set_active(index)
        self._pages.set_active(index)
        self.invalidate(layout=True)
        self._refit()

    def _refit(self) -> None:
        """Size the host window to the content.

        The other panels are fixed-size because their content is — a
        fastfetch dump and a hardware readout come out the same height
        every time, and their specs say so. This one is a tab away from
        one height and an interface away from another, so a fixed size is
        either too tight for three interfaces or mostly empty for one.
        The spec's size is the opening guess; this is the correction.

        Called before the window is mapped on open (from `attach`) and on
        the two things that can change the height afterwards: switching
        tab, and an interface appearing or going away.
        """
        window = self.window
        content = getattr(window, "content", None)
        if content is None:
            return
        natural = content.measure(_UNBOUNDED)
        window.resize_content(math.ceil(natural.width),
                              math.ceil(natural.height))

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        # psutil only — no subprocess — so the cards are already on the
        # first frame the spawn animation freezes and shows, and the
        # refit below has a real height to work from.
        self._sample_rates()
        self._refit()

    def on_shown(self) -> None:
        super().on_shown()
        # Everything that forks waits until the window is up: the spawn
        # transition renders from a frozen snapshot, so a value arriving
        # during it can't be seen anyway, while the work to fetch it
        # stalls the loop that is trying to animate.
        self._poll_firewall()
        self._resolve_ssids()

    def detach(self) -> None:
        # A speedtest is a 30s network job; it must not outlive the panel
        # that asked for it.
        self._kill_speedtest(signal.SIGKILL)
        self._st_task = None
        super().detach()

    def _spawn(self, coro) -> bool:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return False
        return True

    # ── firewall ────────────────────────────────────────────────────────
    def _firewall_color(self) -> str:
        state = self._firewall[0]
        if state == "active":
            return theme.LIME_BRIGHT
        if state in ("inactive", "failed", "unknown"):
            return theme.MAGENTA_BRIGHT
        return theme.BASE_MUTED

    def _poll_firewall(self) -> None:
        # The in-flight guard is cleared by the query's `finally`, so a
        # spawn that never happened has to clear it here — otherwise one
        # failure to schedule stops the poll for the panel's lifetime.
        if not self._firewall_pending:
            self._firewall_pending = self._spawn(self._query_firewall())

    async def _query_firewall(self) -> None:
        try:
            state, service = "inactive", ""
            for name in FIREWALLS:
                out = (await proc.run(
                    ["systemctl", "is-active", name], timeout=2.0)).strip()
                # `is-active` exits non-zero when it isn't, and proc.run
                # keeps stdout either way — "unknown" means no such unit.
                if out == "active":
                    state, service = "active", name
                    break
                if out == "failed":
                    state, service = "failed", name
                    break
            if (state, service) != self._firewall:
                self._firewall = (state, service)
                self._state.set_value(state)
                self._service.set_value(service)
                self.invalidate(layout=True)
        finally:
            self._firewall_pending = False

    # ── interfaces ──────────────────────────────────────────────────────
    def _resolve_ssids(self) -> None:
        for iface in self._keys:
            if _is_wifi(iface) and iface not in self._ssids:
                self._spawn(self._query_ssid(iface))

    async def _query_ssid(self, iface: str) -> None:
        """Connected SSID for a wireless interface, via `iw dev` so it
        works without NetworkManager."""
        self._ssids[iface] = None
        out = await proc.run(["iw", "dev", iface, "link"], timeout=2.0)
        match = re.search(r"^\s*SSID:\s*(.+)$", out, re.MULTILINE)
        self._ssids[iface] = match.group(1).strip() if match else None

    def _sample_rates(self) -> None:
        now = time.monotonic()
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)

        rows = []
        for iface, addr_list in addrs.items():
            if iface == "lo":
                continue
            stat = stats.get(iface)
            if stat is None or not stat.isup:
                continue
            ip = next((a.address for a in addr_list
                       if a.family == socket.AF_INET), None)
            if not ip:
                continue
            up = down = 0.0
            counter = counters.get(iface)
            if counter is not None:
                previous = self._counters.get(iface)
                if previous is not None:
                    sent, recv, then = previous
                    dt = now - then
                    if dt > 0:
                        up = max(0.0, (counter.bytes_sent - sent) / dt)
                        down = max(0.0, (counter.bytes_recv - recv) / dt)
                self._counters[iface] = (counter.bytes_sent,
                                         counter.bytes_recv, now)
            rows.append((iface, ip, up, down))

        keys = tuple(row[0] for row in rows)
        if keys != self._keys:
            self._keys = keys
            self._rebuild_cards(keys)
            self._resolve_ssids()
            self._refit()
        for iface, ip, up, down in rows:
            self._cards[iface].update(ip, self._ssids.get(iface), up, down)

    def _rebuild_cards(self, keys: tuple[str, ...]) -> None:
        """Swap the card list only when the interface *set* changes.

        Rates change four times a second and the card for an interface
        that is still there has nothing to rebuild — only an appearing or
        disappearing interface costs a relayout.
        """
        for iface in list(self._cards):
            if iface not in keys:
                del self._cards[iface]
        for iface in keys:
            if iface not in self._cards:
                self._cards[iface] = _IfaceCard(iface)
        if keys:
            self._iface_column.replace([self._cards[k] for k in keys])
        else:
            self._iface_column.replace([HudCard(
                Label("no active interface", size=BODY,
                      color=theme.BASE_MUTED),
                accent=theme.MAGENTA_DIM)])

    # ── speedtest ───────────────────────────────────────────────────────
    def _toggle_speedtest(self, _source=None) -> None:
        if self._st_task is not None:
            self._st_canceled = True
            self._kill_speedtest(signal.SIGTERM)
            return
        self._st_task = asyncio.get_running_loop().create_task(
            self._run_speedtest())

    def _kill_speedtest(self, sig: int) -> None:
        process = self._st_proc
        if process is None or process.returncode is not None:
            return
        try:
            # Its own session, so the whole group goes — speedtest-cli
            # spawns workers that ignore a signal to the parent alone.
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _reset_speedtest(self) -> None:
        self._st_buf = ""
        self._st_phase = "starting"
        self._st_server_set = False
        self._st_target = None
        self._st_canceled = False
        self._server.color = theme.BASE_MUTED
        self._server.set_value("connecting…")
        for label, unit in ((self._ping, self._ping_unit),
                            (self._down_value, self._down_unit),
                            (self._up_value, self._up_unit)):
            label.set_value("—")
            unit.set_value("")
        self._down_meter.set_value(0)
        self._up_meter.set_value(0)
        self._button.set_label("CANCEL")

    async def _run_speedtest(self) -> None:
        self._reset_speedtest()
        process = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    "speedtest-cli", "--secure",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
            except (OSError, FileNotFoundError):
                self._fail("speedtest-cli not installed")
                return
            self._st_proc = process
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                # Read in chunks, not lines: the progress dots arrive
                # without a newline until the phase is over, so a line
                # reader sees nothing for ten seconds at a time.
                self._st_buf += chunk.decode(errors="replace")
                self._parse_speedtest()
            await process.wait()
        except Exception:
            log.exception("speedtest failed")
        finally:
            if process is not None and self._st_proc is process:
                self._st_proc = None
            self._finish_speedtest()

    def _fail(self, message: str) -> None:
        self._st_phase = "error"
        self._server.color = theme.BASE_MUTED
        self._server.set_value(message)

    def _finish_speedtest(self) -> None:
        self._st_task = None
        self._st_target = None
        if self._st_phase not in ("done", "error"):
            self._fail("canceled" if self._st_canceled else "speedtest failed")
        self._button.set_label("RUN TEST")
        self.invalidate(layout=True)

    def _parse_speedtest(self) -> None:
        buf = self._st_buf

        if not self._st_server_set:
            match = re.search(r"Hosted by (.+?) \[.+?\]:\s*([\d.]+)\s*ms", buf)
            if match:
                self._st_server_set = True
                self._server.color = theme.CYAN_BRIGHT
                self._server.set_value(_elide(match.group(1), SERVER_CHARS))
                self._ping.set_value(match.group(2))
                self._ping_unit.set_value("ms")

        if self._st_phase == "starting" and "Testing download speed" in buf:
            self._st_phase = "download"
            self._start_phase("download")

        if self._st_phase == "download":
            match = re.search(r"Download:\s+([\d.]+)\s+(\S+)", buf)
            if match:
                self._down_value.set_value(match.group(1))
                self._down_unit.set_value(match.group(2))
                self._down_meter.set_value(100)
                self._st_target = None
                self._st_phase = "between"

        if self._st_phase == "between" and "Testing upload speed" in buf:
            self._st_phase = "upload"
            self._start_phase("upload")

        if self._st_phase == "upload":
            match = re.search(r"Upload:\s+([\d.]+)\s+(\S+)", buf)
            if match:
                self._up_value.set_value(match.group(1))
                self._up_unit.set_value(match.group(2))
                self._up_meter.set_value(100)
                self._st_target = None
                self._st_phase = "done"

    def _start_phase(self, target: str) -> None:
        self._st_target = target
        self._st_started = time.monotonic()

    # ── frame clock ─────────────────────────────────────────────────────
    def animate(self, t: float) -> None:
        dt = self.tick_dt(t)

        self._rate_acc += dt
        if self._rate_acc >= RATE_INTERVAL:
            self._rate_acc = 0.0
            self._sample_rates()

        # Firewall state changes rarely — v1 polled it every ~5s.
        self._firewall_acc += dt
        if self._firewall_acc >= 5.0:
            self._firewall_acc = 0.0
            self._poll_firewall()

        if self._st_target is not None:
            pct = min(95.0, (time.monotonic() - self._st_started)
                      / PHASE_DURATION * 100.0)
            meter = (self._down_meter if self._st_target == "download"
                     else self._up_meter)
            meter.set_value(pct)

    # ── layout / paint ──────────────────────────────────────────────────
    def measure(self, avail: Size) -> Size:
        inner = self._column.measure(Size(min(self.width, avail.width),
                                          avail.height))
        return Size(max(self.width, inner.width), inner.height)

    def arrange(self, rect: skia.Rect) -> None:
        self.rect = rect
        self._column.measure(Size(rect.width(), rect.height()))
        self._column.arrange(rect)

    def paint(self, canvas: skia.Canvas) -> None:
        self._column.paint(canvas)
