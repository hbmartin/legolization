"""Console entry point delegating to the explicit command hierarchy.

The legacy bare ``legolization INPUT`` parser was removed in 0.6; the
full command surface lives in :mod:`legolization.cli`.
"""

from __future__ import annotations

import sys

from legolization.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())
