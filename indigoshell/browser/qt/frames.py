"""Frames: a page's iframes, read and acted in, through QWebEngineFrame (Qt 6.8).

protocol.py ("Frames") has the rules as the agent meets them; this is
how Flux keeps them.

**The tree.** `page.mainFrame()` and each frame's `children()` give the
page's frames, every one, cross-origin ones (in another renderer
process) too; `runJavaScript(code, world, callback)` runs reader.js in a
frame's own copy of the agent's world (reader.WORLD), so a frame is read
and acted in exactly as the page is. Qt: QWebEngineFrame and
QWebEnginePage.mainFrame are 6.8; of the frame only isValid, url,
children and runJavaScript are used, all 6.8 (not its comparison, nor
name, htmlName or size). With an older Qt (`supported` false) frames are
not read, and say so.

**Names.** A frame's document is "f<N>": the first call into it offers
it the tab's next number (Tab.next_frame) and it keeps that name in its
own world (reader.js `frameName`) as long as it lasts, its refs carrying
it; Flux never keeps a frame object to know it again, it asks (`whoami`
answers the name). So a name is found again by asking every frame, a
frame that loads another document gets a new name with its new refs (an
old "f2:14" then says no frame f2 is there), and no number is given
twice in a tab. A batch of offers that went partly to frames named
already wastes no number: the counter moves past the ones taken only.

**Matching an element to its frame.** Qt says nothing of which element
holds a frame, and the obvious clues mislead: name() is the frame's
window.name, which its own script may change; url() is where the frame
is now, not its element's src; size() is its viewport; a position it
has none. So Flux matches by identity, asked of both sides: the frame,
in its world, finds which of its parent's windows it is (reader.js
`whoami`: `parent[i] === window`, comparing windows being allowed across
origins); the parent's read says, for each frame element, which of its
document's windows the element holds (`frameIndex`: `window[i] ===
el.contentWindow`). The same i is the same frame: exact, not a guess,
same- and cross-origin, for <iframe>, <frame> and <object>. Measured
(PySide6 6.12, tests/browser_read.py): window[i] goes in the order the
frames were made, not the page's (a frame put first later is last), and
Qt's children() goes the same way; matching by i is untouched by either.
What can still go wrong: a frame made or removed between the two
questions (milliseconds apart, asked together) shifts the indexes; a
frame then matches no element, and is read as "its element not found",
after its parent's content, never under another frame's element. A
frame that does not answer within ANSWER_S (a hung page) is not read,
and that is said too.

**Reading** (`read`): two passes. The first runs the read in the main
page for the part asked (that is the answer when the page has no
frames, one call as before frames were read) and in every frame for
nothing but its size and where its own frames sit ("survey": items or
characters, and marks: after which outline line, at which character of
the article, or after everything for text, links and forms). Flux then
lays the page out as one sequence (each frame's content where its mark
is, with the lines that say which frame it is) and asks each document
in a second pass for just the part of it the window `offset`, `length`
covers. A frame's outline line ends "(frame f2)", or "(not read: why)".

**Acting** (`path`, `to_page`, `child_at`): an element in frame f2 is
found there (its point in the frame's viewport, reader.js `point`),
then each frame above turns that into its own viewport's point
(`framePoint`: the frame element's content box, a CSS transform's scale
too, scrolled into view when the point is not in sight; refused when
something covers the frame element there), up to the page's viewport,
where the real input goes. A point over a frame goes the other way
down: the frame under it is asked what is there.

Kept sites (identity.py "never"): a frame at such a host is asked
nothing but its place (`whoami`, none of its content), is not read and
not acted in.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from indigoshell.browser import protocol

from . import reader

log = logging.getLogger(__name__)

ANSWER_S = 2.0                  # a frame that does not answer this soon is not read
READ_S = 10.0                   # a document's part, in the second pass
TOKEN = re.compile("\x01(\\d+|x)\x01")      # reader.js: where a frame element's line says its frame
CHARS = ("text", "article")     # reads in characters; the others in items


class FrameError(Exception):
    """A frame that cannot be acted in: the words say why."""


def supported(page) -> bool:
    return hasattr(page, "mainFrame")


# ── names ───────────────────────────────────────────────────────────────
def _offers(tab, n: int) -> list[str]:
    """Names for `n` frames' documents that may have none yet."""
    return [protocol.frame_name(tab.next_frame + i) for i in range(n)]


