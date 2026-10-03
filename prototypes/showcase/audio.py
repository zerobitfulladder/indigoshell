"""Music analysis for the reel: visualiser bands and beats from a recording.

The reel's media card should move to the music the viewer hears under
it, and that music is the take's own audio. So the bands are computed
from the same seconds of the same file, the way cava does it — an FFT
per frame, log-spaced bands, automatic gain, fast attack and slower
fall — and the beats come from aubio's tempo tracker on the same
samples.
"""

import subprocess

import numpy as np

RATE = 44100


def samples(path: str, start: float, duration: float) -> np.ndarray:
    """Mono float32 samples of `path` from `start` for `duration` s."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def bands(pcm: np.ndarray, fps: int, count: int, *, low: float = 50.0,
          high: float = 10000.0, window: int = 4096) -> np.ndarray:
    """(frames, count) array of band levels in 0..1, one row per video
    frame."""
    hop = RATE / fps
    frames = int(len(pcm) / hop)
    edges = np.geomspace(low, high, count + 1)
    freqs = np.fft.rfftfreq(window, 1 / RATE)
    which = [np.flatnonzero((freqs >= a) & (freqs < b)) for a, b in zip(edges, edges[1:])]
    taper = np.hanning(window).astype(np.float32)
    padded = np.concatenate([np.zeros(window, np.float32), pcm])
    raw = np.zeros((frames, count), np.float32)
    for f in range(frames):
        end = window + int(f * hop)
        spectrum = np.abs(np.fft.rfft(padded[end - window:end] * taper))
        raw[f] = [spectrum[i].mean() if len(i) else 0.0 for i in which]
    # Log loudness, then gain set from the loud end of the passage so
    # quiet verses still move and choruses do not pin every band.
    level = np.log1p(raw * 40)
    level /= max(np.percentile(level, 98), 1e-6)
    level = np.clip(level, 0.0, 1.0)
    # Attack at once, fall at ~3 units a second: cava's gravity, roughly.
    out = np.empty_like(level)
    fall = 3.0 / fps
    out[0] = level[0]
    for f in range(1, frames):
        out[f] = np.maximum(level[f], out[f - 1] - fall)
    return out


def beats(pcm: np.ndarray) -> list[float]:
    """Beat times, seconds from the start of `pcm`."""
    import aubio
    hop, win = 512, 1024
    tempo = aubio.tempo("default", win, hop, RATE)
    found = []
    for i in range(0, len(pcm) - hop, hop):
        if tempo(pcm[i:i + hop].astype(np.float32)):
            found.append(tempo.get_last_s())
    return found
