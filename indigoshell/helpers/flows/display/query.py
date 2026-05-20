"""Pipeline script — list connected displays.

Stdout (visible in the TermToast running this script): a formatted
list of currently-connected outputs, with current resolution and a
PRIMARY flag.

Manifest (written to $INDIGOSHELL_MANIFEST so the orchestrator can
decide the next stage): one option per connected output. Each option's
`command` references the registered `display_set` script.
"""

import json
import os
import re
import subprocess
import sys


def _connected_outputs(xrandr_out: str) -> list[tuple[str, str, bool]]:
    """Returns [(name, current_resolution, is_primary), ...]."""
    rows: list[tuple[str, str, bool]] = []
    for line in xrandr_out.splitlines():
        if " connected" not in line or line.startswith(" "):
            continue
        parts = line.split()
        name = parts[0]
        primary = "primary" in parts
        mode_m = re.search(r"\b(\d+x\d+)\+\d+\+\d+", line)
        mode = mode_m.group(1) if mode_m else "—"
        rows.append((name, mode, primary))
    return rows


def main() -> None:
    try:
        out = subprocess.check_output(["xrandr", "--query"], text=True)
    except FileNotFoundError:
        print("xrandr not installed")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"xrandr failed: {e}")
        sys.exit(1)

    rows = _connected_outputs(out)
    print("\033[1;36m── connected displays ──\033[0m\n")
    if not rows:
        print("  no connected outputs")
    else:
        for i, (name, mode, primary) in enumerate(rows, 1):
            flag = "  \033[33mPRIMARY\033[0m" if primary else ""
            print(f"  \033[35m{i}\033[0m  \033[36m{name:<14}\033[0m  \033[37m{mode:<12}\033[0m{flag}")

    manifest = {
        "options": [
            {"label": name, "command": ["display_set", name]}
            for name, _mode, _primary in rows
        ],
        # User pressed Escape / mod+q on the menu → run display_set
        # cancel branch instead of dropping the cascade silently.
        "cancel": {"command": ["display_set", "cancel"]},
    }
    path = os.environ.get("INDIGOSHELL_MANIFEST")
    if path:
        with open(path, "w") as f:
            json.dump(manifest, f)


if __name__ == "__main__":
    main()
