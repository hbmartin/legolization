"""Dedicated ``legolization analyze`` command-line surface."""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import replace
from pathlib import Path

from legolization.analysis import (
    AnalysisConfig,
    AnalysisReport,
    analyze_ldraw,
    write_analysis_report,
)
from legolization.redesign import write_repair_model

_LDRAW_SUFFIXES = {".ldr", ".mpd"}


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        msg = f"{value!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from error
    if not math.isfinite(parsed) or parsed <= 0:
        msg = f"{value!r} must be finite and greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the parser independently from the legacy generation CLI."""
    parser = argparse.ArgumentParser(
        prog="legolization analyze",
        description="Analyze an existing LDraw model and search for a valid repair.",
    )
    parser.add_argument("input", type=Path, help="input .ldr or .mpd model")
    parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="JSON report path (default: INPUT.analysis.json; '-' = stdout)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="repair model path (default: INPUT.repaired.ldr when found)",
    )
    parser.add_argument(
        "--time-budget",
        type=_positive_float,
        default=300.0,
        metavar="SECONDS",
        help="hard redesign-search budget (default: 300)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="versioned catalog extension; may be repeated",
    )
    parser.add_argument(
        "--preserve-origin",
        action="store_true",
        help="treat LDraw layer zero as authoritative instead of auto-grounding",
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="analyze only; do not search for a redesigned model",
    )
    parser.add_argument(
        "--no-step-check",
        action="store_true",
        help="skip informational checks of source STEP prefixes",
    )
    parser.add_argument("--seed", type=int, default=0, help="repair search seed")
    return parser


def main(argv: list[str]) -> int:
    """Run the analyze command and return its automation-friendly exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.input.suffix.lower() not in _LDRAW_SUFFIXES:
        parser.error("analyze input must end in .ldr or .mpd")
    report_path = (
        args.report
        if args.report is not None
        else str(args.input.with_suffix(".analysis.json"))
    )
    output = args.output or args.input.with_suffix(".repaired.ldr")
    _validate_paths(parser, args.input, report_path=report_path, output=output)
    result = analyze_ldraw(
        args.input,
        AnalysisConfig(
            auto_ground=not args.preserve_origin,
            check_source_steps=not args.no_step_check,
            repair=not args.no_repair,
            repair_time_budget_s=args.time_budget,
            seed=args.seed,
            catalog_paths=tuple(args.catalog),
        ),
    )
    report = result.report
    if result.repaired_layout is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            write_repair_model(result.repaired_layout, output)
        except OSError as error:
            # The verdict is already determined; losing only the repair
            # artifact degrades the report instead of masking the verdict.
            report = replace(
                report,
                status="partial",
                errors=(*report.errors, f"repair write failed: {error}"),
            )
        else:
            report = replace(
                report,
                repair={**report.repair, "output_path": str(output)},
            )
    try:
        _write_report(report, report_path)
    except OSError as error:
        print(f"error: report write failed: {error}", file=sys.stderr)
        return 1
    _print_summary(report, report_path=report_path)
    if report.status == "error" or report.verdict == "indeterminate":
        return 1
    return 0 if report.verdict == "feasible" else 2


def _write_report(report: AnalysisReport, report_path: str) -> None:
    if report_path == "-":
        print(report.to_json(), end="")
    else:
        write_analysis_report(report, Path(report_path))


def _print_summary(  # noqa: C901 - compact terminal report cases
    report: AnalysisReport,
    *,
    report_path: str,
) -> None:
    stream = sys.stderr if report_path == "-" else sys.stdout
    print(f"analysis: {report.verdict.upper()}", file=stream)
    if report.model:
        print(
            f"  bricks: {report.model['brick_count']}   "
            f"mass: {float(report.model['mass_g']):.1f} g",
            file=stream,
        )
    if report.solvers:
        parity = report.solvers["rbe_5dof"]
        strict = report.solvers["rbe_6dof"]
        capacity = report.solvers["maximin_6dof"]
        print(
            f"  5-DOF: {'STABLE' if parity['stable'] else 'UNSTABLE'}   "
            f"6-DOF: {'STABLE' if strict['stable'] else 'UNSTABLE'}   "
            f"capacity: {float(capacity['capacity_n']):.3f} N",
            file=stream,
        )
        weakest = strict.get("weakest_pair") or parity.get("weakest_pair")
        if weakest is not None:
            locations = [_brick_location(report, int(brick_id)) for brick_id in weakest]
            print(
                f"  weakest interface: {' <-> '.join(locations)}",
                file=stream,
            )
    if report.topology:
        print(
            f"  components: {report.topology['component_count']}   "
            f"floating: {len(report.topology['floating_brick_ids'])}",
            file=stream,
        )
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    match report.repair.get("status"):
        case "found":
            print(
                f"  repair: {report.repair['tier']} -> "
                f"{report.repair.get('output_path', 'validated in memory')}",
                file=stream,
            )
        case "not_found":
            suffix = " (time budget expired)" if report.repair.get("timed_out") else ""
            print(f"  repair: no validated candidate{suffix}", file=stream)
        case "disabled":
            print("  repair: disabled", file=stream)
        case "not_needed":
            print("  repair: not needed", file=stream)
        case "error":
            print(f"  repair error: {report.repair.get('error')}", file=sys.stderr)
    if report_path != "-":
        print(f"wrote {report_path}", file=stream)


def _brick_location(report: AnalysisReport, brick_id: int) -> str:
    """Format a solver brick id with its original LDraw source location."""
    row = next(
        (brick for brick in report.bricks if brick["brick_id"] == brick_id),
        None,
    )
    if row is None:
        return f"brick {brick_id}"
    source = row.get("source")
    if isinstance(source, dict) and source.get("line") is not None:
        return f"brick {brick_id} ({source['model']}:{source['line']})"
    return f"brick {brick_id}"


def _validate_paths(
    parser: argparse.ArgumentParser,
    input_path: Path,
    *,
    report_path: str,
    output: Path,
) -> None:
    reserved = str(input_path.resolve()).casefold()
    if report_path != "-":
        report = Path(report_path)
        if report.suffix.lower() != ".json":
            parser.error("--report must end in .json or be '-'")
        if str(report.resolve()).casefold() == reserved:
            parser.error("analysis report must not overwrite the input")
    if output.suffix.lower() != ".ldr":
        parser.error("--output must end in .ldr")
    if str(output.resolve()).casefold() == reserved:
        parser.error("repair output must not overwrite the input")
    if report_path != "-" and (
        str(Path(report_path).resolve()).casefold() == str(output.resolve()).casefold()
    ):
        parser.error("analysis report and repair output must be distinct")
