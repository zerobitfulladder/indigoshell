"""For niri: columns like qtile's `Columns` layout (two columns, fair
off, split on), driven over niri's IPC.

    python3 -m indigoshell.wm.tiling                the helper
    python3 -m indigoshell.wm.tiling grow DIRECTION one resize (a key)

niri opens every window as a new column on its scrolling strip, at the
config's default width. This keeps each workspace to qtile's shape
instead, tiled windows only (floating ones are left alone):

- one column fills the screen; two share it half and half (more, made
  by hand, share it evenly);
- a window arriving on a workspace that already has two columns (opened
  there, moved there, or made tiled) goes into the column that was
  focused, stacked under it, as qtile adds it to the current column;
- widths are set only when a workspace's number of columns changes (or
  a new column takes a gone one's place between two looks), so a column
  resized by hand (Mod+Ctrl+H/L) stays so until then.

`grow left|right|up|down` is qtile's grow_left/right/up/down for niri's
Mod+Ctrl+H/L/K/J, one resize per run, in steps of qtile's: the columns
keep filling the screen, so left and right move the split between the
focused column and its neighbour (the last column moves the split on its
left, the first the one on its right: right always moves a split right),
and down and up make the focused window taller and shorter in its column,
the others sharing the rest. A lone column or window stays as it is.

Everything goes through actions that name a window by id
(`ConsumeOrExpelWindowLeft`, `SetWindowWidth`), so focus and the pointer
stay where they are. niri puts a new window's column right after the
focused one, so the column on the new window's left is the one that was
focused.

Standard library only: niri starts it from its config (`spawn-sh-at-
startup`) before the shell runs there. It follows niri's event stream,
looks at the windows afresh once a burst of events has settled, and
ends when niri closes the stream.
"""

import asyncio
import logging
import os
import sys

from . import WMError
from .niri import Ipc

log = logging.getLogger("indigoshell.wm.tiling")

MAX_COLUMNS = 2
SETTLE_S = 0.03                 # quiet after an event before looking
ROUNDS = 8                      # consume, look again; at most this often
_LAYOUT_EVENTS = {"WindowOpenedOrChanged", "WindowClosed", "WindowsChanged",
                  "WindowLayoutsChanged", "WorkspacesChanged"}


def columns(windows, workspace_id: int) -> dict[int, list[dict]]:
    """A workspace's tiled windows by column index (1-based), each
    column top to bottom."""
    cols: dict[int, list[dict]] = {}
    for w in windows:
        pos = (w.get("layout") or {}).get("pos_in_scrolling_layout")
        if w.get("workspace_id") != workspace_id or w.get("is_floating") \
                or not pos:
            continue
        cols.setdefault(pos[0], []).append(w)
    for col in cols.values():
        col.sort(key=lambda w: w["layout"]["pos_in_scrolling_layout"][1])
    return dict(sorted(cols.items()))


def width_requests(cols: dict[int, list[dict]]) -> list[dict]:
    """Every column at 1/n of the screen, by one window of each."""
    share = 100.0 / len(cols)
    return [{"Action": {"SetWindowWidth": {
        "id": col[0]["id"], "change": {"SetProportion": share}}}}
        for col in cols.values()]


STEP = 5.0                      # percent per resize: qtile's grow_amount of 10
                                # on two columns' (or windows') 100 each
LEAST = 10.0                    # nothing resized below this share


def _shares(sizes: list[float]) -> list[float]:
    total = sum(sizes) or 1.0
    return [size / total * 100.0 for size in sizes]


def _snap(share: float) -> float:
    return round(share / STEP) * STEP


def grow_requests(windows, window_id: int, direction: str) -> list[dict]:
    """The requests for `grow DIRECTION` on window `window_id` (the
    module docstring): none for a floating window, a lone column (left,
    right) or a window alone in its column (up, down)."""
    win = next((w for w in windows if w.get("id") == window_id), None)
    pos = (win or {}).get("layout", {}).get("pos_in_scrolling_layout")
    if win is None or win.get("is_floating") or not pos:
        return []
    cols = columns(windows, win["workspace_id"])
    if direction in ("left", "right"):
        keys = list(cols)
        if len(keys) < 2:
            return []
        shares = _shares([cols[k][0]["layout"]["tile_size"][0] for k in keys])
        i = keys.index(pos[0])
        if direction == "right":
            left, delta = (i if i < len(keys) - 1 else i - 1), STEP
        else:
            left, delta = (i - 1 if i > 0 else i), -STEP
        pair = shares[left] + shares[left + 1]
        new = min(max(_snap(shares[left] + delta), LEAST), pair - LEAST)
        if abs(new - shares[left]) < 0.01:
            return []
        return [{"Action": {"SetWindowWidth": {"id": cols[keys[k]][0]["id"],
                                               "change": {"SetProportion": share}}}}
                for k, share in ((left, new), (left + 1, pair - new))]
    if direction in ("up", "down"):
        col = cols.get(pos[0], [])
        if len(col) < 2:
            return []
        shares = _shares([w["layout"]["tile_size"][1] for w in col])
        j = next(n for n, w in enumerate(col) if w["id"] == window_id)
        most = 100.0 - LEAST * (len(col) - 1)
        new = min(max(_snap(shares[j] + (STEP if direction == "down" else -STEP)),
                      LEAST), most)
        if abs(new - shares[j]) < 0.01:
            return []
        # The others back to sharing what is left, so the column stays full.
        return [*({"Action": {"ResetWindowHeight": {"id": w["id"]}}}
                  for w in col if w["id"] != window_id),
                {"Action": {"SetWindowHeight": {"id": window_id,
                                                "change": {"SetProportion": new}}}}]
    raise ValueError(f"no such direction: {direction}")


