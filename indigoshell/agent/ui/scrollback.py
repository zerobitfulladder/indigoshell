"""Which exchange the call shows: the live one, or an earlier one.

The call pages through the chat an exchange at a time, both plates
together, in their usual layout: Ctrl+K puts the exchange before on
them (your plate its lines, hers its reply), Ctrl+J the one after, and
past the newest earlier one it is live again. Past the oldest she still
holds there is one more stop, the edge: nothing older on the plates, a
note saying where the rest is (her memory, or the chat's start).

The exchanges are `RemoteAgent.earlier()`, fetched a page at a time;
the next page is asked for while the oldest are still a few Ctrl+Ks
away. Where the call is is kept as the exchange's id, so a page that
arrives above it, or one forgotten under it, moves nothing else.
"""

EDGE = "edge"               # past the oldest exchange she holds
PREFETCH = 5                # ask for older ones this many exchanges early


class Scrollback:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.back: int | str | None = None  # an exchange's id, EDGE, None: live

    def live(self) -> None:
        self.back = None

    def page(self) -> dict | str | None:
        """The exchange on the plates (one of `earlier()`), EDGE, or None
        while live. One forgotten or compacted away gives way to the
        next newer one, or to live."""
        if self.back is None or self.back == EDGE:
            return self.back
        earlier = self.agent.earlier()
        for x in earlier:
            if x["id"] == self.back:
                return x
        newer = [x for x in earlier if x["id"] > self.back]
        self.back = newer[0]["id"] if newer else None
        return newer[0] if newer else None

    def _index(self, earlier: list[dict]) -> int:
        """Where the plates are in `earlier`: len for live, -1 the edge."""
        page = self.page()
        if page is None:
            return len(earlier)
        if page == EDGE:
            return -1
        return next(i for i, x in enumerate(earlier) if x["id"] == page["id"])

    def older(self) -> bool:
        """Ctrl+K: the exchange before; past the oldest she holds, the
        edge. Whether the plates changed. At the oldest one here with
        more on the hub, the page is asked for and the plates stay."""
        a = self.agent
        earlier = a.earlier()
        i = self._index(earlier)
        if i < 0:
            return False
        if i > 0:
            self.back = earlier[i - 1]["id"]
            if i - 1 < PREFETCH:
                a.want_older()
            return True
        if a.more_back or a.back_loading:
            a.want_older()
            return False
        self.back = EDGE
        return True

    def newer(self) -> bool:
        """Ctrl+J: the exchange after; after the newest earlier one,
        live. Whether the plates changed."""
        earlier = self.agent.earlier()
        i = self._index(earlier)
        if i >= len(earlier):
            return False
        nxt = earlier[i + 1] if i + 1 < len(earlier) else None
        self.back = nxt["id"] if nxt is not None else None
        return True

    def count(self) -> int:
        """How far back the plates are: 1 for the exchange before the
        turn on show; 0 live or at the edge."""
        earlier = self.agent.earlier()
        i = self._index(earlier)
        return len(earlier) - i if 0 <= i < len(earlier) else 0
