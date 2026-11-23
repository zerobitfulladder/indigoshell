---
name: new-skill
description: Build yourself a new ability (a job you can repeat, like transcribing a video, converting or renaming files, or pulling data from a site) as a skill in this workspace, or change or remove one you built. Use when the user asks you to set up, learn or build a way to do something, says "from now on" about a kind of task, or when a one-off job needed packages installed.
---

# Building a new ability

An ability you build for yourself is a skill: one folder under
`.claude/skills/` with everything it needs inside it, and nothing
installed anywhere else. The user can read it, have you change it on a
call, and remove it by deleting the folder.

## Layout

```
.claude/skills/<name>/
  SKILL.md       front matter (name, description), then how to use it
  tools/         the code you write for it: scripts, helpers to paste
                 into browser_js, a page it shows, templates
  data/          what it collects or learns: past results, a history,
                 the user's preferences for it. Its tools write here.
```

- `<name>`: short, lowercase, hyphens: `transcribe`, `pdf-merge`.
- `description` is all you see of a skill until you use it. Say what it
  does and when to reach for it, in the words the user would use.
- SKILL.md is for you on a later call: the command to run, what comes
  out, where it goes, how to show it, failures you have seen and their
  fixes. Keep it short. Facts about the machine go in CLAUDE.md, not here.
- Only the parts it needs: a job that only strings together installed
  command-line tools has no tools/ (the commands go in SKILL.md), and a
  job that keeps nothing between runs has no data/.
- Scripts find data/ from their own place,
  `Path(__file__).resolve().parent.parent / "data"`, not from a path
  written into them.

## Code: uv scripts

Python, with its packages declared at the top of the file:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["yt-dlp"]
# ///
```

Run it from the workspace:
`uv run --script .claude/skills/<name>/tools/<name>.py ARGS`. uv builds the
environment once in its cache (`~/.cache/uv/environments-v2/`) and reuses
it after (about 0.2 s). Nothing appears next to the script.

- Never `pip install` (system or `--user`), never create a venv, never
  `uv tool install`. Needing a package means adding it to `dependencies`.
- System packages (`pacman`, `yay`) only after asking; CLAUDE.md's rules
  for package transactions apply. Calling tools already installed
  (ffmpeg, deno, …) is fine.
- Packages that break when a website changes (yt-dlp and the like) go in
  `dependencies` instead of using the system copy, which lags behind.
  When one starts failing:
  `uv run --script --upgrade-package <pkg> <script> ARGS`, then try again.
- A package without a build for the system Python (3.14): pin
  `requires-python = "==3.12.*"`; uv fetches that Python by itself.
- Interface: arguments in; the result (a path, or short text) on stdout;
  errors on stderr, progress through `report` (below); a non-zero exit on failure. Scratch
  files in a temp dir removed at exit.
- The script sets up its own environment, so running it stays one line.
  CUDA libraries from pip (nvidia-* packages) are not on the loader path,
  and LD_LIBRARY_PATH is read only when a process starts: preload them
  with ctypes, or re-exec the script with the variable set.
- Big downloads (models) stay in their usual cache (Hugging Face:
  `~/.cache/huggingface/hub`). Check it first; some are already there.
- GPU: CUDA, RTX 3060 with 6 GB, shared with the desktop.

## Progress on the call

A job that takes more than a few seconds shows its progress on the call,
as a bar under the step that runs it. yt-dlp, ffmpeg, curl and rsync do
that by themselves when run as commands. A script reports its own with
this function, pasted in as it is (standard library only; outside the
shell it does nothing, and it never blocks or fails):

```python
import json, os, socket, time

_progress = [None, 0.0]


def report(**fields):
    """Progress on the call. stages=[...] once, stage="..." as each one
    starts, done=/total= with unit="B" (bytes), "s" (seconds of media),
    "%" or "" (items), text="..." for a short status line."""
    path = os.environ.get("INDIGO_PROGRESS")
    if not path:
        return
    now = time.monotonic()
    if set(fields) <= {"done", "total", "unit"} and now - _progress[1] < 0.1 \
            and fields.get("done") != fields.get("total"):
        return
    _progress[1] = now
    try:
        if _progress[0] is None:
            _progress[0] = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _progress[0].setblocking(False)
        _progress[0].sendto(json.dumps(fields).encode(), path)
    except OSError:
        pass
```

- A pipeline names its stages once,
  `report(label="transcribe", stages=["download", "audio", "transcribe"])`,
  then `report(stage="audio")` as each one starts. A new stage clears
  the bar.
- Numbers where they are known: bytes from a download (yt-dlp's
  `progress_hooks`), seconds of media done out of its length, items out
  of a total. A stage without them shows as busy; an ffmpeg run inside
  it fills the bar by itself.
- `text=` is for one fact worth seeing ("Turkish, 9:48 long"), not a log.
- Progress goes through `report`, not prints: stderr is for errors and
  what you need to read afterwards.

## Logins and keys

Never put them in the skill folder (data/ included) or on a command
line. If a job needs the user's login (a site's cookies from Firefox, an
API key), ask how they want it handled before using it.

## Building it

1. Agree the shape with the user in a line or two: what goes in, what
   comes out, where results are saved and how they are shown (CLAUDE.md's
   "Showing things" has the defaults). Ask what you can't reasonably
   guess; don't interview them.
2. Anything slow (big downloads, model runs): hang_up first.
3. Write the folder, then run it on a real example (one the user gave,
   or a small public one). Fix it until it works end to end.
4. Tell the user in one line: the skill's name, what to ask for, and what
   it can't do yet.

A skill made during a conversation may not be in your skill list until
the next one. Until then, read its SKILL.md and follow it.

## Changing or removing one

When the user wants an ability to work differently ("save them as
.txt", "use the small model"), change its SKILL.md or script: that is
where the preference lives, so later calls follow it. Removing an
ability means deleting its folder, data/ included.

## One-off jobs and projects

A job wanted once doesn't need a skill. If it needs packages, use
`uv run --with <pkg> python <script>` with the script in a temp dir, not
the workspace. If it took real setup, offer at the end, in one line, to
keep it as a skill.

Something you build that isn't a repeatable job (an analysis to run
again on new data, a small page or app for the user) gets its own folder
in `projects/` in the workspace, with a short NOTES.md saying what it is
and how to run it. Never create folders among the user's own projects.
