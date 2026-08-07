"""The ``catalog`` command group: parts-catalog extension workflows.

``catalog infer`` drafts geometry, estimates, and cited sources for one
LDraw part into a ``<key>-legolization-support`` bundle; ``catalog
validate`` reruns the five activation gates over a support bundle or a
bare extension document.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE, PARTIAL

if TYPE_CHECKING:
    import argparse

    from legolization.catalog_infer.validate import GateResult


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``catalog`` operations and handlers."""
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
    infer.add_argument(
        "--key",
        default=None,
        help="catalog key for the drafted part (default: part_<id>)",
    )
    infer.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="parent directory for the support bundle (default: cwd)",
    )
    infer.add_argument(
        "--offline",
        action="store_true",
        help="skip keyed/network sources; the local dump is still consulted",
    )
    add_json_option(infer)
    infer.set_defaults(handler=_run_infer, command_name="catalog infer")
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
    validate.set_defaults(handler=_run_validate, command_name="catalog validate")


def _default_key(part_id: str) -> str:
    return f"part_{part_id}"


def _gate_lines(gates: tuple[GateResult, ...]) -> list[str]:
    return [f"  {gate.gate}: {gate.status} - {gate.detail}" for gate in gates]


def _run_infer(args: argparse.Namespace) -> ResultEnvelope:
    from legolization import catalog_infer  # noqa: PLC0415
    from legolization.catalog_infer import bundle as support  # noqa: PLC0415
    from legolization.catalog_infer.geometry import normalize_part_id  # noqa: PLC0415
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415

    part_id = normalize_part_id(args.part_id)
    key = args.key or _default_key(part_id)
    geometry = catalog_infer.infer_geometry(part_id)
    sources = catalog_infer.lookup_sources(part_id, offline=args.offline)
    estimate = catalog_infer.draft_mass_estimate(
        part_key=key,
        cell_count=geometry.cell_count,
        sources=sources,
    )
    directory = support.resolve_support_dir(
        key,
        output=args.output,
        identity_key=support.infer_identity(
            part_id,
            key,
            input_sha256(geometry.source_path),
        ).key(),
    )
    bundle = support.write_support_bundle(
        directory,
        key=key,
        geometry=geometry,
        estimate=estimate,
        sources=sources,
        offline=args.offline,
    )
    complete = bundle.validated and estimate.measured and geometry.confident
    exit_code = COMPLETE if complete else PARTIAL
    if not args.json:
        print(f"support bundle: {bundle.directory}")
        print(
            f"mass draft: {estimate.mass_g:g} g ({estimate.method}); "
            f"geometry {'confident' if geometry.confident else 'draft-only'}"
        )
        print("\n".join(_gate_lines(bundle.gates)))
        for warning in bundle.warnings:
            print(f"  warning: {warning}")
    return ResultEnvelope(
        command="catalog infer",
        exit_code=exit_code,
        artifacts=tuple(
            ArtifactRecord(path=str(bundle.directory / path), kind=kind)
            for path, kind in bundle.artifacts
        ),
        warnings=bundle.warnings,
        data={
            "part_id": part_id,
            "key": key,
            "bundle_dir": str(bundle.directory),
            "validated": bundle.validated,
            "confident_geometry": geometry.confident,
            "gates": [gate.to_dict() for gate in bundle.gates],
            "estimate": {
                "mass_g": estimate.mass_g,
                "method": estimate.method,
                "volumetric_mass_g": estimate.volumetric_mass_g,
            },
            "sources": [lookup.to_dict() for lookup in sources.lookups],
        },
    )


def _run_validate(args: argparse.Namespace) -> ResultEnvelope:
    from datetime import UTC, datetime  # noqa: PLC0415

    from legolization.catalog import (  # noqa: PLC0415
        SUPPORT_ESTIMATES_FILENAME,
        SUPPORT_EXTENSION_FILENAME,
        SUPPORT_VALIDATION_FILENAME,
    )
    from legolization.catalog_infer.validate import (  # noqa: PLC0415
        all_gates_passed,
        validate_extension,
        validation_payload,
    )
    from legolization.errors import ConfigurationError  # noqa: PLC0415
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    path: Path = args.path
    artifacts: tuple[ArtifactRecord, ...] = ()
    if path.is_dir():
        extension = path / SUPPORT_EXTENSION_FILENAME
        if not extension.is_file():
            msg = (
                f"{path} is not a catalog support bundle: "
                f"it has no {SUPPORT_EXTENSION_FILENAME}"
            )
            raise ConfigurationError(msg)
        sidecar = path / SUPPORT_ESTIMATES_FILENAME
        sidecars = (sidecar,) if sidecar.is_file() else ()
        gates = validate_extension(extension_path=extension, sidecars=sidecars)
        validation_path = path / SUPPORT_VALIDATION_FILENAME
        atomic_json(
            validation_path,
            validation_payload(
                gates,
                extension=SUPPORT_EXTENSION_FILENAME,
                generated_at=datetime.now(UTC).isoformat(),
            ),
        )
        artifacts = (
            ArtifactRecord(path=str(validation_path), kind="catalog-validation"),
        )
    elif path.is_file():
        gates = validate_extension(path)
    else:
        msg = f"catalog validate target does not exist: {path}"
        raise ConfigurationError(msg)
    all_passed = all_gates_passed(gates)
    exit_code = COMPLETE if all_passed else PARTIAL
    if not args.json:
        print(f"validated: {path}")
        print("\n".join(_gate_lines(gates)))
    return ResultEnvelope(
        command="catalog validate",
        exit_code=exit_code,
        artifacts=artifacts,
        data={
            "path": str(path),
            "all_passed": all_passed,
            "gates": [gate.to_dict() for gate in gates],
        },
    )


__all__ = ["configure"]
