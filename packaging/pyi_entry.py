"""PyInstaller entry point: equivalent to `python -m longrebuttal`."""
import sys

from longrebuttal.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
