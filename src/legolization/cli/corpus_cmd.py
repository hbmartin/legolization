"""The ``corpus`` command group: evaluation corpus workflows.

Registered in phase 1; the packaged corpus operations land with
phase 7 of the 0.6 roadmap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, stub_handler

if TYPE_CHECKING:
    import argparse

_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("list", "list corpus models and their availability"),
    ("generate", "generate synthetic corpus inputs"),
    ("download", "download pinned corpus meshes"),
    ("verify", "verify corpus input hashes"),
    ("collect", "run placement strategies and collect candidate artifacts"),
    ("assemble", "assemble collected artifacts into a scorecard"),
    ("evaluate", "collect and assemble across all available inputs"),
)


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``corpus`` operations and handler."""
    operations = parser.add_subparsers(
        dest="corpus_command",
        metavar="OPERATION",
        required=True,
    )
    for name, help_text in _OPERATIONS:
        operation = operations.add_parser(name, help=help_text)
        add_json_option(operation)
        operation.set_defaults(
            handler=stub_handler(f"corpus {name}", "phase 7 (corpus commands)"),
            command_name=f"corpus {name}",
        )


__all__ = ["configure"]
