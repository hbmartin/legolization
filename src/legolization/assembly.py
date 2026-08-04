"""Public geometry-first LDraw assembly analysis and reporting API."""

from __future__ import annotations

import hashlib
import json
import math
import time
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ldraw import Severity, iter_ldr_issues, load_model

from legolization.assembly_connections import (
    ConnectionAnalysis,
    ConnectionConfig,
    analyze_connections,
)
from legolization.assembly_counterfactual import (
    CounterfactualCandidate,
    CounterfactualSearchResult,
    search_counterfactuals,
)
from legolization.assembly_grid import GridFrame, detect_grid_frames
from legolization.assembly_model import (
    AssemblyModel,
    build_assembly_model,
    source_line,
    source_model,
)
from legolization.assembly_paths import (
    AssemblyRegion,
    RegionPath,
    analyze_region_path,
    classify_regions,
)
from legolization.assembly_physics import (
    AssemblyPhysicsResult,
    SupportResolution,
    analyze_assembly_physics,
    resolve_support,
)
from legolization.assembly_registry import build_registry
from legolization.catalog import CATALOG_SCHEMA, load_catalog
from legolization.ldraw_in import OccurrenceImport, import_occurrences
from legolization.ldraw_report import catalog_error, prepare_connection_catalog

if TYPE_CHECKING:
    from pathlib import Path

    from ldraw import Vector
    from ldraw.parts import Parts

    from legolization.assembly_model import AssemblyOccurrence
    from legolization.assembly_registry import ConnectorRegistry

type AssemblyStatus = Literal["complete", "partial", "error"]
type AssemblyTopologyVerdict = Literal["connected", "disconnected", "indeterminate"]
type AssemblyVerdict = Literal[
    "feasible",
    "infeasible",
    "connected",
    "disconnected",
    "indeterminate",
]

_SUPPORT_MODES = {
    "auto",
    "free",
    "wheels",
    "auto-ground",
    "anchored-baseplate",
}
_SCENARIOS = {
    "auto",
    "rest",
    "lift-body",
    "lift-chassis",
    "front-torsion",
    "rear-torsion",
    "side-load",
}


@dataclass(frozen=True, slots=True)
class AssemblyAnalysisConfig:
    """Configuration for tolerant topology, supports, physics, and repair."""

    topology_only: bool = False
    support: str = "auto"
    scenarios: tuple[str, ...] = ("auto",)
    gravity_g: float = 1.0
    side_load_g: float = 1.0
    torsion_load_g: float = 0.5
    path_between: tuple[tuple[str, str], ...] = ()
    connector_catalog_paths: tuple[Path, ...] = ()
    ldcad_metadata_paths: tuple[Path, ...] = ()
    studio_metadata_paths: tuple[Path, ...] = ()
    voxel_catalog_paths: tuple[Path, ...] = ()
    repair: bool = True
    repair_time_budget_s: float = 300.0
    seed: int = 0
    infer_surface_contacts: bool = True
    auto_ground_strict: bool = True

    def __post_init__(self) -> None:
        # Fail-fast cross-field validation is intentionally centralized.
        # lizard forgives(cyclomatic_complexity)
        support = self.support.casefold()
        if support not in _SUPPORT_MODES and not support.startswith("selected:"):
            msg = f"unsupported support mode {self.support!r}"
            raise ValueError(msg)
        if not self.scenarios or any(
            scenario.casefold() not in _SCENARIOS for scenario in self.scenarios
        ):
            msg = "assembly scenarios contain an unsupported value"
            raise ValueError(msg)
        for name, value in (
            ("gravity_g", self.gravity_g),
            ("side_load_g", self.side_load_g),
            ("torsion_load_g", self.torsion_load_g),
        ):
            if not math.isfinite(value) or value < 0:
                msg = f"{name} must be finite and non-negative"
                raise ValueError(msg)
        if (
            not math.isfinite(self.repair_time_budget_s)
            or self.repair_time_budget_s <= 0
        ):
            msg = "repair_time_budget_s must be finite and positive"
            raise ValueError(msg)
        if self.topology_only and (
            support != "auto"
            or self.scenarios != ("auto",)
            or self.gravity_g != 1.0
            or self.side_load_g != 1.0
            or self.torsion_load_g != 0.5
        ):
            msg = "topology-only analysis cannot specify support or load options"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AssemblyAnalysisReport:
    """Versioned JSON-safe geometry-first assembly evidence."""

    status: AssemblyStatus
    verdict: AssemblyVerdict
    topology_verdict: AssemblyTopologyVerdict
    physics_verdict: str
    input: dict[str, Any]
    geometry: dict[str, Any]
    connectivity: dict[str, Any]
    grid_frames: tuple[dict[str, Any], ...]
    support: dict[str, Any]
    regions: tuple[dict[str, Any], ...]
    load_paths: tuple[dict[str, Any], ...]
    physics: dict[str, Any]
    counterfactual: dict[str, Any]
    occurrences: tuple[dict[str, Any], ...]
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    elapsed_s: float = 0.0
    schema: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible representation."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize this report with stable formatting."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class AssemblyAnalysisResult:
    """Report plus heavyweight assembly, topology, and candidate objects."""

    report: AssemblyAnalysisReport
    model: AssemblyModel | None = None
    connectivity: ConnectionAnalysis | None = None
    support: SupportResolution | None = None
    physics: AssemblyPhysicsResult | None = None
    counterfactual: CounterfactualSearchResult | None = None

    @property
    def candidate(self) -> CounterfactualCandidate | None:
        """Return the best validated candidate, when search found one."""
        return (
            self.counterfactual.candidate if self.counterfactual is not None else None
        )


