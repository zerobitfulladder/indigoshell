"""A throwaway test window: is QtWebEngine good enough to build a browser on?

    gauntlet.py [url]                  browse by hand
    gauntlet.py --probe [--out FILE]   report GPU/decoder state, then quit
    gauntlet.py --watch SECONDS url    play a video muted, report CPU + GPU decoder load, then quit
    options: --ua chrome|qt (default chrome), --google-firefox, --flags "<chromium flags>"

By hand it answers: can you sign in to Google, claude.ai, the bank?
--probe answers: which GPU path did Chromium take, and can it decode
1440p in hardware? --watch answers: what does 1440p YouTube cost?

Every window shares one persistent profile: storage in
$XDG_DATA_HOME/<app>/browser, cache in $XDG_CACHE_HOME/<app>/browser.

With a strip listening (strip.py, standing in for the shell) each window
hands it the band and blanks its own label; with none, the label shows
the page as before. See link.py. --strip-test is the browser's half of
`strip.py --selftest`.

--ua chrome       Qt's own User-Agent with the QtWebEngine/x.y.z token removed,
                  and client hints that name Google Chrome at this Chromium version
--ua qt           leave both as Qt sets them
--google-firefox  send a Firefox User-Agent to accounts.google.com only
                  (qutebrowser's workaround for "this browser may not be secure")
--flags           appended to QTWEBENGINE_CHROMIUM_FLAGS

Keys: Ctrl+L address or search, Alt+Left / Alt+Right back / forward,
Ctrl+R reload, F12 inspector, Ctrl+W close the window.

Run it with the project's venv python; it puts the repo on sys.path itself.
"""

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psutil
import shiboken6
from PySide6.QtCore import (QBuffer, QCoreApplication, QEvent, QIODevice, QObject, QPoint,
                            QSocketNotifier, Qt, QTimer, QUrl)
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import (QWebEngineNewWindowRequest, QWebEnginePage,
                                     QWebEngineProfile, QWebEngineProfileBuilder,
                                     QWebEngineSettings,
                                     QWebEngineUrlRequestInterceptor,
                                     qWebEngineChromiumVersion, qWebEngineVersion)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QInputDialog, QLabel, QMessageBox,
                               QSizePolicy, QVBoxLayout, QWidget)

from indigoshell.core.naming import APP
from link import StripLink

NAME = f"{APP}-browser"
BACKGROUND = "#050310"
TEXT = "#d1004f"
BAND = 28                      # the strip the shell will own later
HOME = "https://www.google.com"
ISOLATED = 1                   # QWebEngineScript.ApplicationWorld

FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:160.0) Gecko/20100101 Firefox/160.0"
UA_BY_HOST = {
    "accounts.google.com": FIREFOX_UA,
}

WINDOWS = []                   # every Window opened; closed ones go invalid


def log(*parts):
    print("gauntlet:", *parts, file=sys.stderr, flush=True)


def xdg_dir(var, fallback):
    base = os.environ.get(var) or str(Path.home() / fallback)
    return str(Path(base) / APP / "browser")


def to_url(text):
    """An address if it looks like one, otherwise a Google search."""
    text = text.strip()
    looks_like_address = " " not in text and (
        "." in text or "://" in text or text.startswith(("localhost", "about:", "chrome:")))
    if looks_like_address:
        return QUrl.fromUserInput(text)
    return QUrl("https://www.google.com/search?q=" + quote_plus(text))


# --- profile ----------------------------------------------------------------

