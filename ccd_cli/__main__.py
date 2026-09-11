"""`python3 -m ccd_cli` — the same entry point as the `ccd` console script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
