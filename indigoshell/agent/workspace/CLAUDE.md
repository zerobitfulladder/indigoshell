# Device notes

This is the call agent's notebook for this machine. indigoshell's call
overlay starts the agent in this directory, so Claude Code reads this
file at the start of every call.

The agent adds short, factual notes here as it learns how this computer
is set up. Edit or prune them freely.

This file is about the machine: its setup, fixes, and the user's rules
for working on it, including how to show them things. Things about the
user as a person (how they like to be talked to, nicknames, what they
enjoy) go in the agent's memory instead. Each fact lives in one place.

## Working rules
- New abilities are skills. When the user wants a job you can repeat
  (transcribe a video, convert files, …), build it as a skill under
  .claude/skills/ the way the `new-skill` skill says: SKILL.md, its
  code in tools/, what it collects or learns in data/; Python as uv
  scripts with their packages declared inline. Never pip install or
  create venvs; ask before pacman or yay. Something you build that
  isn't a repeatable job gets its own folder in projects/.
- Long commands show their progress on the call by themselves:
  yt-dlp, ffmpeg, curl and rsync run through shims that read it
  (`which yt-dlp` names the shim; that is expected), and skill
  scripts report it (new-skill says how). For a long download or
  transcode, don't silence the progress (-q, --no-progress, -s,
  -nostats) to save output: the shims pass on only its last line.
  The `progress` tool says how far each one has got.

## Showing things
How the user wants to be shown things. When they ask for it done
differently on a call, change this section (and, for how reports look,
.claude/skills/report/template.html) so later calls follow it.
- Long answers (reports, comparisons, tables, anything past a dozen
  lines): write an HTML page with the `report` skill into reports/ and
  open it with the shell's `open` tool. It opens in Firefox and brings
  that window forward. Say one line about it on the call.
- Images, videos, PDFs: `open` them. Each opens in its usual program
  (Firefox for images and PDFs, Celluloid for mp4) and the user is taken
  to that window's workspace.
