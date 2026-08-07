"""Dedicated ``legolization analyze`` command-line surface."""

from __future__ import annotations

import argparse
import sys
import zipfile
from dataclasses import dataclass, field, replace
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
    from typing import TextIO

    from legolization.assembly_connections import ConnectionAnalysis
    from legolization.assembly_model import AssemblyModel
    from legolization.assembly_physics import SupportResolution
    from legolization.layout import Layout

_LDRAW_SUFFIXES = {".ldr", ".mpd"}


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalyzeOutcome:
    """Machine-readable result of one analysis invocation."""

    exit_code: int
    verdict: str | None = None
    status: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _OutputPaths:
    legacy_report: str | None
    assembly_report: str | None
    manifest: Path | None
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
    connectivity: ConnectionAnalysis
    support: SupportResolution
    written: dict[str, str]
    warnings: list[str]


def build_parser() -> argparse.ArgumentParser:
    """Build the parser independently from the unified command hierarchy."""
    from legolization.cli.analyze import configure_args  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="legolization analyze",
        description="Analyze arbitrary LDraw geometry and search for a valid repair.",
    )
    configure_args(parser)
    return parser


def main(argv: list[str]) -> int:
    """Run both report surfaces and return the assembly-driven exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    outcome = run_analysis(args, parser)
    if outcome.error_message is not None:
        print(f"error: {outcome.error_message}", file=sys.stderr)
    return outcome.exit_code


def run_analysis(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    json_mode: bool = False,
) -> AnalyzeOutcome:
    """Run both report surfaces and return the machine-readable outcome.

    Under ``json_mode`` the human summary goes to stderr and stdout
    report targets are rejected, keeping stdout free for the caller's
    single result envelope.
    """
    if args.input.suffix.lower() not in _LDRAW_SUFFIXES:
        parser.error("analyze input must end in .ldr or .mpd")
    if json_mode and "-" in {args.report, args.assembly_report}:
        parser.error("--json forbids '-' report targets (stdout carries the envelope)")
    try:
        project = _project_config(args)
        _validate_connection_sources(args)
    except (ConfigurationError, OSError) as error:
        return AnalyzeOutcome(
            exit_code=1,
            status="error",
            error_message=str(error),
        )
    _validate_option_combinations(parser, args)
    manifest_enabled = not args.no_manifest and (
        args.manifest is not None or project.output.manifest
    )
    paths = _resolve_paths(args, manifest_enabled=manifest_enabled)
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
    report = assembly_result.report
    try:
        _write_result_views(
            project=project,
            paths=paths,
            legacy_report=legacy_report,
            assembly_result=assembly_result,
        )
    except (ManifestError, OSError) as error:
        return AnalyzeOutcome(
            exit_code=1,
            verdict=report.verdict,
            status="error",
            artifacts=dict(report.artifacts),
            warnings=tuple(report.warnings),
            error_message=f"report write failed: {error}",
        )
    _print_summary(
        report,
        legacy_path=paths.legacy_report,
        assembly_path=paths.assembly_report,
        stream=sys.stderr if json_mode else None,
    )
    return AnalyzeOutcome(
        exit_code=_exit_code(report),
        verdict=report.verdict,
        status=report.status,
        artifacts=dict(report.artifacts),
        warnings=tuple(report.warnings),
    )


def _validate_connection_sources(args: argparse.Namespace) -> None:
    for path in args.ldcad_metadata:
        if not path.exists():
            msg = f"LDCad shadow source does not exist: {path}"
            raise ConfigurationError(msg)
        if not path.is_dir() and not zipfile.is_zipfile(path):
            msg = f"LDCad shadow source must be a directory or ZIP/CSL archive: {path}"
            raise ConfigurationError(msg)
    for path in args.studio_metadata:
        if not path.is_file():
            msg = f"Studio connectivity source is not a readable file: {path}"
            raise ConfigurationError(msg)


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
    per_search_budget = time_budget / 2.0
    repair_enabled = project.stability.repair
    legacy = analyze_ldraw(
        args.input,
        AnalysisConfig(
            auto_ground=not args.preserve_origin,
            check_source_steps=not args.no_step_check,
            repair=repair_enabled,
            repair_time_budget_s=per_search_budget,
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
            repair_time_budget_s=per_search_budget,
            seed=seed,
            auto_ground_strict=not args.preserve_origin,
        ),
    )
    return legacy, assembly


def _write_result_views(
    *,
    project: ProjectConfig,
    paths: _OutputPaths,
    legacy_report: AnalysisReport,
    assembly_result: AssemblyAnalysisResult,
) -> None:
    _write_legacy_report(legacy_report, paths.legacy_report)
    _write_assembly_output(assembly_result.report, paths.assembly_report)
    if assembly_result.report.status == "error" or paths.manifest is None:
        return
    manifest = manifest_for_analysis(
        assembly_result,
        configuration=project.to_dict(),
    )
    write_manifest(manifest, paths.manifest)


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
        except (OSError, TypeError, ValueError) as error:
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
    except (OSError, TypeError, ValueError) as error:
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
    except (OSError, TypeError, ValueError) as error:
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


def _resolve_paths(
    args: argparse.Namespace,
    *,
    manifest_enabled: bool,
) -> _OutputPaths:
    stem = args.input.stem
    artifact_dir = args.artifact_dir
    base = artifact_dir if artifact_dir is not None else args.input.parent
    legacy_report = args.report
    assembly_report = args.assembly_report
    manifest = (
        args.manifest or args.input.with_name(f"{stem}.manifest.json")
        if manifest_enabled
        else None
    )
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
        manifest=manifest,
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
    if paths.manifest is not None:
        if paths.manifest.suffix.lower() != ".json":
            parser.error("--manifest must end in .json")
        file_paths.append(("manifest", paths.manifest))
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
    stream: TextIO | None = None,
) -> None:
    if stream is None:
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
