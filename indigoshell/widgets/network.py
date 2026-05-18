"""Network widget — active connection (ethernet preferred, then wifi).

Subscribes to `nmcli monitor` and only re-reads state when NetworkManager
reports a change. Renders as stacked IP (small, muted) + SSID/conn-name.
Click handler opens the network popup (configured in user config).
"""

import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from ..services import proc
from ..style import Style
from .base import Widget, make_label


class Network(Widget):
    def __init__(
        self,
        ip_color: str | None = None,
        ip_size_pt: int = 8,
        style: Style | None = None,
        **kwargs,
    ):
        super().__init__(style, **kwargs)
        self.ip_color = ip_color or theme.BASE_MUTED
        self.ip_size_pt = ip_size_pt
        self.value: Gtk.Label | None = None
        self._ip_label: Gtk.Label | None = None
        self._mon_proc: subprocess.Popen | None = None

    def build_widget(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        col.set_valign(Gtk.Align.CENTER)

        self._ip_label = make_label("", "ip")
        self._ip_label.set_xalign(0.0)
        col.pack_start(self._ip_label, False, False, 0)

        self.value = make_label("", "value")
        self.value.set_xalign(0.0)
        col.pack_start(self.value, False, False, 0)

        return col

    def start(self):
        threading.Thread(target=self._refresh, daemon=True).start()
        self._mon_proc = proc.subscribe(
            ["nmcli", "monitor"],
            lambda _line: self._refresh(),
            on_missing=lambda: GLib.idle_add(self._apply_state, "[no nmcli]", ""),
        )

    def stop(self):
        if self._mon_proc and self._mon_proc.poll() is None:
            self._mon_proc.terminate()
        self._mon_proc = None

    def _refresh(self):
        label = "--"
        ip = ""
        device = ""
        # Find the first active managed connection (ethernet preferred
        # over wifi since wired is usually the path you care about).
        status = proc.run(["nmcli", "-t", "-f", "device,type,state,connection", "device", "status"])

        eth = wifi = None
        for line in status.splitlines():
            parts = line.split(":")
            if len(parts) < 4 or parts[2] != "connected":
                continue
            dev_name, dev_type, _state, conn = parts[0], parts[1], parts[2], parts[3]
            if dev_type == "ethernet" and eth is None:
                eth = (dev_name, conn)
            elif dev_type == "wifi" and wifi is None:
                wifi = (dev_name, conn)

        if eth is not None:
            device, conn_name = eth
            label = conn_name or "ETH"
        elif wifi is not None:
            device, conn_name = wifi
            for line in proc.run(["nmcli", "-t", "-f", "active,ssid,signal", "dev", "wifi"]).splitlines():
                if line.startswith("yes:"):
                    parts = line.split(":")
                    label = parts[1] or conn_name or "--"
                    break

        if device:
            ipr = proc.run(["nmcli", "-t", "-g", "IP4.ADDRESS", "device", "show", device])
            first = next((l for l in ipr.splitlines() if l.strip()), "")
            ip = first.split("/")[0]

        GLib.idle_add(self._apply_state, label, ip)

    def _apply_state(self, ssid: str, ip: str) -> bool:
        if self.value:
            self.value.set_text(ssid)
        if self._ip_label is not None:
            markup = (
                f"<span size='{self.ip_size_pt * 1000}' "
                f"foreground='{self.ip_color}'>{ip}</span>"
            )
            self._ip_label.set_markup(markup)
        return False
