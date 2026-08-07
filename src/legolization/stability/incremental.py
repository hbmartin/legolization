"""Frozen-boundary rejection screens with mandatory cold certification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from legolization import telemetry
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.stability.constants import T_CAPACITY_N
from legolization.stability.screen import ReducedScreen, ScreenReport
from legolization.stability.solver import (
    StabilityDeadlineError,
    StabilityResult,
    analyze,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from legolization.layout import Layout
    from legolization.stability.solver import SolverConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenBoundaryScreen:
    """Cheap conservative result around changed exact-LDU bounds."""

    feasible: bool
    affected_ids: frozenset[int]
    boundary_ids: frozenset[int]
    frozen_forces: dict[tuple[int, int], tuple[float, float]]
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class IncrementalCertification:
    """Screen evidence plus the required whole-layout cold verdict."""

    screen: FrozenBoundaryScreen
    cold_result: StabilityResult | None
    reduced_report: ScreenReport | None = None
    """Reduced-QP screen evidence when ``SolverConfig.screen`` enabled
    it — advisory only; ``cold_result`` remains the sole verdict."""

    @property
    def cold_certified(self) -> bool:
        """Return whether the candidate reached a full cold solve."""
        return self.cold_result is not None


@dataclass(slots=True)
class FrozenBoundaryAnalyzer:
    """Reuse one full solve's boundary forces for local rejection screening."""

    layout: Layout
    result: StabilityResult
    config: SolverConfig
    k_rings: int = 2
    reduced: ReducedScreen | None = None
    """Optional reduced-QP pre-screen (``SolverConfig.screen``); a
    declined build silently degrades to the heuristic-screen-only
    behaviour."""

    @classmethod
    def create(
        cls,
        layout: Layout,
        *,
        config: SolverConfig,
        result: StabilityResult | None = None,
        k_rings: int = 2,
    ) -> Self:
        """Capture a full cold baseline and its exact interface forces."""
        if k_rings < 0:
            msg = "k_rings must be non-negative"
            raise ValueError(msg)
        cold = result if result is not None else analyze(layout, config)
        reduced = (
            ReducedScreen.create(layout, config)
            if config.screen == "bricksim"
            else None
        )
        return cls(
            layout=layout.copy(),
            result=cold,
            config=config,
            k_rings=k_rings,
            reduced=reduced,
        )

    def screen(
        self,
        candidate: Layout,
        *,
        changed_ids: Iterable[int] = (),
    ) -> FrozenBoundaryScreen:
        """Screen contact-graph rings while freezing last-solve boundary loads."""
        seeds = _changed_ids(self.layout, candidate) | set(changed_ids)
        baseline_graph = ConnectionGraph.from_layout(self.layout)
        candidate_graph = ConnectionGraph.from_layout(candidate)
        adjacency = _combined_adjacency(baseline_graph, candidate_graph)
        affected = _expand(seeds, adjacency=adjacency, rings=self.k_rings)
        boundary = {
            neighbour
            for brick_id in affected
            for neighbour in adjacency.get(brick_id, ())
            if neighbour not in affected and neighbour != GROUND_ID
        }
        frozen = {
            pair: forces
            for pair, forces in self.result.interface_forces.items()
            if _crosses(pair, affected)
        }
        candidate_pairs = set(candidate_graph.support_edges())
        if missing := sorted(set(frozen) - candidate_pairs):
            return FrozenBoundaryScreen(
                feasible=False,
                affected_ids=frozenset(affected),
                boundary_ids=frozenset(boundary),
                frozen_forces=frozen,
                reason=f"changed region removes frozen interfaces {missing}",
            )
        if any(
            not math.isfinite(value) or value < 0
            for forces in frozen.values()
            for value in forces
        ):
            return FrozenBoundaryScreen(
                feasible=False,
                affected_ids=frozenset(affected),
                boundary_ids=frozenset(boundary),
                frozen_forces=frozen,
                reason="baseline contains invalid boundary force evidence",
            )
        if any(drag >= T_CAPACITY_N for _, drag in frozen.values()):
            return FrozenBoundaryScreen(
                feasible=False,
                affected_ids=frozenset(affected),
                boundary_ids=frozenset(boundary),
                frozen_forces=frozen,
                reason="frozen boundary already exhausts drag capacity",
            )
        return FrozenBoundaryScreen(
            feasible=True,
            affected_ids=frozenset(affected),
            boundary_ids=frozenset(boundary),
            frozen_forces=frozen,
        )

    def certify(
        self,
        candidate: Layout,
        *,
        changed_ids: Iterable[int] = (),
        deadline: float | None = None,
    ) -> IncrementalCertification:
        """Run the screens and always cold-solve candidates that survive.

        With ``SolverConfig.screen`` enabled, a candidate the reduced QP
        confidently ranks worse than the baseline skips its cold solve
        (``cold_result=None`` — the caller treats it as a failed
        candidate); everything else, including every non-confident or
        non-"ok" screen outcome, falls through to the cold solve.
        """
        screen = self.screen(candidate, changed_ids=changed_ids)
        reduced_report: ScreenReport | None = None
        if screen.feasible and self.reduced is not None:
            reduced_report = self.reduced.evaluate(candidate, deadline=deadline)
            if self.reduced.should_reject(reduced_report, self.config.screen_margin):
                telemetry.value("stability.screen.reject", 1.0)
                return IncrementalCertification(
                    screen=screen,
                    cold_result=None,
                    reduced_report=reduced_report,
                )
            if reduced_report.status == "ok" and reduced_report.confident:
                telemetry.value("stability.screen.pass", 1.0)
            else:
                telemetry.value("stability.screen.boundary", 1.0)
        try:
            cold = (
                analyze(candidate, self.config, deadline=deadline)
                if screen.feasible and deadline is not None
                else analyze(candidate, self.config)
                if screen.feasible
                else None
            )
        except StabilityDeadlineError:
            cold = None
        return IncrementalCertification(
            screen=screen, cold_result=cold, reduced_report=reduced_report
        )

    def accept(
        self, candidate: Layout, certification: IncrementalCertification
    ) -> None:
        """Advance the baseline only from a full cold-certified candidate."""
        if certification.cold_result is None:
            msg = "cannot accept a candidate without full cold certification"
            raise ValueError(msg)
        self.layout = candidate.copy()
        self.result = certification.cold_result
        if self.reduced is not None and certification.reduced_report is not None:
            self.reduced.rebase(certification.reduced_report)


