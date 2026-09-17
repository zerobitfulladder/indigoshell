"""Every user-visible name the app answers to, in one place.

`APP` is derived from the package directory rather than hardcoded, so
the cutover rename (`git mv indigoshell2 indigoshell`) carries the
daemon name, runtime paths, config dir, log file, env vars and logger
namespace with it — no find-and-replace, nothing left stale.

`CLI` is the only name that can't be derived, since the client command
is deliberately shorter than the package. It's the single edit the
rename needs.
"""

# "indigoshell2.core" -> "indigoshell2"
APP = (__package__ or "indigoshell2").split(".")[0]

CLI = "indigo2"

# Env var prefix, e.g. INDIGOSHELL2_LOG_LEVEL.
ENV_PREFIX = APP.upper().replace("-", "_")


def env(name: str) -> str:
    """Name of an app env var: env("LOG_LEVEL") -> INDIGOSHELL2_LOG_LEVEL."""
    return f"{ENV_PREFIX}_{name}"