async def grow(ipc: Ipc, direction: str) -> None:
    """One `grow DIRECTION` on the focused window."""
    focused = await ipc.query("FocusedWindow")
    if not focused:
        return
    for req in grow_requests(await ipc.query("Windows") or [], focused["id"], direction):
        await ipc.request(req)


class Layout:
    """What has been laid out: each workspace's column count and its
    tiled windows, as last seen. `decide` compares a fresh look at the
    windows with it."""

    def __init__(self) -> None:
        self.counts: dict[int, int] = {}
        self.tiled: dict[int, set[int]] = {}
        self.started = False

    def decide(self, windows) -> tuple[list[dict], bool]:
        """The requests to send now, and whether that settles it (False:
        a window was moved into a column; look again before widths)."""
        spaces = {w.get("workspace_id") for w in windows} - {None}
        if not self.started:
            # What is there already was laid out by someone: keep it.
            self.started = True
            for ws in spaces:
                cols = columns(windows, ws)
                self.counts[ws] = len(cols)
                self.tiled[ws] = {w["id"] for c in cols.values() for w in c}
            return [], True
        looks = {ws: columns(windows, ws) for ws in sorted(spaces)}
        # First a window that arrived as a column too many, one at a time:
        # the layout changes under it, so look again after.
        for ws, cols in looks.items():
            if len(cols) <= MAX_COLUMNS:
                continue
            new = {w["id"] for c in cols.values() for w in c} \
                - self.tiled.get(ws, set())
            lone = [(i, col[0]) for i, col in cols.items()
                    if len(col) == 1 and col[0]["id"] in new]
            if lone:
                index, win = max(lone, key=lambda p: p[0])
                side = "Left" if index > 1 else "Right"
                log.info("window %s on workspace %s: into the column on "
                         "its %s", win["id"], ws, side.lower())
                return [{"Action": {f"ConsumeOrExpelWindow{side}":
                                    {"id": win["id"]}}}], False
        # Then the widths, where the number of columns changed — or a
        # column of none but new windows stands where another went, both
        # between two looks (a window closing as another opens).
        out: list[dict] = []
        for ws, cols in looks.items():
            before = self.tiled.get(ws, set())
            fresh = any(all(w["id"] not in before for w in col)
                        for col in cols.values())
            if cols and (len(cols) != self.counts.get(ws, 0) or fresh):
                log.info("workspace %s: %d column(s)", ws, len(cols))
                out += width_requests(cols)
            self.counts[ws] = len(cols)
            self.tiled[ws] = {w["id"] for c in cols.values() for w in c}
        for gone in set(self.counts) - spaces:
            self.counts.pop(gone, None)
            self.tiled.pop(gone, None)
        return out, True


class Tiler:
    def __init__(self, ipc: Ipc) -> None:
        self.ipc = ipc
        self.layout = Layout()
        self._dirty = asyncio.Event()

    async def settle(self) -> None:
        for _ in range(ROUNDS):
            windows = await self.ipc.query("Windows") or []
            requests, done = self.layout.decide(windows)
            for req in requests:
                try:
                    await self.ipc.request(req)
                except WMError as e:
                    log.warning("%s: %s", req, e)
            if done:
                return
        log.warning("windows kept arriving; leaving the rest as it is")

    async def _worker(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(SETTLE_S)
            self._dirty.clear()
            try:
                await self.settle()
            except WMError as e:
                log.warning("%s", e)

    async def run(self) -> None:
        worker = asyncio.get_running_loop().create_task(self._worker())
        try:
            await self.settle()             # what is there now, as it is
            async for event in self.ipc.events():
                if _LAYOUT_EVENTS & event.keys():
                    self._dirty.set()
        finally:
            worker.cancel()


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="indigoshell tiling: %(message)s")
    path = os.environ.get("NIRI_SOCKET")
    if not path:
        log.error("NIRI_SOCKET is not set: this runs inside niri")
        return 2
    args = sys.argv[1:]
    if args:
        if len(args) != 2 or args[0] != "grow" or args[1] not in ("left", "right", "up", "down"):
            log.error("usage: python3 -m indigoshell.wm.tiling [grow left|right|up|down]")
            return 2
        try:
            asyncio.run(grow(Ipc(path), args[1]))
        except WMError as e:
            log.error("%s", e)
            return 1
        return 0
    try:
        asyncio.run(Tiler(Ipc(path)).run())
    except WMError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        pass
    log.info("niri closed its event stream; done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
