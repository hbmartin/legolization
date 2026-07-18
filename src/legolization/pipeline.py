"""End-to-end orchestration: grid → hollow → place → stabilize → export."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import IGNORE, VoxelGrid
from legolization.hollow import hollow_grid, restore_columns
from legolization.instructions.bom import bill_of_materials
from legolization.instructions.sequencer import (
    InstructionPlan,
    InstructionsConfig,
    plan_instructions,
)
from legolization.ldraw_out import write_model
from legolization.placement.base import ObjectiveWeights
from legolization.placement.merge import final_remerge, resolve_ignore_colours
from legolization.placement.repair import RepairConfig, repair_stability
from legolization.placement.slopes import apply_slopes, apply_tiles
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.catalog import Catalog
    from legolization.layout import Layout
    from legolization.placement.base import PlacementStrategy


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Everything tunable about a pipeline run."""

    strategy: str = "greedy"
    hollow: bool = True
    hollow_rounds: int = 5
    hollow_restore_radius: int = 2
    slopes: bool = False
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
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    solver: SolverConfig = field(default_factory=SolverConfig)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A finished layout plus its physics verdict and summary numbers."""

    layout: Layout
    stability: StabilityResult
    grid: VoxelGrid
    brick_count: int
    mass_g: float
    component_count: int
    floating_count: int
    slopes_added: int = 0
    tiles_added: int = 0
    plan: InstructionPlan | None = None

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
    if config.hollow:
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

    if final_remerge(
        layout,
        working,
        rng,
        weights=config.weights,
        solver_config=config.solver,
    ):
        stability = analyze(layout, config.solver)
    resolve_ignore_colours(layout)

    slopes_added = apply_slopes(layout, working) if config.slopes else 0
    tiles_added = apply_tiles(layout) if config.tiles else 0
    if slopes_added or tiles_added:
        stability = analyze(layout, config.solver)

    plan: InstructionPlan | None = None
    if config.instructions.mode == "smart":
        instructions_config = (
            config.instructions
            if config.instructions.solver is not None
            else replace(config.instructions, solver=config.solver)
        )
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
        plan=plan,
    )


def load_grid(input_path: Path, config: PipelineConfig | None = None) -> VoxelGrid:
    """Load a ``.vox``/``.npy`` grid using the config's voxelization knobs."""
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
        case suffix:
            msg = f"unsupported input format {suffix!r} (expected .vox or .npy)"
            raise ValueError(msg)


def write_outputs(
    result: PipelineResult,
    output_path: Path,
    *,
    bom_path: Path | None = None,
) -> None:
    """Write the ``.ldr``/``.mpd`` model and, when requested, the BOM.

    ``bom_path`` writes the bill of materials (JSON when the suffix is
    ``.json``, text otherwise).
    """
    write_model(result.layout, output_path, plan=result.plan)
    if bom_path is not None:
        bom = (
            result.plan.bom
            if result.plan is not None
            else bill_of_materials(result.layout)
        )
        if bom_path.suffix.lower() == ".json":
            bom_path.write_text(bom.to_json(model_name=output_path.name) + "\n")
        else:
            bom_path.write_text(bom.to_text() + "\n")


def run_file(
    input_path: Path,
    output_path: Path,
    config: PipelineConfig | None = None,
    *,
    bom_path: Path | None = None,
) -> PipelineResult:
    """Load a ``.vox``/``.npy`` grid, run the pipeline, write ``.ldr``/``.mpd``.

    ``bom_path`` additionally writes the bill of materials (JSON when the
    suffix is ``.json``, text otherwise).
    """
    config = config or PipelineConfig()
    result = run(grid=load_grid(input_path, config), config=config)
    write_outputs(result, output_path, bom_path=bom_path)
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
    layout = strategy.place(grid, rng=rng)
    stability = analyze(layout, config.solver)
    if config.repair and not stability.stable:
        repair_stability(
            layout,
            grid,
            catalog=catalog,
            solver_config=config.solver,
            rng=rng,
            config=config.repair_config,
        )
        stability = analyze(layout, config.solver)
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
