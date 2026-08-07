"""The ``validate`` command for assembly manifests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option, error_line, require_file

if TYPE_CHECKING:
    import argparse
from legolization.cli.envelope import ErrorRecord, ResultEnvelope
from legolization.cli.exit_codes import (
    COMPLETE,
    OPERATIONAL_ERROR,
    PARTIAL,
    UNBUILDABLE,
)


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``validate`` arguments and handler."""
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--against", type=Path)
    add_json_option(parser)
    parser.set_defaults(handler=_run, command_name="validate")


def _run(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.manifest import (  # noqa: PLC0415
        read_manifest,
        validate_manifest,
    )

    require_file(args.manifest, label="manifest")
    if args.against is not None:
        require_file(args.against, label="comparison model")
    manifest = read_manifest(args.manifest)
    if errors := validate_manifest(manifest, against=args.against):
        for message in errors:
            error_line(message)
        return ResultEnvelope(
            command="validate",
            exit_code=OPERATIONAL_ERROR,
            error=ErrorRecord(
                type="ManifestValidationError",
                message=f"{len(errors)} manifest validation failure(s)",
                detail={"errors": list(errors)},
            ),
        )
    if not args.json:
        print(f"valid {manifest.schema} version {manifest.version}")
    buildable = (
        manifest.stability.get("stable") is True
        and manifest.stability.get("component_count") == 1
        and manifest.stability.get("floating_count") == 0
    )
    match manifest.status:
        case "partial":
            exit_code = PARTIAL
        case "error":
            exit_code = OPERATIONAL_ERROR
        case _:
            exit_code = COMPLETE if buildable else UNBUILDABLE
    return ResultEnvelope(
        command="validate",
        exit_code=exit_code,
        data={
            "schema": manifest.schema,
            "version": manifest.version,
            "status": manifest.status,
            "buildable": buildable,
        },
    )


__all__ = ["configure"]
