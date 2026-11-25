"""Command line for the crawler agent: ``run``, ``review`` and ``status``."""

from __future__ import annotations

import sys

from crawler.cli import main

if __name__ == "__main__":
    sys.exit(main())
