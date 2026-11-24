"""The call's progress bars at 1x, from the real widgets, with faked
jobs: a download, a skill's pipeline mid-stage, a busy stage, a finished
step with its summary, a background job left from an earlier turn, and
the hung-up status plate.

    PYTHONPATH=../.. ../../.venv/bin/python render_call_progress.py
"""
import time

import skia

from common import txt
from indigoshell import theme
from indigoshell.agent.activity import Activity
from indigoshell.agent.progress import Job, Jobs
from render_call import agent_in as hub_agent, call, status


def agent_in(**state):
    """`render_call.agent_in`, drawing bars from jobs of its own: the hub
    reports none, so a RemoteAgent's are an empty stand-in. Each step's
    bar is its job's, and a job no step of the turn claims shows loose,
    as the desktop's own agent drew them."""
    a = hub_agent(**state)
    a.jobs = Jobs()                     # never started: only what job() adds

    def job_of(act):
        j = a.jobs.by_step(act.tool_id)
        return j if j is not None and j.live and j.visible() else None

    def loose_jobs():
        steps = {x.tool_id for x in a.activity if x.tool_id}
        return [j for j in a.jobs.shown() if j.step not in steps]

    a.job_of, a.loose_jobs = job_of, loose_jobs
    return a


def job(agent, step, *, label="", stages=(), stage="", done=None, total=None,
        unit="", text="", age=20.0, rate=None, ended=False):
    now = time.monotonic()
    j = Job(root=10_000 + len(agent.jobs._jobs), under="cli", cmdline="",
            id=len(agent.jobs._jobs) + 1, step=step, label=label,
            stages=list(stages), stage=stage, done=done, total=total,
            unit=unit, text=text, started=now - age)
    if done is not None and rate:
        j._samples.append((now - 3, max(0.0, done - 3 * rate)))
        j._samples.append((now, done))
    if ended:
        j.ended = now
    agent.jobs._jobs[j.root] = j
    return j


def main():
    now = time.monotonic()
    url = "https://www.youtube.com/watch?v=dQw8x9WgXcQ"

    a1 = agent_in(phase="working", busy=True, turn_started=now - 23,
                  heard="download this talk for me",
                  activity=[Activity("tool", f"$ yt-dlp -f 'bv*+ba' {url}",
                                     tool_id="t1", tool="Bash")])
    job(a1, "t1", label="Designing for the edge - talk [dQw8x9WgXcQ].webm",
        done=22.4e6, total=51.0e6, unit="B", rate=3.1e6)

    a2 = agent_in(phase="working", busy=True, turn_started=now - 64,
                  heard="transcribe the video open in firefox",
                  activity=[Activity("tool", "browser_tabs", tool_id="t0",
                                     result="3 tabs"),
                            Activity("tool", "$ uv run --script .claude/skills/"
                                     f"transcribe/transcribe.py {url}",
                                     tool_id="t2", tool="Bash")])
    job(a2, "t2", label="transcribe",
        stages=("download", "audio", "model", "transcribe", "write"),
        stage="transcribe", done=252.0, total=588.0, unit="s",
        text="tr, 9:48 long", rate=14.2, age=40)

    a3 = agent_in(phase="working", busy=True, turn_started=now - 31,
                  heard="transcribe the voice note from my phone",
                  activity=[Activity("tool", "phone_pull WhatsApp Voice Notes/"
                                     "PTT-20261119-WA0007.opus",
                                     tool_id="t5", tool="mcp__shell__phone_pull",
                                     result="Pulled 1 file", summary="4.2 MB in 0:03"),
                            Activity("tool", "$ uv run --script .claude/skills/"
                                     "transcribe/transcribe.py /tmp/tmp.x/"
                                     "PTT-20261119-WA0007.opus",
                                     tool_id="t3", tool="Bash")])
    job(a3, "t3", label="transcribe",
        stages=("audio", "model", "transcribe", "write"), stage="model",
        text="large-v3 on cuda (float16)", age=6)

    a4 = agent_in(phase="idle", busy=False, heard="what's the weather like",
                  activity=[Activity("tool", "$ curl -s 'wttr.in/?format=3'",
                                     tool_id="t7", tool="Bash",
                                     result="Istanbul: 14°C")],
                  message="**14°C** and clear, choom. Your download is still "
                          "going, about a minute left.")
    job(a4, "t4", label="archlinux-2026.11.01-x86_64.iso", done=812e6,
        total=1.32e9, unit="B", rate=9.4e6, age=90)

    shots = [
        ("a download", call(a1, spin=40)),
        ("a skill's pipeline", call(a2, spin=80)),
        ("a busy stage; a step's summary", call(a3, spin=10)),
        ("a background job from before", call(a4)),
    ]
    s1 = agent_in(phase="working", busy=True, turn_started=now - 64,
                  activity=[Activity("tool", "$ uv run --script ...",
                                     tool_id="t2", tool="Bash")])
    job(s1, "t2", label="transcribe",
        stages=("download", "audio", "model", "transcribe", "write"),
        stage="transcribe", done=252.0, total=588.0, unit="s", rate=14.2)
    s2 = agent_in(phase="working", busy=True, turn_started=now - 23,
                  activity=[Activity("tool", "$ yt-dlp ...", tool_id="t1",
                                     tool="Bash")])
    job(s2, "t1", label="Designing for the edge - talk.webm", done=22.4e6,
        total=51.0e6, unit="B", rate=3.1e6)
    s3 = agent_in(phase="idle", busy=False)
    job(s3, "", label="archlinux-2026.11.01-x86_64.iso", done=812e6,
        total=1.32e9, unit="B", rate=9.4e6, age=90)
    small = [
        ("hung up: a pipeline", status(s1, spin=70)),
        ("hung up: a download", status(s2, spin=20)),
        ("idle, a job left running", status(s3)),
    ]

    gap, top = 24, 40
    rows = [shots[:2], shots[2:], small]
    row_h = [max(img.height() for _, img in r) + top for r in rows]
    w = max(sum(img.width() for _, img in r) + gap * (len(r) + 1) for r in rows)
    h = sum(row_h) + gap
    surface = skia.Surface(w, h)
    c = surface.getCanvas()
    c.clear(skia.ColorSetARGB(255, 28, 28, 34))
    y = 0
    for r, rh in zip(rows, row_h):
        x = gap
        for label, img in r:
            txt(c, label, x + 36, y + 26, 13, theme.HUD_FG_BRIGHT, bold=True)
            c.drawImage(img, x, y + top)
            x += img.width() + gap
        y += rh
    surface.flushAndSubmit()
    surface.makeImageSnapshot().save("call_progress.png", skia.kPNG)
    print("call_progress.png")


if __name__ == "__main__":
    main()
