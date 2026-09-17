"""The chosen configuration, rendered from the real widget classes.

    PYTHONPATH=../.. python render_after.py
"""
import skia

from common import BAR_H, PAD, bar_strip, render_widget, sheet, tw
from indigoshell import theme
from indigoshell.widgets.base import Size
from indigoshell.widgets.layout import Brackets, Row
from indigoshell.widgets.media import Media
from indigoshell.widgets.meters import StatMeter
from indigoshell.widgets.network import Network
from indigoshell.widgets.volume import Volume
from render_widgets import BANDS, IP, NAME, TITLE, VOL

cpu = StatMeter("CPU", lambda: 42.0)
ram = StatMeter("RAM", lambda: 63.0, bright_color=theme.VIOLET_BRIGHT,
                dim_color=theme.VIOLET_DIM, label_color=theme.VIOLET,
                value_color=theme.VIOLET_BRIGHT)
temp = StatMeter("TEMP", lambda: 71.0, value_format="{:.0f}°",
                 dim_color=theme.CYAN_DIM, label_color=theme.CYAN_DIM,
                 to_pct=lambda t: (t - 30) * (100 / 60),
                 gradient=((0.0, theme.CYAN_BRIGHT), (0.5, theme.YELLOW_BRIGHT),
                           (0.8, theme.ERROR)))
gauges = Brackets(Row([cpu, ram, temp], spacing=theme.SPACING_XL))

media = Media(player="x", max_chars=26, size=theme.FONT_SIZE_LG, baseline_shift=1.0,
              show_cava_bg=True, beat_pulse=True, plate=True,
              plate_corners=("top-right",), plate_inset_y=3)
media._title, media._title_w = TITLE, tw(TITLE, theme.FONT_SIZE_LG, True)
media._bands, media._peak, media._active = BANDS, float(max(BANDS)), True

idle = Media(player="x", max_chars=26, size=theme.FONT_SIZE_LG, baseline_shift=1.0,
             show_cava_bg=True, beat_pulse=True, plate=True,
             plate_corners=("top-right",), plate_inset_y=3)

net = Network()
net._apply(NAME, IP)
net._f_up, net._f_down, net._t = 0.0, 2.0, 0.1     # up idle, down blinking (on)
net_dim = Network()
net_dim._apply(NAME, IP)
net_dim._f_up, net_dim._f_down, net_dim._t = 1.0, 2.0, 0.3   # up on, down off
net_down = Network()
net_down._apply("--", "")

vol = Volume(cap=True, cap_color=theme.YELLOW_BRIGHT)
vol._percent = float(VOL)

sheet([("gauges|one bracket frame", render_widget(gauges)),
       ("media|chip plate, no tag", render_widget(media)),
       ("media idle|same chip, narrower", render_widget(idle)),
       ("volume|yellow cap line", render_widget(vol)),
       ("network|up idle+down on / up on+down off / no link", render_widget(net)),
       ("", render_widget(net_dim)),
       ("", render_widget(net_down))],
      "after.png", title="Chosen configuration, real widgets")
