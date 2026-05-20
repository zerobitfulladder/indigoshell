"""Hardware HUD popup — single flat view stacking DISK / CPU / RAM / GPU.

Layout (one HudCard per section):

    ┌─ DISK / ──────────────────────────────┐
    │ ▮▮▮▮▮▮▮▮▮▯▯▯▯▯  47%  120G / 256G       │
    └────────────────────────────────────────┘
    ┌─ CPU ──────────────────────────────────┐
    │ 34%                                    │
    │ [history line graph]                   │
    └────────────────────────────────────────┘
    ┌─ RAM ──────────────────────────────────┐
    │ 62%                  10.4G / 16.0G     │
    │ [history line graph]                   │
    └────────────────────────────────────────┘
    ┌─ GPU GeForce RTX 4070 ─────────────────┐
    │ UTIL  25%   ▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯           │
    │  MEM  46%   ▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯  7G/16G   │
    │ TEMP  67°C  ▮▮▮▮▮▮▮▮▮▯▯▯▯▯▯▯           │
    └────────────────────────────────────────┘

CPU/RAM keep 60-sample history via `LineGraph`. GPU is polled fresh
each tick — no history. To keep `nvidia-smi` off the hot path when the
popup is hidden, GPU sampling is gated on the popup window being
mapped.
"""

import os
import re
import shutil
import subprocess
import threading

import psutil

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from ..services import sysinfo
from .bar_meter import BarMeter
from .base import Widget
from .hud import HudCard, plain_label
from .line_graph import LineGraph


_HISTORY = 60  # one sample per tick (~1s) → 1 minute visible

# Shared role colors — used wherever a "current value" or a "history
# line" is rendered so the panel reads as one styled HUD.
_VALUE_FG   = theme.YELLOW_BRIGHT
_GRAPH_FG   = theme.MAGENTA_BRIGHT


# ── helpers ────────────────────────────────────────────────────────────
def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _heat_color(pct: float) -> str:
    """Cool → warm → hot ramp shared by disk, temperature, etc."""
    if pct >= 90:
        return theme.MAGENTA_BRIGHT
    if pct >= 75:
        return theme.YELLOW_BRIGHT
    return theme.CYAN_BRIGHT


# ── GPU sampling ──────────────────────────────────────────────────────
def _sample_gpu_nvidia() -> dict | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip().splitlines()
    except Exception:
        return None
    if not out:
        return None
    parts = [p.strip() for p in out[0].split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "util": float(parts[1]),
            "mem_used_mib": float(parts[2]),
            "mem_total_mib": float(parts[3]),
            "temp": float(parts[4]),
        }
    except ValueError:
        return None


def _sample_gpu_amdgpu() -> dict | None:
    """Best-effort amdgpu sysfs read. Skips silently if no compatible
    card surfaces a `gpu_busy_percent` file."""
    try:
        entries = sorted(p for p in os.listdir("/sys/class/drm")
                         if re.fullmatch(r"card\d+", p))
    except FileNotFoundError:
        return None
    for card_dir in entries:
        base = f"/sys/class/drm/{card_dir}/device"
        busy_path = f"{base}/gpu_busy_percent"
        if not os.path.isfile(busy_path):
            continue
        try:
            with open(busy_path) as f:
                util = float(f.read().strip())
            with open(f"{base}/mem_info_vram_used") as f:
                mem_used = float(f.read().strip())
            with open(f"{base}/mem_info_vram_total") as f:
                mem_total = float(f.read().strip())
        except Exception:
            continue
        temp = 0.0
        hwmon_root = f"{base}/hwmon"
        if os.path.isdir(hwmon_root):
            try:
                for hm in sorted(os.listdir(hwmon_root)):
                    tp = f"{hwmon_root}/{hm}/temp1_input"
                    if os.path.isfile(tp):
                        with open(tp) as f:
                            temp = float(f.read().strip()) / 1000.0
                        break
            except Exception:
                pass
        return {
            "name": "AMD GPU",
            "util": util,
            "mem_used_mib": mem_used / (1024 * 1024),
            "mem_total_mib": mem_total / (1024 * 1024),
            "temp": temp,
        }
    return None


