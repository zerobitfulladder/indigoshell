"""Wayland, for what any compositor with the common protocols gives: the
clipboard through wl-clipboard (wl-copy, wl-paste), and screenshots
through wlr-screencopy.

The screenshots come from a small client of its own on the Wayland wire
protocol (a socket, a memfd and a dozen messages; no library): it binds
the outputs and the screencopy manager, has each output copied into
shared memory, and hands back the pixels. Nothing is saved and the
clipboard is left alone, which is what the compositor's own screenshot
actions can't promise (niri's always put the picture on the clipboard).
Where the outputs sit, and where a window is, a compositor's backend
says (niri.py); Wayland itself doesn't.

Standard library only at load time; skia is imported when frames are
turned into an image.
"""

import asyncio
import logging
import mmap
import os
import shutil
import socket
import struct
import time
from dataclasses import dataclass

from . import Unsupported, WMError, run, serve

log = logging.getLogger(__name__)


# ── clipboard ───────────────────────────────────────────────────────────
def copy(value: str, selection: str = "clipboard") -> bool:
    """wl-copy forks and serves the text until something else takes the
    selection, like xclip does on X."""
    if not value or shutil.which("wl-copy") is None:
        if value:
            log.warning("cannot copy: wl-copy is not installed")
        return False
    serve(["wl-copy", *(["--primary"] if selection == "primary" else [])],
          value, "wl-copy")
    return True


def paste_argv(selection: str = "clipboard") -> list[str]:
    # --type text picks whichever text type is offered; --no-newline
    # keeps the text as it was copied.
    return ["wl-paste", *(["--primary"] if selection == "primary" else []),
            "--no-newline", "--type", "text"]


async def paste(selection: str = "clipboard") -> str:
    rc, out, err = await run(paste_argv(selection), timeout=5)
    if rc != 0:
        raise WMError(err or f"the {selection} is empty or not text")
    return out.decode(errors="replace")


# ── the wire ────────────────────────────────────────────────────────────
class WaylandError(WMError):
    pass


def _socket_path() -> str:
    name = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if os.path.isabs(name):
        return name
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise WaylandError("XDG_RUNTIME_DIR is not set")
    return os.path.join(runtime, name)


def _pack(sig: str, args) -> tuple[bytes, list[int]]:
    """Arguments by signature: u uint, i int, s string, o object,
    n new_id, h fd (sent beside the message)."""
    out, fds = bytearray(), []
    for kind, value in zip(sig, args):
        if kind in "uon":
            out += struct.pack("<I", value)
        elif kind == "i":
            out += struct.pack("<i", value)
        elif kind == "s":
            raw = value.encode() + b"\0"
            out += struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)
        elif kind == "h":
            fds.append(value)
    return bytes(out), fds


def _unpack(sig: str, data: bytes) -> list:
    out, pos = [], 0
    for kind in sig:
        if kind in "uon":
            out.append(struct.unpack_from("<I", data, pos)[0])
            pos += 4
        elif kind == "i":
            out.append(struct.unpack_from("<i", data, pos)[0])
            pos += 4
        elif kind == "s":
            n = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            out.append(data[pos:pos + max(0, n - 1)].decode(errors="replace"))
            pos += n + (-n % 4)
    return out


