"""In-process helpers used by menus and other config-level wiring.

Each module exposes plain `Callable[[], None]` functions that orchestrate
system tools through `services.proc`. Use them as menu actions:

    from .helpers import display, layout, profile, power
    MenuItem("1", "SUSPEND", power.suspend)

Adding a new helper: create `helpers/<name>.py`, expose top-level
functions, import it where it's needed. No registration step.
"""
