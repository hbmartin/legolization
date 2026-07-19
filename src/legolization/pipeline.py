"""End-to-end orchestration: grid → hollow → place → stabilize → export."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization import telemetry
from legolization.catalog import default_catalog
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
from legolization.ldraw_out import write_model
from legolization.mesh import MESH_SUFFIXES, MeshOptions, mesh_to_grid
from legolization.placement.base import ObjectiveWeights
from legolization.placement.merge import final_remerge, resolve_ignore_colours
from legolization.placement.repair import RepairConfig, repair_stability
from legolization.placement.slopes import SlopeMode, apply_slopes, apply_tiles
from legolization.placement.snot import apply_snot
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.catalog import Catalog
    from legolization.instructions.booklet import Booklet
    from legolization.layout import Layout
    from legolization.placement.base import PlacementStrategy


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Everything tunable about a pipeline run."""

    strategy: str = "greedy"
    hollow: bool = True
    hollow_rounds: int = 5
    hollow_restore_radius: int = 2
    slopes: bool | SlopeMode = False
    """``"preserve"`` swaps exact in-shape profile matches (adds nothing);
    ``"smooth"`` (or legacy ``True``) adds slopes outside the shape."""

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
    progress: Callable[[str], None] | None = None
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
    plan: InstructionPlan | None = None

    # Appended after the 0.2.0 layout for positional compatibility
    # (PR #17 review); add new fields at the end only.
    snot_added: int = 0

    @property
    def step_count(self) -> int:
        """Number of instruction steps (0 without a plan)."""
        return len(self.plan.steps) if self.plan is not None else 0

    @property
    def buildable(self) -> bool:
        """Stable, single brick-graph component, and nothing floating.

        Components count stud connectivity between bricks only: two
        grounded but disconnected towers are NOT buildable as one model.
        """
        return (
            self.stability.stable
            and self.component_count == 1
            and self.floating_count == 0
        )


def run(grid: VoxelGrid, config: PipelineConfig | None = None) -> PipelineResult:
    """Run the full pipeline on a voxel grid."""
    if grid.filled_count == 0:
        msg = "input grid contains no filled voxels"
        raise ValueError(msg)
    config = config or PipelineConfig()
    catalog = default_catalog()
    rng = np.random.default_rng(config.seed)
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
        if config.ignore_interior:
            working = _ignore_interior(working)

    layout, stability = _place_and_repair(working, catalog, config, rng)
    _phase_gauge("pipeline.placed", layout, stability)
    if config.hollow:
        with telemetry.span("phase.hollow_restore"):
            rounds = 0
            while not stability.stable and rounds < config.hollow_rounds:
                trouble = {
                    cell
                    for brick_id in stability.unstable_ids
                    for cell in layout.cells_of(layout.bricks[brick_id])
                }
                restored = restore_columns(
                    grid,
                    working,
                    trouble,
                    radius=config.hollow_restore_radius,
                )
                if restored is working:
                    break
                working = restored
                layout, stability = _place_and_repair(working, catalog, config, rng)
                rounds += 1
                telemetry.value("pipeline.hollow_restore.round", rounds)
                _phase_gauge("pipeline.restored", layout, stability)

    with telemetry.span("phase.remerge"):
        if final_remerge(
            layout,
            working,
            rng,
            weights=config.weights,
            solver_config=config.solver,
        ):
            stability = analyze(layout, config.solver)
        resolve_ignore_colours(layout)
        _phase_gauge("pipeline.remerged", layout, stability)

    with telemetry.span("phase.finish_surfaces"):
        stability, slopes_added, tiles_added, snot_added = _finish_surfaces(
            layout, working, stability, config
        )

    plan: InstructionPlan | None = None
    if config.instructions.mode == "smart":
        instructions_config = (
            config.instructions
            if config.instructions.solver is not None
            else replace(config.instructions, solver=config.solver)
        )
        with telemetry.span("phase.sequencing"):
            plan = plan_instructions(layout, config=instructions_config)

    graph = ConnectionGraph.from_layout(layout)
    return PipelineResult(
        layout=layout,
        stability=stability,
        grid=working,
        brick_count=len(layout),
        mass_g=layout.total_mass_g(),
        component_count=graph.component_count(),
        floating_count=len(graph.floating_ids()),
        slopes_added=slopes_added,
        tiles_added=tiles_added,
        snot_added=snot_added,
        plan=plan,
    )