class _Wire:
    """One connection: requests out, events dispatched to a handler per
    object. Blocking, with a deadline; run it off the loop."""

    def __init__(self, path: str, deadline: float) -> None:
        self.deadline = deadline
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._timeout()
        try:
            self.sock.connect(path)
        except OSError as e:
            self.sock.close()
            raise WaylandError(f"can't reach the compositor: {e}") from None
        self._next = 2
        self._buf = b""
        self.handlers: dict[int, object] = {1: self._display}

    def _timeout(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise WaylandError("the compositor didn't answer in time")
        self.sock.settimeout(left)

    def new(self, handler) -> int:
        oid, self._next = self._next, self._next + 1
        self.handlers[oid] = handler
        return oid

    def send(self, oid: int, opcode: int, sig: str = "", *args) -> None:
        body, fds = _pack(sig, args)
        msg = struct.pack("<II", oid, (8 + len(body)) << 16 | opcode) + body
        anc = ([(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                 struct.pack(f"{len(fds)}i", *fds))] if fds else [])
        self._timeout()
        self.sock.sendmsg([msg], anc)

    def dispatch(self) -> None:
        """Read what is there (waiting for at least one event) and hand
        each event to its object's handler."""
        self._timeout()
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            raise WaylandError("the compositor didn't answer in time") from None
        if not chunk:
            raise WaylandError("the compositor closed the connection")
        self._buf += chunk
        while len(self._buf) >= 8:
            oid, word = struct.unpack_from("<II", self._buf)
            size, opcode = word >> 16, word & 0xFFFF
            if len(self._buf) < size:
                break
            body, self._buf = self._buf[8:size], self._buf[size:]
            handler = self.handlers.get(oid)
            if handler is not None:
                handler(opcode, body)

    def roundtrip(self) -> None:
        done = []
        cb = self.new(lambda _op, _b: done.append(1))
        self.send(1, 0, "n", cb)                    # wl_display.sync
        while not done:
            self.dispatch()

    def _display(self, opcode: int, body: bytes) -> None:
        if opcode == 0:                             # error
            oid, code, message = _unpack("uus", body)
            raise WaylandError(f"protocol error on object {oid} "
                               f"({code}): {message}")

    def close(self) -> None:
        self.sock.close()


# ── screencopy ──────────────────────────────────────────────────────────
@dataclass
class Frame:
    """One output's pixels, as the compositor copied them: in the
    output's buffer orientation (`transform`, `y_invert` say how to turn
    it upright) and in physical pixels."""
    name: str
    width: int
    height: int
    stride: int
    format: int                 # wl_shm format
    data: bytes
    y_invert: bool
    transform: int              # wl_output.transform


# wl_shm formats: ARGB8888 and XRGB8888 are 0 and 1, the rest fourcc.
def _fourcc(code: str) -> int:
    return struct.unpack("<I", code.encode())[0]


ARGB8888, XRGB8888 = 0, 1
ABGR8888, XBGR8888 = _fourcc("AB24"), _fourcc("XB24")
ARGB2101010, XRGB2101010 = _fourcc("AR30"), _fourcc("XR30")
ABGR2101010, XBGR2101010 = _fourcc("AB30"), _fourcc("XB30")
FORMATS = (XRGB8888, ARGB8888, XBGR8888, ABGR8888, XRGB2101010, ARGB2101010,
           XBGR2101010, ABGR2101010)


class _Output:
    def __init__(self, global_name: int) -> None:
        self.global_name = global_name
        self.oid = 0
        self.name = ""
        self.transform = 0

    def event(self, opcode: int, body: bytes) -> None:
        if opcode == 0:                             # geometry
            self.transform = _unpack("iiiiissi", body)[-1]
        elif opcode == 4:                           # name (v4)
            self.name = _unpack("s", body)[0]


def _capture(names: list[str] | None, timeout: float) -> list[Frame]:
    wire = _Wire(_socket_path(), time.monotonic() + timeout)
    try:
        globals_: dict[str, list[tuple[int, int]]] = {}

        def on_global(opcode: int, body: bytes) -> None:
            if opcode == 0:
                name, iface, version = _unpack("usu", body)
                globals_.setdefault(iface, []).append((name, version))

        registry = wire.new(on_global)
        wire.send(1, 1, "n", registry)              # get_registry
        wire.roundtrip()

        def bind(iface: str, name: int, version: int, handler) -> int:
            oid = wire.new(handler)
            wire.send(registry, 0, "usun", name, iface, version, oid)
            return oid

        if "zwlr_screencopy_manager_v1" not in globals_:
            raise Unsupported("the compositor offers no wlr-screencopy")
        if "wl_shm" not in globals_:
            raise Unsupported("the compositor offers no shared memory")
        shm = bind("wl_shm", globals_["wl_shm"][0][0], 1, lambda *_: None)
        mname, mver = globals_["zwlr_screencopy_manager_v1"][0]
        manager = bind("zwlr_screencopy_manager_v1", mname, min(mver, 3),
                       lambda *_: None)
        outputs = []
        for gname, version in globals_.get("wl_output", []):
            out = _Output(gname)
            out.oid = bind("wl_output", gname, min(version, 4), out.event)
            outputs.append(out)
        wire.roundtrip()                            # their names

        if names is not None:
            by_name = {o.name: o for o in outputs}
            missing = [n for n in names if n not in by_name]
            if missing and not (len(outputs) == 1 and len(names) == 1
                                and not outputs[0].name):
                raise WaylandError(f"no output {missing[0]} on Wayland")
            picked = ([by_name[n] for n in names] if not missing
                      else outputs)
        else:
            picked = outputs
        frames = [_copy(wire, shm, manager, min(mver, 3), o)
                  for o in picked]
        wire.send(manager, 2)                       # destroy
        return frames
    finally:
        wire.close()


def _copy(wire: _Wire, shm: int, manager: int, version: int,
          out: _Output) -> Frame:
    st: dict = {"buffers": [], "done": False, "flags": 0,
                "ready": False, "failed": False}

    def on_frame(opcode: int, body: bytes) -> None:
        if opcode == 0:                             # buffer
            st["buffers"].append(_unpack("uuuu", body))
        elif opcode == 1:                           # flags
            st["flags"] = _unpack("u", body)[0]
        elif opcode == 2:                           # ready
            st["ready"] = True
        elif opcode == 3:                           # failed
            st["failed"] = True
        elif opcode == 6:                           # buffer_done (v3)
            st["done"] = True

    frame = wire.new(on_frame)
    wire.send(manager, 0, "nio", frame, 0, out.oid)  # capture_output, no cursor
    # v3 lists every buffer kind and then says done; before, the one shm
    # buffer event is all there is.
    while not (st["done"] if version >= 3 else st["buffers"]):
        if st["failed"]:
            raise WaylandError(f"the compositor wouldn't copy {out.name}")
        wire.dispatch()
    usable = [b for b in st["buffers"] if b[0] in FORMATS]
    if not usable:
        raise Unsupported("no pixel format this can read: "
                          + ", ".join(f"0x{b[0]:x}" for b in st["buffers"]))
    fmt, width, height, stride = min(usable, key=lambda b: FORMATS.index(b[0]))
    size = stride * height
    fd = os.memfd_create("indigoshell-screencopy", os.MFD_CLOEXEC)
    try:
        os.ftruncate(fd, size)
        pool = wire.new(None)
        wire.send(shm, 0, "nhi", pool, fd, size)    # create_pool
        buffer = wire.new(lambda *_: None)
        wire.send(pool, 0, "niiiiu", buffer, 0, width, height, stride, fmt)
        wire.send(frame, 0, "o", buffer)            # copy
        while not (st["ready"] or st["failed"]):
            wire.dispatch()
        if st["failed"]:
            raise WaylandError(f"the compositor couldn't copy {out.name}")
        with mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ) as mem:
            data = bytes(mem)
        wire.send(frame, 1)                         # destroy
        wire.send(buffer, 0)                        # destroy
        wire.send(pool, 1)                          # destroy
    finally:
        os.close(fd)
    return Frame(out.name, width, height, stride, fmt, data,
                 bool(st["flags"] & 1), out.transform)


