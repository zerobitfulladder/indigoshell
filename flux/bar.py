"""Flux's section of the shell's top bar: what a window's state becomes as
a `bar` (indigoshell/applink/PROTOCOL.md; applink/bar.py's builders).

Pure Python, as the rest of Flux's side of the line. A window's state
(`state`, what qt/window.py's `state()` gives) is Flux's own snapshot of
it: its tabs (`tab`: id, title, host, loading, and the id of its page's
icon, flux/qt/favicon.py, while it has one), the current one, its
load, whether its page is secure, its address, whether it counts as a
bookmark, Flux's own find line while one is open (`find`: only with no
shell, or one that could not show its input), the agent's hold on each
tab, and the zoom the user just set (`zoom`, "110%", for ZOOM_NOTE_S
after each of their zoom steps; none for a site's zoom given to a page
as it loads). The window's line over the page (no shell) says the
same.

    start   the agent's name in her colour (the `agent` role), for her
            own window ("Lilac"); then the tabs, one look for every app: a
            tab per page, its id the page's, its page's icon (`image`,
            the PNG itself sent apart: qt/link.py) or, while it has none,
            the first letter of its host (`letter`), the current one's
            load as its `progress`, a tab behind that loads `busy`, the
            agent's hold on each (`agent`)
    end     the current page's host, after an open padlock (`error`)
            when it came over an insecure connection and a bookmark when
            the page counts as one; the zoom in its place (`info`) while
            the state has one; `fixed`, so switching to a tab with a
            longer host leaves the tabs their room

            and, while Flux's own find is open, its line before the host
    inputs  `address` (Ctrl+L: the page's address, selected) and
            `new_tab` (Ctrl+T, empty), both answered from Flux's places
            (flux/addresses.py), a bookmark's row taken out with
            Shift+Delete (`remove`); `find` (Ctrl+F, `steps`): the count
            answered as the page counts, then n / N step
    menus   `actions` (Ctrl+M): first, for each tab the user took over
            from the agent, handing it back ("Let Lilac drive again");
            then Add bookmark and Remove bookmark for the page, the one
            that does not apply dimmed

A main window's tabs can be closed, moved and torn off; a single
window's (a sign-in popup, the inspector) nothing, and it has no inputs
or menus: its keys stay its own.
"""

from flux import places
from flux.addresses import REMOVE, TYPED  # noqa: F401  (the address inputs' names)
from indigoshell.applink import bar as B
from indigoshell.core import persona

ADDRESS, NEW_TAB, FIND, ACTIONS = "address", "new_tab", "find", "actions"
ADD_ROW, REMOVE_ROW = "add", "remove"
RESUME = "resume:"              # + a tab id: hand that tab back to the agent
PLACEHOLDER = "address or search"

TABS_ITEM = "tabs"
HOST_ITEM = "host"
OWNER_ITEM = "owner"
FIND_ITEM = "find"

FIND_LABEL = "find"
FIND_CARET = "\u258f"           # ▏, after the query while it is typed (Flux's own line)
NO_MATCHES = "no matches"
ZOOM_NOTE_S = 0.5               # the zoom shown in the host's place after a step


# ── a window's state ────────────────────────────────────────────────────
def tab(title: str, host: str, loading: bool, *, id_: int, image: str | None = None) -> dict:
    """One tab of a window's state; `image`: its page's icon's id."""
    return {"tab": int(id_), "title": title, "host": host, "loading": bool(loading),
            "image": image}


def letter_of(host: str) -> str | None:
    """What a tab with no icon shows: the first letter of its host, past
    a "www."; None for no host (an empty page, a file)."""
    host = host[4:] if host.startswith("www.") else host
    return host[0] if host and host[0].isprintable() and not host[0].isspace() else None


def find(query: str, active: int | None, total: int | None, typing: bool) -> dict:
    return {"query": query, "active": active, "total": total, "typing": bool(typing)}


def find_count(f: dict) -> str:
    """The count after a find's query: "3/12", "no matches", or nothing
    before the page has counted."""
    total, active = f.get("total"), f.get("active")
    if total is None or not f.get("query"):
        return ""
    if total == 0:
        return NO_MATCHES
    return f"{active or 0}/{total}"


def state(tabs: list[dict], current: int, progress: int, secure: bool, *, url: str,
          bookmarked: bool, find: dict | None = None,
          agent: list[dict | None] | None = None, zoom: str | None = None) -> dict:
    """A window's state (the module docstring)."""
    agents = list(agent) if agent is not None else [None] * len(tabs)
    return {"tabs": tabs, "current": int(current), "progress": int(progress),
            "secure": bool(secure), "url": url, "bookmarked": bool(bookmarked), "find": find,
            "agent": agents, "zoom": zoom}


