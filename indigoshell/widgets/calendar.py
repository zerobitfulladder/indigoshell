import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from ..style import Style
from .base import Widget


class Calendar(Widget):
    def __init__(self, style: Style | None = None, **kwargs):
        super().__init__(style, **kwargs)

    def build_widget(self):
        return Gtk.Calendar()
