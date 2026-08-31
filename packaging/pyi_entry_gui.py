"""PyInstaller entry point for the windowed build: defaults to the native shell."""
import sys

from bitrebuttal.__main__ import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["gui"]
    sys.exit(main(argv))