def _taken(tab, names) -> None:
    """The names frames now answer to: the next offer goes past them."""
    for name in names:
        if isinstance(name, str) and name[1:].isdigit():
            tab.next_frame = max(tab.next_frame, int(name[1:]) + 1)


def _number(name) -> int | None:
    return int(name[1:]) if isinstance(name, str) and name[1:].isdigit() else None


def descendants(tab) -> list:
    """Every frame below the main one, breadth first, at most FRAMES_MAX."""
    return [f for f, _parent in _tree(tab)]


def _tree(tab) -> list:
    """(frame, index of its parent in this list or None), breadth first,
    at most FRAMES_MAX frames, FRAMES_DEPTH deep."""
    page = tab.page
    if not supported(page):
        return []
    out, todo = [], [(f, None, 1) for f in page.mainFrame().children()]
    while todo and len(out) < protocol.FRAMES_MAX:
        f, parent, depth = todo.pop(0)
        if not f.isValid():
            continue
        out.append((f, parent))
        if depth < protocol.FRAMES_DEPTH:
            todo.extend((c, len(out) - 1, depth + 1) for c in f.children())
    return out


@dataclass(eq=False)
class Node:
    """One document of the page: the main one, or a frame's."""
    target: object                  # what runs its scripts: the page, or the QWebEngineFrame
    frame: object = None            # its QWebEngineFrame (the main frame's, for the page)
    number: int | None = None       # None for the main page
    parent: "Node | None" = None
    depth: int = 0
    index: int | None = None        # its place among its parent's windows
    url: str = ""
    host: str = ""
    kept: bool = False
    why: str | None = None          # why it is not read
    info: dict | None = None        # its first pass's answer
    children: list = field(default_factory=list)
    skipped: int = 0                # frames below it not looked at (FRAMES_MAX, FRAMES_DEPTH)

    @property
    def name(self) -> str:
        return protocol.frame_name(self.number) if self.number else ""

    @property
    def label(self) -> str:
        """Its name, or what it is when it gave none (it did not answer)."""
        return self.name or "(a frame that did not answer)"

    @property
    def shown_url(self) -> str:
        """Its address, cut to the site where the user keeps it."""
        if self.kept:
            scheme = urlsplit(self.url).scheme or "https"
            return f"{scheme}://{self.host}/"
        return self.url

    def child(self, index):
        return next((c for c in self.children if c.index is not None and c.index == index), None)

    def readable(self) -> bool:
        return self.why is None and self.info is not None


def _node(frame, parent: Node | None) -> Node:
    url = frame.url()
    return Node(frame, frame, None, parent, parent.depth + 1 if parent is not None else 1,
                url=url.toString(), host=url.host())


def _me(raw) -> dict | None:
    try:
        value = reader.answer(raw)
    except reader.ScriptError:
        return None
    if not isinstance(value, dict):
        return None
    return value.get("me", value)


