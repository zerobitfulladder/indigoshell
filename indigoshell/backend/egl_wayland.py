"""EGL on the Wayland platform, for Skia's GPU backend: the canvas's GPU
target (backend/wayland.py).

backend/gl.py moved to Wayland: the EGL display is made from the
client's own `wl_display *` (pywayland's), and the native window is a
`wl_egl_window` from libwayland-egl wrapped around the canvas's
`wl_surface *`. Skia draws into FBO 0 of that window surface, as on X11.
There is one context, current for the life of the process: on the
canvas's window surface while there is one (`attach`), on a 1x1 surface
of its own otherwise (`start`, `detach`). The regions' offscreen
surfaces (`offscreen`) live in that same GrDirectContext, so they
survive the canvas going away and coming back.

The traps carried over from X11:

- A silent software fallback. EGL initialises fine on llvmpipe and says
  nothing; only GL_RENDERER tells. It is read, logged, and `software`
  says so.
- The swap interval is per *surface*. On Wayland interval 1 makes
  eglSwapBuffers wait for the previous frame's wl_surface.frame
  callback, inside the event loop, and a compositor that is not
  repainting (a nested niri on a hidden workspace, an output that is
  off) sends none, so the swap blocks for as long as that lasts. It is
  set to 0 on the surface right after it is made current.

Partial redraw uses EGL_EXT_buffer_age (how stale the back buffer is)
and EGL_KHR_swap_buffers_with_damage (what changed, so the compositor
re-composites only that), when the driver offers them.

Any failure raises; the canvas then draws on the CPU into wl_shm.
"""

import ctypes
import logging

import skia

log = logging.getLogger(__name__)

EGL_PLATFORM_WAYLAND_KHR = 0x31D8
EGL_NONE = 0x3038
EGL_SURFACE_TYPE, EGL_WINDOW_BIT = 0x3033, 0x0004
EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT = 0x3040, 0x0008
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = (
    0x3024, 0x3023, 0x3022, 0x3021)
EGL_DEPTH_SIZE, EGL_STENCIL_SIZE = 0x3025, 0x3026
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_VENDOR, EGL_VERSION, EGL_EXTENSIONS = 0x3053, 0x3054, 0x3055
EGL_BUFFER_AGE_EXT = 0x313D
GL_RGBA8 = 0x8058
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02
STENCIL = 8


def _ints(values):
    return (ctypes.c_int * len(values))(*values)


class _Lib:
    def __init__(self) -> None:
        e = ctypes.CDLL("libEGL.so.1")
        e.eglGetProcAddress.restype = ctypes.c_void_p
        e.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        e.eglQueryString.restype = ctypes.c_char_p
        e.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
        e.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int)]
        e.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int)]
        e.eglGetConfigAttrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        e.eglCreateContext.restype = ctypes.c_void_p
        e.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        e.eglCreateWindowSurface.restype = ctypes.c_void_p
        e.eglCreateWindowSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        e.eglDestroySurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
        e.eglSwapBuffers.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.eglSwapInterval.argtypes = [ctypes.c_void_p, ctypes.c_int]
        e.eglQuerySurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int)]
        e.eglGetError.restype = ctypes.c_int
        e.eglTerminate.argtypes = [ctypes.c_void_p]
        e.eglDestroyContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.egl = e

        w = ctypes.CDLL("libwayland-egl.so.1")
        w.wl_egl_window_create.restype = ctypes.c_void_p
        w.wl_egl_window_create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        w.wl_egl_window_resize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int]
        w.wl_egl_window_destroy.argtypes = [ctypes.c_void_p]
        self.wlegl = w

        g = ctypes.CDLL("libGL.so.1")
        g.glGetString.restype = ctypes.c_char_p
        g.glGetString.argtypes = [ctypes.c_uint]
        g.glFinish.argtypes = []
        self.gl = g


def flip_rects(rects, height: int) -> list[int]:
    """Top-left-origin rects as the flat x, y, w, h list EGL's damage
    takes, bottom-left origin."""
    flat: list[int] = []
    for r in rects:
        flat += [r.left(), height - r.bottom(), r.width(), r.height()]
    return flat