def analyze_assembly(
    path: Path,
    config: AssemblyAnalysisConfig | None = None,
) -> AssemblyAnalysisResult:
    """Analyze arbitrary LDraw geometry without requiring strict ``Layout``."""
    # One tolerant error boundary composes typed geometry/topology/physics phases.
    # lizard forgives(cyclomatic_complexity, length)
    config = config or AssemblyAnalysisConfig()
    started = time.perf_counter()
    input_payload = _input_payload(path)
    try:
        voxel_catalog = load_catalog(*config.voxel_catalog_paths)
        prepared = prepare_connection_catalog(
            connection_shadows=config.ldcad_metadata_paths,
            studio_metadata=config.studio_metadata_paths,
        )
        if (catalog_failure := catalog_error(prepared)) is not None:
            return _error_result(
                input_payload,
                started=started,
                message=catalog_failure,
            )
        if prepared.parts is None:
            raise AssertionError
        loaded = load_model(path)
        load_diagnostics = tuple(iter_ldr_issues(loaded))
        model_analysis = loaded.analyze(prepared.parts)
        if model_analysis is None or model_analysis.inspection is None:
            return _error_result(
                input_payload,
                started=started,
                message="LDraw model could not produce resolved inspection geometry",
            )
        connection_config = ConnectionConfig(
            infer_surface_contacts=config.infer_surface_contacts,
        )
        registry = build_registry(config.connector_catalog_paths)
        model = build_assembly_model(
            model_analysis,
            registry=registry,
            voxel_catalog=voxel_catalog,
        )
        if not model.occurrences:
            return _error_result(
                input_payload,
                started=started,
                message="model contains no occurrences with resolved geometry",
            )
        connectivity = analyze_connections(
            model_analysis.inspection,
            registry=registry,
            config=connection_config,
        )
        strict_import = import_occurrences(
            model_analysis.occurrences,
            catalog=voxel_catalog,
            ground=config.auto_ground_strict,
            default_model=path.name,
        )
        frames = detect_grid_frames(model_analysis.occurrences)
        regions = classify_regions(model)
        support = resolve_support(
            config.support,
            model=model,
            regions=regions,
            strict_import=(strict_import if not strict_import.problems else None),
        )
        paths, path_warnings = _load_paths(
            config.path_between,
            model=model,
            connectivity=connectivity,
            regions=regions,
        )
        topology_verdict = _topology_verdict(connectivity)
        physics = (
            AssemblyPhysicsResult(verdict="not_run", support=support, scenarios=())
            if config.topology_only
            else analyze_assembly_physics(
                model=model,
                connectivity=connectivity,
                support=support,
                regions=regions,
                scenarios=tuple(item.casefold() for item in config.scenarios),
                gravity_g=config.gravity_g,
                side_load_g=config.side_load_g,
                torsion_load_g=config.torsion_load_g,
                strict_import=(strict_import if not strict_import.problems else None),
            )
        )
        verdict = _overall_verdict(topology_verdict, physics.verdict)
        counterfactual = _counterfactual(
            path,
            config=config,
            verdict=verdict,
            model=model,
            connectivity=connectivity,
            parts=prepared.parts,
            registry=registry,
            connection_config=connection_config,
        )
        warnings = (
            tuple(
                f"{diagnostic.code}: {diagnostic.message}"
                for diagnostic in load_diagnostics
                if diagnostic.severity is Severity.WARNING
            )
            + tuple(item.message for item in connectivity.diagnostics)
            + path_warnings
        )
        status: AssemblyStatus = (
            "partial"
            if model.skipped_occurrence_ids
            or any(
                diagnostic.severity is Severity.ERROR for diagnostic in load_diagnostics
            )
            else "complete"
        )
        report = _report(
            status=status,
            verdict=verdict,
            topology_verdict=topology_verdict,
            input_payload=input_payload,
            model=model,
            connectivity=connectivity,
            strict_import=strict_import,
            frames=frames,
            support=support,
            regions=regions,
            paths=paths,
            physics=physics,
            counterfactual=counterfactual,
            warnings=warnings,
            elapsed_s=time.perf_counter() - started,
        )
        return AssemblyAnalysisResult(
            report=report,
            model=model,
            connectivity=connectivity,
            support=support,
            physics=physics,
            counterfactual=counterfactual,
        )
    except (OSError, RuntimeError, TypeError, ValueError, zipfile.BadZipFile) as error:
        return _error_result(
            input_payload,
            started=started,
            message=str(error),
        )


