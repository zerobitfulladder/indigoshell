"""The agent's workspace: what it starts with, and what is kept current.

Two kinds of file, set up on every dial:

- Starting files — `agent/workspace/` mirrored into the workspace: the
  notebook (CLAUDE.md) and the report skill. Copied in only when
  missing. After that they belong to the agent and the user: a change
  asked for on a call ("show videos in mpv", "plainer reports") is an
  edit to these, and it has to stick.

- Generated files — derived from the shell itself and rewritten every
  time, so they cannot drift: `palette.css`, the theme's colours as CSS
  variables, which reports paste in to match the desktop. Edit
  `indigoshell/theme.py`, not the generated file.

Nothing here is loaded into the model by default. Claude Code puts only
the CLAUDE.md and each skill's one-line description in a session's
context; the skill's body, template and palette are read when it writes
a report.
"""

import logging
import os
import shutil

from .. import theme

log = logging.getLogger(__name__)

TEMPLATE = os.path.join(os.path.dirname(__file__), "workspace")
PALETTE = os.path.join(".claude", "skills", "report", "palette.css")

# The theme's colour ramps and the HUD text and plate colours; the
# per-widget names (NOTIF_*, WORKSPACE_*, ...) are roles on top of these
# and would only add noise to a report.
_KEEP = ("BASE_", "MAGENTA_", "YELLOW_", "CYAN_", "LIME_", "VIOLET",
         "ERROR", "HUD_", "POPUP_BG")


def seed(workspace: str) -> None:
    """Copy in the missing starting files and rewrite the generated ones."""
    for here, _dirs, files in os.walk(TEMPLATE):
        rel = os.path.relpath(here, TEMPLATE)
        dest_dir = os.path.normpath(os.path.join(workspace, rel))
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            dest = os.path.join(dest_dir, name)
            if not os.path.exists(dest):
                shutil.copyfile(os.path.join(here, name), dest)
                log.info("agent workspace: added %s",
                         os.path.relpath(dest, workspace))
    path = os.path.join(workspace, PALETTE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    css = palette_css()
    try:
        with open(path) as f:
            if f.read() == css:
                return
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(css)


def palette_css() -> str:
    """The theme's colours and font as CSS custom properties:
    CYAN_BRIGHT becomes --cyan-bright."""
    lines = [
        "/* indigoshell's palette, generated from indigoshell/theme.py on",
        "   every call. Paste it at the top of a report's <style> so the",
        "   page carries its own colours. Do not edit: it is rewritten;",
        "   change the theme instead. */",
        ":root {",
    ]
    for name, value in vars(theme).items():
        if (name.isupper() and isinstance(value, str)
                and value.startswith("#") and name.startswith(_KEEP)):
            lines.append(f"  --{name.lower().replace('_', '-')}: {value.lower()};")
    fonts = ", ".join(f'"{f}"' if f != "monospace" else f
                      for f in (theme.FONT, *theme.FONT_FALLBACKS))
    lines += [f"  --font-mono: {fonts};", "}", ""]
    return "\n".join(lines)
