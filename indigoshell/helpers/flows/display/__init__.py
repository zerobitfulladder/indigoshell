"""Display flow — pick one monitor as the sole primary.

Steps:
  1. `display_query`  — list connected outputs, branch on count
  2. `display_set N`  — apply xrandr for the chosen output (leaf)

`indigoshell open display-menu` triggers the cascade.
"""

from pathlib import Path

_HERE = Path(__file__).parent

SCRIPTS: dict[str, list[str] | str] = {
    "display_query": str(_HERE / "query.py"),
    "display_set":   str(_HERE / "set.py"),
}

PIPELINES: dict[str, list[str]] = {
    "display-menu": ["display_query"],
}
