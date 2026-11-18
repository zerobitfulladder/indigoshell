"""The INDIGO bridge add-on's icons: the call agent's own chip, at rest,
rendered by the real Chip widget into indigoshell/agent/extension/.

    cd prototypes/visuals && PYTHONPATH=../.. ../../.venv/bin/python render_bridge_icon.py
"""

import os

import skia

from common import C  # noqa: F401  (also quiets logging)
from indigoshell.agent.ui.chip import Chip
from indigoshell.agent.ui.style import AGENT_ACCENT

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "..", "indigoshell", "agent",
                                    "extension"))


def main() -> None:
    # The toolbar uses 16 and 32; at 16 there is no room for a word.
    for size in (16, 32, 48, 96, 128):
        label = "INDIGO" if size >= 96 else "AI" if size >= 32 else ""
        chip = Chip(size, lambda label=label: (AGENT_ACCENT, label, "still"))
        chip.rect = skia.Rect.MakeWH(size, size)
        s = skia.Surface(size, size)
        s.getCanvas().clear(skia.ColorTRANSPARENT)
        chip.paint(s.getCanvas())
        s.flushAndSubmit()
        path = os.path.join(OUT, f"icon-{size}.png")
        s.makeImageSnapshot().save(path, skia.kPNG)
        print("wrote", path)


if __name__ == "__main__":
    main()
