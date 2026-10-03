"""Cut a take into the showcase.

    edit.py ~/Videos/indigoshell-showcase/take-YYYYmmdd-HHMMSS.json
    edit.py <take.json> --punch 41.2:44.0:bl     # add or override zooms

Reads the take's markers and writes, beside the video:

    <take>-showcase.mp4        the recording's size, 60fps master
    <take>-showcase-1080.mp4   1920x1080 60fps for upload

The cut, all from markers rather than hand-entered times:

  * starts just before the cold open (the recorder's warm-up is dropped)
    and ends 2.4s after the power menu closes, fading both picture and
    sound over the last two seconds;
  * plays VS Code's start-up at 2.5x — the one wait in the take;
  * ends on the widget reel, when `reel.py render` has made one: the
    desktop footage stops where the reel was cut in, the reel's frames
    follow, and the sound stays the take's own — the song carries on
    across the cut and fades with the reel's last two seconds;
  * zooms 1.9x into the bar three times, because at 1080p the whole bar
    is 31px tall: on the music starting (bottom right: title, visualiser,
    the song's toast), on the first lyric line that lands during that
    hold (bottom left), and on the volume scroll (bottom right). Each
    zoom eases in and out over 0.35s; between zooms the frame is whole.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ZOOM = 1.9
RAMP = 0.35
FAST = 2.5
FPS = 60
HEAD, TAIL = 0.5, 2.4
FADE = 2.0


def first(markers: list[dict], name: str) -> float | None:
    return next((m["t"] for m in markers if m["name"] == name), None)


def probe(video: Path) -> tuple[float, int, int]:
    """Duration and frame size of the recording."""
    def ask(entries: str, stream: bool) -> str:
        cmd = ["ffprobe", "-v", "error", "-show_entries", entries,
               "-of", "csv=p=0", str(video)]
        if stream:
            cmd[3:3] = ["-select_streams", "v:0"]
        return subprocess.run(cmd, capture_output=True, text=True,
                              check=True).stdout.strip()
    w, h = ask("stream=width,height", True).split(",")
    return float(ask("format=duration", False)), int(w), int(h)


class Timeline:
    """Take time -> output time, through the trims and the speed-up."""

    def __init__(self, start: float, end: float,
                 fast: tuple[float, float] | None) -> None:
        self.start, self.end, self.fast = start, end, fast

    def segments(self) -> list[tuple[float, float, float]]:
        if self.fast is None:
            return [(self.start, self.end, 1.0)]
        a, b = self.fast
        return [(self.start, a, 1.0), (a, b, FAST), (b, self.end, 1.0)]

    def map(self, t: float) -> float:
        out = 0.0
        for s, e, speed in self.segments():
            if t <= s:
                break
            out += (min(t, e) - s) / speed
        return out

    @property
    def length(self) -> float:
        return self.map(self.end)


def punches(markers: list[dict]) -> list[tuple[float, float, str]]:
    """(start, end, anchor) in take time; anchor "br" or "bl"."""
    music, close = first(markers, "music"), first(markers, "close_vscode")
    volume = first(markers, "volume")
    out = []
    if music is not None:
        hold_end = (close or music + 7.5) - 0.3
        lyric = next((m["t"] for m in markers if m["name"] == "lyric"
                      and music + 0.3 <= m["t"] <= hold_end - 2.0), None)
        if lyric is None:
            out.append((music + 0.6, hold_end, "br"))
        elif lyric - music < 2.5:
            # A line lands right on the drop: show it first, then the
            # title and visualiser.
            out.append((lyric - 0.25, lyric + 2.6, "bl"))
            out.append((lyric + 3.3, hold_end, "br"))
        else:
            out.append((music + 0.6, lyric - 0.9, "br"))
            out.append((lyric - 0.25, min(lyric + 2.6, hold_end), "bl"))
    if volume is not None:
        out.append((volume - 0.1, volume + 2.6, "br"))
    return [p for p in out if p[1] - p[0] > 2 * RAMP]


def zoom_exprs(windows: list[tuple[float, float, str]]) -> tuple[str, str, str]:
    """zoompan's z, x and y. Each window is a raised-cosine bump in time;
    the anchor only switches while zoomed out, so it never jumps."""
    t = f"(on/{FPS})"

    def bump(s: float, e: float) -> str:
        rise = f"(1-cos(PI*clip(({t}-{s:.3f})/{RAMP},0,1)))/2"
        fall = f"(1-cos(PI*clip(({e:.3f}-{t})/{RAMP},0,1)))/2"
        return f"{rise}*{fall}"

    if not windows:
        return "1", "0", "0"
    z = f"1+{ZOOM - 1:.3f}*(" + "+".join(bump(s, e) for s, e, _ in windows) + ")"
    right = "+".join(f"between({t},{s:.3f},{e:.3f})"
                     for s, e, a in windows if a == "br") or "0"
    x = f"if(gt({right},0),iw-iw/zoom,0)"
    y = "ih-ih/zoom"
    return z, x, y


def build(take: Path, extra: list[str]) -> None:
    meta = json.loads(take.read_text())
    markers, video = meta["markers"], Path(meta["video"])
    opened, ended = first(markers, "open"), first(markers, "end")
    if opened is None or ended is None:
        sys.exit("edit: the take has no open/end markers — was it aborted?")
    length, width, height = probe(video)
    # Markers count from the recorder's launch, the video from its first
    # frame, a second or so later. The take marks the moment it stops
    # the recorder — the video's last frame — which measures the gap.
    stop = first(markers, "stop")
    offset = 0.0
    if stop is not None:
        offset = stop - length
        markers = [{**m, "t": m["t"] - offset} for m in markers]
        opened, ended = opened - offset, ended - offset
        print(f"edit: markers shifted {offset:.2f}s onto the video")
    start = max(0.0, opened - HEAD)
    end = min(length, ended + TAIL)
    reel_meta = take.with_name(take.stem + "-reel.json")
    reel = json.loads(reel_meta.read_text()) if reel_meta.exists() else None
    if reel is not None:
        end = reel["audio_at"]      # the desktop ends where the reel begins

    launch, ready = first(markers, "vscode_launch"), first(markers, "vscode_ready")
    fast = (launch + 0.3, ready) if launch and ready and ready - launch > 1.5 else None
    line = Timeline(start, end, fast)

    windows = punches(markers)
    for spec in extra:
        s, e, anchor = spec.split(":")
        windows.append((float(s) - offset, float(e) - offset, anchor))
    out_windows = [(line.map(s), line.map(e), a) for s, e, a in windows]
    z, x, y = zoom_exprs(out_windows)
    total = line.length

    parts, labels = [], []
    for i, (s, e, speed) in enumerate(line.segments()):
        tempo = "atempo=2.0,atempo=1.25" if speed == FAST else "anull"
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=(PTS-STARTPTS)/{speed}[v{i}]")
        parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS,{tempo}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    if reel is not None:
        # The reel's pictures over the take's own sound: the same seconds
        # of audio the reel was cut to, so its beats land on the music.
        i, a0, dur = len(labels), reel["audio_at"], reel["duration"]
        parts.append(f"[1:v]trim=0:{dur:.3f},setpts=PTS-STARTPTS,fps={FPS}[v{i}]")
        parts.append(f"[0:a]atrim={a0:.3f}:{a0 + dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
        total += dur
    n = len(labels)
    parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[cv][ca]")
    parts.append(f"[cv]fps={FPS},zoompan=z='{z}':x='{x}':y='{y}':d=1"
                 f":s={width}x{height}:fps={FPS},"
                 f"fade=t=out:st={total - FADE:.3f}:d={FADE}[vout]")
    # Silence until the take's seek: the instant of intro spotify_player
    # played before it jumped to the chorus never reaches the video.
    seek = first(markers, "seek")
    mute = ""
    if seek is not None:
        s_out = line.map(seek + 0.15)
        mute = (f"volume=volume=0:enable='lt(t,{s_out:.3f})',"
                f"afade=t=in:st={s_out:.3f}:d=0.06,")
    parts.append(f"[ca]{mute}afade=t=out:st={total - FADE:.3f}:d={FADE}[aout]")

    master = video.with_name(video.stem + "-showcase.mp4")
    small = video.with_name(video.stem + "-showcase-1080.mp4")
    reel_note = f" + reel {reel['duration']:.1f}s" if reel else ""
    print(f"edit: {total:.1f}s — take {start:.1f}-{end:.1f}{reel_note}; "
          f"zooms at {[(round(s, 1), round(e, 1), a) for s, e, a in out_windows]}")
    inputs = ["-i", str(video)] + (["-i", reel["video"]] if reel else [])
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
                    "-y", *inputs, "-filter_complex", ";".join(parts),
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(master)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
                    "-y", "-i", str(master),
                    "-vf", "scale=1920:1080:flags=lanczos",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "copy",
                    "-movflags", "+faststart", str(small)], check=True)
    print(f"master: {master}\n1080p:  {small}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("take", type=Path)
    parser.add_argument("--punch", action="append", default=[],
                        metavar="START:END:br|bl", help="extra zoom, take time")
    args = parser.parse_args()
    build(args.take, args.punch)
