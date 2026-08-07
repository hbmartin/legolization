"""The ``instructions`` command group: build-instruction auditing."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE, PARTIAL, UNBUILDABLE

if TYPE_CHECKING:
    import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``instructions`` operations and handler."""
    operations = parser.add_subparsers(
        dest="instructions_command",
        metavar="OPERATION",
        required=True,
    )
    audit = operations.add_parser(
        "audit",
        help="audit step ordering, stability, and insertion pressure",
    )
    audit.add_argument(
        "input",
        type=Path,
        help="step-annotated .ldr/.mpd model or bundle directory",
    )
    audit.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="audit report destination (default: alongside the input)",
    )
    audit.add_argument(
        "--render-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="also render per-step PNGs for visual inspection",
    )
    add_json_option(audit)
    audit.set_defaults(handler=_run_audit, command_name="instructions audit")


def _run_audit(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415
    from legolization.instructions.audit import audit_model  # noqa: PLC0415
    from legolization.model_render import resolve_model  # noqa: PLC0415

    model = resolve_model(args.input)
    payload = audit_model(model, render_dir=args.render_dir)
    report_path = _report_path(args, model)
    atomic_json(report_path, payload)
    if not args.json:
        _print_summary(payload, report_path)
    match payload["verdict"]:
        case "certified":
            exit_code = COMPLETE
        case "infeasible":
            exit_code = UNBUILDABLE
        case _:
            exit_code = PARTIAL
    return ResultEnvelope(
        command="instructions audit",
        exit_code=exit_code,
        artifacts=(ArtifactRecord(path=str(report_path), kind="report"),),
        warnings=tuple(payload.get("render_warnings", [])),
        data=payload,
    )


def _report_path(args: argparse.Namespace, model: Path) -> Path:
    from legolization.bundle.paths import numbered_file  # noqa: PLC0415

    if args.report is not None:
        return args.report
    if args.input.is_dir():
        target = args.input / "instructions" / "audit.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    return numbered_file(model.with_name(f"{model.stem}.audit.json"))


def _print_summary(payload: dict[str, Any], report_path: Path) -> None:
    source = payload["input"]
    print(
        f"{source['filename']}: {source['brick_count']} bricks in "
        f"{source['step_count']} step(s) -> {payload['verdict']}"
    )
    certification = payload["certification"]
    for violation in certification["violations"]:
        print(f"  violation: {violation}")
    flagged = [row for row in payload["steps"] if row["flags"]]
    for row in flagged:
        print(f"  step {row['index']}: {', '.join(row['flags'])}")
    print(f"wrote {report_path}")


__all__ = ["configure"]
