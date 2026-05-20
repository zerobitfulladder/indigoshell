"""Power / session helpers.

`logout` is wired to qtile's IPC shutdown — adjust if you swap WMs.
"""

from ..services import proc


def suspend()  -> None: proc.fire(["systemctl", "suspend"])
def poweroff() -> None: proc.fire(["systemctl", "poweroff"])
def reboot()   -> None: proc.fire(["systemctl", "reboot"])
def logout()   -> None: proc.fire(["qtile", "cmd-obj", "-o", "cmd", "-f", "shutdown"])
