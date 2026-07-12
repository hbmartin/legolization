"""Luo et al. (2015) split-and-remerge placement.

Two phases over a maximal random merge of 1x1 atoms:

1. **Topology**: while the structure is not a single ground-connected
   component, split a growing k-ring around bricks bordering other
   components back to atoms and remerge randomly, accepting only strictly
   fewer components (Algorithm 5).
2. **Stability**: while the RBE says the structure is unstable, split a
   k-ring around an importance-sampled collapsing brick plus the weakest
   contact's pair and remerge, accepting only strict improvement — by
   Luo's maximin friction capacity ``C_M`` (the default) or the legacy
   lexicographic RBE comparison (Algorithm 7).

``k = failures // 10 + 1`` grows the reconfigured region as attempts fail;
each phase gives up after ``fail_max`` consecutive failures (Luo's 100).
``colour_mode="soft"`` lets merges cross colour boundaries via Luo's
importance sampling (weighted by ``colour_weight``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization.catalog import default_catalog
from legolization.placement.merge import (
    ColourMode,
    atomize,
    compact_columns,
    compact_vertical,
    improve_connectivity,
    k_ring,
    maximal_random_merge,
    split_to_atoms,
)
from legolization.stability.solver import (
    SolverConfig,
    StabilityResult,
    analyze,
    build_model,
    solve_maximin,
)

if TYPE_CHECKING:
    from legolization.catalog import Catalog
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

_RING_GROWTH = 10  # failures per extra ring (Luo's N)


@dataclass(slots=True)
class LuoStrategy:
    """Maximal random merge + component/stability split-remerge refinement."""

    catalog: Catalog = field(default_factory=default_catalog)
    solver_config: SolverConfig = field(default_factory=SolverConfig)
    fail_max: int = 100
    acceptance: Literal["rbe", "maximin"] = "maximin"
    colour_mode: ColourMode = "hard"
    colour_weight: float = 1.0
    refine: bool = True

    def place(self, grid: VoxelGrid, *, rng: np.random.Generator) -> Layout:
        """Produce a merged layout, refined for connectivity then stability."""
        layout = atomize(grid, self.catalog)
        maximal_random_merge(
            layout,
            rng,
            colour_mode=self.colour_mode,
            colour_weight=self.colour_weight,
        )
        if self.refine:
            improve_connectivity(
                layout,
                grid,
                rng,
                fail_max=self.fail_max,
                colour_mode=self.colour_mode,
                colour_weight=self.colour_weight,
            )
            self._stabilize(layout, grid, rng)
        compact_vertical(layout)
        return layout

    def _stabilize(
        self,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
    ) -> None:
        """Phase 2: split-remerge around the weakest bricks until stable."""
        result = analyze(layout, self.solver_config)
        capacity = self._capacity(layout)
        failures = 0
        while not result.stable and failures < self.fail_max:
            seeds = self._seeds(result, rng)
            if not seeds:
                return
            region = k_ring(layout, seeds, failures // _RING_GROWTH + 1)
            candidate = layout.copy()
            atom_ids = split_to_atoms(candidate, region, grid)
            compact_columns(candidate, atom_ids)
            maximal_random_merge(
                candidate,
                rng,
                colour_mode=self.colour_mode,
                colour_weight=self.colour_weight,
            )
            candidate_result = analyze(candidate, self.solver_config)
            candidate_capacity = self._capacity(candidate)
            if self._better(candidate_result, result, candidate_capacity, capacity):
                layout.replace_with(candidate)
                result = candidate_result
                capacity = candidate_capacity
                failures = 0
            else:
                failures += 1

    def _seeds(
        self,
        result: StabilityResult,
        rng: np.random.Generator,
    ) -> set[int]:
        """Luo's critical portion: weakest pair + one sampled unstable brick."""
        seeds: set[int] = set()
        if result.weakest_pair is not None:
            seeds |= {bid for bid in result.weakest_pair if bid >= 0}
        if unstable := sorted(result.unstable_ids):
            scores = np.asarray([result.scores[bid].score for bid in unstable])
            seeds.add(int(rng.choice(unstable, p=scores / scores.sum())))
        return seeds

    def _capacity(self, layout: Layout) -> float:
        """Maximin capacity C_M, or -inf when equilibrium is infeasible."""
        if self.acceptance != "maximin":
            return 0.0
        result = solve_maximin(build_model(layout))
        return result.capacity if result.feasible else float("-inf")

    def _better(
        self,
        candidate: StabilityResult,
        current: StabilityResult,
        candidate_capacity: float,
        capacity: float,
    ) -> bool:
        """Strict improvement under the configured acceptance metric."""
        if self.acceptance == "maximin":
            return candidate_capacity > capacity
        return (len(candidate.unstable_ids), -candidate.min_capacity) < (
            len(current.unstable_ids),
            -current.min_capacity,
        )
