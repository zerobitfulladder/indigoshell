"""Interactive braindance-style glitch panel as a real X11 floating window.

This is a working prototype of the proposed GTK-free indigoshell stack:

  xcffib  — window creation (override-redirect, centered), input events,
            seat grabs (keyboard + pointer, like indigoshell chord menus)
  skia    — CPU raster rendering of the panel UI
  SkSL    — per-frame post-processing (RGB separation, glitch bands,
            scanlines, materialize) on the CPU, no GL context
  PutImage— chunked pixel upload to the window (no SHM needed at this size)

Interactivity (keyboard is grabbed while open, exactly like a chord menu):
  Tab / Shift+Tab   switch tab            (big glitch burst)
  Up / Down         move selection        (small burst)
  Left / Right      adjust value          (medium burst; bool/enum/num)
  Enter / Escape / q  dissolve out and close
  click             select row / switch tab; click outside panel closes

Run:   uv run --no-project --with skia-python --with xcffib python panel_x11.py
Smoke: ... panel_x11.py --smoke 40   (no grabs, self-verifies, auto-exits)
"""

import selectors
import sys
import time

import skia
import xcffib
import xcffib.xproto as xp

# ── palette (braindance render.py _PAL) ─────────────────────────────────
PAL = {
    "bg": (10, 4, 24), "border": (255, 42, 109), "magenta": (255, 42, 109),
    "cyan": (5, 217, 232), "yellow": (252, 238, 12), "lime": (204, 255, 0),
    "fg": (160, 168, 200), "fg_hi": (200, 208, 232),
    "muted": (90, 74, 120), "sel_bg": (36, 24, 64), "sect_bg": (26, 16, 48),
    "black": (5, 3, 16),
}


def C(name, a=255):
    return skia.ColorSetARGB(a, *PAL[name])


W, H = 620, 420
MARGIN = 24
PW, PH = W - 2 * MARGIN, H - 2 * MARGIN
FPS = 20
BEVEL = 26                        # 45° cut on the top-right corner

_p = skia.Path()
_p.moveTo(0, 0)
_p.lineTo(PW - BEVEL, 0)
_p.lineTo(PW, BEVEL)
_p.lineTo(PW, PH)
_p.lineTo(0, PH)
_p.close()
PANEL_PATH = _p

# ── fonts ───────────────────────────────────────────────────────────────
def load_typeface(bold=False):
    style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
    for fam in ("JetBrainsMono Nerd Font", "JetBrains Mono", "Hack",
                "DejaVu Sans Mono", "monospace"):
        try:
            tf = skia.FontMgr().matchFamilyStyle(fam, style)
        except Exception:
            tf = None
        if tf is not None:
            return tf
    return None


TF, TF_B = load_typeface(), load_typeface(bold=True)
F = skia.Font(TF, 15)
F_B = skia.Font(TF_B or TF, 18)
F_S = skia.Font(TF, 12)


def text(canvas, s, x, y, f, color):
    p = skia.Paint(AntiAlias=True)
    p.setColor(color)
    canvas.drawString(s, x, y, f, p)


def fill(canvas, x, y, w, h, color):
    p = skia.Paint(AntiAlias=False)
    p.setColor(color)
    canvas.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


def stroke_rect(canvas, x, y, w, h, color, sw):
    p = skia.Paint(AntiAlias=False)
    p.setColor(color)
    p.setStyle(skia.Paint.kStroke_Style)
    p.setStrokeWidth(sw)
    canvas.drawRect(skia.Rect.MakeXYWH(x, y, w, h), p)


# ── menu model (mini version of braindance menu.py) ─────────────────────
def S(label):
    return {"section": True, "label": label}


def num(label, value, step, lo, hi):
    return {"label": label, "kind": "num", "value": value,
            "step": step, "lo": lo, "hi": hi}


def boolean(label, value):
    return {"label": label, "kind": "bool", "value": value}


def enum(label, value, options):
    return {"label": label, "kind": "enum", "value": value, "options": options}


