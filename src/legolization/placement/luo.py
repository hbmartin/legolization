"""Luo et al. (2015) split-and-remerge placement.

Two phases over a maximal random merge of 1x1 atoms:

1. **Topology**: while the structure is not a single ground-connected
   component, split a growing k-ring around bricks bordering other
   components back to atoms and remerge randomly, accepting only strictly
   fewer components (Algorithm 5).
2. **Stability**: while the RBE says the structure is unstable, split a
   k-ring around the collapsing bricks and the weakest contact's pair and
   remerge, accepting only strictly better physics (Algorithm 7).

``k = failures // 10 + 1`` grows the reconfigured region as attempts fail;
each phase gives up after ``fail_max`` consecutive failures (Luo used 100).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from legolization.catalog import default_catalog
from legolization.placement.merge import (
    atomize,
    compact_vertical,
    improve_connectivity,
    k_ring,
    maximal_random_merge,
    split_to_atoms,
)
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    import numpy as np

    from legolization.catalog import Catalog
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

_RING_GROWTH = 10  # failures per extra ring (Luo's N)


@dataclass(slots=True)
class LuoStrategy:
    """Maximal random merge + component/stability split-remerge refinement."""

    catalog: Catalog = field(default_factory=default_catalog)
    solver_config: SolverConfig = field(default_factory=SolverConfig)
    fail_max: int = 30

    def place(self, grid: VoxelGrid, *, rng: np.random.Generator) -> Layout:
        """Produce a merged layout, refined for connectivity then stability."""
        layout = atomize(grid, self.catalog)
        maximal_random_merge(layout, rng)
        improve_connectivity(layout, grid, rng, fail_max=self.fail_max)
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
        failures = 0
        while not result.stable and failures < self.fail_max:
            seeds = set(result.unstable_ids)
            if result.weakest_pair is not None:
                seeds |= {bid for bid in result.weakest_pair if bid >= 0}
            if not seeds:
                return
            region = k_ring(layout, seeds, failures // _RING_GROWTH + 1)
            candidate = layout.copy()
            split_to_atoms(candidate, region, grid)
            maximal_random_merge(candidate, rng)
            candidate_result = analyze(candidate, self.solver_config)
            if _better(candidate_result, result):
                layout.replace_with(candidate)
                result = candidate_result
                failures = 0
            else:
                failures += 1


def _better(candidate: StabilityResult, current: StabilityResult) -> bool:
    """Strict improvement on (collapsing bricks, min contact capacity)."""
    return (len(candidate.unstable_ids), -candidate.min_capacity) < (
        len(current.unstable_ids),
        -current.min_capacity,
    )
