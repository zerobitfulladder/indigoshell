"""MOD+D's app search: the installed apps as the rows of the top bar's
input, the shell's own (no app behind it: PROTOCOL.md, section 1).

`AppSearch` is a prompt's `local` source (applink/prompt.py): it
searches the app catalog (services/apps.py: fuzzy on the name, then the
generic name, keywords and program, launch history lifting the apps
actually used) as typed, and picks there. A row is an app: its name, its
matched characters lit, its generic name as the detail, its icon (drawn
by the drop-down from the icon theme, the name's initial where there is
none). With nothing matching what was typed, one row instead: RUN it as
a command line.

    Return          launch the selected app; with no app matching, run
                    the text as a command line
    Shift+Return    run the text as a command line whatever is selected

A launch or a command line that fails flashes the row and leaves the
search open.
"""

import logging

from ..services.apps import Catalog

log = logging.getLogger(__name__)

APPS = "apps"                   # the input's name
PLACEHOLDER = "search applications"
RUN = "run"                     # the row's id for running the text


class AppSearch:
    """The installed apps, as a prompt's rows (the module docstring)."""

    def __init__(self, catalog: Catalog, icons=None) -> None:
        self.catalog = catalog
        self.icons = icons
        first = not catalog.apps
        if catalog.refresh() and not first and icons is not None:
            icons.refresh()             # installed apps bring icons along
        self.results: dict[str, object] = {}

    @property
    def spec(self) -> dict:
        return {"icon": "search", "placeholder": PLACEHOLDER}

    @property
    def total(self) -> int:
        return len(self.catalog.apps)

    def search(self, text: str) -> tuple[list[dict], int]:
        results = self.catalog.search(text)
        self.results = {}
        rows = []
        for r in results:
            entry = r.app.entry
            self.results[entry.id] = r.app
            row = {"id": entry.id, "title": entry.name, "_icon": entry.icon,
                   "_initial": entry.name[:1].upper()}
            if entry.generic_name:
                row["detail"] = entry.generic_name
            if r.positions:
                row["match"] = list(r.positions)
            rows.append(row)
        if not rows and text.strip():
            rows.append({"id": RUN, "title": text.strip(), "tag": "RUN"})
        return rows, 0

    def pick(self, row: dict | None, text: str, *, shift: bool = False) -> bool:
        """Return on `row` (None: nothing listed), or with `shift` the
        text itself. False when it failed (the row flashes)."""
        if shift or row is None or row.get("id") == RUN:
            line = text.strip()
            if not line:
                return False
            try:
                self.catalog.run(line)
            except ValueError:          # unbalanced quotes
                log.warning("cannot parse command line %r", line)
                return False
            return True
        app = self.results.get(row.get("id"))
        if app is None:
            return False
        try:
            self.catalog.launch(app)
        except Exception:
            log.exception("cannot launch %s", row.get("id"))
            return False
        return True

    def close(self) -> None:
        self.results = {}
