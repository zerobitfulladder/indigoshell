"""Cyberpunk HUD-style network panel.

A tabbed popup with two pages:

  • NETWORK    — one card per UP interface: name, SSID (wifi only),
                 IPv4, and live up/down rates.
  • SPEEDTEST  — runnable `speedtest-cli` harness. Streaming-parse the
                 stdout; animate two segmented progress meters (same
                 recipe as the notification toast); show server /
                 ping / final results.

Tabs use compact custom chrome and the page bodies live in a Gtk.Stack
so switching is a cheap visibility flip.
"""

import os
import re
import socket
import subprocess
import time

import psutil

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from .base import Widget, paint
from .hud import HudCard as _HudCard, TabBar as _TabBar


# ── small helpers ──────────────────────────────────────────────────────
def _fmt_rate(bps: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.0f} {unit}/s" if unit == "B" else f"{bps:.1f} {unit}/s"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def _is_wifi(iface: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{iface}/wireless")


def _ssid_for(iface: str) -> str | None:
    """Connected SSID for a wireless iface, or None. Uses `iw dev`
    so it works without NetworkManager."""
    if not _is_wifi(iface):
        return None
    try:
        out = subprocess.check_output(
            ["iw", "dev", iface, "link"], stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return None
    m = re.search(r"^\s*SSID:\s*(.+)$", out, re.MULTILINE)
    return m.group(1).strip() if m else None


def _pango_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str, css_class: str | None = None, *, xalign: float = 0.0) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    if css_class:
        lbl.get_style_context().add_class(css_class)
    lbl.set_xalign(xalign)
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


# ── segmented progress meter (notification toast recipe) ──────────────
class _Meter(Gtk.DrawingArea):
    _MIN_WIDTH = 200

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(self._MIN_WIDTH, theme.NOTIF_METER_THICK + 4)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)
        self._progress = 0.0
        self._color = theme.MAGENTA_BRIGHT
        self.connect("draw", self._on_draw)

    def set_progress(self, p: float) -> None:
        self._progress = max(0.0, min(100.0, p))
        self.queue_draw()

    def set_color(self, hex_color: str) -> None:
        self._color = hex_color

    def _on_draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        n   = theme.NOTIF_METER_SEGMENTS
        gap = theme.NOTIF_METER_GAP
        h   = theme.NOTIF_METER_THICK
        usable_w = max(1, width)
        tick_w = max(1.0, (usable_w - gap * (n - 1)) / n)
        ratio = self._progress / 100.0
        fill_end_x = ratio * usable_w
        y = (height - h) / 2
        for i in range(n):
            x = i * (tick_w + gap)
            mid = x + tick_w / 2
            color = self._color if mid <= fill_end_x else theme.NOTIF_METER_DIM
            paint(cr, color)
            cr.rectangle(x, y, tick_w, h)
            cr.fill()
        return False


