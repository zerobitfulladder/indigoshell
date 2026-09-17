"""Hardware panel — disk, CPU, RAM, GPU and system state.

Cards in the HUD language: a segmented bar for disk, rolling one-minute
line graphs for CPU and RAM primed from the shared sampler, and per-metric
bars for the GPU when one is detectable.

The one-shot state queries (optimus-manager, tuned-adm) block for
100-500ms, and GPU sampling shells out on every sample, so all of them
are coroutines: the panel awaits them and the shell keeps painting.
"""

import asyncio
import logging
import os
import re
import shutil

import psutil
import skia

from .. import theme
from ..services import proc, sysinfo
from .base import Insets, Size, Widget
from .hud import HudCard, key_label, meta_label, value_label
from .label import Label
from .layout import Align, Column, Row, Spacer
from .line_graph import LineGraph
from .meters import BarMeter

log = logging.getLogger(__name__)

HISTORY = 60          # one sample per second -> one minute visible
VALUE_FG = theme.YELLOW_BRIGHT
GRAPH_FG = theme.MAGENTA_BRIGHT


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def heat_color(pct: float) -> str:
    """Cool -> warm -> hot ramp, shared by disk and temperature."""
    if pct >= 90:
        return theme.MAGENTA_BRIGHT
    if pct >= 75:
        return theme.YELLOW_BRIGHT
    return theme.CYAN_BRIGHT


# ── GPU sampling ────────────────────────────────────────────────────────
async def _sample_nvidia() -> dict | None:
    if shutil.which("nvidia-smi") is None:
        return None
    out = await proc.run([
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu",
        "--format=csv,noheader,nounits",
    ], timeout=2.0)
    lines = out.strip().splitlines()
    if not lines:
        return None
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) < 5:
        return None
    try:
        return {"name": parts[0], "util": float(parts[1]),
                "mem_used_mib": float(parts[2]),
                "mem_total_mib": float(parts[3]), "temp": float(parts[4])}
    except ValueError:
        return None


def _sample_amdgpu() -> dict | None:
    """Best-effort amdgpu sysfs read; silent when no card exposes
    gpu_busy_percent."""
    try:
        cards = sorted(p for p in os.listdir("/sys/class/drm")
                       if re.fullmatch(r"card\d+", p))
    except FileNotFoundError:
        return None
    for card in cards:
        base = f"/sys/class/drm/{card}/device"
        busy = f"{base}/gpu_busy_percent"
        if not os.path.isfile(busy):
            continue

        def read(path, cast=float):
            try:
                with open(path) as f:
                    return cast(f.read().strip())
            except (OSError, ValueError):
                return None

        util = read(busy)
        if util is None:
            continue
        used = read(f"{base}/mem_info_vram_used")
        total = read(f"{base}/mem_info_vram_total")
        temp = None
        hwmon = f"{base}/hwmon"
        if os.path.isdir(hwmon):
            for entry in sorted(os.listdir(hwmon)):
                value = read(f"{hwmon}/{entry}/temp1_input")
                if value is not None:
                    temp = value / 1000.0
                    break
        return {
            "name": "AMD GPU", "util": util,
            "mem_used_mib": (used or 0) / (1024 * 1024),
            "mem_total_mib": (total or 0) / (1024 * 1024),
            "temp": temp or 0.0,
        }
    return None


async def sample_gpu() -> dict | None:
    return await _sample_nvidia() or _sample_amdgpu()


