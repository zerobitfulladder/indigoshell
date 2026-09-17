"""Network — the active connection, ethernet preferred over wifi.

Two small square dots before the name, stacked: cyan on top for traffic
sent, lime below for traffic received. Each blinks as a square wave whose
frequency follows that direction's throughput — steady and dim when the
link is quiet, up to a few Hz under load. The SSID / connection name sits
beside them, the IP underneath with its host octet picked out.

Event-driven for the connection: `nmcli monitor` reports changes and only
then do we re-read state. Throughput is sampled from the interface's
byte counters once a second and smoothed, which is deliberately slow —
the dots are a glance at whether the link is busy, not a meter. The
frame clock only runs while a dot is actually blinking.
"""

import asyncio
import logging
import math

import psutil
import skia

from .. import text, theme
from ..services import proc
from .base import Size, Widget
from .label import Label

log = logging.getLogger(__name__)


class Network(Widget):
    @property
    def animation_fps(self) -> int:
        """Enough frames to show the fastest blink's edges, none while
        both dots are steady."""
        if not self.dot or not self._ip.value:
            return 0
        fastest = max(self._f_up, self._f_down)
        if fastest <= 0.0:
            return 0
        return int(min(16, max(4, math.ceil(4.0 * fastest))))

    def __init__(self, *, ip_color: str | None = None,
                 ip_size: float = 8,          # points
                 name_color: str | None = None,
                 name_size: float | None = None,
                 host_color: str = theme.CYAN_MID,
                 dot: bool = True, dot_size: int = 5, dot_gap: int = 6,
                 dot_stack_gap: int = 3, dot_shape: str = "square",
                 dot_sent: str = theme.CYAN_BRIGHT,
                 dot_recv: str = theme.LIME_BRIGHT,
                 dot_down: str = theme.ERROR,
                 rate_floor: float = 2048.0,      # B/s: below this, steady
                 rate_ceil: float = 20e6,         # B/s: at this, blink_max
                 blink_min: float = 0.5, blink_max: float = 4.0,   # Hz
                 sample_s: float = 1.0, smoothing: float = 0.5,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        # Labels as value holders only — they carry size and colour, and
        # this widget paints them itself so it can pick the host octet
        # out of the IP and put the dots before the name.
        self._ip = Label("", size=text.pt(ip_size),
                         color=ip_color or theme.BASE_MUTED)
        self._name = Label("--", size=name_size or theme.FONT_SIZE,
                           color=name_color or theme.FG)
        self.host_color = host_color
        self.dot = dot
        self.dot_size = dot_size
        self.dot_gap = dot_gap
        self.dot_stack_gap = dot_stack_gap
        # "square": two plain dots, told apart by colour and position.
        # "arrow": up-triangle for sent, down-triangle for received.
        self.dot_shape = dot_shape
        # Sent on top in cyan, received below in lime; both red with no
        # address, since then there is no direction to tell apart.
        self.dot_sent = dot_sent
        self.dot_recv = dot_recv
        self.dot_down = dot_down
        # Throughput -> blink frequency, log-mapped between the two
        # rates: 2 KB/s is the first blink, 20 MB/s the fastest.
        self.rate_floor = rate_floor
        self.rate_ceil = rate_ceil
        self.blink_min = blink_min
        self.blink_max = blink_max
        self.sample_s = sample_s
        self.smoothing = smoothing

        self._device = ""
        self._t = 0.0
        self._f_up = 0.0
        self._f_down = 0.0
        self._rate_up = 0.0
        self._rate_down = 0.0
        self._last_counters: tuple[float, int, int] | None = None
        self._sub: proc.Subscription | None = None
        self._sampler: asyncio.Task | None = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        if self._sub is None:
            self._sub = proc.subscribe(
                ["nmcli", "monitor"], lambda _line: self._schedule(),
                on_missing=lambda: self._apply("[no nmcli]", ""))
            self._schedule()
        if self._sampler is None and self.dot:
            try:
                self._sampler = asyncio.get_running_loop().create_task(
                    self._sample_loop())
            except RuntimeError:
                pass

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        if self._sampler is not None:
            self._sampler.cancel()
            self._sampler = None

    def _schedule(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self._refresh())
        except RuntimeError:
            pass

    async def _refresh(self) -> None:
        label, ip, device = "--", "", ""
        status = await proc.run(
            ["nmcli", "-t", "-f", "device,type,state,connection",
             "device", "status"])

        eth = wifi = None
        for line in status.splitlines():
            parts = line.split(":")
            if len(parts) < 4 or parts[2] != "connected":
                continue
            name, kind, _state, conn = parts[0], parts[1], parts[2], parts[3]
            # Wired first — it's usually the path you care about.
            if kind == "ethernet" and eth is None:
                eth = (name, conn)
            elif kind == "wifi" and wifi is None:
                wifi = (name, conn)

        if eth is not None:
            device, conn_name = eth
            label = conn_name or "ETH"
        elif wifi is not None:
            device, conn_name = wifi
            for line in (await proc.run(
                    ["nmcli", "-t", "-f", "active,ssid,signal",
                     "dev", "wifi"])).splitlines():
                if line.startswith("yes:"):
                    label = line.split(":")[1] or conn_name or "--"
                    break

        if device:
            raw = await proc.run(
                ["nmcli", "-t", "-g", "IP4.ADDRESS", "device", "show", device])
            first = next((l for l in raw.splitlines() if l.strip()), "")
            ip = first.split("/")[0]

        if device != self._device:
            self._device = device
            self._last_counters = None       # counters are per interface
        self._apply(label, ip)

    def _apply(self, name: str, ip: str) -> None:
        if (name, ip) == (self._name.value, self._ip.value):
            return
        self._ip.set_value(ip)
        self._name.set_value(name)
        # The labels are not children, so their own invalidate goes
        # nowhere; the width changed, so this is a relayout.
        self.invalidate(layout=True)

    # ── throughput sampling ─────────────────────────────────────────────
    async def _sample_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.sample_s)
            try:
                self._sample(loop.time())
            except Exception:
                log.exception("network sample failed")

    def _sample(self, now: float) -> None:
        if not self._device:
            return
        counters = psutil.net_io_counters(pernic=True).get(self._device)
        if counters is None:
            return
        sent, recv = counters.bytes_sent, counters.bytes_recv
        last = self._last_counters
        self._last_counters = (now, sent, recv)
        if last is None:
            return
        dt = max(1e-3, now - last[0])
        a = self.smoothing
        self._rate_up = a * ((sent - last[1]) / dt) + (1 - a) * self._rate_up
        self._rate_down = a * ((recv - last[2]) / dt) + (1 - a) * self._rate_down
        f_up, f_down = self._blink(self._rate_up), self._blink(self._rate_down)
        if (f_up, f_down) != (self._f_up, self._f_down):
            self._f_up, self._f_down = f_up, f_down
            # Wakes the clock when a dot starts blinking; a steady one
            # that changed brightness class repaints once.
            self.invalidate()

    def _blink(self, rate: float) -> float:
        """Blink frequency for a byte rate, 0 for steady."""
        if rate < self.rate_floor:
            return 0.0
        span = math.log10(self.rate_ceil / self.rate_floor)
        k = min(1.0, math.log10(rate / self.rate_floor) / span)
        # Quantised so a rate wobbling around a value does not re-time
        # the wave every second.
        f = self.blink_min + (self.blink_max - self.blink_min) * k
        return round(f * 4) / 4

    def animate(self, t: float) -> None:
        self._t = t

    # ── layout / paint ──────────────────────────────────────────────────
    def _indent(self) -> float:
        return self.dot_size + self.dot_gap if self.dot else 0.0

    def measure(self, avail: Size) -> Size:
        name_w = text.measure(self._name.value, self._name.size)
        ip_w = text.measure(self._ip.value, self._ip.size)
        return Size(max(self._indent() + name_w, ip_w),
                    text.line_height(self._name.size) + text.line_height(self._ip.size))

    def _dot_alpha(self, f: float) -> float:
        """Square wave at `f` Hz between dim and full; steady dim at 0."""
        if f <= 0.0:
            return 0.45
        return 1.0 if (self._t * f) % 1.0 < 0.5 else 0.25

    def paint(self, canvas: skia.Canvas) -> None:
        r = self.rect
        x, y = r.left(), r.top()
        name, ip = self._name.value, self._ip.value
        nsize, isize = self._name.size, self._ip.size
        name_base = y - text.font(nsize).getMetrics().fAscent
        tx = x + self._indent()

        if self.dot:
            # Two dots, sent over received, the pair centred on the
            # name's cap height. Red and steady without an address.
            s, g = self.dot_size, self.dot_stack_gap
            top = name_base - 0.35 * nsize - (2 * s + g) / 2
            if ip:
                dots = ((self.dot_sent, self._dot_alpha(self._f_up)),
                        (self.dot_recv, self._dot_alpha(self._f_down)))
            else:
                dots = ((self.dot_down, 1.0), (self.dot_down, 1.0))
            for i, (color, alpha) in enumerate(dots):
                dy = top + i * (s + g)
                shape = skia.Path()
                if self.dot_shape == "arrow" and i == 0:      # sent: up
                    shape.moveTo(x + s / 2, dy)
                    shape.lineTo(x + s, dy + s)
                    shape.lineTo(x, dy + s)
                    shape.close()
                elif self.dot_shape == "arrow":               # received: down
                    shape.moveTo(x, dy)
                    shape.lineTo(x + s, dy)
                    shape.lineTo(x + s / 2, dy + s)
                    shape.close()
                else:
                    shape.addRect(skia.Rect.MakeXYWH(x, dy, s, s))
                if alpha >= 1.0:
                    glow = skia.Paint(AntiAlias=True)
                    glow.setColor(theme.color(color, 0.55))
                    glow.setMaskFilter(skia.MaskFilter.MakeBlur(
                        skia.kNormal_BlurStyle, 2.0))
                    canvas.drawPath(shape, glow)
                dot = skia.Paint(AntiAlias=True)
                dot.setColor(theme.color(color, alpha))
                canvas.drawPath(shape, dot)

        text.draw(canvas, name, tx, name_base, nsize, theme.color(self._name.color))

        # IP under the name, flush with the dots rather than with the
        # name's text, so the widget's left edge is one straight line.
        ip_base = (y + text.line_height(nsize)
                   - text.font(isize).getMetrics().fAscent)
        head, _, host = ip.rpartition(".")
        ix = x
        if head:
            text.draw(canvas, head + ".", ix, ip_base, isize,
                      theme.color(self._ip.color))
            ix += text.measure(head + ".", isize)
        if host:
            text.draw(canvas, host, ix, ip_base, isize,
                      theme.color(self.host_color), True)
