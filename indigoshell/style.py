from dataclasses import dataclass

from . import theme


def css_color(c: str) -> str:
    """Translate #RRGGBBAA hex (not natively supported by GTK 3 CSS) to rgba()."""
    if c.startswith("#") and len(c) == 9:
        r = int(c[1:3], 16)
        g = int(c[3:5], 16)
        b = int(c[5:7], 16)
        a = int(c[7:9], 16) / 255
        return f"rgba({r}, {g}, {b}, {a:.3f})"
    return c


@dataclass
class Style:
    bg: str | None = None
    fg: str | None = None
    font_size: int | None = None
    bold: bool = False
    italic: bool = False
    radius: int | None = None
    padding: int | None = None
    border: str | None = None  # CSS shorthand, e.g. "1px solid #39ff14"


def style_to_css(name: str, style: Style, state_class: str | None = None) -> str:
    suffix = f".{state_class}" if state_class else ""
    return _style_rules(f"#{name}{suffix}", style)


def child_style_to_css(name: str, child_class: str, style: Style) -> str:
    return _style_rules(f"#{name} .{child_class}", style)


def _style_rules(selector: str, style: Style) -> str:
    container: list[str] = []
    text: list[str] = []
    if style.bg is not None:
        container.append(f"background-color: {css_color(style.bg)};")
    if style.radius is not None:
        container.append(f"border-radius: {style.radius}px;")
    if style.padding is not None:
        container.append(f"padding: {style.padding}px;")
    if style.border is not None:
        container.append(f"border: {style.border};")
    if style.fg is not None:
        text.append(f"color: {css_color(style.fg)};")
    if style.font_size is not None:
        text.append(f"font-size: {style.font_size}px;")
    if style.bold:
        text.append("font-weight: bold;")
    if style.italic:
        text.append("font-style: italic;")

    out = ""
    if container:
        out += f"{selector} {{ {' '.join(container)} }}\n"
    if text:
        out += f"{selector}, {selector} label {{ {' '.join(text)} }}\n"
    return out


def build_css(
    font: str | None = None,
    font_size: int | None = None,
    bar_bg: str | None = None,
    bar_radius: int = 0,
) -> str:
    font = font or theme.FONT
    font_size = font_size or theme.FONT_SIZE
    bar_bg = css_color(bar_bg or theme.BG)

    return f"""
window {{
    background-color: {bar_bg};
    border-radius: {bar_radius}px;
}}
label {{
    font-family: {font};
    font-size: {font_size}px;
    color: {theme.FG};
}}
"""
