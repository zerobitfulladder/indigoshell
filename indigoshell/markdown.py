"""A small Markdown reader for text the shell draws itself.

Not CommonMark — the subset a chat reply actually uses, read forgivingly
because the text may be half-streamed: an unclosed fence runs to the end,
an unclosed `**` is just two asterisks.

The result is a list of `Block`s, each holding *plain* text (markers
stripped) and the style `spans` over it. Keeping the text plain is what
lets the caller wrap it by character count on a monospace grid, select
it by column and copy it without the asterisks.

Single newlines are kept as line breaks rather than joined, since a
reply that puts a command on its own line means it.
"""

import re
from dataclasses import dataclass, field

BULLET = "•"        # an unordered item's mark; the call paints it as a square light

_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


@dataclass
class Span:
    start: int
    end: int
    style: str          # "b" bold, "i" italic, "bi" both, "code", "link",
                        # "s" strike
    url: str = ""       # a link's target


@dataclass
class Block:
    kind: str           # p h li quote code rule table blank
    text: str = ""
    spans: list[Span] = field(default_factory=list)
    level: int = 0      # heading level, or list nesting depth
    bullet: str = ""    # BULLET or "3." for list items
    lang: str = ""      # a code block's info string


def parse(source: str) -> list[Block]:
    blocks: list[Block] = []
    lines = source.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            mark, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(mark[:3]):
                body.append(lines[i])
                i += 1
            i += 1                      # the closing fence, if any
            blocks.append(Block("code", "\n".join(body), lang=lang))
            continue
        if not line.strip():
            if blocks and blocks[-1].kind != "blank":
                blocks.append(Block("blank"))
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            text, spans = inline(m.group(2))
            blocks.append(Block("h", text, spans, level=len(m.group(1))))
        elif _RULE.match(line):
            blocks.append(Block("rule"))
        elif line.lstrip().startswith("|"):
            if not _TABLE_SEP.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                text, spans = inline("  │  ".join(cells))
                blocks.append(Block("table", text, spans))
        elif (m := _ITEM.match(line)) is not None:
            depth = len(m.group(1)) // 2
            mark = m.group(2)
            bullet = BULLET if mark in "-*+" else mark.rstrip(")").rstrip(".") + "."
            text, spans = inline(m.group(3))
            blocks.append(Block("li", text, spans, level=depth, bullet=bullet))
        elif (m := _QUOTE.match(line)) is not None:
            text, spans = inline(m.group(1))
            blocks.append(Block("quote", text, spans))
        else:
            text, spans = inline(line.strip())
            blocks.append(Block("p", text, spans))
        i += 1
    while blocks and blocks[-1].kind == "blank":
        blocks.pop()
    return blocks


def _closer(src: str, marker: str, start: int) -> int:
    """Index of a closing `marker` after `start` that follows a non-space,
    or -1. A single `*`/`_` does not close on half of a doubled one."""
    j = src.find(marker, start)
    while j != -1:
        doubled = len(marker) == 1 and src.startswith(marker * 2, j)
        if j > start and not src[j - 1].isspace() and not doubled:
            return j
        j = src.find(marker, j + (2 if doubled else 1))
    return -1


# An address in the text: <https://…> or a bare https://…, which stops
# before trailing punctuation that is part of the sentence, not the URL.
_BARE_URL = re.compile(r"<(https?://[^>\s]+)>|(https?://[^\s<>]*[^\s<>.,;:!?'\")\]])")

_EMPHASIS = (("***", "bi"), ("**", "b"), ("__", "b"), ("~~", "s"),
             ("*", "i"), ("_", "i"))


def _emphasis(src: str, i: int) -> tuple[str, str, int, int] | None:
    """(marker, style, inner start, closer index) for an emphasis run
    opening at `i`, or None if the character there is literal."""
    n = len(src)
    for marker, style in _EMPHASIS:
        if not src.startswith(marker, i):
            continue
        if len(marker) == 3 and src.find(marker, i + 3) == -1:
            continue                    # "***" only pairs with "***"
        after = i + len(marker)
        if after >= n or src[after].isspace():
            return None                 # "2 * 3", a stray star: literal
        word_marker = marker[0] == "_"
        if word_marker and i > 0 and src[i - 1].isalnum():
            return None                 # snake_case, __init__
        j = _closer(src, marker, after)
        if j == -1:
            return None
        if word_marker and j + len(marker) < n \
                and src[j + len(marker)].isalnum():
            return None
        return marker, style, after, j
    return None


def inline(src: str) -> tuple[str, list[Span]]:
    """Plain text and style spans for one line of inline Markdown."""
    out: list[str] = []
    spans: list[Span] = []
    pos = 0

    def emit(s: str) -> None:
        nonlocal pos
        out.append(s)
        pos += len(s)

    def nested(inner_src: str, style: str, url: str = "") -> None:
        inner, inner_spans = inline(inner_src)
        start = pos
        emit(inner)
        spans.extend(Span(x.start + start, x.end + start, x.style, x.url)
                     for x in inner_spans)
        spans.append(Span(start, pos, style, url))

    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and not src[i + 1].isalnum():
            emit(src[i + 1])
            i += 2
            continue
        if c == "`":
            ticks = len(src[i:]) - len(src[i:].lstrip("`"))
            j = src.find("`" * ticks, i + ticks)
            if j != -1:
                code = src[i + ticks:j]
                start = pos
                emit(code.strip() if ticks > 1 else code)
                spans.append(Span(start, pos, "code"))
                i = j + ticks
                continue
        if c == "[":
            m = re.match(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)",
                         src[i:])
            if m:
                nested(m.group(1), "link", m.group(2))
                i += m.end()
                continue
        if c in "hH<" and (i == 0 or not src[i - 1].isalnum()):
            # A bare address, or one in <angle brackets>, is a link too.
            m = _BARE_URL.match(src, i)
            if m:
                url = m.group(1) or m.group(2)
                start = pos
                emit(url)
                spans.append(Span(start, pos, "link", url))
                i = m.end()
                continue
        if c in "*_~":
            run = _emphasis(src, i)
            if run is not None:
                marker, style, after, j = run
                nested(src[after:j], style)
                i = j + len(marker)
                continue
        emit(c)
        i += 1
    return "".join(out), spans
