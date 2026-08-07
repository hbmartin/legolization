"""The ``parts`` command group: managed official LDraw parts library.

Registered in phase 1; the managed library sync lands with phase 6 of
the 0.6 roadmap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, stub_handler

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``parts`` operations and handler."""
    operations = parser.add_subparsers(
        dest="parts_command",
        metavar="OPERATION",
        required=True,
    )
    sync = operations.add_parser(
        "sync",
        help="install or update the managed official LDraw parts library",
    )
    sync.add_argument(
        "--force",
        action="store_true",
        help="re-download even when the weekly check is fresh",
    )
    add_json_option(sync)
    sync.set_defaults(
        handler=stub_handler("parts sync", "phase 6 (managed parts library)"),
        command_name="parts sync",
    )


__all__ = ["configure"]
