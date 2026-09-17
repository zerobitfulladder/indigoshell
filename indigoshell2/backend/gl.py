"""EGL/OpenGL surfaces for Skia, so shaders run on the GPU.

The CPU raster path costs two things per frame: the SkSL pass is
per-pixel work on the CPU, and the result is read back and pushed over
the X socket with PutImage — 2.3MB and 40 requests for the settings
panel. Rendering into a GL framebuffer owned by the window removes both;
`eglSwapBuffers` replaces the upload entirely.

Measured on this machine with `prototypes/skia/gpu_egl.py`, same shader
and window size:

    CPU raster, effect_scale 0.35    11.9  ms/frame
    GPU, full resolution              0.07 ms/frame

**The platform choice is not cosmetic.** `EGL_EXT_platform_xcb` is
advertised here and would take xcffib's `xcb_connection_t *` directly,
with no second connection and no Xlib at all. NVIDIA's EGL does not
implement it, so GLVND falls through to Mesa, which has no DRI driver
for this card and returns **llvmpipe** — a software rasteriser that
initialises cleanly, renders correctly, and is roughly 40x slower than
the GPU. Nothing in the EGL API reports this; only `GL_RENDERER` does.
So this uses `EGL_EXT_platform_x11` with its own Xlib connection, and
`Backend.renderer` records what we actually got.

Everything degrades: if any step fails, `create()` returns None and the
window falls back to the raster path.
"""

import ctypes
import logging
import os

import skia

log = logging.getLogger(__name__)

# ── EGL/GL constants ────────────────────────────────────────────────────
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
EGL_RENDER_BUFFER, EGL_BACK_BUFFER = 0x3086, 0x3084
EGL_WIDTH, EGL_HEIGHT = 0x3057, 0x3056
EGL_PBUFFER_BIT = 0x0001
GL_RGBA8 = 0x8058
GL_VENDOR, GL_RENDERER = 0x1F00, 0x1F01

_STENCIL_BITS = 8


def _int_array(values):
    return (ctypes.c_int * len(values))(*values)


class _Lib:
    """Lazily loaded C entry points, so importing this module on a box
    with no GL doesn't explode at import time."""

    def __init__(self) -> None:
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        e = ctypes.CDLL("libEGL.so.1")
        e.eglGetProcAddress.restype = ctypes.c_void_p
        e.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        e.eglQueryString.restype = ctypes.c_char_p
        e.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
        e.eglInitialize.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int)]
        e.eglChooseConfig.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int)]
        e.eglGetConfigAttrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int)]
        e.eglCreateContext.restype = ctypes.c_void_p
        e.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int)]
        e.eglCreateWindowSurface.restype = ctypes.c_void_p
        e.eglCreateWindowSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_int)]
        e.eglCreatePbufferSurface.restype = ctypes.c_void_p
        e.eglCreatePbufferSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_int)]
        e.eglDestroySurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
        e.eglSwapBuffers.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.eglSwapInterval.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.egl = e

        g = ctypes.CDLL("libGL.so.1")
        g.glGetString.restype = ctypes.c_char_p
        g.glGetString.argtypes = [ctypes.c_uint]
        self.gl = g


class GLSurface:
    """One window's EGL surface plus the Skia surface wrapping FBO 0."""

    def __init__(self, backend: "Backend", wid: int, egl_surface,
                 width: int, height: int) -> None:
        self.backend = backend
        self.wid = wid
        self.egl_surface = egl_surface
        self.width, self.height = width, height
        self.skia_surface = None
        self._wrap()

    def _wrap(self) -> None:
        self.backend.make_current(self)
        # Per *surface*, not per display — EGL stores the swap interval on
        # the bound draw surface, so setting it once at backend setup only
        # ever covered the first window. Every window after that stayed
        # vsync-blocked, and since eglSwapBuffers blocks inside the event
        # loop, every one of their frames cost exactly 16.67ms.
        self.backend.lib.egl.eglSwapInterval(self.backend.dpy, 0)
        fb = skia.GrGLFramebufferInfo(0, GL_RGBA8)
        target = skia.GrBackendRenderTargets.MakeGL(
            self.width, self.height, 0, _STENCIL_BITS, fb)
        # `Surfaces.WrapBackendRenderTarget` is bound too but its overload
        # rejects a valid GrDirectContext in this wheel; this one takes
        # the same arguments. colorSpace must be None for the same reason.
        self.skia_surface = skia.Surfaces.MakeFromBackendRenderTarget(
            self.backend.context, target,
            skia.kBottomLeft_GrSurfaceOrigin, skia.kRGBA_8888_ColorType, None)
        if self.skia_surface is None:
            raise RuntimeError("MakeFromBackendRenderTarget returned None")

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.skia_surface = None
        self._wrap()

    def swap(self) -> None:
        self.backend.context.flushAndSubmit()
        self.backend.lib.egl.eglSwapBuffers(self.backend.dpy, self.egl_surface)

    def destroy(self) -> None:
        b = self.backend
        self.skia_surface = None
        if not self.egl_surface:
            return
        if b._current == self.egl_surface:
            # EGL only *defers* destroying a surface that is still
            # current, so it has to be unbound first or one window
            # surface leaks per open.
            #
            # Unbind onto the idle pbuffer rather than to EGL_NO_SURFACE:
            # releasing the context entirely leaves Skia's GrDirectContext
            # without a live GL context, and every window opened after
            # that rendered at ~47ms/frame instead of 0.3ms. Keeping a
            # 1x1 pbuffer current means the context is never released.
            if b.context is not None:
                b.context.flushAndSubmit()
            b.make_idle()
        b.lib.egl.eglDestroySurface(b.dpy, self.egl_surface)
        self.egl_surface = None