def _finish_surfaces(
    layout: Layout,
    working: VoxelGrid,
    stability: StabilityResult,
    config: PipelineConfig,
) -> tuple[StabilityResult, int, int, int]:
    """Run the opt-in slope/tile/snot finishing passes; re-analyze if used."""
    slope_mode: SlopeMode | None = (
        "smooth" if config.slopes is True else config.slopes or None
    )
    slopes_added = 0
    if slope_mode is not None:
        # Preserve mode must never trade stability for looks: carving
        # donors fragments load paths, so keep a snapshot to revert to.
        guard = (
            (layout.copy(), stability)
            if slope_mode == "preserve" and stability.stable
            else None
        )
        with telemetry.span("finish.slopes"):
            slopes_added = apply_slopes(layout, working, mode=slope_mode)
        if slopes_added:
            stability = analyze(layout, config.solver)
            if guard is not None and not stability.stable:
                layout.replace_with(guard[0])
                stability = guard[1]
                slopes_added = 0
                if config.progress is not None:
                    config.progress(
                        "slopes: preserve pass would break stability; reverted"
                    )
    snot_added = 0
    if config.snot:
        with telemetry.span("finish.snot"):
            snot_added, stability = _snot_tiers(layout, working, config, stability)
    with telemetry.span("finish.tiles"):
        tiles_added = apply_tiles(layout) if config.tiles else 0
    if tiles_added:
        stability = analyze(layout, config.solver)
    return stability, slopes_added, tiles_added, snot_added


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
                config.progress("snot: cladding pass would break stability; reverted")
    checkpoint = (layout.copy(), stability) if stability.stable else None
    bold_added = apply_snot(layout, working, spanning_donors=True)
    if bold_added:
        stability = analyze(layout, config.solver)
        if checkpoint is not None and not stability.stable:
            layout.replace_with(checkpoint[0])
            stability = checkpoint[1]
            if config.progress is not None:
                config.progress(
                    "snot: wall-carving tier would break stability; "
                    "kept the conservative tier"
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
    progress: Callable[[str], None] | None = None,
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
            progress(f"warning: {warning}")
    return booklet


def run_file(
    input_path: Path,
    output_path: Path,
    config: PipelineConfig | None = None,
    *,
    bom_path: Path | None = None,
    instructions_path: Path | None = None,
) -> PipelineResult:
    """Load a ``.vox``/``.npy`` grid, run the pipeline, write ``.ldr``/``.mpd``.

    ``bom_path`` additionally writes the bill of materials (JSON when the
    suffix is ``.json``, text otherwise); ``instructions_path`` an
    instruction booklet (``.html`` or ``.pdf``).
    """
    config = config or PipelineConfig()
    result = run(grid=load_grid(input_path, config), config=config)
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
) -> tuple[Layout, StabilityResult]:
    """Place, then rearrange at constant volume before any material is added."""
    strategy = _strategy(catalog, config)
    with telemetry.span("phase.place"):
        layout = strategy.place(grid, rng=rng)
    stability = analyze(layout, config.solver)
    if config.repair and not stability.stable:
        with telemetry.span("phase.repair"):
            repair_stability(
                layout,
                grid,
                catalog=catalog,
                solver_config=config.solver,
                rng=rng,
                config=config.repair_config,
            )
            stability = analyze(layout, config.solver)
            _phase_gauge("pipeline.repaired", layout, stability)
    return layout, stability


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
