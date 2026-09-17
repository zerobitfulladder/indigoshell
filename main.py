"""Entry point for indigoshell.

Run by absolute path — `python /path/to/main.py` puts this file's
directory on sys.path, so `indigoshell` imports from any working
directory. That's what lets the window manager spawn it without a
console script or a PYTHONPATH.
"""

from indigoshell.app import main

if __name__ == "__main__":
    main()
