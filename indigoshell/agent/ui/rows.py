"""Text as rows on the monospace grid.

Every line the overlay draws is a `Row`: its source string and the slice
of it the row shows, which is what wrapping, selection and copy all work
on. Markdown replies, code blocks and tool output are all built from
these and painted by `paint_line`."""

from dataclasses import dataclass, field

import skia

from ... import markdown, text, theme
from ..activity import Activity
from .style import ACT_SIZE, AGENT_ACCENT, BODY_SIZE, CODE_FG, LINK_FG, SELECT_BG, font_for, line_h


def cols_for(width: float, size: float) -> int:
    return max(8, int(width // text.advance(size)))


def wrap_spans(value: str, cols: int) -> list[tuple[int, int]]:
    """(start, end) of each wrapped line of `value`, by character count
    on the monospace grid. Breaks at the last space that fits, or mid-word
    when a word is longer than a line; newlines are hard breaks."""
    out: list[tuple[int, int]] = []
    pos = 0
    for line in value.split("\n"):
        i, n = pos, pos + len(line)
        if i == n:
            out.append((i, i))
        while i < n:
            if n - i <= cols:
                out.append((i, n))
                break
            cut = value.rfind(" ", i, i + cols + 1)
            if cut <= i:
                out.append((i, i + cols))
                i += cols
            else:
                out.append((i, cut))
                i = cut + 1
        pos = n + 1
    return out


def hard_spans(value: str, cols: int) -> list[tuple[int, int]]:
    """Like `wrap_spans`, but breaks at exactly `cols` — for code and output,
    where a space is not a better place to break than any other."""
    out: list[tuple[int, int]] = []
    pos = 0
    for line in value.split("\n"):
        n = pos + len(line)
        if pos == n:
            out.append((pos, pos))
        for i in range(pos, n, cols):
            out.append((i, min(n, i + cols)))
        pos = n + 1
    return out


# ── rows ────────────────────────────────────────────────────────────────
@dataclass(eq=False)
class Row:
    """One line of a plate. Text rows carry their source string and the
    slice of it they show, which is what selection and copy work on."""

    kind: str                  # line act think more gap hr keys header ...
    h: float
    x: float = 0.0             # text indent from the plate's text edge, px
    size: float = BODY_SIZE
    color: str = theme.HUD_FG_BRIGHT
    key: str = ""              # source id, for selection
    src: str = ""              # source text
    start: int = 0
    end: int = 0
    spans: list = field(default_factory=list)
    bold: bool = False
    bar: str | None = None     # colour of a rule down the left edge
    bullet: str = ""
    bullet_x: float = 0.0
    act: Activity | None = None
    label: str = ""            # for non-source rows (more, notes)
    cursor: bool = False

    @property
    def selectable(self) -> bool:
        return bool(self.key)


# ── text rows ───────────────────────────────────────────────────────────
def text_rows(key: str, value: str, size: float, w: float,
               color: str, x: float = 0.0, spans=(), bold=False,
               bar: str | None = None) -> list[Row]:
    value = text.pad_wide(value)     # unchanged if already padded
    cols = cols_for(w - x, size)
    return [Row("line", line_h(size) + 3, x=x, size=size, color=color,
                key=key, src=value, start=s, end=e, spans=list(spans),
                bold=bold, bar=bar)
            for s, e in wrap_spans(value, cols)]

def code_rows(key: str, value: str, w: float, bar: str,
               color: str = CODE_FG) -> list[Row]:
    value = text.pad_wide(value)
    x = text.advance(ACT_SIZE) * 1.5
    cols = cols_for(w - x, ACT_SIZE)
    return [Row("line", line_h(ACT_SIZE) + 3, x=x, size=ACT_SIZE,
                color=color, key=key, src=value, start=s, end=e, bar=bar)
            for s, e in hard_spans(value, cols)]


def markdown_rows(message: str, w: float, prefix: str = "m") -> list[Row]:
    """Rows for a Markdown reply, `w` px wide, selectable by block.
    Wide characters get their second cell before parsing, so the
    spans' offsets already count it. Each block's key is `prefix` and
    its index: rows that show reply text keep an "m" prefix, which is
    how the reveal and the plate's painter know them."""
    out: list[Row] = []
    cell = text.advance(BODY_SIZE)
    for i, b in enumerate(markdown.parse(text.pad_wide(message))):
        key = f"{prefix}{i}"
        if b.kind == "blank":
            out.append(Row("gap", 7))
        elif b.kind == "rule":
            out.append(Row("hr", 11))
        elif b.kind == "code":
            out += code_rows(key, b.text, w, theme.YELLOW_DIM)
        elif b.kind == "h":
            if out and out[-1].kind != "gap":
                out.append(Row("gap", 4))
            out += text_rows(key, b.text, BODY_SIZE, w,
                                   AGENT_ACCENT, spans=b.spans, bold=True)
        elif b.kind == "li":
            indent = b.level * 2 * cell
            bw = (len(b.bullet) + 1) * cell
            rows = text_rows(key, b.text, BODY_SIZE, w,
                                   theme.HUD_FG_BRIGHT,
                                   x=indent + bw, spans=b.spans)
            if rows:
                rows[0].bullet, rows[0].bullet_x = b.bullet, indent
            out += rows
        elif b.kind == "quote":
            out += text_rows(key, b.text, BODY_SIZE, w,
                                   theme.HUD_FG, x=cell * 1.5,
                                   spans=b.spans, bar=theme.BASE_MUTED)
        else:                       # p, table
            out += text_rows(key, b.text, BODY_SIZE, w,
                                   theme.HUD_FG_BRIGHT, spans=b.spans)
    return out


def highlight(canvas, box, row: Row, sel, visible: int) -> None:
    cell = text.advance(row.size)
    s = sel[0] - row.start
    e = min(sel[1] - row.start, visible)
    if e <= s:
        return
    p = skia.Paint(AntiAlias=False)
    p.setColor(theme.color(*SELECT_BG))
    x = box.left() + row.x
    canvas.drawRect(skia.Rect.MakeLTRB(x + s * cell, box.top() + 1,
                                       x + e * cell, box.bottom() - 1), p)


def _draw_cells(canvas, seg: str, x: float, base: float, cell: float,
                size: float, bold: bool, f, paint) -> None:
    """A run with characters the theme font may lack, one cell each: a
    fallback font for those, fitted to their cells (two for a wide one,
    whose PAD then draws nothing)."""
    for i, ch in enumerate(seg):
        if ch == text.PAD:
            continue
        cx = x + i * cell
        if text.has_glyph(ch, bold):
            canvas.drawString(ch, cx, base, f, paint)
            continue
        cells = 2 if text.is_wide(ch) else 1
        fb = text.grid_font(ch, size, cells * cell)
        if fb is None:
            continue
        # Centred in its cells: a glyph narrower than them sits mid-way.
        w = fb.measureText(ch)
        canvas.drawString(ch, cx + max(0.0, (cells * cell - w) / 2), base,
                          fb, paint)


def paint_line(canvas, box, row: Row, sel, accent) -> None:
    cell = text.advance(row.size)
    x0 = box.left() + row.x
    base = text.baseline_in(box, row.size)
    if row.bar is not None:
        p = skia.Paint(AntiAlias=False)
        p.setColor(theme.color(row.bar, 0.8))
        canvas.drawRect(skia.Rect.MakeXYWH(x0 - text.advance(ACT_SIZE) * 1.5
                                           + 1, box.top() + 1, 2,
                                           box.height() - 2), p)
    if row.bullet:
        text.draw(canvas, row.bullet, box.left() + row.bullet_x, base,
                  row.size, theme.color(accent), bold=True)
    if sel is not None:
        highlight(canvas, box, row, sel, row.end - row.start)

    # Runs of uniform style across the row's slice of its source.
    cuts = {row.start, row.end}
    for sp in row.spans:
        if sp.end > row.start and sp.start < row.end:
            cuts.add(max(row.start, sp.start))
            cuts.add(min(row.end, sp.end))
    edges = sorted(cuts)
    for a, b in zip(edges, edges[1:]):
        seg = row.src[a:b]
        if not seg:
            continue
        styles = {sp.style for sp in row.spans
                  if sp.start <= a and b <= sp.end}
        x = x0 + (a - row.start) * cell
        bold = row.bold or "b" in styles or "bi" in styles
        italic = "i" in styles or "bi" in styles
        color = row.color
        if "code" in styles:
            color = CODE_FG
            bg = skia.Paint(AntiAlias=False)
            bg.setColor(theme.color(theme.YELLOW_FAINT, 0.55))
            canvas.drawRect(skia.Rect.MakeXYWH(
                x, box.top() + 2, len(seg) * cell, box.height() - 4), bg)
        if "link" in styles:
            color = LINK_FG
        paint = skia.Paint(AntiAlias=True)
        paint.setColor(theme.color(color))
        # Glyph by glyph on the grid for skewed italics, so they keep
        # the columns selection is measured in.
        f = font_for(row.size, bold, italic)
        if not seg.isascii():
            _draw_cells(canvas, seg, x, base, cell, row.size, bold, f, paint)
        elif italic:
            for i, ch in enumerate(seg):
                canvas.drawString(ch, x + i * cell, base, f, paint)
        else:
            canvas.drawString(seg, x, base, f, paint)
        if "link" in styles or "s" in styles:
            line = skia.Paint(AntiAlias=False)
            line.setColor(theme.color(color, 0.7))
            y = base + 2 if "link" in styles else base - row.size * 0.3
            canvas.drawRect(skia.Rect.MakeXYWH(x, y, len(seg) * cell, 1),
                            line)
    if row.cursor:
        p = skia.Paint(AntiAlias=False)
        p.setColor(theme.color(accent))
        canvas.drawRect(skia.Rect.MakeXYWH(
            x0 + (row.end - row.start) * cell + 2, box.top() + 3, 8,
            box.height() - 6), p)
