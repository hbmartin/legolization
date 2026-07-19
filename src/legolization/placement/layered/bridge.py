"""Structure-preserving bridge synthesis for the connectivity pass.

`improve_connectivity` historically re-tiled the repair ring with a
random maximal merge — measured in docs/kollsker-drift-report.md as the
count-inflation hotspot (+155 bricks on mushroom's 112-brick minimum
tiling even under best-of-k). This module replaces the random rewrite
for layered strategies with a principled one: the ring's cells are
re-decomposed through the same absolute-3-plate slab policy the
strategies place with (`slab_problems`), and each slab component is
solved by a two-stage exact-cover MILP — stage 1 minimizes the part
count **subject to actually bridging** (at least one chosen rect must
touch two stud-graph components; a minimum cover that reproduces the
fragmenting seam is infeasible under that row), stage 2 pins the count
and maximizes extra component crossings plus Kollsker's stagger reward.

The known trap this dodges: a pure minimum-count cover of the ring can
reproduce the very straight seam that fragmented the layout. The
bridging row makes that cover infeasible, and stage 2 spends the
equal-count degrees of freedom on more crossings and better bond.

Re-phasing-only bridges (plate columns joined by `compact_columns`'
mod-3 vote in the random path) are invisible to an absolute-slab
re-tiling; the synthesizer returns None for those rings and the random
fallback still covers them. No rng is consumed on the MILP path, so
runs stay deterministic and the fallback's draw sequence is unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import Bounds, LinearConstraint

from legolization.catalog import Catalog, default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.placement.layered.engine import (
    LayerContext,
    LayerProblem,
    Rect2D,
    build_context,
    enumerate_layer_rects,
    realize,
    slab_problems,
)
from legolization.placement.layered.kollsker import (
    _RANK_EPS,
    _components,
    _cover_matrix,
    _guarded_milp,
    bond_reward,
)
from legolization.placement.merge import _MERGEABLE, _cell_code, compact_vertical

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.catalog import Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

    BridgeFn = Callable[[Layout, set[int], VoxelGrid], Layout | None]

_MIN_SOLVE_S = 0.05


@dataclass(slots=True)
class BridgeSynthesizer:
    """Exact-cover MILP re-tiling of a connectivity-repair ring.

    Callable as ``synthesizer(layout, region, grid) -> Layout | None``:
    a re-tiled copy whose stud-graph component count strictly dropped,
    or None (ring not carvable, candidates blown up, solver failure or
    timeout, or no bridge achievable) — the caller falls back to the
    random rewrite. Deterministic: no rng, rank-epsilon tiebreaks.
    """

    catalog: Catalog = field(default_factory=default_catalog)
    slab_time_s: float = 2.0
    total_time_s: float = 10.0
    candidate_limit: int = 20_000
    bond_weight: float = 1.0
    bridge_bonus: float = 10.0

    def __call__(
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
    ) -> Layout | None:
        """Re-tile ``region``'s carvable bricks; None on any failure."""
        deadline = time.monotonic() + self.total_time_s
        before = ConnectionGraph.from_layout(layout).component_count()
        candidate = layout.copy()
        cells: dict[Cell, int] = {}
        for brick_id in sorted(region):
            brick = candidate.bricks.get(brick_id)
            if brick is None or candidate.part_of(brick).category not in _MERGEABLE:
                continue
            for cell in candidate.cells_of(brick):
                cells[cell] = _cell_code(grid, cell, brick.colour_code)
            candidate.remove(brick_id)
        if not cells:
            return None
        for problem in slab_problems(cells):
            context = build_context(candidate, problem)
            labels = ConnectionGraph.from_layout(candidate).brick_components()
            chosen: list[Rect2D] = []
            for component in _components(problem.columns):
                rects = self._solve_component(
                    candidate, problem, context, labels, component, deadline
                )
                if rects is None:
                    return None
                chosen.extend(rects)
            realize(candidate, problem, chosen)
        compact_vertical(candidate)
        after = ConnectionGraph.from_layout(candidate).component_count()
        return candidate if after < before else None

    def _touch_count(
        self,
        candidate: Layout,
        problem: LayerProblem,
        context: LayerContext,
        labels: dict[int, int],
        rect: Rect2D,
    ) -> int:
        """Distinct stud-graph components this rect would mate with.

        Below contacts come through ``support_of`` (the slab's cells were
        free above their supports, so a stud mate is real); above
        contacts require the neighbour's *bottom* face exactly on this
        slab's top plane — that guard is what lets the mushroom
        stem-below/cap-above sandwich count as a bridge.
        """
        top = problem.layer + problem.height_plates
        touched: set[int] = set()
        for column in rect.columns():
            support = context.support_of.get(column)
            if support is not None and support != GROUND_ID:
                label = labels.get(support)
                if label is not None:
                    touched.add(label)
            x, y = column
            above = candidate.brick_at((x, y, top))
            if above is not None and above.layer == top:
                label = labels.get(above.brick_id)
                if label is not None:
                    touched.add(label)
        return len(touched)

    def _budget(self, deadline: float, *, spent: float = 0.0) -> float | None:
        """Remaining per-solve budget, or None when exhausted."""
        budget = min(self.slab_time_s - spent, deadline - time.monotonic())
        if budget < _MIN_SOLVE_S:
            return None
        return budget

    def _solve_component(  # noqa: PLR0913 - one slab component is six facts
        self,
        candidate: Layout,
        problem: LayerProblem,
        context: LayerContext,
        labels: dict[int, int],
        component: list[tuple[int, int]],
        deadline: float,
    ) -> list[Rect2D] | None:
        """Two-stage lexicographic MILP with a hard bridging floor."""
        rects = enumerate_layer_rects(problem, component, self.catalog)
        if not rects or len(rects) > self.candidate_limit:
            return None
        if (stage1_limit := self._budget(deadline)) is None:
            return None
        started = time.monotonic()
        cover = _cover_matrix(component, rects)
        ones = np.ones(len(rects))
        touches = np.array(
            [
                self._touch_count(candidate, problem, context, labels, rect)
                for rect in rects
            ]
        )
        constraints = [LinearConstraint(cover, lb=1.0, ub=1.0)]
        bridging = touches >= 2  # two components make a bridge
        if bridging.any():
            constraints.append(
                LinearConstraint(
                    bridging.astype(float).reshape(1, -1),
                    lb=1.0,
                    ub=float(bridging.sum()),
                )
            )
        stage1 = _guarded_milp(
            c=ones,
            constraints=constraints,
            integrality=ones,
            bounds=Bounds(0, 1),
            options={"time_limit": stage1_limit},
        )
        if stage1 is None or not stage1.success or stage1.x is None:
            return None
        n_star = float(np.round(stage1.fun))
        rewards = self.bridge_bonus * np.maximum(touches - 1, 0) + np.array(
            [self.bond_weight * bond_reward(rect, context) for rect in rects]
        )
        rank = _RANK_EPS * np.arange(len(rects))
        stage2_limit = self._budget(deadline, spent=time.monotonic() - started)
        stage2 = (
            None
            if stage2_limit is None
            else _guarded_milp(
                c=-rewards + rank,
                constraints=[
                    *constraints,
                    LinearConstraint(np.ones((1, len(rects))), n_star, n_star),
                ],
                integrality=ones,
                bounds=Bounds(0, 1),
                options={"time_limit": stage2_limit},
            )
        )
        chosen = (
            stage2
            if stage2 is not None and stage2.success and stage2.x is not None
            else stage1
        )
        return [
            rect for value, rect in zip(chosen.x, rects, strict=True) if value > 0.5
        ]
