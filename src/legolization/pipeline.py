"""End-to-end orchestration: grid → hollow → place → stabilize → export."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import IGNORE, VoxelGrid
from legolization.hollow import hollow_grid, restore_columns
from legolization.ldraw_out import write_model
from legolization.placement.base import ObjectiveWeights
from legolization.placement.greedy import GreedyStrategy
from legolization.placement.luo import LuoStrategy
from legolization.placement.merge import final_remerge, resolve_ignore_colours
from legolization.placement.slopes import apply_slopes, apply_tiles
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    from pathlib import Path

    from legolization.catalog import Catalog
    from legolization.layout import Layout
    from legolization.placement.base import PlacementStrategy


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Everything tunable about a pipeline run."""

    strategy: Literal["greedy", "luo"] = "greedy"
    hollow: bool = True
    hollow_rounds: int = 5
    hollow_restore_radius: int = 2
    slopes: bool = False
    tiles: bool = False
    refine: bool = True
    seed: int = 0
    plates_per_voxel: int = 3
    ignore_interior: bool = True
    colour_mode: Literal["hard", "soft"] = "hard"
    colour_weight: float = 1.0
    dither: bool = False
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

    @property
    def buildable(self) -> bool:
        """Stable, single brick-graph component, and nothing floating.

        Components count stud connectivity between bricks only: two
        grounded but disconnected towers are NOT buildable as one model.
        """
        return (
            self.stability.stable
            and self.component_count <= 1
            and self.floating_count == 0
        )


def run(grid: VoxelGrid, config: PipelineConfig | None = None) -> PipelineResult:
    """Run the full pipeline on a voxel grid."""
    config = config or PipelineConfig()
    catalog = default_catalog()
    rng = np.random.default_rng(config.seed)
    working = hollow_grid(grid) if config.hollow else grid
    if config.ignore_interior:
        working = _ignore_interior(working)

    layout, stability = _place(working, catalog, config, rng)
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
            layout, stability = _place(working, catalog, config, rng)
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
    )


def run_file(
    input_path: Path,
    output_path: Path,
    config: PipelineConfig | None = None,
) -> PipelineResult:
    """Load a ``.vox``/``.npy`` grid, run the pipeline, write ``.ldr``/``.mpd``."""
    config = config or PipelineConfig()
    match input_path.suffix.lower():
        case ".vox":
            grid = VoxelGrid.from_vox(
                input_path,
                plates_per_voxel=config.plates_per_voxel,
                dither=config.dither,
            )
        case ".npy":
            grid = VoxelGrid.from_npy(
                input_path,
                plates_per_voxel=config.plates_per_voxel,
                dither=config.dither,
            )
        case suffix:
            msg = f"unsupported input format {suffix!r} (expected .vox or .npy)"
            raise ValueError(msg)
    result = run(grid, config)
    write_model(result.layout, output_path)
    return result


def _ignore_interior(grid: VoxelGrid) -> VoxelGrid:
    """Mark interior cells colour-free so merges cross invisible boundaries."""
    interior = grid.interior_mask()
    if not interior.any():
        return grid
    codes = grid.codes.copy()
    codes[interior] = IGNORE
    return grid.with_codes(codes)


def _place(
    grid: VoxelGrid,
    catalog: Catalog,
    config: PipelineConfig,
    rng: np.random.Generator,
) -> tuple[Layout, StabilityResult]:
    strategy = _strategy(catalog, config)
    layout = strategy.place(grid, rng=rng)
    return layout, analyze(layout, config.solver)


def _strategy(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    match config.strategy:
        case "greedy":
            return GreedyStrategy(
                catalog=catalog,
                weights=config.weights,
                solver_config=config.solver,
                refine=config.refine,
            )
        case "luo":
            return LuoStrategy(
                catalog=catalog,
                solver_config=config.solver,
                colour_mode=config.colour_mode,
                colour_weight=config.colour_weight,
            )


__all__ = ["PipelineConfig", "PipelineResult", "run", "run_file"]
