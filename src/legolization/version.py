"""Package version reporting without importing heavy pipeline modules."""

from __future__ import annotations

import importlib.metadata

_FALLBACK_VERSION = "0+unknown"


def package_version() -> str:
    """Return the installed distribution version.

    Falls back to a sentinel when the package metadata is unavailable,
    such as when the sources are imported without an installation.
    """
    try:
        return importlib.metadata.version("legolization")
    except importlib.metadata.PackageNotFoundError:
        return _FALLBACK_VERSION


__version__: str = package_version()