TABS = [
    ("DISPLAY", [S("VIEW"), enum("MODE", "sbs", ["sbs", "mono", "anaglyph"]),
                 num("ZOOM", 1.0, 0.05, 0.5, 2.0),
                 enum("SUPERSAMPLE", "off", ["off", "1.5x", "2x"])]),
    ("DEPTH", [S("MODEL"), enum("MODEL", "vitl14", ["vitl14", "vitb14", "vits14"]),
               S("PIPELINE"), boolean("TENSORRT", True), boolean("SYNC", False),
               num("DEPTH EMA", 0.55, 0.05, 0.0, 1.0)]),
    ("3D", [S("GEOMETRY"), num("3D STRENGTH", 24, 1, 0, 64),
            num("SCREEN DEPTH", 12, 1, -32, 64),
            num("COMFORT", 0.35, 0.05, 0.0, 1.0)]),
    ("TEAR", [boolean("DEPTH TEAR", True),
              enum("TEAR FILL", "stretch", ["stretch", "inpaint"]),
              num("TEAR THRESH", 0.08, 0.01, 0.0, 0.5)]),
    ("REFINE", [enum("SHARP MODE", "jbu", ["jbu", "guided", "off"]),
                num("JBU REACH", 12, 1, 1, 32),
                num("GF EPS", 0.02, 0.01, 0.0, 0.2)]),
    ("DEBUG", [enum("DEPTH VIEW", "off", ["off", "gray", "heat"]),
               boolean("HUD", False)]),
]


class Menu:
    def __init__(self, tabs):
        self.tabs = tabs
        self.t = 0
        self.sel = 0

    def items(self):
        return self.tabs[self.t][1]

    def nav(self):
        return [it for it in self.items() if not it.get("section")]

    def current(self):
        nav = self.nav()
        self.sel = min(self.sel, len(nav) - 1)
        return nav[self.sel]

    def switch_tab(self, direction):
        self.t = (self.t + direction) % len(self.tabs)
        self.sel = 0
        print(f"[panel] tab -> {self.tabs[self.t][0]}", flush=True)

    def move(self, direction):
        nav = self.nav()
        self.sel = (min(self.sel, len(nav) - 1) + direction) % len(nav)

    def adjust(self, direction):
        it = self.current()
        v = it["value"]
        if it["kind"] == "bool":
            nv = not v
        elif it["kind"] == "enum":
            i = it["options"].index(v)
            nv = it["options"][(i + direction) % len(it["options"])]
        else:
            nv = min(it["hi"], max(it["lo"], v + direction * it["step"]))
            if isinstance(v, int) and not isinstance(it["step"], float):
                nv = int(round(nv))
        if nv == v:
            return False
        it["value"] = nv
        print(f"[panel] {it['label']} -> {self.display(it)}", flush=True)
        return True

    @staticmethod
    def display(it):
        v = it["value"]
        if it["kind"] == "bool":
            return "ON" if v else "OFF"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)


def val_color(it):
    if it["kind"] == "bool":
        return C("lime") if it["value"] else C("muted")
    if it["kind"] == "enum":
        return C("cyan")
    return C("yellow")


