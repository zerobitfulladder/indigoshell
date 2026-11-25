"""The agent's effects over a page it drives: the shell's glitch, a tint, its scanlines.

The user's design: while the agent is in control of a tab (the Watch's
lock, watch.py), the page says so, in the shell's own look:

- The lock starts with the shell's panel spawn over the page (the
  shell's indigoshell/effects.py ScanLock, played with config_default's
  PANEL_SPAWN): bands of the page snap in out of order, torn sideways,
  their colours split.
- While it holds: a faint tint in her colour (core/persona.py, as the
  frame and the mark: every mark she makes is hers), the panels' raster
  over it, and the panels' scan (widgets/holo.py): every few seconds a
  glowing band runs down the page, tearing the rows under it. And the glitch again at random
  intervals (REPEAT_S), never while one of the agent's actions is in
  flight there.
- The lock ends with the glitch played backwards over the live page; a
  glitch still playing in reverses from where it is (a take-over). A
  locked tab that closes or stops being shown simply vanishes.

How. A transparent QQuickWidget over the window's page (stacked on top,
a transparent clear colour, no input, no focus), inset by the Watch's
frame and with the Watch's mark kept out, so both stay as they are. Two
ShaderEffects (effects.qml) whose fragment shaders are ports of the
shell's SkSL, line for line (shaders/*.frag), precompiled with qsb into
the .qsb files beside them: nothing compiles at run time. Python sets
their uniforms on a timer at the shell's rates (60 fps for the glitch,
30 for the scan) and stops it when nothing moves: between passes, and
while the window is not exposed (another workspace), the overlay draws
no frame at all. A page that is taller than the band covers in the
shell's 7 s gets a pass and a breath (`period`).

The page's pixels come from the view's own QQuickWidget (its focus
proxy), read back (grabFramebuffer) only after it has drawn since the
last read, at most every REGRAB_S while it keeps drawing, and handed
over whole: a still page costs uniforms alone per frame. Not
view.grab(): 2-3x slower, and it makes the page widget draw again. None
of this is in the agent's screenshots: they grab the view alone
(reader.grab).

Measured on an NVIDIA laptop at 2560x1380 (glitch: 14 frames 17.5 ms
apart; scan: 30 fps, 0.2 ms a frame, ~6% of a core; idle: no frames). A
page that draws every frame (a video) makes each read back wait behind
the window's vsync'd swap, ~18 ms: the scan drops to ~25 fps for a pass
over such a page.

Where it cannot run, the Watch keeps its plain frame and mark and the
reason is logged once (`make`): a scene graph that does not draw on
the GPU (Software), the QML failing to load, or a shader failing to (seen after
the overlay's first frame: a ShaderEffect loads its shader then, and
draws nothing when it cannot).

Rebuild the .qsb files after editing a .frag (qsb, from Qt Shader Tools,
found through QLibraryInfo or PATH; 6.12's writes the format Qt 6.8
reads):

    python -m flux.qt.effects --build-shaders
    python -m flux.qt.effects --check-shaders

Qt: nothing here is newer than 6.8.
"""

import argparse
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QLibraryInfo, QObject, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QImage, QVector4D
from PySide6.QtQuick import QQuickImageProvider, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget

from indigoshell.core import persona

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
QML = HERE / "effects.qml"
SHADERS = HERE / "shaders"
SOURCES = ("scanlock.frag", "holo.frag")

