"""The ``catalog`` command group: parts-catalog extension workflows.

Registered in phase 1; inference and validation land with phase 7 of
the 0.6 roadmap.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, stub_handler

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``catalog`` operations and handler."""
    operations = parser.add_subparsers(
        dest="catalog_command",
        metavar="OPERATION",
        required=True,
    )
    infer = operations.add_parser(
        "infer",
        help="infer draft geometry and estimates for an unsupported part",
    )
    infer.add_argument("part_id", help="LDraw part number, e.g. 3001")
    add_json_option(infer)
    infer.set_defaults(
        handler=stub_handler("catalog infer", "phase 7 (catalog extension)"),
        command_name="catalog infer",
    )
    validate = operations.add_parser(
        "validate",
        help="validate a catalog extension or support bundle",
    )
    validate.add_argument(
        "path",
        type=Path,
        help="extension JSON or -legolization-support directory",
    )
    add_json_option(validate)
    validate.set_defaults(
        handler=stub_handler("catalog validate", "phase 7 (catalog extension)"),
        command_name="catalog validate",
    )


__all__ = ["configure"]
