"""The ``instructions`` command group: build-instruction auditing.

Registered in phase 1; auditing lands with phase 6 of the 0.6
roadmap.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, stub_handler

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``instructions`` operations and handler."""
    operations = parser.add_subparsers(
        dest="instructions_command",
        metavar="OPERATION",
        required=True,
    )
    audit = operations.add_parser(
        "audit",
        help="audit step ordering, stability, and insertion pressure",
    )
    audit.add_argument(
        "input",
        type=Path,
        help="step-annotated .ldr/.mpd model or bundle directory",
    )
    add_json_option(audit)
    audit.set_defaults(
        handler=stub_handler("instructions audit", "phase 6 (instruction auditing)"),
        command_name="instructions audit",
    )


__all__ = ["configure"]
