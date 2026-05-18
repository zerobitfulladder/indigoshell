"""Shared audio source.

Two independent backends:

- **cava** streams 16-bit-unsigned band magnitudes at 60 fps for the
  visualizer (Media's background, etc).
- **aubio** does beat detection: a `parec` subprocess pipes raw PCM to
  `aubio.tempo` running in a worker thread; each detected beat is
  dispatched to listeners on the GTK main loop.

Both lifecycles are independent — backends start lazily when someone
subscribes and stop when nobody's listening. `BarKind.teardown` force-
stops both so daemon reload doesn't leak processes.
"""

import os
import struct
import subprocess
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from . import proc

CAVA_CONFIG = os.path.join(os.path.dirname(__file__), "cava_raw.conf")

# Must match cava_raw.conf
N_BARS = 20
FRAMERATE = 60
BYTES_PER_BAR = 2
FRAME_BYTES = N_BARS * BYTES_PER_BAR
UNPACK = struct.Struct(f"<{N_BARS}H").unpack

# Aubio beat tracker config
SAMPLE_RATE = 44100
AUBIO_WIN = 1024
AUBIO_HOP = 512
AUBIO_MIN_CONFIDENCE = 0.1  # drop beats aubio isn't confident about


class BeatDetector:
    def __init__(self):
        self._beat_listeners: list[Callable[[], None]] = []
        self._bands_listeners: list[Callable[[tuple[int, ...]], None]] = []
        self._lock = threading.Lock()

        self._cava_proc: subprocess.Popen | None = None
        self._cava_thread: threading.Thread | None = None
        self._cava_running = False

        self._parec_proc: subprocess.Popen | None = None
        self._aubio_thread: threading.Thread | None = None
        self._aubio_running = False

    # ── public API ────────────────────────────────────────────────────
    def add_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._beat_listeners.append(fn)
            need_aubio = not self._aubio_running
        if need_aubio:
            self._start_aubio()

    def remove_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._beat_listeners.remove(fn)
            except ValueError:
                pass
            stop = not self._beat_listeners and self._aubio_running
        if stop:
            self._stop_aubio()

    def add_bands_listener(self, fn: Callable[[tuple[int, ...]], None]) -> None:
        with self._lock:
            self._bands_listeners.append(fn)
            need_cava = not self._cava_running
        if need_cava:
            self._start_cava()

    def remove_bands_listener(self, fn: Callable[[tuple[int, ...]], None]) -> None:
        with self._lock:
            try:
                self._bands_listeners.remove(fn)
            except ValueError:
                pass
            stop = not self._bands_listeners and self._cava_running
        if stop:
            self._stop_cava()

    def _stop(self) -> None:
        """Force-stop both backends — called by BarKind.teardown."""
        self._stop_cava()
        self._stop_aubio()

    # ── cava (bands) ──────────────────────────────────────────────────
    def _start_cava(self) -> None:
        self._cava_proc = proc.popen(["cava", "-p", CAVA_CONFIG])
        if self._cava_proc is None:
            return
        self._cava_running = True
        self._cava_thread = threading.Thread(target=self._cava_loop, daemon=True)
        self._cava_thread.start()

    def _stop_cava(self) -> None:
        self._cava_running = False
        proc = self._cava_proc
        self._cava_proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

    def _cava_loop(self) -> None:
        proc = self._cava_proc
        if proc is None or proc.stdout is None:
            return
        read = proc.stdout.read
        while self._cava_running:
            buf = read(FRAME_BYTES)
            if not buf or len(buf) < FRAME_BYTES:
                break
            bands = UNPACK(buf)
            self._fire_bands(bands)

    # ── aubio (beats) ─────────────────────────────────────────────────
    def _monitor_source(self) -> str | None:
        sink = proc.run(["pactl", "get-default-sink"]).strip()
        return f"{sink}.monitor" if sink else None

    def _start_aubio(self) -> None:
        monitor = self._monitor_source()
        if monitor is None:
            return
        self._parec_proc = proc.popen([
            "parec",
            f"--device={monitor}",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            "--latency-msec=50",
        ])
        if self._parec_proc is None:
            return
        self._aubio_running = True
        self._aubio_thread = threading.Thread(target=self._aubio_loop, daemon=True)
        self._aubio_thread.start()

    def _stop_aubio(self) -> None:
        self._aubio_running = False
        proc = self._parec_proc
        self._parec_proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

    def _aubio_loop(self) -> None:
        # Import lazily so a system missing aubio doesn't break the
        # module's import (BeatDetector is created on bar init).
        try:
            import aubio
            import numpy as np
        except ImportError:
            return

        proc = self._parec_proc
        if proc is None or proc.stdout is None:
            return
        tempo = aubio.tempo("default", AUBIO_WIN, AUBIO_HOP, SAMPLE_RATE)
        bytes_per_hop = AUBIO_HOP * 2  # s16le mono
        read = proc.stdout.read

        while self._aubio_running:
            data = read(bytes_per_hop)
            if not data or len(data) < bytes_per_hop:
                break
            samples = (
                np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            )
            if tempo(samples) and tempo.get_confidence() >= AUBIO_MIN_CONFIDENCE:
                self._fire_beat()

    # ── dispatch ──────────────────────────────────────────────────────
    def _fire_beat(self) -> None:
        with self._lock:
            fns = list(self._beat_listeners)
        if not fns:
            return
        GLib.idle_add(self._dispatch_beats, fns)

    def _fire_bands(self, bands: tuple[int, ...]) -> None:
        with self._lock:
            fns = list(self._bands_listeners)
        if not fns:
            return
        GLib.idle_add(self._dispatch_bands, bands, fns)

    def _dispatch_beats(self, fns) -> bool:
        for fn in fns:
            try:
                fn()
            except Exception:
                pass
        return False

    def _dispatch_bands(self, bands, fns) -> bool:
        for fn in fns:
            try:
                fn(bands)
            except Exception:
                pass
        return False


_detector: BeatDetector | None = None


def get_detector() -> BeatDetector:
    global _detector
    if _detector is None:
        _detector = BeatDetector()
    return _detector
