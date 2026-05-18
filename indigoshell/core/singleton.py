import fcntl
import os
import sys

from .paths import lock_path


def acquire_lock() -> int:
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        print("indigoshell: another instance is already running", file=sys.stderr)
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd
