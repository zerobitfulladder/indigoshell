"""The agent's progress bars, end to end without the agent: the shell's
listener (agent/progress.py) and the wrapper behind the shims
(agent/watch.py), driven with the real programs.

    .venv/bin/python tests/progress.py

Each command runs the way Claude Code runs one: a process whose command
line says `--output-format stream-json` (a stand-in for the CLI, started
by this process as the SDK starts the real one) runs `bash -c "... eval
'<command>' ..."` with $INDIGO_PROGRESS and the shims on its PATH. The
checks:

- every program's progress arrives as numbers, and the job is the one
  Bash call (`under == "cli"`, matched to its command), not the program;
- what the program prints reaches the caller as a terminal would have
  left it: the final progress line, not hundreds of rewrites;
- data on stdout passes through untouched, exit codes come through,
  and without $INDIGO_PROGRESS the shim just runs the program;
- a skill script's own stages and numbers outrank the ffmpeg it runs,
  whose numbers fill in a stage the script has none for.

Needs ffmpeg, curl, rsync and yt-dlp; serves its media on 127.0.0.1.
Nothing here touches the running shell: its own socket and shims live
in a temporary directory.
"""

import asyncio
import functools
import hashlib
import http.server
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indigoshell.agent import progress  # noqa: E402
from indigoshell.agent.progress import Jobs, runs_command  # noqa: E402

TMP = tempfile.mkdtemp(prefix="progress-test-")
MEDIA = os.path.join(TMP, "media")
OUT = os.path.join(TMP, "out")
os.makedirs(MEDIA)
os.makedirs(OUT)

# A stand-in for Claude Code: started by this process, running one Bash
# call the way the real CLI does.
FAKE_CLI = textwrap.dedent("""\
    import subprocess, sys
    cmd = sys.argv[1]
    q = "'" + cmd.replace("'", "'\\"'\\"'") + "'"
    line = "source /dev/null && eval " + q + " < /dev/null && pwd -P >| /dev/null"
    r = subprocess.run(["bash", "-c", line], capture_output=True)
    sys.stdout.buffer.write(r.stdout)
    sys.stderr.buffer.write(r.stderr)
    sys.exit(r.returncode)
""")

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + what)
    if not ok:
        failures.append(what)


def serve(folder: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_Quiet, directory=folder)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.handle_error = lambda *_: None     # clients that hang up early
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def run(jobs: Jobs, command: str, *, env_extra=None, cli=True):
    """Run `command` as an agent's Bash call; return (rc, out, err, job
    events seen)."""
    events: list[tuple[str, progress.Job, dict]] = []
    mine: set[int] = set()

    def seen(event, job):
        # Only this command's jobs: an earlier one's "show" or "drop"
        # can still come in.
        if event == "new":
            mine.add(job.id)
        if job.id not in mine:
            return
        events.append((event, job, {"done": job.done, "total": job.total,
                                    "unit": job.unit, "stage": job.stage,
                                    "label": job.label, "text": job.text}))
    jobs._listeners = [seen]
    env = dict(os.environ)
    env.update(jobs.env())
    env.update(env_extra or {})
    argv = [sys.executable, "-c", FAKE_CLI, command]
    if cli:
        argv += ["--output-format", "stream-json"]
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    await asyncio.sleep(0.3)        # the last datagrams, the pidfd
    return proc.returncode, out, err, events


def numbers(events) -> list[dict]:
    return [v for _e, _j, v in events if v["done"] is not None]


