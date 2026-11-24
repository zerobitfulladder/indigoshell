"""What the agent shows in the chat, as the agent's plate draws it: a
picture inline in the turn's reply, scaled to the column, its caption
under it; a file as one line (its name, size and caption). Either opens
when clicked (or with Ctrl+F, the turn's last): saved to ~/Downloads and
handed to its usual program.

The bytes come from the hub only when a picture is on show
(`RemoteAgent.want_media`); until then it is a line saying it is on its
way. Pictures are decoded once and kept here, by the hub's id.
"""

from collections import OrderedDict

import skia

from ... import text, theme
from .rows import Row, text_rows
from .style import ACT_SIZE, BODY_SIZE, line_h

IMAGE_MAX_H = 360           # a picture's height in the turn, at most
IMAGE_PAD = 4               # above and below it
DECODED_KEEP = 12           # decoded pictures kept at once
FILE_GLYPH = "↓"
MEDIA_KINDS = ("image", "file", "media")


def size_text(size) -> str:
    if not isinstance(size, int) or size < 0:
        return ""
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (f"{size} {unit}" if unit == "bytes"
                    else f"{size:.1f} {unit}" if size < 10
                    else f"{size:.0f} {unit}")
        size /= 1024
    return ""


class Pictures:
    """Decoded pictures, by the hub's id."""

    def __init__(self) -> None:
        self._images: OrderedDict[str, skia.Image | None] = OrderedDict()

    def get(self, agent, sid: str) -> skia.Image | None | bool:
        """The picture; None while its bytes aren't here; False when
        they don't decode."""
        if sid in self._images:
            self._images.move_to_end(sid)
            return self._images[sid] if self._images[sid] is not None \
                else False
        item = agent.media(sid)
        if item is None:
            return None
        image = skia.Image.MakeFromEncoded(
            skia.Data.MakeWithCopy(item["data"]))
        image = image.makeRasterImage() if image is not None else None
        self._images[sid] = image
        while len(self._images) > DECODED_KEEP:
            self._images.popitem(last=False)
        return image if image is not None else False


def shown_rows(agent, pictures: Pictures, sid: str, w: float) -> list[Row]:
    """The rows of one thing the agent showed."""
    info = agent.shown.get(sid) or {"name": sid, "kind": "file",
                                    "caption": "", "size": None}
    lh = line_h(BODY_SIZE) + 3
    caption = info.get("caption") or ""
    big = isinstance(info.get("size"), int) and info["size"] > getattr(
        agent, "media_auto_max", 12 * 1024 * 1024)
    if info["kind"] != "image" or big:
        return [Row("file", lh, label=sid)]
    image = pictures.get(agent, sid)
    if image is None:
        failed = agent.media_failed(sid)
        if failed:
            return [Row("note", lh, label=f"{info['name']}: couldn't load "
                        f"it ({failed})", color=theme.BASE_MUTED)]
        agent.want_media(sid)
        return [Row("media", lh, label=sid)]
    if image is False:
        return [Row("file", lh, label=sid)]
    iw, ih = image.width(), image.height()
    scale = min(1.0, w / iw, IMAGE_MAX_H / ih)
    rows = [Row("image", round(ih * scale) + 2 * IMAGE_PAD, label=sid)]
    if caption:
        rows += text_rows(f"c{sid}", caption, ACT_SIZE, w, theme.HUD_FG)
    return rows


def paint_image(canvas: skia.Canvas, box: skia.Rect, row: Row,
                pictures: Pictures, agent) -> None:
    image = pictures.get(agent, row.label)
    if not image:
        return
    iw, ih = image.width(), image.height()
    h = box.height() - 2 * IMAGE_PAD
    w = iw * h / ih
    dst = skia.Rect.MakeXYWH(box.left(), box.top() + IMAGE_PAD, w, h)
    canvas.drawImageRect(image, dst, skia.SamplingOptions(
        skia.FilterMode.kLinear, skia.MipmapMode.kLinear))
    edge = skia.Paint(AntiAlias=False)
    edge.setStyle(skia.Paint.kStroke_Style)
    edge.setStrokeWidth(1)
    edge.setColor(theme.color(theme.BASE_MUTED, 0.8))
    canvas.drawRect(dst.makeOutset(0.5, 0.5), edge)


def paint_file(canvas: skia.Canvas, box: skia.Rect, row: Row, agent,
               accent: str) -> None:
    """↓ name   12 KB   caption"""
    info = agent.shown.get(row.label) or {"name": row.label}
    base = text.baseline_in(box, BODY_SIZE)
    text.draw(canvas, FILE_GLYPH, box.left(), base, BODY_SIZE,
              theme.color(accent), bold=True)
    x = box.left() + text.advance(BODY_SIZE) * 2
    right = box.right()
    name = text.ellipsize(str(info.get("name") or ""), BODY_SIZE,
                          (right - x) * 0.6)
    text.draw(canvas, name, x, base, BODY_SIZE,
              theme.color(theme.HUD_FG_BRIGHT), bold=True)
    x += text.measure(name + "   ", BODY_SIZE, bold=True)
    small = text.baseline_in(box, ACT_SIZE)
    size = size_text(info.get("size"))
    if size:
        text.draw(canvas, size, x, small, ACT_SIZE,
                  theme.color(theme.BASE_MUTED))
        x += text.measure(size + "   ", ACT_SIZE)
    caption = str(info.get("caption") or "")
    if caption and x < right:
        text.draw(canvas, text.ellipsize(" ".join(caption.split()), ACT_SIZE,
                                         right - x),
                  x, small, ACT_SIZE, theme.color(theme.HUD_FG))


def paint_waiting(canvas: skia.Canvas, box: skia.Rect, row: Row,
                  agent) -> None:
    info = agent.shown.get(row.label) or {"name": row.label}
    text.draw(canvas, text.ellipsize(f"{info.get('name')}: on its way",
                                     ACT_SIZE, box.width()),
              box.left(), text.baseline_in(box, ACT_SIZE), ACT_SIZE,
              theme.color(theme.BASE_MUTED))
