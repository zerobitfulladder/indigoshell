"""The call: the agent on the line, from a corner of the screen.

The agent lives on a hub, a machine of its own (lilac, on a Raspberry
Pi): Lilac, in one chat that never ends. This package is the shell's
side of it. Press the keybinding (`indigoshell toggle call`),
type, and the agent answers in a frosted overlay; hang up and it keeps
working, then rings you back through the top bar's chip. While it works
it uses this machine's hands: the tools the laptop lends it.

The package, by job:

    remote.py        `RemoteAgent`: the line to the hub (a WebSocket with
                     TLS) and the chat as the call reads it: turns,
                     approvals, ring-backs, how full it is, the earlier
                     exchanges to scroll back through. No drawing; the
                     overlay only reads and subscribes.
    activity.py      the agent's steps and questions, as the overlay
                     shows them
    hands.py         `Hands`: the tools this machine lends the hub's
                     agent, and the calls to them (what a tool is:
                     indigoshell/core/hands.py)
    laptop.py        the desktop as those tools: open, screenshot,
                     windows, apps, the clipboard, a shell (root asks
                     first, in the call), background jobs and files
    desk.py          the desktop helpers those and the call share:
                     running a program, the window manager
                     (indigoshell/wm), opening a link or a file brought
                     forward on its workspace
    progress.py      `Jobs`: how far long commands have got, which the
                     call can draw as progress bars. A socket the
                     commands report to ($INDIGO_PROGRESS), each job
                     matched to the step that started it, and the shims
                     that put yt-dlp, ffmpeg, curl and rsync under...
    watch.py         ...the wrapper that reads their progress; and
                     `Reporter`, the sending end (standard library only).
                     Nothing on the hub reports them yet.

    ui/style.py      sizes, colours, motion; the shared frame and heading
    ui/rows.py       text on the monospace grid: wrapping, Markdown rows,
                     selection-ready lines
    ui/chip.py       the CALL chip
    ui/context.py    the bar of how full the chat is
    ui/agent_plate.py, ui/you_plate.py
                     the two lines of the call
    ui/bars.py       progress bars: under a step, and on the status chip
    ui/call.py       `CallHost`: the call window and its keys
    ui/status.py     `AgentChip` + `CallRouter` (+ the old corner plate,
                     `CallStatus`): the top bar's chip while a hung-up
                     call works or rings

Flux, the shell's browser, lends its own hands (flux/hands.py), which
`laptop_hands()` adds when Flux is here.

Wiring it into a config: config_default's `call(hub)` gives the call
window and the services it needs (the hub's line, and the router the
top bar's chip reads):

    HUB = RemoteAgent(endpoints=[...],
                      tls={"ca": ..., "cert": ..., "key": ...},
                      hands=laptop_hands())
    CALL, CALL_SERVICES = call(HUB)
    WINDOWS = [*WINDOWS, CALL]
    SERVICES = [*SERVICES, *CALL_SERVICES]
"""

from .hands import laptop_hands
from .remote import RemoteAgent
from .ui.call import CALL_WINDOW, CallHost
from .ui.status import STATUS_WINDOW, CallRouter, CallStatus

__all__ = ["RemoteAgent", "laptop_hands", "CallHost", "CallStatus", "CallRouter",
           "CALL_WINDOW", "STATUS_WINDOW"]