# ── reading ─────────────────────────────────────────────────────────────
async def survey(job, tab, what: str, opts: dict, kept) -> Node:
    """The first pass: the main page's part asked, every frame's size and
    marks, the frames matched to their elements."""
    page = tab.page
    root = Node(page)
    root.info = await job.read(page, reader.script(what, opts))
    if not supported(page):
        return root
    root.frame = page.mainFrame()
    level, count = [root], 0
    empty = {**opts, "offset": 0, "length": 0}
    while level:
        made = []
        for node in level:
            for f in node.frame.children():
                if not f.isValid():
                    continue
                if count >= protocol.FRAMES_MAX or node.depth >= protocol.FRAMES_DEPTH:
                    root.skipped += 1
                    continue
                count += 1
                child = _node(f, node)
                node.children.append(child)
                made.append(child)
        calls = []
        for child, offer in zip(made, _offers(tab, len(made))):
            if kept(child.host):
                child.kept, child.why = True, protocol.kept(child.host)
                calls.append((child.target, reader.script("whoami", frame=offer)))
            else:
                calls.append((child.target, reader.script("survey", what, empty, frame=offer)))
        answers = await job.many(calls, ANSWER_S) if calls else []
        level = []
        for child, raw in zip(made, answers):
            try:
                value = reader.answer(raw)
            except reader.ScriptError:
                child.why = child.why or "it did not answer"
                continue
            me = value.get("me", value) if isinstance(value, dict) else {}
            me = me if isinstance(me, dict) else {}
            child.index, child.number = me.get("index"), _number(me.get("name"))
            if child.why is None:
                child.info = value.get("read")
                level.append(child)
        _taken(tab, [c.name for c in made if c.number])
    return root


@dataclass
class _Doc:
    """Items (or characters) [a, b) of a document's read, its outline
    lines indented `indent` more."""
    node: Node
    a: int
    b: int
    indent: int = 0


def _size(seg) -> int:
    return seg.b - seg.a if isinstance(seg, _Doc) else len(seg)


def _status(node: Node, index: str) -> str:
    """What a frame element's outline line says of its frame."""
    if index == "x":
        return " (not read: it holds no page)"
    child = node.child(int(index))
    if child is None:
        if node.frame is None:
            return " (not read: Flux reads frames with Qt 6.8 on)"
        if _root_of(node).skipped:
            return (f" (not read: past the {protocol.FRAMES_MAX} frames, "
                    f"{protocol.FRAMES_DEPTH} deep, Flux reads at once)")
        return " (not read: Flux found no frame for it)"
    if child.why:
        return (f" (frame {child.name}, not read: {child.why})" if child.name
                else f" (not read: {child.why})")
    return f" (frame {child.name})"


