"""Logging setup.

Modules do the standard thing:

    import logging
    log = logging.getLogger(__name__)

which makes every logger a child of `APP` and follows the cutover rename
for free. `setup()` installs handlers on the *root* logger so that
warnings from asyncio, watchdog and dbus-fast still surface, while only
our own namespace follows the requested level.

Default is stderr — a session daemon's stderr is captured by whatever
started it. `--log-file` adds a rotating file for the common case where
that's /dev/null.
"""

import logging
import logging.handlers
import os
import sys

from .naming import APP, env

LEVELS = ("debug", "info", "warning", "error")

# ── palette (INDIGO theme, 256-color) ───────────────────────────────────
_RESET = "\033[0m"
_MUTED = "\033[38;5;60m"    # BASE_MUTED  — timestamps
_VIOLET = "\033[38;5;141m"  # VIOLET_BRIGHT — logger names

_LEVEL_COLOR = {
    logging.DEBUG:    "\033[38;5;60m",      # muted violet-grey
    logging.INFO:     "\033[38;5;44m",      # CYAN_BRIGHT
    logging.WARNING:  "\033[38;5;220m",     # YELLOW_BRIGHT
    logging.ERROR:    "\033[38;5;198m",     # MAGENTA_BRIGHT
    logging.CRITICAL: "\033[1;38;5;196m",   # ERROR, bold
}
# Body text is left uncolored below WARNING so log output stays readable
# against any terminal background; problems tint their whole line.
_MSG_COLOR = {
    logging.WARNING:  "\033[38;5;220m",
    logging.ERROR:    "\033[38;5;198m",
    logging.CRITICAL: "\033[1;38;5;196m",
}

_DATEFMT = "%H:%M:%S"
_NAME_W = 18
_LEVEL_W = 7


def _use_color(stream) -> bool:
    # https://no-color.org — an env var set to anything disables color.
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get(env("FORCE_COLOR")) is not None:
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class _Formatter(logging.Formatter):
    """Builds the line by hand.

    Padding has to be applied to the bare text and the color wrapped
    around the result — `%(levelname)-7s` on an already-escaped string
    counts the escape bytes toward the field width and the columns drift.
    """

    def __init__(self, color: bool) -> None:
        super().__init__(datefmt=_DATEFMT)
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, _DATEFMT)
        name = self._short(record.name).ljust(_NAME_W)
        level = record.levelname.ljust(_LEVEL_W)
        msg = record.getMessage()

        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            msg += "\n" + self.formatStack(record.stack_info)

        if not self.color:
            return f"{ts} {level} {name} {msg}"

        level_tint = _LEVEL_COLOR.get(record.levelno, "")
        msg_tint = _MSG_COLOR.get(record.levelno)
        body = f"{msg_tint}{msg}{_RESET}" if msg_tint else msg
        return (
            f"{_MUTED}{ts}{_RESET} "
            f"{level_tint}{level}{_RESET} "
            f"{_VIOLET}{name}{_RESET} "
            f"{body}"
        )

    @staticmethod
    def _short(name: str) -> str:
        # "indigoshell2.core.daemon" -> "core.daemon"
        if name == APP:
            return "-"
        if name.startswith(APP + "."):
            return name[len(APP) + 1:]
        return name


def default_level() -> str:
    return os.environ.get(env("LOG_LEVEL"), "info").lower()


def setup(level: str | None = None, log_file: str | None = None) -> None:
    level = (level or default_level()).lower()
    if level not in LEVELS:
        level = "info"
    numeric = getattr(logging, level.upper())

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_Formatter(color=_use_color(sys.stderr)))
    root.addHandler(stream)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1 << 20, backupCount=3, encoding="utf-8",
        )
        rotating.setFormatter(_Formatter(color=False))
        root.addHandler(rotating)

    # Root stays at WARNING so third-party chatter (asyncio slow-callback
    # warnings, watchdog, dbus-fast) is visible but not verbose; our own
    # namespace follows the requested level.
    root.setLevel(logging.WARNING)
    logging.getLogger(APP).setLevel(numeric)


def is_debug() -> bool:
    return logging.getLogger(APP).isEnabledFor(logging.DEBUG)
