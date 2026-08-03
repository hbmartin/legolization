"""Dedicated ``legolization analyze`` command-line surface."""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from legolization.analysis import (
    AnalysisConfig,
    AnalysisReport,
    AnalysisResult,
    analyze_ldraw,
    write_analysis_report,
)
from legolization.assembly import (
    AssemblyAnalysisConfig,
    AssemblyAnalysisReport,
    AssemblyAnalysisResult,
    analyze_assembly,
    write_assembly_report,
    write_counterfactual_candidate,
)
from legolization.assembly_artifacts import (
    render_comparison,
    write_callout_svg,
    write_component_mpd,
    write_floating_mpd,
    write_graph_json,
    write_html_report,
)
from legolization.configuration import (
    ProjectConfig,
    load_project_config,
    merge_overrides,
)
from legolization.errors import ConfigurationError, ManifestError
from legolization.manifest import (
    manifest_for_analysis,
    write_manifest,
)
from legolization.redesign import write_repair_model

if TYPE_CHECKING:
    from pyldcad import ConnectivityAnalysis

    from legolization.assembly_model import AssemblyModel
    from legolization.assembly_physics import SupportResolution
    from legolization.layout import Layout

_LDRAW_SUFFIXES = {".ldr", ".mpd"}
_SCENARIOS = (
    "auto",
    "rest",
    "lift-body",
    "lift-chassis",
    "front-torsion",
    "rear-torsion",
    "side-load",
)


@dataclass(frozen=True, slots=True)
class _OutputPaths:
    legacy_report: str | None
    assembly_report: str | None
    candidate: Path
    graph: Path | None
    components: Path | None
    floating: Path | None
    html: Path | None
    callout_dir: Path | None
    comparison_dir: Path | None


