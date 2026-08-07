"""The ``model`` command group: operations on generated models."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE, OPERATIONAL_ERROR, PARTIAL

if TYPE_CHECKING:
    import argparse

    from legolization.model_render import ModelRenderReport


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
    render.add_argument(
        "--views",
        default="front,iso,top",
        metavar="NAMES",
        help="comma-separated views from front, iso, top (default: all)",
    )
    render.add_argument(
        "--size",
        type=int,
        default=800,
        metavar="PIXELS",
        help="image width in pixels (height is 3/4 of the width)",
    )
    render.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="image directory (default: beside the model)",
    )
    add_json_option(render)
    render.set_defaults(handler=_run_render, command_name="model render")


def _run_render(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.model_render import (  # noqa: PLC0415
        render_model_views,
        write_render_info,
    )

    views = tuple(part.strip() for part in args.views.split(",") if part.strip())
    report = render_model_views(
        args.input,
        views=views,
        out_dir=args.output,
        width=args.size,
        height=args.size * 3 // 4,
    )
    out_dir = args.output if args.output is not None else report.model.parent
    info_path = write_render_info(report, out_dir=out_dir)
    succeeded = report.succeeded
    if not args.json:
        _print_summary(report, info_path)
    artifacts = [
        ArtifactRecord(path=str(outcome.path), kind="render")
        for outcome in succeeded
        if outcome.path is not None
    ]
    artifacts.append(ArtifactRecord(path=str(info_path), kind="render-info"))
    if len(succeeded) == len(report.outcomes):
        exit_code = COMPLETE
    elif succeeded:
        exit_code = PARTIAL
    else:
        exit_code = OPERATIONAL_ERROR
    warnings = tuple(
        f"{outcome.view}: {outcome.detail}"
        for outcome in report.outcomes
        if outcome.status == "failed" and outcome.detail
    )
    return ResultEnvelope(
        command="model render",
        exit_code=exit_code,
        artifacts=tuple(artifacts),
        warnings=warnings,
        data=report.to_dict(),
    )


def _print_summary(report: ModelRenderReport, info_path: Path) -> None:
    for outcome in report.outcomes:
        if outcome.status == "ok":
            print(f"rendered {outcome.view}: {outcome.path}")
        else:
            print(f"failed {outcome.view}: {outcome.detail}")
    print(f"wrote {info_path}")


__all__ = ["configure"]
