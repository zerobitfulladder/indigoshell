"""Flux's section of the shell's top bar: what a window's state becomes as
a `bar` (indigoshell/applink/PROTOCOL.md; applink/bar.py's builders).

Pure Python, as the rest of Flux's side of the line.

    start   the agent's name in its voice's colour, for the agent's own
            window ("Lilac"); then the tabs, one look for every app: a
            tab per page, its id the page's, the current one's load as
            its `progress`, a tab behind that loads `busy`, the agent's
            hold on each (`agent`)
    end     the current page's host, after an open padlock (`error`)
            when it came over an insecure connection and a bookmark when
            the page counts as one

A main window's tabs can be closed, moved and torn off; a single
window's (a sign-in popup, the inspector) nothing.
"""

from indigoshell.applink import bar as B
from indigoshell.core.voices import voice_id

TABS_ITEM = "tabs"
HOST_ITEM = "host"
OWNER_ITEM = "owner"


def tabs_item(state: dict, *, single: bool) -> dict:
    tabs, current = state["tabs"], state["current"]
    agents = state.get("agent") or [None] * len(tabs)
    out = []
    for i, t in enumerate(tabs):
        here = i == current
        loading = bool(t.get("loading"))
        progress = state.get("progress", 100)
        out.append(B.tab(t["tab"], t["title"], busy=loading,
                         progress=progress if here and loading and progress < 100 else None,
                         agent=B.agent(voice_id(agents[i]["voice"]), agents[i]["state"])
                         if i < len(agents) and agents[i] else None))
    return B.tabs(TABS_ITEM, out, current=tabs[current]["tab"] if tabs else None,
                  can=() if single else ("close", "move", "detach"))


def host_item(state: dict) -> dict:
    tabs, current = state["tabs"], state["current"]
    host = tabs[current]["host"] if tabs else ""
    spans = []
    if state.get("bookmarked"):
        spans.append(B.span(icon="bookmark"))
    if not state.get("secure", True):
        spans.append(B.span(icon="unlocked", color="error"))
    spans.append(B.span(host))
    return B.text(HOST_ITEM, spans=spans)


def bar_of(state: dict, *, single: bool = False, agent: str | None = None) -> dict:
    """The bar for a window's state; `agent`: the voice whose own window
    it is (none for the user's)."""
    start = []
    if agent:
        from indigoshell.core.voices import voice_name
        start.append(B.text(OWNER_ITEM, voice_name(agent), color=f"voice:{voice_id(agent)}"))
    start.append(tabs_item(state, single=single))
    return B.bar(start, [host_item(state)])
