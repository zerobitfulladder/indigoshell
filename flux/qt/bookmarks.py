"""The browser's bookmarks: places.BookmarkFile, kept in step with the file.

The browser writes bookmarks.json when a bookmark is added or removed
(the shell's Ctrl+M actions send the command, and Shift+Delete on a
bookmark in its address list, naming it) and re-reads it when it
changes on disk, the way identity.py follows sites.json: a hand edit
(renaming a bookmark, deleting one) applies without a restart, and a
file that does not parse is reported and the list before it stays.
`changed` tells the windows, whose state says whether their page is a
bookmark.
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from flux import places

log = logging.getLogger(__name__)


class Bookmarks(QObject):
    changed = Signal()
    DEBOUNCE_MS = 150

    def __init__(self, parent=None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.file = places.BookmarkFile(path)
        self.file.load()
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._soon)
        self.watcher.directoryChanged.connect(self._soon)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.DEBOUNCE_MS)
        self.timer.timeout.connect(self._reload)
        self._watch()

    @property
    def path(self) -> Path:
        return self.file.path

    def _watch(self) -> None:
        # The directory too: a save that writes a new file and renames it
        # over the old one (ours, and most editors') ends the watch on the
        # file. The directory may not exist before the first bookmark.
        watched = self.watcher.files() + self.watcher.directories()
        for p in (str(self.path.parent), str(self.path)):
            if os.path.exists(p) and p not in watched:
                self.watcher.addPath(p)

    def _soon(self, *_) -> None:
        self.timer.start()

    def _reload(self) -> None:
        self._watch()
        if self.file.load():
            log.info("bookmarks: %d in %s", len(self.file.entries), self.path)
            self.changed.emit()

    def contains(self, url: str) -> bool:
        return self.file.contains(url)

    def find(self, url: str) -> dict | None:
        """The bookmark for `url`'s page, as the file has it."""
        return self.file.find(url)

    def add(self, url: str, name: str) -> bool:
        """False when nothing changed: a bookmark already, or the file
        could not be written (said in the log)."""
        try:
            done = self.file.add(url, name)
        except (places.BookmarksBroken, OSError) as e:
            log.warning("bookmark not added: %s", e)
            return False
        self._after(done, "added", url)
        return done

    def remove(self, url: str) -> bool:
        try:
            done = self.file.remove(url)
        except (places.BookmarksBroken, OSError) as e:
            log.warning("bookmark not removed: %s", e)
            return False
        self._after(done, "removed", url)
        return done

    def _after(self, done: bool, what: str, url: str) -> None:
        self._watch()
        if done:
            log.info("bookmark %s: %s", what, url)
            self.changed.emit()
