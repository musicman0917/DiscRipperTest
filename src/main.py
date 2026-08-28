"""Entry point: `python -m src.main` or the packaged .exe launches here."""

from __future__ import annotations

import sys


def run() -> None:
    from .gui import main

    main()


if __name__ == "__main__":
    sys.exit(run() or 0)
