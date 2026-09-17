"""Shared audio source — two independent backends.

- **cava** streams 16-bit unsigned band magnitudes for the visualiser
  (the Media widget's background).
- **aubio** does beat detection: `parec` pipes raw PCM off the default
  sink's monitor and `aubio.tempo` turns it into beats.

Each backend is reference-counted on its own listener list, starts lazily
when someone subscribes and dies when the last one leaves — and the
widgets only subscribe while a player reports Playing, so neither process
exists when the room is quiet.

Neither backend needs a thread. `await readexactly` does not block, and
`aubio.tempo` costs ~13µs per 512-sample hop (~0.1% of a core at
44.1kHz), so both run directly on the shell's one loop and a callback
fires on the thread that owns the widget tree — no marshalling.
"""

import asyncio
import logging
import os
import shutil
import struct
import sys
import sysconfig
from typing import Awaitable, Callable

from . import proc

log = logging.getLogger(__name__)

CAVA_CONFIG = os.path.join(os.path.dirname(__file__), "cava_raw.conf")

# Must match cava_raw.conf.
BARS = 20
FRAMERATE = 30
FRAME_BYTES = BARS * 2
_unpack = struct.Struct(f"<{BARS}H").unpack

# Aubio beat tracker config.
SAMPLE_RATE = 44100
AUBIO_WIN = 1024
AUBIO_HOP = 512
AUBIO_MIN_CONFIDENCE = 0.1      # drop beats aubio isn't confident about
HOP_BYTES = AUBIO_HOP * 2       # s16le mono

Bands = tuple[int, ...]

_aubio = None
_numpy = None
_aubio_checked = False


def load_aubio():
    """Import aubio (and the numpy its binding rides on), reaching into
    the base interpreter's site-packages if this venv cannot see it.

    aubio ships as a distro package (Arch's `python-aubio`) and has no
    wheel for this interpreter, so the system install is the only one
    there is. The venv is created with `include-system-site-packages =
    true` so the import works, but `uv sync` rewrites `pyvenv.cfg` and
    would take the beat pulse silently with it. Hence the fallback:
    probe the base prefix directly rather than let a venv rebuild turn
    the feature off.

    Deferred rather than imported at module scope so a host without
    aubio loses the pulse and nothing else — this module is imported
    from the config, and the visualiser does not need it.
    """
    global _aubio, _numpy, _aubio_checked
    if _aubio_checked:
        return _aubio
    _aubio_checked = True
    try:
        import aubio
        import numpy
    except ImportError:
        base = {"base": sys.base_prefix, "platbase": sys.base_prefix}
        for key in ("purelib", "platlib"):
            path = sysconfig.get_path(key, scheme="posix_prefix", vars=base)
            if path and path not in sys.path and os.path.isdir(path):
                sys.path.append(path)
        try:
            import aubio
            import numpy
        except ImportError:
            log.warning("aubio not importable — beat pulse is off "
                        "(install the system `python-aubio` package)")
            return None
    _aubio, _numpy = aubio, numpy
    return aubio


class _PipeReader:
    """One subprocess, read in fixed-size chunks on the loop.

    Both backends want the same five things — spawn lazily, read frames
    of a known size, hand them to a callback, survive the producer
    exiting, and reap the child on the way out — so the lifecycle lives
    here once and each backend supplies only its argv and its handler.
    """

    def __init__(self, name: str, chunk: int,
                 argv: Callable[[], Awaitable[list[str] | None]],
                 on_chunk: Callable[[bytes], None]) -> None:
        self.name = name
        self.chunk = chunk
        self._argv = argv
        self._on_chunk = on_chunk
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("no running loop; %s not started", self.name)
            return
        self._task = loop.create_task(self._run())

    def stop(self) -> None:
        """Drop the process and let `_run` unwind on EOF.

        The task is orphaned rather than cancelled: cancelling would
        unwind straight through the `await proc.wait()` that reaps the
        child, leaving a zombie until the shell exits.
        """
        self._task = None
        proc, self._proc = self._proc, None
        self._terminate(proc)

    @staticmethod
    def _terminate(proc) -> None:
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass

    async def _run(self) -> None:
        task = asyncio.current_task()
        proc = None
        chunks = 0
        try:
            argv = await self._argv()
            if argv is None:
                return
            if shutil.which(argv[0]) is None:
                log.warning("%s not found — %s is off", argv[0], self.name)
                return
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                log.exception("cannot start %s", self.name)
                return
            # Everyone may have unsubscribed while the spawn was in
            # flight, in which case nobody holds this process any more.
            if self._task is not task:
                return
            self._proc = proc
            log.debug("%s started (pid %s)", self.name, proc.pid)
            assert proc.stdout is not None
            while True:
                try:
                    data = await proc.stdout.readexactly(self.chunk)
                except asyncio.IncompleteReadError:
                    break
                chunks += 1
                try:
                    self._on_chunk(data)
                except Exception:
                    log.exception("%s handler raised", self.name)
        finally:
            if proc is not None:
                self._terminate(proc)
                await proc.wait()
                if chunks == 0:
                    log.warning("%s produced no data (rc %s) — is a monitor "
                                "source available?",
                                self.name, proc.returncode)
                else:
                    log.debug("%s stopped after %d chunks", self.name, chunks)
                if self._proc is proc:
                    self._proc = None
            if self._task is task:
                self._task = None