class UserAgentByHost(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        ua = UA_BY_HOST.get(info.requestUrl().host())
        if ua:
            info.setHttpHeader(b"User-Agent", ua.encode())


def on_download(request):
    request.accept()
    log("download:", Path(request.downloadDirectory()) / request.downloadFileName())


def make_profile(args):
    builder = QWebEngineProfileBuilder()
    builder.setPersistentStoragePath(xdg_dir("XDG_DATA_HOME", ".local/share"))
    builder.setCachePath(xdg_dir("XDG_CACHE_HOME", ".cache"))
    builder.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile = builder.createProfile("browser")

    if args.ua == "chrome":
        profile.setHttpUserAgent(re.sub(r"QtWebEngine/\S+\s*", "", profile.httpUserAgent()))
        chromium = qWebEngineChromiumVersion()
        profile.clientHints().setFullVersionList({
            "Chromium": chromium,
            "Google Chrome": chromium,
            "Not=A?Brand": "24.0.0.0",
        })
    if args.google_firefox:
        # Parented to the profile, so it lives exactly as long as it does.
        profile.setUrlRequestInterceptor(UserAgentByHost(profile))

    settings = profile.settings()
    A = QWebEngineSettings.WebAttribute
    for attribute in (A.FullScreenSupportEnabled, A.PdfViewerEnabled, A.PluginsEnabled,
                      A.JavascriptCanOpenWindows, A.ScrollAnimatorEnabled):
        settings.setAttribute(attribute, True)

    profile.downloadRequested.connect(on_download)
    return profile


# --- window -----------------------------------------------------------------

class Window(QWidget):
    ask_permissions = True     # the automated modes deny instead of asking
    band_background = BACKGROUND

    def __init__(self, profile, dialog=False):
        super().__init__(None, Qt.WindowType.Dialog if dialog else Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.profile = profile
        self.progress = 0
        self.loading = False
        self.inspector = None

        self.band = QLabel()
        self.band.setFixedHeight(BAND)
        self.band.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.band.setStyleSheet(
            f"background: {self.band_background}; color: {TEXT}; padding: 0 8px;")

        # The shell's strip draws over the band when one is listening.
        self.link = StripLink(self.winId, BAND, self)
        self.link.attached_changed.connect(self.update_band)
        self.link.command.connect(self.on_strip_command)

        self.view = QWebEngineView()
        self.page = QWebEnginePage(profile, self.view)
        self.view.setPage(self.page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.band)
        layout.addWidget(self.view)

        page = self.page
        page.titleChanged.connect(self.update_band)
        page.urlChanged.connect(self.update_band)
        page.loadProgress.connect(self.on_progress)
        page.loadStarted.connect(lambda: self.on_loading(True))
        page.loadFinished.connect(lambda _ok: self.on_loading(False))
        page.newWindowRequested.connect(self.on_new_window)
        page.fullScreenRequested.connect(self.on_fullscreen)
        page.permissionRequested.connect(self.on_permission)
        page.windowCloseRequested.connect(self.close)

        for keys, slot in (("Ctrl+L", self.ask_address),
                           ("Alt+Left", self.view.back),
                           ("Alt+Right", self.view.forward),
                           ("Ctrl+R", self.view.reload),
                           ("F12", self.open_inspector),
                           ("Ctrl+W", self.close)):
            QShortcut(QKeySequence(keys), self).activated.connect(slot)

        self.resize(1280, 800)
        self.update_band()

    def update_band(self, *_):
        page = self.page
        title = page.title()
        self.setWindowTitle(title or NAME)
        history = page.history()
        self.link.push(url=page.url().toString(), title=title, progress=self.progress,
                       loading=self.loading, back=history.canGoBack(),
                       forward=history.canGoForward())
        # Blank under a strip: it covers the band, and text left here
        # would show through for a frame whenever the window grows.
        self.band.setText("" if self.link.attached
                          else f"{page.url().host()} · {title} · {self.progress}%")

    def on_progress(self, percent):
        self.progress = percent
        self.update_band()

    def on_loading(self, loading):
        self.loading = loading
        self.update_band()

    def on_strip_command(self, cmd):
        action = {"back": self.view.back, "forward": self.view.forward,
                  "reload": self.view.reload}.get(cmd)
        if action:
            action()

    def showEvent(self, event):
        super().showEvent(event)
        self.link.start()

    def ask_address(self):
        text, ok = QInputDialog.getText(self, "Open", "Address or search:",
                                        text=self.page.url().toString())
        if ok and text.strip():
            self.page.load(to_url(text))

    def open_inspector(self):
        if self.inspector is None or not shiboken6.isValid(self.inspector):
            self.inspector = open_window(self.profile)
            self.page.setDevToolsPage(self.inspector.page)
        self.inspector.show()
        self.inspector.raise_()

    def on_new_window(self, request):
        dialog = request.destination() == QWebEngineNewWindowRequest.DestinationType.InNewDialog
        window = open_window(self.profile, dialog=dialog)
        geometry = request.requestedGeometry()
        if dialog and geometry.width() > 0 and geometry.height() > 0:
            window.setGeometry(geometry.adjusted(0, 0, 0, BAND))
        request.openIn(window.page)
        window.show()

    def on_fullscreen(self, request):
        request.accept()
        if request.toggleOn():
            self.link.set_band(0)       # first: the page is about to move up into the band
            self.band.hide()
            self.showFullScreen()
        else:
            self.band.show()
            self.showNormal()
            self.link.set_band(BAND)

    def on_permission(self, permission):
        what = permission.permissionType().name
        origin = permission.origin().toString()
        if not Window.ask_permissions:
            log(f"permission denied (automated run): {origin} {what}")
            permission.deny()
            return
        answer = QMessageBox.question(self, "Permission", f"{origin}\nwants {what}")
        if answer == QMessageBox.StandardButton.Yes:
            permission.grant()
        else:
            permission.deny()

    def closeEvent(self, event):
        if self.inspector is not None and shiboken6.isValid(self.inspector):
            self.inspector.close()
        self.link.close()
        super().closeEvent(event)


def open_window(profile, dialog=False):
    window = Window(profile, dialog)
    WINDOWS.append(window)
    return window


# --- --probe ----------------------------------------------------------------

# The page text, walked through shadow roots (chrome://gpu keeps its content
# inside custom elements). Headings come out as "## title" lines, table
# cells are separated by tabs.
PAGE_TEXT_JS = r"""
(() => {
  const SKIP = new Set(['STYLE', 'SCRIPT', 'TEMPLATE', 'LINK']);
  const BREAK = /^(DIV|P|TR|LI|UL|OL|TABLE|TBODY|THEAD|SECTION|PRE|BR|DL|DT|DD|SUMMARY|DETAILS)$/;
  let out = '';
  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) { out += node.textContent.replace(/\s+/g, ' '); return; }
    const tag = node.nodeType === Node.ELEMENT_NODE ? node.tagName : '';
    if (SKIP.has(tag)) return;
    const heading = /^H[1-6]$/.test(tag);
    if (heading) out += '\n## ';
    else if (BREAK.test(tag)) out += '\n';
    if (node.shadowRoot) walk(node.shadowRoot);
    for (const child of node.childNodes) walk(child);
    if (tag === 'TD' || tag === 'TH') out += '\t';
    if (heading) out += '\n';
  }
  walk(document);
  return out.split('\n')
    .map(line => line.replace(/ +/g, ' ').replace(/ *\t */g, '\t').trim())
    .filter(Boolean).join('\n');
})()
"""

IDENTITY_JS = r"""
JSON.stringify({
  ua: navigator.userAgent,
  brands: navigator.userAgentData ? navigator.userAgentData.brands : null,
  dark: matchMedia('(prefers-color-scheme: dark)').matches,
})
"""

DECODE_TESTS = [
    # label, content type, width, height, fps, bitrate
    ("VP9 2560x1440@60", 'video/webm; codecs="vp09.00.51.08"', 2560, 1440, 60, 20_000_000),
    ("AV1 2560x1440@60", 'video/mp4; codecs="av01.0.12M.08"', 2560, 1440, 60, 15_000_000),
    ("H.264 1920x1080@60", 'video/mp4; codecs="avc1.64002a"', 1920, 1080, 60, 8_000_000),
]

# decodingInfo is a promise and runJavaScript does not await, so the answer
# is left on a global in this world and read back by a second call.
DECODE_START_JS = r"""
window.__gauntlet = null;
try {
  Promise.all(CONFIGS.map(c => navigator.mediaCapabilities.decodingInfo(c).then(
      r => ({supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient}),
      e => ({error: String(e)}))))
    .then(r => { window.__gauntlet = JSON.stringify(r); });
} catch (e) {
  window.__gauntlet = JSON.stringify(CONFIGS.map(() => ({error: String(e)})));
}
"""
DECODE_READ_JS = "window.__gauntlet"

PROBE_SECTIONS = ("Graphics Feature Status", "Problems Detected",
                  "Video Acceleration Information")


def sections(text):
    """{heading: [lines]} for a page text with "## heading" lines."""
    found, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = found.setdefault(line[3:].rstrip("= "), [])
        elif current is not None:
            current.append(line)
    return found


class Probe:
    def __init__(self, app, window, out):
        self.app, self.window, self.out = app, window, out
        self.page = window.page
        self.text = ""
        self.polls = 0
        self.decode_polls = 0
        self.page.loadFinished.connect(self.on_loaded)
        self.page.load(QUrl("chrome://gpu"))

    def on_loaded(self, ok):
        self.page.loadFinished.disconnect(self.on_loaded)
        if not ok:
            log("chrome://gpu failed to load")
        QTimer.singleShot(1000, self.poll_text)

    def poll_text(self):
        self.page.runJavaScript(PAGE_TEXT_JS, ISOLATED, self.on_text)

    def on_text(self, text):
        text = text or ""
        self.polls += 1
        populated = all(f"## {s}" in text for s in ("Graphics Feature Status",
                                                     "Video Acceleration Information"))
        settled = populated and len(text) == len(self.text)
        self.text = text
        if settled or self.polls >= 30:
            if not settled:
                log("chrome://gpu never settled; reporting what it showed")
            self.page.runJavaScript(IDENTITY_JS, ISOLATED, self.on_identity)
        else:
            QTimer.singleShot(500, self.poll_text)

    def on_identity(self, identity):
        self.identity = json.loads(identity) if identity else {}
        configs = [{"type": "media-source",
                    "video": {"contentType": ctype, "width": w, "height": h,
                              "framerate": fps, "bitrate": bitrate}}
                   for _, ctype, w, h, fps, bitrate in DECODE_TESTS]
        self.page.runJavaScript(DECODE_START_JS.replace("CONFIGS", json.dumps(configs)),
                                ISOLATED, lambda _: None)
        QTimer.singleShot(200, self.poll_decode)

    def poll_decode(self):
        self.page.runJavaScript(DECODE_READ_JS, ISOLATED, self.on_decode)

    def on_decode(self, result):
        self.decode_polls += 1
        if not result and self.decode_polls < 50:
            QTimer.singleShot(200, self.poll_decode)
            return
        self.report(json.loads(result) if result else None)
        self.app.quit()

    def report(self, decode):
        if self.out:
            Path(self.out).write_text(self.text + "\n")
            log("full chrome://gpu text written to", self.out)

        identity = self.identity
        brands = identity.get("brands")
        print(f"chromium      {qWebEngineChromiumVersion()}  (QtWebEngine {qWebEngineVersion()})")
        print(f"userAgent     {identity.get('ua')}")
        print("brands        " + (", ".join(f"{b['brand']} {b['version']}" for b in brands)
                                  if brands else "none"))
        print(f"dark scheme   {identity.get('dark')}")

        found = sections(self.text)
        for line in self.text.splitlines():
            if line.startswith(("GL_RENDERER", "GL_VENDOR")):
                print(line.replace("\t", "  ").strip())
        for name in PROBE_SECTIONS:
            print(f"\n{name}")
            for line in found.get(name, ["(not on the page)"]):
                print("  " + line.replace("\t", "  ").strip())

        print("\ndecodingInfo (media-source; powerEfficient = hardware decode)")
        for i, (label, *_rest) in enumerate(DECODE_TESTS):
            r = decode[i] if decode else {"error": "no answer"}
            if "error" in r:
                print(f"  {label:20} error: {r['error']}")
            else:
                print(f"  {label:20} supported={r['supported']}  smooth={r['smooth']}"
                      f"  powerEfficient={r['powerEfficient']}")


# --- --watch ----------------------------------------------------------------

NVIDIA_SMI = ["nvidia-smi", "--query-gpu=utilization.decoder,utilization.gpu",
              "--format=csv,noheader,nounits"]

# Main world: #movie_player's API lives on the page's own objects. Only asks
# for 1440p / play when it is not already so, so repeating it is harmless.
KICK_JS = r"""
(() => {
  const r = {};
  try {
    const p = document.getElementById('movie_player');
    if (p && typeof p.playVideo === 'function') {
      r.player = 'youtube';
      r.ad = p.classList.contains('ad-showing');
      try { r.quality = p.getPlaybackQuality(); } catch (e) {}
      try { r.state = p.getPlayerState(); } catch (e) {}
      if (r.quality !== 'hd1440') {
        try { p.setPlaybackQualityRange('hd1440', 'hd1440'); r.asked1440 = true; }
        catch (e) { r.qualityError = String(e); }
      }
      if (r.state !== 1) {
        try { p.playVideo(); r.askedPlay = true; } catch (e) { r.playError = String(e); }
      }
    } else {
      const v = document.querySelector('video');
      r.player = v ? 'video' : 'none';
      if (v && v.paused) { const pr = v.play(); if (pr) pr.catch(() => {}); }
    }
  } catch (e) { r.error = String(e); }
  return JSON.stringify(r);
})()
"""

FACTS_JS = r"""
(() => {
  const r = {};
  try {
    const v = document.querySelector('video');
    if (v) {
      r.size = v.videoWidth + 'x' + v.videoHeight;
      r.paused = v.paused;
      r.time = v.currentTime;
      const b = v.getBoundingClientRect();
      r.rect = [b.left, b.top, b.width, b.height];
      if (v.getVideoPlaybackQuality) {
        const q = v.getVideoPlaybackQuality();
        r.frames = q.totalVideoFrames;
        r.dropped = q.droppedVideoFrames;
      }
    }
    const p = document.getElementById('movie_player');
    if (p) {
      try { r.quality = p.getPlaybackQuality(); } catch (e) {}
      if (typeof p.getStatsForNerds === 'function') {
        try {
          const s = p.getStatsForNerds();
          r.codecs = s.codecs;
          r.resolution = s.resolution;
          if (r.codecs === undefined && r.resolution === undefined) r.nerdsKeys = Object.keys(s);
        } catch (e) { r.nerdsError = String(e); }
      }
    }
    r.ad = !!document.querySelector('.ad-showing');
  } catch (e) { r.error = String(e); }
  return JSON.stringify(r);
})()
"""


def nvidia_once():
    """(decoder %, gpu %) right now, or None without nvidia-smi."""
    try:
        out = subprocess.run(NVIDIA_SMI, capture_output=True, text=True, timeout=5).stdout
        return parse_nvidia(out.splitlines()[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def parse_nvidia(line):
    decoder, gpu = (float(x) for x in line.split(","))
    return decoder, gpu


class GpuSampler:
    """nvidia-smi in its own once-a-second loop, read on a thread, so the
    GUI thread (Chromium's UI thread) never waits on it."""

    def __init__(self):
        self.rows = []
        self.proc = None

    def start(self):
        try:
            self.proc = subprocess.Popen(NVIDIA_SMI + ["-lms", "1000"], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.proc.stdout:
            try:
                self.rows.append(parse_nvidia(line))
            except ValueError:
                pass

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def pid(self):
        return self.proc.pid if self.proc else None


class CpuSampler:
    """CPU of this process and every descendant, by Chromium process type.

    Process objects are kept across ticks so cpu_percent(None) measures
    from the previous tick; a process seen for the first time only gets
    its baseline and counts from the next tick.
    """

    def __init__(self, exclude):
        self.me = psutil.Process()
        self.exclude = exclude      # pids to leave out (our nvidia-smi)
        self.known = {}             # pid -> (Process, type)
        self.ticks = []             # one {type: percent} per tick

    def kind(self, proc):
        if proc.pid == self.me.pid:
            return "browser"
        args = proc.cmdline()
        kind = next((a.split("=", 1)[1] for a in args if a.startswith("--type=")), proc.name())
        sub = next((a.split("=", 1)[1] for a in args if a.startswith("--utility-sub-type=")), "")
        return f"{kind}:{sub.split('.')[0]}" if sub else kind

    def tick(self, record=True):
        sample = {}
        for proc in [self.me] + self.me.children(recursive=True):
            if proc.pid in self.exclude():
                continue
            try:
                if proc.pid not in self.known:
                    self.known[proc.pid] = (proc, self.kind(proc))
                    proc.cpu_percent(None)
                    continue
                known, kind = self.known[proc.pid]
                sample[kind] = sample.get(kind, 0.0) + known.cpu_percent(None)
            except psutil.Error:
                self.known.pop(proc.pid, None)
        if record:
            self.ticks.append(sample)


class Watch:
    KICK_DELAY = 3000      # ms after load before touching the player
    KICK_EVERY = 3000
    SETTLE = 12000         # ms from the first kick to the first sample

    def __init__(self, app, window, url, seconds, shot=None):
        self.app, self.window, self.seconds = app, window, seconds
        self.shot = shot       # where to save what the picture check read
        self.page = window.page
        self.sampling = False
        self.gpu = GpuSampler()
        self.cpu = CpuSampler(exclude=lambda: {self.gpu.pid()})
        self.baseline = nvidia_once()
        self.page.loadFinished.connect(self.on_loaded)
        self.page.load(url)

    def on_loaded(self, ok):
        self.page.loadFinished.disconnect(self.on_loaded)
        if not ok:
            log("page failed to load")
        QTimer.singleShot(self.KICK_DELAY, self.kick)
        QTimer.singleShot(self.KICK_DELAY + self.SETTLE, self.start_sampling)

    def kick(self):
        if self.sampling:
            return
        self.page.runJavaScript(KICK_JS, 0, lambda r: log("kick:", r))
        QTimer.singleShot(self.KICK_EVERY, self.kick)

    def start_sampling(self):
        self.sampling = True
        self.page.runJavaScript(FACTS_JS, 0, self.on_start_facts)
        self.gpu.start()
        self.cpu.tick(record=False)
        self.remaining = self.seconds
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

    def on_start_facts(self, facts):
        self.start_facts = json.loads(facts) if facts else {}

    def tick(self):
        self.cpu.tick()
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.stop()
            self.gpu.stop()
            self.page.runJavaScript(FACTS_JS, 0, self.on_end_facts)

    def on_end_facts(self, facts):
        facts = json.loads(facts) if facts else {}
        self.report(facts, self.picture(facts.get("rect")))
        self.app.quit()

    def picture(self, rect):
        """(mean brightness, lit share) of the window where the video is drawn.

        A decoder can be busy while nothing reaches the window, and the
        page cannot tell: its frame counters keep counting. This reads the
        window's own contents, which under a compositor are whole even
        while another window covers it.
        """
        if not rect:
            return None
        view = self.window.view
        x, y = max(0, int(rect[0])), max(0, int(rect[1]))
        w = min(int(rect[2]), view.width() - x)
        h = min(int(rect[3]), view.height() - y)
        if w <= 0 or h <= 0:
            return None
        origin = view.mapTo(self.window, QPoint(x, y))
        shot = self.window.screen().grabWindow(self.window.winId(), origin.x(), origin.y(), w, h)
        if self.shot:
            shot.save(self.shot)
        image = shot.toImage().scaled(48, 27)
        values = [image.pixelColor(i, j).lightnessF()
                  for i in range(image.width()) for j in range(image.height())]
        lit = sum(v > 0.2 for v in values)
        return sum(values) / len(values), lit / len(values)

    def report(self, facts, picture):
        ticks = self.cpu.ticks
        totals = [sum(t.values()) for t in ticks]
        kinds = sorted({k for t in ticks for k in t})
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0

        print(f"watched  {self.page.url().toString()}")
        print(f"         {len(ticks)} s sampled after ~{self.SETTLE // 1000} s settle")
        print(f"cpu      total mean {mean(totals):.1f}%  max {max(totals, default=0):.1f}%"
              "  (100 = one core)")
        by_kind = sorted(((mean([t.get(k, 0.0) for t in ticks]), k) for k in kinds), reverse=True)
        for value, kind in by_kind:
            print(f"           {kind:26} {value:6.1f}%")

        rows = self.gpu.rows
        if rows:
            base = (f"baseline decoder {self.baseline[0]:.0f}%  gpu {self.baseline[1]:.0f}%"
                    if self.baseline else "no baseline")
            print(f"nvidia   decoder mean {mean([r[0] for r in rows]):.1f}%"
                  f"  gpu mean {mean([r[1] for r in rows]):.1f}%  ({base}; {len(rows)} samples)")
        else:
            print("nvidia   no readings (nvidia-smi missing or silent)")

        f = facts
        print(f"video    {f.get('size')}  paused={f.get('paused')}  t={f.get('time', 0):.1f}s")
        if picture:
            verdict = "drawn" if picture[1] > 0.3 else "BLACK: frames are not reaching the window"
            print(f"picture  brightness mean {picture[0]:.2f}  lit {picture[1]:.0%}  ({verdict})")
        start = getattr(self, "start_facts", {})
        if "frames" in f:
            during = ""
            if "frames" in start:
                during = (f"  (while sampled: {f['frames'] - start['frames']} total,"
                          f" {f['dropped'] - start['dropped']} dropped)")
            print(f"frames   {f['frames']} total, {f['dropped']} dropped{during}")
        if "quality" in f:
            print(f"youtube  quality={f.get('quality')}  codecs={f.get('codecs')}"
                  f"  resolution={f.get('resolution')}")
        for key in ("nerdsKeys", "nerdsError", "error"):
            if key in f:
                print(f"         {key}: {f[key]}")
        print(f"ad       {'showing' if f.get('ad') else 'none'}")


# --- --strip-test -----------------------------------------------------------

# The band's colour under test. The strip never draws it, so any of it
# read back from the window is band the strip is not covering.
TELL = "#00ff00"


class FakeFullScreenRequest:
    """Drives on_fullscreen without the page asking. The test keeps the
    window's geometry (a real fullscreen would cover the user's screen),
    so only the band and the strip's half of it run."""

    def __init__(self, on):
        self.on = on

    def accept(self):
        pass

    def toggleOn(self):
        return self.on


class KeyCounter(QObject):
    """Every key press Qt delivers to one window."""

    def __init__(self):
        super().__init__()
        self.presses = []
        self.targets = set()        # what Qt hands them to inside the window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            self.presses.append(event.text())
            target = obj.focusObject()
            self.targets.add(target.metaObject().className() if target else "nothing")
        return False


class StripTest:
    """The browser's half of selftest.py: one JSON command per line on
    stdin, each answered by one "@@ <json>" line on stdout."""

    def __init__(self, app, profile, first):
        self.app, self.profile = app, profile
        self.windows = []
        self.keys = {}
        self.watch(first)
        self.buffer = b""
        self.notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self.on_stdin)

    def watch(self, window):
        window.winId()          # the native window, so it has a QWindow to filter
        counter = KeyCounter()
        window.windowHandle().installEventFilter(counter)
        self.keys[id(window)] = counter
        self.windows.append(window)

    def on_stdin(self):
        data = os.read(sys.stdin.fileno(), 65536)
        if not data:
            self.app.quit()     # the test went away
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.strip():
                self.handle(json.loads(line))

    def reply(self, seq, **fields):
        print("@@ " + json.dumps({"seq": seq, **fields}), flush=True)

    def handle(self, cmd):
        seq = cmd.get("seq")
        window = self.windows[cmd.get("win", 0)]
        try:
            getattr(self, "op_" + cmd["op"])(seq, window, cmd)
        except Exception as e:
            self.reply(seq, error=repr(e))

    def op_info(self, seq, w, _cmd):
        valid = shiboken6.isValid(w)
        if not valid:
            self.reply(seq, valid=False)
            return
        rect = lambda r: [r.x(), r.y(), r.width(), r.height()]
        history = w.page.history()
        counter = self.keys[id(w)]
        presses = counter.presses
        self.reply(seq, valid=True, wid=int(w.winId()), geometry=rect(w.geometry()),
                   band=rect(w.band.geometry()), band_visible=w.band.isVisible(),
                   view=rect(w.view.geometry()), url=w.page.url().toString(),
                   title=w.page.title(), progress=w.progress, loading=w.loading,
                   back=history.canGoBack(), forward=history.canGoForward(),
                   attached=w.link.attached, label=w.band.text(),
                   keys=len(presses), key_text="".join(presses),
                   key_targets=sorted(counter.targets), active=w.isActiveWindow())

    def op_load(self, seq, w, cmd):
        w.page.load(QUrl(cmd["url"]))
        self.reply(seq)

    def op_open(self, seq, _w, cmd):
        window = open_window(self.profile, dialog=True)
        keep_out_of_the_way(window, self.app, offset=40 * len(self.windows))
        self.watch(window)
        window.page.load(QUrl(cmd["url"]))
        window.show()
        self.reply(seq, win=len(self.windows) - 1)

    def op_close(self, seq, w, _cmd):
        w.close()
        self.reply(seq)

    def op_resize(self, seq, w, cmd):
        t = time.monotonic()
        w.resize(cmd["w"], cmd["h"])
        self.reply(seq, t=t)

    def op_fullscreen(self, seq, w, cmd):
        w.showFullScreen = w.showNormal = lambda: None
        try:
            t = time.monotonic()
            w.on_fullscreen(FakeFullScreenRequest(cmd["on"]))
        finally:
            del w.showFullScreen, w.showNormal
        self.reply(seq, t=t)

    def op_js(self, seq, w, cmd):
        w.page.runJavaScript(cmd["code"], 0, lambda value: self.reply(seq, value=value))

    def op_grab(self, seq, w, _cmd):
        """The band as Qt reads it: QScreen.grabWindow on the window."""
        shot = w.screen().grabWindow(w.winId(), 0, 0, w.width(), BAND)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        shot.toImage().save(buffer, "PNG")
        self.reply(seq, png=base64.b64encode(bytes(buffer.data())).decode())

    def op_quit(self, seq, _w, _cmd):
        self.reply(seq)
        self.app.quit()


# --- main -------------------------------------------------------------------

def keep_out_of_the_way(window, app, offset=0):
    """An unattended run's window: floating in the bottom-right corner,
    never taking the keyboard, muted."""
    window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    area = app.primaryScreen().availableGeometry()
    window.resize(1000, 660)
    window.move(area.right() - window.width() - 12 - offset,
                area.bottom() - window.height() - 12 - offset)
    window.page.setAudioMuted(True)


def parse_args():
    argv = sys.argv[1:]
    # argparse refuses `--flags --enable-features=X` (a value that looks like
    # an option), so glue the value onto the flag.
    if "--flags" in argv[:-1]:
        i = argv.index("--flags")
        argv[i:i + 2] = [f"--flags={argv[i + 1]}"]

    parser = argparse.ArgumentParser(usage=__doc__.split("\n\n")[1])
    parser.add_argument("url", nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--watch", type=int, metavar="SECONDS")
    mode.add_argument("--strip-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--out")
    parser.add_argument("--shot", metavar="FILE")
    parser.add_argument("--ua", choices=("chrome", "qt"), default="chrome")
    parser.add_argument("--google-firefox", action="store_true")
    parser.add_argument("--flags", default="")
    args = parser.parse_args(argv)
    if args.watch is not None and not args.url:
        parser.error("--watch needs a url")
    return args


def main():
    args = parse_args()
    # Pages follow Qt's colour scheme, but asking Qt for dark is ignored by
    # platform themes that do not implement the request (qt6ct, the generic
    # one), so Blink is also told directly.
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(
        filter(None, [flags, "--blink-settings=preferredColorScheme=0", args.flags]))

    QCoreApplication.setApplicationName(NAME)
    QGuiApplication.setDesktopFileName(NAME)
    app = QApplication(sys.argv[:1])
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)

    # Ctrl+C in the terminal: Python only sees the signal when it runs, so
    # give it a moment every quarter second.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)

    profile = make_profile(args)
    automated = args.probe or args.watch is not None or args.strip_test
    if args.strip_test:
        Window.band_background = TELL
    window = open_window(profile, dialog=automated)

    if automated:
        keep_out_of_the_way(window, app)
        Window.ask_permissions = False
        limit = (90 if args.probe else 300 if args.strip_test
                 else Watch.KICK_DELAY // 1000 + Watch.SETTLE // 1000 + args.watch + 60)
        QTimer.singleShot(limit * 1000, lambda: (log(f"gave up after {limit} s"), app.exit(1)))

    # `job` only keeps the Probe / Watch alive: signals hold its methods weakly.
    if args.probe:
        job = Probe(app, window, args.out)
    elif args.watch is not None:
        profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        job = Watch(app, window, QUrl.fromUserInput(args.url), args.watch, args.shot)
    elif args.strip_test:
        job = StripTest(app, profile, window)
        window.page.load(QUrl.fromUserInput(args.url) if args.url else QUrl("about:blank"))
    else:
        window.page.load(QUrl.fromUserInput(args.url) if args.url else QUrl(HOME))
    window.show()

    code = app.exec()

    # Pages first, then the profile: deleting a window takes its view and
    # page with it, and a profile released under a live page complains.
    job = None
    for w in WINDOWS:
        if shiboken6.isValid(w):
            shiboken6.delete(w)
    WINDOWS.clear()
    shiboken6.delete(profile)
    return code


if __name__ == "__main__":
    sys.exit(main())
