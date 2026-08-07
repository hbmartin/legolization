"""End-to-end orchestration: grid → hollow → place → stabilize → export."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization import telemetry
from legolization.catalog import catalog_hash, default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import IGNORE, VoxelGrid
from legolization.hollow import hollow_grid, restore_columns
from legolization.instructions.bom import bill_of_materials
from legolization.instructions.booklet import (
    ModelStats,
    validate_booklet_path,
    write_booklet,
)
from legolization.instructions.render import render_step_images
from legolization.instructions.sequencer import (
    InstructionPlan,
    InstructionsConfig,
    plan_instructions,
)
from legolization.instructions.verification import (
    InstructionCertification,
    certify_instructions,
)
from legolization.ldraw_out import write_model
from legolization.mesh import MESH_SUFFIXES, MeshOptions, mesh_to_grid
from legolization.placement.base import ObjectiveWeights
from legolization.placement.merge import final_remerge, resolve_ignore_colours
from legolization.placement.repair import RepairConfig, repair_stability
from legolization.placement.slopes import (
    apply_plate_caps,
    apply_slopes,
    apply_tiles,
)
from legolization.placement.snot import apply_snot
from legolization.placement.templates import (
    TemplateContext,
    instantiate_repeated_templates,
)
from legolization.repetition import repeated_components
from legolization.runtime import Deadline, ProgressCallback, ProgressEvent
from legolization.stability.solver import SolverConfig, StabilityResult, analyze
from legolization.support import emit_support_plate
from legolization.template_cache import TemplateCache

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.catalog import Catalog
    from legolization.instructions.booklet import Booklet
    from legolization.layout import Layout
    from legolization.placement.base import PlacementStrategy
    from legolization.placement.global_exact import ExactOutcome


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Everything tunable about a pipeline run."""

    strategy: str = "greedy"
    hollow: bool = True
    hollow_rounds: int = 5
    hollow_restore_radius: int = 2
    slopes: bool = False
    """Swap exact in-shape profile matches for slope parts."""

    tiles: bool = False
    refine: bool = True
    seed: int = 0
    plates_per_voxel: int = 3
    aspect_correct: bool = False
    shell_studs: int = 1
    shell_plates: int = 3
    ignore_interior: bool = True
    colour_mode: Literal["hard", "soft"] = "hard"
    colour_weight: float = 1.0
    dither: bool = False
    time_budget_s: float | None = None
    ga_generations: int = 200
    beauty_preset: Literal["balanced", "stability", "aesthetics", "efficiency"] = (
        "balanced"
    )
    progress: ProgressCallback | None = None
    repair: bool = True
    repair_config: RepairConfig = field(default_factory=RepairConfig)
    instructions: InstructionsConfig = field(default_factory=InstructionsConfig)
    mesh: MeshOptions = field(default_factory=MeshOptions)
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    solver: SolverConfig = field(default_factory=SolverConfig)

    # Fields below are appended after the 0.2.0 layout so positional
    # callers keep their meaning (PR #17 review); add new fields at the
    # end only.
    snot: bool = False
    """Clad tall flat wall faces with sideways tiles on 87087 brackets."""

    milp_layer_time_s: float = 10.0
    milp_bond_weight: float = 1.0
    connectivity_fail_max: int | None = None
    """Override every strategy's improve_connectivity fail_max (None =
    keep each class default; 0 disables the pass — drift diagnostics)."""

    milp_bridge: bool = True
    """Layered strategies bridge connectivity repairs with the
    exact-cover synthesizer before falling back to random draws; False
    restores the v4 random-only path (ablation knob)."""

    bridge_rephase: bool = False
    """Try all three plate phases in exact-cover bridge synthesis.

    Default-off until the mushroom and thin-shell end-to-end acceptance
    gates close; phase 0 preserves the historical production path.
    """

    hybrid_bridge: bool = False
    """Complete the deterministic phase-1 bridge candidate with a
    bounded hard-connectivity local MILP. Independent of ``bridge_rephase``
    and opt-in while its corpus acceptance gates mature.
    """

    exact_max_cells: int = 256
    exact_max_candidates: int = 100_000
    exact_time_limit_s: float = 60.0
    exact_limit_policy: Literal["fail", "fallback", "continue"] = "fail"
    exact_fallback_strategy: Literal["bond", "fast", "greedy"] = "bond"
    exact_auto_preflight_fallback: bool = False
    cost_objective: Literal["bricks", "mass"] = "bricks"
    plate_cap: bool = False
    emit_support: bool = False
    template_cache_enabled: bool = False
    template_cache_path: Path | None = None
    template_configuration_hash: str = ""
    template_physics_profile: str = "corrected"
    exact_max_stability_cuts: int = 256


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A finished layout plus its physics verdict and summary numbers."""

    layout: Layout
    stability: StabilityResult
    grid: VoxelGrid | None
    """The voxel grid the layout was placed from; None for imported models."""

    brick_count: int
    mass_g: float
    component_count: int
    floating_count: int
    slopes_added: int = 0
    tiles_added: int = 0

    # Appended after the 0.2.0 layout for positional compatibility.
    snot_added: int = 0

    placement_strategy: str = "unknown"
    exact_status: str | None = None
    exact_candidate_count: int = 0
    plate_caps_added: int = 0
    support_ids: tuple[int, ...] = ()
    instruction_certification: InstructionCertification | None = None
    cache_provenance: tuple[dict[str, int | str], ...] = ()
    plan: InstructionPlan | None = None

    @property
    def step_count(self) -> int:
        """Number of instruction steps (0 without a plan)."""
        return len(self.plan.steps) if self.plan is not None else 0

    @property
    def buildable(self) -> bool:
        """Return whether physics and graph checks certify the layout."""
        return (
            self.stability.stable
            and self.component_count == 1
            and self.floating_count == 0
        )


@dataclass(frozen=True, slots=True)
class _PlacementPhase:
    """Typed result from placement, repair, and exactness certification."""

    layout: Layout
    stability: StabilityResult
    exact_outcome: ExactOutcome | None = None


@dataclass(slots=True)
class _PipelineState:
    """Mutable phase state kept out of the public immutable result contract."""

    source: VoxelGrid
    working: VoxelGrid
    config: PipelineConfig
    catalog: Catalog
    rng: np.random.Generator
    deadline: Deadline | None
    placement: _PlacementPhase


def run(
    grid: VoxelGrid,
    config: PipelineConfig | None = None,
    *,
    catalog: Catalog | None = None,
) -> PipelineResult:
    """Run the full pipeline on a voxel grid.

    ``catalog`` substitutes a resolved overlay catalog for the builtin
    one (estimate sidecars applied verbatim by ``resolve_catalog``).
    """
    if grid.filled_count == 0:
        msg = "input grid contains no filled voxels"
        raise ValueError(msg)
    config = config or PipelineConfig()
    working = _prepare_grid(grid, config)
    catalog = catalog if catalog is not None else default_catalog()
    rng = np.random.default_rng(config.seed)
    deadline = Deadline.after(config.time_budget_s)
    state = _PipelineState(
        source=grid,
        working=working,
        config=config,
        catalog=catalog,
        rng=rng,
        deadline=deadline,
        placement=_place_and_repair(
            working,
            catalog,
            config,
            rng,
            deadline,
        ),
    )
    _phase_gauge("pipeline.placed", state.placement.layout, state.placement.stability)
    _restore_hollow_columns(state)
    _remerge(state)
    return _complete_pipeline(state)


def _prepare_grid(grid: VoxelGrid, config: PipelineConfig) -> VoxelGrid:
    with telemetry.span("phase.hollow"):
        working = (
            hollow_grid(
                grid,
                shell_studs=config.shell_studs,
                shell_plates=config.shell_plates,
            )
            if config.hollow
            else grid
        )
        return _ignore_interior(working) if config.ignore_interior else working


def _restore_hollow_columns(state: _PipelineState) -> None:
    if not state.config.hollow:
        return
    with telemetry.span("phase.hollow_restore"):
        for rounds in range(state.config.hollow_rounds):
            if state.placement.stability.stable:
                break
            if state.deadline is not None and state.deadline.expired:
                telemetry.value("pipeline.hollow_restore.deadline_stop", rounds)
                break
            restored = _restore_once(state)
            if restored is state.working:
                break
            state.working = restored
            state.placement = _place_and_repair(
                restored,
                state.catalog,
                state.config,
                state.rng,
                state.deadline,
            )
            telemetry.value("pipeline.hollow_restore.round", rounds + 1)
            _phase_gauge(
                "pipeline.restored",
                state.placement.layout,
                state.placement.stability,
            )


def _restore_once(state: _PipelineState) -> VoxelGrid:
    trouble = {
        cell
        for brick_id in state.placement.stability.unstable_ids
        for cell in state.placement.layout.cells_of(
            state.placement.layout.bricks[brick_id]
        )
    }
    return restore_columns(
        state.source,
        state.working,
        trouble,
        radius=state.config.hollow_restore_radius,
    )


def _remerge(state: _PipelineState) -> None:
    with telemetry.span("phase.remerge"):
        if final_remerge(
            state.placement.layout,
            state.working,
            state.rng,
            weights=state.config.weights,
            solver_config=state.config.solver,
        ):
            state.placement = replace(
                state.placement,
                stability=analyze(state.placement.layout, state.config.solver),
            )
        resolve_ignore_colours(state.placement.layout)
        _phase_gauge(
            "pipeline.remerged",
            state.placement.layout,
            state.placement.stability,
        )


def _complete_pipeline(state: _PipelineState) -> PipelineResult:
    layout = state.placement.layout
    with telemetry.span("phase.finish_surfaces"):
        finishing = _finish_surfaces(
            layout=layout,
            working=state.working,
            stability=state.placement.stability,
            config=state.config,
        )
    stability, slopes, tiles, snot, plate_caps = finishing
    stability, cache_provenance = _canonicalize_templates(
        layout=layout,
        grid=state.working,
        stability=stability,
        config=state.config,
    )
    support_ids, stability = _add_support(layout, stability, state.config)
    plan, certification = _instruction_plan(layout, state.config)
    graph = ConnectionGraph.from_layout(layout)
    return PipelineResult(
        layout=layout,
        stability=stability,
        grid=state.working,
        brick_count=len(layout),
        mass_g=layout.total_mass_g(),
        component_count=graph.component_count(),
        floating_count=len(graph.floating_ids()),
        slopes_added=slopes,
        tiles_added=tiles,
        snot_added=snot,
        plan=plan,
        placement_strategy=_selected_strategy(state),
        exact_status=(
            state.placement.exact_outcome.status
            if state.placement.exact_outcome is not None
            else None
        ),
        exact_candidate_count=(
            state.placement.exact_outcome.candidate_count
            if state.placement.exact_outcome is not None
            else 0
        ),
        plate_caps_added=plate_caps,
        support_ids=support_ids,
        instruction_certification=certification,
        cache_provenance=cache_provenance,
    )


def _canonicalize_templates(
    layout: Layout,
    grid: VoxelGrid,
    stability: StabilityResult,
    config: PipelineConfig,
) -> tuple[StabilityResult, tuple[dict[str, int | str], ...]]:
    repeated = repeated_components(grid)
    if not repeated:
        return stability, ()
    cache = None
    if config.template_cache_enabled:
        cache = (
            TemplateCache(root=config.template_cache_path)
            if config.template_cache_path is not None
            else TemplateCache.default()
        )
    guard = layout.copy()
    applications = instantiate_repeated_templates(
        layout,
        repeated,
        cache=cache,
        context=TemplateContext(
            catalog_hash=catalog_hash(),
            configuration_hash=config.template_configuration_hash,
            physics_profile=config.template_physics_profile,
        ),
    )
    if all(item.status == "skipped" for item in applications):
        return stability, tuple(asdict(item) for item in applications)
    certified = analyze(layout, config.solver)
    if stability.stable and not certified.stable:
        layout.replace_with(guard)
        rejected = tuple(
            asdict(item)
            if item.status == "skipped"
            else {**asdict(item), "status": "rejected_stability"}
            for item in applications
        )
        return stability, rejected
    return certified, tuple(asdict(item) for item in applications)


def _selected_strategy(state: _PipelineState) -> str:
    outcome = state.placement.exact_outcome
    if outcome is not None and outcome.status == "fallback":
        return state.config.exact_fallback_strategy
    return state.config.strategy


def _add_support(
    layout: Layout,
    stability: StabilityResult,
    config: PipelineConfig,
) -> tuple[tuple[int, ...], StabilityResult]:
    if not config.emit_support:
        return (), stability
    with telemetry.span("phase.emit_support"):
        support_ids = emit_support_plate(layout)
        return support_ids, analyze(layout, config.solver)


def _instruction_plan(
    layout: Layout,
    config: PipelineConfig,
) -> tuple[InstructionPlan | None, InstructionCertification | None]:
    if config.instructions.mode != "smart":
        return None, None
    instructions_config = (
        config.instructions
        if config.instructions.solver is not None
        else replace(config.instructions, solver=config.solver)
    )
    with telemetry.span("phase.sequencing"):
        plan = plan_instructions(layout, config=instructions_config)
    with telemetry.span("phase.sequencing.certify"):
        certification = certify_instructions(
            layout,
            plan,
            config=instructions_config,
        )
    return plan, certification


@dataclass(frozen=True, slots=True)
class _FinishOperation:
    operation: Callable[[], int]
    span_name: str
    warning: str


def _finish_surfaces(
    layout: Layout,
    working: VoxelGrid,
    stability: StabilityResult,
    config: PipelineConfig,
) -> tuple[StabilityResult, int, int, int, int]:
    """Run the opt-in slope/tile/snot finishing passes; re-analyze if used."""
    slopes_added = 0
    if config.slopes:
        slopes_added, stability = _guarded_finish(
            layout,
            stability,
            config,
            finish=_FinishOperation(
                operation=lambda: apply_slopes(layout, working),
                span_name="finish.slopes",
                warning="slopes: preserve pass would break stability; reverted",
            ),
        )
    snot_added = 0
    if config.snot:
        with telemetry.span("finish.snot"):
            snot_added, stability = _snot_tiers(layout, working, config, stability)
    plate_caps_added = 0
    if config.plate_cap:
        plate_caps_added, stability = _guarded_finish(
            layout,
            stability,
            config,
            finish=_FinishOperation(
                operation=lambda: apply_plate_caps(layout),
                span_name="finish.plate_caps",
                warning="plate caps: pass would break stability; reverted",
            ),
        )
    with telemetry.span("finish.tiles"):
        tiles_added = apply_tiles(layout) if config.tiles else 0
    if tiles_added:
        stability = analyze(layout, config.solver)
    return stability, slopes_added, tiles_added, snot_added, plate_caps_added


def _guarded_finish(
    layout: Layout,
    stability: StabilityResult,
    config: PipelineConfig,
    *,
    finish: _FinishOperation,
) -> tuple[int, StabilityResult]:
    guard = (layout.copy(), stability) if stability.stable else None
    with telemetry.span(finish.span_name):
        added = finish.operation()
    if not added:
        return 0, stability
    updated = analyze(layout, config.solver)
    if guard is None or updated.stable:
        return added, updated
    layout.replace_with(guard[0])
    if config.progress is not None:
        config.progress(
            ProgressEvent(
                finish.warning,
                phase=finish.span_name,
                level="warning",
            )
        )
    return 0, guard[1]


def _snot_tiers(
    layout: Layout,
    working: VoxelGrid,
    config: PipelineConfig,
    stability: StabilityResult,
) -> tuple[int, StabilityResult]:
    """Run the cladding pass in two stability-checkpointed tiers.

    Tier one mounts only sites whose donors live inside their own
    columns (v1's conservative carve — never weakens wall bonding) and
    is reverted wholesale if it breaks stability. Tier two re-runs the
    pass with wall-spanning donors allowed; already-clad windows fail
    their plans, so only the bolder mounts are new. If those break
    stability the layout retreats to the tier-one checkpoint instead of
    losing every mount — one bad wall carve must not cost the safe
    cladding (measured on mushroom: 86 accepted mounts, all reverted).
    """
    guard = (layout.copy(), stability) if stability.stable else None
    snot_added = apply_snot(layout, working, spanning_donors=False)
    if snot_added:
        stability = analyze(layout, config.solver)
        if guard is not None and not stability.stable:
            layout.replace_with(guard[0])
            stability = guard[1]
            snot_added = 0
            if config.progress is not None:
                config.progress(
                    ProgressEvent(
                        "snot: cladding pass would break stability; reverted",
                        phase="finish.snot",
                        level="warning",
                    )
                )
    checkpoint = (layout.copy(), stability) if stability.stable else None
    bold_added = apply_snot(layout, working, spanning_donors=True)
    if bold_added:
        stability = analyze(layout, config.solver)
        if checkpoint is not None and not stability.stable:
            layout.replace_with(checkpoint[0])
            stability = checkpoint[1]
            if config.progress is not None:
                config.progress(
                    ProgressEvent(
                        "snot: wall-carving tier would break stability; "
                        "kept the conservative tier",
                        phase="finish.snot",
                        level="warning",
                    )
                )
        else:
            snot_added += bold_added
    return snot_added, stability


def _phase_gauge(
    name: str,
    layout: Layout,
    stability: StabilityResult,
) -> None:
    """Exact per-phase readings for the count-trajectory diagnostics.

    Free when not recording; the component count builds a graph, so all
    readings are gated on an active session.
    """
    if telemetry.current() is None:
        return
    telemetry.value(f"{name}.bricks", len(layout))
    telemetry.value(f"{name}.stable", 1.0 if stability.stable else 0.0)
    telemetry.value(
        f"{name}.components",
        ConnectionGraph.from_layout(layout).component_count(),
    )


def load_grid(input_path: Path, config: PipelineConfig | None = None) -> VoxelGrid:
    """Load a ``.vox``/``.npy``/mesh grid using the config's voxelization knobs."""
    config = config or PipelineConfig()
    match input_path.suffix.lower():
        case ".vox":
            return VoxelGrid.from_vox(
                input_path,
                plates_per_voxel=config.plates_per_voxel,
                dither=config.dither,
                aspect_correct=config.aspect_correct,
            )
        case ".npy":
            return VoxelGrid.from_npy(
                input_path,
                plates_per_voxel=config.plates_per_voxel,
                dither=config.dither,
                aspect_correct=config.aspect_correct,
            )
        case suffix if suffix in MESH_SUFFIXES:
            return mesh_to_grid(
                input_path,
                options=config.mesh,
                progress=config.progress,
            )
        case suffix:
            msg = (
                f"unsupported input format {suffix!r} "
                f"(expected .vox, .npy, .obj, .stl, or .ply)"
            )
            raise ValueError(msg)


def write_outputs(
    result: PipelineResult,
    output_path: Path,
    *,
    bom_path: Path | None = None,
    instructions_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Booklet | None:
    """Write the model plus, when requested, the BOM and instruction booklet.

    ``bom_path`` writes the bill of materials (JSON when the suffix is
    ``.json``, text otherwise). ``instructions_path`` renders per-step images
    of the just-written model and writes a booklet (``.html`` or ``.pdf``);
    without a renderer installed the booklet gets placeholder boxes.
    """
    plan = result.plan
    if instructions_path is not None:
        if plan is None:
            msg = 'an instruction booklet needs smart steps (not steps mode "layer")'
            raise ValueError(msg)
        validate_booklet_path(instructions_path)
    write_model(result.layout, output_path, plan=plan)
    if bom_path is not None:
        bom = plan.bom if plan is not None else bill_of_materials(result.layout)
        if bom_path.suffix.lower() == ".json":
            bom_path.write_text(bom.to_json(model_name=output_path.name) + "\n")
        else:
            bom_path.write_text(bom.to_text() + "\n")
    if instructions_path is None or plan is None:
        return None
    images = render_step_images(
        model_path=output_path,
        plan=plan,
        progress=progress,
    )
    booklet = write_booklet(
        plan=plan,
        stats=ModelStats(
            name=output_path.stem,
            brick_count=result.brick_count,
            mass_g=result.mass_g,
            step_count=result.step_count,
            stable=result.stability.stable,
            buildable=result.buildable,
            component_count=result.component_count,
            floating_count=result.floating_count,
        ),
        images=images,
        path=instructions_path,
    )
    if progress is not None:
        for warning in images.warnings:
            progress(
                ProgressEvent(
                    f"warning: {warning}",
                    phase="instructions.render",
                    level="warning",
                )
            )
    return booklet


def run_file(  # noqa: PLR0913 - optional artifact paths + the preloaded grid
    input_path: Path,
    output_path: Path,
    config: PipelineConfig | None = None,
    *,
    bom_path: Path | None = None,
    instructions_path: Path | None = None,
    grid: VoxelGrid | None = None,
    catalog: Catalog | None = None,
) -> PipelineResult:
    """Load a ``.vox``/``.npy`` grid, run the pipeline, write ``.ldr``/``.mpd``.

    ``bom_path`` additionally writes the bill of materials (JSON when the
    suffix is ``.json``, text otherwise); ``instructions_path`` an
    instruction booklet (``.html`` or ``.pdf``). ``grid`` skips the
    load: the restart race already voxelized the input, and a mesh
    voxelization is not free to repeat (PR #20 review). ``catalog``
    substitutes a resolved overlay catalog for the builtin one.
    """
    config = config or PipelineConfig()
    result = run(
        grid=grid if grid is not None else load_grid(input_path, config),
        config=config,
        catalog=catalog,
    )
    write_outputs(
        result,
        output_path,
        bom_path=bom_path,
        instructions_path=instructions_path,
        progress=config.progress,
    )
    return result


def _ignore_interior(grid: VoxelGrid) -> VoxelGrid:
    """Mark interior cells colour-free so merges cross invisible boundaries."""
    interior = grid.interior_mask()
    if not interior.any():
        return grid
    codes = grid.codes.copy()
    codes[interior] = IGNORE
    return grid.with_codes(codes)


def _place_and_repair(
    grid: VoxelGrid,
    catalog: Catalog,
    config: PipelineConfig,
    rng: np.random.Generator,
    deadline: float | None = None,
) -> _PlacementPhase:
    """Place, then rearrange at constant volume before any material is added."""
    strategy = _strategy(catalog, config)
    with telemetry.span("phase.place"):
        layout = strategy.place(grid, rng=rng, deadline=deadline)
    stability = analyze(layout, config.solver)
    if config.repair and not stability.stable:
        with telemetry.span("phase.repair"):
            report = repair_stability(
                layout,
                grid,
                catalog=catalog,
                solver_config=config.solver,
                rng=rng,
                config=config.repair_config,
                deadline=deadline,
                initial_stability=stability,
            )
            stability = (
                report.result
                if report.result is not None
                else analyze(layout, config.solver)
            )
            _phase_gauge("pipeline.repaired", layout, stability)
    return _PlacementPhase(
        layout=layout,
        stability=stability,
        exact_outcome=strategy.outcome,
    )


def _strategy(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    from legolization.placement.registry import make_strategy  # noqa: PLC0415 - cycle

    return make_strategy(config.strategy, catalog=catalog, config=config)


__all__ = [
    "PipelineConfig",
    "PipelineResult",
    "load_grid",
    "run",
    "run_file",
    "write_outputs",
]