# ── the shell's numbers ─────────────────────────────────────────────────
# Copied, with where they come from: this package imports no skia and no
# shell theme (__init__.py).
# config_default.PANEL_SPAWN = ScanLock(duration=0.22, band_px=36.0,
#     tear_px=29.0, split_px=17.0, spark=(0.035, 0.33, 0.35))
SPAWN_S = 0.22
SPAWN_FPS = 60
BAND_PX = 36.0
TEAR_PX = 29.0
SPLIT_PX = 17.0
SPARK = (0.035, 0.33, 0.35)
NOISE = 0.0
MAX_PHASE_STEP = 2.0        # window.py: a stall holds the effect, it does not skip ahead
# widgets/holo.py
RASTER_PITCH = 3
RASTER_ALPHA = 0.07
RASTER_LIFT = 0.35          # toward theme.HUD_FG_BRIGHT
HUD_FG_BRIGHT = "#c8d0e8"
SCAN_EVERY_S = 7.0
SCAN_SPEED = 170.0
SCAN_HEAD_PX = 11.0
SCAN_WAKE_PX = 100.0
SCAN_SHIFT_PX = 9.0
SCAN_SPLIT_PX = 2.5
SCAN_RATE = 18.0
SCAN_FPS = 30
BELOW = 2.5
SCAN_BREATH_S = 2.0         # a page too tall for SCAN_EVERY_S: this long between passes
# agent/ui/style.py: a plate is its base pulled PLATE_TINT toward its colour
PLATE_TINT = 0.12
RASTER = "#05d9e8"          # theme.POPUP_RASTER: the panels' raster
BACKGROUND = "#050310"      # theme.BAR_BG, Flux's window background

# Flux's own.
REPEAT_S = (5.0, 15.0)      # the glitch again, after a uniform pause in this range
REGRAB_S = 0.1              # a page that keeps drawing: read back at most this often
HOLD_MARGIN_S = 1 / 60      # an action waits for the glitch's last frame too
EVENTS_KEPT = 200

_off: str | None = None     # why the effects do not run in this process


class Unavailable(RuntimeError):
    """The effects cannot run here: the Watch keeps its plain overlay."""


def off_reason() -> str | None:
    return _off


def turn_off(why: str, *, quiet: bool = False) -> None:
    """No effects in this process from now on; said once."""
    global _off
    if _off is None:
        _off = why
        (log.info if quiet else log.warning)(
            "the agent's page effects are off: %s; the Watch's frame and mark stay", why)


def make(window, acting=None) -> "PageFx | None":
    """The effects for `window`, or None where they cannot run (said once).
    `acting(tab_id)`: one of the agent's actions is in flight on the tab."""
    if _off is not None:
        return None
    missing = [p.name for p in (QML, *(SHADERS / f"{s}.qsb" for s in SOURCES)) if not p.exists()]
    if missing:
        turn_off(f"missing {', '.join(missing)}")
        return None
    try:
        return PageFx(window, acting)
    except Unavailable as e:
        turn_off(str(e))
    except Exception as e:              # noqa: BLE001  (anything: the plain Watch goes on)
        log.exception("the page effects could not be made")
        turn_off(f"{type(e).__name__}: {e}")
    return None


