"""The kinds of item an app's bar holds, and the shell's widget for each
(PROTOCOL.md, section 2): `tabs` (tabs.py), `text` and `button`
(text.py); the open input's box (input.py) is no item, the section
itself turned into a text box.

Every item widget is an `Item`: it is given the item's dict whole
(`set`) every time the app's bar changes and keeps its own state (hover,
a drag, motion) for as long as the item keeps its id and kind; what the
user does with it goes out through `emit` (an event naming the item, set
by the view: applink/view.py) or `open` (an input or menu it opens). Its
look is the shell's, from look.py: an app says what, never how.
"""

import logging

from ...widgets.base import Widget

log = logging.getLogger(__name__)


class Item(Widget):
    """One item of an app's bar."""

    kind = ""

    def __init__(self, item_id, **kwargs) -> None:
        super().__init__(**kwargs)
        self.id = item_id
        self.spec: dict = {}
        self.lit = False                # what it opens is showing
        self.emit = None                # emit(type, **fields): an event naming this item
        self.open = None                # open(what id, item): an input or menu it names

    def set(self, spec: dict) -> None:
        """The item as the app sends it now (the same id and kind)."""
        if spec != self.spec:
            self.spec = spec
            self.invalidate(layout=True)

    def set_lit(self, value: bool) -> None:
        if value != self.lit:
            self.lit = value
            self.invalidate()

    def natural(self) -> float:
        """Its width with room enough: the view gives it this or less."""
        return 0.0

    def _emit(self, type_: str, **fields) -> None:
        if self.emit is not None:
            self.emit(type_, **fields)


def make(kind: str, item_id) -> Item | None:
    """A widget for an item of `kind`; None for a kind the shell does not
    know (an app's newer kind: skipped)."""
    from .tabs import TabsItem
    from .text import ButtonItem, TextItem
    cls = {"tabs": TabsItem, "text": TextItem, "button": ButtonItem}.get(kind)
    return cls(item_id) if cls is not None else None
