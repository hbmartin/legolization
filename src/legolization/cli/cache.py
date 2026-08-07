"""The ``cache`` command for the persistent template cache."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from legolization.cli.common import (
    add_config_options,
    add_json_option,
    resolve_config,
)
from legolization.cli.envelope import ResultEnvelope
from legolization.cli.exit_codes import COMPLETE

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``cache`` operations and handler."""
    add_config_options(parser)
    operations = parser.add_subparsers(
        dest="cache_command",
        metavar="OPERATION",
        required=True,
    )
    inspect = operations.add_parser("inspect", help="list cache entries")
    add_json_option(inspect)
    inspect.set_defaults(handler=_run, command_name="cache")
    clear = operations.add_parser("clear", help="remove cache entries")
    target = clear.add_mutually_exclusive_group(required=True)
    target.add_argument("--key")
    target.add_argument("--all", action="store_true")
    add_json_option(clear)
    clear.set_defaults(handler=_run, command_name="cache")


def _run(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.template_cache import TemplateCache  # noqa: PLC0415

    config = resolve_config(args)
    cache = (
        TemplateCache(root=config.cache.path)
        if config.cache.path is not None
        else TemplateCache.default()
    )
    data: dict[str, object]
    match args.cache_command:
        case "inspect":
            entries = [
                {
                    "key": entry.key,
                    "payload_sha256": entry.payload_sha256,
                    "size_bytes": entry.size_bytes,
                }
                for entry in cache.inspect()
            ]
            if not args.json:
                print(json.dumps(entries, indent=2, sort_keys=True))
            data = {"entries": entries}
        case _:
            removed = cache.clear(key=args.key, all_entries=args.all)
            if not args.json:
                print(f"removed {removed} cache entr{'y' if removed == 1 else 'ies'}")
            data = {"removed": removed}
    return ResultEnvelope(command="cache", exit_code=COMPLETE, data=data)


__all__ = ["configure"]
