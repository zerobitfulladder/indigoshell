"""Pipeline script — make one display the sole primary.

Argv:  display_set NAME

Stdout (visible in the TermToast running this script): the xrandr
command being applied, and xrandr's own output/errors — or a "nothing
to do" line when the target is already the sole active primary.

Manifest: empty options list — this is a leaf node, the orchestrator
will trigger the linger animation and close the cascade afterwards.
"""

import json
import os
import re
import subprocess
import sys

_MODE_RE = re.compile(r"\d+x\d+\+\d+\+\d+")


def _query_state() -> tuple[str | None, set[str], list[str]]:
    """Returns (current_primary, names_of_active_outputs, all_connected).
    An output is "active" when its header line carries a `WxH+X+Y` mode
    token — i.e. it's on, not just connected-but-disabled."""
    out = subprocess.check_output(["xrandr", "--query"], text=True)
    primary: str | None = None
    active: set[str] = set()
    connected: list[str] = []
    for line in out.splitlines():
        if " connected" not in line or line.startswith(" "):
            continue
        parts = line.split()
        name = parts[0]
        connected.append(name)
        if "primary" in parts:
            primary = name
        if any(_MODE_RE.search(p) for p in parts):
            active.add(name)
    return primary, active, connected


def _write_leaf_manifest() -> None:
    path = os.environ.get("INDIGOSHELL_MANIFEST")
    if path:
        with open(path, "w") as f:
            json.dump({"options": []}, f)


def main() -> None:
    if len(sys.argv) < 2:
        print("\033[31musage: display_set NAME\033[0m")
        sys.exit(1)
    target = sys.argv[1]

    # Cancel branch — invoked when the user dismisses the menu instead
    # of picking an output. No xrandr call; just announce and end.
    if target == "cancel":
        print("\033[33m— cancelled, no changes —\033[0m")
        _write_leaf_manifest()
        return

    primary, active, connected = _query_state()
    # No-op short-circuit: target is already the sole active primary.
    # Skip xrandr entirely and announce the state.
    if primary == target and active == {target}:
        print(f"\033[32m✓ {target} is already the sole primary — nothing to do\033[0m")
        _write_leaf_manifest()
        return

    args = ["xrandr", "--output", target, "--auto", "--primary"]
    for other in connected:
        if other != target:
            args += ["--output", other, "--off"]
    print(f"\033[1;33m→ {' '.join(args)}\033[0m\n")
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        print("\033[31mxrandr not installed\033[0m")
        sys.exit(1)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stdout.write(f"\033[31m{result.stderr}\033[0m")
    if result.returncode == 0:
        print(f"\n\033[32m✓ {target} is now primary\033[0m")
    else:
        print(f"\n\033[31m✗ exited {result.returncode}\033[0m")

    _write_leaf_manifest()


if __name__ == "__main__":
    main()
