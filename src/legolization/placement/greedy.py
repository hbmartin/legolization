"""Greedy bottom-up placement with delete-and-rebuild reinforcement.

Placement sweeps layers bottom-up, covering each uncovered filled voxel with
the largest colour-uniform part that fits, preferring placements that bridge
many distinct bricks below and whose seams stagger against the layer below
(Kollsker's stretcher-bond term). Reinforcement then repeatedly deletes a
k-ring around the physically weakest bricks (per the RBE scores) and refills
it with a different random order, keeping changes only when the weighted
objective improves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from legolization.catalog import Category, default_catalog, rotate_offset
from legolization.layout import Layout
from legolization.placement.base import ObjectiveWeights, evaluate
from legolization.placement.merge import (
    compact_vertical,
    improve_connectivity,
    k_ring,
)
from legolization.stability.solver import SolverConfig

if TYPE_CHECKING:
    import numpy as np

    from legolization.catalog import Catalog, Cell, Part
    from legolization.grid import VoxelGrid

_RING_GROWTH = 10  # failures per extra ring (Luo's N)


@dataclass(frozen=True, slots=True)
class _Candidate:
    part: Part
    anchor: tuple[int, int, int]
    yaw: int
    cells: tuple[Cell, ...]


@dataclass(slots=True)
class GreedyStrategy:
    """Largest-first greedy placement + stability-driven rebuild loop."""

    catalog: Catalog = field(default_factory=default_catalog)
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    solver_config: SolverConfig = field(default_factory=SolverConfig)
    refine: bool = True
    fail_max: int = 20

    def place(self, grid: VoxelGrid, *, rng: np.random.Generator) -> Layout:
        """Cover the grid greedily, then reinforce until stable or exhausted."""
        layout = Layout(catalog=self.catalog)
        uncovered = {
            (int(x), int(y), int(z))
            for x, y, z in zip(*grid.filled_mask.nonzero(), strict=True)
        }
        self._fill(layout, grid, uncovered, rng)
        if self.refine:
            self._reinforce(layout, grid, rng)
            compact_vertical(layout)
        return layout

    def _fill(
        self,
        layout: Layout,
        grid: VoxelGrid,
        uncovered: set[Cell],
        rng: np.random.Generator,
    ) -> None:
        """Greedily cover ``uncovered`` (mutated) with largest-fitting parts."""
        parts = self.catalog.by_category(Category.BRICK, Category.PLATE)
        for seed in sorted(uncovered, key=lambda cell: (cell[2], cell[0], cell[1])):
            if seed not in uncovered:
                continue
            best: tuple[float, float, _Candidate] | None = None
            colour = grid.code_at(*seed)
            for part in parts:
                for candidate in _placements(part, seed, grid, uncovered, layout):
                    bond = self._bond_score(layout, candidate)
                    jitter = float(rng.random()) * 1e-3
                    key = (len(candidate.cells), bond + jitter)
                    if best is None or key > (best[0], best[1]):
                        best = (key[0], key[1], candidate)
            if best is None:  # always false: plate_1x1 fits any lone cell
                uncovered.discard(seed)
                continue
            candidate = best[2]
            layout.add(
                candidate.part.key,
                *candidate.anchor,
                candidate.yaw,
                colour,
            )
            uncovered.difference_update(candidate.cells)

    def _bond_score(self, layout: Layout, candidate: _Candidate) -> float:
        """Bonding quality: many distinct supports, staggered seams below."""
        below: set[int] = set()
        aligned = 0
        borders = 0
        columns = {(x, y) for x, y, _ in candidate.cells}
        base = candidate.anchor[2]
        for x, y in columns:
            if (support := layout.brick_at((x, y, base - 1))) is not None:
                below.add(support.brick_id)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (x + dx, y + dy) in columns:
                    continue
                borders += 1
                under_a = layout.brick_at((x, y, base - 1))
                under_b = layout.brick_at((x + dx, y + dy, base - 1))
                if (
                    under_a is not None
                    and under_b is not None
                    and (under_a.brick_id != under_b.brick_id)
                ):
                    aligned += 1  # seam below continues our border: d = 0
        bond_penalty = (
            self.weights.bond_alpha1 * (aligned / borders) if borders else 0.0
        )
        return len(below) - bond_penalty

    def _reinforce(
        self,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
    ) -> None:
        """Repair connectivity, then delete-and-rebuild until stable."""
        # Straight seams can strand towers no greedy refill bridges (the
        # largest-first fill would just recreate them); random remerging
        # across the seam does.
        if _floating(layout):
            improve_connectivity(layout, grid, rng, fail_max=self.fail_max)
        report = evaluate(layout, grid, self.weights, self.solver_config)
        failures = 0
        while failures < self.fail_max:
            stability = report.stability
            if stability.stable and not _floating(layout):
                return
            seeds = set(stability.unstable_ids) | _floating(layout)
            if stability.weakest_pair is not None:
                seeds |= {bid for bid in stability.weakest_pair if bid >= 0}
            if not seeds:
                return
            rings = failures // _RING_GROWTH + 1
            region = k_ring(layout, seeds, rings)
            candidate_layout = layout.copy()
            freed: set[Cell] = set()
            for brick_id in region:
                brick = candidate_layout.bricks[brick_id]
                freed |= set(candidate_layout.filled_cells_of(brick))
                candidate_layout.remove(brick_id)
            freed = {c for c in freed if _is_filled(grid, c)}
            self._fill(candidate_layout, grid, freed, rng)
            candidate_report = evaluate(
                candidate_layout, grid, self.weights, self.solver_config
            )
            if candidate_report.total < report.total:
                layout.replace_with(candidate_layout)
                report = candidate_report
                failures = 0
            else:
                failures += 1


def _placements(
    part: Part,
    seed: Cell,
    grid: VoxelGrid,
    uncovered: set[Cell],
    layout: Layout,
) -> list[_Candidate]:
    """All valid placements of ``part`` covering ``seed``."""
    colour = grid.code_at(*seed)
    results: list[_Candidate] = []
    seen_anchors: set[tuple[int, int, int, int]] = set()
    for yaw in part.orientations:
        rotated = [rotate_offset(cell, yaw) for cell in sorted(part.occupied_cells)]
        for rx, ry, rz in rotated:
            anchor = (seed[0] - rx, seed[1] - ry, seed[2] - rz)
            if anchor[2] < 0 or (*anchor, yaw) in seen_anchors:
                continue
            seen_anchors.add((*anchor, yaw))
            cells = [
                (anchor[0] + cx, anchor[1] + cy, anchor[2] + cz)
                for cx, cy, cz in rotated
            ]
            if all(
                cell in uncovered and grid.code_at(*cell) == colour for cell in cells
            ) and layout.can_place(part, *anchor, yaw):
                results.append(
                    _Candidate(
                        part=part,
                        anchor=anchor,
                        yaw=yaw,
                        cells=tuple(cells),
                    )
                )
    return results


def _floating(layout: Layout) -> set[int]:
    from legolization.graph import ConnectionGraph  # noqa: PLC0415 - cycle guard

    return set(ConnectionGraph.from_layout(layout).floating_ids())


def _is_filled(grid: VoxelGrid, cell: Cell) -> bool:
    x, y, z = cell
    nx, ny, nz = grid.shape
    return 0 <= x < nx and 0 <= y < ny and 0 <= z < nz and grid.codes[x, y, z] >= 0
