"""Greedy bottom-up placement with delete-and-rebuild reinforcement.

Placement sweeps layers bottom-up, covering each uncovered filled voxel with
the candidate that (in order) minimizes the estimated parts left for its row
(Kollsker's ``h(r)`` remainder lookahead), maximizes bonding — many distinct
supports below, seams staggered against the layer below with the full
``alpha1 * exp(-alpha2 * d)`` distance term — and then covers the most cells.
``h(r)`` rarely lowers the part count by itself (every remainder decomposes
near-optimally with lengths 1/2/3/4/6/8); its value is turning 6+1-versus-4+3
into ties that the bond term breaks toward staggered seams. Reinforcement
then repeatedly deletes a k-ring around the physically weakest bricks (per
the RBE scores) and refills it in a shuffled-within-layers random order,
keeping changes only when the weighted objective improves.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING

from legolization.catalog import Category, default_catalog, rotate_offset
from legolization.grid import EMPTY, colour_matches, merge_colour
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
_STUD_LENGTHS = (1, 2, 3, 4, 6, 8)  # catalog part lengths
_LENGTH_MAX = 8
_H_EXACT_LIMIT = 25  # Kollsker's rho: exact knapsack below, peel above
_SEAM_WINDOW = 3  # studs scanned for the nearest seam below a border


@cache
def _h_exact(remainder: int) -> int:
    """Minimum parts that exactly cover ``remainder`` studs (equality DP)."""
    if remainder == 0:
        return 0
    return 1 + min(
        _h_exact(remainder - length) for length in _STUD_LENGTHS if length <= remainder
    )


def _h_lookahead(remainder: int) -> int:
    """Kollsker's h3(r): peel 8-stud chunks, exact knapsack on the tail."""
    if remainder <= 0:
        return 0
    peeled = 0
    while remainder >= _H_EXACT_LIMIT:
        peeled += 1
        remainder -= _LENGTH_MAX
    return peeled + _h_exact(remainder)


@dataclass(frozen=True, slots=True)
class _Candidate:
    part: Part
    anchor: tuple[int, int, int]
    yaw: int
    cells: tuple[Cell, ...]
    colour: int


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
        *,
        shuffle_within_layers: bool = False,
    ) -> None:
        """Greedily cover ``uncovered`` (mutated), best candidate per seed.

        The initial fill visits seeds in a deterministic bottom-up ``(z, x,
        y)`` sweep; rebuild attempts pass ``shuffle_within_layers`` so each
        retry explores a genuinely different fill order (still bottom-up —
        the bond score reads the layer below).
        """
        parts = self.catalog.by_category(Category.BRICK, Category.PLATE)
        order_key = (
            (lambda cell: (cell[2], rng.random()))
            if shuffle_within_layers
            else (lambda cell: (cell[2], cell[0], cell[1]))
        )
        for seed in sorted(uncovered, key=order_key):
            if seed not in uncovered:
                continue
            best: tuple[tuple[float, float, int, float], _Candidate] | None = None
            for part in parts:
                for candidate in _placements(part, seed, grid, uncovered, layout):
                    estimate = self._parts_estimate(grid, uncovered, candidate, seed)
                    bond = self._bond_score(layout, candidate)
                    jitter = float(rng.random()) * 1e-3
                    key = (-estimate, bond, len(candidate.cells), jitter)
                    if best is None or key > best[0]:
                        best = (key, candidate)
            if best is None:  # always false: plate_1x1 fits any lone cell
                uncovered.discard(seed)
                continue
            candidate = best[1]
            layout.add(
                candidate.part.key,
                *candidate.anchor,
                candidate.yaw,
                candidate.colour,
            )
            uncovered.difference_update(candidate.cells)

    def _parts_estimate(
        self,
        grid: VoxelGrid,
        uncovered: set[Cell],
        candidate: _Candidate,
        seed: Cell,
    ) -> float:
        """Estimated parts for this candidate's row: 1 + h(left) + h(right).

        The remainder runs are the contiguous same-colour uncovered cells
        beyond the candidate's two ends along its long axis, measured at the
        seed's row and plate layer (Kollsker's 1D cost adapted per-row).
        """
        xs = [x for x, _, _ in candidate.cells]
        ys = [y for _, y, _ in candidate.cells]
        if max(xs) - min(xs) >= max(ys) - min(ys):
            step = (1, 0)
            lo, hi = min(xs), max(xs)
            starts = ((lo - 1, seed[1]), (hi + 1, seed[1]))
        else:
            step = (0, 1)
            lo, hi = min(ys), max(ys)
            starts = ((seed[0], lo - 1), (seed[0], hi + 1))
        colour = grid.code_at(*seed)
        runs = (
            _run_length(
                grid, uncovered, colour, (*starts[0], seed[2]), (-step[0], -step[1])
            ),
            _run_length(grid, uncovered, colour, (*starts[1], seed[2]), step),
        )
        return 1 + _h_lookahead(runs[0]) + _h_lookahead(runs[1])

    def _bond_score(self, layout: Layout, candidate: _Candidate) -> float:
        """Bonding quality: many distinct supports, staggered seams below.

        Each border segment is penalized ``alpha1 * exp(-alpha2 * d)`` where
        ``d`` is the stud distance to the nearest seam in the layer below
        along the border's normal (d = 0 continues a seam — the stack-bond
        smell), averaged over the candidate's border segments.
        """
        below: set[int] = set()
        penalty = 0.0
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
                distance = _seam_distance(layout, x, y, dx, dy, base)
                if distance is not None:
                    penalty += math.exp(-self.weights.bond_alpha2 * distance)
        bond_penalty = self.weights.bond_alpha1 * penalty / borders if borders else 0.0
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
        # across the seam does. Grounded-but-disconnected towers count too.
        if _floating(layout) or _component_count(layout) > 1:
            improve_connectivity(layout, grid, rng, fail_max=self.fail_max)
        report = evaluate(layout, grid, self.weights, self.solver_config)
        failures = 0
        while failures < self.fail_max:
            stability = report.stability
            components = _component_count(layout)
            if stability.stable and not _floating(layout) and components == 1:
                return
            seeds = set(stability.unstable_ids) | _floating(layout)
            if components > 1:
                seeds |= _non_primary_component_ids(layout)
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
            self._fill(candidate_layout, grid, freed, rng, shuffle_within_layers=True)
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
    """All valid placements of ``part`` covering ``seed``.

    A placement is colour-valid when its cells carry at most one specific
    colour (IGNORE interior cells are wildcards); the brick takes that
    colour, or stays IGNORE when the whole footprint is interior.
    """
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
            if not all(cell in uncovered for cell in cells):
                continue
            colour = merge_colour(*(grid.code_at(*cell) for cell in cells))
            if colour is not None and layout.can_place(part, *anchor, yaw):
                results.append(
                    _Candidate(
                        part=part,
                        anchor=anchor,
                        yaw=yaw,
                        cells=tuple(cells),
                        colour=colour,
                    )
                )
    return results


