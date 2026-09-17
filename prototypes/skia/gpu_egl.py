"""GPU path proof: EGL on the xcffib connection, Skia rendering to FBO 0.

The CPU raster path costs two things a GPU surface does not:

  * the SkSL pass is per-pixel work on the CPU (~8ms for the settings
    panel's ScanLock at 928x628), and
  * every frame is read back and pushed over the X socket — 2.3MB and
    40 PutImage requests, 140MB/s at 60fps.

Both vanish if Skia renders straight into a GL framebuffer backed by the
window and we swap buffers instead of uploading.

The binding is EGL, on `EGL_EXT_platform_x11` — which means a second,
Xlib connection alongside xcffib's.

That is worth stating because the obvious alternative silently does the
wrong thing. `EGL_EXT_platform_xcb` also exists here and would take
xcffib's `xcb_connection_t *` directly with no interop at all — but
NVIDIA's EGL does not implement it, so GLVND falls through to Mesa,
which has no DRI driver for this card and hands back **llvmpipe**. It
initialises, it renders, it is entirely software, and nothing in the API
says so. Only `glGetString(GL_RENDERER)` tells you:

    EGL_PLATFORM_XCB_EXT -> Mesa Project -> llvmpipe (LLVM 22.1.8)
    EGL_PLATFORM_X11_EXT -> NVIDIA      -> NVIDIA GeForce RTX 3060

The window itself is still created by xcffib. An X window ID is valid on
any connection to the same server, so EGL only needs its own connection
to query the window and present to it.

Run:
    .venv/bin/python prototypes/skia/gpu_egl.py            # ~4s, self-closing
    .venv/bin/python prototypes/skia/gpu_egl.py --bench    # frame timings
"""

import ctypes
import math
import sys
import time

import skia
import xcffib
import xcffib.xproto as xp

# ── EGL constants ───────────────────────────────────────────────────────
EGL_PLATFORM_X11_EXT = 0x31D5
EGL_NONE = 0x3038
EGL_SURFACE_TYPE, EGL_WINDOW_BIT = 0x3033, 0x0004
EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT = 0x3040, 0x0008
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = (
    0x3024, 0x3023, 0x3022, 0x3021)
EGL_DEPTH_SIZE, EGL_STENCIL_SIZE = 0x3025, 0x3026
EGL_NATIVE_VISUAL_ID = 0x302E
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_VENDOR, EGL_EXTENSIONS = 0x3053, 0x3055
GL_RGBA8 = 0x8058

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]

egl = ctypes.CDLL("libEGL.so.1")
egl.eglGetProcAddress.restype = ctypes.c_void_p
egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
egl.eglQueryString.restype = ctypes.c_char_p
egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                              ctypes.POINTER(ctypes.c_int)]
egl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                ctypes.POINTER(ctypes.c_int)]
egl.eglGetConfigAttrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
egl.eglCreateContext.restype = ctypes.c_void_p
egl.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
egl.eglCreateWindowSurface.restype = ctypes.c_void_p
egl.eglCreateWindowSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_ulong,
                                       ctypes.POINTER(ctypes.c_int)]
egl.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
egl.eglSwapBuffers.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
egl.eglSwapInterval.argtypes = [ctypes.c_void_p, ctypes.c_int]


def _int_array(values):
    return (ctypes.c_int * len(values))(*values)


