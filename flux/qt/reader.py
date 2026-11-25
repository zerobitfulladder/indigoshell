"""Reading a page for the agent: the script world, and pictures of the view.

The agent's reads run reader.js in an isolated script world of their
own (`WORLD`): the page shares the DOM with it but none of its
variables, so the page's scripts can neither see the refs it hands out
nor tamper with the reading. The acting requests (control.py) use the
same world and file: reader.js's acting part finds where a ref is on
screen, focuses a field and sets what no key can, with the refs the
reads handed out. A frame's document is read the same way, in the same
world of that frame (frames.py), its refs named after the frame.

The script worlds of a page, by number:
    0  the MainWorld: the page's own; identity.py's navigator script
    1  the ApplicationWorld: Flux's own (find's selection clearing)
    2  the scrollbars' sheet (profile.py)
    3  the agent's reading and acting (WORLD, here)
    4  the agent's own scripts, behind their guard (script.py)

Two things QtWebEngine (PySide6 6.12, and as documented for 6.8) does
that shape the calls:

- `runJavaScript` gives an array or object back as an empty string, so
  every call answers JSON text: {"ok": true, "value": ...} or
  {"ok": false, "error": str} (`script`, `answer`).
- it never waits for a promise, so what takes time in the page (two
  animation frames, for a fresh screenshot) is left in the world and
  polled.

`page.selectedText()` is not used: it hands over the text selected in
a password field in the clear (measured on 6.12). The selection is read
in the world, where a password field's reads as bullets and is dropped.

Pictures: `QWidget.grab()` of the tab's view, the frame last shown,
which works for a window nobody looks at (a hidden compositor's, in the
tests) as on screen, and for a window on another workspace. Scaled so the long side is at most
protocol.SHOT_MAX_SIDE, sent as PNG in base64.
"""

import base64
import json
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QImage

from flux import protocol

# The agent's isolated world: not the page's (MainWorld, 0), not Flux's
# own (ApplicationWorld, 1), not the first one for user scripts (2).
WORLD = 3

SOURCE = Path(__file__).with_name("reader.js").read_text(encoding="utf-8")


def script(fn: str, *args, frame: str = "") -> str:
    """JS that defines the reader if this document has none yet, runs
    its `fn(*args)` and answers JSON text. `frame`: the name ("f2") a
    frame's document takes if it has none yet (frames.py); its refs carry
    the name it has."""
    call = f"window.__flux.{fn}({', '.join(json.dumps(a) for a in args)})"
    return (SOURCE + "\n(() => { try { window.__flux.frameName = window.__flux.frameName || "
            + json.dumps(frame) + "; return JSON.stringify({ok: true, value: " + call
            + "}); } catch (e) { return JSON.stringify({ok: false, error: "
            "String(e && e.message || e)}); } })()")


def expression(code: str) -> str:
    """JS for a bare expression in the world (after the reader is
    defined), answering JSON text like `script`."""
    return (SOURCE + "\n(() => { try { return JSON.stringify({ok: true, value: (" + code
            + ")}); } catch (e) { return JSON.stringify({ok: false, error: "
            "String(e && e.message || e)}); } })()")


class ScriptError(Exception):
    """The page threw, or answered something that is no answer."""


def answer(raw):
    """What `script` answered, as Python; raises ScriptError."""
    if not isinstance(raw, str) or not raw:
        raise ScriptError("the page gave no answer (it may have navigated away)")
    try:
        msg = json.loads(raw)
    except ValueError:
        raise ScriptError("the page's answer is not JSON") from None
    if not isinstance(msg, dict) or not msg.get("ok"):
        raise ScriptError(str(msg.get("error") if isinstance(msg, dict) else msg))
    return msg.get("value")


# ── pictures ────────────────────────────────────────────────────────────
def grab(view) -> QImage:
    """The view as it was last painted, in device pixels."""
    return view.grab().toImage()


def crop(image: QImage, rect: dict, css_width: float, css_height: float) -> QImage | None:
    """The part of `image` (the whole viewport) that `rect` covers, a
    rect in CSS pixels of a viewport `css_width` x `css_height`."""
    if css_width <= 0 or css_height <= 0:
        return None
    sx, sy = image.width() / css_width, image.height() / css_height
    area = QRect(round(rect["x"] * sx), round(rect["y"] * sy),
                 round(rect["w"] * sx), round(rect["h"] * sy)).intersected(image.rect())
    return image.copy(area) if area.width() > 1 and area.height() > 1 else None


def from_data_url(url: str) -> QImage | None:
    """A `data:image/...;base64,` URL (reader.js `pixels`) as an image."""
    head, _, data = url.partition(",")
    if not head.startswith("data:image/") or not head.endswith(";base64"):
        return None
    image = QImage()
    try:
        image.loadFromData(base64.b64decode(data))
    except ValueError:
        return None
    return None if image.isNull() else image


def png(image: QImage, max_side: int = protocol.SHOT_MAX_SIDE) -> dict:
    """`image` as a PNG in base64, scaled down to `max_side` on its long
    side: {"png", "width", "height", "source_width", "source_height"}."""
    w, h = image.width(), image.height()
    shown = image
    if max(w, h) > max_side:
        shown = image.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    shown.save(buffer, "PNG")
    buffer.close()
    return {"png": base64.b64encode(bytes(data)).decode("ascii"),
            "width": shown.width(), "height": shown.height(),
            "source_width": w, "source_height": h}
