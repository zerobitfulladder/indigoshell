"""python -m flux [url ...]: the browser (flux/qt/__main__.py has the
options)."""

import sys

from flux.qt.__main__ import main

sys.exit(main(sys.argv[1:], prog="python -m flux"))
