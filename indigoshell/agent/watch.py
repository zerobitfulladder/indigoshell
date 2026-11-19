"""The reporting end of the agent's progress bars: standard library only,
so a shim or a skill script can use it without the shell's packages.

`Reporter` sends progress to the shell: one JSON object per datagram to
the socket named by $INDIGO_PROGRESS (the fields are listed in
agent/progress.py). With no shell listening it does nothing, and it
never blocks or raises: a progress report must not break the job it
reports on.

Run as a script, it is the wrapper behind the shims at the front of the
agent's PATH (agent/progress.py writes them):

    python -I -S watch.py /usr/bin/yt-dlp ARGS...

It runs the real program, reads the output streams that carry its
progress, reports what it finds and passes the output on the way a
terminal would have left it. A line the program kept rewriting with \\r
comes through once, as it ended, instead of as hundreds of lines in the
agent's context. A stream that can carry data (curl's or ffmpeg's
stdout, yt-dlp's with `-o -`) is never read: the program writes it
straight through. Without $INDIGO_PROGRESS it just runs the program.

The agent's commands never have a terminal (the Bash tool runs them on
pipes), so the program sees the same pipes it would have without the
wrapper, and prints the same thing.
"""

import json
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time

ENV = "INDIGO_PROGRESS"
# Updates that only move the numbers or the status line go out at most
# this often; a line held back longer than this is passed on raw.
SEND_EVERY_S = 0.1
LINE_MAX = 1 << 20
# What can be thinned: a newer one says the same thing, only later.
_THIN = {"done", "total", "unit", "text"}


class Reporter:
    """Progress to the shell, as `send(stage=..., done=..., total=...)`.

    Updates that only move the numbers or change the status line are
    thinned to one per SEND_EVERY_S, keeping the newest; a new label,
    stage or exit goes out at once, carrying anything held back.
    Call `flush()` before exiting so the last numbers arrive."""

    def __init__(self, path: str | None = None) -> None:
        self.path = os.environ.get(ENV, "") if path is None else path
        self._sock: socket.socket | None = None
        self._last = 0.0
        self._pending: dict | None = None

    def __bool__(self) -> bool:
        return bool(self.path)

    def send(self, **msg) -> None:
        if not self.path:
            return
        now = time.monotonic()
        if set(msg) <= _THIN:
            if self._pending is not None:
                msg = {**self._pending, **msg}
            if now - self._last < SEND_EVERY_S:
                self._pending = msg
                return
        elif self._pending is not None:
            msg = {**self._pending, **msg}
        self._pending = None
        self._last = now
        self._write(msg)

    def due(self) -> None:
        """Send thinned numbers whose time has come."""
        if self._pending is not None \
                and time.monotonic() - self._last >= SEND_EVERY_S:
            self.flush()

    def flush(self) -> None:
        if self._pending is not None:
            msg, self._pending = self._pending, None
            self._last = time.monotonic()
            self._write(msg)

    def _write(self, msg: dict) -> None:
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            self._sock.sendto(json.dumps(msg).encode(), self.path)
        except OSError:
            pass


# ── reading numbers ─────────────────────────────────────────────────────
_SIZE = re.compile(r"([\d.]+)\s*([kKMGTP]?)(i?)([bB]?)")
_POWERS = {"": 0, "k": 1, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}


def size(value: str) -> float | None:
    """Bytes in "50.12MiB", "1234k", "3.1MB", "123". A bare letter
    counts in 1024s, as curl and rsync print them; "MB" in 1000s."""
    m = _SIZE.fullmatch(value.strip().replace(",", ""))
    if not m:
        return None
    try:
        n = float(m[1])
    except ValueError:
        return None
    base = 1000 if m[4] and not m[3] else 1024
    return n * base ** _POWERS[m[2]]


