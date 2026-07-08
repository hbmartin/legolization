"""Placement strategy interface and the configurable weighted objective.

The objective is a weighted sum (all terms normalized to ~[0, 1], lower is
better): part cost, physical instability (from the RBE), aesthetics (seam
bonding), and colour fidelity. Strategies use it to accept or reject
refinement steps; the CLI reports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from legolization.graph import ConnectionGraph
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    import numpy as np

    from legolization.grid import VoxelGrid
    from legolization.layout import Layout


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Relative importance of the four objective terms."""

    cost: float = 1.0
    stability: float = 4.0
    aesthetics: float = 0.5
    colour: float = 1.0
    # Kollsker stretcher-bond constants for the aesthetics term.
    bond_alpha1: float = 4.0
    bond_alpha2: float = 0.8


@dataclass(frozen=True, slots=True)
class ObjectiveReport:
    """Scored terms for one layout (all lower-is-better)."""

    cost: float
    instability: float
    aesthetics: float
    colour_error: float
    total: float
    stability: StabilityResult


def evaluate(
    layout: Layout,
    grid: VoxelGrid,
    weights: ObjectiveWeights | None = None,
    solver_config: SolverConfig | None = None,
) -> ObjectiveReport:
    """Score a layout against the weighted objective."""
    weights = weights or ObjectiveWeights()
    stability = analyze(layout, solver_config)
    voxels = max(grid.filled_count, 1)
    cost = len(layout) / voxels
    instability = stability.max_score
    aesthetics = _seam_alignment(layout)
    colour_error = _colour_mismatch(layout, grid.codes)
    total = (
        weights.cost * cost
        + weights.stability * instability
        + weights.aesthetics * aesthetics
        + weights.colour * colour_error
    )
    return ObjectiveReport(
        cost=cost,
        instability=instability,
        aesthetics=aesthetics,
        colour_error=colour_error,
        total=total,
        stability=stability,
    )


class PlacementStrategy(Protocol):
    """Turns a voxel grid into a placed-brick layout."""

    def place(
        self,
        grid: VoxelGrid,
        *,
        rng: np.random.Generator,
    ) -> Layout:
        """Produce a layout covering every filled voxel of ``grid``."""
        ...


def connection_density(layout: Layout) -> float:
    """Mean distinct vertical partners per brick (higher = better bonded)."""
    if not len(layout):
        return 0.0
    graph = ConnectionGraph.from_layout(layout)
    partners = set(graph.support_edges())
    return 2.0 * len(partners) / len(layout)


def _seam_alignment(layout: Layout) -> float:
    """Fraction of intra-layer seams repeated directly one layer up.

    Stacked (aligned) seams are structurally and visually weak — the classic
    stack-bond smell the stretcher-bond term penalizes locally.
    """
    seams: set[tuple[int, int, int, int]] = set()
    for brick in layout:
        own = brick.brick_id
        for x, y, z in layout.cells_of(brick):
            for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
                neighbour = layout.brick_at((x + dx, y + dy, z))
                if neighbour is not None and neighbour.brick_id != own:
                    seams.add((x, y, z, axis))
    if not seams:
        return 0.0
    repeated = sum(1 for (x, y, z, axis) in seams if (x, y, z + 1, axis) in seams)
    return repeated / len(seams)


def _colour_mismatch(layout: Layout, codes: np.ndarray) -> float:
    """Fraction of covered voxels whose brick colour differs from the grid."""
    total = 0
    wrong = 0
    nx, ny, nz = codes.shape
    for brick in layout:
        for x, y, z in layout.filled_cells_of(brick):
            if 0 <= x < nx and 0 <= y < ny and 0 <= z < nz:
                target = int(codes[x, y, z])
                if target >= 0:
                    total += 1
                    if target != brick.colour_code:
                        wrong += 1
    return wrong / total if total else 0.0