# ── colours ─────────────────────────────────────────────────────────────
def _lerp(a: str, b: str, t: float) -> QColor:
    """theme.lerp: blended in 8-bit channels, as the shell's colours are."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


def _premul(color: QColor, alpha: float) -> QVector4D:
    """theme.color(spec, alpha): alpha in 8 bits, then premultiplied."""
    a = round(alpha * 255) / 255
    return QVector4D(color.redF() * a, color.greenF() * a, color.blueF() * a, a)


def tint_rgb(over: tuple) -> tuple:
    """What the tint makes of an 8-bit colour on a row without a raster
    line (the tests compare the window's pixels with it)."""
    t = _premul(QColor(persona.color()), PLATE_TINT)
    return tuple(round(c * 255 + (1 - t.w()) * v) for c, v in zip((t.x(), t.y(), t.z()), over))


# ── the page's pixels, to the scene ─────────────────────────────────────
class Pictures(QQuickImageProvider):
    """image://flux/<name>/<n>: the latest QImage handed in under name."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.images: dict[str, QImage] = {}

    def requestImage(self, id_: str, size: QSize, requested: QSize) -> QImage:
        img = self.images.get(id_.split("/", 1)[0], QImage())
        if size is not None:
            size.setWidth(img.width())
            size.setHeight(img.height())
        return img


# ── one window's effects ────────────────────────────────────────────────
class PageFx(QObject):
    """The effects over one window's page. The Watch decides (watch.py
    `_show`): `lock` a tab, `unlock` (the glitch out), `vanish`; `place`
    keeps the overlay over the page; `hold_s` says how long an action
    should wait for a glitch to end."""

    def __init__(self, window, acting=None) -> None:
        super().__init__(window)
        self.window = window
        self.acting = acting or (lambda tab_id: False)
        self.dead = False
        self.phase = "off"              # off | in | holo | out
        self.tab_id: int | None = None
        self.view = None
        self.frames = 0                 # frames the overlay drew (the tests count them)
        self.events: list[tuple[str, float]] = []   # (what, monotonic): lock, repeat, skip, ...
        self.repeats = self.skipped = 0
        w = self.widget = QQuickWidget(window)
        w.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop)     # over the page's own widget
        w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        w.setClearColor(Qt.GlobalColor.transparent)
        w.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        w.hide()
        api = w.quickWindow().rendererInterface().graphicsApi()
        if not QSGRendererInterface.isApiRhiBased(api):
            self._refuse(f"Qt Quick draws with {api.name} here, not the GPU")
        self.pictures = Pictures()      # PySide: held here, or it is dropped
        w.engine().addImageProvider("flux", self.pictures)
        w.rootContext().setContextProperty("fluxShaders",
                                           QUrl.fromLocalFile(str(SHADERS) + os.sep).toString())
        w.setSource(QUrl.fromLocalFile(str(QML)))
        if w.status() != QQuickWidget.Status.Ready:
            errors = "; ".join(e.toString() for e in w.errors()) or str(w.status())
            self._refuse(f"effects.qml: {errors}")
        root = self.root = w.rootObject()
        self.glitch = root.findChild(QObject, "glitch")
        self.holo = root.findChild(QObject, "holo")
        self.page = root.findChild(QObject, "page")
        if None in (self.glitch, self.holo, self.page):
            self._refuse("effects.qml lacks an item")
        root.setProperty("line", _premul(_lerp(RASTER, HUD_FG_BRIGHT, RASTER_LIFT), RASTER_ALPHA))
        bg = QColor(BACKGROUND)
        root.setProperty("bg", QVector4D(bg.redF(), bg.greenF(), bg.blueF(), 1.0))
        root.setProperty("pitch", float(RASTER_PITCH))
        g = self.glitch
        g.setProperty("spark", QVector4D(*SPARK, 0.0))
        g.setProperty("noise", NOISE)
        h = self.holo
        for name, value in (("head_px", SCAN_HEAD_PX), ("wake", SCAN_WAKE_PX),
                            ("shift", SCAN_SHIFT_PX), ("split", SCAN_SPLIT_PX),
                            ("rate", SCAN_RATE)):
            h.setProperty(name, float(value))
        self._n = 0                     # image sources are new names each time
        self._checked: set[str] = set() # effects whose shader was seen to load
        self._img: QImage | None = None # the page's pixels, cropped to the overlay
        self._dirty = True              # the page drew since `_img`
        self._read_at = 0.0
        self._reading = False           # our own read makes the page widget draw: not news
        self._source = None             # the page widget listened to
        self._handle = None             # the QWindow whose exposure is followed
        self._following = False         # ... and the window's own Show and WinIdChange
        self._elapsed = 0.0             # of the running glitch, frames' own time
        self._at: float | None = None
        self._t0 = time.monotonic()
        self._epoch = 0.0               # the scan's passes count from here
        self._new_lock = False          # the next tint is a new lock's (not a repeat's)
        self._owed = False              # the lock's glitch, put off until the window is seen
        self._y: float | None = None    # where the band is, None between passes
        self._pass_seed = 0.0
        self.timer = QTimer(self)       # frames
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.wake = QTimer(self)        # the scan's next pass
        self.wake.setSingleShot(True)
        self.wake.timeout.connect(self._next_pass)
        self.again = QTimer(self)       # the next repeat glitch
        self.again.setSingleShot(True)
        self.again.timeout.connect(self._repeat)
        w.quickWindow().afterRendering.connect(self._drew)

    def _note(self, what: str) -> None:
        """What happened when (the tests read it), the latest EVENTS_KEPT."""
        self.events.append((what, time.monotonic()))
        del self.events[:-EVENTS_KEPT]

    def _refuse(self, why: str) -> None:
        """Made in vain: gone again, and why."""
        self.widget.deleteLater()
        self.deleteLater()
        raise Unavailable(why)

    # ── what the Watch asks ─────────────────────────────────────────────
    def place(self, page: QRect, keep: QRect | None) -> None:
        """Over `page` (the window's page, inside the Watch's frame), with
        `keep` (the mark, in the window's coordinates) left alone."""
        if self.widget.geometry() != page:
            self.widget.setGeometry(page)
            self._dirty = True
        k = keep.translated(-page.x(), -page.y()) if keep is not None else QRect()
        self.root.setProperty("keep", QVector4D(k.x(), k.y(), k.width(), k.height()))

    def lock(self, tab) -> None:
        """The agent's lock on `tab` starts: the glitch in, then the tint,
        the raster and the scan, and repeats now and then."""
        self._stop()
        self.tab_id, self.view = tab.id, tab.view
        # Her colour as it is now: the shell may have told us since.
        self.root.setProperty("tint", _premul(QColor(persona.color()), PLATE_TINT))
        self._new_lock = True
        self._follow_exposure()
        self._note("lock")
        self._glitch("in", lock=True)

    def unlock(self) -> None:
        """The lock ends with the tab still shown: the glitch out over the
        live page. A glitch in still playing reverses from where it is."""
        if self.phase == "off" or self.dead:
            return
        self.wake.stop()
        self.again.stop()
        self.root.setProperty("keep", QVector4D(0, 0, 0, 0))     # the mark goes with the lock
        self._note("unlock")
        if self.phase == "in":
            # The same still image and tears, played back from here.
            appear = float(self.glitch.property("appear") or 0.0)
            self.phase = "out"
            self._elapsed, self._at = (1.0 - appear) * SPAWN_S, None
            self.timer.start(round(1000 / SPAWN_FPS))
            return
        self._glitch("out")

    def vanish(self) -> None:
        """Gone at once: the locked tab closed, or is no longer shown."""
        if self.phase != "off":
            self._note("vanish")
        self._stop()
        self.tab_id = self.view = None

    def hold_s(self) -> float:
        """How long an action should wait: what is left of a glitch in
        (the lock's or a repeat), whose still image would hide its change."""
        if self.phase != "in":
            return 0.0
        return max(0.0, SPAWN_S - self._elapsed) + HOLD_MARGIN_S

    def close(self) -> None:
        self._stop()
        self._listen(None)
        if self._following and shiboken6.isValid(self.window):
            self.window.removeEventFilter(self)
        if self._handle is not None and shiboken6.isValid(self._handle):
            self._handle.removeEventFilter(self)
        if shiboken6.isValid(self.widget):
            self.widget.deleteLater()
        self.deleteLater()

    # ── state ───────────────────────────────────────────────────────────
    def _stop(self) -> None:
        self.timer.stop()
        self.wake.stop()
        self.again.stop()
        self.phase = "off"
        self._y = None
        self._owed = False
        if shiboken6.isValid(self.widget):
            self.root.setProperty("mode", "off")
            self.widget.hide()

    def _exposed(self) -> bool:
        handle = self.window.windowHandle()
        return (handle is not None and handle.isExposed() and self.window.isVisible()
                and self.widget.width() > 0 and self.widget.height() > 0)

    def _follow_exposure(self) -> None:
        """Expose events from the window's QWindow. A window can lock
        before it has one (a tab borrowed into the agent's window before
        it shows): its Show then, or a new native window, says when."""
        if not self._following:
            self._following = True
            self.window.installEventFilter(self)
        handle = self.window.windowHandle()
        if handle is not self._handle:
            if self._handle is not None and shiboken6.isValid(self._handle):
                self._handle.removeEventFilter(self)
            self._handle = handle
            if handle is not None:
                handle.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if obj is self.window and kind in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            self._follow_exposure()
            if self.phase == "holo":
                QTimer.singleShot(0, self, self._exposure)
        elif kind == QEvent.Type.Expose and self.phase == "holo":
            QTimer.singleShot(0, self, self._exposure)
        return False

    def _exposure(self) -> None:
        """Shown (again): a lock's glitch put off until now plays (a
        borrowed tab arriving locked in the agent's window before it
        shows); else the scan picks up where its clock is."""
        if self.phase == "holo" and self._exposed() and self._owed:
            self._owed = False
            self.timer.stop()
            self.wake.stop()
            self._glitch("in")
            return
        if self.phase == "holo" and self._exposed() and not self.timer.isActive():
            self.wake.stop()
            self._scan_frame()
            if self._y is not None:
                self.timer.start(round(1000 / SCAN_FPS))
            else:
                self._arm_pass()

    def _drew(self) -> None:
        """The overlay drew a frame; the first in each effect shows whether
        its shader loaded (a ShaderEffect loads it on its first frame)."""
        self.frames += 1
        mode = self.root.property("mode")
        if mode in ("glitch", "holo") and mode not in self._checked:
            QTimer.singleShot(0, self, lambda: self._check(mode))

    def _check(self, mode: str) -> None:
        if self.dead or mode in self._checked:
            return
        status = int((self.glitch if mode == "glitch" else self.holo).property("statusCode"))
        if status == 0:                 # ShaderEffect.Compiled
            self._checked.add(mode)
        elif status == 2:               # Error: it draws nothing
            turn_off(f"the {mode} shader did not load")
            self.dead = True
            self.vanish()

    # ── the page's pixels ───────────────────────────────────────────────
    def _page_widget(self):
        """The view's own QQuickWidget, which draws the page: its focus
        proxy (made again when the page moves to another renderer)."""
        if self.view is None or not shiboken6.isValid(self.view):
            return None
        proxy = self.view.focusProxy()
        if isinstance(proxy, QQuickWidget):
            return proxy
        return self.view.findChild(QQuickWidget)

    def _listen(self, source) -> None:
        if source is self._source:
            return
        if self._source is not None and shiboken6.isValid(self._source):
            try:
                self._source.quickWindow().afterRendering.disconnect(self._page_drew)
            except (RuntimeError, TypeError):
                pass
        self._source = source
        self._dirty = True
        if source is not None:
            source.quickWindow().afterRendering.connect(self._page_drew)

    def _page_drew(self) -> None:
        if not self._reading:
            self._dirty = True

    def _read(self, *, force: bool = False) -> bool:
        """The page's pixels into the scene when it drew since the last
        read (or `force`); False when there is no page to read."""
        source = self._page_widget()
        self._listen(source)
        if source is None:
            return False
        now = time.monotonic()
        if self._img is not None and not force and (
                not self._dirty or now - self._read_at < REGRAB_S):
            return True
        self._reading = True
        try:
            full = source.grabFramebuffer()
        finally:
            self._reading = False
        if full.isNull() or source.width() <= 0:
            return False
        # The overlay sits inside the Watch's frame: the same part of the page.
        s = full.width() / source.width()
        at = self.widget.geometry().topLeft() - source.mapTo(self.window, source.rect().topLeft())
        self._img = full.copy(round(at.x() * s), round(at.y() * s),
                              round(self.widget.width() * s), round(self.widget.height() * s))
        self._dirty, self._read_at = False, now
        self._n += 1
        self.pictures.images["page"] = self._img
        self.page.setProperty("source", QUrl(f"image://flux/page/{self._n}"))
        return True

    # ── the glitch ──────────────────────────────────────────────────────
    def _glitch(self, phase: str, *, lock: bool = False) -> None:
        exposed = self._exposed()
        if not exposed or not self._read(force=True):
            # Nothing to see it, or nothing to play it over: its end at once
            # (a lock's own glitch: owed until the window is seen).
            self._note(f"{phase}-skipped")
            if phase == "in":
                self._owed = lock and not exposed
                self._holo()
            else:
                self._stop()
            return
        w, h = self.widget.width(), self.widget.height()
        g = self.glitch
        g.setProperty("rows", h / BAND_PX)
        g.setProperty("tear", 2.0 * TEAR_PX / w)
        g.setProperty("split", SPLIT_PX / w)
        g.setProperty("seed", random.random() * 997.0)    # every glitch its own tears
        g.setProperty("appear", 0.0 if phase == "in" else 1.0)
        g.setProperty("t", 0.0)
        self.phase = phase
        self._elapsed, self._at = 0.0, None
        self._t0 = time.monotonic()
        self.root.setProperty("mode", "glitch")
        self.widget.show()
        self.widget.raise_()
        self.timer.start(round(1000 / SPAWN_FPS))

    def _advance(self) -> float:
        """window.py `_advance_phase`: frames' own time, the first frame
        one interval in, a stall holding the effect where it was."""
        now = time.monotonic()
        interval = 1.0 / SPAWN_FPS
        step = interval if self._at is None else now - self._at
        self._at = now
        self._elapsed += min(step, MAX_PHASE_STEP * interval)
        return self._elapsed

    def _tick(self) -> None:
        if self.phase in ("in", "out"):
            if not self._exposed():
                self._elapsed = SPAWN_S             # hidden meanwhile: over
            k = min(1.0, self._advance() / SPAWN_S)
            self.glitch.setProperty("appear", k if self.phase == "in" else 1.0 - k)
            self.glitch.setProperty("t", time.monotonic() - self._t0)
            if k >= 1.0:
                if self.phase == "in":
                    self._note("in-end")
                    self._holo()
                else:
                    self._note("out-end")
                    self._stop()
        elif self.phase == "holo":
            self._scan_frame()

    # ── the tint, the raster, the scan ──────────────────────────────────
    def _travel(self) -> float:
        return (self.widget.height() + SCAN_WAKE_PX + 2 * BELOW * SCAN_HEAD_PX) / SCAN_SPEED

    def period(self) -> float:
        """The shell's 7 s between passes, or a pass and a breath for a
        page taller than the band covers in that time."""
        return max(SCAN_EVERY_S, self._travel() + SCAN_BREATH_S)

    def _holo(self) -> None:
        self.phase = "holo"
        if self._new_lock:
            self._new_lock = False
            self._epoch = time.monotonic()          # the first pass starts with the lock
        self.holo.setProperty("scanning", 0.0)
        self.root.setProperty("mode", "holo")
        self.widget.show()
        self.widget.raise_()
        self.timer.stop()
        self._y = None
        if self._exposed():
            self._scan_frame()
            if self._y is not None:
                self.timer.start(round(1000 / SCAN_FPS))
            else:
                self._arm_pass()
        self._arm_repeat()

    def _band_y(self) -> float | None:
        into = (time.monotonic() - self._epoch) % self.period()
        y = -BELOW * SCAN_HEAD_PX + into * SCAN_SPEED
        over = self.widget.height() + SCAN_WAKE_PX + BELOW * SCAN_HEAD_PX
        return y if y < over else None

    def _arm_pass(self) -> None:
        into = (time.monotonic() - self._epoch) % self.period()
        self.wake.start(max(1, round((self.period() - into) * 1000)))

    def _next_pass(self) -> None:
        if self.phase == "holo" and self._exposed():
            self._scan_frame()
            self.timer.start(round(1000 / SCAN_FPS))

    def _scan_frame(self) -> None:
        h = self.holo
        if not self._exposed():
            self.timer.stop()               # Expose picks it up again (`_exposure`)
            return
        was, y = self._y, self._band_y()
        self._y = y
        if y is None or not self._read():
            h.setProperty("scanning", 0.0)      # one last frame clears the strip
            self.timer.stop()
            self._arm_pass()
            return
        now = time.monotonic()
        if was is None or y < was:
            self._pass_seed = (now * 7.31) % 97.0   # holo.py: a new pass, new tears
        height = self.widget.height()
        top = max(0, math.floor(y - SCAN_WAKE_PX - 2))
        bottom = min(height, math.ceil(y + BELOW * SCAN_HEAD_PX + 2))
        for name, value in (("stripTop", top), ("stripH", max(0, bottom - top)),
                            ("texTop", 0.0), ("texH", float(height)),
                            ("head", y), ("t", now - self._t0), ("seed", self._pass_seed),
                            ("scanning", 1.0)):
            h.setProperty(name, float(value))

    # ── the glitch again, now and then ──────────────────────────────────
    def _arm_repeat(self) -> None:
        lo, hi = REPEAT_S
        self.again.start(max(1, round(random.uniform(lo, hi) * 1000)))

    def _repeat(self) -> None:
        if self.phase != "holo" or self.dead:
            return
        if not self._exposed() or self.acting(self.tab_id):
            # Nobody to see it, or an action in flight whose change the
            # still image would hide: not this time.
            self.skipped += 1
            self._note("skip")
            self._arm_repeat()
            return
        self.repeats += 1
        self._note("repeat")
        self.timer.stop()
        self.wake.stop()
        self._glitch("in")


# ── the shaders ─────────────────────────────────────────────────────────
def find_qsb() -> str | None:
    """Qt Shader Tools' qsb: beside Qt's own tools, else on PATH."""
    for where in (QLibraryInfo.LibraryPath.BinariesPath,
                  QLibraryInfo.LibraryPath.LibraryExecutablesPath):
        path = Path(QLibraryInfo.path(where)) / "qsb"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for name in ("qsb", "qsb6", "qsb-qt6"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_shaders(out: Path | None = None, qsb: str | None = None) -> list[Path]:
    """Every .frag in shaders/ into its .qsb (in `out`, else beside it)."""
    qsb = qsb or find_qsb()
    if qsb is None:
        raise FileNotFoundError("no qsb (Qt Shader Tools: qt6-shadertools on Arch)")
    made = []
    for name in SOURCES:
        target = (out or SHADERS) / f"{name}.qsb"
        subprocess.run([qsb, "--qt6", "-o", str(target), str(SHADERS / name)], check=True,
                       capture_output=True, text=True)
        made.append(target)
    return made


def shaders_current(qsb: str | None = None) -> list[str]:
    """The .qsb files that differ from a fresh build of their .frag."""
    with tempfile.TemporaryDirectory() as tmp:
        fresh = build_shaders(Path(tmp), qsb)
        return [p.name for p in fresh
                if not (SHADERS / p.name).exists()
                or (SHADERS / p.name).read_bytes() != p.read_bytes()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build-shaders", action="store_true",
                       help="rebuild shaders/*.qsb from shaders/*.frag")
    group.add_argument("--check-shaders", action="store_true",
                       help="say whether the .qsb files match a fresh build")
    args = parser.parse_args(argv)
    try:
        if args.build_shaders:
            for path in build_shaders():
                print(f"built {path}")
            return 0
        stale = shaders_current()
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(getattr(e, "stderr", None) or e, file=sys.stderr)
        return 2
    print("stale: " + ", ".join(stale) if stale else "the .qsb files match their sources")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