# ── panel drawing (braindance set_panel layout) ─────────────────────────
def draw_ui(canvas, menu, fps_now, backdrop=None):
    layout = {"tabs": [], "rows": []}
    canvas.clear(skia.ColorSetARGB(0, 0, 0, 0))   # transparent sheet
    canvas.save()
    canvas.translate(MARGIN, MARGIN)

    # Frosted backdrop clipped EXACTLY to the beveled outline — the blur is
    # part of our own paint, so it cannot leak past the corner cut the way
    # rectangular compositor blur does.
    canvas.save()
    canvas.clipPath(PANEL_PATH, skia.ClipOp.kIntersect, True)
    if backdrop is not None:
        canvas.drawImage(backdrop, -MARGIN, -MARGIN)
    fill(canvas, 0, 0, PW, PH, skia.ColorSetARGB(205, *PAL["bg"]))
    canvas.restore()

    bp = skia.Paint(AntiAlias=True)
    bp.setColor(C("border"))
    bp.setStyle(skia.Paint.kStroke_Style)
    bp.setStrokeWidth(2)
    canvas.drawPath(PANEL_PATH, bp)
    # yellow accent riding the inside of the bevel cut
    bp.setColor(C("yellow"))
    bp.setStrokeWidth(4)
    canvas.drawLine(PW - BEVEL + 1, 9, PW - 9, BEVEL - 1, bp)

    pad = 28
    text(canvas, "BRAINDANCE", pad, 30, F_B, C("magenta"))
    tw = F_B.measureText("BRAINDANCE")
    text(canvas, "v4.0-NC", pad + tw + 14, 30, F_S, C("yellow"))

    tx, ty = pad, 46
    for i, (name, _) in enumerate(menu.tabs):
        w = F.measureText(name) + 22
        if i == menu.t:
            fill(canvas, tx, ty, w, 30, C("sel_bg"))
            fill(canvas, tx, ty + 28, w, 2, C("magenta"))
            text(canvas, name, tx + 11, ty + 20, F, C("fg_hi"))
        else:
            text(canvas, name, tx + 11, ty + 20, F, C("muted"))
        layout["tabs"].append((MARGIN + tx, MARGIN + tx + w, i))
        tx += w + 5
    fill(canvas, pad, ty + 36, PW - 2 * pad, 1, C("muted"))

    cw, lh = PW - 2 * pad, 34
    y = ty + 48
    nav_i = 0
    cur = menu.current()
    for it in menu.items():
        if it.get("section"):
            y += 6
            fill(canvas, pad, y, cw, 24, C("sect_bg"))
            fill(canvas, pad, y, 5, 24, C("cyan"))
            text(canvas, it["label"], pad + 14, y + 17, F_S, C("cyan"))
            y += 24 + 6
            continue
        sel = it is cur
        if sel:
            fill(canvas, pad, y - 4, cw, 30, C("sel_bg"))
            fill(canvas, pad, y - 4, 5, 30, C("magenta"))
        text(canvas, it["label"], pad + 16, y + 16, F,
             C("fg_hi") if sel else C("fg"))
        val, col = Menu.display(it), val_color(it)
        vx = pad + cw - 150
        if sel:
            text(canvas, "‹", vx - 22, y + 16, F, C("magenta"))
            text(canvas, val, vx, y + 16, F, col)
            w = F.measureText(val)
            text(canvas, "›", vx + w + 8, y + 16, F, C("magenta"))
        else:
            text(canvas, val, vx, y + 16, F, col)
        layout["rows"].append((MARGIN + y - 4, MARGIN + y + 30, nav_i))
        nav_i += 1
        y += lh

    fill(canvas, pad, PH - 44, PW - 2 * pad, 1, C("muted"))
    fcol = C("lime") if fps_now >= 18 else C("yellow")
    text(canvas, "FPS", pad, PH - 20, F_S, C("fg"))
    text(canvas, f"{fps_now:.0f}", pad + 46, PH - 20, F_S, fcol)
    hints = "↑↓ ←→   TAB   ESC"
    text(canvas, hints, PW - pad - F_S.measureText(hints), PH - 20, F_S,
         C("muted"))

    canvas.restore()
    return layout


# ── SkSL post-processing (same shader as panel_glitch.py) ───────────────
SKSL = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float g;
uniform float appear;

float hash(float x) { return fract(sin(x) * 43758.5453); }