def zoom_text(factor: float) -> str:
    """A zoom as the bar says it: "110%"."""
    return f"{int(round(factor * 100))}%"


# ── its bar ─────────────────────────────────────────────────────────────


TITLE_CUT = 300                 # characters of a tab's title sent, at most


def _cut(value: str, n: int = TITLE_CUT) -> str:
    return value if len(value) <= n else value[:n - 1] + "…"


def tabs_item(state: dict, *, single: bool) -> dict:
    tabs, current = state["tabs"], state["current"]
    agents = state.get("agent") or [None] * len(tabs)
    out = []
    for i, t in enumerate(tabs):
        here = i == current
        loading = bool(t.get("loading"))
        progress = state.get("progress", 100)
        image = t.get("image")
        out.append(B.tab(t["tab"], _cut(t["title"]), busy=loading,
                         progress=progress if here and loading and progress < 100 else None,
                         agent=B.agent(agents[i]["state"])
                         if i < len(agents) and agents[i] else None,
                         image=image, letter=None if image else letter_of(t["host"])))
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
    zoom = state.get("zoom")
    spans.append(B.span(zoom, color="info") if zoom else B.span(_cut(host)))
    return B.text(HOST_ITEM, spans=spans, fixed=True)


def find_item(state: dict) -> dict | None:
    """Flux's own find, while it is open (no shell showed its input):
    its line as text before the host."""
    f = state.get("find")
    if not f:
        return None
    count = find_count(f)
    caret = FIND_CARET if f.get("typing") else ""
    spans = [B.span(icon="search"), B.span(_cut(f["query"]) + caret, color="strong")]
    if count:
        spans.append(B.span("  " + count, color="error" if count == NO_MATCHES else "info"))
    return B.text(FIND_ITEM, spans=spans)


def inputs_of(state: dict) -> dict:
    url = state.get("url") or ""
    # The page's address, to edit (none past the bound: a box to type in).
    here = "" if url in ("", "about:blank") or len(url) > B.INPUT_TEXT_MAX else url
    remove = {REMOVE: B.action("shift+delete", drops=True)}
    return {ADDRESS: B.input_(chords=["ctrl+l"], icon="search", placeholder=PLACEHOLDER,
                              text=here or None, select=bool(here), query=True, actions=remove),
            NEW_TAB: B.input_(chords=["ctrl+t"], icon="search", placeholder=PLACEHOLDER,
                              query=True, actions=remove),
            FIND: B.input_(chords=["ctrl+f"], icon="search", placeholder=FIND_LABEL, query=True,
                           steps=True)}


def actions_rows(state: dict) -> list[dict]:
    """The window's actions for its current page; first, while the user
    has taken a tab over from the agent, handing it back (by her name, as
    the shell gave it: core/persona.py)."""
    url = state.get("url") or ""
    marked = bool(state.get("bookmarked"))
    page = places.recordable(url)
    host = places.host_of(url)
    tabs = state["tabs"]
    rows = []
    for i, a in enumerate(state.get("agent") or ()):
        if a and a.get("state") == "paused" and i < len(tabs):
            detail = " · ".join(filter(None, (f"tab {i + 1}", tabs[i].get("host") or "")))
            rows.append(B.row(f"{RESUME}{tabs[i]['tab']}", f"Let {persona.name()} drive again",
                              icon="play", detail=detail))
    rows.append(B.row(ADD_ROW, "Add bookmark", icon="bookmark", detail=host or None,
                      enabled=page and not marked))
    rows.append(B.row(REMOVE_ROW, "Remove bookmark", icon="bookmark", detail=host or None,
                      enabled=page and marked))
    return rows


def bar_of(state: dict, *, single: bool = False, agent: bool = False) -> dict:
    """The bar for a window's state; `agent`: whether it is the agent's
    own window (not the user's)."""
    start = []
    if agent:
        start.append(B.text(OWNER_ITEM, persona.name(), color="agent"))
    start.append(tabs_item(state, single=single))
    end = [item for item in (find_item(state), host_item(state)) if item is not None]
    if single:
        return B.bar(start, end)
    return B.bar(start, end, inputs_of(state),
                 {ACTIONS: B.menu(actions_rows(state), chords=["ctrl+m"])})
