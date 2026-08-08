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

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np

from legolization import telemetry
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
from legolization.stability.screen import ReducedScreen, ScreenReport
from legolization.stability.solver import (
    SolverConfig,
    StabilityResult,
    analyze,
    build_model_from_config,
    solve_maximin,
)

if TYPE_CHECKING:
    from legolization.catalog import Catalog
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

_RING_GROWTH = 10  # failures per extra ring (Luo's N)


def _rebase(
    screen: ReducedScreen | None,
    report: ScreenReport | None,
    layout: Layout,
    config: SolverConfig,
    deadline: float | None,
) -> ReducedScreen | None:
    """Advance the screen baseline to an accepted candidate's report.

    ``ReducedScreen.rebase`` keeps the old baseline for a non-"ok"
    report, and ``should_reject`` passes any such report through to the
    cold solve — so a nonconverged or errored candidate can be accepted
    and leave the screen ranking every later candidate against a layout
    that no longer exists. Rebuild the baseline from the new layout
    instead; a decline returns ``None`` and drops the loop to the cold
    path rather than screening against stale ground truth.
    """
    if screen is None:
        return None
    if report is not None and report.status == "ok":
        screen.rebase(report)
        return screen
    return ReducedScreen.create(layout, config, deadline=deadline)


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
    time_budget_s: float | None = None
    outcome: None = field(default=None, init=False)
    """Soft place() budget: the stability split-remerge loop stops at
    round boundaries once spent (each round costs two full ~n^2.8 RBE
    solves — the measured Armadillo wall). None = unbounded (historical
    behaviour, byte-identical)."""

    def place(
        self,
        grid: VoxelGrid,
        *,
        rng: np.random.Generator,
        deadline: float | None = None,
    ) -> Layout:
        """Produce a merged layout, refined for connectivity then stability."""
        local_deadline = (
            time.monotonic() + self.time_budget_s
            if self.time_budget_s is not None
            else None
        )
        if local_deadline is not None:
            deadline = (
                local_deadline if deadline is None else min(deadline, local_deadline)
            )
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
                deadline=deadline,
            )
            self._stabilize(layout, grid, rng, deadline=deadline)
        compact_vertical(layout)
        return layout

    def _stabilize(
        self,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        *,
        deadline: float | None = None,
    ) -> None:
        """Phase 2: split-remerge around the weakest bricks until stable."""
        if self._budget_spent(deadline, 0):
            return
        result = analyze(layout, self.solver_config)
        if result.stable:
            return
        if self._budget_spent(deadline, 0):
            return
        capacity = self._capacity(layout)
        screen = (
            # Deadline-gated: _capacity() above can start inside the
            # budget and finish past it, and a declined create leaves
            # the loop on the cold path.
            ReducedScreen.create(layout, self.solver_config, deadline=deadline)
            if self.solver_config.screen == "bricksim"
            else None
        )
        failures = 0
        while not result.stable and failures < self.fail_max:
            if self._budget_spent(deadline, failures):
                break
            seeds = self._seeds(result, rng)
            if not seeds:
                break
            candidate = self._respin(layout, grid, rng, seeds, failures)
            report, rejected = self._screened_out(screen, candidate, deadline)
            if rejected:
                failures += 1
                continue
            solved = self._solve_candidate(candidate, deadline, failures)
            if solved is None:
                break
            candidate_result, candidate_capacity = solved
            if self._better(candidate_result, result, candidate_capacity, capacity):
                layout.replace_with(candidate)
                result = candidate_result
                capacity = candidate_capacity
                screen = _rebase(screen, report, layout, self.solver_config, deadline)
                failures = 0
            else:
                failures += 1

    def _respin(
        self,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        seeds: set[int],
        failures: int,
    ) -> Layout:
        """Split the k-ring around ``seeds`` to atoms and re-merge it."""
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
        return candidate

    def _solve_candidate(
        self,
        candidate: Layout,
        deadline: float | None,
        failures: int,
    ) -> tuple[StabilityResult, float] | None:
        """Exact-solve a screened-in candidate; ``None`` past the deadline.

        Neither solve takes a deadline of its own, and the screen above
        may have consumed the whole remaining budget, so each is gated
        on its own boundary check: starting one past expiry overshoots
        by a full solve (two, under maximin acceptance).
        """
        if self._budget_spent(deadline, failures):
            return None
        result = analyze(candidate, self.solver_config)
        if self.acceptance == "maximin" and self._budget_spent(deadline, failures):
            return None
        return result, self._capacity(candidate)

    def _budget_spent(self, deadline: float | None, failures: int) -> bool:
        """Whether the round budget is gone, recording the stop counter.

        Called at every solve boundary: the exact solves below take no
        deadline of their own, so the policy is round-boundary — a solve
        that started before expiry runs to completion, but none starts
        after it.
        """
        if deadline is None or time.monotonic() < deadline:
            return False
        telemetry.value("luo.stabilize.deadline_stop", float(failures))
        return True

    def _screened_out(
        self,
        screen: ReducedScreen | None,
        candidate: Layout,
        deadline: float | None,
    ) -> tuple[ScreenReport | None, bool]:
        """Screen a candidate against the current layout's reduced verdict.

        A confidently-worse candidate skips its two exact solves
        (analyze + maximin) and counts a failure.
        """
        if screen is None:
            return None, False
        report = screen.evaluate(candidate, deadline=deadline)
        if screen.should_reject(report, self.solver_config.screen_margin):
            telemetry.value("stability.screen.reject", 1.0)
            return report, True
        return report, False

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
        result = solve_maximin(build_model_from_config(layout, self.solver_config))
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