class BeatDetector:
    """Bands and beats, each with its own backend and its own refcount.

    Listener lists are plain lists rather than sets because callbacks are
    bound methods and a widget subscribes and unsubscribes on every
    play/pause — it has to remove exactly what it added.
    """

    def __init__(self) -> None:
        self._bands_listeners: list[Callable[[Bands], None]] = []
        self._beat_listeners: list[Callable[[], None]] = []
        self._tempo = None
        self._cava = _PipeReader("cava", FRAME_BYTES, self._cava_argv,
                                 self._on_cava)
        self._aubio = _PipeReader("aubio", HOP_BYTES, self._parec_argv,
                                  self._on_pcm)

    # ── subscription ────────────────────────────────────────────────────
    def add_bands_listener(self, fn: Callable[[Bands], None]) -> None:
        self._bands_listeners.append(fn)
        if not self._cava.running:
            self._cava.start()

    def remove_bands_listener(self, fn: Callable[[Bands], None]) -> None:
        try:
            self._bands_listeners.remove(fn)
        except ValueError:
            pass
        if not self._bands_listeners:
            self._cava.stop()

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Subscribe to beats — the detector's primary product, with
        the cava bands as the extra."""
        self._beat_listeners.append(fn)
        if not self._aubio.running:
            self._aubio.start()

    def remove_listener(self, fn: Callable[[], None]) -> None:
        try:
            self._beat_listeners.remove(fn)
        except ValueError:
            pass
        if not self._beat_listeners:
            self._aubio.stop()
            self._tempo = None

    def stop(self) -> None:
        """Force both backends down — for shutdown and reload, where the
        widgets are torn down without unsubscribing."""
        self._cava.stop()
        self._aubio.stop()
        self._tempo = None

    # ── cava (bands) ────────────────────────────────────────────────────
    async def _cava_argv(self) -> list[str]:
        return ["cava", "-p", CAVA_CONFIG]

    def _on_cava(self, frame: bytes) -> None:
        bands = _unpack(frame)
        for fn in list(self._bands_listeners):
            try:
                fn(bands)
            except Exception:
                log.exception("bands listener failed")

    # ── aubio (beats) ───────────────────────────────────────────────────
    async def _parec_argv(self) -> list[str] | None:
        aubio = load_aubio()
        if aubio is None:
            return None
        sink = (await proc.run(["pactl", "get-default-sink"])).strip()
        if not sink:
            log.warning("no default sink — beat pulse is off")
            return None
        # Fresh per start: aubio.tempo carries phase and tempo estimates
        # from whatever it last heard, and a track it never finished is
        # not a useful prior for the next one.
        self._tempo = aubio.tempo("default", AUBIO_WIN, AUBIO_HOP,
                                  SAMPLE_RATE)
        return ["parec", f"--device={sink}.monitor", "--format=s16le",
                f"--rate={SAMPLE_RATE}", "--channels=1", "--latency-msec=50"]

    def _on_pcm(self, data: bytes) -> None:
        tempo, np = self._tempo, _numpy
        if tempo is None or np is None:
            return
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if tempo(samples) and tempo.get_confidence() >= AUBIO_MIN_CONFIDENCE:
            for fn in list(self._beat_listeners):
                try:
                    fn()
                except Exception:
                    log.exception("beat listener failed")


_detector: BeatDetector | None = None


def get_detector() -> BeatDetector:
    global _detector
    if _detector is None:
        _detector = BeatDetector()
    return _detector
