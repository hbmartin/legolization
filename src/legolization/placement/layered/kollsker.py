"""Kollsker & Malaguti's exact set-partitioning MILP, scoped per layer.

The paper's core model (eqs. 1-3): a binary per feasible placement,
minimize the part count subject to exact cover. Whole-model instances are
exponential, so this strategy solves it where it is tractable — one MILP
per 4-connected component of each layer problem (hundreds to a few
thousand binaries) — mirroring the paper's own matheuristic, which fixes
a heuristic layout and re-optimizes free regions (eqs. 26-39).

Two-stage lexicographic solve: stage 1 finds the minimum part count N*;
stage 2 pins the count to N* and maximizes a stagger/bond reward against
the layer below, so brick economy is never traded for bond quality. The
``h3`` lookahead is deliberately absent: it guides *sequential*
commitment, which the simultaneous exact cover subsumes — any heuristic
tiling is a feasible point, so the per-layer optimum is never worse than
the constructive bond pass. Timeout, candidate blowup, or solver failure
falls back to that bond pass at component granularity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from legolization.placement.layered.bond import BondStrategy
from legolization.placement.layered.engine import (
    Column,
    LayerContext,
    LayerProblem,
    Rect2D,
    enumerate_layer_rects,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_RANK_EPS = 1e-6  # deterministic tiebreak among equal-reward covers


@dataclass(slots=True)
class KollskerStrategy(BondStrategy):
    """Per-layer exact set-partitioning MILP with a bond-quality stage."""

    layer_time_s: float = 10.0
    bond_weight: float = 1.0
    candidate_limit: int = 20_000

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Solve each 4-connected component exactly; fall back per component."""
        rects: list[Rect2D] = []
        for component in _components(problem.columns):
            solved = self._solve_component(problem, below, component, deadline)
            if solved is None:
                sub = LayerProblem(
                    layer=problem.layer,
                    height_plates=problem.height_plates,
                    columns=frozenset(component),
                    colour_of={c: problem.colour_of[c] for c in component},
                )
                # Explicit base call: zero-arg super() breaks under
                # @dataclass(slots=True), which rebuilds the class object.
                solved = BondStrategy.tile(self, sub, below, rng=rng, deadline=deadline)
            rects.extend(solved)
        return rects

    def _time_limit(self, deadline: float | None) -> float:
        """Per-solve budget from the layer cap and the remaining deadline."""
        if deadline is None:
            return self.layer_time_s
        return min(self.layer_time_s, max(deadline - time.monotonic(), 0.1))

    def _solve_component(
        self,
        problem: LayerProblem,
        below: LayerContext,
        component: list[Column],
        deadline: float | None,
    ) -> list[Rect2D] | None:
        """Two-stage lexicographic MILP; None means use the fallback."""
        candidates = enumerate_layer_rects(problem, component, self.catalog)
        if not candidates or len(candidates) > self.candidate_limit:
            return None
        cover = _cover_matrix(component, candidates)
        ones = np.ones(len(candidates))
        stage1 = milp(
            c=ones,
            constraints=LinearConstraint(cover, lb=1.0, ub=1.0),
            integrality=ones,
            bounds=Bounds(0, 1),
            options={"time_limit": self._time_limit(deadline)},
        )
        if not stage1.success or stage1.x is None:
            return None
        n_star = float(np.round(stage1.fun))
        rewards = np.array([self._bond_reward(rect, below) for rect in candidates])
        rank = _RANK_EPS * np.arange(len(candidates))
        stage2 = milp(
            c=-self.bond_weight * rewards + rank,
            constraints=[
                LinearConstraint(cover, lb=1.0, ub=1.0),
                LinearConstraint(np.ones((1, len(candidates))), n_star, n_star),
            ],
            integrality=ones,
            bounds=Bounds(0, 1),
            # Recomputed: stage 1 may have consumed most of the budget.
            options={"time_limit": self._time_limit(deadline)},
        )
        chosen = stage2 if stage2.success and stage2.x is not None else stage1
        return [
            rect
            for value, rect in zip(chosen.x, candidates, strict=True)
            if value > 0.5
        ]

    def _bond_reward(self, rect: Rect2D, below: LayerContext) -> float:
        """Stagger quality of one candidate against the below layer.

        A below-seam *straddled* by the rect earns its priority (the rect
        bridges it); a below-seam aligned with the rect's own border loses
        half its priority (stacked seams weaken the wall). Normalized by
        perimeter so large rects are not rewarded merely for size.
        """
        reward = 0.0
        for x in range(rect.x0, rect.x1 + 1):
            for y in range(rect.y0, rect.y1 + 1):
                if x < rect.x1:
                    reward += below.seam_priority.get(((x, y), 0), 0.0)
                if y < rect.y1:
                    reward += below.seam_priority.get(((x, y), 1), 0.0)
        for y in range(rect.y0, rect.y1 + 1):
            for x in (rect.x0 - 1, rect.x1):
                reward -= 0.5 * below.seam_priority.get(((x, y), 0), 0.0)
        for x in range(rect.x0, rect.x1 + 1):
            for y in (rect.y0 - 1, rect.y1):
                reward -= 0.5 * below.seam_priority.get(((x, y), 1), 0.0)
        return reward / (2.0 * (rect.width + rect.length))


def _components(columns: frozenset[Column]) -> list[list[Column]]:
    """4-connected components, each sorted, ordered by their minimum column."""
    remaining = set(columns)
    components: list[list[Column]] = []
    while remaining:
        seed = min(remaining)
        stack = [seed]
        remaining.discard(seed)
        found = [seed]
        while stack:
            x, y = stack.pop()
            for neighbour in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    stack.append(neighbour)
                    found.append(neighbour)
        components.append(sorted(found))
    components.sort(key=lambda component: component[0])
    return components


def _cover_matrix(
    component: Iterable[Column],
    candidates: list[Rect2D],
) -> coo_matrix:
    """Sparse exact-cover matrix: one row per column, one col per candidate."""
    cell_index = {cell: i for i, cell in enumerate(sorted(component))}
    rows: list[int] = []
    cols: list[int] = []
    for col, rect in enumerate(candidates):
        for cell in rect.columns():
            rows.append(cell_index[cell])
            cols.append(col)
    return coo_matrix(
        (np.ones(len(rows)), (rows, cols)),
        shape=(len(cell_index), len(candidates)),
    ).tocsc()