# ── main panel ─────────────────────────────────────────────────────────
class NetworkPanel(Widget):
    # 4Hz refresh — rates and the firewall pip stay snappy while the
    # popup is open (and the popup is non-persistent, so the timer is
    # only running while you're looking at it).
    interval_ms = 250

    # speedtest-cli's download/upload phases run ~10–15s on a typical
    # link. The dot stream isn't a percentage, so we animate 0→95% on
    # a fixed schedule and snap to 100% when the result line is parsed.
    _PHASE_DURATION_SEC = 12.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._iface_box: Gtk.Box | None = None
        self._counters: dict[str, tuple[int, int, float]] = {}
        self._iface_keys: tuple[str, ...] = ()
        self._firewall_lbl: Gtk.Label | None = None
        self._firewall_last: str | None = None
        self._firewall_tick: int = 0

        # speedtest state
        self._st_proc: subprocess.Popen | None = None
        self._st_watch_id: int | None = None
        self._st_buf: str = ""
        self._st_phase: str = "idle"
        self._st_server_set: bool = False
        self._st_anim_id: int | None = None
        self._st_anim_target: str | None = None
        self._st_anim_start: float = 0.0

        # speedtest widgets — populated in build_widget
        self._st_server_lbl: Gtk.Label | None = None
        self._st_ping_lbl: Gtk.Label | None = None
        self._st_dn_meter: _Meter | None = None
        self._st_up_meter: _Meter | None = None
        self._st_dn_val: Gtk.Label | None = None
        self._st_up_val: Gtk.Label | None = None
        self._st_button: Gtk.Button | None = None
        self._st_button_lbl: Gtk.Label | None = None

    # ── construction ──────────────────────────────────────────────────
    def build_widget(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(140)

        stack.add_named(self._build_network_page(), "NETWORK")
        stack.add_named(self._build_speedtest_page(), "SPEEDTEST")

        tabs = _TabBar(["NETWORK", "SPEEDTEST"], stack.set_visible_child_name)
        outer.pack_start(tabs, False, False, 0)
        outer.pack_start(stack, True, True, 0)
        return outer

    def default_css(self) -> str:
        # One body font-size for everything in the panel; per-role
        # classes layer on color/weight/family overrides without
        # repeating the size everywhere.
        sel = f"#{self.name}"
        body  = theme.FONT_SIZE - 3   # 13 by default
        small = theme.FONT_SIZE - 5   # 11
        big   = theme.FONT_SIZE       # 16 — interface names, result values
        return (
            f"{sel} {{ background: transparent; font-size: {body}px; "
            f"font-family: {theme.FONT}; min-width: 520px; }}"
            f"{sel} label {{ color: {theme.FG}; text-shadow: none; }}"
            f"{sel} .panel-title {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-size: {small}px; font-weight: bold; letter-spacing: 2px; }}"
            f"{sel} .panel-subtitle {{ color: {theme.BASE_MUTED}; "
            f"  font-size: {small}px; }}"
            f"{sel} .label-key  {{ color: {theme.CYAN_DIM}; "
            f"  font-size: {small}px; letter-spacing: 1px; }}"
            f"{sel} .iface-name {{ color: {theme.CYAN_BRIGHT}; "
            f"  font-size: {big}px; font-weight: bold; letter-spacing: 2px; }}"
            f"{sel} .ssid       {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-weight: bold; }}"
            f"{sel} .ip         {{ color: {theme.FG}; "
            f"  font-family: monospace; }}"
            f"{sel} .rate-dn    {{ color: {theme.MAGENTA_BRIGHT}; "
            f"  font-family: monospace; font-weight: bold; }}"
            f"{sel} .rate-up    {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-family: monospace; font-weight: bold; }}"
            f"{sel} .empty      {{ color: {theme.BASE_MUTED}; font-style: italic; }}"
            f"{sel} .server-name {{ color: {theme.CYAN_BRIGHT}; font-weight: bold; }}"
            f"{sel} .server-idle {{ color: {theme.BASE_MUTED}; font-style: italic; }}"
            f"{sel} .ping       {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-family: monospace; }}"
            f"{sel} .result-dn  {{ color: {theme.MAGENTA_BRIGHT}; "
            f"  font-size: {big}px; font-weight: bold; font-family: monospace; }}"
            f"{sel} .result-up  {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-size: {big}px; font-weight: bold; font-family: monospace; }}"
            f"{sel} .result-unit {{ color: {theme.BASE_MUTED}; "
            f"  font-size: {small}px; }}"
            f"{sel} .placeholder {{ color: {theme.BASE_MUTED}; }}"
            f"{sel} button.st-btn {{"
            f"  background-image: none;"
            f"  background-color: rgba(10, 32, 48, 0.46);"
            f"  border: 1.2px solid {theme.HIGHLIGHT};"
            f"  border-radius: 0;"
            f"  padding: 6px 18px;"
            f"  min-height: 0;"
            f"}}"
            f"{sel} button.st-btn:hover {{"
            f"  background-color: rgba(5, 217, 232, 0.20);"
            f"  border-color: {theme.YELLOW_BRIGHT};"
            f"}}"
        )

    # ── network page ─────────────────────────────────────────────────
    def _build_network_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(2)
        page.pack_start(self._section_header("NETLINK", "LIVE INTERFACE TELEMETRY"),
                        False, False, 0)
        page.pack_start(self._build_status_row(), False, False, 0)
        self._iface_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.pack_start(self._iface_box, False, False, 0)
        return page

    def _build_status_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(2)
        row.pack_start(_plain("FIREWALL", "label-key"), False, False, 0)
        lbl = Gtk.Label()
        lbl.set_valign(Gtk.Align.CENTER)
        lbl.set_xalign(0.0)
        self._firewall_lbl = lbl
        row.pack_start(lbl, False, False, 0)
        # Reset the cached state — the new label starts at "unknown",
        # so the next poll must re-render even if the underlying state
        # matches what we saw before close.
        self._firewall_last = None
        self._firewall_tick = 0
        self._render_firewall("unknown")
        return row

    def _render_firewall(self, state: str) -> None:
        if self._firewall_lbl is None:
            return
        if state == "active":
            dot, color, text = theme.LIME_BRIGHT, theme.LIME_BRIGHT, "active"
        elif state in ("inactive", "failed", "unknown"):
            dot, color, text = theme.MAGENTA_BRIGHT, theme.MAGENTA_BRIGHT, state
        else:
            dot, color, text = theme.BASE_MUTED, theme.BASE_MUTED, state
        self._firewall_lbl.set_markup(
            f"<span color='{dot}' weight='bold'>● </span>"
            f"<span color='{color}' weight='bold' "
            f"letter_spacing='1024'>{text}</span>"
        )

    def _poll_firewall(self) -> None:
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", "firewalld"],
                stderr=subprocess.DEVNULL, text=True, timeout=1,
            ).strip()
        except subprocess.CalledProcessError as e:
            out = (e.output or "").strip() or "inactive"
        except Exception:
            out = "unknown"
        if out != self._firewall_last:
            self._firewall_last = out
            self._render_firewall(out)

    def _section_header(self, title: str, subtitle: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(2)
        title_lbl = Gtk.Label()
        title_lbl.set_markup(
            f"<span color='{theme.CYAN_BRIGHT}' weight='bold' "
            f"letter_spacing='2048'>{title}</span>"
        )
        title_lbl.get_style_context().add_class("panel-title")
        title_lbl.set_valign(Gtk.Align.CENTER)
        sub_lbl = _plain(subtitle, "panel-subtitle")
        row.pack_start(title_lbl, False, False, 0)
        row.pack_start(sub_lbl, False, False, 0)
        return row

    def tick(self) -> bool:
        if self._iface_box is None:
            return True
        self._render_ifaces(self._gather())
        # Firewall state changes rarely — poll every 5 ticks (~5s).
        # ~5s cadence at 4Hz ticks.
        if self._firewall_tick % 20 == 0:
            self._poll_firewall()
        self._firewall_tick += 1
        return True

    def _gather(self):
        now = time.monotonic()
        addrs    = psutil.net_if_addrs()
        stats    = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)
        out = []
        for iface, addr_list in addrs.items():
            if iface == "lo":
                continue
            st = stats.get(iface)
            if not st or not st.isup:
                continue
            ip = next((a.address for a in addr_list if a.family == socket.AF_INET), None)
            if not ip:
                continue
            c = counters.get(iface)
            up = dn = 0.0
            if c is not None:
                prev = self._counters.get(iface)
                if prev is not None:
                    ls, lr, lt = prev
                    dt = now - lt
                    if dt > 0:
                        up = max(0.0, (c.bytes_sent - ls) / dt)
                        dn = max(0.0, (c.bytes_recv - lr) / dt)
                self._counters[iface] = (c.bytes_sent, c.bytes_recv, now)
            out.append((iface, ip, _ssid_for(iface), up, dn))
        return out

    def _render_ifaces(self, rows) -> None:
        assert self._iface_box is not None
        for child in self._iface_box.get_children():
            self._iface_box.remove(child)
        if not rows:
            self._iface_box.pack_start(
                _HudCard(_plain("no active interface", "empty"),
                         accent=theme.MAGENTA_DIM),
                False, False, 0,
            )
            self._iface_box.show_all()
        else:
            for (iface, ip, ssid, up, dn) in rows:
                self._iface_box.pack_start(
                    self._iface_card(iface, ip, ssid, up, dn), False, False, 0
                )
            self._iface_box.show_all()

        # Refit the popup if the interface set changed — otherwise the
        # window keeps its old size+position even as cards add/remove.
        keys = tuple(r[0] for r in rows)
        if keys != self._iface_keys:
            self._iface_keys = keys
            self._refit_popup()

    def _refit_popup(self) -> None:
        if self.gtk_widget is None:
            return
        top = self.gtk_widget.get_toplevel()
        if not isinstance(top, Gtk.Window) or not top.get_realized():
            return
        from ..core.daemon import get_daemon  # deferred: avoid import cycle
        d = get_daemon()
        name = next((n for n, w in d.instances.items() if w is top), None)
        if name is None:
            return
        kind = d.kinds.get(name)
        anchor = d.anchors.get(name)
        if kind is not None and hasattr(kind, "refit"):
            kind.refit(top, anchor)

    def _iface_card(
        self, iface: str, ip: str, ssid: str | None, up: float, dn: float,
    ) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # Header row: iface (cyan, big) + SSID on the right when wifi.
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.pack_start(_plain(iface, "iface-name"), False, False, 0)
        if ssid:
            head.pack_end(_plain(ssid, "ssid"), False, False, 0)
        card.pack_start(head, False, False, 0)

        # IP row: small dim "ADDR" key + mono value.
        ip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ip_row.pack_start(_plain("ADDR", "label-key"), False, False, 0)
        ip_row.pack_start(_plain(ip, "ip"), False, False, 0)
        card.pack_start(ip_row, False, False, 0)

        # Rate row: ↓ on the left, ↑ on the right.
        rates = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        rates.set_margin_top(2)
        dn_lbl = Gtk.Label()
        dn_lbl.set_markup(
            f"<span color='{theme.MAGENTA_BRIGHT}'>↓</span>  "
            f"<span color='{theme.MAGENTA_BRIGHT}' font_family='monospace' "
            f"weight='bold'>{_fmt_rate(dn)}</span>"
        )
        up_lbl = Gtk.Label()
        up_lbl.set_markup(
            f"<span color='{theme.YELLOW_BRIGHT}' font_family='monospace' "
            f"weight='bold'>{_fmt_rate(up)}</span>  "
            f"<span color='{theme.YELLOW_BRIGHT}'>↑</span>"
        )
        rates.pack_start(dn_lbl, False, False, 0)
        rates.pack_end(up_lbl, False, False, 0)
        card.pack_start(rates, False, False, 0)
        return _HudCard(card, accent=theme.HIGHLIGHT)

    # ── speedtest page ────────────────────────────────────────────────
    def _build_speedtest_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(2)
        page.pack_start(self._section_header("THROUGHPUT", "SECURE SPEEDTEST ROUTINE"),
                        False, False, 0)

        # Server on the left, ping pushed to the right.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta.pack_start(_plain("SERVER", "label-key"), False, False, 0)
        self._st_server_lbl = _plain("idle", "server-idle")
        meta.pack_start(self._st_server_lbl, False, False, 0)
        self._st_ping_lbl = _plain("—", "placeholder")
        meta.pack_end(self._st_ping_lbl, False, False, 0)
        meta.pack_end(_plain("PING", "label-key"), False, False, 8)
        body.pack_start(meta, False, False, 0)

        # Download row.
        self._st_dn_meter = _Meter()
        self._st_dn_meter.set_color(theme.MAGENTA_BRIGHT)
        self._st_dn_val = _plain("—", "placeholder", xalign=1.0)
        self._st_dn_val.set_width_chars(12)
        body.pack_start(
            self._speed_row("DN", "↓", theme.MAGENTA_BRIGHT,
                            self._st_dn_meter, self._st_dn_val),
            False, False, 0,
        )

        # Upload row.
        self._st_up_meter = _Meter()
        self._st_up_meter.set_color(theme.YELLOW_BRIGHT)
        self._st_up_val = _plain("—", "placeholder", xalign=1.0)
        self._st_up_val.set_width_chars(12)
        body.pack_start(
            self._speed_row("UP", "↑", theme.YELLOW_BRIGHT,
                            self._st_up_meter, self._st_up_val),
            False, False, 0,
        )

        # Run/cancel button — right-aligned so the meters' value column
        # stays the visual anchor on the left.
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_row.set_margin_top(8)
        btn_row.set_halign(Gtk.Align.END)
        self._st_button = Gtk.Button()
        self._st_button.set_relief(Gtk.ReliefStyle.NONE)
        self._st_button_lbl = Gtk.Label()
        self._st_button_lbl.set_markup(self._button_markup("RUN TEST"))
        self._st_button.add(self._st_button_lbl)
        self._st_button.get_style_context().add_class("st-btn")
        self._st_button.connect("clicked", self._on_speedtest_click)
        btn_row.pack_start(self._st_button, False, False, 0)
        body.pack_start(btn_row, False, False, 0)
        page.pack_start(_HudCard(body, accent=theme.MAGENTA_BRIGHT), False, False, 0)
        return page

    def _speed_row(self, tag: str, arrow: str, arrow_color: str,
                   meter: _Meter, val: Gtk.Label) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head = Gtk.Label()
        head.set_markup(
            f"<span color='{theme.CYAN_DIM}' weight='bold' "
            f"letter_spacing='1024'>{tag}</span> "
            f"<span color='{arrow_color}' weight='bold'>{arrow}</span>"
        )
        head.set_xalign(0.0)
        head.set_width_chars(5)
        row.pack_start(head, False, False, 0)
        row.pack_start(meter, True, True, 0)
        row.pack_start(val, False, False, 0)
        return row

    def _button_markup(self, label: str) -> str:
        return (
            f"<span color='{theme.YELLOW_MID}' weight='bold'>//</span> "
            f"<span color='{theme.HIGHLIGHT}' weight='bold' "
            f"letter_spacing='1536'>{label}</span>"
        )

    # ── speedtest engine ──────────────────────────────────────────────
    def _on_speedtest_click(self, _btn) -> None:
        if self._st_proc is not None:
            self._st_cancel()
        else:
            self._st_start()

    def _st_start(self) -> None:
        self._st_phase = "starting"
        self._st_buf = ""
        self._st_server_set = False
        if self._st_server_lbl:
            self._st_server_lbl.set_text("connecting…")
            self._reapply_class(self._st_server_lbl, "server-idle")
        if self._st_ping_lbl:
            self._st_ping_lbl.set_text("—")
        if self._st_dn_val:
            self._st_dn_val.set_text("—")
            self._reapply_class(self._st_dn_val, "placeholder")
        if self._st_up_val:
            self._st_up_val.set_text("—")
            self._reapply_class(self._st_up_val, "placeholder")
        if self._st_dn_meter: self._st_dn_meter.set_progress(0)
        if self._st_up_meter: self._st_up_meter.set_progress(0)
        if self._st_button_lbl: self._st_button_lbl.set_markup(self._button_markup("CANCEL"))

        try:
            self._st_proc = subprocess.Popen(
                ["speedtest-cli", "--secure"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                preexec_fn=os.setsid,  # so we can kill the group on cancel
            )
        except FileNotFoundError:
            if self._st_server_lbl:
                self._st_server_lbl.set_text("speedtest-cli not installed")
            if self._st_button_lbl:
                self._st_button_lbl.set_markup(self._button_markup("RUN TEST"))
            self._st_proc = None
            return

        assert self._st_proc.stdout is not None
        fd = self._st_proc.stdout.fileno()
        os.set_blocking(fd, False)
        self._st_watch_id = GLib.io_add_watch(
            fd, GLib.IO_IN | GLib.IO_HUP, self._st_on_io
        )

    def _st_cancel(self) -> None:
        proc = self._st_proc
        if proc is not None:
            try:
                os.killpg(proc.pid, 15)
            except Exception:
                pass
        self._st_finalize(canceled=True)

    def _st_on_io(self, fd: int, _cond) -> bool:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            self._st_finalize(canceled=False)
            return False
        self._st_buf += chunk.decode("utf-8", errors="replace")
        self._st_parse()
        return True

    def _st_parse(self) -> None:
        s = self._st_buf

        if not self._st_server_set:
            m = re.search(r"Hosted by (.+?) \[.+?\]:\s*([\d.]+)\s*ms", s)
            if m and self._st_server_lbl and self._st_ping_lbl:
                self._st_server_set = True
                self._st_server_lbl.set_text(m.group(1))
                self._reapply_class(self._st_server_lbl, "server-name")
                self._st_ping_lbl.set_markup(
                    f"<span color='{theme.YELLOW_BRIGHT}' weight='bold' "
                    f"font_family='monospace'>{m.group(2)}</span>"
                    f"<span color='{theme.BASE_MUTED}'> ms</span>"
                )

        # Phase transitions detected by substring — dot lines don't
        # end with \n until the phase is over.
        if self._st_phase == "starting" and "Testing download speed" in s:
            self._st_phase = "download"
            self._st_anim_start_for("download")

        if self._st_phase == "download":
            m = re.search(r"Download:\s+([\d.]+)\s+(\S+)", s)
            if m and self._st_dn_val and self._st_dn_meter:
                self._st_dn_val.set_markup(
                    f"<span color='{theme.MAGENTA_BRIGHT}' weight='bold' "
                    f"font_family='monospace'>{m.group(1)}</span>"
                    f"<span color='{theme.BASE_MUTED}'> {_pango_escape(m.group(2))}</span>"
                )
                self._st_dn_meter.set_progress(100)
                self._st_anim_stop()
                self._st_phase = "between"

        if self._st_phase == "between" and "Testing upload speed" in s:
            self._st_phase = "upload"
            self._st_anim_start_for("upload")

        if self._st_phase == "upload":
            m = re.search(r"Upload:\s+([\d.]+)\s+(\S+)", s)
            if m and self._st_up_val and self._st_up_meter:
                self._st_up_val.set_markup(
                    f"<span color='{theme.YELLOW_BRIGHT}' weight='bold' "
                    f"font_family='monospace'>{m.group(1)}</span>"
                    f"<span color='{theme.BASE_MUTED}'> {_pango_escape(m.group(2))}</span>"
                )
                self._st_up_meter.set_progress(100)
                self._st_anim_stop()
                self._st_phase = "done"

    def _st_anim_start_for(self, target: str) -> None:
        self._st_anim_target = target
        self._st_anim_start = time.monotonic()
        if self._st_anim_id is None:
            self._st_anim_id = GLib.timeout_add(80, self._st_anim_tick)

    def _st_anim_tick(self) -> bool:
        elapsed = time.monotonic() - self._st_anim_start
        pct = min(95.0, (elapsed / self._PHASE_DURATION_SEC) * 100)
        if self._st_anim_target == "download" and self._st_dn_meter:
            self._st_dn_meter.set_progress(pct)
        elif self._st_anim_target == "upload" and self._st_up_meter:
            self._st_up_meter.set_progress(pct)
        return True

    def _st_anim_stop(self) -> None:
        if self._st_anim_id is not None:
            GLib.source_remove(self._st_anim_id)
            self._st_anim_id = None
        self._st_anim_target = None

    def _st_finalize(self, canceled: bool) -> None:
        if self._st_watch_id is not None:
            GLib.source_remove(self._st_watch_id)
            self._st_watch_id = None
        if self._st_proc is not None:
            try:
                self._st_proc.wait(timeout=1)
            except Exception:
                pass
            self._st_proc = None
        self._st_anim_stop()
        if canceled and self._st_phase != "done":
            self._st_phase = "idle"
            if self._st_server_lbl:
                self._st_server_lbl.set_text("canceled")
                self._reapply_class(self._st_server_lbl, "server-idle")
        elif self._st_phase != "done":
            self._st_phase = "error"
            if self._st_server_lbl:
                self._st_server_lbl.set_text("speedtest failed")
                self._reapply_class(self._st_server_lbl, "server-idle")
        if self._st_button_lbl:
            self._st_button_lbl.set_markup(self._button_markup("RUN TEST"))

    def _reapply_class(self, lbl: Gtk.Label, css_class: str) -> None:
        """Swap state-driven CSS class on a label without piling up
        stale ones (idle/name, placeholder/result)."""
        ctx = lbl.get_style_context()
        for c in ("server-idle", "server-name", "placeholder",
                  "result-dn", "result-up"):
            ctx.remove_class(c)
        ctx.add_class(css_class)

    def stop(self) -> None:
        # Don't let a background subprocess outlive the widget.
        if self._st_proc is not None:
            try:
                os.killpg(self._st_proc.pid, 9)
            except Exception:
                pass
        self._st_anim_stop()
        super().stop()
