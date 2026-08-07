"""The ``bundle`` command: complete noninteractive generation pipeline.

Registered in phase 1; the portable bundle orchestration lands with
phases 2-4 of the 0.6 roadmap.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import (
    add_catalog_options,
    add_config_options,
    add_json_option,
    stub_handler,
)

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``bundle`` arguments and handler."""
    parser.add_argument(
        "input",
        type=Path,
        help="input .vox/.npy/.obj/.stl/.ply model or .ldr/.mpd assembly",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="bundle directory (default: operation-specific sibling)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="force a fresh numeric-sibling run instead of resuming",
    )
    add_config_options(parser)
    add_catalog_options(parser)
    add_json_option(parser)
    parser.set_defaults(
        handler=stub_handler("bundle", "phase 2 (portable bundle foundation)"),
        command_name="bundle",
    )


__all__ = ["configure"]