def _run_length(
    grid: VoxelGrid,
    uncovered: set[Cell],
    colour: int,
    start: Cell,
    step: tuple[int, int],
) -> int:
    """Length of the colour-compatible uncovered run from ``start``."""
    x, y, z = start
    length = 0
    while (x, y, z) in uncovered and colour_matches(grid.code_at(x, y, z), colour):
        length += 1
        x += step[0]
        y += step[1]
    return length


def _seam_distance(  # noqa: PLR0913 - a border probe is naturally six scalars
    layout: Layout,
    x: int,
    y: int,
    dx: int,
    dy: int,
    base: int,
) -> int | None:
    """Stud distance to the nearest seam below a border, or None if none.

    The border sits between column ``(x, y)`` (inside the candidate) and
    ``(x + dx, y + dy)`` (outside); seams in the layer below are scanned up
    to ``_SEAM_WINDOW`` studs inward and outward along the border normal.
    """

    def below(px: int, py: int) -> int | None:
        brick = layout.brick_at((px, py, base - 1))
        return None if brick is None else brick.brick_id

    def is_seam(m: int) -> bool:
        """Seam between normal offsets ``m`` and ``m + 1`` below."""
        a = below(x + m * dx, y + m * dy)
        b = below(x + (m + 1) * dx, y + (m + 1) * dy)
        return a is not None and b is not None and a != b

    for distance in range(_SEAM_WINDOW + 1):
        if is_seam(distance) or (distance > 0 and is_seam(-distance)):
            return distance
    return None


def _floating(layout: Layout) -> set[int]:
    from legolization.graph import ConnectionGraph  # noqa: PLC0415 - cycle guard

    return set(ConnectionGraph.from_layout(layout).floating_ids())


def _component_count(layout: Layout) -> int:
    from legolization.graph import ConnectionGraph  # noqa: PLC0415 - cycle guard

    return ConnectionGraph.from_layout(layout).component_count()


def _non_primary_component_ids(layout: Layout) -> set[int]:
    """Return every brick outside the largest stud-connected component."""
    from legolization.graph import ConnectionGraph  # noqa: PLC0415 - cycle guard

    labels = ConnectionGraph.from_layout(layout).brick_components()
    if not labels:
        return set()
    counts = Counter(labels.values())
    primary = min(counts, key=lambda label: (-counts[label], label))
    return {brick_id for brick_id, label in labels.items() if label != primary}


def _is_filled(grid: VoxelGrid, cell: Cell) -> bool:
    x, y, z = cell
    nx, ny, nz = grid.shape
    return 0 <= x < nx and 0 <= y < ny and 0 <= z < nz and grid.codes[x, y, z] != EMPTY
