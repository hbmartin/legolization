"""The ``input`` command group: source-model inspection and normalization."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_config_options, add_json_option
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE

if TYPE_CHECKING:
    import argparse

    from legolization.inspection import InputInspection, NormalizedOutput


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
    inspect.add_argument(
        "--write",
        action="store_true",
        help=(
            "also write the -prepared sibling bundle with a normalized "
            ".npy target and JSON sidecar"
        ),
    )
    inspect.add_argument(
        "--up",
        choices=("x", "y", "z"),
        default=None,
        help="override the mesh vertical axis instead of classifying it",
    )
    scale = inspect.add_mutually_exclusive_group()
    scale.add_argument("--target-studs", type=int, metavar="N")
    scale.add_argument(
        "--auto-scale",
        type=int,
        nargs=2,
        metavar=("MIN", "MAX"),
    )
    add_config_options(inspect)
    add_json_option(inspect)
    inspect.set_defaults(handler=_run_inspect, command_name="input inspect")


def _run_inspect(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.inspection import (  # noqa: PLC0415
        inspect_input,
        write_normalized,
    )

    auto_scale = tuple(args.auto_scale) if args.auto_scale is not None else None
    artifacts: tuple[ArtifactRecord, ...] = ()
    output = None
    if args.write:
        inspection, output = write_normalized(
            args.input,
            up=args.up,
            target_studs=args.target_studs,
            auto_scale=auto_scale,
        )
        artifacts = (
            ArtifactRecord(path=str(output.npy_path), kind="normalized"),
            ArtifactRecord(path=str(output.sidecar_path), kind="sidecar"),
            ArtifactRecord(
                path=str(output.directory / "bundle.json"),
                kind="bundle-record",
            ),
        )
    else:
        inspection = inspect_input(
            args.input,
            up=args.up,
            target_studs=args.target_studs,
            auto_scale=auto_scale,
        )
    if not args.json:
        _print_summary(inspection, output)
    return ResultEnvelope(
        command="input inspect",
        exit_code=COMPLETE,
        artifacts=artifacts,
        warnings=inspection.warnings,
        data=inspection.to_dict(),
    )


def _print_summary(
    inspection: InputInspection,
    output: NormalizedOutput | None,
) -> None:
    print(f"{inspection.filename}: {inspection.kind} ({inspection.format})")
    if inspection.voxel is not None:
        report = inspection.voxel
        shape = "x".join(str(value) for value in report.grid_shape)
        print(
            f"  grid: {shape}   filled: {report.filled_count}   "
            f"colours: {len(report.colour_codes)}   "
            f"recommended plates/voxel: {report.recommended_plates_per_voxel}"
        )
    if inspection.mesh is not None:
        report = inspection.mesh
        recommendation = report.recommendation
        print(
            f"  triangles: {report.triangle_count}   "
            f"watertight: {report.watertight}   "
            f"components: {report.component_count}"
        )
        print(
            f"  up: {report.up.recommended} ({report.up.confidence})   "
            f"recommended target studs: {recommendation.target_studs}"
        )
        colour = "sampled" if report.has_colour_data else "none (uniform needed)"
        print(f"  colour data: {colour}")
    for condition in inspection.conditions:
        print(f"  condition: {condition}")
    for warning in inspection.warnings:
        print(f"  warning: {warning}")
    if output is not None:
        print(f"wrote {output.directory}")


__all__ = ["configure"]
