"""The ``parts`` command group: managed official LDraw parts library."""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ResultEnvelope
from legolization.cli.exit_codes import COMPLETE

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
    sync.set_defaults(handler=_run_sync, command_name="parts sync")


def _run_sync(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.parts_library import sync  # noqa: PLC0415

    result = sync(force=args.force)
    if not args.json:
        location = result.ldraw_dir if result.ldraw_dir is not None else result.root
        print(f"parts library {result.status}: {location}")
        for warning in result.warnings:
            print(f"  warning: {warning}")
    return ResultEnvelope(
        command="parts sync",
        exit_code=COMPLETE,
        warnings=result.warnings,
        data=result.to_dict(),
    )


__all__ = ["configure"]