class Backend:
    """One EGL context and one Skia GPU context, shared by every window."""

    def __init__(self, depth32_visuals) -> None:
        # Which X visuals are depth 32. The EGL config we pick must map
        # to one of them: the window is created with EGL's visual at
        # depth 32, and a config whose native visual is depth 24 makes
        # that a BadMatch — which kills the X connection, not just the
        # window.
        self.depth32 = set(depth32_visuals)
        self.lib = _Lib()
        self.xdpy = None
        self.dpy = None
        self.ctx = None
        self.config = None
        self.visual_id = None
        self.context = None          # skia.GrDirectContext
        self.renderer = "?"
        self.vendor = "?"
        self._current = None
        self._idle = None            # 1x1 pbuffer; see make_idle()
        self._setup()

    # ── setup ───────────────────────────────────────────────────────────
    def _setup(self) -> None:
        egl = self.lib.egl
        exts = (egl.eglQueryString(None, EGL_EXTENSIONS) or b"").decode()
        if "EGL_EXT_platform_x11" not in exts:
            raise RuntimeError("EGL_EXT_platform_x11 unavailable")

        addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if not addr:
            raise RuntimeError("eglGetPlatformDisplayEXT unavailable")
        proto = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))

        self.xdpy = self.lib.x11.XOpenDisplay(None)
        if not self.xdpy:
            raise RuntimeError("XOpenDisplay failed")
        self.dpy = proto(addr)(EGL_PLATFORM_X11_EXT, ctypes.c_void_p(self.xdpy),
                               _int_array([EGL_NONE]))
        if not self.dpy:
            raise RuntimeError("eglGetPlatformDisplay returned nothing")
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(self.dpy, major, minor):
            raise RuntimeError("eglInitialize failed")
        self.version = (major.value, minor.value)
        self.egl_vendor = (egl.eglQueryString(self.dpy, EGL_VENDOR)
                           or b"?").decode()

        self._choose_config()
        if not egl.eglBindAPI(EGL_OPENGL_API):
            raise RuntimeError("eglBindAPI(OPENGL) failed")
        self.ctx = egl.eglCreateContext(
            self.dpy, self.config, None,
            _int_array([EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE]))
        if not self.ctx:
            raise RuntimeError("eglCreateContext failed")
        # Somewhere to park the context when no window owns it.
        self._idle = egl.eglCreatePbufferSurface(
            self.dpy, self.config,
            _int_array([EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE])) or None

    def _choose_config(self) -> None:
        """An alpha-capable config whose native visual is depth 32.

        The window has to be created with exactly the visual EGL picked —
        an ARGB window on a mismatched visual is a BadMatch at surface
        creation, or renders as garbage.
        """
        egl = self.lib.egl
        want = _int_array([
            # Pbuffer as well as window: the idle surface the context is
            # parked on between windows comes from this same config.
            EGL_SURFACE_TYPE, EGL_WINDOW_BIT | EGL_PBUFFER_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
            EGL_ALPHA_SIZE, 8, EGL_DEPTH_SIZE, 0,
            EGL_STENCIL_SIZE, _STENCIL_BITS, EGL_NONE,
        ])
        configs = (ctypes.c_void_p * 64)()
        n = ctypes.c_int()
        if not egl.eglChooseConfig(self.dpy, want, configs, 64, n) or not n.value:
            raise RuntimeError("no EGL config with alpha and stencil")
        # Skia needs the stencil buffer for clipping; alpha for the
        # compositor; and the native visual must be one of the server's
        # depth-32 ones.
        seen = []
        for i in range(n.value):
            vid = ctypes.c_int()
            if not egl.eglGetConfigAttrib(self.dpy, configs[i],
                                          EGL_NATIVE_VISUAL_ID, vid):
                continue
            seen.append(vid.value)
            if vid.value in self.depth32:
                self.config, self.visual_id = configs[i], vid.value
                return
        raise RuntimeError(
            f"no EGL config maps to a depth-32 visual "
            f"(configs offered {sorted(set(seen))[:8]}, "
            f"server has {sorted(self.depth32)[:8]})")

    def start(self) -> None:
        """Bring up the Skia GPU context. Needs a current context, so it
        happens after the first surface exists — see `create`."""

    # ── surfaces ────────────────────────────────────────────────────────
    def create(self, wid: int, width: int, height: int) -> GLSurface | None:
        egl = self.lib.egl
        surface = egl.eglCreateWindowSurface(
            self.dpy, self.config, ctypes.c_ulong(wid), None)
        if not surface:
            log.warning("eglCreateWindowSurface failed for 0x%x", wid)
            return None
        if self.context is None:
            # First surface: make it current, then build the Skia context
            # and record what we actually got — llvmpipe would mean the
            # platform fell through to Mesa and the whole point is lost.
            egl.eglMakeCurrent(self.dpy, surface, surface, self.ctx)
            self._current = surface
            # Swap interval is set per surface in GLSurface._wrap; we
            # already pace frames on a deadline clock and picom
            # composites at its own cadence, so a blocking swap would
            # only move the wait into the event loop.
            self.vendor = (self.lib.gl.glGetString(GL_VENDOR) or b"?").decode()
            self.renderer = (self.lib.gl.glGetString(GL_RENDERER)
                             or b"?").decode()
            interface = skia.GrGLInterface.MakeEGL()
            if interface is None:
                raise RuntimeError("GrGLInterface.MakeEGL() returned None")
            self.context = skia.GrDirectContexts.MakeGL(interface)
            if self.context is None:
                raise RuntimeError("GrDirectContexts.MakeGL() returned None")
            log.info("GPU: %s / %s (EGL %d.%d %s)", self.vendor, self.renderer,
                     *self.version, self.egl_vendor)
            if "llvmpipe" in self.renderer or "softpipe" in self.renderer:
                log.warning("GL renderer is a software rasteriser (%s) — "
                            "the GPU path is not actually on the GPU",
                            self.renderer)
        return GLSurface(self, wid, surface, width, height)

    def make_idle(self) -> None:
        """Park the context on the 1x1 pbuffer so it is never released."""
        if self._idle is None:
            self.lib.egl.eglMakeCurrent(self.dpy, None, None, None)
            self._current = None
            return
        self.lib.egl.eglMakeCurrent(self.dpy, self._idle, self._idle, self.ctx)
        self._current = self._idle
        if self.context is not None:
            self.context.resetContext()

    def make_current(self, surface: GLSurface) -> None:
        # `==`, not `is`: these are ctypes pointers surfacing as Python
        # ints, and equal ints are not always the same object.
        if self._current == surface.egl_surface:
            return
        self.lib.egl.eglMakeCurrent(self.dpy, surface.egl_surface,
                                    surface.egl_surface, self.ctx)
        self._current = surface.egl_surface
        # Skia caches GL state; after a context switch it must re-read it
        # or it will issue draws against the wrong bindings.
        if self.context is not None:
            self.context.resetContext()

    def offscreen(self, width: int, height: int):
        """A GPU-backed offscreen surface — the frozen content frame."""
        return skia.Surfaces.MakeRenderTarget(
            self.context, skia.Budgeted.kYes,
            skia.ImageInfo.MakeN32Premul(width, height))


_backend: Backend | None = None
_tried = False


def backend(display=None) -> Backend | None:
    """The process-wide GL backend, or None if it could not be set up.

    `display` is only needed on the first call, to learn which visuals
    are depth 32. Set INDIGOSHELL2_GPU=0 to force the raster path.
    """
    global _backend, _tried
    if _tried:
        return _backend
    if display is None:
        return None          # not initialised yet and nothing to do it with
    _tried = True
    from ..core.naming import env
    if os.environ.get(env("GPU"), "1") in ("0", "no", "off"):
        log.info("GPU disabled by %s", env("GPU"))
        return None
    try:
        visuals = {v.visual_id for d in display.screen.allowed_depths
                   if d.depth == 32 for v in d.visuals}
        _backend = Backend(visuals)
    except Exception as exc:
        log.warning("GPU unavailable (%s); falling back to CPU raster", exc)
        _backend = None
    return _backend