@dataclass(slots=True)
class _ArtifactState:
    """Shared typed state for best-effort artifact writers."""

    model: AssemblyModel
    connectivity: ConnectivityAnalysis
    support: SupportResolution
    written: dict[str, str]
    warnings: list[str]


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


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        msg = f"{value!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from error
    if not math.isfinite(parsed) or parsed < 0:
        msg = f"{value!r} must be finite and non-negative"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the parser independently from the legacy generation CLI."""
    # Declarative argparse registration is intentionally kept contiguous.
    # lizard forgives(length)
    parser = argparse.ArgumentParser(
        prog="legolization analyze",
        description="Analyze arbitrary LDraw geometry and search for a valid repair.",
    )
    parser.add_argument("input", type=Path, help="input .ldr or .mpd model")
    parser.add_argument("--config", type=Path, default=None)
    manifest = parser.add_mutually_exclusive_group()
    manifest.add_argument("--manifest", type=Path, default=None)
    manifest.add_argument("--no-manifest", action="store_true")
    parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="optional legacy schema-2 JSON view ('-' = stdout)",
    )
    parser.add_argument(
        "--assembly-report",
        default=None,
        metavar="PATH",
        help="optional legacy assembly schema-1 JSON view ('-' = stdout)",
    )
    parser.add_argument("--graph", type=Path, default=None, metavar="PATH")
    parser.add_argument("--diagnostic-mpd", type=Path, default=None, metavar="PATH")
    parser.add_argument("--floating-mpd", type=Path, default=None, metavar="PATH")
    parser.add_argument("--html-report", type=Path, default=None, metavar="PATH")
    parser.add_argument("--callout-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument("--comparison-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="collect assembly artifacts and enable HTML/rendered diagnostics",
    )
    parser.add_argument(
        "--no-data-artifacts",
        action="store_true",
        help="suppress default graph, component MPD, and floating MPD outputs",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="best repair path (default: INPUT.repaired.ldr/.mpd when found)",
    )
    parser.add_argument(
        "--time-budget",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="hard repair-search budget (default: 300)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="legacy voxel catalog extension; may be repeated",
    )
    parser.add_argument(
        "--connector-catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="native pyLDCad connector catalog; may be repeated",
    )
    parser.add_argument(
        "--ldcad-metadata",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="LDCad SNAP metadata source; may be repeated",
    )
    parser.add_argument(
        "--studio-metadata",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Studio connectivity JSON export; may be repeated",
    )
    parser.add_argument(
        "--preserve-origin",
        action="store_true",
        help="keep LDraw layer zero authoritative in legacy and adaptive support",
    )
    parser.add_argument(
        "--repair",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable repaired-model search",
    )
    parser.add_argument(
        "--no-step-check",
        action="store_true",
        help="skip informational checks of source STEP prefixes",
    )
    parser.add_argument("--seed", type=int, default=None, help="repair search seed")
    parser.add_argument(
        "--topology-only",
        action="store_true",
        help="skip mass, support, and equilibrium analysis",
    )
    parser.add_argument(
        "--support",
        default=None,
        metavar="MODE",
        help=("auto, free, wheels, auto-ground, anchored-baseplate, or selected:IDS"),
    )
    parser.add_argument(
        "--path-between",
        nargs=2,
        action="append",
        default=[],
        metavar=("LEFT", "RIGHT"),
        help="region path selectors, e.g. pages:1-4 occurrences:20-30",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=_SCENARIOS,
        default=None,
        help="assembly load scenario; may be repeated",
    )
    parser.add_argument("--gravity-g", type=_nonnegative_float, default=None)
    parser.add_argument("--side-load-g", type=_nonnegative_float, default=None)
    parser.add_argument("--torsion-load-g", type=_nonnegative_float, default=None)
    return parser


def main(argv: list[str]) -> int:
    """Run both report surfaces and return the assembly-driven exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.input.suffix.lower() not in _LDRAW_SUFFIXES:
        parser.error("analyze input must end in .ldr or .mpd")
    try:
        project = _project_config(args)
    except (ConfigurationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    _validate_option_combinations(parser, args)
    paths = _resolve_paths(args)
    _validate_paths(parser, args.input, paths=paths)
    legacy_result, assembly_result = _run_analyses(args, project=project)
    legacy_report, assembly_result = _write_candidate(
        legacy_result.report,
        legacy_result.repaired_layout,
        assembly_result,
        output=paths.candidate,
    )
    assembly_result = _write_artifacts(
        assembly_result,
        input_path=args.input,
        paths=paths,
    )
    try:
        _write_result_views(
            args,
            project=project,
            paths=paths,
            legacy_report=legacy_report,
            assembly_result=assembly_result,
        )
    except (ManifestError, OSError) as error:
        print(f"error: report write failed: {error}", file=sys.stderr)
        return 1
    _print_summary(
        assembly_result.report,
        legacy_path=paths.legacy_report,
        assembly_path=paths.assembly_report,
    )
    return _exit_code(assembly_result.report)


def _project_config(args: argparse.Namespace) -> ProjectConfig:
    project = load_project_config(args.config)
    overrides = {
        key: value
        for key, value in {
            "placement.seed": args.seed,
            "placement.time_budget_s": args.time_budget,
            "stability.repair": args.repair,
        }.items()
        if value is not None
    }
    return merge_overrides(project, overrides) if overrides else project


def _run_analyses(
    args: argparse.Namespace,
    *,
    project: ProjectConfig,
) -> tuple[AnalysisResult, AssemblyAnalysisResult]:
    seed = project.placement.seed
    time_budget = project.placement.time_budget_s or 300.0
    repair_enabled = project.stability.repair
    legacy = analyze_ldraw(
        args.input,
        AnalysisConfig(
            auto_ground=not args.preserve_origin,
            check_source_steps=not args.no_step_check,
            repair=repair_enabled,
            repair_time_budget_s=time_budget,
            seed=seed,
            catalog_paths=tuple(args.catalog),
        ),
    )
    assembly = analyze_assembly(
        args.input,
        AssemblyAnalysisConfig(
            topology_only=args.topology_only,
            support=args.support
            or ("anchored-baseplate" if args.preserve_origin else "auto"),
            scenarios=tuple(args.scenario or ("auto",)),
            gravity_g=1.0 if args.gravity_g is None else args.gravity_g,
            side_load_g=1.0 if args.side_load_g is None else args.side_load_g,
            torsion_load_g=0.5 if args.torsion_load_g is None else args.torsion_load_g,
            path_between=tuple(tuple(pair) for pair in args.path_between),
            connector_catalog_paths=tuple(args.connector_catalog),
            ldcad_metadata_paths=tuple(args.ldcad_metadata),
            studio_metadata_paths=tuple(args.studio_metadata),
            voxel_catalog_paths=tuple(args.catalog),
            repair=repair_enabled,
            repair_time_budget_s=time_budget,
            seed=seed,
            auto_ground_strict=not args.preserve_origin,
        ),
    )
    return legacy, assembly


def _write_result_views(
    args: argparse.Namespace,
    *,
    project: ProjectConfig,
    paths: _OutputPaths,
    legacy_report: AnalysisReport,
    assembly_result: AssemblyAnalysisResult,
) -> None:
    _write_legacy_report(legacy_report, paths.legacy_report)
    _write_assembly_output(assembly_result.report, paths.assembly_report)
    if (
        assembly_result.report.status == "error"
        or args.no_manifest
        or (args.manifest is None and not project.output.manifest)
    ):
        return
    manifest_path = args.manifest or args.input.with_name(
        f"{args.input.stem}.manifest.json"
    )
    manifest = manifest_for_analysis(
        assembly_result,
        configuration=project.to_dict(),
    )
    write_manifest(manifest, manifest_path)


def _write_candidate(
    legacy_report: AnalysisReport,
    legacy_layout: Layout | None,
    assembly_result: AssemblyAnalysisResult,
    *,
    output: Path,
) -> tuple[AnalysisReport, AssemblyAnalysisResult]:
    try:
        if write_counterfactual_candidate(assembly_result, output):
            counterfactual = dict(assembly_result.report.counterfactual)
            candidate = dict(counterfactual.get("candidate") or {})
            candidate["output_path"] = str(output)
            counterfactual["candidate"] = candidate
            report = replace(
                assembly_result.report,
                counterfactual=counterfactual,
                artifacts={
                    **assembly_result.report.artifacts,
                    "candidate": str(output),
                },
            )
            return legacy_report, replace(assembly_result, report=report)
        if legacy_layout is not None:
            write_repair_model(legacy_layout, output)
            legacy_report = replace(
                legacy_report,
                repair={**legacy_report.repair, "output_path": str(output)},
            )
    except OSError as error:
        legacy_report = replace(
            legacy_report,
            status="partial",
            errors=(*legacy_report.errors, f"repair write failed: {error}"),
        )
        assembly_result = _degrade_assembly(
            assembly_result,
            f"repair write failed: {error}",
        )
    return legacy_report, assembly_result


def _write_artifacts(
    result: AssemblyAnalysisResult,
    *,
    input_path: Path,
    paths: _OutputPaths,
) -> AssemblyAnalysisResult:
    if result.model is None or result.connectivity is None or result.support is None:
        return result
    model = result.model
    connectivity = result.connectivity
    support = result.support
    state = _ArtifactState(
        model=model,
        connectivity=connectivity,
        support=support,
        written=dict(result.report.artifacts),
        warnings=[],
    )
    _write_data_artifacts(state, paths=paths)
    _write_callout(state, path=paths.callout_dir)
    candidate_path = (
        Path(state.written["candidate"]) if "candidate" in state.written else None
    )
    if paths.comparison_dir is not None and candidate_path is not None:
        comparison = render_comparison(input_path, candidate_path, paths.comparison_dir)
        state.written.update(comparison.paths)
        state.warnings.extend(comparison.warnings)
    report = _artifact_report(
        result.report,
        written=state.written,
        warnings=state.warnings,
    )
    report = _write_html_view(report, path=paths.html)
    return replace(result, report=report)


def _write_data_artifacts(
    state: _ArtifactState,
    *,
    paths: _OutputPaths,
) -> None:
    operations = (
        (
            "graph",
            paths.graph,
            lambda path: write_graph_json(state.connectivity, path),
        ),
        (
            "components",
            paths.components,
            lambda path: write_component_mpd(state.model, state.connectivity, path),
        ),
        (
            "floating",
            paths.floating,
            lambda path: write_floating_mpd(
                state.model,
                state.connectivity,
                state.support,
                path,
            ),
        ),
    )
    for name, path, operation in operations:
        if path is None:
            continue
        try:
            operation(path)
        except OSError as error:
            state.warnings.append(f"{name} artifact write failed: {error}")
        else:
            state.written[name] = str(path)


def _write_callout(
    state: _ArtifactState,
    *,
    path: Path | None,
) -> None:
    if path is None:
        return
    callout = path / "missing-connections.svg"
    try:
        write_callout_svg(state.model, state.connectivity, callout)
    except OSError as error:
        state.warnings.append(f"callout artifact write failed: {error}")
    else:
        state.written["callouts"] = str(callout)


def _artifact_report(
    report: AssemblyAnalysisReport,
    *,
    written: dict[str, str],
    warnings: list[str],
) -> AssemblyAnalysisReport:
    return replace(
        report,
        artifacts=written,
        warnings=(*report.warnings, *warnings),
        status=(
            "partial" if warnings and report.status == "complete" else report.status
        ),
    )


def _write_html_view(
    report: AssemblyAnalysisReport,
    *,
    path: Path | None,
) -> AssemblyAnalysisReport:
    if path is None:
        return report
    report = replace(report, artifacts={**report.artifacts, "html": str(path)})
    try:
        write_html_report(report, path)
    except OSError as error:
        return replace(
            report,
            status="partial" if report.status == "complete" else report.status,
            warnings=(*report.warnings, f"HTML report write failed: {error}"),
            artifacts={
                key: value for key, value in report.artifacts.items() if key != "html"
            },
        )
    return report


def _degrade_assembly(
    result: AssemblyAnalysisResult,
    warning: str,
) -> AssemblyAnalysisResult:
    report = replace(
        result.report,
        status="partial"
        if result.report.status == "complete"
        else result.report.status,
        warnings=(*result.report.warnings, warning),
    )
    return replace(result, report=report)


def _write_legacy_report(report: AnalysisReport, report_path: str | None) -> None:
    if report_path is None:
        return
    if report_path == "-":
        print(report.to_json(), end="")
    else:
        write_analysis_report(report, Path(report_path))


def _write_assembly_output(
    report: AssemblyAnalysisReport,
    report_path: str | None,
) -> None:
    if report_path is None:
        return
    if report_path == "-":
        print(report.to_json(), end="")
    else:
        write_assembly_report(report, Path(report_path))


def _resolve_paths(args: argparse.Namespace) -> _OutputPaths:
    stem = args.input.stem
    artifact_dir = args.artifact_dir
    base = artifact_dir if artifact_dir is not None else args.input.parent
    legacy_report = args.report
    assembly_report = args.assembly_report
    candidate = args.output or args.input.with_name(
        f"{stem}.repaired{args.input.suffix.lower()}"
    )
    if args.no_data_artifacts:
        graph = components = floating = None
    else:
        graph = args.graph or base / f"{stem}.connections.json"
        components = args.diagnostic_mpd or base / f"{stem}.components.mpd"
        floating = args.floating_mpd or base / f"{stem}.floating.mpd"
    html_path = args.html_report or (
        base / f"{stem}.analysis.html" if artifact_dir is not None else None
    )
    callout_dir = args.callout_dir or (
        base / f"{stem}.callouts" if artifact_dir is not None else None
    )
    comparison_dir = args.comparison_dir or (
        base / f"{stem}.comparison" if artifact_dir is not None else None
    )
    return _OutputPaths(
        legacy_report=legacy_report,
        assembly_report=assembly_report,
        candidate=candidate,
        graph=graph,
        components=components,
        floating=floating,
        html=html_path,
        callout_dir=callout_dir,
        comparison_dir=comparison_dir,
    )


def _validate_option_combinations(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.report == "-" and args.assembly_report == "-":
        parser.error("only one report may target stdout")
    if args.no_data_artifacts and any(
        value is not None
        for value in (args.graph, args.diagnostic_mpd, args.floating_mpd)
    ):
        parser.error("--no-data-artifacts conflicts with explicit data artifact paths")
    if args.topology_only and any(
        value is not None
        for value in (
            args.support,
            args.scenario,
            args.gravity_g,
            args.side_load_g,
            args.torsion_load_g,
        )
    ):
        parser.error("--topology-only cannot specify support or load options")


def _validate_paths(  # noqa: C901 - all output collision checks stay centralized
    parser: argparse.ArgumentParser,
    input_path: Path,
    *,
    paths: _OutputPaths,
) -> None:
    file_paths: list[tuple[str, Path]] = [("repair output", paths.candidate)]
    if paths.legacy_report is not None and paths.legacy_report != "-":
        legacy = Path(paths.legacy_report)
        if legacy.suffix.lower() != ".json":
            parser.error("--report must end in .json or be '-'")
        file_paths.append(("legacy report", legacy))
    if paths.assembly_report is not None and paths.assembly_report != "-":
        assembly = Path(paths.assembly_report)
        if assembly.suffix.lower() != ".json":
            parser.error("--assembly-report must end in .json or be '-'")
        file_paths.append(("assembly report", assembly))
    for name, path, suffix in (
        ("graph", paths.graph, ".json"),
        ("diagnostic MPD", paths.components, ".mpd"),
        ("floating MPD", paths.floating, ".mpd"),
        ("HTML report", paths.html, ".html"),
    ):
        if path is not None:
            if path.suffix.lower() != suffix:
                parser.error(f"{name} must end in {suffix}")
            file_paths.append((name, path))
    if paths.candidate.suffix.lower() not in _LDRAW_SUFFIXES:
        parser.error("--output must end in .ldr or .mpd")
    resolved_input = str(input_path.resolve()).casefold()
    seen: dict[str, str] = {resolved_input: "input"}
    for name, path in file_paths:
        resolved = str(path.resolve()).casefold()
        if resolved in seen:
            parser.error(f"{name} must be distinct from {seen[resolved]}")
        seen[resolved] = name


def _print_summary(
    report: AssemblyAnalysisReport,
    *,
    legacy_path: str | None,
    assembly_path: str | None,
) -> None:
    stream = sys.stderr if "-" in {legacy_path, assembly_path} else sys.stdout
    print(f"analysis: {report.verdict.upper()}", file=stream)
    if report.geometry:
        print(
            f"  occurrences: {report.geometry['resolved_count']}/"
            f"{report.geometry['occurrence_count']}   "
            f"mass: {_mass_text(report.geometry.get('mass_g'))}",
            file=stream,
        )
    if report.connectivity:
        interval = report.connectivity["component_interval"]
        print(
            f"  components: {interval[0]}-{interval[1]}   "
            f"confirmed connections: "
            f"{report.connectivity['confirmed_connection_count']}",
            file=stream,
        )
    if report.support:
        print(f"  support: {report.support['resolved']}", file=stream)
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    if legacy_path is not None and legacy_path != "-":
        print(f"wrote {legacy_path}", file=stream)
    if assembly_path is not None and assembly_path != "-":
        print(f"wrote {assembly_path}", file=stream)


def _mass_text(value: float | None) -> str:
    return "unknown" if value is None else f"{float(value):.1f} g"


def _exit_code(report: AssemblyAnalysisReport) -> int:
    if report.status == "error":
        return 1
    if report.verdict in {"disconnected", "infeasible"}:
        return 2
    if report.status == "partial" or report.verdict == "indeterminate":
        return 3
    return 0
