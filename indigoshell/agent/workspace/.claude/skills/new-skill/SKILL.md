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
  <name>.py      the work, when it needs code
  ...            anything else it reads: templates, word lists
```

- `<name>`: short, lowercase, hyphens: `transcribe`, `pdf-merge`.
- `description` is all you see of a skill until you use it. Say what it
  does and when to reach for it, in the words the user would use.
- SKILL.md is for you on a later call: the command to run, what comes
  out, where it goes, how to show it, failures you have seen and their
  fixes. Keep it short. Facts about the machine go in CLAUDE.md, not here.
- A job that only strings together installed command-line tools needs no
  script: put the commands in SKILL.md, or a short `<name>.sh`.

## Code: uv scripts

Python, with its packages declared at the top of the file:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["yt-dlp"]
# ///
```

Run it from the workspace:
`uv run --script .claude/skills/<name>/<name>.py ARGS`. uv builds the
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
  progress and errors on stderr; a non-zero exit on failure. Scratch
  files in a temp dir removed at exit.
- The script sets up its own environment, so running it stays one line.
  CUDA libraries from pip (nvidia-* packages) are not on the loader path,
  and LD_LIBRARY_PATH is read only when a process starts: preload them
  with ctypes, or re-exec the script with the variable set.
- Big downloads (models) stay in their usual cache (Hugging Face:
  `~/.cache/huggingface/hub`). Check it first; some are already there.
- GPU: CUDA, RTX 3060 with 6 GB, shared with the desktop.

## Logins and keys

Never put them in the skill folder or on a command line. If a job needs
the user's login (a site's cookies from Firefox, an API key), ask how
they want it handled before using it.

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
ability means deleting its folder (that asks the user).

## One-off jobs

A job wanted once doesn't need a skill. If it needs packages, use
`uv run --with <pkg> python <script>` with the script in a temp dir, not
the workspace. If it took real setup, offer at the end, in one line, to
keep it as a skill.
