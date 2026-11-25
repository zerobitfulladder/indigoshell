"""The browser strip's unattended test: strip.py --selftest [--scratch DIR]

Starts its own strip processes (strip.py, on a private socket) and its
own browser (gauntlet.py --strip-test: the floating corner window that
never takes the keyboard, its profile under the scratch dir), serves
the pages itself on 127.0.0.1, and answers in numbers:

  1. is the strip in the browser window's own pixels (four ways of reading them)
  2. does it follow the width, and how late
  3. do clicks on it reach the strip process and act in the browser
  4. do keys typed with the pointer over it still reach the page; does it take focus
  5. does it go away for fullscreen and come back
  6. either side restarted in any order; leaked X resources; X errors
  7. does Qt misbehave with a foreign child in its window

It reads only the browser window it started: its composite pixmap, a
copy of it, Qt's grab of it. Never the root, never the screen. It moves
no pointer and sets no focus; input is sent with SendEvent to its own
windows, which is what makes questions 3 and 4 answerable while someone
works at this desktop (and is also the limit of what they prove: see
the notes printed at the end).

During the run Qt paints the browser's band in a test colour (pure
green) that the strip never draws, so any of it read back is band the
strip is not covering.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import skia
import xcffib
import xcffib.composite
import xcffib.res
import xcffib.xproto as xp

from indigoshell import theme
from indigoshell.core.naming import APP
from indigoshell.core.paths import runtime_dir

HERE = Path(__file__).resolve().parent
PY = sys.executable
BAND = 28
GREEN = (0, 255, 0)
PROGRESS_RGB = tuple(int(theme.BAR_RULE[i:i + 2], 16) for i in (1, 3, 5))
KEY_MASK = xp.EventMask.KeyPress | xp.EventMask.KeyRelease


# ── pages ───────────────────────────────────────────────────────────────

PAGE = """<!doctype html><title>{title}</title>
<body style="background:#1a1a2a;color:#ccc;font:16px sans-serif;margin:0">
<p style="margin:0;padding:8px">{title}</p><input id="t" style="margin:8px">{extra}"""

ANIM = """<div style="width:120px;height:120px;background:#c33;
animation:spin 0.6s linear infinite"></div>
<style>@keyframes spin{from{transform:translateX(0)}to{transform:translateX(400px)}}</style>"""


class Pages(BaseHTTPRequestHandler):
    hits: dict = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        Pages.hits[path] = Pages.hits.get(path, 0) + 1
        if path == "/slow":
            # The document itself takes 3 s: half now, the rest later, no
            # length, so nothing can call the load finished early.
            body = PAGE.format(title="Slow page", extra="").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body + b"<!--" + b" " * 4096 + b"-->")
            self.wfile.flush()
            time.sleep(3.0)
            self.wfile.write(b"<p>done</p>")
            return
        title = {"/a": "Page A", "/b": "Page B", "/c": "Page C",
                 "/slow": "Slow page", "/anim": "Moving page"}.get(path)
        if title is None:
            self.send_error(404)
            return
        extra = ANIM if path == "/anim" else ""
        self._send(PAGE.format(title=title, extra=extra).encode(), "text/html")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── pixels ──────────────────────────────────────────────────────────────

def rgb_from_bgrx(data: bytes, w: int, h: int) -> np.ndarray:
    a = np.frombuffer(data, np.uint8)[:w * h * 4].reshape(h, w, 4)
    return a[..., [2, 1, 0]].astype(np.int16)


def rgb_from_png(png: bytes) -> np.ndarray:
    image = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
    a = image.toarray(colorType=skia.kRGBA_8888_ColorType, alphaType=skia.kUnpremul_AlphaType)
    return a[..., :3].astype(np.int16)


def share_matching(a: np.ndarray, b: np.ndarray) -> float:
    """Share of pixels within 2 levels per channel; 0 if sizes differ."""
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float((np.abs(a - b).max(axis=-1) <= 2).mean())


def share_of(a: np.ndarray, rgb, tol=24) -> float:
    return float((np.abs(a - np.array(rgb)).max(axis=-1) <= tol).mean())


def ms(seconds) -> str:
    return "never" if seconds is None else f"{seconds * 1000:.1f} ms"


def after(event, t0) -> str:
    return "never" if event is None else ms(event["t"] - t0)


def progress_px(a: np.ndarray) -> int:
    """Length of the progress line: pixels in its colour on the band's last row."""
    return int((np.abs(a[-1] - np.array(PROGRESS_RGB)).max(axis=-1) <= 6).sum())


# ── X: only ever our own windows ────────────────────────────────────────

