"""The process's single X11 connection.

One connection is shared by every window: X resource IDs are allocated
client-side out of a per-connection range, so windows created on
different connections can't reference each other's colormaps, GCs or
grabs. The daemon owns this object; window kinds borrow it.

The event pump is deliberately loop-agnostic — it drains libxcb's queue
and dispatches, and knows nothing about asyncio. `Daemon` wires `fd`
into the loop with `add_reader`.
"""

import logging
import struct
from dataclasses import dataclass

import xcffib
import xcffib.randr
import xcffib.shape
import xcffib.xproto as xp


def shape_ext(conn):
    """The SHAPE extension, or None if the server lacks it."""
    try:
        return conn(xcffib.shape.key)
    except Exception:
        return None

log = logging.getLogger(__name__)


class DisplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    name: str = ""


def _event_window(ev) -> int | None:
    """Which window an event belongs to.

    Input events (Key/Button/Motion/Enter/Leave) carry the window that
    received them in `.event`; structure and exposure events carry it in
    `.window`. Order matters: ConfigureNotify has both, and `.event` is
    the window whose StructureNotify mask selected it — the one that
    asked to hear about it.
    """
    wid = getattr(ev, "event", None)
    if wid is not None:
        return wid
    return getattr(ev, "window", None)


class Display:
    def __init__(self) -> None:
        try:
            self.conn = xcffib.connect()
        except Exception as e:  # no DISPLAY, unreachable server, auth failure
            raise DisplayError(f"cannot connect to X server: {e}") from e

        # Detectable auto-repeat: a held key then repeats KeyPress alone,
        # with one KeyRelease at the true release. Without it the server
        # synthesises release/press pairs while a key is held, and a
        # release-to-fire menu would fire on the first synthetic one.
        try:
            import xcffib.xkb as xkb
            ext = self.conn(xkb.key)
            ext.UseExtension(1, 0).reply()
            ext.PerClientFlags(xkb.ID.UseCoreKbd,
                               xkb.PerClientFlag.DetectableAutoRepeat, 1,
                               0, 0, 0).reply()
        except Exception:
            log.debug("XKB detectable auto-repeat unavailable", exc_info=True)

        self.setup = self.conn.get_setup()
        self.screen = self.setup.roots[0]
        self.root = self.screen.root

        self._atoms: dict[str, int] = {}
        self._handlers: dict[int, object] = {}
        self._pumping = False
        self._loop = None
        self._drain_queued = False
        self._closed = False
        # Events for windows we don't (or no longer) own — an unmapped
        # window can still have queued events. Logged at debug rather
        # than dropped silently, since a widget that "doesn't respond to
        # clicks" usually shows up here first.
        self.on_unhandled = None
        self._root_listeners: list = []
        self._screen_listeners: list = []
        self._keysyms: dict[int, int] | None = None

    # ── connection ──────────────────────────────────────────────────────
    @property
    def fd(self) -> int:
        return self.conn.get_file_descriptor()

    def attach_loop(self, loop) -> None:
        self._loop = loop

    def flush(self) -> None:
        if self._closed:
            return
        self.conn.flush()
        self.wake()

    def wake(self) -> None:  # noqa: D401 - see below
        """Queue a drain for the next loop iteration.

        libxcb reads from the socket while *writing* a large request, so
        it doesn't deadlock when the server's buffer fills. A bar frame is
        a ~430KB PutImage, so any flush can quietly pull pending events
        into libxcb's queue and leave the fd unreadable — epoll then never
        fires and the event sits there until an unrelated one arrives.
        That is exactly the "click does nothing until I move the mouse"
        failure.

        Deferred via call_soon rather than drained inline: flush() is
        reached from inside redraw, and dispatching events there would
        re-enter painting.
        """
        if self._loop is None or self._drain_queued or self._closed:
            return
        self._drain_queued = True
        self._loop.call_soon(self.drain)

    def drain(self) -> None:
        self._drain_queued = False
        # Teardown flushes (DestroyWindow, UngrabPointer), each of which
        # queues a drain — but the connection is gone by the time these
        # callbacks run.
        if self._closed:
            return
        total = 0
        while True:
            count = self.pump()
            if not count:
                break
            total += count
        if total:
            log.debug("deferred drain dispatched %d event(s) the fd never "
                      "signalled", total)

    def close(self) -> None:
        self._closed = True
        try:
            self.conn.disconnect()
        except Exception:
            pass

    # ── atoms ───────────────────────────────────────────────────────────
    def atom(self, name: str) -> int:
        """Intern an atom, cached. Round-trips once per new name."""
        cached = self._atoms.get(name)
        if cached is not None:
            return cached
        raw = name.encode()
        value = self.conn.core.InternAtom(False, len(raw), raw).reply().atom
        self._atoms[name] = value
        log.debug("interned atom %s -> %d", name, value)
        return value

    # ── event routing ───────────────────────────────────────────────────
    def on(self, wid: int, handler) -> None:
        log.debug("routing events for window 0x%x", wid)
        self._handlers[wid] = handler

    def off(self, wid: int) -> None:
        if self._handlers.pop(wid, None) is not None:
            log.debug("stopped routing events for window 0x%x", wid)

    def pump(self) -> int:
        """Drain every queued event and dispatch it. Returns the count.

        Must be called after any code path that does a round-trip
        (`.reply()`), not only when the socket is readable: reading a
        reply pulls bytes off the wire, and any events that arrive in
        the same read land in libxcb's queue with the fd left clean. An
        add_reader-only pump would leave those events sitting there
        until the *next* unrelated event woke the loop — which is what
        made a click appear to do nothing until the mouse moved.

        Re-entrant calls return 0 immediately: handlers open windows,
        opening a window interns atoms, and interning round-trips, so
        this would otherwise recurse into itself mid-dispatch and
        deliver events out of order. The outer loop drains what's left.
        """
        if self._pumping or self._closed:
            return 0
        self._pumping = True
        count = 0
        try:
            while True:
                try:
                    ev = self.conn.poll_for_event()
                except xcffib.Error:
                    # Errors for unchecked requests arrive through the
                    # event queue, so poll_for_event raises them here —
                    # usually a window that died between our request and
                    # the server's reply (another client crashed). The
                    # connection is fine; skip it and keep draining.
                    log.warning("X protocol error in event queue",
                                exc_info=True)
                    continue
                except Exception as e:
                    raise DisplayError(f"X connection lost: {e}") from e
                if ev is None:
                    return count
                count += 1
                self.dispatch(ev)
        finally:
            self._pumping = False

    def dispatch(self, ev) -> None:
        if isinstance(ev, (xcffib.randr.ScreenChangeNotifyEvent,
                           xcffib.randr.NotifyEvent)):
            # Not addressed to any of our windows: the screen itself
            # changed (a mode set, an output switched) or an output's
            # connection did (a cable in or out — that one is a plain
            # RRNotify, not a ScreenChange).
            for cb in list(self._screen_listeners):
                try:
                    cb(ev)
                except Exception:
                    log.exception("screen listener failed")
            return
        wid = _event_window(ev)
        handler = self._handlers.get(wid) if wid is not None else None
        if handler is None:
            if self.on_unhandled is not None:
                self.on_unhandled(ev)
            elif log.isEnabledFor(logging.DEBUG):
                log.debug("unhandled %s for window %s",
                          type(ev).__name__,
                          f"0x{wid:x}" if wid is not None else "?")
            return
        try:
            handler(ev)
        except Exception:
            # One misbehaving window must not take the event pump — and
            # with it every other window — down with it.
            log.exception("handler for window 0x%x raised on %s",
                          wid, type(ev).__name__)

    # ── keyboard ────────────────────────────────────────────────────────
    def keysym(self, keycode: int, shift: bool = False) -> int:
        """Unshifted (or shifted) keysym for a keycode.

        X delivers keycodes, not keysyms — the mapping is per-keyboard and
        has to be fetched. Cached, and dropped on MappingNotify so a
        layout switch doesn't leave us translating with a stale table.
        """
        if self._keysyms is None:
            self._load_keymap()
        assert self._keysyms is not None
        return self._keysyms.get((keycode, bool(shift)), 0)

    def _load_keymap(self) -> None:
        lo, hi = self.setup.min_keycode, self.setup.max_keycode
        reply = self.conn.core.GetKeyboardMapping(lo, hi - lo + 1).reply()
        per = reply.keysyms_per_keycode
        table: dict[tuple[int, bool], int] = {}
        for i in range(hi - lo + 1):
            base = i * per
            table[(lo + i, False)] = reply.keysyms[base]
            if per > 1:
                table[(lo + i, True)] = reply.keysyms[base + 1] or reply.keysyms[base]
        self._keysyms = table
        log.debug("loaded keymap for keycodes %d..%d", lo, hi)

    def invalidate_keymap(self) -> None:
        self._keysyms = None

    # ── root events ─────────────────────────────────────────────────────
    def add_root_listener(self, callback) -> None:
        """Subscribe to PropertyNotify on the root window.

        EWMH state (_NET_CURRENT_DESKTOP, _NET_CLIENT_LIST, ...) lives in
        root properties, so this is how a widget learns about workspace
        changes without polling.
        """
        if not self._root_listeners:
            self.conn.core.ChangeWindowAttributes(
                self.root, xp.CW.EventMask, [xp.EventMask.PropertyChange])
            self.on(self.root, self._on_root_event)
            self.flush()
        self._root_listeners.append(callback)

    def add_screen_listener(self, callback) -> None:
        """Subscribe to RandR changes: the screen configuration, and
        outputs' connection state. Pushed by the server as the driver
        reports them — nothing here polls. The daemon uses them to let
        plugins react (the display fallback) and to re-place its windows
        on whatever the primary monitor has become."""
        if not self._screen_listeners:
            try:
                self.conn(xcffib.randr.key).SelectInput(
                    self.root, xcffib.randr.NotifyMask.ScreenChange
                    | xcffib.randr.NotifyMask.OutputChange)
                self.flush()
            except Exception:
                log.warning("RandR screen-change events unavailable",
                            exc_info=True)
        self._screen_listeners.append(callback)

    def watch_window_properties(self, wid: int) -> None:
        """Select PropertyNotify on a client window we don't own.

        Wrapped because a client can die between us listing it and us
        selecting on it, which is a BadWindow we simply don't care about.
        """
        try:
            self.conn.core.ChangeWindowAttributes(
                wid, xp.CW.EventMask, [xp.EventMask.PropertyChange])
        except Exception:
            log.debug("cannot watch 0x%x", wid, exc_info=True)

    def _on_root_event(self, ev) -> None:
        if not isinstance(ev, xp.PropertyNotifyEvent):
            return
        for cb in list(self._root_listeners):
            try:
                cb(ev)
            except Exception:
                log.exception("root listener failed")

    # ── properties ──────────────────────────────────────────────────────
    def get_ints(self, wid: int, name: str, length: int = 64) -> list[int]:
        """Read a 32-bit property as a list of ints. [] when absent."""
        try:
            reply = self.conn.core.GetProperty(
                False, wid, self.atom(name), xp.GetPropertyType.Any,
                0, length).reply()
        except Exception:
            return []
        raw = bytes(reply.value.buf())
        if not raw or reply.format != 32:
            return []
        return list(struct.unpack(f"<{len(raw) // 4}I", raw[:len(raw) // 4 * 4]))

    def get_int(self, wid: int, name: str) -> int | None:
        values = self.get_ints(wid, name, 1)
        return values[0] if values else None

    def send_client_message(self, target: int, type_name: str,
                            data: list[int]) -> None:
        """Send a 32-bit ClientMessage to the root, the way EWMH clients
        ask the window manager to do something."""
        values = (list(data) + [0, 0, 0, 0, 0])[:5]
        payload = xp.ClientMessageData.synthetic(values, "I" * 5)
        event = xp.ClientMessageEvent.synthetic(
            32, target, self.atom(type_name), payload)
        # SendEvent wants the 32-byte wire form, not the object.
        self.conn.core.SendEvent(
            False, self.root,
            xp.EventMask.SubstructureRedirect | xp.EventMask.SubstructureNotify,
            event.pack())
        self.flush()

    def set_prop(self, wid: int, name: str, type_: int, values) -> None:
        """Set a 32-bit property (CARDINAL / ATOM / WINDOW list).

        `data_len` counts *items*, not bytes — passing the byte length
        makes the server read past the request and reply with a length
        error that points nowhere near the real mistake.
        """
        data = struct.pack(f"<{len(values)}I", *values)
        self.conn.core.ChangeProperty(
            xp.PropMode.Replace, wid, self.atom(name), type_, 32,
            len(values), data)

    def set_text_prop(self, wid: int, prop: int, type_: int, text: str) -> None:
        raw = text.encode("utf-8")
        self.conn.core.ChangeProperty(
            xp.PropMode.Replace, wid, prop, type_, 8, len(raw), raw)

    def set_wm_class(self, wid: int, instance: str, cls: str) -> None:
        # WM_CLASS is two NUL-terminated strings back to back. Window
        # manager rules (qtile's floating_layout, picom's exclusions)
        # match on these, so they are load-bearing, not cosmetic.
        raw = f"{instance}\0{cls}\0".encode()
        self.conn.core.ChangeProperty(
            xp.PropMode.Replace, wid, xp.Atom.WM_CLASS, xp.Atom.STRING, 8,
            len(raw), raw)

    # ── monitors ────────────────────────────────────────────────────────
    def monitors(self) -> list[Monitor]:
        """Active monitors via RandR 1.5, primary first.

        `screen.width_in_pixels` is the whole X screen — the union of
        every output — so anything that should span "the display" has to
        ask RandR instead, or it stretches across all of them.
        """
        try:
            randr = self.conn(xcffib.randr.key)
            reply = randr.GetMonitors(self.root, True).reply()
        except Exception as e:
            w, h = self.geometry()
            log.warning("RandR unavailable (%s); treating the X screen as one "
                        "%dx%d monitor", e, w, h)
            return [Monitor(0, 0, w, h, primary=True)]

        out: list[Monitor] = []
        for m in reply.monitors:
            try:
                name = self.conn.core.GetAtomName(m.name).reply().name.to_string()
            except Exception:
                name = ""
            out.append(Monitor(m.x, m.y, m.width, m.height,
                               primary=bool(m.primary), name=name))
        out.sort(key=lambda m: not m.primary)
        # A headless or mid-reconfigure server can report zero monitors.
        if not out:
            w, h = self.geometry()
            return [Monitor(0, 0, w, h, primary=True)]
        return out

    def primary_monitor(self) -> Monitor:
        return self.monitors()[0]

    # ── visuals ─────────────────────────────────────────────────────────
    def argb_visual(self) -> tuple[int, int] | None:
        """(depth, visual_id) of a 32-bit ARGB visual, or None.

        Without one there is no real per-pixel alpha — the window is
        opaque no matter what the compositor does.
        """
        for d in self.screen.allowed_depths:
            if d.depth == 32 and d.visuals:
                return 32, d.visuals[0].visual_id
        return None

    def geometry(self) -> tuple[int, int]:
        return self.screen.width_in_pixels, self.screen.height_in_pixels


__all__ = ["Display", "DisplayError", "xp"]
