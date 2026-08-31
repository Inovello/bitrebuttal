"""PyInstaller entry point: equivalent to `python -m bitrebuttal`."""
import sys

from bitrebuttal.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