async def capture(names: list[str] | None = None,
                  timeout: float = 10.0) -> list[Frame]:
    """The named outputs' pixels (every output's with None), without the
    pointer."""
    try:
        return await asyncio.to_thread(_capture, names, timeout)
    except OSError as e:
        raise WaylandError(f"screencopy failed: {e}") from None


# ── into an image ───────────────────────────────────────────────────────
def upright(frame: Frame):
    """The frame as a skia image the way the output shows it."""
    import skia
    kinds = {
        ARGB8888: (skia.kBGRA_8888_ColorType, skia.kPremul_AlphaType),
        XRGB8888: (skia.kBGRA_8888_ColorType, skia.kOpaque_AlphaType),
        ABGR8888: (skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType),
        XBGR8888: (skia.kRGBA_8888_ColorType, skia.kOpaque_AlphaType),
        ARGB2101010: (skia.kBGRA_1010102_ColorType, skia.kPremul_AlphaType),
        XRGB2101010: (skia.kBGR_101010x_ColorType, skia.kOpaque_AlphaType),
        ABGR2101010: (skia.kRGBA_1010102_ColorType, skia.kPremul_AlphaType),
        XBGR2101010: (skia.kRGB_101010x_ColorType, skia.kOpaque_AlphaType),
    }
    ct, at = kinds[frame.format]
    raw = skia.Image.MakeRasterData(
        skia.ImageInfo.Make(frame.width, frame.height, ct, at),
        frame.data, frame.stride)
    if raw is None:
        raise WMError(f"can't read the pixels of {frame.name}")
    # The buffer holds the output's content turned by its transform
    # (counter-clockwise, flipped first for the flipped ones); undo that,
    # after the y-invert the copy may carry.
    t = frame.transform & 7
    turns = t & 3
    w, h = (frame.height, frame.width) if turns & 1 else (frame.width,
                                                          frame.height)
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.translate(w / 2, h / 2)
    if t & 4:
        canvas.scale(-1, 1)
    canvas.rotate(90 * turns)
    if frame.y_invert:
        canvas.scale(1, -1)
    canvas.translate(-frame.width / 2, -frame.height / 2)
    canvas.drawImage(raw, 0, 0)
    return surface.makeImageSnapshot()


def compose(parts, width: int, height: int):
    """One image of `width`x`height` from (image, x, y, w, h) parts, each
    drawn into its box."""
    import skia
    surface = skia.Surface(max(1, width), max(1, height))
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorBLACK)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear,
                                    skia.MipmapMode.kLinear)
    for image, x, y, w, h in parts:
        canvas.drawImageRect(image, skia.Rect.MakeXYWH(x, y, w, h), sampling)
    return surface.makeImageSnapshot()


def crop(image, x: float, y: float, w: float, h: float):
    import skia
    box = skia.IRect.MakeXYWH(round(x), round(y), round(w), round(h))
    if not box.intersect(skia.IRect.MakeWH(image.width(), image.height())):
        raise WMError("that is off the screen")
    return image.makeSubset(box).makeRasterImage()


def png(image) -> bytes:
    import skia
    return bytes(image.encodeToData(skia.EncodedImageFormat.kPNG, 100))
