#!/usr/bin/env python3
"""Start the brainrot reel bot:  python brainrot.py   (see README.md for all options)."""

import sys

if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required.")

from brainrot_bot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
