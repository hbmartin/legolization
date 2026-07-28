"""Physics analysis and repair reporting for existing LDraw models."""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from legolization.catalog import CATALOG_SCHEMA, load_catalog
from legolization.graph import ConnectionGraph
from legolization.ldraw_in import (
    ImportedLdrawModel,
    LdrawImportError,
    LdrawSourceRef,
    import_ldraw,
)
from legolization.stability.links import LinkReport, localize_instability
from legolization.stability.prefix import PrefixSolver
from legolization.stability.solver import (
    BrickScore,
    SolverConfig,
    StabilityResult,
    analyze,
    build_model_from_config,
    solve_maximin,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from legolization.layout import Layout

Verdict = Literal["feasible", "infeasible", "indeterminate"]


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """Configuration for a completed-model LDraw feasibility analysis."""

    auto_ground: bool = True
    check_source_steps: bool = True
    repair: bool = True
    repair_time_budget_s: float = 300.0
    seed: int = 0
    catalog_paths: tuple[Path, ...] = ()
    parity_solver: SolverConfig = field(
        default_factory=lambda: SolverConfig(torque_z=False, ground_pull=True)
    )
    strict_solver: SolverConfig = field(
        default_factory=lambda: SolverConfig(torque_z=True, ground_pull=True)
    )

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.repair_time_budget_s)
            or self.repair_time_budget_s <= 0
        ):
            msg = "repair_time_budget_s must be finite and positive"
            raise ValueError(msg)
        if self.parity_solver.ground_pull is not True:
            msg = "the analysis parity solver must use anchored baseplate ground"
            raise ValueError(msg)
        if self.strict_solver.ground_pull is not True:
            msg = "the analysis strict solver must use anchored baseplate ground"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Versioned, JSON-safe evidence from one analysis run."""

    status: Literal["complete", "partial", "error"]
    verdict: Verdict
    input: dict[str, Any]
    assumptions: dict[str, Any]
    model: dict[str, Any]
    topology: dict[str, Any]
    solvers: dict[str, Any]
    catalog: dict[str, Any] = field(default_factory=dict)
    bricks: tuple[dict[str, Any], ...] = ()
    source_steps: tuple[dict[str, Any], ...] = ()
    repair: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    problems: tuple[dict[str, Any], ...] = ()
    schema: int = 1

    @property
    def feasible(self) -> bool:
        """Whether the original completed model passed every official check."""
        return self.status != "error" and self.verdict == "feasible"

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible representation."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize the report with stable key and list ordering."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Report plus heavyweight imported and repaired layouts for API users."""

    report: AnalysisReport
    imported: ImportedLdrawModel | None = None
    repaired_layout: Layout | None = None


@dataclass(frozen=True, slots=True)
class _PhysicsRun:
    parity: StabilityResult
    strict: StabilityResult
    strict_capacity: float
    strict_capacity_feasible: bool
    graph: ConnectionGraph
    links: LinkReport | None

    @property
    def feasible(self) -> bool:
        return (
            self.graph.component_count() == 1
            and not self.graph.floating_ids()
            and self.parity.stable
            and self.strict.stable
            and self.strict_capacity_feasible
            and self.strict_capacity > 0.0
        )