class HardwarePanel(Widget):
    # No frame clock. The panel has nothing to animate between samples,
    # and a 1Hz clock of its own drifted against the sampler's 1Hz —
    # which made the graphs scroll on frames where no new data had
    # arrived. It redraws when `sysinfo` says there is something new.
    animation_fps = 0

    def __init__(self, *, disk_mount: str = "/", width: int = 560,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.disk_mount = disk_mount
        self.width = width
        self._gpu: dict | None = None
        self._state_pending = False

        # ── disk ────────────────────────────────────────────────────────
        self._disk_value = value_label()
        self._disk_bar = BarMeter(color=heat_color, min_width=width - 40)
        disk = HudCard(Column([
            Row([key_label("DISK"), meta_label(disk_mount, color=theme.FG),
                 Spacer(), self._disk_value], spacing=10, align=Align.CENTER),
            self._disk_bar,
        ], spacing=6, align=Align.STRETCH), accent=theme.CYAN_BRIGHT)

        # ── cpu ─────────────────────────────────────────────────────────
        self._cpu_value = value_label()
        self._cpu_graph = LineGraph(color=GRAPH_FG, max_samples=HISTORY,
                                    height=54, min_width=width - 40,
                                    show_dot=True)
        cpu = HudCard(Column([
            Row([key_label("CPU"), Spacer(), self._cpu_value],
                spacing=10, align=Align.CENTER),
            self._cpu_graph,
        ], spacing=6, align=Align.STRETCH), accent=theme.MAGENTA_BRIGHT)

        # ── ram ─────────────────────────────────────────────────────────
        self._ram_value = value_label()
        self._ram_total = meta_label(color=theme.FG)
        self._ram_graph = LineGraph(color=theme.VIOLET_BRIGHT,
                                    max_samples=HISTORY, height=54,
                                    min_width=width - 40, show_dot=True)
        ram = HudCard(Column([
            Row([key_label("RAM"), self._ram_total, Spacer(),
                 self._ram_value], spacing=10, align=Align.CENTER),
            self._ram_graph,
        ], spacing=6, align=Align.STRETCH), accent=theme.VIOLET_BRIGHT)

        # ── gpu ─────────────────────────────────────────────────────────
        self._gpu_name = meta_label()
        self._gpu_util_bar = BarMeter(color=theme.CYAN_BRIGHT, min_width=200)
        self._gpu_mem_bar = BarMeter(color=theme.VIOLET_BRIGHT, min_width=200)
        self._gpu_temp_bar = BarMeter(color=heat_color, min_width=200)
        self._gpu_util = value_label()
        self._gpu_mem = value_label()
        self._gpu_temp = value_label()
        self._gpu_card = HudCard(Column([
            Row([key_label("GPU"), self._gpu_name, Spacer()],
                spacing=10, align=Align.CENTER),
            self._metric_row("UTIL", self._gpu_util_bar, self._gpu_util),
            self._metric_row("MEM", self._gpu_mem_bar, self._gpu_mem),
            self._metric_row("TEMP", self._gpu_temp_bar, self._gpu_temp),
        ], spacing=6, align=Align.STRETCH), accent=theme.CYAN_BRIGHT)

        # ── state ───────────────────────────────────────────────────────
        self._mode_value = value_label("—", color=theme.BASE_MUTED)
        self._profile_value = value_label("—", color=theme.BASE_MUTED)
        state = HudCard(Column([
            Row([key_label("GPU MODE"), Spacer(), self._mode_value],
                spacing=10, align=Align.CENTER),
            Row([key_label("PROFILE"), Spacer(), self._profile_value],
                spacing=10, align=Align.CENTER),
        ], spacing=6, align=Align.STRETCH), accent=theme.YELLOW_BRIGHT)

        self._cards = [disk, cpu, ram, self._gpu_card, state]
        self._column = Column(self._cards, spacing=12, align=Align.STRETCH)

    # UTIL/MEM/TEMP are different lengths, so without a fixed cell each
    # row's bar would start at its own x, so the key is pinned to 5.
    METRIC_KEY_CHARS = 5

    def _metric_row(self, key: str, bar: BarMeter, value: Label) -> Widget:
        return Row([key_label(key, width_chars=self.METRIC_KEY_CHARS,
                              bold=True),
                    bar, Spacer(), value],
                   spacing=10, align=Align.CENTER)

    def children(self):
        return (self._column,)

    # ── lifecycle ───────────────────────────────────────────────────────
    def attach(self, window) -> None:
        super().attach(window)
        sysinfo.subscribe(self._on_sample)
        self._refresh()
        self._refresh_state()

    def detach(self) -> None:
        sysinfo.unsubscribe(self._on_sample)
        super().detach()

    def _on_sample(self) -> None:
        self._refresh()

    def _schedule(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            pass

    def _refresh_state(self) -> None:
        """One-shot queries that only change when the user changes them."""
        if self._state_pending:
            return
        self._state_pending = True
        self._schedule(self._query_state())

    async def _query_state(self) -> None:
        try:
            mode = (await proc.run(["optimus-manager", "--print-mode"],
                                   timeout=2.0)).strip()
            # "Current GPU mode : nvidia" -> keep the mode word.
            if ":" in mode:
                mode = mode.split(":", 1)[1].strip()
            if "\n" in mode:
                mode = mode.splitlines()[-1].strip()
            profile = (await proc.run(["tuned-adm", "active"],
                                      timeout=2.0)).strip()
            if ":" in profile:
                profile = profile.split(":", 1)[1].strip()
            self._apply_state(mode or None, profile or None)
        finally:
            self._state_pending = False

    def _apply_state(self, mode: str | None, profile: str | None) -> None:
        for label, value in ((self._mode_value, mode),
                             (self._profile_value, profile)):
            label.color = VALUE_FG if value else theme.BASE_MUTED
            label.set_value(value or "—")

    # ── sampling ────────────────────────────────────────────────────────
    def _refresh(self) -> None:
        # Mirror the sampler's history rather than keeping a second one
        # fed by push(). `sysinfo` already owns a 60-sample ring at one
        # sample per second; appending to a private copy on a separate
        # clock is what let the two disagree.
        cpu = sysinfo.cpu_percent()
        ram = sysinfo.memory_percent()
        self._cpu_value.set_value(f"{cpu:.0f}%")
        self._cpu_graph.set_samples(sysinfo.cpu_history())
        self._ram_value.set_value(f"{ram:.0f}%")
        self._ram_graph.set_samples(sysinfo.memory_history())
        vm = psutil.virtual_memory()
        self._ram_total.set_value(
            f"{fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}")

        try:
            usage = psutil.disk_usage(self.disk_mount)
        except OSError:
            usage = None
        if usage is not None:
            self._disk_bar.set_value(usage.percent)
            self._disk_value.set_value(
                f"{fmt_bytes(usage.used)} / {fmt_bytes(usage.total)}"
                f"  {usage.percent:.0f}%")

        self._schedule(self._refresh_gpu())

    async def _refresh_gpu(self) -> None:
        gpu = await sample_gpu()
        self._gpu = gpu
        if gpu is None:
            self._gpu_name.set_value("not detected")
            return
        self._gpu_name.set_value(gpu["name"])
        self._gpu_util_bar.set_value(gpu["util"])
        self._gpu_util.set_value(f"{gpu['util']:.0f}%")
        total = gpu["mem_total_mib"] or 1.0
        self._gpu_mem_bar.set_value(gpu["mem_used_mib"] / total * 100.0)
        self._gpu_mem.set_value(
            f"{gpu['mem_used_mib']:.0f} / {gpu['mem_total_mib']:.0f} MiB")
        # 30°C -> 0%, 100°C -> 100%, same ramp the bar meter reads.
        self._gpu_temp_bar.set_value((gpu["temp"] - 30) * (100 / 70))
        self._gpu_temp.set_value(f"{gpu['temp']:.0f}°C")

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