class X:
    def __init__(self):
        self.conn = xcffib.connect()
        self.setup = self.conn.get_setup()
        self.root = self.setup.roots[0].root
        self.composite = self.conn(xcffib.composite.key)
        self.composite.QueryVersion(0, 4).reply()
        self.res = self.conn(xcffib.res.key)
        self.res.QueryVersion(1, 2).reply()
        self._keycodes = None

    def geometry(self, wid):
        g = self.conn.core.GetGeometry(wid).reply()
        return g.width, g.height, g.depth

    def children(self, wid):
        try:
            return list(self.conn.core.QueryTree(wid).reply().children)
        except Exception:
            return []

    def attrs(self, wid):
        return self.conn.core.GetWindowAttributes(wid).reply()

    def focus(self):
        return self.conn.core.GetInputFocus().reply().focus

    def band(self, wid, method, w, h=BAND):
        """The top `h` rows of window `wid` as RGB, read one of three ways."""
        core = self.conn.core
        if method == "GetImage on the window":
            img = core.GetImage(xp.ImageFormat.ZPixmap, wid, 0, 0, w, h, 0xFFFFFFFF).reply()
            return rgb_from_bgrx(bytes(img.data.buf()), w, h)
        pix = self.conn.generate_id()
        gc = None
        if method == "CopyArea IncludeInferiors":
            # What Qt's QScreen.grabWindow does under the hood.
            depth = self.geometry(wid)[2]
            core.CreatePixmap(depth, pix, wid, w, h)
            gc = self.conn.generate_id()
            core.CreateGC(gc, pix, xp.GC.SubwindowMode, [xp.SubwindowMode.IncludeInferiors])
            core.CopyArea(wid, pix, gc, 0, 0, 0, 0, w, h)
        else:   # "composite pixmap": what picom draws the window from
            self.composite.NameWindowPixmap(wid, pix)
        # The window manager's border is part of the composite pixmap
        # (qtile draws one), so the window's own pixels start inside it.
        at = self.border(wid) if gc is None else 0
        try:
            img = core.GetImage(xp.ImageFormat.ZPixmap, pix, at, at, w, h, 0xFFFFFFFF).reply()
            return rgb_from_bgrx(bytes(img.data.buf()), w, h)
        finally:
            core.FreePixmap(pix)
            if gc is not None:
                core.FreeGC(gc)
            self.conn.flush()

    def border(self, wid):
        return self.conn.core.GetGeometry(wid).reply().border_width

    def composite_sample(self, wid, max_w, at=None):
        """(window width, band RGB) from the window's composite pixmap now."""
        at = self.border(wid) if at is None else at
        pix = self.conn.generate_id()
        self.composite.NameWindowPixmap(wid, pix)
        try:
            g = self.conn.core.GetGeometry(pix).reply()
            width = g.width - 2 * at
            w = min(width, max_w)
            img = self.conn.core.GetImage(xp.ImageFormat.ZPixmap, pix, at, at, w, BAND,
                                          0xFFFFFFFF).reply()
            return width, rgb_from_bgrx(bytes(img.data.buf()), w, BAND)
        finally:
            self.conn.core.FreePixmap(pix)
            self.conn.flush()

    def select(self, wid, mask):
        self.conn.core.ChangeWindowAttributes(wid, xp.CW.EventMask, [mask])
        self.conn.flush()

    def events(self):
        out = []
        while True:
            try:
                ev = self.conn.poll_for_event()
            except Exception:
                continue
            if ev is None:
                return out
            out.append((time.monotonic(), ev))

    def keycode(self, ch):
        if self._keycodes is None:
            lo, hi = self.setup.min_keycode, self.setup.max_keycode
            reply = self.conn.core.GetKeyboardMapping(lo, hi - lo + 1).reply()
            per, syms = reply.keysyms_per_keycode, list(reply.keysyms)
            self._keycodes = {}
            for i in range(hi - lo + 1):
                sym = syms[i * per]
                self._keycodes.setdefault(sym, lo + i)
        return self._keycodes[ord(ch)]

    def click(self, child, x, y):
        for cls, mask in ((xp.ButtonPressEvent, xp.EventMask.ButtonPress),
                          (xp.ButtonReleaseEvent, xp.EventMask.ButtonRelease)):
            ev = cls.synthetic(1, 0, self.root, child, 0, 0, 0, int(x), int(y), 0, 1)
            self.conn.core.SendEvent(False, child, mask, ev.pack())
        self.conn.flush()

    def type_via(self, child, top, text, x=200, y=14):
        """Keys the way X builds them for a pointer resting on `child`
        inside the focused `top`: source window `child`, then propagated
        to the first window up the tree where someone selected them.
        SendEvent with propagate does that same walk; the fields are the
        ones the server fills in once it reaches `top`. With `child`
        None, straight to `top`: no strip in the way at all."""
        dest = child or top
        for ch in text:
            code = self.keycode(ch)
            for cls, mask in ((xp.KeyPressEvent, xp.EventMask.KeyPress),
                              (xp.KeyReleaseEvent, xp.EventMask.KeyRelease)):
                ev = cls.synthetic(code, 0, self.root, top, child or 0, 0, 0, x, y, 0, 1)
                self.conn.core.SendEvent(child is not None, dest, mask, ev.pack())
        self.conn.flush()

    def resources_of(self, pid):
        """{type: count} over every X client of process `pid`."""
        spec = xcffib.res.ClientIdSpec.synthetic(0, xcffib.res.ClientIdMask.LocalClientPID)
        ids = self.res.QueryClientIds(1, [spec]).reply().ids
        bases = [i.spec.client for i in ids if list(i.value) == [pid]]
        total = {}
        for base in bases:
            for t in self.res.QueryClientResources(base).reply().types:
                name = self.conn.core.GetAtomName(t.resource_type).reply().name.to_string()
                if name in ("WINDOW", "PIXMAP", "GC", "COLORMAP", "CURSOR"):
                    total[name] = total.get(name, 0) + t.count
        return {"clients": len(bases), **total}


# ── processes ───────────────────────────────────────────────────────────

