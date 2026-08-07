"""The ``model`` command group: operations on generated models.

Registered in phase 1; rendering lands with phase 6 of the 0.6
roadmap.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, stub_handler

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``model`` operations and handler."""
    operations = parser.add_subparsers(
        dest="model_command",
        metavar="OPERATION",
        required=True,
    )
    render = operations.add_parser(
        "render",
        help="render a model or bundle to PNG images from requested views",
    )
    render.add_argument(
        "input",
        type=Path,
        help="input .ldr/.mpd model or bundle directory",
    )
    add_json_option(render)
    render.set_defaults(
        handler=stub_handler("model render", "phase 6 (rendering)"),
        command_name="model render",
    )


__all__ = ["configure"]
