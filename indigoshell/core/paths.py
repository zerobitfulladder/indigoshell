import os


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


def socket_path() -> str:
    return os.path.join(runtime_dir(), f"indigoshell-{os.getuid()}.sock")


def lock_path() -> str:
    return os.path.join(runtime_dir(), f"indigoshell-{os.getuid()}.lock")