def analyze_ldraw(
    path: Path,
    config: AnalysisConfig | None = None,
) -> AnalysisResult:
    """Analyze an existing LDraw model and optionally search for a repair."""
    config = config or AnalysisConfig()
    started = time.perf_counter()
    input_info = _input_info(path)
    assumptions = _assumptions(config)
    catalog_info: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "version": f"schema-{CATALOG_SCHEMA}",
        "extensions": [str(extension) for extension in config.catalog_paths],
    }
    try:
        catalog = load_catalog(*config.catalog_paths)
        imported = import_ldraw(path, catalog=catalog, ground=config.auto_ground)
    except (LdrawImportError, OSError, TypeError, ValueError) as error:
        problems = (
            list(error.problems)
            if isinstance(error, LdrawImportError)
            else [str(error)]
        )
        return AnalysisResult(
            report=AnalysisReport(
                status="error",
                verdict="indeterminate",
                input=input_info,
                assumptions=assumptions,
                model={},
                topology={},
                solvers={},
                catalog=catalog_info,
                repair={"status": "skipped", "reason": "import failed"},
                errors=tuple(problems),
                problems=(
                    tuple(asdict(problem) for problem in error.details)
                    if isinstance(error, LdrawImportError)
                    else ()
                ),
            )
        )

    if not imported.layout.bricks:
        return AnalysisResult(
            imported=imported,
            report=AnalysisReport(
                status="error",
                verdict="indeterminate",
                input={
                    **input_info,
                    "ground_offset_layers": imported.ground_offset_layers,
                    "ground_translation_layers": -imported.ground_offset_layers,
                },
                assumptions=assumptions,
                model=_model_summary(imported.layout),
                topology={},
                solvers={},
                catalog={**catalog_info, "part_count": len(catalog.parts)},
                repair={"status": "skipped", "reason": "empty model"},
                errors=("model contains no supported pieces",),
            ),
        )

    try:
        physics = _run_physics(imported.layout, config)
    except (RuntimeError, ValueError) as error:
        return AnalysisResult(
            imported=imported,
            report=AnalysisReport(
                status="error",
                verdict="indeterminate",
                input={
                    **input_info,
                    "ground_offset_layers": imported.ground_offset_layers,
                    "ground_translation_layers": -imported.ground_offset_layers,
                },
                assumptions=assumptions,
                model=_model_summary(imported.layout),
                topology={},
                solvers={},
                catalog={**catalog_info, "part_count": len(catalog.parts)},
                repair={"status": "skipped", "reason": "analysis failed"},
                errors=(str(error),),
            ),
        )

    step_warnings: list[str] = []
    try:
        source_steps = (
            _analyze_source_steps(imported, config)
            if config.check_source_steps and imported.has_explicit_steps
            else ()
        )
    except (RuntimeError, ValueError) as error:
        source_steps = ()
        step_warnings.append(f"source-step analysis failed: {error}")
    warnings = tuple(step_warnings) + tuple(
        f"source step {row['step']} is not feasible"
        for row in source_steps
        if not bool(row["feasible"])
    )
    repair_payload: dict[str, Any] = {
        "status": "not_needed" if physics.feasible else "disabled",
    }
    repaired_layout: Layout | None = None
    if not physics.feasible and config.repair:
        from legolization.redesign import search_repair  # noqa: PLC0415 - cycle

        try:
            repair_result = search_repair(
                imported.layout,
                physics_seed_ids=_physics_seed_ids(physics),
                parity_solver=config.parity_solver,
                strict_solver=config.strict_solver,
                time_budget_s=config.repair_time_budget_s,
                seed=config.seed,
            )
        except Exception as error:  # noqa: BLE001 - report worker start failures
            repair_payload = {
                "status": "error",
                "error": f"{type(error).__name__}: {error}",
                "before_metrics": _physics_metrics(physics),
            }
        else:
            repair_payload = {
                **repair_result.to_report(imported.source_refs),
                "before_metrics": _physics_metrics(physics),
            }
            repaired_layout = repair_result.layout

    # The finished-model verdict is already determined here; a repair-search
    # failure or timeout only makes the repair evidence incomplete, so it
    # degrades the status to "partial" rather than masking the verdict.
    if (
        repair_payload.get("status") == "error"
        or repair_payload.get("timed_out") is True
    ):
        status: Literal["complete", "partial", "error"] = "partial"
    else:
        status = "complete"
    report = AnalysisReport(
        status=status,
        verdict="feasible" if physics.feasible else "infeasible",
        input={
            **input_info,
            "catalog_schema": CATALOG_SCHEMA,
            "catalog_parts": len(catalog.parts),
            "ground_offset_layers": imported.ground_offset_layers,
            "ground_translation_layers": -imported.ground_offset_layers,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
        assumptions=assumptions,
        model=_model_summary(imported.layout),
        topology=_topology_payload(physics.graph),
        solvers=_solver_payload(physics),
        catalog={**catalog_info, "part_count": len(catalog.parts)},
        bricks=_brick_payloads(imported, physics),
        source_steps=source_steps,
        repair=repair_payload,
        warnings=warnings,
    )
    return AnalysisResult(
        report=report,
        imported=imported,
        repaired_layout=repaired_layout,
    )


def write_analysis_report(report: AnalysisReport, path: Path) -> None:
    """Atomically write a JSON report, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(report.to_json())
    temporary.replace(path)


def _run_physics(layout: Layout, config: AnalysisConfig) -> _PhysicsRun:
    graph = ConnectionGraph.from_layout(layout)
    parity = analyze(layout, config.parity_solver, graph)
    strict = analyze(layout, config.strict_solver, graph)
    maximin = solve_maximin(
        build_model_from_config(layout, config.strict_solver, graph)
    )
    official_failure = (
        graph.component_count() != 1
        or bool(graph.floating_ids())
        or not parity.stable
        or not strict.stable
        or not maximin.feasible
        or maximin.capacity <= 0.0
    )
    links = (
        localize_instability(layout, graph=graph, config=config.strict_solver)
        if official_failure
        else None
    )
    return _PhysicsRun(
        parity=parity,
        strict=strict,
        strict_capacity=maximin.capacity,
        strict_capacity_feasible=maximin.feasible,
        graph=graph,
        links=links,
    )


def _physics_seed_ids(physics: _PhysicsRun) -> tuple[int, ...]:
    seeds = set(physics.parity.unstable_ids | physics.strict.unstable_ids)
    seeds |= set(physics.graph.floating_ids())
    for pair in (physics.parity.weakest_pair, physics.strict.weakest_pair):
        if pair is not None:
            seeds |= {brick_id for brick_id in pair if brick_id >= 0}
    if physics.links is not None:
        for link in physics.links.links[:8]:
            seeds |= {brick_id for brick_id in (link.a_id, link.b_id) if brick_id >= 0}
    return tuple(sorted(seeds))


def _analyze_source_steps(
    imported: ImportedLdrawModel,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[int, list[int]] = {}
    for brick_id, ref in imported.source_refs.items():
        step = ref.global_step or ref.source_step or 1
        grouped.setdefault(step, []).append(brick_id)
    solver = PrefixSolver.create(imported.layout, config.strict_solver)
    present: set[int] = set()
    rows: list[dict[str, Any]] = []
    for step, brick_ids in sorted(grouped.items()):
        chunk = tuple(sorted(brick_ids))
        result = (
            solver.probe(chunk)
            if solver is not None
            else analyze(
                imported.layout.subset(present | set(chunk)),
                config.strict_solver,
            )
        )
        present.update(chunk)
        if solver is not None:
            solver.commit(chunk)
        prefix = imported.layout.subset(present)
        graph = ConnectionGraph.from_layout(prefix)
        feasible = (
            result.stable and graph.component_count() == 1 and not graph.floating_ids()
        )
        rows.append(
            {
                "step": step,
                "brick_ids": list(chunk),
                "brick_count": len(present),
                "stable": result.stable,
                "max_score": result.max_score,
                "component_count": graph.component_count(),
                "floating_count": len(graph.floating_ids()),
                "feasible": feasible,
            }
        )
    return tuple(rows)


def _brick_payloads(
    imported: ImportedLdrawModel,
    physics: _PhysicsRun,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    floating = physics.graph.floating_ids()
    for brick_id, brick in sorted(imported.layout.bricks.items()):
        parity = physics.parity.scores[brick_id]
        strict = physics.strict.scores[brick_id]
        source = imported.source_refs.get(brick_id)
        flags: list[str] = []
        if brick_id in floating:
            flags.append("floating")
        if parity.score >= 1.0:
            flags.append("unstable-5dof")
        if strict.score >= 1.0:
            flags.append("unstable-6dof")
        result.append(
            {
                "brick_id": brick_id,
                "part_key": brick.part_key,
                "ldraw_part": imported.layout.part_of(brick).ldraw_part,
                "colour_code": brick.colour_code,
                "position": [brick.x, brick.y, brick.layer],
                "yaw": brick.yaw,
                "source": _source_payload(source),
                "parity": _score_payload(parity),
                "strict": _score_payload(strict),
                "flags": flags,
            }
        )
    return tuple(result)


def _source_payload(source: LdrawSourceRef | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "occurrence": source.occurrence,
        "model": source.source_model,
        "line": source.source_line,
        "source_step": source.source_step,
        "global_step": source.global_step,
    }


def _score_payload(score: BrickScore) -> dict[str, Any]:
    return {
        "score": score.score,
        "drag_max_n": score.drag_max,
        "in_equilibrium": score.in_equilibrium,
    }


def _model_summary(layout: Layout) -> dict[str, Any]:
    return {
        "brick_count": len(layout),
        "mass_g": layout.total_mass_g(),
        "bounds": _bounds(layout),
    }


def _bounds(layout: Layout) -> dict[str, list[int]] | None:
    if not layout.occupancy:
        return None
    xs, ys, zs = zip(*layout.occupancy, strict=True)
    return {
        "minimum": [min(xs), min(ys), min(zs)],
        "maximum": [max(xs), max(ys), max(zs)],
    }


def _topology_payload(graph: ConnectionGraph) -> dict[str, Any]:
    labels = graph.brick_components()
    components: dict[int, list[int]] = {}
    for brick_id, label in labels.items():
        components.setdefault(label, []).append(brick_id)
    return {
        "component_count": graph.component_count(),
        "connected": graph.component_count() == 1,
        "ground_reachable": not graph.floating_ids(),
        "components": [components[label] for label in sorted(components)],
        "grounded_brick_ids": sorted(graph.grounded_ids),
        "floating_brick_ids": sorted(graph.floating_ids()),
        "stud_connections": [list(edge) for edge in graph.support_edges()],
        "knob_contact_count": len(graph.knob_contacts),
        "side_contact_count": len(graph.side_contacts),
    }


def _solver_payload(physics: _PhysicsRun) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rbe_5dof": _stability_payload(physics.parity),
        "rbe_6dof": _stability_payload(physics.strict),
        "maximin_6dof": {
            "feasible": physics.strict_capacity_feasible,
            "capacity_n": physics.strict_capacity,
        },
    }
    if physics.links is not None:
        payload["localization"] = {
            "status": physics.links.status,
            "q": physics.links.q,
            "links": [asdict(link) for link in physics.links.links],
        }
    return payload


def _physics_metrics(physics: _PhysicsRun) -> dict[str, Any]:
    """Compact official metrics used for repair before/after comparison."""
    return {
        "feasible": physics.feasible,
        "component_count": physics.graph.component_count(),
        "floating_count": len(physics.graph.floating_ids()),
        "rbe_5dof_stable": physics.parity.stable,
        "rbe_5dof_max_score": physics.parity.max_score,
        "rbe_6dof_stable": physics.strict.stable,
        "rbe_6dof_max_score": physics.strict.max_score,
        "strict_capacity_n": physics.strict_capacity,
    }


def _stability_payload(result: StabilityResult) -> dict[str, Any]:
    return {
        "stable": result.stable,
        "status": result.status,
        "objective": result.objective,
        "max_score": result.max_score,
        "min_capacity_n": result.min_capacity,
        "weakest_pair": (
            list(result.weakest_pair) if result.weakest_pair is not None else None
        ),
        "unstable_brick_ids": sorted(result.unstable_ids),
    }


def _input_info(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"path": str(path), "format": path.suffix.lower()}
    with suppress(OSError):
        payload.update({"sha256": _sha256(path), "size_bytes": path.stat().st_size})
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assumptions(config: AnalysisConfig) -> dict[str, Any]:
    return {
        "ground": "anchored-baseplate",
        "grounding": "auto" if config.auto_ground else "preserve-origin",
        "loads": "dead-load-only",
        "required_profiles": ["rbe-5dof", "rbe-6dof", "maximin-6dof"],
        "source_steps_official": False,
        "repair_time_budget_s": config.repair_time_budget_s,
    }


PlacementSignature = tuple[str, int, int, int, int, int]


def placement_signature(layout: Layout, brick_id: int) -> PlacementSignature:
    """Stable identifier for diffs and deterministic repair ordering."""
    brick = layout.bricks[brick_id]
    return (
        brick.part_key,
        brick.x,
        brick.y,
        brick.layer,
        brick.yaw,
        brick.colour_code,
    )


def signatures(layout: Layout) -> tuple[PlacementSignature, ...]:
    """Sorted placement signatures for an entire layout."""
    return tuple(sorted(placement_signature(layout, bid) for bid in layout.bricks))


def source_refs_by_signature(
    layout: Layout,
    refs: Mapping[int, LdrawSourceRef],
) -> dict[PlacementSignature, LdrawSourceRef]:
    """Resolve original placement signatures back to source occurrences."""
    return {
        placement_signature(layout, brick_id): ref
        for brick_id, ref in refs.items()
        if brick_id in layout.bricks
    }