def seconds(value: str) -> float | None:
    """Seconds in "01:02:03.5", "02:03" or "93.5"."""
    try:
        parts = [float(p) for p in value.strip().split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


# ── what each program prints ────────────────────────────────────────────
# Each parser takes one line as the program left it and its own state,
# and returns (message or None, whether the line is only progress). A run
# of progress-only lines is passed on as its last line.

_YT_PCT = re.compile(r"^\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\w+)")
_YT_BYTES = re.compile(r"^\[download\]\s+([\d.]+\s*[KMGTP]?i?B)\s+at\s")
_YT_DEST = re.compile(r"^\[download\] Destination: (.+)$")
_YT_ITEM = re.compile(r"^\[download\] Downloading (?:item|video) (\d+) of (\d+)")
_YT_STEPS = {"Merger": "merging", "ExtractAudio": "extracting audio",
             "VideoConvertor": "converting", "VideoRemuxer": "remuxing",
             "FixupM3u8": "fixing up", "EmbedSubtitle": "embedding subtitles",
             "EmbedThumbnail": "embedding the thumbnail",
             "SponsorBlock": "cutting sponsors", "ModifyChapters": "cutting"}
_YT_STEP = re.compile(r"^\[(\w+)\]")
_YT_DATA = {"-j", "-J", "--dump-json", "--dump-single-json", "--print",
            "-O", "--print-json", "-g", "--get-url", "-e", "--get-title",
            "--get-id", "--get-filename", "--get-format", "--get-duration",
            "--get-description", "--get-thumbnail", "-F", "--list-formats"}


def _ytdlp_streams(args: list[str]) -> set[str]:
    # With `-o -` the media goes to stdout and the progress to stderr;
    # the print modes put JSON or names on stdout for the agent to read.
    to_stdout = any(a in ("-o-", "--output=-") for a in args) or any(
        a in ("-o", "--output") and b == "-" for a, b in zip(args, args[1:]))
    return {"err"} if to_stdout or _YT_DATA & set(args) else {"out", "err"}


def ytdlp(line: str, st: dict):
    m = _YT_PCT.match(line)
    if m:
        total = size(m[2])
        if total:
            return {"done": total * float(m[1]) / 100, "total": total,
                    "unit": "B"}, True
    m = _YT_BYTES.match(line)
    if m:
        return {"done": size(m[1]), "total": None, "unit": "B"}, True
    m = _YT_DEST.match(line)
    if m:
        name = re.sub(r"\.f\d+(?=\.\w+$)", "", os.path.basename(m[1]))
        return {"label": name, "done": None, "total": None, "text": ""}, False
    m = _YT_ITEM.match(line)
    if m:
        return {"text": f"item {m[1]} of {m[2]}"}, False
    m = _YT_STEP.match(line)
    if m and m[1] in _YT_STEPS:
        return {"text": _YT_STEPS[m[1]], "done": None, "total": None}, False
    return None, False


_FF_DUR = re.compile(r"^\s*Duration:\s*(\d+:\d+:[\d.]+)")
_FF_TIME = re.compile(r"\btime=\s*(-?\d+:\d+:[\d.]+)")


def _ffmpeg_setup(args: list[str], st: dict) -> None:
    # An output limit (-t) is what the run will reach, not the input's
    # length; the output file names the job.
    for a, b in zip(args, args[1:]):
        if a == "-t":
            st["limit"] = seconds(b)
    if args and not args[-1].startswith("-"):
        st["label"] = os.path.basename(args[-1])


def ffmpeg(line: str, st: dict):
    m = _FF_DUR.match(line)
    if m and "total" not in st:
        st["total"] = seconds(m[1])
        return None, False
    m = _FF_TIME.search(line)
    if m and ("size=" in line or "frame=" in line):
        done = max(0.0, seconds(m[1]) or 0.0)
        msg = {"done": done, "unit": "s"}
        total = st.get("limit") or st.get("total")
        if total:
            msg["total"] = total
        return msg, True
    return None, False


# % Total  % Received  % Xferd  then speeds and times; curl 8.x leaves a
# time column blank until it is known, so only the first six are sure.
_CURL_ROW = re.compile(r"^\s*(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\S+)"
                       r"\s+\S+\s+\S+")
_CURL_BAR = re.compile(r"^[#=O\-\s]*?\s*(\d+(?:\.\d+)?)%\s*$")


def curl(line: str, st: dict):
    m = _CURL_ROW.match(line)
    if m:
        total, got, sent = size(m[2]), size(m[4]), size(m[6])
        done = got if got or not sent else sent      # a download, or an upload
        return {"done": done, "total": total or None, "unit": "B"}, True
    m = _CURL_BAR.match(line)
    if m and "#" in line:
        return {"done": float(m[1]), "total": 100, "unit": "%"}, True
    return None, False


# "1,081,344B  36%  1,026,003B/s  0:00:01" (3.5; older ones drop the B)
_RS_ROW = re.compile(r"^\s*([\d,.]+\s*[KMGT]?i?B?)\s+(\d+)%\s+\S+/s\s+\d+:\d+:\d+")
_RS_INFO = ("sending ", "sent ", "total size", "receiving ", "building ",
            "created ", "deleting ", "Number of ", "delta-transmission")


def rsync(line: str, st: dict):
    m = _RS_ROW.match(line)
    if m:
        done, pct = size(m[1]), int(m[2])
        total = done * 100 / pct if done and pct else None
        return {"done": done, "total": total, "unit": "B"}, True
    name = line.strip()
    if name and not line.startswith(" ") and not name.startswith(_RS_INFO) \
            and not name.endswith("/"):
        return {"text": os.path.basename(name)}, False
    return None, False


_PCT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%")


def any_percent(line: str):
    """A percentage on a line the program kept rewriting: progress, in
    nearly any program that draws a bar."""
    m = _PCT.search(line)
    if m and float(m[1]) <= 100:
        return {"done": float(m[1]), "total": 100, "unit": "%"}
    return None


class Profile:
    def __init__(self, parse, streams=lambda args: {"err"},
                 setup=lambda args, st: None) -> None:
        self.parse, self.streams, self.setup = parse, streams, setup


# The programs that get a shim (progress.TOOLS names the same ones).
PROFILES = {
    "yt-dlp": Profile(ytdlp, _ytdlp_streams),
    "ffmpeg": Profile(ffmpeg, setup=_ffmpeg_setup),
    "curl": Profile(curl),
    "rsync": Profile(rsync, lambda args: {"out", "err"}),
}


# ── passing output on ───────────────────────────────────────────────────
class Stream:
    """One watched stream of the program: its lines passed on as a
    terminal would show them, its progress parsed out on the way."""

    def __init__(self, fd: int, parse, st: dict, rep: Reporter) -> None:
        self.fd = fd
        self.parse = parse
        self.st = st
        self.rep = rep
        self.buf = b""
        self.held: bytes | None = None      # the last of a progress run
        self.open = True                    # whoever reads us still is

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line, self.buf = self.buf[:i], self.buf[i + 1:]
            self._line(line)
        # A line still being rewritten: report its newest finished state
        # now, and keep only that and what follows it.
        if len(self.buf) > LINE_MAX:        # not text after all
            self._release()
            self._out(self.buf)
            self.buf = b""
            return
        cut = self.buf.rfind(b"\r")
        if cut > 0:
            prev = self.buf.rfind(b"\r", 0, cut) + 1
            seg = self.buf[prev:cut].rstrip(b" ")
            if seg:
                self._report(seg.decode(errors="replace"), transient=True)
            self.buf = self.buf[max(0, prev - 1):]

    def close(self) -> None:
        if self.buf:
            self._line(self.buf)
            self.buf = b""
        self._release()

    def _line(self, raw: bytes) -> None:
        parts = [p.rstrip(b" ") for p in raw.split(b"\r")]
        shown = next((p for p in reversed(parts) if p), b"")
        rewritten = len([p for p in parts if p]) > 1
        progress = self._report(shown.decode(errors="replace"),
                                transient=rewritten)
        if progress or (rewritten and self._percent_only(shown)):
            self.held = shown
            return
        self._release()
        self._out(shown + b"\n")

    def _percent_only(self, line: bytes) -> bool:
        return any_percent(line.decode(errors="replace")) is not None

    def _report(self, line: str, transient: bool) -> bool:
        msg, progress = self.parse(line, self.st)
        if msg is None and transient:
            msg = any_percent(line)
        if msg is not None:
            if not self.st.get("labeled") and "label" not in msg:
                msg["label"] = self.st.get("label") or self.st["tool"]
            self.st["labeled"] = True
            self.rep.send(**msg)
        return progress

    def _release(self) -> None:
        if self.held is not None:
            held, self.held = self.held, None
            self._out(held + b"\n")

    def _out(self, data: bytes) -> None:
        while data and self.open:
            try:
                n = os.write(self.fd, data)
            except BrokenPipeError:
                self.open = False       # nobody reads: stop passing on
                return
            except InterruptedError:
                continue
            data = data[n:]


def run(real: str, args: list[str]) -> int:
    path = os.environ.get(ENV)
    tool = os.path.basename(real)
    profile = PROFILES.get(tool)
    if not path or profile is None:
        os.execv(real, [tool, *args])
    st: dict = {"tool": tool}
    profile.setup(args, st)
    watch = profile.streams(args)

    # A signal sent to this process alone goes on to the program. One
    # sent to the whole group (Ctrl+C, the agent stopping a command)
    # reaches the program by itself, so the wrapper does not pass Ctrl+C
    # on a second time: ffmpeg counts them and hard-exits. Handlers, not
    # SIG_IGN: the program must start with its signals at their defaults.
    started: list[subprocess.Popen] = []

    def forward(sig, _frame):
        for child in started:
            try:
                child.send_signal(sig)
            except OSError:
                pass
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    for sig in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(sig, lambda *_: None)
    try:
        child = subprocess.Popen(
            [tool, *args], executable=real,
            stdout=subprocess.PIPE if "out" in watch else None,
            stderr=subprocess.PIPE if "err" in watch else None)
    except OSError as e:
        print(f"{tool}: {e}", file=sys.stderr)
        return 127
    started.append(child)

    rep = Reporter(path)
    sel = selectors.DefaultSelector()
    for pipe, fd in ((child.stdout, 1), (child.stderr, 2)):
        if pipe is not None:
            sel.register(pipe, selectors.EVENT_READ,
                         Stream(fd, profile.parse, st, rep))
    while sel.get_map():
        for key, _ in sel.select(timeout=SEND_EVERY_S):
            stream = key.data
            try:
                data = os.read(key.fd, 65536)
            except InterruptedError:
                continue
            if not data:
                stream.close()
                sel.unregister(key.fileobj)
                key.fileobj.close()
                continue
            stream.feed(data)
            if not stream.open:
                # The reader went away: let the program find out, as it
                # would have without us in between.
                sel.unregister(key.fileobj)
                key.fileobj.close()
        rep.due()
    code = child.wait()
    rep.flush()
    if st.get("labeled"):
        rep.send(exit=code)
    if code < 0:                     # died of a signal: so do we
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
        return 128 - code
    return code


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: watch.py /path/to/program [args...]", file=sys.stderr)
        return 2
    return run(argv[1], argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
