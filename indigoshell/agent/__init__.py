"""The call: Claude on the line, from a corner of the screen.

Press the keybinding (`indigoshell toggle call`), type, and the agent —
Claude Code, run through the Agent SDK on its own account — answers in a
frosted overlay; hang up and it keeps working, then rings you back.

The package, by job:

    session.py       `Agent`: the conversation, its Claude Code process,
                     turns, approvals, ring-backs, past conversations.
                     No drawing; the overlay only reads and subscribes.
    prompts.py       what the agent is told: the call protocol, the
                     persona (its voice), its notebook in the workspace
    permissions.py   `ASK_FIRST`: what always asks you, on top of auto mode
    activity.py      the agent's steps and questions, as the overlay
                     shows them
    tools.py         `ShellTools`: the desktop as tools — screenshots,
                     opening links and files (brought forward on their
                     workspace), apps, workspaces, the clipboard, Spotify
    workspace/       the agent's starting files: its notebook
                     (CLAUDE.md, including how you want to be shown
                     things) and its report skill (steps, page template)
    seed.py          puts those in its workspace when missing — never
                     over its own copies, which it edits when you ask it
                     to work differently — and regenerates the report
                     palette from theme.py on every call

    ui/style.py      sizes, colours, motion; the shared frame and heading
    ui/rows.py       text on the monospace grid: wrapping, Markdown rows,
                     selection-ready lines
    ui/chip.py       the CALL chip
    ui/agent_plate.py, ui/you_plate.py
                     the two lines of the call
    ui/call.py       `CallHost`: the call window and its keys
    ui/status.py     `CallStatus` + `CallRouter`: the corner chip while a
                     hung-up call works or rings

The account: the agent signs in from `~/.config/indigoshell/claude`
(CLAUDE_CONFIG_DIR, passed only to its own process), so it can be a
different account from a `claude` in a terminal. Its working directory is
`~/.config/indigoshell/assistant`, whose CLAUDE.md is its notebook for
this machine. Wiring it into a config:

    AGENT = Agent(name="INDIGO", model="opus",
                  tools=ShellTools())
    SERVICES = [..., AGENT, CallRouter(AGENT)]
    WINDOWS = [..., WindowSpec(name=CALL_WINDOW, content=CallHost(AGENT), ...),
               WindowSpec(name=STATUS_WINDOW, content=CallStatus(AGENT), ...)]
"""

from .session import Agent
from .tools import ShellTools
from .ui.call import CALL_WINDOW, CallHost
from .ui.status import STATUS_WINDOW, CallRouter, CallStatus

__all__ = ["Agent", "ShellTools", "CallHost", "CallStatus", "CallRouter",
           "CALL_WINDOW", "STATUS_WINDOW"]