def _sample_gpu() -> dict | None:
    return _sample_gpu_nvidia() or _sample_gpu_amdgpu()


# ── main panel ────────────────────────────────────────────────────────
class HardwarePanel(Widget):
    """Flat stack: disk, CPU graph, RAM graph, GPU bars. Persistent
    popup so CPU/RAM history accumulates while hidden; GPU polling is
    gated on visibility to avoid running `nvidia-smi` for nothing."""

    # CPU/RAM history is sampled by `services.sysinfo` at 1 Hz; the
    # panel tick aligns to the same cadence so live labels, graph pushes,
    # disk, and GPU all advance once per second when visible.
    interval_ms = 1000

    # Temperature → 0..100% bar fill mapping. 30°C maps to 0% (cool),
    # 90°C maps to 100% (hot). Used for GPU temp visualization.
    _TEMP_MIN_C = 30.0
    _TEMP_MAX_C = 90.0

    def __init__(self, *, disk_mount: str = "/", **kwargs):
        super().__init__(**kwargs)
        self._disk_mount = disk_mount

        # Disk widgets
        self._disk_bar: BarMeter | None = None
        self._disk_lbl: Gtk.Label | None = None

        # CPU widgets (single aggregate graph)
        self._cpu_graph: LineGraph | None = None
        self._cpu_val_lbl: Gtk.Label | None = None

        # RAM widgets
        self._ram_graph: LineGraph | None = None
        self._ram_val_lbl: Gtk.Label | None = None
        self._ram_total_lbl: Gtk.Label | None = None

        # GPU widgets — may stay None if no GPU is detected.
        self._gpu_card: Gtk.Widget | None = None
        self._gpu_name_lbl: Gtk.Label | None = None
        self._gpu_util_bar: BarMeter | None = None
        self._gpu_mem_bar: BarMeter | None = None
        self._gpu_temp_bar: BarMeter | None = None
        self._gpu_util_lbl: Gtk.Label | None = None
        self._gpu_mem_lbl: Gtk.Label | None = None
        self._gpu_temp_lbl: Gtk.Label | None = None
        # Cheap presence check — just look for the binary / sysfs node
        # rather than actually invoking nvidia-smi at startup.
        self._gpu_available: bool = (
            shutil.which("nvidia-smi") is not None
            or any(
                os.path.isfile(f"/sys/class/drm/{p}/device/gpu_busy_percent")
                for p in (os.listdir("/sys/class/drm") if os.path.isdir("/sys/class/drm") else [])
            )
        )

        # State row (envycontrol mode + power profile) — fetched once
        # per popup open, not on every tick.
        self._state_card: Gtk.Widget | None = None
        self._gpu_mode_lbl: Gtk.Label | None = None
        self._profile_lbl: Gtk.Label | None = None
        self._was_visible: bool = False

        # Background-fetch guards — never spawn a second worker while
        # the previous one is still running.
        self._gpu_pending: bool = False
        self._state_pending: bool = False

    # ── construction ──────────────────────────────────────────────────
    def build_widget(self) -> Gtk.Widget:
        # Reset transient references so a rebuild starts clean.
        self._disk_bar = self._disk_lbl = None
        self._cpu_graph = self._cpu_val_lbl = None
        self._ram_graph = self._ram_val_lbl = self._ram_total_lbl = None
        self._gpu_card = self._gpu_name_lbl = None
        self._gpu_util_bar = self._gpu_mem_bar = self._gpu_temp_bar = None
        self._gpu_util_lbl = self._gpu_mem_lbl = self._gpu_temp_lbl = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        outer.pack_start(self._build_disk_card(), False, False, 0)
        outer.pack_start(self._build_cpu_card(),  False, False, 0)
        outer.pack_start(self._build_ram_card(),  False, False, 0)
        if self._gpu_available:
            self._gpu_card = self._build_gpu_card()
            outer.pack_start(self._gpu_card, False, False, 0)
        self._state_card = self._build_state_card()
        outer.pack_start(self._state_card, False, False, 0)

        # Prime graphs from the shared sampler so the rolling window
        # shows the last minute the moment the popup opens, instead of
        # starting blank and filling in over time.
        if self._cpu_graph is not None:
            for v in sysinfo.cpu_history():
                self._cpu_graph.push(v)
        if self._ram_graph is not None:
            for v in sysinfo.memory_history():
                self._ram_graph.push(v)
        return outer

    def start(self) -> None:
        super().start()
        if self.gtk_widget is None:
            return
        top = self.gtk_widget.get_toplevel()
        if isinstance(top, Gtk.Window):
            # Force a full tick the moment the popup window is mapped
            # so the labels/disk/GPU/state card all populate immediately
            # instead of waiting up to `interval_ms` for the next tick.
            top.connect("map", lambda _w: self.tick())

    def default_css(self) -> str:
        sel = f"#{self.name}"
        body  = theme.FONT_SIZE - 3
        small = theme.FONT_SIZE - 5
        return (
            f"{sel} {{ background: transparent; font-size: {body}px; "
            f"font-family: {theme.FONT}; min-width: 560px; }}"
            f"{sel} label {{ color: {theme.FG}; text-shadow: none; }}"
            f"{sel} .panel-title {{ color: {theme.YELLOW_BRIGHT}; "
            f"  font-size: {small}px; font-weight: bold; letter-spacing: 2px; }}"
            f"{sel} .panel-subtitle {{ color: {theme.BASE_MUTED}; "
            f"  font-size: {small}px; }}"
            f"{sel} .label-key {{ color: {theme.CYAN_DIM}; "
            f"  font-size: {small}px; letter-spacing: 1px; }}"
            f"{sel} .value-mono {{ color: {theme.FG}; font-family: monospace; }}"
        )

    # ── disk card ─────────────────────────────────────────────────────
    def _build_disk_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.pack_start(plain_label("DISK", "label-key"), False, False, 0)
        head.pack_start(plain_label(self._disk_mount, "value-mono"), False, False, 0)
        self._disk_lbl = Gtk.Label()
        self._disk_lbl.set_xalign(1.0)
        self._disk_lbl.set_hexpand(True)
        self._disk_lbl.set_valign(Gtk.Align.CENTER)
        head.pack_start(self._disk_lbl, True, True, 0)
        body.pack_start(head, False, False, 0)

        self._disk_bar = BarMeter(color=_heat_color)
        body.pack_start(self._disk_bar, False, False, 0)
        return HudCard(body, accent=theme.CYAN_BRIGHT)

    # ── CPU card (aggregate utilization + history graph) ──────────────
    def _build_cpu_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.pack_start(plain_label("CPU", "label-key"), False, False, 0)
        self._cpu_val_lbl = Gtk.Label()
        self._cpu_val_lbl.set_xalign(1.0)
        self._cpu_val_lbl.set_hexpand(True)
        self._cpu_val_lbl.set_valign(Gtk.Align.CENTER)
        self._cpu_val_lbl.set_markup(
            f"<span color='{_VALUE_FG}' font_family='monospace' "
            f"weight='bold'>0%</span>"
        )
        head.pack_start(self._cpu_val_lbl, True, True, 0)
        body.pack_start(head, False, False, 0)

        self._cpu_graph = LineGraph(
            color=_GRAPH_FG,
            max_samples=_HISTORY,
            height=72,
            min_width=300,
            fill_alpha=0.28,
        )
        body.pack_start(self._cpu_graph, True, True, 0)
        return HudCard(body, accent=_GRAPH_FG)

    # ── RAM card ──────────────────────────────────────────────────────
    def _build_ram_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.pack_start(plain_label("RAM", "label-key"), False, False, 0)
        self._ram_val_lbl = Gtk.Label()
        self._ram_val_lbl.set_xalign(0.0)
        self._ram_val_lbl.set_valign(Gtk.Align.CENTER)
        self._ram_val_lbl.set_markup(
            f"<span color='{_VALUE_FG}' font_family='monospace' "
            f"weight='bold'>0%</span>"
        )
        head.pack_start(self._ram_val_lbl, False, False, 0)
        self._ram_total_lbl = Gtk.Label()
        self._ram_total_lbl.set_xalign(1.0)
        self._ram_total_lbl.set_hexpand(True)
        self._ram_total_lbl.set_valign(Gtk.Align.CENTER)
        head.pack_start(self._ram_total_lbl, True, True, 0)
        body.pack_start(head, False, False, 0)

        self._ram_graph = LineGraph(
            color=_GRAPH_FG,
            max_samples=_HISTORY,
            height=72,
            min_width=300,
            fill_alpha=0.28,
        )
        body.pack_start(self._ram_graph, True, True, 0)
        return HudCard(body, accent=_GRAPH_FG)

    # ── GPU card (no history — three bars) ────────────────────────────
    def _build_gpu_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.pack_start(plain_label("GPU", "label-key"), False, False, 0)
        self._gpu_name_lbl = Gtk.Label()
        self._gpu_name_lbl.set_xalign(1.0)
        self._gpu_name_lbl.set_hexpand(True)
        self._gpu_name_lbl.set_valign(Gtk.Align.CENTER)
        self._gpu_name_lbl.set_markup(
            f"<span color='{theme.BASE_MUTED}'>…</span>"
        )
        head.pack_start(self._gpu_name_lbl, True, True, 0)
        body.pack_start(head, False, False, 0)

        # Each row: 6-char key | bar | mono value.
        self._gpu_util_bar = BarMeter(color=theme.CYAN_BRIGHT, min_width=200)
        self._gpu_util_lbl = self._make_mono_value("0%", theme.CYAN_BRIGHT)
        body.pack_start(self._gpu_row("UTIL", self._gpu_util_bar, self._gpu_util_lbl),
                        False, False, 0)

        self._gpu_mem_bar = BarMeter(color=theme.VIOLET_BRIGHT, min_width=200)
        self._gpu_mem_lbl = self._make_mono_value("—", theme.VIOLET_BRIGHT)
        body.pack_start(self._gpu_row("MEM", self._gpu_mem_bar, self._gpu_mem_lbl),
                        False, False, 0)

        self._gpu_temp_bar = BarMeter(color=_heat_color, min_width=200)
        self._gpu_temp_lbl = self._make_mono_value("—", theme.YELLOW_BRIGHT)
        body.pack_start(self._gpu_row("TEMP", self._gpu_temp_bar, self._gpu_temp_lbl),
                        False, False, 0)

        return HudCard(body, accent=theme.VIOLET_BRIGHT)

    def _gpu_row(self, key: str, bar: BarMeter, val: Gtk.Label) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head = Gtk.Label()
        head.set_markup(
            f"<span color='{theme.CYAN_DIM}' weight='bold' "
            f"letter_spacing='1024'>{key}</span>"
        )
        head.set_xalign(0.0)
        head.set_width_chars(5)
        head.set_valign(Gtk.Align.CENTER)
        row.pack_start(head, False, False, 0)
        row.pack_start(bar,  True, True, 0)
        row.pack_start(val,  False, False, 0)
        return row

    # ── state card (one-shot per popup open) ──────────────────────────
    def _build_state_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        def make_row(key: str) -> tuple[Gtk.Widget, Gtk.Label]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.pack_start(plain_label(key, "label-key"), False, False, 0)
            val = Gtk.Label()
            val.set_xalign(1.0)
            val.set_hexpand(True)
            val.set_valign(Gtk.Align.CENTER)
            val.set_markup(
                f"<span color='{theme.BASE_MUTED}' font_family='monospace'>…</span>"
            )
            row.pack_start(val, True, True, 0)
            return row, val

        mode_row, self._gpu_mode_lbl = make_row("GPU MODE")
        prof_row, self._profile_lbl  = make_row("PROFILE")
        body.pack_start(mode_row, False, False, 0)
        body.pack_start(prof_row, False, False, 0)
        return HudCard(body, accent=theme.YELLOW_BRIGHT)

    def _refresh_state_oneshot(self) -> None:
        """Kick off envycontrol + tuned-adm queries on a worker thread.
        Their results are marshalled back via GLib.idle_add — running
        them on the GTK main thread would freeze the whole bar for
        100–500ms on each popup open."""
        if self._state_pending:
            return
        self._state_pending = True

        def worker() -> None:
            mode = self._run_oneshot(["envycontrol", "-q"])
            prof = self._read_profile()
            GLib.idle_add(self._apply_state, mode, prof)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_state(self, mode: str | None, prof: str | None) -> bool:
        self._state_pending = False
        if self._gpu_mode_lbl is not None:
            self._gpu_mode_lbl.set_markup(self._state_markup(mode))
        if self._profile_lbl is not None:
            self._profile_lbl.set_markup(self._state_markup(prof))
        return False  # one-shot idle callback

    @staticmethod
    def _read_profile() -> str | None:
        """Reuse the friendly-name mapping the rest of the shell already
        uses for tuned profiles — keeps `performance` / `balanced` /
        `powersave` labels consistent across the menu and this card."""
        if shutil.which("tuned-adm") is None:
            return None
        try:
            from ..helpers.profile import _active_friendly  # deferred
            name = _active_friendly()
        except Exception:
            return None
        return name if name and name != "?" else None

    @staticmethod
    def _run_oneshot(argv: list[str]) -> str | None:
        if shutil.which(argv[0]) is None:
            return None
        try:
            out = subprocess.check_output(
                argv, stderr=subprocess.DEVNULL, text=True, timeout=2,
            ).strip()
        except Exception:
            return None
        # envycontrol prints multiple lines on some installs; take the last.
        if "\n" in out:
            out = out.splitlines()[-1].strip()
        return out or None

    @staticmethod
    def _state_markup(value: str | None) -> str:
        if not value:
            return (
                f"<span color='{theme.BASE_MUTED}' font_family='monospace'>—</span>"
            )
        return (
            f"<span color='{_VALUE_FG}' font_family='monospace' "
            f"weight='bold'>{value}</span>"
        )

    @staticmethod
    def _make_mono_value(text: str, color: str) -> Gtk.Label:
        lbl = Gtk.Label()
        lbl.set_xalign(1.0)
        lbl.set_valign(Gtk.Align.CENTER)
        lbl.set_width_chars(14)
        lbl.set_single_line_mode(True)
        lbl.set_markup(
            f"<span color='{color}' font_family='monospace' "
            f"weight='bold'>{text}</span>"
        )
        return lbl

    # ── tick ──────────────────────────────────────────────────────────
    def tick(self) -> bool:
        visible = self._popup_visible()

        # Rising edge — refresh one-shot state (envycontrol, profile).
        if visible and not self._was_visible:
            self._refresh_state_oneshot()
        self._was_visible = visible

        # Latest cached samples from the shared sampler — no psutil
        # contention with StatMeter.
        cpu = sysinfo.cpu_percent()
        ram_pct = sysinfo.memory_percent()

        if visible:
            if self._cpu_val_lbl is not None:
                self._cpu_val_lbl.set_markup(
                    f"<span color='{_VALUE_FG}' font_family='monospace' "
                    f"weight='bold'>{cpu:.0f}%</span>"
                )
            if self._ram_val_lbl is not None:
                self._ram_val_lbl.set_markup(
                    f"<span color='{_VALUE_FG}' font_family='monospace' "
                    f"weight='bold'>{ram_pct:.0f}%</span>"
                )
            if self._ram_total_lbl is not None:
                # Bytes only — percent comes from sysinfo above.
                vm = psutil.virtual_memory()
                self._ram_total_lbl.set_markup(
                    f"<span color='{theme.BASE_MUTED}'>"
                    f"{_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)}</span>"
                )

        if self._cpu_graph is not None:
            self._cpu_graph.push(cpu)
        if self._ram_graph is not None:
            self._ram_graph.push(ram_pct)

        if visible and self._disk_bar is not None and self._disk_lbl is not None:
            try:
                u = psutil.disk_usage(self._disk_mount)
                self._disk_bar.set_value(u.percent)
                self._disk_lbl.set_markup(
                    f"<span color='{_VALUE_FG}' font_family='monospace' "
                    f"weight='bold'>{u.percent:.0f}%</span>  "
                    f"<span color='{theme.BASE_MUTED}'>"
                    f"{_fmt_bytes(u.used)} / {_fmt_bytes(u.total)}</span>"
                )
            except Exception:
                pass

        if visible and self._gpu_available:
            self._update_gpu()
        return True

    def _popup_visible(self) -> bool:
        if self.gtk_widget is None:
            return False
        top = self.gtk_widget.get_toplevel()
        if not isinstance(top, Gtk.Window):
            return False
        return top.get_mapped() and top.is_visible()

    def _update_gpu(self) -> None:
        """Kick off `nvidia-smi` (or amdgpu sysfs read) on a worker
        thread. Both take long enough — particularly nvidia-smi at
        ~150ms — that running them on the GTK main loop visibly hitches
        the bar."""
        if self._gpu_pending:
            return
        self._gpu_pending = True

        def worker() -> None:
            sample = _sample_gpu()
            GLib.idle_add(self._apply_gpu, sample)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_gpu(self, g: dict | None) -> bool:
        self._gpu_pending = False
        if g is None:
            return False
        if self._gpu_name_lbl is not None:
            self._gpu_name_lbl.set_markup(
                f"<span color='{theme.BASE_MUTED}'>{g['name']}</span>"
            )
        if self._gpu_util_bar is not None and self._gpu_util_lbl is not None:
            self._gpu_util_bar.set_value(g["util"])
            self._gpu_util_lbl.set_markup(
                f"<span color='{theme.CYAN_BRIGHT}' font_family='monospace' "
                f"weight='bold'>{g['util']:.0f}%</span>"
            )
        if self._gpu_mem_bar is not None and self._gpu_mem_lbl is not None:
            total = max(1.0, g["mem_total_mib"])
            pct = (g["mem_used_mib"] / total) * 100.0
            self._gpu_mem_bar.set_value(pct)
            self._gpu_mem_lbl.set_markup(
                f"<span color='{theme.VIOLET_BRIGHT}' font_family='monospace' "
                f"weight='bold'>{g['mem_used_mib']/1024:.1f}G/"
                f"{g['mem_total_mib']/1024:.1f}G</span>"
            )
        if self._gpu_temp_bar is not None and self._gpu_temp_lbl is not None:
            span = self._TEMP_MAX_C - self._TEMP_MIN_C
            pct = max(0.0, min(100.0, (g["temp"] - self._TEMP_MIN_C) / span * 100.0))
            self._gpu_temp_bar.set_value(pct)
            self._gpu_temp_lbl.set_markup(
                f"<span color='{_heat_color(pct)}' font_family='monospace' "
                f"weight='bold'>{g['temp']:.0f}°C</span>"
            )
        return False  # one-shot idle callback