class GLWindow:
    """An ARGB override-redirect window with a GL context on it."""

    def __init__(self, conn, width, height):
        self.conn = conn
        self.width, self.height = width, height
        setup = conn.get_setup()
        self.screen = setup.roots[0]

        self._egl_display()
        config, visual_id = self._pick_config()
        self._make_window(visual_id, width, height)
        self._make_context(config)
        self._make_skia()

    # ── EGL ─────────────────────────────────────────────────────────────
    def _egl_display(self):
        exts = (egl.eglQueryString(None, EGL_EXTENSIONS) or b"").decode()
        if "EGL_EXT_platform_x11" not in exts:
            raise RuntimeError("EGL_EXT_platform_x11 missing")
        addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if not addr:
            raise RuntimeError("eglGetPlatformDisplayEXT unavailable")
        proto = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
        get_platform_display = proto(addr)

        # EGL's own Xlib connection to the same server. Only NVIDIA's
        # EGL serves this platform; the XCB one lands on llvmpipe.
        self.xdpy = x11.XOpenDisplay(None)
        if not self.xdpy:
            raise RuntimeError("XOpenDisplay failed")
        self.dpy = get_platform_display(EGL_PLATFORM_X11_EXT,
                                        ctypes.c_void_p(self.xdpy),
                                        _int_array([EGL_NONE]))
        if not self.dpy:
            raise RuntimeError("eglGetPlatformDisplayEXT returned no display")
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(self.dpy, major, minor):
            raise RuntimeError("eglInitialize failed")
        self.egl_version = (major.value, minor.value)
        self.egl_vendor = (egl.eglQueryString(self.dpy, EGL_VENDOR)
                           or b"?").decode()

    def _pick_config(self):
        """A config with alpha whose native visual is a depth-32 one.

        The visual has to match the window's: an ARGB window created with
        a visual EGL didn't pick renders as garbage or fails BadMatch.
        """
        want = _int_array([
            EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
            EGL_ALPHA_SIZE, 8, EGL_DEPTH_SIZE, 0, EGL_STENCIL_SIZE, 8,
            EGL_NONE,
        ])
        configs = (ctypes.c_void_p * 64)()
        n = ctypes.c_int()
        if not egl.eglChooseConfig(self.dpy, want, configs, 64, n) or not n.value:
            raise RuntimeError("no EGL config with alpha + stencil")

        depth32 = {v.visual_id for d in self.screen.allowed_depths
                   if d.depth == 32 for v in d.visuals}
        for i in range(n.value):
            vid = ctypes.c_int()
            egl.eglGetConfigAttrib(self.dpy, configs[i],
                                   EGL_NATIVE_VISUAL_ID, vid)
            if vid.value in depth32:
                return configs[i], vid.value
        raise RuntimeError("no EGL config maps to a depth-32 visual")

    # ── X window ────────────────────────────────────────────────────────
    def _make_window(self, visual_id, w, h):
        conn = self.conn
        self.wid = conn.generate_id()
        cmap = conn.generate_id()
        conn.core.CreateColormap(xp.ColormapAlloc._None, cmap,
                                 self.screen.root, visual_id)
        # BackPixel *and* BorderPixel are both required on a depth-32
        # window or CreateWindow returns BadMatch.
        conn.core.CreateWindow(
            32, self.wid, self.screen.root,
            (self.screen.width_in_pixels - w) // 2,
            (self.screen.height_in_pixels - h) // 2,
            w, h, 0, xp.WindowClass.InputOutput, visual_id,
            xp.CW.BackPixel | xp.CW.BorderPixel | xp.CW.OverrideRedirect
            | xp.CW.EventMask | xp.CW.Colormap,
            [0, 0, 1, xp.EventMask.Exposure | xp.EventMask.StructureNotify,
             cmap])
        conn.core.MapWindow(self.wid)
        # EGL will look this window up over its own connection, so the
        # server has to have processed the CreateWindow before we ask.
        conn.core.GetInputFocus().reply()

    def _make_context(self, config):
        if not egl.eglBindAPI(EGL_OPENGL_API):
            raise RuntimeError("eglBindAPI(OPENGL) failed")
        self.surface = egl.eglCreateWindowSurface(
            self.dpy, config, ctypes.c_ulong(self.wid), None)
        if not self.surface:
            raise RuntimeError("eglCreateWindowSurface failed")
        ctx_attrs = _int_array([EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE])
        self.ctx = egl.eglCreateContext(self.dpy, config, None, ctx_attrs)
        if not self.ctx:
            raise RuntimeError("eglCreateContext failed")
        if not egl.eglMakeCurrent(self.dpy, self.surface, self.surface,
                                  self.ctx):
            raise RuntimeError("eglMakeCurrent failed")
        egl.eglSwapInterval(self.dpy, 0)      # unthrottled, so we can measure

    # ── Skia ────────────────────────────────────────────────────────────
    def _make_skia(self):
        interface = skia.GrGLInterface.MakeEGL()
        if interface is None:
            raise RuntimeError("GrGLInterface.MakeEGL() returned None")
        self.context = skia.GrDirectContexts.MakeGL(interface)
        if self.context is None:
            raise RuntimeError("GrDirectContexts.MakeGL() returned None")
        # FBO 0 is the window's own framebuffer, so Skia draws straight
        # into what eglSwapBuffers presents — no readback anywhere.
        fb = skia.GrGLFramebufferInfo(0, GL_RGBA8)
        target = skia.GrBackendRenderTargets.MakeGL(
            self.width, self.height, 0, 8, fb)
        # `Surfaces.WrapBackendRenderTarget` is also bound but its
        # overload rejects a perfectly good GrDirectContext in this
        # wheel; MakeFromBackendRenderTarget takes the same arguments and
        # works. colorSpace must be None — passing MakeSRGB() trips the
        # same overload mismatch.
        self.skia_surface = skia.Surfaces.MakeFromBackendRenderTarget(
            self.context, target, skia.kBottomLeft_GrSurfaceOrigin,
            skia.kRGBA_8888_ColorType, None)
        if self.skia_surface is None:
            raise RuntimeError("WrapBackendRenderTarget returned None")

    def renderer(self):
        """GL_VENDOR / GL_RENDERER — proof we are not on llvmpipe."""
        gl = ctypes.CDLL("libGL.so.1")
        gl.glGetString.restype = ctypes.c_char_p
        gl.glGetString.argtypes = [ctypes.c_uint]
        GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02
        return tuple((gl.glGetString(k) or b"?").decode()
                     for k in (GL_VENDOR, GL_RENDERER, GL_VERSION))

    def flush(self):
        self.context.flushAndSubmit()
        egl.eglSwapBuffers(self.dpy, self.surface)

    def destroy(self):
        try:
            self.conn.core.DestroyWindow(self.wid)
            self.conn.flush()
        except Exception:
            pass