def write_assembly_report(report: AssemblyAnalysisReport, path: Path) -> None:
    """Write one assembly report, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8")


def write_counterfactual_candidate(
    result: AssemblyAnalysisResult,
    path: Path,
) -> bool:
    """Write the best source-preserving candidate and return whether one exists."""
    if (candidate := result.candidate) is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate.model_text, encoding="utf-8")
    return True


def _report(  # noqa: PLR0913 - explicit report evidence inputs
    *,
    status: AssemblyStatus,
    verdict: AssemblyVerdict,
    topology_verdict: AssemblyTopologyVerdict,
    input_payload: dict[str, Any],
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    strict_import: OccurrenceImport,
    frames: tuple[GridFrame, ...],
    support: SupportResolution,
    regions: tuple[AssemblyRegion, ...],
    paths: tuple[RegionPath, ...],
    physics: AssemblyPhysicsResult,
    counterfactual: CounterfactualSearchResult,
    warnings: tuple[str, ...],
    elapsed_s: float,
) -> AssemblyAnalysisReport:
    # Explicit evidence fields make report construction reviewable and deterministic.
    # lizard forgives(parameter_count)
    coverage_counts = Counter(item.level.value for item in connectivity.coverage)
    bounds = model.analysis.inspection.bounds if model.analysis.inspection else None
    return AssemblyAnalysisReport(
        status=status,
        verdict=verdict,
        topology_verdict=topology_verdict,
        physics_verdict=physics.verdict,
        input=input_payload,
        geometry={
            "occurrence_count": model.occurrence_count,
            "resolved_count": len(model.occurrences),
            "skipped_occurrence_ids": list(model.skipped_occurrence_ids),
            "bounds_ldu": (
                {
                    "min": _vector_payload(bounds.min),
                    "max": _vector_payload(bounds.max),
                }
                if bounds is not None
                else None
            ),
            "mass_g": model.total_mass_g,
            "mass_complete": model.total_mass_g is not None,
        },
        connectivity={
            "endpoint_count": len(connectivity.endpoints),
            "confirmed_connection_count": len(connectivity.confirmed_connections),
            "potential_connection_count": len(connectivity.potential_connections),
            "unmatched_endpoint_count": len(connectivity.unmatched_endpoints),
            "coverage": dict(sorted(coverage_counts.items())),
            "confirmed_component_count": connectivity.confirmed_component_count,
            "optimistic_component_count": connectivity.optimistic_component_count,
            "component_interval": list(connectivity.component_interval),
            "confirmed_components": [
                list(item) for item in connectivity.confirmed_components
            ],
            "optimistic_components": [
                list(item) for item in connectivity.optimistic_components
            ],
            "strict_layout_compatible": not strict_import.problems,
            "strict_import_problem_count": len(strict_import.problems),
        },
        grid_frames=tuple(_grid_payload(item) for item in frames),
        support=asdict(support),
        regions=tuple(asdict(item) for item in regions),
        load_paths=tuple(asdict(item) for item in paths),
        physics={
            "verdict": physics.verdict,
            "scenarios": [asdict(item) for item in physics.scenarios],
        },
        counterfactual=_counterfactual_payload(counterfactual),
        occurrences=tuple(_occurrence_payload(item) for item in model.occurrences),
        warnings=warnings,
        elapsed_s=elapsed_s,
    )


def _error_result(
    input_payload: dict[str, Any],
    *,
    started: float,
    message: str,
) -> AssemblyAnalysisResult:
    return AssemblyAnalysisResult(
        report=AssemblyAnalysisReport(
            status="error",
            verdict="indeterminate",
            topology_verdict="indeterminate",
            physics_verdict="not_run",
            input=input_payload,
            geometry={},
            connectivity={},
            grid_frames=(),
            support={},
            regions=(),
            load_paths=(),
            physics={},
            counterfactual={"status": "skipped", "reason": "analysis failed"},
            occurrences=(),
            errors=(message,),
            elapsed_s=time.perf_counter() - started,
        )
    )


def _topology_verdict(
    connectivity: ConnectionAnalysis,
) -> AssemblyTopologyVerdict:
    if connectivity.confirmed_component_count <= 1:
        return "connected"
    if connectivity.optimistic_component_count > 1:
        return "disconnected"
    return "indeterminate"


def _overall_verdict(
    topology: AssemblyTopologyVerdict,
    physics: str,
) -> AssemblyVerdict:
    if topology == "disconnected":
        return "disconnected"
    if physics == "infeasible":
        return "infeasible"
    if topology == "indeterminate" or physics == "indeterminate":
        return "indeterminate"
    if physics == "feasible":
        return "feasible"
    return "connected"


def _counterfactual(  # noqa: PLR0913 - explicit search boundary
    path: Path,
    *,
    config: AssemblyAnalysisConfig,
    verdict: AssemblyVerdict,
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    parts: Parts,
    registry: ConnectorRegistry,
    connection_config: ConnectionConfig,
) -> CounterfactualSearchResult:
    if not config.repair:
        return CounterfactualSearchResult(
            status="disabled",
            candidate=None,
            tested_candidates=0,
            timed_out=False,
            elapsed_s=0.0,
            message="counterfactual search was disabled",
        )
    if verdict not in {"disconnected", "infeasible"}:
        return CounterfactualSearchResult(
            status="not_needed",
            candidate=None,
            tested_candidates=0,
            timed_out=False,
            elapsed_s=0.0,
            message="analysis has no definite failure",
        )
    return search_counterfactuals(
        path,
        model=model,
        connectivity=connectivity,
        parts=parts,
        registry=registry,
        config=connection_config,
        time_budget_s=config.repair_time_budget_s,
    )


def _load_paths(
    configured: tuple[tuple[str, str], ...],
    *,
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    regions: tuple[AssemblyRegion, ...],
) -> tuple[tuple[RegionPath, ...], tuple[str, ...]]:
    pairs = list(configured)
    if not pairs:
        body = next((item for item in regions if item.name == "body"), None)
        chassis = next((item for item in regions if item.name == "chassis"), None)
        if body is not None and chassis is not None:
            pairs.append(
                (
                    _occurrence_selector(body.occurrence_ids),
                    _occurrence_selector(chassis.occurrence_ids),
                )
            )
    paths: list[RegionPath] = []
    warnings: list[str] = []
    for left, right in pairs:
        try:
            paths.append(
                analyze_region_path(
                    left,
                    right,
                    model=model,
                    connectivity=connectivity,
                )
            )
        except (TypeError, ValueError) as error:
            warnings.append(f"load path {left!r} -> {right!r} skipped: {error}")
    return tuple(paths), tuple(warnings)


def _input_payload(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError:
        return {
            "path": str(path),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "voxel_catalog_schema": CATALOG_SCHEMA,
    }


def _grid_payload(frame: GridFrame) -> dict[str, Any]:
    return {
        "frame_id": frame.frame_id,
        "kind": frame.kind,
        "origin_ldu": _vector_payload(frame.origin_ldu),
        "basis": list(frame.basis),
        "inlier_occurrence_ids": list(frame.inlier_occurrence_ids),
        "confidence": frame.confidence,
        "horizontal_spacing_ldu": frame.horizontal_spacing_ldu,
        "vertical_spacing_ldu": frame.vertical_spacing_ldu,
    }


def _occurrence_payload(item: AssemblyOccurrence) -> dict[str, Any]:
    source = item.source
    bounds = item.bounds_ldu
    center = item.center_of_mass_ldu
    inertia = item.inertia_g_ldu2
    return {
        "occurrence_id": item.occurrence_id,
        "pyldraw_index": item.pyldraw_index,
        "part_id": item.part_id,
        "reference": item.reference,
        "colour_code": item.colour_code,
        "position_ldu": _vector_payload(item.position_ldu),
        "matrix": list(item.matrix),
        "bounds_ldu": {
            "min": _vector_payload(bounds.minimum),
            "max": _vector_payload(bounds.maximum),
        },
        "mass_g": item.mass_g,
        "mass_source": item.mass_source,
        "center_of_mass_ldu": (_vector_payload(center) if center is not None else None),
        "inertia_g_ldu2": (_vector_payload(inertia) if inertia is not None else None),
        "tags": sorted(item.tags),
        "source": {
            "model_path": list(source.model_path),
            "reference_path": list(source.reference_path),
            "source_line_path": list(source.source_line_path),
            "local_step_path": list(source.local_step_path),
            "effective_step_path": list(source.effective_step_path),
            "page_path": list(source.page_path),
            "source_model": source_model(source),
            "source_line": source_line(source),
            "source_page": source.source_page,
        },
    }


def _counterfactual_payload(result: CounterfactualSearchResult) -> dict[str, Any]:
    candidate = result.candidate
    return {
        "status": result.status,
        "tested_candidates": result.tested_candidates,
        "timed_out": result.timed_out,
        "elapsed_s": result.elapsed_s,
        "message": result.message,
        "candidate": (
            {
                "occurrence_id": candidate.occurrence_id,
                "source_model": candidate.source_model,
                "source_line": candidate.source_line,
                "part_id": candidate.part_id,
                "kind": candidate.kind,
                "description": candidate.description,
                "before_component_count": candidate.before_component_count,
                "after_component_count": candidate.after_component_count,
                "before_connection_count": candidate.before_connection_count,
                "after_connection_count": candidate.after_connection_count,
                "confirmed_connection_gain": candidate.confirmed_connection_gain,
                "component_reduction": candidate.component_reduction,
                "confidence": candidate.confidence,
            }
            if candidate is not None
            else None
        ),
    }


def _occurrence_selector(occurrence_ids: tuple[int, ...]) -> str:
    return f"occurrences:{','.join(str(item) for item in occurrence_ids)}"


def _vector_payload(value: Vector) -> list[float]:
    return [
        float(value.x),
        float(value.y),
        float(value.z),
    ]