class WaylandGL:
    """One EGL display + context + window surface for one wl_surface."""

    def __init__(self, wl_display_ptr: int) -> None:
        self.lib = lib = _Lib()
        egl = lib.egl
        client_exts = (egl.eglQueryString(None, EGL_EXTENSIONS) or b"").decode()
        if "EGL_KHR_platform_wayland" not in client_exts \
                and "EGL_EXT_platform_wayland" not in client_exts:
            raise RuntimeError("no EGL_*_platform_wayland")
        addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if not addr:
            raise RuntimeError("eglGetPlatformDisplayEXT unavailable")
        get_display = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int))(addr)
        self.dpy = get_display(EGL_PLATFORM_WAYLAND_KHR, ctypes.c_void_p(wl_display_ptr),
                               _ints([EGL_NONE]))
        if not self.dpy:
            raise RuntimeError("eglGetPlatformDisplayEXT(WAYLAND) returned nothing")
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(self.dpy, major, minor):
            raise RuntimeError(f"eglInitialize failed (0x{egl.eglGetError():x})")
        self.egl_version = f"{major.value}.{minor.value}"
        self.egl_vendor = (egl.eglQueryString(self.dpy, EGL_VENDOR) or b"?").decode()
        self.exts = (egl.eglQueryString(self.dpy, EGL_EXTENSIONS) or b"").decode().split()

        want = _ints([EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
                      EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
                      EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
                      EGL_ALPHA_SIZE, 8, EGL_DEPTH_SIZE, 0,
                      EGL_STENCIL_SIZE, STENCIL, EGL_NONE])
        configs = (ctypes.c_void_p * 64)()
        n = ctypes.c_int()
        if not egl.eglChooseConfig(self.dpy, want, configs, 64, n) or not n.value:
            raise RuntimeError("no EGL config with alpha + stencil")
        # First config with exactly 8 bits of alpha: anything else and
        # the compositor gets an opaque or 10-bit buffer.
        self.config = configs[0]
        for i in range(n.value):
            a = ctypes.c_int()
            egl.eglGetConfigAttrib(self.dpy, configs[i], EGL_ALPHA_SIZE, a)
            if a.value == 8:
                self.config = configs[i]
                break
        if not egl.eglBindAPI(EGL_OPENGL_API):
            raise RuntimeError("eglBindAPI(OPENGL) failed")
        self.ctx = egl.eglCreateContext(self.dpy, self.config, None,
                                        _ints([EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE]))
        if not self.ctx:
            raise RuntimeError("eglCreateContext failed")

        swap = None
        self.swap_damage_name = None
        for name, ext in ((b"eglSwapBuffersWithDamageKHR", "EGL_KHR_swap_buffers_with_damage"),
                          (b"eglSwapBuffersWithDamageEXT", "EGL_EXT_swap_buffers_with_damage")):
            fn = egl.eglGetProcAddress(name)
            if fn and ext in self.exts:
                swap = ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int), ctypes.c_int)(fn)
                self.swap_damage_name = ext
                break
        self._swap_damage = swap
        self.has_age = "EGL_EXT_buffer_age" in self.exts
        self.egl_window = None
        self.surface = None
        self._idle_window = None
        self._idle = None
        self.context = None
        self.skia_surface = None
        self.width = self.height = 0
        self.renderer = self.vendor = self.gl_version = "?"

    def start(self, idle_surface_ptr: int) -> None:
        """Make the context current on a 1x1 window surface around
        `idle_surface_ptr` (a wl_surface with no role, never shown) and
        bring up Skia's GPU context on it.

        The canvas's own surface comes and goes (a fullscreen window
        takes it away, an output going away closes it), but the regions'
        pixels are textures in this context, which must outlive it and
        stay current meanwhile: releasing it leaves Skia without a live
        context (gl.py parks on a pbuffer for the same reason). A
        role-less wl_surface's EGL window surface works on any driver,
        where pbuffers and surfaceless contexts are optional."""
        lib = self.lib
        self._idle_window = lib.wlegl.wl_egl_window_create(
            ctypes.c_void_p(idle_surface_ptr), 1, 1)
        if not self._idle_window:
            raise RuntimeError("wl_egl_window_create failed (idle)")
        self._idle = lib.egl.eglCreateWindowSurface(
            self.dpy, self.config, ctypes.c_void_p(self._idle_window), None)
        if not self._idle:
            raise RuntimeError(f"eglCreateWindowSurface failed (0x{lib.egl.eglGetError():x})")
        if not lib.egl.eglMakeCurrent(self.dpy, self._idle, self._idle, self.ctx):
            raise RuntimeError("eglMakeCurrent failed")
        self.vendor = (lib.gl.glGetString(GL_VENDOR) or b"?").decode()
        self.renderer = (lib.gl.glGetString(GL_RENDERER) or b"?").decode()
        self.gl_version = (lib.gl.glGetString(GL_VERSION) or b"?").decode()
        interface = skia.GrGLInterface.MakeEGL()
        if interface is None:
            raise RuntimeError("GrGLInterface.MakeEGL() returned None")
        self.context = skia.GrDirectContexts.MakeGL(interface)
        if self.context is None:
            raise RuntimeError("GrDirectContexts.MakeGL() returned None")
        log.info("GPU: %s / %s (EGL %s %s; buffer age %s, damage %s)",
                 self.vendor, self.renderer, self.egl_version, self.egl_vendor,
                 "yes" if self.has_age else "no", self.swap_damage_name or "no")
        if self.software:
            log.warning("GL renderer is a software rasteriser (%s): the canvas "
                        "is not actually drawn on the GPU", self.renderer)

    def attach(self, wl_surface_ptr: int, width: int, height: int) -> None:
        """Draw into `wl_surface_ptr` from now on, `width` x `height`."""
        lib = self.lib
        self.detach()
        self.egl_window = lib.wlegl.wl_egl_window_create(
            ctypes.c_void_p(wl_surface_ptr), width, height)
        if not self.egl_window:
            raise RuntimeError("wl_egl_window_create failed")
        self.surface = lib.egl.eglCreateWindowSurface(
            self.dpy, self.config, ctypes.c_void_p(self.egl_window), None)
        if not self.surface:
            raise RuntimeError(f"eglCreateWindowSurface failed (0x{lib.egl.eglGetError():x})")
        if not lib.egl.eglMakeCurrent(self.dpy, self.surface, self.surface, self.ctx):
            raise RuntimeError("eglMakeCurrent failed")
        # Per surface: see the module docstring.
        lib.egl.eglSwapInterval(self.dpy, 0)
        # Skia caches GL state; after a switch it must read it again.
        self.context.resetContext()
        self.width, self.height = width, height
        self._wrap()

    def detach(self) -> None:
        """Stop drawing into the window surface (it is going away): the
        context goes back to the idle surface, with everything in it."""
        lib = self.lib
        self.skia_surface = None
        if self.surface:
            if self.context is not None:
                self.context.flushAndSubmit()
            lib.egl.eglMakeCurrent(self.dpy, self._idle, self._idle, self.ctx)
            if self.context is not None:
                self.context.resetContext()
            lib.egl.eglDestroySurface(self.dpy, self.surface)
            self.surface = None
        if self.egl_window:
            lib.wlegl.wl_egl_window_destroy(self.egl_window)
            self.egl_window = None

    @property
    def software(self) -> bool:
        return any(s in self.renderer.lower() for s in ("llvmpipe", "softpipe", "swrast"))

    def _wrap(self) -> None:
        fb = skia.GrGLFramebufferInfo(0, GL_RGBA8)
        target = skia.GrBackendRenderTargets.MakeGL(self.width, self.height, 0, STENCIL, fb)
        self.skia_surface = skia.Surfaces.MakeFromBackendRenderTarget(
            self.context, target, skia.kBottomLeft_GrSurfaceOrigin,
            skia.kRGBA_8888_ColorType, None)
        if self.skia_surface is None:
            raise RuntimeError("MakeFromBackendRenderTarget returned None")

    def resize(self, width: int, height: int) -> None:
        self.lib.wlegl.wl_egl_window_resize(self.egl_window, width, height, 0, 0)
        self.width, self.height = width, height
        self.skia_surface = None
        self._wrap()

    def buffer_age(self) -> int:
        """0 = unknown contents: repaint everything."""
        if not self.has_age:
            return 0
        age = ctypes.c_int(0)
        self.lib.egl.eglQuerySurface(self.dpy, self.surface, EGL_BUFFER_AGE_EXT, age)
        return age.value

    def swap(self, damage) -> None:
        """Flush Skia and present: attaches, damages and commits the
        wl_surface. `damage` in surface coordinates, top-left origin."""
        self.context.flushAndSubmit()
        if damage and self._swap_damage is not None:
            self._swap_damage(self.dpy, self.surface,
                              _ints(flip_rects(damage, self.height)), len(damage))
        else:
            self.lib.egl.eglSwapBuffers(self.dpy, self.surface)

    def offscreen(self, width: int, height: int) -> skia.Surface:
        """A GPU surface in the same context, for a region's pixels."""
        surface = skia.Surfaces.MakeRenderTarget(
            self.context, skia.Budgeted.kYes,
            skia.ImageInfo.MakeN32Premul(max(1, width), max(1, height)))
        if surface is None:
            raise RuntimeError(f"no GPU surface of {width}x{height}")
        return surface

    def finish(self) -> None:
        """Wait for the GPU, for honest timings."""
        self.context.flushAndSubmit(skia.GrSyncCpu.kYes)
        self.lib.gl.glFinish()

    def destroy(self) -> None:
        egl = self.lib.egl
        self.detach()
        if self.context is not None:
            self.context.abandonContext()
            self.context = None
        egl.eglMakeCurrent(self.dpy, None, None, None)
        if self._idle:
            egl.eglDestroySurface(self.dpy, self._idle)
            self._idle = None
        if self._idle_window:
            self.lib.wlegl.wl_egl_window_destroy(self._idle_window)
            self._idle_window = None
        if self.ctx:
            egl.eglDestroyContext(self.dpy, self.ctx)
            self.ctx = None
        egl.eglTerminate(self.dpy)