# ── demo content ────────────────────────────────────────────────────────
SCAN_LOCK = """
uniform shader src;
uniform float2 res;
uniform float t;
uniform float appear;
float h1(float x){ return fract(sin(x * 12.9898) * 43758.5453123); }
float h2(float2 p){ return fract(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453123); }
half4 main(float2 xy) {
    float2 uv = xy / res;
    half4 col = src.eval(xy);
    if (appear < 0.999) {
        float row  = floor(uv.y * 30.0);
        float thr  = h1(row * 1.93 + 0.5);
        float lock = smoothstep(thr, thr + 0.18, appear);
        float unl  = 1.0 - lock;
        float j    = (h1(row + floor(t * 28.0)) - 0.5) * unl;
        float2 tuv = uv + float2(j * 0.30, 0.0);
        float sp   = 0.03 * unl;
        half4 ts = src.eval(tuv * res);
        half3 tc = half3(src.eval((tuv + float2(sp,0.0)) * res).r, ts.g,
                         src.eval((tuv - float2(sp,0.0)) * res).b);
        tc += half3(half((h2(uv * float2(700.0,40.0) + t) - 0.5) * 0.25 * unl)) * ts.a;
        col = half4(mix(tc, col.rgb, half(lock)), mix(ts.a, col.a, half(lock)));
        col *= half(smoothstep(0.0, 0.12, appear));
    }
    col.rgb = min(col.rgb, half3(col.a));
    return col;
}
"""

W, H = 928, 628


def draw_content(canvas):
    canvas.clear(skia.ColorSetARGB(235, 20, 10, 40))
    p = skia.Paint(AntiAlias=True, Color=skia.ColorSetARGB(255, 255, 42, 109))
    p.setStyle(skia.Paint.kStroke_Style)
    p.setStrokeWidth(2)
    canvas.drawRect(skia.Rect.MakeXYWH(1, 1, W - 2, H - 2), p)
    bar = skia.Paint(AntiAlias=True, Color=skia.ColorSetARGB(255, 5, 217, 232))
    for i in range(12):
        canvas.drawRect(skia.Rect.MakeXYWH(40, 40 + i * 44, 700, 26), bar)


def main():
    bench = "--bench" in sys.argv
    conn = xcffib.connect()
    win = GLWindow(conn, W, H)
    print(f"EGL {win.egl_version[0]}.{win.egl_version[1]} "
          f"vendor {win.egl_vendor!r} via EGL_EXT_platform_x11, "
          f"window {win.wid:#x}")
    vendor, renderer, version = win.renderer()
    print(f"GL vendor   : {vendor}")
    print(f"GL renderer : {renderer}")
    print(f"GL version  : {version}")

    effect = skia.RuntimeEffect.MakeForShader(SCAN_LOCK)
    if effect is None:
        raise RuntimeError("SkSL failed to compile")

    canvas = win.skia_surface.getCanvas()
    # Content is drawn once into an offscreen GPU surface, exactly as the
    # shell freezes it during a transition.
    content = skia.Surfaces.MakeRenderTarget(
        win.context, skia.Budgeted.kYes,
        skia.ImageInfo.MakeN32Premul(W, H))
    draw_content(content.getCanvas())
    content.flushAndSubmit()
    frozen = content.makeImageSnapshot()

    frames, t0 = 0, time.monotonic()
    duration = 0.22
    times = []
    while True:
        elapsed = time.monotonic() - t0
        appear = min(1.0, elapsed / duration) if not bench else \
            (elapsed % duration) / duration
        f0 = time.perf_counter()
        b = skia.RuntimeEffectBuilder(effect)
        b.setChild("src", frozen.makeShader(
            skia.TileMode.kDecal, skia.TileMode.kDecal,
            skia.SamplingOptions()))
        b.setUniform("res", [float(W), float(H)])
        b.setUniform("t", float(elapsed))
        b.setUniform("appear", float(appear))
        paint = skia.Paint(Shader=b.makeShader())
        paint.setBlendMode(skia.BlendMode.kSrc)
        canvas.drawPaint(paint)
        win.flush()
        times.append(time.perf_counter() - f0)
        frames += 1
        if elapsed > (3.0 if bench else max(duration + 0.8, 1.5)):
            break

    span = time.monotonic() - t0
    times.sort()
    print(f"\n{frames} frames in {span:.2f}s = {frames/span:.0f} fps")
    print(f"  median frame {times[len(times)//2]*1000:6.2f} ms")
    print(f"  p95 frame    {times[int(len(times)*0.95)]*1000:6.2f} ms")
    print(f"  budget at 60fps is 16.67 ms")
    win.destroy()
    conn.disconnect()


if __name__ == "__main__":
    main()
