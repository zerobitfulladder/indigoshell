"""Fuzzy matching: fzy's scorer, with the positions it matched.

A query matches a text when its characters appear in the text in order,
not necessarily together — "vsc" matches "Visual Studio Code". Among
the ways a query can land in a text the scorer picks the best one,
rewarding characters that start a word or follow the previous match
and charging a little for every character skipped.

The algorithm and its constants are fzy's (github.com/jhawthorn/fzy,
MIT): two dynamic-programming rows per query character, D for "best
score with this character matched here" and M for "best score so far",
then a backtrack through D for the positions. Ranking across *fields*
and launch history is the caller's business; this module only answers
"how well does this one string match".

Both sides are folded first — case and diacritics dropped, so typing
"s" finds "ş" and "i" finds "İ" and "ı" — and the positions are mapped
back to the unfolded text, which is what a highlight has to index.
"""

import unicodedata
from typing import NamedTuple

GAP_LEADING = -0.005
GAP_TRAILING = -0.005
GAP_INNER = -0.01
MATCH_CONSECUTIVE = 1.0
MATCH_SLASH = 0.9
MATCH_WORD = 0.8
MATCH_CAPITAL = 0.7
MATCH_DOT = 0.6

# Matching the whole text is better than any sum of bonuses can be.
EXACT = 1000.0

# Longer texts are not scored, only rejected — the DP is quadratic, and
# nothing a launcher shows is this long.
MAX_LEN = 256

_NEG = float("-inf")

# Letters with no decomposition that a user typing on a plain layout
# would still expect to find. NFKD splits "ş" into "s" + cedilla, but
# dotless ı is its own letter.
_FOLD_EXTRA = {"ı": "i", "ø": "o", "đ": "d", "ł": "l", "ħ": "h", "ŧ": "t"}


class Match(NamedTuple):
    score: float
    positions: tuple[int, ...]      # indices into the unfolded text


def fold(text: str) -> tuple[str, tuple[int, ...]]:
    """`text` without case or diacritics, and for each folded character
    the index of the original character it came from.

    One character can fold to several ("ß" -> "ss", "ﬁ" -> "fi") or to
    none (a lone combining mark), which is why the map is needed rather
    than assuming the two strings line up.
    """
    out: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        for part in unicodedata.normalize("NFKD", ch.casefold()):
            if unicodedata.combining(part):
                continue
            part = _FOLD_EXTRA.get(part, part)
            out.append(part)
            origin.append(i)
    return "".join(out), tuple(origin)


def _bonus(prev: str, ch: str) -> float:
    if not ch.isalnum():
        return 0.0
    if prev == "/":
        return MATCH_SLASH
    if prev in "-_ ":
        return MATCH_WORD
    if prev == ".":
        return MATCH_DOT
    if prev.islower() and ch.isupper():
        return MATCH_CAPITAL
    return 0.0


def _is_subsequence(needle: str, hay: str) -> bool:
    at = 0
    for ch in needle:
        at = hay.find(ch, at)
        if at < 0:
            return False
        at += 1
    return True


class Text:
    """A text prepared once for matching against many queries — the
    fold and the per-character bonuses depend only on the text."""

    __slots__ = ("raw", "folded", "origin", "bonus")

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.folded, self.origin = fold(raw)
        # Bonuses read the *unfolded* neighbours: folding throws away
        # the case that marks a camelCase boundary. The start of the
        # text counts as following a slash, as in fzy.
        bonus = []
        for k, i in enumerate(self.origin):
            if k and self.origin[k - 1] == i:
                bonus.append(0.0)       # 2nd half of one folded character
            else:
                bonus.append(_bonus(raw[i - 1] if i else "/", raw[i]))
        self.bonus = tuple(bonus)


def match(query: str, text: Text) -> Match | None:
    """Score `query` (already folded) against `text`; None if it does
    not occur in order."""
    hay = text.folded
    n, m = len(query), len(hay)
    if not n or n > m or m > MAX_LEN or not _is_subsequence(query, hay):
        return None
    if n == m:
        # Folded equality: every character matched, in place.
        return Match(EXACT, tuple(sorted(set(text.origin))))

    bonus = text.bonus
    D = [[_NEG] * m for _ in range(n)]
    M = [[_NEG] * m for _ in range(n)]
    for i, qc in enumerate(query):
        gap = GAP_TRAILING if i == n - 1 else GAP_INNER
        prev = _NEG
        d_row, m_row = D[i], M[i]
        d_last = D[i - 1] if i else None
        m_last = M[i - 1] if i else None
        for j, hc in enumerate(hay):
            if qc == hc:
                if i == 0:
                    score = j * GAP_LEADING + bonus[j]
                elif j:
                    score = max(m_last[j - 1] + bonus[j],
                                d_last[j - 1] + MATCH_CONSECUTIVE)
                else:
                    score = _NEG
                d_row[j] = score
                prev = max(score, prev + gap)
            else:
                prev = prev + gap
            m_row[j] = prev

    # Backtrack: walk each query character right to left, preferring
    # the cell where matching here *was* the best score, and following
    # a consecutive run once one was taken.
    positions = [0] * n
    must_match = False
    j = m - 1
    for i in range(n - 1, -1, -1):
        while j >= 0:
            if D[i][j] != _NEG and (must_match or D[i][j] == M[i][j]):
                must_match = bool(i and j and
                                  M[i][j] == D[i - 1][j - 1] + MATCH_CONSECUTIVE)
                positions[i] = j
                j -= 1
                break
            j -= 1

    origin = text.origin
    return Match(M[n - 1][m - 1],
                 tuple(sorted({origin[p] for p in positions})))
