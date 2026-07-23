"""Placement strategy interface and the configurable weighted objective.

The objective is a weighted sum (all terms normalized to ~[0, 1], lower is
better): part cost, physical instability (from the RBE), aesthetics (seam
bonding), colour fidelity, perpendicularity (alternating brick directions
between layers), and per-layer mirror symmetry. Strategies use it to accept
or reject refinement steps; the CLI reports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from legolization.graph import ConnectionGraph
from legolization.placement.aesthetics import perpendicularity_error, symmetry_error
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    import numpy as np

    from legolization.grid import VoxelGrid
    from legolization.layout import Layout


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Relative importance of the objective terms."""

    cost: float = 1.0
    stability: float = 4.0
    aesthetics: float = 0.5
    colour: float = 1.0
    perpendicularity: float = 0.25
    symmetry: float = 0.25
    # Kollsker stretcher-bond constants used by greedy candidate scoring:
    # a border whose seam below sits d studs away is penalized
    # ``bond_alpha1 * exp(-bond_alpha2 * d)`` (d = 0 is a stacked seam).
    bond_alpha1: float = 4.0
    bond_alpha2: float = 0.8


@dataclass(frozen=True, slots=True)
class ObjectiveReport:
    """Scored terms for one layout (all lower-is-better)."""

    cost: float
    instability: float
    aesthetics: float
    colour_error: float
    perpendicularity: float
    symmetry: float
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
    perpendicularity = perpendicularity_error(layout)
    symmetry = symmetry_error(layout)
    total = (
        weights.cost * cost
        + weights.stability * instability
        + weights.aesthetics * aesthetics
        + weights.colour * colour_error
        + weights.perpendicularity * perpendicularity
        + weights.symmetry * symmetry
    )
    return ObjectiveReport(
        cost=cost,
        instability=instability,
        aesthetics=aesthetics,
        colour_error=colour_error,
        perpendicularity=perpendicularity,
        symmetry=symmetry,
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
        deadline: float | None = None,
    ) -> Layout:
        """Produce a layout, honoring an absolute deadline when supported."""
        ...


def connection_density(layout: Layout) -> float:
    """Mean distinct vertical partners per brick (higher = better bonded)."""
    if not len(layout):
        return 0.0
    graph = ConnectionGraph.from_layout(layout)
    partners = set(graph.support_edges())
    return 2.0 * len(partners) / len(layout)


def _seam_alignment(layout: Layout) -> float:
    """Fraction of brick-pair interfaces repeated directly one plate up.

    Stacked (aligned) seams are structurally and visually weak — the classic
    stack-bond smell. A seam is counted once per touching brick pair per
    vertical run (at the run's top plate), not once per plate cell, so a
    brick-to-brick joint doesn't triple-count: the metric genuinely spans
    [0, 1] — a stretcher-bond wall scores 0.0, an n-course stack-bond wall
    (n - 1) / n.
    """
    seams: dict[tuple[int, int, int, int], tuple[int, int]] = {}
    interfaces: dict[tuple[tuple[int, int], int], set[tuple[int, int, int]]] = {}
    for brick in layout:
        own = brick.brick_id
        for x, y, z in layout.cells_of(brick):
            for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
                neighbour = layout.brick_at((x + dx, y + dy, z))
                if neighbour is not None and neighbour.brick_id != own:
                    pair = (min(own, neighbour.brick_id), max(own, neighbour.brick_id))
                    seams[(x, y, z, axis)] = pair
                    interfaces.setdefault((pair, axis), set()).add((x, y, z))
    tops = len(interfaces)
    repeated = 0
    for (_, axis), cells in interfaces.items():
        top = max(z for _, _, z in cells)
        if any((x, y, top + 1, axis) in seams for x, y, z in cells if z == top):
            repeated += 1
    return repeated / tops if tops else 0.0


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
