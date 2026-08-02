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
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.ldraw_in import (
    ImportedLdrawModel,
    LdrawImportError,
    LdrawSourceRef,
    _import_occurrences,
    import_ldraw,
)
from legolization.ldraw_report import (
    build_ldraw_payload,
    catalog_degraded,
    catalog_error,
    catalog_payload,
    diagnostic_payload,
    instruction_issues,
    prepare_analysis_catalog,
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

    from ldraw import (
        CatalogPreparationResult,
        InstructionIssue,
        InstructionSection,
        ModelOccurrence,
        Vector,
    )

    from legolization.catalog import Catalog
    from legolization.layout import Layout

Verdict = Literal["feasible", "infeasible", "indeterminate"]
_MAX_LOCALIZED_SEED_LINKS = 8
_MAX_SOURCE_STEPS = 5_000


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
    ldraw: dict[str, Any] = field(default_factory=dict)
    bricks: tuple[dict[str, Any], ...] = ()
    source_steps: tuple[dict[str, Any], ...] = ()
    repair: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    problems: tuple[dict[str, Any], ...] = ()
    schema: int = 2

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
class _AnalysisInputs:
    """Catalogs and imported model prepared before physics begins."""

    catalog: Catalog
    pyldraw_catalog: CatalogPreparationResult
    catalog_info: dict[str, Any]
    imported: ImportedLdrawModel


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


@dataclass(slots=True)
class _PrefixTopology:
    """Incremental component and grounding state for source-step prefixes."""

    neighbours: dict[int, set[int]]
    grounded_ids: frozenset[int]
    present: set[int] = field(default_factory=set)
    parent: dict[int, int] = field(default_factory=dict)
    sizes: dict[int, int] = field(default_factory=dict)
    grounded_roots: dict[int, bool] = field(default_factory=dict)
    component_count: int = 0
    floating_count: int = 0

    @classmethod
    def from_layout(cls, layout: Layout) -> _PrefixTopology:
        graph = ConnectionGraph.from_layout(layout)
        neighbours = {brick_id: set() for brick_id in graph.brick_ids}
        for contact in graph.knob_contacts:
            if contact.below_id == GROUND_ID:
                continue
            neighbours[contact.below_id].add(contact.above_id)
            neighbours[contact.above_id].add(contact.below_id)
        return cls(neighbours=neighbours, grounded_ids=graph.grounded_ids)

    def extend(self, brick_ids: tuple[int, ...]) -> None:
        """Add a source-step chunk and update topology in near-linear time."""
        for brick_id in brick_ids:
            if brick_id in self.present:
                continue
            self.present.add(brick_id)
            self.parent[brick_id] = brick_id
            self.sizes[brick_id] = 1
            grounded = brick_id in self.grounded_ids
            self.grounded_roots[brick_id] = grounded
            self.component_count += 1
            self.floating_count += int(not grounded)
            for neighbour in self.neighbours.get(brick_id, ()):
                if neighbour in self.present:
                    self._union(brick_id, neighbour)

    def _find(self, brick_id: int) -> int:
        root = brick_id
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[brick_id] != brick_id:
            self.parent[brick_id], brick_id = root, self.parent[brick_id]
        return root

    def _union(self, first: int, second: int) -> None:
        first_root = self._find(first)
        second_root = self._find(second)
        if first_root == second_root:
            return
        if self.sizes[first_root] < self.sizes[second_root]:
            first_root, second_root = second_root, first_root
        before = self._floating_size(first_root) + self._floating_size(second_root)
        self.parent[second_root] = first_root
        self.sizes[first_root] += self.sizes.pop(second_root)
        self.grounded_roots[first_root] = (
            self.grounded_roots.pop(second_root) or self.grounded_roots[first_root]
        )
        self.floating_count += self._floating_size(first_root) - before
        self.component_count -= 1

    def _floating_size(self, root: int) -> int:
        return 0 if self.grounded_roots[root] else self.sizes[root]


def _error_report(  # noqa: PLR0913 - shared report fields stay explicit
    *,
    reason: str,
    errors: tuple[str, ...],
    input_info: dict[str, Any],
    assumptions: dict[str, Any],
    catalog_info: dict[str, Any],
    ldraw_info: dict[str, Any] | None = None,
    imported: ImportedLdrawModel | None = None,
    problems: tuple[dict[str, Any], ...] = (),
) -> AnalysisReport:
    """Build one consistent indeterminate report for an analysis boundary."""
    return AnalysisReport(
        status="error",
        verdict="indeterminate",
        input=input_info,
        assumptions=assumptions,
        model=(
            _model_summary(imported.layout, imported=imported)
            if imported is not None
            else {}
        ),
        topology={},
        solvers={},
        catalog=catalog_info,
        ldraw=ldraw_info or {},
        repair={"status": "skipped", "reason": reason},
        errors=errors,
        problems=problems,
    )


def _prepare_analysis_inputs(
    path: Path,
    config: AnalysisConfig,
    input_info: dict[str, Any],
    assumptions: dict[str, Any],
) -> _AnalysisInputs | AnalysisResult:
    """Load both catalogs and strictly import one tolerantly parsed model."""
    catalog_info: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "version": f"schema-{CATALOG_SCHEMA}",
        "extensions": [str(extension) for extension in config.catalog_paths],
    }
    try:
        catalog = load_catalog(*config.catalog_paths)
    except (OSError, TypeError, ValueError) as error:
        return AnalysisResult(
            report=_error_report(
                reason="catalog loading failed",
                errors=(f"catalog loading failed: {error}",),
                input_info=input_info,
                assumptions=assumptions,
                catalog_info=catalog_info,
            )
        )
    try:
        pyldraw_catalog = prepare_analysis_catalog()
    except (OSError, RuntimeError, ValueError) as error:
        return AnalysisResult(
            report=_error_report(
                reason="pyldraw catalog preparation failed",
                errors=(f"pyldraw catalog preparation failed: {error}",),
                input_info=input_info,
                assumptions=assumptions,
                catalog_info=catalog_info,
            )
        )
    pyldraw_catalog_payload = catalog_payload(pyldraw_catalog)
    catalog_info = {**catalog_info, "pyldraw": pyldraw_catalog_payload}
    if (catalog_failure := catalog_error(pyldraw_catalog)) is not None:
        return AnalysisResult(
            report=_error_report(
                reason="pyldraw catalog unavailable",
                errors=(catalog_failure,),
                input_info=input_info,
                assumptions=assumptions,
                catalog_info=catalog_info,
                ldraw_info={"catalog": pyldraw_catalog_payload},
            )
        )
    if pyldraw_catalog.parts is None:  # narrowed by catalog_error above
        raise AssertionError
    try:
        imported = import_ldraw(
            path,
            catalog=catalog,
            ground=config.auto_ground,
            parts=pyldraw_catalog.parts,
        )
    except (LdrawImportError, OSError, ValueError) as error:
        errors, problems = _import_error_payload(error)
        diagnostics = (
            error.load_diagnostics if isinstance(error, LdrawImportError) else ()
        )
        return AnalysisResult(
            report=_error_report(
                reason="import failed",
                errors=errors,
                input_info=input_info,
                assumptions=assumptions,
                catalog_info=catalog_info,
                problems=problems,
                ldraw_info={
                    "catalog": pyldraw_catalog_payload,
                    "load": {
                        "complete": (
                            error.load_complete
                            if isinstance(error, LdrawImportError)
                            and error.load_complete is not None
                            else False
                        ),
                        "diagnostics": [
                            diagnostic_payload(item) for item in diagnostics
                        ],
                    },
                },
            )
        )
    return _AnalysisInputs(
        catalog=catalog,
        pyldraw_catalog=pyldraw_catalog,
        catalog_info=catalog_info,
        imported=imported,
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
    prepared = _prepare_analysis_inputs(path, config, input_info, assumptions)
    if isinstance(prepared, AnalysisResult):
        return prepared
    catalog = prepared.catalog
    pyldraw_catalog = prepared.pyldraw_catalog
    catalog_info = prepared.catalog_info
    imported = prepared.imported

    grounded_input = {
        **input_info,
        "ground_offset_layers": imported.ground_offset_layers,
        "ground_translation_layers": -imported.ground_offset_layers,
    }
    catalog_info = {**catalog_info, "part_count": len(catalog.parts)}

    if not imported.layout.bricks:
        return AnalysisResult(
            imported=imported,
            report=_error_report(
                reason="empty model",
                errors=("model contains no supported pieces",),
                input_info=grounded_input,
                assumptions=assumptions,
                catalog_info=catalog_info,
                imported=imported,
            ),
        )

    try:
        physics = _run_physics(imported.layout, config)
    except (RuntimeError, ValueError) as error:
        return AnalysisResult(
            imported=imported,
            report=_error_report(
                reason="analysis failed",
                errors=(str(error),),
                input_info=grounded_input,
                assumptions=assumptions,
                catalog_info=catalog_info,
                imported=imported,
            ),
        )

    section_physics, issues, step_warnings = _instruction_section_payload(
        imported,
        config,
    )
    document = imported.instruction_document
    source_steps = (
        section_physics.get(document.root.name, ())
        if document is not None
        and config.check_source_steps
        and imported.has_explicit_steps
        else ()
    )
    warnings = (
        tuple(
            f"pyldraw catalog {diagnostic.code}: {diagnostic.message}"
            for diagnostic in pyldraw_catalog.diagnostics
            if str(diagnostic.severity) == "warning"
        )
        + tuple(
            f"{diagnostic.code}: {diagnostic.message}"
            for diagnostic in imported.diagnostics
            if str(diagnostic.severity) == "warning"
        )
        + tuple(
            f"instruction {issue.section}:{issue.line_number or '?'} "
            f"[{issue.code}]: {issue.message}"
            for issue in issues
        )
        + step_warnings
    )
    repair_payload, repaired_layout = _repair_payload(imported, physics, config)
    ldraw_info = build_ldraw_payload(
        imported,
        pyldraw_catalog,
        section_physics=section_physics,
        issues=issues,
    )
    catalog_errors = tuple(
        f"pyldraw catalog {diagnostic.code}: {diagnostic.message}"
        for diagnostic in pyldraw_catalog.diagnostics
        if str(diagnostic.severity) == "error"
    )
    report = AnalysisReport(
        status=_status_for(
            repair_payload,
            catalog_is_degraded=catalog_degraded(pyldraw_catalog),
        ),
        verdict="feasible" if physics.feasible else "infeasible",
        input={
            **grounded_input,
            "catalog_schema": CATALOG_SCHEMA,
            "catalog_parts": len(catalog.parts),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
        assumptions=assumptions,
        model=_model_summary(imported.layout, imported=imported),
        topology=_topology_payload(physics.graph),
        solvers=_solver_payload(physics),
        catalog=catalog_info,
        ldraw=ldraw_info,
        bricks=_brick_payloads(imported, physics),
        source_steps=source_steps,
        repair=repair_payload,
        warnings=warnings,
        errors=catalog_errors,
    )
    return AnalysisResult(
        report=report,
        imported=imported,
        repaired_layout=repaired_layout,
    )


def _import_error_payload(
    error: LdrawImportError | OSError | ValueError,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    if isinstance(error, LdrawImportError):
        return (
            error.problems,
            tuple(
                diagnostic_payload(diagnostic)
                for diagnostic in error.diagnostics
                if str(diagnostic.severity) == "error"
            )
            + tuple(asdict(problem) for problem in error.details),
        )
    return ((str(error),), ())


def _instruction_section_payload(
    imported: ImportedLdrawModel,
    config: AnalysisConfig,
) -> tuple[
    dict[str, tuple[dict[str, Any], ...]],
    tuple[InstructionIssue, ...],
    tuple[str, ...],
]:
    """Analyze every reliable reachable semantic instruction section."""
    document = imported.instruction_document
    if document is None:
        return {}, (), ()
    issues = instruction_issues(document)
    if not config.check_source_steps:
        return {}, issues, ()
    step_count = sum(len(section.steps) for section in document.sections)
    if step_count > _MAX_SOURCE_STEPS:
        message = (
            f"source-step analysis skipped: {step_count:_} steps exceeds "
            f"the {_MAX_SOURCE_STEPS:_}-step safety limit"
        )
        return {}, issues, (message,)
    error_sections = {
        issue.section for issue in issues if str(issue.severity) == "error"
    }
    payload: dict[str, tuple[dict[str, Any], ...]] = {}
    warnings: list[str] = []
    for section in document.sections:
        if section.name in error_sections:
            warnings.append(
                f"source-step analysis skipped for section {section.name}: "
                "instruction validation failed"
            )
            continue
        try:
            rows = (
                _analyze_root_instruction_section(imported, section, config)
                if section.is_root
                else _analyze_local_instruction_section(
                    section,
                    catalog=imported.layout.catalog,
                    config=config,
                )
            )
        except (RecursionError, RuntimeError, ValueError) as error:
            warnings.append(
                f"source-step analysis skipped for section {section.name}: {error}"
            )
            continue
        payload[section.name] = rows
        warnings.extend(
            (
                f"source step {row['step']} is not feasible"
                if section.is_root
                else f"source section {section.name} step {row['step']} is not feasible"
            )
            for row in rows
            if row["evaluated"] and row["feasible"] is False
        )
    return payload, issues, tuple(warnings)


def _analyze_root_instruction_section(
    imported: ImportedLdrawModel,
    section: InstructionSection,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], ...]:
    if imported.model_analysis is None:
        return ()
    brick_by_occurrence = {
        source.occurrence: brick_id for brick_id, source in imported.source_refs.items()
    }
    step_by_outer_piece = {
        id(piece): step.number for step in section.steps for piece in step.added_pieces
    }
    grouped: dict[int, list[int]] = {}
    for index, occurrence in enumerate(
        imported.model_analysis.occurrences,
        start=1,
    ):
        if index not in brick_by_occurrence or not occurrence.path:
            continue
        step = step_by_outer_piece[id(occurrence.path[0].piece)]
        grouped.setdefault(step, []).append(brick_by_occurrence[index])
    groups = tuple(
        (step.number, tuple(grouped.get(step.number, ()))) for step in section.steps
    )
    return _analyze_step_groups(
        imported.layout,
        groups,
        section=section.name,
        ground_offset_layers=imported.ground_offset_layers,
        config=config,
    )


def _analyze_local_instruction_section(
    section: InstructionSection,
    *,
    catalog: Catalog,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], ...]:
    occurrences: list[ModelOccurrence] = []
    indexes_by_step: list[tuple[int, tuple[int, ...]]] = []
    for step in section.steps:
        added = step.added_occurrences()
        start = len(occurrences) + 1
        occurrences.extend(added)
        indexes_by_step.append((step.number, tuple(range(start, start + len(added)))))
    decoded = _import_occurrences(
        occurrences,
        catalog=catalog,
        ground=True,
        default_model=section.name,
        allow_main_colour=True,
    )
    if decoded.problems:
        message = "; ".join(problem.summary for problem in decoded.problems)
        raise ValueError(message)
    groups = tuple(
        (
            step,
            tuple(
                decoded.occurrence_bricks[index]
                for index in indexes
                if index in decoded.occurrence_bricks
            ),
        )
        for step, indexes in indexes_by_step
    )
    return _analyze_step_groups(
        decoded.layout,
        groups,
        section=section.name,
        ground_offset_layers=decoded.ground_offset_layers,
        config=config,
    )


def _repair_payload(
    imported: ImportedLdrawModel,
    physics: _PhysicsRun,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], Layout | None]:
    payload: dict[str, Any] = {
        "status": "not_needed" if physics.feasible else "disabled",
    }
    if physics.feasible or not config.repair:
        return payload, None
    from legolization.redesign import search_repair  # noqa: PLC0415 - cycle

    try:
        result = search_repair(
            imported.layout,
            physics_seed_ids=_physics_seed_ids(physics),
            parity_solver=config.parity_solver,
            strict_solver=config.strict_solver,
            time_budget_s=config.repair_time_budget_s,
            seed=config.seed,
        )
    except Exception as error:  # noqa: BLE001 - report worker start failures
        return (
            {
                "status": "error",
                "error": f"{type(error).__name__}: {error}",
                "before_metrics": _physics_metrics(physics),
            },
            None,
        )
    return (
        {
            **result.to_report(imported.source_refs),
            "before_metrics": _physics_metrics(physics),
        },
        result.layout,
    )


