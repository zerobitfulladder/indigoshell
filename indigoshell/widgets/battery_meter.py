"""Battery shown as a `[||||  ]` silhouette: vertical brackets on the
sides, internal vertical cells that light up to match the charge.

Colors switch on state:
  - charging         → cyan bright
  - full (>=99% AC)  → lime bright
  - on battery       → gradient yellow → red as it drops
  - cells unlit      → dim variant of the active hue

A single click can be wired via `on_left_click=...` like other widgets.
"""

import math
import os

import gi
import psutil

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import theme
from .base import Widget, paint
from .stdout_text import _lerp_hex


def _ac_online_from_sysfs() -> bool | None:
    """Fallback for psutil reporting power_plugged=None (some laptops).
    Returns True/False if any Mains-type supply reports online, else None.
    """
    root = "/sys/class/power_supply"
    try:
        names = os.listdir(root)
    except OSError:
        return None
    found = False
    for name in names:
        try:
            with open(f"{root}/{name}/type") as f:
                if f.read().strip() != "Mains":
                    continue
            with open(f"{root}/{name}/online") as f:
                online = f.read().strip()
        except OSError:
            continue
        found = True
        if online == "1":
            return True
    return False if found else None


class BatteryMeter(Widget):
    interval_ms = 5000

    def __init__(
        self,
        cells: int = 16,
        cell_thick: int = 2,
        gap: int = 2,
        height: int = 22,
        corner_arm: int = 4,
        corner_thick: int = 1,
        pad_x: int = 5,
        pad_y: int = 3,
        fake_state: tuple[float, bool] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.cells = max(1, cells)
        self.cell_thick = max(1, cell_thick)
        self.gap = max(0, gap)
        self.h = max(4, height)
        self.corner_arm = max(2, corner_arm)
        self.corner_thick = max(1, corner_thick)
        self.pad_x = max(2, pad_x)
        self.pad_y = max(0, pad_y)
        self.fake_state = fake_state
        self._area: Gtk.EventBox | None = None
        self._percent: float = 0.0
        self._charging: bool = False
        self._present: bool = True
        self._mode: str = "static"  # "static" | "sweep" | "blink" | "breathe"
        self._sweep_dir: int = 1
        self._sweep_pos: int = 0
        self._anim_timer: int | None = None
        self._blink_on: bool = True
        self._breath_phase: float = 0.0

    def build_widget(self):
        cells_w = self.cells * self.cell_thick + (self.cells - 1) * self.gap
        width = cells_w + 2 * self.pad_x
        filler = Gtk.Box()
        filler.set_size_request(width, self.h)
        ev = Gtk.EventBox()
        ev.add(filler)
        ev.set_visible_window(False)
        ev.connect_after("draw", self._draw)
        self._area = ev
        return ev

    def tick(self) -> bool:
        if self.fake_state is not None:
            self._present = True
            self._percent, self._charging = self.fake_state
        else:
            b = psutil.sensors_battery()
            if b is None:
                self._present = False
            else:
                self._present = True
                self._percent = float(b.percent)
                plugged = b.power_plugged
                if plugged is None:
                    plugged = _ac_online_from_sysfs()
                self._charging = bool(plugged)
        self._reconfigure_anim()
        if self._area is not None:
            self._area.queue_draw()
        return True

    def stop(self) -> None:
        self._kill_anim()
        super().stop()

    # ── state → behavior table ────────────────────────────────────────
    def _state_config(self):
        """Returns dict with: mode, sweep, lit, empty, bracket, dir, ms."""
        if not self._present:
            return dict(
                mode="static", sweep=theme.FG_MUTED, lit=theme.FG_MUTED,
                empty=theme.FG_MUTED, bracket=theme.FG_MUTED, dir=0, ms=0,
            )
        if self._charging and self._percent >= 99:
            return dict(
                mode="breathe", sweep=theme.CYAN_BRIGHT, lit=theme.CYAN_MID,
                empty=theme.CYAN_DIM, bracket=theme.CYAN_DIM,
                dir=0, ms=33,  # ~30fps; under 0.1% CPU
            )
        if self._charging:
            return dict(
                mode="sweep", sweep=theme.CYAN_BRIGHT, lit=theme.CYAN_DIM,
                empty=theme.MAGENTA_DIM, bracket=theme.CYAN_DIM,
                dir=1, ms=70,
            )
        if self._percent < 20:
            return dict(
                mode="blink", sweep=theme.MAGENTA_BRIGHT, lit=theme.MAGENTA_BRIGHT,
                empty=theme.MAGENTA_DIM, bracket=theme.MAGENTA_DIM,
                dir=0, ms=150,
            )
        if self._percent < 50:
            return dict(
                mode="sweep", sweep=theme.YELLOW_BRIGHT, lit=theme.YELLOW_DIM,
                empty=theme.MAGENTA_DIM, bracket=theme.YELLOW_DIM,
                dir=-1, ms=90,
            )
        return dict(
            mode="sweep", sweep=theme.CYAN_BRIGHT, lit=theme.CYAN_DIM,
            empty=theme.MAGENTA_DIM, bracket=theme.CYAN_DIM,
            dir=-1, ms=110,
        )

    # ── animation timer ───────────────────────────────────────────────
    def _reconfigure_anim(self) -> None:
        cfg = self._state_config()
        prev_mode, prev_dir = self._mode, self._sweep_dir
        self._mode = cfg["mode"]
        self._sweep_dir = cfg["dir"]
        # Reset sweep position when entering a new sweep direction so
        # the animation starts at a sensible endpoint.
        if self._mode == "sweep" and (
            prev_mode != "sweep" or prev_dir != self._sweep_dir
        ):
            lit_count = max(1, int(self._percent / 100.0 * self.cells))
            self._sweep_pos = 0 if self._sweep_dir > 0 else lit_count
        if self._mode == "static":
            self._kill_anim()
            return
        # Restart timer if interval changed (cheap and correct).
        self._kill_anim()
        self._anim_timer = GLib.timeout_add(cfg["ms"], self._tick_anim)

    def _kill_anim(self) -> None:
        if self._anim_timer is not None:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None

    def _tick_anim(self) -> bool:
        if self._mode == "sweep":
            lit_count = max(1, int(self._percent / 100.0 * self.cells))
            # Sweep across [0..lit_count] inclusive. Charging counts
            # up (filling); discharging counts down (emptying).
            nxt = self._sweep_pos + self._sweep_dir
            if nxt < 0:
                nxt = lit_count
            elif nxt > lit_count:
                nxt = 0
            self._sweep_pos = nxt
        elif self._mode in ("blink", "pulse"):
            self._blink_on = not self._blink_on
        elif self._mode == "breathe":
            # ~2.5 s full cycle: 0.08 rad/frame at 30fps.
            self._breath_phase = (self._breath_phase + 0.08) % (2 * math.pi)
        else:
            self._anim_timer = None
            return False
        if self._area is not None:
            self._area.queue_draw()
        return True

    # ── draw ──────────────────────────────────────────────────────────
    def _draw(self, w, cr) -> bool:
        alloc = w.get_allocation()
        width, height = alloc.width, alloc.height
        cfg = self._state_config()
        ct = self.cell_thick
        gap = self.gap
        lit_count = int(self._percent / 100.0 * self.cells)

        # ── Corner brackets (4 L-shapes) ──
        paint(cr, cfg["bracket"])
        arm = self.corner_arm
        t = self.corner_thick
        # top-left
        cr.rectangle(0, 0, arm, t)
        cr.rectangle(0, 0, t, arm)
        # top-right
        cr.rectangle(width - arm, 0, arm, t)
        cr.rectangle(width - t, 0, t, arm)
        # bottom-left
        cr.rectangle(0, height - t, arm, t)
        cr.rectangle(0, height - arm, t, arm)
        # bottom-right
        cr.rectangle(width - arm, height - t, arm, t)
        cr.rectangle(width - t, height - arm, t, arm)
        cr.fill()

        # ── Cells ──
        inner_top = self.pad_y
        inner_h = max(1, height - 2 * self.pad_y)
        x = self.pad_x
        for i in range(self.cells):
            if i >= lit_count:
                color = cfg["empty"]
            elif self._mode == "sweep":
                # Cumulative from the LEFT in both directions. Charging
                # fills up; discharging empties (sweep_pos decreases).
                color = cfg["sweep"] if i < self._sweep_pos else cfg["lit"]
            elif self._mode == "blink":
                color = cfg["sweep"] if self._blink_on else cfg["empty"]
            elif self._mode == "pulse":
                color = cfg["sweep"] if self._blink_on else cfg["lit"]
            elif self._mode == "breathe":
                t = (math.sin(self._breath_phase) + 1) / 2
                color = _lerp_hex(cfg["lit"], cfg["sweep"], t)
            else:
                color = cfg["lit"]
            paint(cr, color)
            cr.rectangle(x, inner_top, ct, inner_h)
            cr.fill()
            x += ct + gap
        return False