half4 main(float2 xy) {
    float2 uv = xy;
    float band = floor(xy.y / 7.0);

    float seed = floor(t * 24.0);
    float h1   = hash(band * 91.7 + seed * 13.1);
    uv.x += (h1 - 0.5) * 46.0 * g * step(0.72, h1);

    float sep = 1.1 + 12.0 * g;
    half4 sc = src.eval(uv);
    half4 sr = src.eval(float2(uv.x - sep, uv.y));
    half4 sb = src.eval(float2(uv.x + sep, uv.y));
    half3 col = half3(sr.r, sc.g, sb.b);
    half a = max(sc.a, max(sr.a, sb.a));   // fringes keep their own alpha

    float h2 = hash(band * 17.3 + seed * 7.7);
    if (g > 0.15 && h2 > 0.94) {
        half m = half(dot(col, half3(0.3333)));
        col = half3(m * 0.15, m * 1.05, m * 1.2);
    }

    // subtle scanlines with a lifted floor + mild gain (brighter)
    col *= half(0.94 + 0.06 * sin(xy.y * 3.14159));
    col *= half(1.12);

    float n = fract(sin(dot(xy, float2(12.9898, 78.233)) + t * 57.0) * 43758.5453);
    col += half3(half((n - 0.5) * (0.04 + 0.25 * g))) * a;

    float2 vc = xy / res - 0.5;
    col *= half(1.0 - 0.25 * dot(vc, vc) * 2.0);

    if (appear < 1.0) {
        float on = step(hash(band * 3.7 + 5.0), appear);
        col *= half(on * (0.35 + 0.65 * appear));
        a   *= half(on * appear);          // bands dissolve to transparent
    }
    col = min(col, half3(a));              // keep premultiplied output valid
    return half4(col, a);
}
"""

# ── keysyms we care about ───────────────────────────────────────────────
XK_ESCAPE, XK_RETURN, XK_TAB, XK_ISO_LEFT_TAB = 0xFF1B, 0xFF0D, 0xFF09, 0xFE20
XK_LEFT, XK_UP, XK_RIGHT, XK_DOWN, XK_Q = 0xFF51, 0xFF52, 0xFF53, 0xFF54, 0x71


class Panel:
    def __init__(self, smoke_frames=0):
        self.smoke = smoke_frames
        self.menu = Menu(TABS)
        self.g = 0.0
        self.appear = 0.0
        self.closing = False
        self.frame = 0
        self.layout = {"tabs": [], "rows": []}
        self.fps_now = float(FPS)

        self.conn = xcffib.connect()
        setup = self.conn.get_setup()
        self.screen = setup.roots[0]
        sw, sh = self.screen.width_in_pixels, self.screen.height_in_pixels
        x, y = (sw - W) // 2, (sh - H) // 2

        # Prefer a 32-bit ARGB visual → real transparency under a compositor.
        self.depth, visual = self.screen.root_depth, self.screen.root_visual
        for d in self.screen.allowed_depths:
            if d.depth == 32 and d.visuals:
                self.depth, visual = 32, d.visuals[0].visual_id
                break
        print(f"[panel] visual depth {self.depth}"
              + ("" if self.depth == 32 else " — no ARGB visual, opaque!"),
              flush=True)
        self.wid = self.conn.generate_id()
        cmap = self.conn.generate_id()
        self.conn.core.CreateColormap(xp.ColormapAlloc._None, cmap,
                                      self.screen.root, visual)
        events = (xp.EventMask.Exposure | xp.EventMask.KeyPress
                  | xp.EventMask.ButtonPress)
        self.conn.core.CreateWindow(
            self.depth, self.wid, self.screen.root,
            x, y, W, H, 0, xp.WindowClass.InputOutput, visual,
            xp.CW.BackPixel | xp.CW.BorderPixel | xp.CW.OverrideRedirect
            | xp.CW.EventMask | xp.CW.Colormap,
            [0, 0, 1, events, cmap])
        name = b"braindance-panel"
        self.conn.core.ChangeProperty(xp.PropMode.Replace, self.wid,
                                      xp.Atom.WM_NAME, xp.Atom.STRING, 8,
                                      len(name), name)
        self.gc = self.conn.generate_id()
        self.conn.core.CreateGC(self.gc, self.wid, xp.GC.Foreground,
                                [self.screen.black_pixel])

        # unshifted keysym per keycode
        mn, mx = setup.min_keycode, setup.max_keycode
        km = self.conn.core.GetKeyboardMapping(mn, mx - mn + 1).reply()
        per = km.keysyms_per_keycode
        self.keysym = {mn + i: km.keysyms[i * per] for i in range(mx - mn + 1)}

        # Capture what's behind us BEFORE mapping, blur it once with Skia.
        # Static snapshot — fine while a modal seat grab freezes the desktop.
        self.backdrop = None
        try:
            shot = self.conn.core.GetImage(
                xp.ImageFormat.ZPixmap, self.screen.root, x, y, W, H,
                0xFFFFFFFF).reply()
            raw = bytearray(shot.data)
            raw[3::4] = b"\xff" * (len(raw) // 4)   # root pad bytes -> opaque
            info = skia.ImageInfo.Make(
                W, H, skia.ColorType.kBGRA_8888_ColorType,
                skia.AlphaType.kPremul_AlphaType)
            base = skia.Image.MakeRasterData(info, bytes(raw), W * 4)
            try:
                flt = skia.ImageFilters.Blur(14, 14, skia.TileMode.kClamp)
            except TypeError:
                flt = skia.ImageFilters.Blur(14, 14)
            surf = skia.Surface(W, H)
            surf.getCanvas().drawImage(base, 0, 0, skia.SamplingOptions(),
                                       skia.Paint(ImageFilter=flt))
            self.backdrop = surf.makeImageSnapshot()
        except Exception as e:
            print(f"[panel] backdrop blur unavailable: {e}", flush=True)

        self.conn.core.MapWindow(self.wid)
        self.conn.flush()

        self.grabbed = False
        if not self.smoke:
            self.grab()

        self.ui_surface = skia.Surface(W, H)   # N32 == BGRA here (verified)
        self.out_surface = skia.Surface(W, H)
        self.effect = skia.RuntimeEffect.MakeForShader(SKSL)
        self.builder = skia.RuntimeEffectBuilder(self.effect)
        self.sampling = skia.SamplingOptions()

    def grab(self):
        for _ in range(20):                      # grabs can race; retry briefly
            r = self.conn.core.GrabKeyboard(
                False, self.wid, xp.Time.CurrentTime,
                xp.GrabMode.Async, xp.GrabMode.Async).reply()
            if r.status == xp.GrabStatus.Success:
                self.grabbed = True
                break
            time.sleep(0.05)
        self.conn.core.GrabPointer(
            False, self.wid, xp.EventMask.ButtonPress,
            xp.GrabMode.Async, xp.GrabMode.Async, 0, 0, xp.Time.CurrentTime).reply()
        self.conn.flush()
        if not self.grabbed:
            print("[panel] WARNING: keyboard grab failed; keys may not arrive",
                  flush=True)

    def ungrab(self):
        try:
            self.conn.core.UngrabKeyboard(xp.Time.CurrentTime)
            self.conn.core.UngrabPointer(xp.Time.CurrentTime)
            self.conn.flush()
        except Exception:
            pass

    # ── rendering ──────────────────────────────────────────────────────
    def render(self):
        self.layout = draw_ui(self.ui_surface.getCanvas(), self.menu,
                              self.fps_now, self.backdrop)
        child = self.ui_surface.makeImageSnapshot().makeShader(
            skia.TileMode.kClamp, skia.TileMode.kClamp, self.sampling)
        b = self.builder
        b.setChild("src", child)
        b.setUniform("res", [float(W), float(H)])
        b.setUniform("t", self.frame / FPS)
        b.setUniform("g", self.g)
        b.setUniform("appear", self.appear)
        paint = skia.Paint(Shader=b.makeShader())
        # Replace pixels outright — SrcOver would composite each frame onto
        # the previous one, accumulating ghosts and saturating the alpha.
        paint.setBlendMode(skia.BlendMode.kSrc)
        self.out_surface.getCanvas().drawPaint(paint)
        self.out_surface.flushAndSubmit()
        self.upload(self.out_surface.makeImageSnapshot().tobytes())

    def upload(self, pixels):
        stride = W * 4
        rows_per = max(1, 60000 // stride)       # stay under request limits
        for y0 in range(0, H, rows_per):
            h = min(rows_per, H - y0)
            chunk = pixels[y0 * stride:(y0 + h) * stride]
            self.conn.core.PutImage(
                xp.ImageFormat.ZPixmap, self.wid, self.gc, W, h, 0, y0,
                0, self.depth, len(chunk), chunk)
        self.conn.flush()

    # ── input ──────────────────────────────────────────────────────────
    def burst(self, amount):
        self.g = max(self.g, amount)

    def begin_close(self, why):
        if not self.closing:
            print(f"[panel] closed: {why}", flush=True)
            self.closing = True
            self.burst(0.6)

    def on_key(self, ev):
        ks = self.keysym.get(ev.detail, 0)
        shift = bool(ev.state & xp.KeyButMask.Shift)
        if ks in (XK_ESCAPE, XK_RETURN, XK_Q):
            self.begin_close("key")
        elif ks == XK_ISO_LEFT_TAB or (ks == XK_TAB and shift):
            self.menu.switch_tab(-1); self.burst(0.9)
        elif ks == XK_TAB:
            self.menu.switch_tab(+1); self.burst(0.9)
        elif ks == XK_UP:
            self.menu.move(-1); self.burst(0.25)
        elif ks == XK_DOWN:
            self.menu.move(+1); self.burst(0.25)
        elif ks == XK_LEFT:
            if self.menu.adjust(-1): self.burst(0.5)
        elif ks == XK_RIGHT:
            if self.menu.adjust(+1): self.burst(0.5)

    def on_click(self, ev):
        x, y = ev.event_x, ev.event_y
        if not (0 <= x < W and 0 <= y < H):
            self.begin_close("outside click")
            return
        for x0, x1, i in self.layout["tabs"]:
            if x0 <= x < x1 and MARGIN + 40 <= y <= MARGIN + 82:
                if i != self.menu.t:
                    self.menu.t, self.menu.sel = i, 0
                    print(f"[panel] tab -> {self.menu.tabs[i][0]}", flush=True)
                    self.burst(0.9)
                return
        for y0, y1, i in self.layout["rows"]:
            if y0 <= y < y1:
                self.menu.sel = i
                self.burst(0.25)
                return

    # ── main loop ──────────────────────────────────────────────────────
    def run(self):
        sel = selectors.DefaultSelector()
        sel.register(self.conn.get_file_descriptor(), selectors.EVENT_READ)
        frame_dt = 1.0 / FPS
        next_frame = time.monotonic()
        last_activity = time.monotonic()
        t_last = time.monotonic()
        try:
            while True:
                timeout = max(0.0, next_frame - time.monotonic())
                sel.select(timeout)
                while True:
                    ev = self.conn.poll_for_event()
                    if ev is None:
                        break
                    last_activity = time.monotonic()
                    if isinstance(ev, xp.KeyPressEvent) and not self.smoke:
                        self.on_key(ev)
                    elif isinstance(ev, xp.ButtonPressEvent) and not self.smoke:
                        self.on_click(ev)

                now = time.monotonic()
                if now >= next_frame:
                    if self.closing:
                        self.appear = max(0.0, self.appear - 1.0 / 6)
                        if self.appear <= 0.0:
                            self.render()
                            break
                    else:
                        self.appear = min(1.0, self.appear + 1.0 / 6)
                    self.render()
                    self.g *= 0.82
                    self.frame += 1
                    inst = 1.0 / max(1e-6, now - t_last)
                    self.fps_now += 0.1 * (min(inst, 99) - self.fps_now)
                    t_last = now
                    next_frame += frame_dt
                    if next_frame < now:          # fell behind; don't spiral
                        next_frame = now + frame_dt

                    if self.smoke and self.frame >= self.smoke:
                        self.verify()
                        break
                    if self.smoke and self.frame == self.smoke // 2:
                        self.burst(1.0)           # show a burst in smoke mode
                # safety: never hold a seat grab forever
                if self.grabbed and time.monotonic() - last_activity > 180:
                    self.begin_close("idle timeout")
        finally:
            self.ungrab()
            try:
                self.conn.core.UnmapWindow(self.wid)
                self.conn.flush()
                self.conn.disconnect()
            except Exception:
                pass

    def verify(self):
        """Smoke mode: read the window back through X and sanity-check it."""
        img = self.conn.core.GetImage(
            xp.ImageFormat.ZPixmap, self.wid, 0, 0, W, H, 0xFFFFFFFF).reply()
        data = bytes(img.data)
        colors = {data[i:i + 3] for i in range(0, min(len(data), 400000), 4)}
        alphas = [data[i + 3] for i in range(0, min(len(data), 400000), 4)]
        translucent = sum(1 for a in alphas if a < 250)
        print(f"[smoke] readback {len(data)} bytes, {len(colors)} distinct "
              f"colors, alpha min/max {min(alphas)}/{max(alphas)}, "
              f"{translucent} translucent px in sample — "
              f"{'OK' if len(colors) > 50 and translucent > 0 else 'SUSPICIOUS'}",
              flush=True)
        info = skia.ImageInfo.Make(W, H, skia.ColorType.kBGRA_8888_ColorType,
                                   skia.AlphaType.kUnpremul_AlphaType)
        skia.Image.MakeRasterData(info, data, W * 4).save(
            "smoke_readback.png", skia.kPNG)
        print("[smoke] wrote smoke_readback.png", flush=True)


if __name__ == "__main__":
    smoke = 0
    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        smoke = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 40
    Panel(smoke_frames=smoke).run()
    print("[panel] exited cleanly", flush=True)
