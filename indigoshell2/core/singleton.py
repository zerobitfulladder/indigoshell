import fcntl
import logging
import os

from .paths import lock_path

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Another daemon holds the lock."""


def acquire_lock() -> int:
    """Take the exclusive daemon lock.

    The fd is deliberately left open for the process lifetime — flock is
    released when the last descriptor closes. Python opens fds
    non-inheritable (CLOEXEC) by default, which is what makes `reload`
    work: the exec'd process gets a clean slate and can re-acquire.
    """
    path = lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_pid(fd)
        os.close(fd)
        raise AlreadyRunning(
            f"another instance is already running (pid {holder})" if holder
            else "another instance is already running"
        ) from None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    log.debug("acquired %s (pid %d)", path, os.getpid())
    return fd


def _read_pid(fd: int) -> str | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 32).decode().strip() or None
    except OSError:
        return None


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    log.debug("released %s", lock_path())