def _root_of(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _around(child: Node, what: str, mark: dict | None, indent: int) -> list:
    """A frame's segments where its mark is (None: its element was not
    found): its content, and the lines that say whose it is."""
    name, url = child.label, child.shown_url
    readable = child.readable()
    if what == "outline":
        if mark is not None:
            return _segments(child, what, indent + mark.get("depth", 1)) if readable else []
        line = ("  " * indent + f"frame {name} -> {url} (its element was not found in the page"
                + (f"; not read: {child.why}" if child.why else "") + ")")
        return [[line]] + (_segments(child, what, indent + 1) if readable else [])
    if what in ("links", "forms"):
        return _segments(child, what, indent) if readable else []
    held = mark.get("name") if mark is not None and mark.get("name") else (
        "its element was not found in the page")
    if what == "text":
        head = f"\n\n[frame {name}: {held} -> {url}]"
        if not readable:
            return [head + f" not read: {child.why}\n"]
        return [head + "\n", *_segments(child, what, indent), f"\n[end of frame {name}]"]
    head = ("" if mark is not None else "\n\n") + f"[frame {name} -> {url}]"
    if not readable:
        return [head + f" (not read: {child.why})\n\n"]
    return [head + "\n\n", *_segments(child, what, indent), f"\n\n[end of frame {name}]\n\n"]


def _segments(node: Node, what: str, indent: int = 0) -> list:
    """The document's read laid out with its frames': _Doc parts, and
    what Flux says between them (str for characters, [lines] for the
    outline)."""
    info = node.info or {}
    total = info.get("total") or 0
    out, pos, placed = [], 0, set()
    for mark in sorted(info.get("frames") or (), key=lambda m: m.get("at", 0)):
        child = node.child(mark.get("index"))
        if child is None or id(child) in placed:
            continue
        placed.add(id(child))
        at = min(max(int(mark.get("at", total)), pos), total)
        if at > pos:
            out.append(_Doc(node, pos, at, indent))
            pos = at
        out += _around(child, what, mark, indent)
    if pos < total:
        out.append(_Doc(node, pos, total, indent))
    known = set(info.get("known") or ())
    for child in node.children:
        if id(child) not in placed and (child.index is None or child.index not in known):
            out += _around(child, what, None, indent)
    return out


def _lines(node: Node, lines: list, indent: int) -> list:
    pad = "  " * indent
    return [pad + TOKEN.sub(lambda m: _status(node, m[1]), line) for line in lines]


async def _compose(job, root: Node, what: str, opts: dict) -> dict:
    """The second pass: the window asked of the page laid out whole."""
    segs = _segments(root, what)
    total = sum(_size(s) for s in segs)
    offset, length = opts["offset"], opts["length"]
    picks, pos = [], 0
    for seg in segs:
        n = _size(seg)
        lo, hi = max(offset, pos), min(offset + length, pos + n)
        if lo < hi:
            picks.append((seg, lo - pos, hi - pos))
        pos += n
    docs = [(s, lo, hi) for s, lo, hi in picks if isinstance(s, _Doc)]
    calls = [(s.node.target, reader.script(what, {**opts, "offset": s.a + lo, "length": hi - lo},
                                           frame=s.node.name)) for s, lo, hi in docs]
    answers = await job.many(calls, READ_S) if calls else []
    got = {}
    for (s, lo, hi), raw in zip(docs, answers):
        try:
            got[id(s)] = reader.answer(raw)
        except reader.ScriptError:
            got[id(s)] = None
    parts = []
    for seg, lo, hi in picks:
        if not isinstance(seg, _Doc):
            parts.append(seg[lo:hi])
            continue
        value = got.get(id(seg))
        if value is None:
            missing = f"(frame {seg.node.name} did not answer)" if seg.node.name else \
                "(the page did not answer)"
            parts.append(missing if what in CHARS else ["  " * seg.indent + missing]
                         if what == "outline" else [])
            continue
        piece = value.get(what) or ([] if what not in CHARS else "")
        parts.append(_lines(seg.node, piece, seg.indent) if what == "outline" else piece)
    body = "".join(parts) if what in CHARS else [item for p in parts for item in p]
    end = min(offset + length, total)
    more = end < total
    out = {what: body, "offset": offset, "total": total, "more": more,
           "next": end if more else None}
    if what == "outline":
        out["cut"] = _any_cut(root)
    if what == "article":
        out["found"] = (root.info or {}).get("found") or ""
    return out


def _any_cut(node: Node) -> bool:
    return bool((node.info or {}).get("cut")) or any(_any_cut(c) for c in node.children
                                                    if c.readable())


def summary(root: Node) -> list[dict]:
    """FRAME, for each frame met (protocol.py)."""
    out = []

    def walk(node: Node) -> None:
        for c in node.children:
            entry = {"frame": c.name or None, "in": node.name or None, "url": c.shown_url,
                     "read": c.readable()}
            if c.why:
                entry["why"] = c.why
            out.append(entry)
            walk(c)
    walk(root)
    return out


async def read(job, tab, what: str, opts: dict, kept) -> dict:
    """A read (reader.js `what`) of the page with its frames: the window
    `opts` asks for, and FRAME for each frame met."""
    root = await survey(job, tab, what, opts, kept)
    info = dict(root.info or {})
    for key in ("frames", "known", "length"):
        info.pop(key, None)
    extra = {"frames_more": root.skipped} if root.skipped else {}
    if not root.children:
        if what == "outline":
            info["outline"] = _lines(root, info.get("outline") or [], 0)
        return {**info, "frames": [], **extra}
    return {**await _compose(job, root, what, opts), "frames": summary(root), **extra}


# ── acting ──────────────────────────────────────────────────────────────
async def path(job, tab, n: int, kept) -> list[Node]:
    """Frame f<n> and the frames it is in, from the main page's down,
    each with its place in the one above: every frame asked its name
    and place at once. FrameError: gone, kept, or not answering."""
    page = tab.page
    if not supported(page):
        raise FrameError("frames are reached with Qt 6.8 on; this Flux runs an older Qt")
    tree = _tree(tab)
    answers = await job.many([(f, reader.script("whoami", frame=offer)) for (f, _p), offer
                              in zip(tree, _offers(tab, len(tree)))], ANSWER_S) if tree else []
    mes = [_me(raw) for raw in answers]
    _taken(tab, [me.get("name") for me in mes if me])
    want = protocol.frame_name(n)
    at = next((i for i, me in enumerate(mes) if me and me.get("name") == want), None)
    if at is None:
        raise FrameError(f"no frame {want} in this page now (a frame's refs go with its "
                         "document: read the page again)")
    chain = []
    while at is not None:
        chain.append(at)
        at = tree[at][1]
    nodes, parent = [], None
    for i in reversed(chain):
        node = _node(tree[i][0], parent)
        me = mes[i] or {}
        node.number, node.index = _number(me.get("name")), me.get("index")
        if me is None or node.index is None:
            raise FrameError(f"frame {node.label} could not say where it is in the page")
        if kept(node.host):
            raise FrameError(protocol.kept(node.host))
        nodes.append(node)
        parent = node
    return nodes


async def to_page(job, tab, nodes: list[Node], x: float, y: float, cover: bool) -> dict:
    """The point (x, y) of the last frame's viewport in the page's, each
    frame's element brought into sight in the one above: {"x", "y"}, or
    {"covered": what covers it, "x", "y", "frame"} where that is."""
    for i in range(len(nodes) - 1, -1, -1):
        node = nodes[i]
        above = nodes[i - 1] if i else None
        got = await job.read(above.target if above else tab.page, reader.script(
            "framePoint", {"index": node.index, "x": x, "y": y, "cover": cover},
            frame=above.name if above else ""))
        if got.get("covered"):
            return {"covered": got["covered"], "x": got["x"], "y": got["y"], "frame": node.name}
        x, y = got["x"], got["y"]
    return {"x": x, "y": y}


async def child_at(job, tab, parent: Node | None, index: int, kept) -> Node:
    """The frame that is window `index` of `parent` (the main page for
    None): a point over its element goes on in it."""
    above = parent.frame if parent is not None else tab.page.mainFrame()
    nodes = [_node(f, parent) for f in above.children() if f.isValid()]
    answers = await job.many([(node.target, reader.script("whoami", frame=offer))
                              for node, offer in zip(nodes, _offers(tab, len(nodes)))],
                             ANSWER_S) if nodes else []
    mes = [_me(raw) for raw in answers]
    _taken(tab, [me.get("name") for me in mes if me])
    for node, me in zip(nodes, mes):
        if me is not None and me.get("index") == index:
            node.index, node.number = index, _number(me.get("name"))
            if kept(node.host):
                raise FrameError(protocol.kept(node.host))
            return node
    raise FrameError("the frame under the point did not answer")


# ── the agent's scripts (script.py) ─────────────────────────────────────
async def guard_all(job, tab, code: str, world: int) -> None:
    """`code` (the guard) run in `world` of every frame of the page."""
    frames = descendants(tab)
    if frames:
        await job.many([(f, code) for f in frames], ANSWER_S, world=world)


async def scrub(job, tab, text: str, kept) -> str | None:
    """`text` with every password field's value in it taken out, in the
    reader's world of each document that has one (reader.js `scrub`):
    the values never leave the page. None when it cannot be done: the
    main page did not answer, or a document holding a password stopped
    answering (the page left for another document meanwhile), and then
    nothing of `text` may go out. A frame that does not answer at all
    is passed over (its fields are out of the agent's script's reach,
    the guard not got around)."""
    targets = [tab.page, *[f for f in descendants(tab) if not kept(f.url().host())]]
    has = await job.many([(t, reader.script("hasSecrets", protocol.JS_SCRUB_MIN))
                          for t in targets], ANSWER_S)
    for i, (target, raw) in enumerate(zip(targets, has)):
        try:
            holds = reader.answer(raw)
        except reader.ScriptError:
            if i == 0:
                return None
            continue
        if holds:
            text = await job.read(target, reader.script("scrub", text, protocol.JS_SCRUB_MIN),
                                  timeout=ANSWER_S, quiet=True)
            if not isinstance(text, str):
                return None
    return text