def _status_for(
    repair_payload: dict[str, Any],
    *,
    catalog_is_degraded: bool = False,
) -> Literal["complete", "partial"]:
    """Preserve a determined verdict while marking incomplete repair evidence."""
    if catalog_is_degraded:
        return "partial"
    match repair_payload.get("status"), repair_payload.get("timed_out"):
        case ("error", _) | (_, True):
            return "partial"
        case _:
            return "complete"


def write_analysis_report(report: AnalysisReport, path: Path) -> None:
    """Atomically write a JSON report, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(report.to_json())
    temporary.replace(path)


def _run_physics(layout: Layout, config: AnalysisConfig) -> _PhysicsRun:
    graph = ConnectionGraph.from_layout(layout)
    parity = analyze(layout=layout, config=config.parity_solver, graph=graph)
    strict = analyze(layout=layout, config=config.strict_solver, graph=graph)
    maximin = solve_maximin(
        build_model_from_config(
            layout=layout,
            config=config.strict_solver,
            graph=graph,
        )
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
        for link in physics.links.links[:_MAX_LOCALIZED_SEED_LINKS]:
            seeds |= {brick_id for brick_id in (link.a_id, link.b_id) if brick_id >= 0}
    return tuple(sorted(seeds))


def _analyze_step_groups(
    layout: Layout,
    groups: tuple[tuple[int, tuple[int, ...]], ...],
    *,
    section: str,
    ground_offset_layers: int,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], ...]:
    solver = (
        PrefixSolver.create(layout=layout, config=config.strict_solver)
        if layout.bricks
        else None
    )
    prefix = layout.subset(())
    topology = _PrefixTopology.from_layout(layout)
    rows: list[dict[str, Any]] = []
    previous: StabilityResult | None = None
    for step, brick_ids in groups:
        chunk = tuple(sorted(brick_ids))
        geometry_changed = bool(chunk)
        if geometry_changed:
            if solver is None:
                _extend_prefix_layout(prefix, layout, chunk)
                result = analyze(layout=prefix, config=config.strict_solver)
            else:
                result = solver.probe(chunk)
            topology.extend(chunk)
            if solver is not None:
                solver.commit(chunk)
            previous = result
        else:
            result = previous
        feasible = (
            result is not None
            and result.stable
            and topology.component_count == 1
            and topology.floating_count == 0
        )
        rows.append(
            {
                "section": section,
                "step": step,
                "brick_ids": list(chunk),
                "brick_count": len(topology.present),
                "geometry_changed": geometry_changed,
                "evaluated": result is not None,
                "stable": result.stable if result is not None else None,
                "max_score": result.max_score if result is not None else None,
                "component_count": topology.component_count,
                "floating_count": topology.floating_count,
                "feasible": feasible if result is not None else None,
                "ground_offset_layers": ground_offset_layers,
            }
        )
    return tuple(rows)


def _extend_prefix_layout(
    prefix: Layout,
    complete: Layout,
    brick_ids: tuple[int, ...],
) -> None:
    """Append original id-preserving bricks to a growing prefix layout."""
    for brick_id in brick_ids:
        brick = complete.bricks[brick_id]
        prefix.bricks[brick_id] = brick
        prefix.occupancy.update(dict.fromkeys(complete.cells_of(brick), brick_id))


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
        "path": [
            {
                "model": item.source_model,
                "reference": item.reference,
                "line": item.source_line,
                "local_step": item.local_step,
                "effective_step": item.effective_step,
            }
            for item in source.path
        ],
    }


def _score_payload(score: BrickScore) -> dict[str, Any]:
    return {
        "score": score.score,
        "drag_max_n": score.drag_max,
        "in_equilibrium": score.in_equilibrium,
    }


def _model_summary(
    layout: Layout,
    *,
    imported: ImportedLdrawModel | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "brick_count": len(layout),
        "mass_g": layout.total_mass_g(),
        "bounds": _bounds(layout),
    }
    if imported is None or imported.model_analysis is None:
        return payload
    summary = imported.model_analysis.summary
    payload.update(
        {
            "exact_bounds_ldu": (
                {
                    "minimum": _ldraw_vector(summary.bounds.min),
                    "maximum": _ldraw_vector(summary.bounds.max),
                    "size": _ldraw_vector(summary.bounds.size),
                }
                if summary.bounds is not None
                else None
            ),
            "size_mm": (
                _ldraw_vector(summary.size_mm) if summary.size_mm is not None else None
            ),
        }
    )
    return payload


def _ldraw_vector(vector: Vector) -> list[float]:
    return [float(vector.x), float(vector.y), float(vector.z)]


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
        "components": [sorted(components[label]) for label in sorted(components)],
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