def _changed_ids(baseline: Layout, candidate: Layout) -> set[int]:
    ids = set(baseline.bricks) | set(candidate.bricks)
    return {
        brick_id
        for brick_id in ids
        if brick_id not in baseline.bricks
        or brick_id not in candidate.bricks
        or baseline.bricks[brick_id] != candidate.bricks[brick_id]
        or baseline.collision_boxes_of(baseline.bricks[brick_id])
        != candidate.collision_boxes_of(candidate.bricks[brick_id])
    }


def _combined_adjacency(*graphs: ConnectionGraph) -> dict[int, set[int]]:
    adjacency: dict[int, set[int]] = {}
    for graph in graphs:
        for left, right in graph.support_edges():
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        for side in graph.side_contacts:
            adjacency.setdefault(side.a_id, set()).add(side.b_id)
            adjacency.setdefault(side.b_id, set()).add(side.a_id)
    return adjacency


def _expand(
    seeds: set[int],
    *,
    adjacency: dict[int, set[int]],
    rings: int,
) -> set[int]:
    affected = set(seeds)
    frontier = set(seeds)
    for _ in range(rings):
        frontier = {
            neighbour
            for brick_id in frontier
            for neighbour in adjacency.get(brick_id, ())
            if neighbour != GROUND_ID and neighbour not in affected
        }
        affected |= frontier
    return affected


def _crosses(pair: tuple[int, int], affected: set[int]) -> bool:
    left, right = pair
    return (left in affected) != (right in affected)


__all__ = [
    "FrozenBoundaryAnalyzer",
    "FrozenBoundaryScreen",
    "IncrementalCertification",
]
