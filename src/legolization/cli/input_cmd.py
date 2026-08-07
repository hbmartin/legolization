"""The ``input`` command group: source-model inspection and normalization.

Registered in phase 1; inspection lands with phase 3 of the 0.6
roadmap.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import (
    add_config_options,
    add_json_option,
    stub_handler,
)

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``input`` operations and handler."""
    operations = parser.add_subparsers(
        dest="input_command",
        metavar="OPERATION",
        required=True,
    )
    inspect = operations.add_parser(
        "inspect",
        help="inspect a native source model and recommend generation settings",
    )
    inspect.add_argument(
        "input",
        type=Path,
        help="input .vox/.npy/.obj/.stl/.ply model",
    )
    add_config_options(inspect)
    add_json_option(inspect)
    inspect.set_defaults(
        handler=stub_handler("input inspect", "phase 3 (input inspection)"),
        command_name="input inspect",
    )


__all__ = ["configure"]
