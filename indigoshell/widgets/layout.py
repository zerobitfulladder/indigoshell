import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .. import theme
from ..style import Style
from .base import Widget


class Box(Widget):
    """Groups child widgets in a horizontal or vertical box."""

    def __init__(
        self,
        children: list[Widget],
        spacing: int = theme.BOX_SPACING,
        orientation: str = "horizontal",
        expand: bool = False,
        style: Style | None = None,
        **kwargs,
    ):
        super().__init__(style, **kwargs)
        self.children = children
        self.spacing = spacing
        self.orientation = orientation
        self.expand = expand

    def build_widget(self):
        orient = (
            Gtk.Orientation.HORIZONTAL
            if self.orientation == "horizontal"
            else Gtk.Orientation.VERTICAL
        )
        box = Gtk.Box(orientation=orient, spacing=self.spacing)
        for child in self.children:
            box.pack_start(child.build(), child.expand, child.expand, 0)
        return box

    def start(self):
        for child in self.children:
            child.start()

    def stop(self):
        super().stop()
        for child in self.children:
            child.stop()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Spacer(Widget):
    """Expanding empty widget — pushes neighbors apart."""

    expand = True

    def build_widget(self):
        return Gtk.Box()
