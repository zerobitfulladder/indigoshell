"""Progress bars on the call, live: real reporting processes, the real
listener, the real plates, on a call opened twice.

    .venv/bin/python tests/call_progress.py

Each command runs under a stand-in for Claude Code (a child of this
process whose command line says `--output-format stream-json`, running
`bash -c "... eval '<command>' ..."`), so a job is matched to its step
the way it is live. Per open:

- a step's bar shows under it while its command reports, and what it
  came to stays on the step when it ends;
- a command left running after the turn has ended shows on its own
  (no step to sit under) and keeps the plate animating with nothing
  else moving;
- the hung-up status plate draws the same job in its second line;
- once every job is over, nothing is left on screen (`_live.stale`).
"""

import asyncio
import os
import sys
import tempfile
import textwrap

import _live

from indigoshell import config_default as config
from indigoshell.agent import progress
from indigoshell.agent.progress import Jobs
from indigoshell.agent.ui.call import CallHost
from indigoshell.agent.ui.status import CallStatus
from indigoshell.core import paths

progress.LINGER_S = 0.8         # a finished loose job goes within the test

TMP = tempfile.mkdtemp(prefix="call-progress-")
REPORTER = os.path.join(TMP, "reporter.py")
with open(REPORTER, "w") as f:
    f.write(textwrap.dedent("""\
        import json, os, socket, sys, time
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        path = os.environ["INDIGO_PROGRESS"]
        def report(**fields):
            s.sendto(json.dumps(fields).encode(), path)
        name, seconds = sys.argv[1], float(sys.argv[2])
        report(label=name, stages=["download", "audio"], stage="download")
        t0 = time.monotonic()
        while (t := time.monotonic() - t0) < seconds:
            report(done=t / seconds * 50e6, total=50e6, unit="B")
            time.sleep(0.1)
        report(done=50e6, total=50e6, unit="B")
    """))
FAKE_CLI = textwrap.dedent("""\
    import subprocess, sys
    q = "'" + sys.argv[1].replace("'", "'\\"'\\"'") + "'"
    sys.exit(subprocess.call(["bash", "-c", "eval " + q + " < /dev/null"]))
""")


async def start(agent, command: str):
    env = dict(os.environ, **agent.jobs.env())
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", FAKE_CLI, command, "--output-format",
        "stream-json", env=env)


def job_rows(plate, loose: bool) -> list:
    return [r for r in plate._built if r.kind == "job"
            and bool(r.label) == loose]


async def main() -> int:
    display = _live.connect()
    agent = _live.ScriptedAgent(voice="indigo")
    # Jobs of its own, on a socket of the test's own (the hub reports
    # none, so a RemoteAgent has no bars of its own to draw).
    sock = os.path.join(paths.runtime_dir(),
                        f"indigoshell-test-progress-{os.getpid()}.sock")
    agent.track(Jobs(path=sock, shims=os.path.join(TMP, "shims")))
    agent.jobs.start()
    agent.dial()

    host = CallHost(agent)
    _live.count_frames(host.agent_plate)
    spec, _services = config.call(agent)
    win = None
    bad = 0
    for label in ("first open", "opened again"):
        if win is None:
            win = _live.open_window(display,
                                    _live.offscreen(spec, content=host))
        else:
            await _live.reopen(win)
        await asyncio.sleep(0.4)
        tag = label.replace(" ", "-")

        # A step whose command reports.
        agent.say("download the clip")
        command = f"python3 {REPORTER} clip-{tag}.webm 1.6"
        agent.tool(f"t-{tag}", command)
        proc = await start(agent, command)
        await asyncio.sleep(1.0)
        under_step = [r.job.step for r in job_rows(host.agent_plate, False)]
        await proc.wait()
        await asyncio.sleep(0.3)
        agent.result(f"t-{tag}", "done")
        act = next(a for a in agent.activity if a.tool_id == f"t-{tag}")
        agent.text_start()
        agent.text("Got it, choom.")
        agent.end()

        # A command left running once the turn is over.
        await asyncio.sleep(0.5)
        loose_cmd = f"python3 {REPORTER} big-{tag}.iso 2.4"
        proc = await start(agent, loose_cmd)
        await asyncio.sleep(1.0)
        loose = job_rows(host.agent_plate, True)
        host.agent_plate.frames = 0
        await asyncio.sleep(1.0)
        idle_fps = host.agent_plate.frames / 1.0
        await proc.wait()
        await asyncio.sleep(progress.LINGER_S + 0.6)
        left = job_rows(host.agent_plate, True) + job_rows(host.agent_plate,
                                                           False)
        n, box = _live.stale(win, f"progress-{tag}")
        good = (under_step == [f"t-{tag}"]
                and act.summary.startswith("50.0 MB in 0:0")
                and len(loose) == 1 and idle_fps > 12 and not left
                and n == 0)
        bad += not good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: bar under its step "
              f"{under_step}, summary {act.summary!r}; loose job rows "
              f"{len(loose)}, plate {idle_fps:.0f} frames/s with only it "
              f"moving; {len(left)} rows left; "
              + (f"{n}px left over at {box}" if n else "nothing left over"),
              flush=True)

    done = asyncio.Event()
    win.dismiss(done.set)
    await done.wait()
    win.destroy()

    # The status plate: a job in its second line, then gone.
    widget = CallStatus(agent)
    _live.count_frames(widget)
    swin = _live.open_window(display,
                             _live.offscreen(_live.status_spec(widget)))
    await asyncio.sleep(0.4)
    proc = await start(agent, f"python3 {REPORTER} status.iso 2.0")
    await asyncio.sleep(1.0)
    shown = widget._job()
    widget.frames = 0
    await asyncio.sleep(0.6)
    fps = widget.frames / 0.6
    await proc.wait()
    await asyncio.sleep(progress.LINGER_S + 0.6)
    n, box = _live.stale(swin, "progress-status")
    good = (shown is not None and shown.label == "status.iso" and fps > 12
            and widget._job() is None and n == 0)
    bad += not good
    print(f"  {'ok  ' if good else 'FAIL'} status plate: shows "
          f"{shown.label if shown else None}, {fps:.0f} frames/s; "
          + (f"{n}px left over at {box}" if n else "nothing left over"),
          flush=True)
    swin.destroy()
    agent.jobs.stop()
    print("FAILED" if bad else "all passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
