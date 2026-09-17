"""Entry point for indigoshell2 (mirrors main.py for v1).

Run by absolute path — `python /path/to/main2.py` puts this file's
directory on sys.path, so `indigoshell2` imports from any working
directory. That's what lets the window manager spawn it without a
console script or a PYTHONPATH.
"""

from indigoshell2.app import main

if __name__ == "__main__":
    main()
