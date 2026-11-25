"""Flux in the tests, out of sight: inside a hidden cage.

Flux always runs on a screen (flux/qt/startup.py). A test that runs it
where nobody sees it runs it as a client of cage, the kiosk Wayland
compositor, on wlroots' headless backend: no output reaches any screen,
no input comes from any device.

    WLR_BACKENDS=headless cage -- <the test, or Flux>

with QT_QPA_PLATFORM=wayland and the WAYLAND_DISPLAY cage gives its
child (its own socket, in $XDG_RUNTIME_DIR). A test calls `inside()`
first thing in its main: outside a cage it runs itself again inside one
and exits with its exit code; inside, it goes on, and every Flux it
starts (in this process, or a child inheriting the environment) is that
cage's client. $DISPLAY is dropped in there (cage's own Xwayland, the
user's session: neither is for Flux), and so is the WAYLAND_DISPLAY the
test was started with: only cage's reaches the test.

cage's own log goes to a file beside nothing of the user's (the system's
temporary directory), said when cage ends badly.

A Wayland client cannot take the keyboard focus or move the pointer:
`activateWindow()` is a request cage ignores, and the headless backend
has no pointer. cage gives the focus to the window mapped last, so
`focus(window)` maps it again (and where cage keeps it elsewhere, tells
Qt the window is the active one, as the user's click would);
`point_at(point)` makes QCursor.pos(), as Flux reads it, say where a
test puts the pointer.

Needs cage (0.2 or later: it exits with its client's exit code).
"""

import os
import shutil
import subprocess
import sys
import tempfile

MARK = "FLUX_TEST_IN_CAGE"      # set in the cage's client: already inside


def environ(base: dict | None = None) -> dict:
    """The environment to start cage with: its headless backend, Qt on
    Wayland, nothing of a screen the test was started on."""
    env = dict(os.environ if base is None else base)
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        env.pop(var, None)
    env.update({"WLR_BACKENDS": "headless", "WLR_LIBINPUT_NO_DEVICES": "1",
                "QT_QPA_PLATFORM": "wayland", MARK: "1"})
    return env


def command(argv: list[str], stderr_fd: int) -> list[str]:
    """`argv` as cage's client, its stderr the test's (`stderr_fd`, kept
    open across cage) and with no $DISPLAY."""
    return ["cage", "--", "sh", "-c", f'unset DISPLAY; exec "$@" 2>&{stderr_fd}', "sh", *argv]


def inside() -> None:
    """Go on when this process is a hidden cage's client; else run this
    script again as one, with the same arguments, and exit with its code."""
    if os.environ.get(MARK):
        os.environ.pop("DISPLAY", None)
        return
    if shutil.which("cage") is None:
        sys.exit("the Flux tests need cage (a kiosk Wayland compositor) to run Flux out of "
                 "sight: not on PATH")
    err = os.dup(2)
    log = tempfile.NamedTemporaryFile(prefix="flux-test-cage-", suffix=".log", delete=False)
    try:
        code = subprocess.run(command([sys.executable, *sys.argv], err), env=environ(),
                              stdout=None, stderr=log, pass_fds=(err,)).returncode
    finally:
        os.close(err)
        log.close()
    if code != 0:
        print(f"(cage's log: {log.name})", file=sys.stderr)
    else:
        os.unlink(log.name)
    sys.exit(code)


def focus(window, timeout: float = 3.0) -> bool:
    """`window` (a top-level QWidget) given the keyboard focus, as cage
    gives it: to the window mapped last. Whether it has it in time."""
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    window.hide()
    window.show()
    waited = 0.0
    while QApplication.activeWindow() is not window and waited < timeout:
        if waited >= 1.0:
            QApplication.setActiveWindow(window)
        QTest.qWait(20)
        waited += 0.02
    return QApplication.activeWindow() is window


def point_at(point) -> None:
    """QCursor.pos() says `point` (global coordinates) from now on: the
    pointer where the test puts it."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QCursor
    where = QPoint(point)
    QCursor.pos = staticmethod(lambda *_: QPoint(where))