class Browser:
    def __init__(self, test, url, n):
        self.test, self.url, self.n = test, url, n
        self.seq = 0
        self.waiting = {}

    async def start(self):
        t = self.test
        self.stderr = open(t.scratch / f"browser-{self.n}.log", "w")
        self.proc = await asyncio.create_subprocess_exec(
            PY, str(HERE / "gauntlet.py"), "--strip-test", self.url, env=t.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        t.procs.append(self.proc)
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        while line := await self.proc.stdout.readline():
            line = line.decode(errors="replace")
            if line.startswith("@@ "):
                msg = json.loads(line[3:])
                fut = self.waiting.pop(msg.get("seq"), None)
                if fut and not fut.done():
                    fut.set_result(msg)

    async def ask(self, op, timeout=10.0, **kw):
        self.seq += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiting[self.seq] = fut
        self.proc.stdin.write((json.dumps({"seq": self.seq, "op": op, **kw}) + "\n").encode())
        await self.proc.stdin.drain()
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(f"browser {op}: {msg['error']}")
        return msg

    async def until(self, pred, timeout=10.0, win=0, every=0.05):
        """Poll info until pred(info); returns (info, seconds) or (info, None)."""
        t0 = time.monotonic()
        info = None
        while time.monotonic() - t0 < timeout:
            info = await self.ask("info", win=win)
            if pred(info):
                return info, time.monotonic() - t0
            await asyncio.sleep(every)
        return info, None


class StripProc:
    def __init__(self, test, flags, n):
        self.test, self.flags, self.n = test, flags, n

    async def start(self):
        t = self.test
        self.log_path = t.scratch / f"strip-{self.n}.log"
        self.stderr = open(self.log_path, "w")
        self.proc = await asyncio.create_subprocess_exec(
            PY, str(HERE / "strip.py"), "--socket", t.sock, *self.flags, env=t.env,
            stderr=self.stderr)
        t.procs.append(self.proc)
        self.started = time.monotonic()
        for _ in range(200):
            if os.path.exists(t.sock):
                try:
                    r, w = await asyncio.open_unix_connection(t.sock)
                    break
                except OSError:
                    pass
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("strip never listened")
        self.events = []
        w.write(b'{"monitor": true}\n')
        self.monitor = (r, w)
        self.reader = asyncio.create_task(self._read(r))

    async def _read(self, r):
        while line := await r.readline():
            self.events.append(json.loads(line))

    async def event(self, pred, since=0, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for e in self.events[since:]:
                if pred(e):
                    return e
            await asyncio.sleep(0.005)
        return None

    async def request(self, msg):
        r, w = await asyncio.open_unix_connection(self.test.sock, limit=1 << 24)
        w.write((json.dumps(msg) + "\n").encode())
        want = "stats" if "stats" in msg else "frame"
        try:
            while line := await asyncio.wait_for(r.readline(), 5):
                reply = json.loads(line)
                if reply.get("ev") == want:
                    return reply
        finally:
            w.close()

    async def frame(self, parent):
        f = await self.request({"frame": parent})
        f["rgb"] = rgb_from_png(base64.b64decode(f["png"])) if f.get("png") else None
        return f

    def errors_in_log(self):
        text = self.log_path.read_text(errors="replace")
        return text.count("X protocol error"), text.count("Xlib error")


# ── the test ────────────────────────────────────────────────────────────

class Test:
    def __init__(self, scratch):
        self.scratch = Path(scratch)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.sock = os.path.join(runtime_dir(), f"{APP}-strip-test-{os.getpid()}.sock")
        self.env = dict(os.environ,
                        XDG_DATA_HOME=str(self.scratch / "data"),
                        XDG_CACHE_HOME=str(self.scratch / "cache"),
                        **{"INDIGOSHELL_STRIP_SOCKET": self.sock})
        self.procs = []           # only ever these are signalled
        self.lines = []
        self.failures = 0
        self.x = X()
        self.ours = set()           # every window of ours seen: strips, browser tops
        self.focus_seen = set()
        self.focus_reads = 0
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.refresh = self._refresh_rate()

    # ── output ──────────────────────────────────────────────────────────
    def say(self, text=""):
        self.lines.append(text)
        print(text, flush=True)

    def check(self, ok, text):
        if not ok:
            self.failures += 1
        self.say(f"  [{'ok' if ok else 'FAIL'}] {text}")

    def note(self, text):
        self.say(f"         {text}")

    def sample_focus(self):
        self.focus_seen.add(self.x.focus())
        self.focus_reads += 1

    @staticmethod
    def _refresh_rate():
        try:
            out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r"(\d+\.\d+)\*", out)
            return float(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            return None

    # ── helpers ─────────────────────────────────────────────────────────
    async def strip_child(self, top, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            kids = self.x.children(top)
            if kids:
                return kids[-1]
            await asyncio.sleep(0.02)
        raise RuntimeError(f"no strip window appeared in browser 0x{top:x}")

    async def visibility(self, browser, strip, win=0, label=""):
        info = await browser.ask("info", win=win)
        top, w = info["wid"], info["geometry"][2]
        await asyncio.sleep(0.15)
        frame = await strip.frame(top)
        reads = {}
        for method in ("composite pixmap", "CopyArea IncludeInferiors", "GetImage on the window"):
            reads[method] = self.x.band(top, method, w)
        grab = await browser.ask("grab", win=win)
        reads["Qt grabWindow"] = rgb_from_png(base64.b64decode(grab["png"]))
        self.say(f"  band of browser 0x{top:x} ({w}x{BAND}) {label}vs the strip's own last frame:")
        for method, rgb in reads.items():
            self.note(f"{method:28} same as strip frame {share_matching(rgb, frame['rgb']):6.1%}"
                      f"   test green {share_of(rgb, GREEN):6.1%}")
        return reads, frame

    async def stability(self, browser, strip, seconds=1.5):
        """Sample the composite band while the page animates (Qt repaints
        and swaps its whole window every frame)."""
        info = await browser.ask("info")
        top, w = info["wid"], info["geometry"][2]
        frame = await strip.frame(top)
        before = (await strip.request({"stats": True}))["strips"]
        worst, greenest, n = 1.0, 0.0, 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            _, rgb = self.x.composite_sample(top, w)
            worst = min(worst, share_matching(rgb, frame["rgb"]))
            greenest = max(greenest, share_of(rgb, GREEN))
            n += 1
            await asyncio.sleep(0.004)
        after = (await strip.request({"stats": True}))["strips"]
        paints = sum(s["paints"] for s in after) - sum(s["paints"] for s in before)
        return worst, greenest, n, paints

    async def resize_timeline(self, browser, strip, to_w, h=660):
        info = await browser.ask("info")
        top, from_w = info["wid"], info["geometry"][2]
        self.x.select(top, xp.EventMask.StructureNotify)
        self.x.events()
        since = len(strip.events)
        reply = await browser.ask("resize", w=to_w, h=h)
        t_req = reply["t"]
        samples, t_cfg = [], None
        at = self.x.border(top)
        while time.monotonic() - t_req < 0.6:
            for t, ev in self.x.events():
                if isinstance(ev, xp.ConfigureNotifyEvent) and ev.window == top \
                        and ev.width == to_w and t_cfg is None:
                    t_cfg = t
            pw, rgb = self.x.composite_sample(top, max(from_w, to_w), at)
            samples.append((time.monotonic(), pw, rgb))
            await asyncio.sleep(0.001)
        self.x.select(top, 0)
        cfg = await strip.event(lambda e: e.get("ev") == "configure" and e["parent"] == top
                                and e["width"] == to_w, since, timeout=1.0)
        frame = await strip.frame(top)
        final = frame["rgb"]
        ms = lambda t: (t - t_req) * 1000 if t is not None else float("nan")
        good_from, green_ms, stale_ms = None, [], []
        for t, pw, rgb in samples:
            ok = pw == to_w and share_matching(rgb, final) >= 0.999
            if ok and good_from is None:
                good_from = t
            elif not ok:
                good_from = None
            if pw == to_w and share_of(rgb, GREEN) > 0:
                green_ms.append(ms(t))
            if pw == to_w and share_matching(rgb, final) < 0.999:
                stale_ms.append(ms(t))
        self.say(f"  {from_w} -> {to_w} px wide ({len(samples)} reads of the composite pixmap, "
                 f"times from Qt's resize call):")
        self.note(f"browser ConfigureNotify seen at   {ms(t_cfg):6.1f} ms")
        if cfg:
            self.note(f"strip handled it at               {ms(cfg['t_event']):6.1f} ms, "
                      f"presented its new frame at {ms(cfg['t_presented']):6.1f} ms")
            self.note(f"strip's own work: resize {1000 * (cfg['t_resized'] - cfg['t_event']):.1f}"
                      f" ms, draw {cfg['draw_ms']:.1f} ms, present {cfg['present_ms']:.1f} ms")
        else:
            self.note("strip never reported the configure")
        if stale_ms:
            self.note(f"band not yet the final strip      {min(stale_ms):6.1f} .. "
                      f"{max(stale_ms):6.1f} ms ({len(stale_ms)} reads)")
        if green_ms:
            self.note(f"uncovered band (test green) seen  {min(green_ms):6.1f} .. "
                      f"{max(green_ms):6.1f} ms ({len(green_ms)} reads)")
        else:
            self.note("uncovered band (test green) never seen at the new width")
        self.note(f"band correct for good from        {ms(good_from):6.1f} ms")
        # From the earliest sign the browser had its new size: whichever of
        # us (this loop is busy reading pixels) or the strip heard first.
        heard = [t for t in (t_cfg, cfg and cfg["t_event"]) if t]
        late = (good_from - min(heard)) * 1000 if (good_from and heard) else None
        return late, cfg, frame

    async def resources(self, strip):
        return self.x.resources_of(strip.proc.pid)

    # ── the run ─────────────────────────────────────────────────────────
    async def run(self):
        S = self.say
        frame_ms = 1000 / self.refresh if self.refresh else None
        S(f"strip self-test  (display refresh {self.refresh or '?'} Hz"
          f"{f' = {frame_ms:.1f} ms a frame' if frame_ms else ''}; scratch {self.scratch.name})")

        # ── 0. browser first, nobody listening ──────────────────────────
        S("\n0. browser started first, no strip listening")
        b = Browser(self, f"{self.base}/a", 0)
        await b.start()
        info, _ = await b.until(lambda i: i.get("title") == "Page A", timeout=30)
        top = info["wid"]
        self.ours.add(top)
        await asyncio.sleep(0.5)
        rgb = self.x.band(top, "composite pixmap", info["geometry"][2])
        self.check(not info["attached"] and self.x.children(top) == [],
                   f"no strip: label shows {info['label']!r}, top window has "
                   f"{len(self.x.children(top))} children")
        self.check(share_of(rgb, GREEN) > 0.5,
                   f"band is Qt's label: test green {share_of(rgb, GREEN):.1%} of the band")
        self.sample_focus()

        # ── 1. GL strip, with targets, started after the browser ────────
        S("\n1. strip started after the browser (GL path, --targets)")
        s1 = StripProc(self, ["--targets"], 1)
        await s1.start()
        info, dt = await b.until(lambda i: i["attached"], timeout=8)
        self.check(dt is not None, f"browser attached {ms(dt)} after the strip started "
                   f"listening (the browser retries every 2 s)")
        created = await s1.event(lambda e: e.get("ev") == "created")
        child = await self.strip_child(top)
        self.ours.add(child)
        g = self.x.geometry(child)
        a = self.x.attrs(child)
        self.check(created and created["path"] == "gl",
                   f"path {created and created['path']}, renderer {created and created['renderer']}")
        self.note(f"browser top 0x{top:x}: depth {created['parent_depth']}, visual "
                  f"0x{created['parent_visual']:x}; strip child 0x{child:x}: {g[0]}x{g[1]} depth "
                  f"{g[2]}, map_state {a.map_state} (2 = viewable)")
        self.check(info["label"] == "", f"Qt label blank while attached: {info['label']!r}")
        gw, gh = info["geometry"][2:]
        self.check(info["band"] == [0, 0, gw, BAND] and info["view"] == [0, BAND, gw, gh - BAND],
                   f"Qt's own layout untouched by the child: band {info['band']}, "
                   f"view {info['view']}")
        reads, frame = await self.visibility(b, s1)
        self.check(share_matching(reads["composite pixmap"], frame["rgb"]) > 0.999,
                   "composite pixmap band = strip frame")
        self.gl_reads = {m: share_matching(r, frame["rgb"]) for m, r in reads.items()}
        self.sample_focus()

        # ── 1b. progress line ──────────────────────────────────────────
        S("\n1b. load progress line (a page that takes 3 s to arrive)")
        await b.ask("load", url=f"{self.base}/slow")
        seen = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6:
            i = await b.ask("info")
            _, rgb = self.x.composite_sample(top, i["geometry"][2])
            seen.append((i["loading"], i["progress"], progress_px(rgb)))
            if not i["loading"] and time.monotonic() - t0 > 0.5:
                break
            await asyncio.sleep(0.1)
        loading = [(p, px) for l, p, px in seen if l and p < 100]
        await asyncio.sleep(0.2)
        _, rgb = self.x.composite_sample(top, info["geometry"][2])
        drawn = sum(px > 0 for _, px in loading)
        self.check(bool(loading) and drawn >= 0.8 * len(loading),
                   f"line drawn while loading: {drawn} of {len(loading)} reads; "
                   f"(progress %, line px) seen {sorted(set(loading))[:4]}")
        self.check(progress_px(rgb) == 0, f"line gone after load: {progress_px(rgb)} px")

        # ── 1c. damage-driven: idle, and under Qt repaints ──────────────
        S("\n1c. strip repaints only on change; band holds while Qt repaints underneath")
        await b.ask("load", url=f"{self.base}/anim")
        await b.until(lambda i: not i["loading"] and i["title"] == "Moving page")
        await asyncio.sleep(0.3)
        p0 = (await s1.request({"stats": True}))["strips"][0]["paints"]
        await asyncio.sleep(2.0)
        p1 = (await s1.request({"stats": True}))["strips"][0]["paints"]
        self.check(p1 == p0, f"strip paints over 2 s with nothing changing: {p1 - p0}")
        worst, greenest, n, paints = await self.stability(b, s1)
        self.check(worst > 0.999 and greenest == 0,
                   f"page animating: {n} composite reads in 1.5 s, worst match {worst:.2%}, "
                   f"max test green {greenest:.2%}, strip paints meanwhile {paints}")

        # ── 2. width ────────────────────────────────────────────────────
        S("\n2. width follows the browser (GL)")
        await b.ask("load", url=f"{self.base}/b")
        await b.until(lambda i: i["title"] == "Page B")
        for to_w in (760, 1000):
            late, cfg, frame = await self.resize_timeline(b, s1, to_w)
            info = await b.ask("info")
            xg = self.x.geometry(top)
            self.check(cfg is not None and cfg["width"] == to_w and xg[0] == to_w
                       and info["geometry"][2] == to_w,
                       f"strip {cfg and cfg['width']} px, X says top {xg[0]}x{xg[1]}, "
                       f"Qt says {info['geometry'][2]}x{info['geometry'][3]}")
            if late is not None:
                self.note(f"=> band right {late:.1f} ms after the browser's ConfigureNotify"
                          + (f" ({late / frame_ms:.2f} frames)" if frame_ms else ""))
        self.sample_focus()

        # ── 3. clicks ───────────────────────────────────────────────────
        S("\n3. clicks on the targets (SendEvent to the strip window)")
        for path, title in (("/a", "Page A"), ("/b", "Page B")):
            await b.ask("load", url=f"{self.base}{path}")
            await b.until(lambda i: i["title"] == title and not i["loading"] and
                          (path == "/a" or i["back"]))
        await asyncio.sleep(0.3)
        info = await b.ask("info")
        frame = await s1.frame(top)
        rects = frame["targets"]
        centre = lambda r: (r[0] + r[2] / 2, r[1] + r[3] / 2)
        expect = {"back": "/a", "forward": "/b", "reload": "/b"}
        for name in ("back", "forward", "reload"):
            since = len(s1.events)
            hits = Pages.hits.get("/b", 0)
            before = (await b.ask("info"))["url"]
            t0 = time.monotonic()
            self.x.click(child, *centre(rects[name]))
            ev = await s1.event(lambda e: e.get("ev") == "cmd", since)
            if name == "reload":
                _, dt = await b.until(lambda i: Pages.hits.get("/b", 0) > hits, timeout=5)
            else:
                _, dt = await b.until(lambda i: i["url"].endswith(expect[name]) and
                                      i["url"] != before and not i["loading"], timeout=5)
            self.check(ev is not None and ev["cmd"] == name and dt is not None,
                       f"{name:8} -> strip sent {ev and ev['cmd']!r} after {after(ev, t0)}; "
                       f"browser {'reloaded /b' if name == 'reload' else 'at ' + expect[name]} "
                       f"after {ms(dt)}")
            await asyncio.sleep(0.3)
        since = len(s1.events)
        self.x.click(child, 60, 14)
        ev = await s1.event(lambda e: e.get("ev") == "cmd", since, timeout=0.5)
        self.check(ev is None, "click on the title text sends nothing")

        # ── 4. keyboard ─────────────────────────────────────────────────
        S("\n4. keys with the pointer over the strip; focus")
        a = self.x.attrs(child)
        self.check(not (a.all_event_masks & KEY_MASK) and a.do_not_propagate_mask == 0,
                   f"strip window: all clients' event masks 0x{a.all_event_masks:x} (no "
                   f"KeyPress/KeyRelease), do-not-propagate 0x{a.do_not_propagate_mask:x}")
        await b.ask("js", code="document.getElementById('t').focus(); document.activeElement.id")
        k0 = (await b.ask("info"))["keys"]
        self.x.type_via(child, top, "strip")
        info, _ = await b.until(lambda i: i["keys"] >= k0 + 5, timeout=2)
        value = (await b.ask("js", code="document.getElementById('t').value"))["value"]
        self.check(info["keys"] - k0 == 5,
                   f"5 keys sent at the strip: Qt's window got {info['keys'] - k0} "
                   f"({info['key_text'][-5:]!r}); page input now {value!r}")
        # Control: someone selecting KeyPress on the strip window takes them.
        self.x.select(child, KEY_MASK)
        self.x.events()
        k0 = info["keys"]
        self.x.type_via(child, top, "xx")
        await asyncio.sleep(0.4)
        stolen = sum((e.response_type & 0x7F) == 2 for _, e in self.x.events())
        k1 = (await b.ask("info"))["keys"]
        self.x.select(child, 0)
        self.check(stolen == 2 and k1 == k0,
                   f"control, KeyPress selected on the strip: it got {stolen} of 2, Qt {k1 - k0}")
        # Control: the same keys straight at the browser's window.
        k0 = (await b.ask("info"))["keys"]
        self.x.type_via(None, top, "abc")
        info, _ = await b.until(lambda i: i["keys"] >= k0 + 3, timeout=2)
        direct = (await b.ask("js", code="document.getElementById('t').value"))["value"]
        focused = (await b.ask("js", code="document.hasFocus()"))["value"]
        self.check(info["keys"] - k0 == 3 and direct == value,
                   f"control, 3 keys sent straight to the browser window: Qt got "
                   f"{info['keys'] - k0}, page input {direct!r}: same as through the strip")
        self.note(f"Qt handed every key to {info['key_targets']}; window active "
                  f"{info['active']}, document.hasFocus() {focused}: the test window "
                  f"refuses focus, so the page types nothing either way")
        stats = await s1.request({"stats": True})
        self.check(stats["keys"] == 0, f"key/focus events reaching the strip process: "
                   f"{stats['keys']}")

        # ── 5. fullscreen ───────────────────────────────────────────────
        S("\n5. fullscreen (band 0 and back; the window keeps its size in the test)")
        since = len(s1.events)
        t0 = (await b.ask("fullscreen", on=True))["t"]
        ev = await s1.event(lambda e: e.get("ev") == "map" and not e["mapped"], since)
        await asyncio.sleep(0.3)
        info = await b.ask("info")
        a = self.x.attrs(child)
        rgb = self.x.band(top, "composite pixmap", info["geometry"][2])
        self.check(ev is not None and a.map_state == 0 and info["view"][1] == 0,
                   f"band 0: strip unmapped {after(ev, t0)} after the call, "
                   f"map_state {a.map_state}, page view at y={info['view'][1]}")
        self.check(share_matching(rgb, frame["rgb"]) < 0.5 and share_of(rgb, GREEN) == 0,
                   f"top rows are the page now: match with strip {share_matching(rgb, frame['rgb']):.1%}, "
                   f"test green {share_of(rgb, GREEN):.1%}")
        since = len(s1.events)
        t0 = (await b.ask("fullscreen", on=False))["t"]
        ev = await s1.event(lambda e: e.get("ev") == "map" and e["mapped"], since)
        await asyncio.sleep(0.3)
        info = await b.ask("info")
        frame = await s1.frame(top)
        rgb = self.x.band(top, "composite pixmap", info["geometry"][2])
        self.check(ev is not None and self.x.attrs(child).map_state == 2
                   and share_matching(rgb, frame["rgb"]) > 0.999,
                   f"band back: strip mapped {after(ev, t0)} after the call, "
                   f"band = strip frame {share_matching(rgb, frame['rgb']):.1%}")
        self.sample_focus()

        # ── 6a. a second window ─────────────────────────────────────────
        S("\n6a. a second browser window, then closing it")
        r1 = await self.resources(s1)
        win = (await b.ask("open", url=f"{self.base}/c"))["win"]
        info2, dt = await b.until(lambda i: i.get("valid") and i["attached"]
                                  and i["title"] == "Page C", timeout=15, win=win)
        top2 = info2["wid"]
        self.ours.add(top2)
        stats = await s1.request({"stats": True})
        self.check(dt is not None and len(stats["strips"]) == 2,
                   f"two strips: {[hex(s['parent']) for s in stats['strips']]}")
        await asyncio.sleep(0.3)
        f2 = await s1.frame(top2)
        rgb = self.x.band(top2, "composite pixmap", info2["geometry"][2])
        self.check(share_matching(rgb, f2["rgb"]) > 0.999,
                   f"second band = its strip frame {share_matching(rgb, f2['rgb']):.1%}")
        r2 = await self.resources(s1)
        since = len(s1.events)
        await b.ask("close", win=win)
        ev = await s1.event(lambda e: e.get("ev") == "detached", since)
        await asyncio.sleep(0.3)
        r3 = await self.resources(s1)
        stats = await s1.request({"stats": True})
        self.check(ev is not None and len(stats["strips"]) == 1 and r3 == r1,
                   f"closed: strip dropped it ({ev and ev['reason']!r}); strip process X "
                   f"resources {r1} -> {r2} -> {r3}")
        self.check(stats["x_errors"] == 0 and stats["xlib_errors"] == 0,
                   f"X errors in the strip so far: core {stats['x_errors']}, "
                   f"Xlib/EGL {stats['xlib_errors']}")

        # ── 6b. strip killed under the browser ──────────────────────────
        S("\n6b. strip killed (SIGKILL) while the browser runs")
        t0 = time.monotonic()
        s1.proc.kill()
        await s1.proc.wait()
        info, dt = await b.until(lambda i: not i["attached"], timeout=5, every=0.01)
        await asyncio.sleep(0.3)
        rgb = self.x.band(top, "composite pixmap", info["geometry"][2])
        self.check(dt is not None and self.x.children(top) == [],
                   f"browser noticed in {ms(dt)}; strip windows left in the "
                   f"browser: {len(self.x.children(top))}")
        self.check(share_of(rgb, GREEN) > 0.5 and info["label"] != "",
                   f"label is back: {info['label']!r}, test green {share_of(rgb, GREEN):.1%}")

        # ── 6c. restarted, raster path, default text-only look ──────────
        S("\n6c. strip restarted (raster path, text only) while the browser runs")
        s2 = StripProc(self, ["--raster"], 2)
        await s2.start()
        info, dt = await b.until(lambda i: i["attached"], timeout=8)
        self.check(dt is not None, f"browser re-attached {ms(dt)} after the strip started "
                   f"listening (retry every 2 s)")
        created = await s2.event(lambda e: e.get("ev") == "created")
        child = await self.strip_child(top)
        self.ours.add(child)
        g = self.x.geometry(child)
        self.check(created and created["path"] == "raster",
                   f"path {created and created['path']}; strip child 0x{child:x} depth {g[2]} "
                   f"(copied from the browser's)")
        reads, frame = await self.visibility(b, s2)
        self.raster_reads = {m: share_matching(r, frame["rgb"]) for m, r in reads.items()}
        self.check(self.raster_reads["composite pixmap"] > 0.999,
                   "composite pixmap band = strip frame")
        self.check(frame["targets"] == {}, "no targets drawn by default")
        await b.ask("load", url=f"{self.base}/anim")
        await b.until(lambda i: not i["loading"] and i["title"] == "Moving page")
        await asyncio.sleep(0.3)
        worst, greenest, n, paints = await self.stability(b, s2)
        self.check(worst > 0.999 and greenest == 0,
                   f"page animating: {n} reads, worst match {worst:.2%}, max test green "
                   f"{greenest:.2%}, strip paints meanwhile {paints}")
        S("  width (raster):")
        for to_w in (760, 1000):
            late, cfg, frame = await self.resize_timeline(b, s2, to_w)
            if late is not None:
                self.note(f"=> band right {late:.1f} ms after the browser's ConfigureNotify"
                          + (f" ({late / frame_ms:.2f} frames)" if frame_ms else ""))
        a = self.x.attrs(child)
        k0 = (await b.ask("info"))["keys"]
        await b.ask("js", code="document.getElementById('t') && document.getElementById('t').focus()")
        self.x.type_via(child, top, "ok")
        info, _ = await b.until(lambda i: i["keys"] >= k0 + 2, timeout=2)
        self.check(not (a.all_event_masks & KEY_MASK) and info["keys"] - k0 == 2,
                   f"keys at the raster strip reach Qt: {info['keys'] - k0} of 2")
        self.sample_focus()

        # ── 6d. browser closed while the strip runs ─────────────────────
        S("\n6d. browser closed while the strip runs")
        r_attached = await self.resources(s2)
        since = len(s2.events)
        await b.ask("quit")
        code = await asyncio.wait_for(b.proc.wait(), 15)
        ev = await s2.event(lambda e: e.get("ev") == "detached", since)
        await asyncio.sleep(0.3)
        stats = await s2.request({"stats": True})
        r_after = await self.resources(s2)
        self.check(ev is not None and stats["strips"] == [],
                   f"browser exited ({code}); strip dropped it: {ev and ev['reason']!r}")
        self.check(r_after.get("WINDOW", 0) == 0 and r_after.get("GC", 0) == 0,
                   f"strip process X resources {r_attached} -> {r_after}")
        self.check(stats["x_errors"] == 0, f"X errors in the strip: {stats['x_errors']}")

        # ── 6e. browser started while the strip runs, then strip stops ──
        S("\n6e. browser started while the strip runs; then the strip exits (SIGTERM)")
        b2 = Browser(self, f"{self.base}/a", 1)
        t0 = time.monotonic()
        await b2.start()
        info, _ = await b2.until(lambda i: i["attached"], timeout=30)
        self.check(info["attached"], f"attached {time.monotonic() - t0:.1f} s after the browser "
                   f"process started (QtWebEngine start-up included), no retry needed")
        top3 = info["wid"]
        self.ours.add(top3)
        for c in self.x.children(top3):
            self.ours.add(c)
        s2.proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(s2.proc.wait(), 10)
        info, dt = await b2.until(lambda i: not i["attached"], timeout=5, every=0.01)
        self.check(dt is not None and self.x.children(top3) == []
                   and not os.path.exists(self.sock),
                   f"strip exited ({s2.proc.returncode}); browser detached in "
                   f"{ms(dt)}; windows left {len(self.x.children(top3))}; "
                   f"socket removed {not os.path.exists(self.sock)}")

        # ── 6f. GL again, the strip window screen-wide (--wide) ─────────
        S("\n6f. a third strip: GL with a screen-wide window the browser clips (--wide)")
        s3 = StripProc(self, ["--wide"], 3)
        await s3.start()
        info, dt = await b2.until(lambda i: i["attached"], timeout=8)
        self.check(dt is not None, f"browser re-attached {ms(dt)} after the strip started")
        child = await self.strip_child(top3)
        self.ours.add(child)
        stats = await s3.request({"stats": True})
        st = stats["strips"][0]
        self.check(st["path"] == "gl" and st["child_width"] > st["width"],
                   f"path {st['path']}, strip window {st['child_width']} px wide in a "
                   f"{st['width']} px browser")
        reads, frame = await self.visibility(b2, s3)
        self.wide_reads = {m: share_matching(r, frame["rgb"]) for m, r in reads.items()}
        self.check(self.wide_reads["composite pixmap"] > 0.999,
                   "composite pixmap band = strip frame")
        S("  width (GL, --wide):")
        for to_w in (760, 1000):
            late, cfg, frame = await self.resize_timeline(b2, s3, to_w)
            if late is not None:
                self.note(f"=> band right {late:.1f} ms after the browser's ConfigureNotify"
                          + (f" ({late / frame_ms:.2f} frames)" if frame_ms else ""))
        await b2.ask("quit")
        await asyncio.wait_for(b2.proc.wait(), 15)
        await asyncio.sleep(0.3)
        stats = await s3.request({"stats": True})
        self.check(stats["strips"] == [] and stats["x_errors"] == 0
                   and stats["xlib_errors"] == 0,
                   f"browser quit: strips left {len(stats['strips'])}, X errors core "
                   f"{stats['x_errors']}, Xlib/EGL {stats['xlib_errors']}")
        s3.proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(s3.proc.wait(), 10)

        # ── focus, errors, Qt ───────────────────────────────────────────
        S("\n7. focus, errors, Qt")
        self.sample_focus()
        theirs = self.focus_seen - self.ours
        self.check(not (self.focus_seen & self.ours),
                   f"focus never on our windows: {self.focus_reads} readings over the run, "
                   f"{len(theirs)} distinct focus windows, none of them ours")
        for s in (s1, s2, s3):
            core, xlib = s.errors_in_log()
            self.check(core == 0 and xlib == 0,
                       f"strip run {s.n} log: X protocol errors {core}, Xlib errors {xlib}")
        for n in (0, 1):
            text = (self.scratch / f"browser-{n}.log").read_text(errors="replace")
            hits = sorted({line.strip() for line in text.splitlines()
                           if re.search(r"xcb|X Error|BadWindow|BadMatch|BadDrawable|"
                                        r"qt\.qpa|QWidget|QWindow|geometry", line)})
            self.check(not hits, f"browser run {n} stderr: {len(hits)} X/Qt window warnings"
                       + "".join(f"\n           {h[:150]}" for h in hits[:6]))

        S("\nwhich reads of the browser window include the strip (match with its frame):")
        for m in self.gl_reads:
            self.note(f"{m:28} GL {self.gl_reads[m]:6.1%}   raster "
                      f"{self.raster_reads[m]:6.1%}   GL wide {self.wide_reads[m]:6.1%}")

    async def cleanup(self):
        for proc in self.procs:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        try:
            os.unlink(self.sock)
        except FileNotFoundError:
            pass
        self.http.shutdown()


async def amain(args) -> int:
    test = Test(args.scratch or tempfile.mkdtemp(prefix=f"{APP}-strip-test-"))
    try:
        await asyncio.wait_for(test.run(), 240)
    except Exception as e:
        test.failures += 1
        test.say(f"\nABORTED: {e!r}")
        import traceback
        traceback.print_exc()
    finally:
        await test.cleanup()
    test.say(f"\n{'PASS' if test.failures == 0 else f'{test.failures} FAILED'}")
    return 0 if test.failures == 0 else 1


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="strip.py --selftest")
    parser.add_argument("--scratch", help="profile, logs (default: a new temp dir)")
    args = parser.parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