async def main() -> int:
    jobs = Jobs(path=os.path.join(TMP, "p.sock"),
                shims=os.path.join(TMP, "shims"))
    jobs.claim = lambda job: "claimed" if job.under == "cli" else ""
    jobs.start()
    shims = sorted(os.listdir(jobs.shims))
    print(f"shims: {', '.join(shims)}")
    check(set(shims) == set(progress.TOOLS), "a shim for every tool, all installed")

    # Media: 20 s of video, a few MB, made quickly.
    video = os.path.join(MEDIA, "clip.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=25:duration=20", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=20", "-c:v", "libx264",
                    "-b:v", "2M", "-c:a", "aac", "-shortest", video],
                   check=True)
    blob = os.path.join(MEDIA, "blob.bin")
    with open(blob, "wb") as f:
        f.write(os.urandom(6 * 1024 * 1024))
    httpd, base = serve(MEDIA)

    # ── ffmpeg ──────────────────────────────────────────────────────────
    print("ffmpeg")
    cmd = (f"ffmpeg -y -readrate 10 -i {shlex.quote(video)} -c:a libmp3lame "
           f"{shlex.quote(os.path.join(OUT, 'clip.mp3'))}")
    t0 = time.monotonic()
    rc, out, err, ev = await run(jobs, cmd)
    nums = numbers(ev)
    job = ev[0][1] if ev else None
    check(rc == 0, f"exit 0 (took {time.monotonic() - t0:.1f}s)")
    check(len(nums) >= 5, f"{len(nums)} updates")
    check(bool(nums) and nums[-1]["unit"] == "s"
          and abs((nums[-1]["total"] or 0) - 20) < 0.5,
          "seconds of media, out of the input's 20 s")
    check(bool(nums) and nums[-1]["done"] >= 19, "reaches the end")
    check(job is not None and job.under == "cli" and job.step == "claimed",
          "one job for the Bash call, claimed")
    check(job is not None and runs_command(job.cmdline, cmd),
          "its command line names the command")
    check(job is not None and job.label == "clip.mp3", "named for its output")
    check(err.count(b"time=") == 1, f"one progress line passed on "
          f"({err.count(b'time=')}), the final one")

    # ── curl ────────────────────────────────────────────────────────────
    print("curl")
    dest = os.path.join(OUT, "blob.bin")
    cmd = f"curl --limit-rate 3M -o {shlex.quote(dest)} {base}/blob.bin"
    rc, out, err, ev = await run(jobs, cmd)
    nums = numbers(ev)
    check(rc == 0, "exit 0")
    check(len(nums) >= 3, f"{len(nums)} updates")
    check(bool(nums) and nums[-1]["unit"] == "B"
          and abs((nums[-1]["total"] or 0) - 6 * 1024 * 1024) < 64 * 1024,
          "bytes, out of the file's size")
    rows = [ln for ln in err.replace(b"\r", b"\n").splitlines()
            if ln.strip()[:1].isdigit()]
    check(len(rows) == 1, f"one meter row passed on ({len(rows)})")
    rc, out, err, ev = await run(jobs, f"curl -s {base}/blob.bin | sha256sum")
    want = hashlib.sha256(open(blob, "rb").read()).hexdigest()
    check(out.split()[0].decode() == want, "stdout data untouched")
    rc, out, err, ev = await run(jobs, "curl -s http://127.0.0.1:9/")
    check(rc == 7, f"exit code passed on (curl: {rc}, want 7)")

    # ── rsync ───────────────────────────────────────────────────────────
    print("rsync")
    cmd = (f"rsync --progress --bwlimit=3000 {shlex.quote(blob)} "
           f"{shlex.quote(os.path.join(OUT, 'rs.bin'))}")
    rc, out, err, ev = await run(jobs, cmd)
    nums = numbers(ev)
    check(rc == 0, "exit 0")
    check(len(nums) >= 3, f"{len(nums)} updates")
    check(bool(nums) and nums[-1]["unit"] == "B"
          and (nums[-1]["done"] or 0) >= 6 * 1024 * 1024 * 0.95,
          "bytes, to the end")
    check(out.count(b"%") == 1, f"one progress line passed on "
          f"({out.count(b'%')})")

    # ── yt-dlp ──────────────────────────────────────────────────────────
    print("yt-dlp")
    cmd = (f"yt-dlp --limit-rate 2M -o {shlex.quote(os.path.join(OUT, 'yt.%(ext)s'))} "
           f"{base}/clip.mp4")
    rc, out, err, ev = await run(jobs, cmd)
    nums = numbers(ev)
    check(rc == 0, f"exit 0 {err.decode(errors='replace')[-300:] if rc else ''}")
    check(len(nums) >= 2, f"{len(nums)} updates")
    check(bool(nums) and nums[-1]["unit"] == "B"
          and nums[-1]["done"] == nums[-1]["total"], "bytes, to 100%")
    check(out.count(b"[download]") <= 4,
          f"progress lines collapsed ({out.count(b'[download]')} left)")

    # ── a skill script, with ffmpeg inside one stage ────────────────────
    print("skill")
    script = os.path.join(TMP, "skill.py")
    with open(script, "w") as f:
        f.write(textwrap.dedent(f"""\
            import json, os, socket, subprocess, time
            def report(**fields):
                path = os.environ.get("INDIGO_PROGRESS")
                if path:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                    s.sendto(json.dumps(fields).encode(), path)
            report(label="transcribe", stages=["audio", "transcribe"])
            report(stage="audio")
            subprocess.run(["ffmpeg", "-y", "-readrate", "10", "-i",
                            {video!r}, "-vn", {os.path.join(OUT, "s.wav")!r}],
                           capture_output=True, check=True)
            report(stage="transcribe")
            for i in range(1, 11):
                report(done=i, total=10, unit="")
                time.sleep(0.05)
        """))
    rc, out, err, ev = await run(jobs, f"python3 {shlex.quote(script)}")
    check(rc == 0, "exit 0")
    roots = {j.root for _e, j, _v in ev}
    check(len(roots) == 1, f"one job for the script and its ffmpeg ({len(roots)})")
    audio = [v for v in numbers(ev) if v["stage"] == "audio"]
    tx = [v for v in numbers(ev) if v["stage"] == "transcribe"]
    check(bool(audio) and audio[-1]["unit"] == "s",
          "ffmpeg's numbers fill the audio stage")
    check(bool(tx) and tx[-1]["unit"] == "" and tx[-1]["done"] == 10,
          "the script's own numbers in its transcribe stage")
    check(all(v["label"] == "transcribe" for _e, _j, v in ev[1:]),
          "the script's label outranks ffmpeg's")

    # ── without the shell ───────────────────────────────────────────────
    print("no listener")
    rc, out, err, ev = await run(
        jobs, f"ffmpeg -v error -y -i {shlex.quote(video)} -t 1 "
              f"{shlex.quote(os.path.join(OUT, 'x.wav'))}",
        env_extra={"INDIGO_PROGRESS": ""})
    check(rc == 0 and not ev, "the shim just runs the program")

    # ── not under Claude Code ───────────────────────────────────────────
    rc, out, err, ev = await run(jobs, cmd.replace("yt.%(ext)s", "yt2.%(ext)s"),
                                 cli=False)
    job = ev[0][1] if ev else None
    check(job is not None and job.under == "shell",
          "a helper of the shell's own is its own job")

    # ── shim overhead ───────────────────────────────────────────────────
    env = dict(os.environ, **jobs.env())
    t0 = time.monotonic()
    for _ in range(5):
        subprocess.run(["curl", "--version"], env=env, capture_output=True)
    shim = (time.monotonic() - t0) / 5
    t0 = time.monotonic()
    for _ in range(5):
        subprocess.run(["curl", "--version"], capture_output=True)
    bare = (time.monotonic() - t0) / 5
    print(f"  curl --version: {bare * 1000:.0f} ms bare, "
          f"{shim * 1000:.0f} ms through the shim")
    check(shim - bare < 0.08, "the shim costs under 80 ms a call")

    httpd.shutdown()
    jobs.stop()
    print("FAILED: " + "; ".join(failures) if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
