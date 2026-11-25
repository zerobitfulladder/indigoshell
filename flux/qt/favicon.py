"""A page's icon as Flux sends it to the shell: a small PNG and its id
(indigoshell/applink/PROTOCOL.md, "Images").

QtWebEngine gives a page's favicon as a QIcon holding every size the
site offers (`QWebEnginePage.icon`, `iconChanged`). The shell draws it
16 px in a tab's spot, through its own look (applink/kinds/glow.py), so
what goes over the line is the icon at IMAGE_SIDE px (32, room for a
crisp downscale), never larger: Qt picks the size nearest (QtWebEngine
offers a 16 px favicon at 32 too, scaled up by Chromium). Asked for at
a device pixel ratio of 1, so a HiDPI screen does not double it.

Its id is the PNG's hash: the same icon on many tabs, or again after a
navigation within a site, is the same id, sent once (link.py), and a
changed icon is a new id.
"""

import hashlib

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QIcon, QImage

from indigoshell.applink import bar as B


def png_of(icon: QIcon) -> bytes | None:
    """The icon as a PNG at most B.IMAGE_SIDE px a side (its shape kept);
    None for no icon, or one too large to send."""
    if icon is None or icon.isNull():
        return None
    side = B.IMAGE_SIDE
    image = icon.pixmap(QSize(side, side), 1.0).toImage()
    if image.isNull():
        return None
    if image.width() > side or image.height() > side:
        image = image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    png = bytes(data)
    return png if 0 < len(png) <= B.IMAGE_BYTES_MAX else None


def id_of(png: bytes) -> str:
    """The id a PNG goes by: its hash, short."""
    return hashlib.blake2s(png, digest_size=8).hexdigest()
