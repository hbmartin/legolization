"""Min et al.'s objective-driven tiling with balance terms (KSII TIIS 2018).

Each layer is tiled by bounded best-first search (the paper's A*, honestly
a beam search: the OPEN list is capped and the guidance heuristic is not
admissible). A whole-model placement tiles once with x as the fixed mirror axis
and once with y, then post-processes only the lower-cost candidate (both when
their tiling costs are equal within floating-point tolerance). Tied finalists
share the remaining deadline and prefer a buildable result, then a grounded
result, before final symmetry and brick count. Nodes expand only through
placements covering the first uncovered column in scan order, which collapses
permutations of the same tiling into one path. The cost accumulates per placed
rect:

- efficiency ``g_h``: small rects cost ``(A_MAX - area) / (A_MAX - 1)``;
- balance ``g_a``: a rect not centred on the whole-model mirror axis and
  without an already-placed mirror partner costs 1 (a direct :meth:`tile`
  call retains the paper's per-layer better-axis fallback);
- vertical merge ``g_v``: a plate rect that fails to complete a 3-plate
  stack ``compact_vertical`` could brickify costs 1 (Min's multi-height
  term reinterpreted at plate resolution — the catalog has no 2-brick-tall
  parts);
- stability ``g_s``: at completion, every below-seam left uncovered costs
  its priority (1.0 disconnected / 0.5 unsupported pair / 0.1 tied pair).

Weight presets follow the paper's balanced / stability / aesthetics /
efficiency profiles.
"""

from __future__ import annotations

import heapq
import math
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self

import numpy as np

from legolization.graph import ConnectionGraph, TopologyMetrics
from legolization.grid import merge_colour
from legolization.placement.aesthetics import symmetry_error
from legolization.placement.layered.engine import (
    Column,
    LayerContext,
    LayeredStrategy,
    LayerProblem,
    Rect2D,
    grounding_gain,
    random_fill,
    rects_covering,
)
from legolization.runtime import deadline_share

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

_A_MAX = 16  # largest catalog footprint (2x8)
_COST_TIE_TOLERANCE = 1e-12

_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "balanced": (0.25, 0.25, 0.25, 0.25),
    "stability": (0.55, 0.15, 0.15, 0.15),
    "aesthetics": (0.15, 0.55, 0.15, 0.15),
    "efficiency": (0.1, 0.1, 0.4, 0.4),
}

PresetName = Literal["balanced", "stability", "aesthetics", "efficiency"]
type _Node = tuple[float, int, frozenset[Column], tuple[Rect2D, ...]]


@dataclass(frozen=True, slots=True)
class BeautyWeights:
    """Min's per-layer objective weights (w_s, w_a, w_h, w_v)."""

    w_s: float
    w_a: float
    w_h: float
    w_v: float

    @classmethod
    def preset(cls, name: PresetName) -> Self:
        """Return one of the paper's four weight profiles."""
        w_s, w_a, w_h, w_v = _PRESETS[name]
        return cls(w_s=w_s, w_a=w_a, w_h=w_h, w_v=w_v)


@dataclass(frozen=True, slots=True)
class _AxisCandidate:
    """One raw whole-model tiling and the RNG state that produced it."""

    cost: float
    axis: int
    layout: Layout
    rng: np.random.Generator


@dataclass(frozen=True, slots=True)
class _Finalist:
    """A finalized candidate plus telemetry metrics when they already exist."""

    candidate: _AxisCandidate
    topology: TopologyMetrics | None


def _candidate_key(
    finalist: _Finalist,
    *,
    component_target: int,
) -> tuple[bool, bool, float, int, int, int, int]:
    """Rank final layouts by feasibility tier, aesthetics, then efficiency."""
    candidate = finalist.candidate
    topology = finalist.topology
    if topology is None:
        topology = ConnectionGraph.from_layout(candidate.layout).topology_metrics()
    floating = topology.floating_count
    component_excess = max(topology.component_count - component_target, 0)
    return (
        not topology.is_buildable(component_target=component_target),
        floating > 0,
        symmetry_error(candidate.layout),
        floating,
        component_excess,
        len(candidate.layout),
        candidate.axis,
    )


@dataclass(slots=True)
class BeautyStrategy(LayeredStrategy):
    """Bounded best-first tiler over Min's weighted layer objective."""

    beauty: BeautyWeights = field(
        default_factory=lambda: BeautyWeights.preset("balanced")
    )
    beam_width: int = 512
    w_g: float = 0.25
    """Cost per covered ungrounded-support column the rect fails to
    anchor (positive-only, so the best-first prune stays sound); 0.0
    restores pre-v5 behaviour."""
    _mirror_x: int | None = field(default=None, init=False)
    _mirror_y: int | None = field(default=None, init=False)
    _mirror_axis: int | None = field(default=None, init=False)
    _run_cost: float = field(default=0.0, init=False)

    def place(
        self,
        grid: VoxelGrid,
        *,
        rng: np.random.Generator,
        deadline: float | None = None,
    ) -> Layout:
        """Search both whole-model mirror axes and keep the better run.

        Every layer balancing about its own bbox centre is the drift blind
        spot the global-plane ``symmetry_error`` charges (a staircase of
        individually centred layers scores perfectly); the target footprint
        is known before any tiling. Fixing the axis as well as the centre makes
        the accumulated layer cost match the one-axis global objective.
        """
        footprint = grid.filled_mask.any(axis=2)
        xs, ys = np.nonzero(footprint)
        if not xs.size:
            return LayeredStrategy.place(self, grid=grid, rng=rng, deadline=deadline)

        self._mirror_x = int(xs.min()) + int(xs.max())
        self._mirror_y = int(ys.min()) + int(ys.max())
        overall_deadline = self._resolve_deadline(deadline)
        candidates: list[_AxisCandidate] = []
        try:
            for axis in (0, 1):
                axis_rng = deepcopy(rng)
                axis_deadline = overall_deadline
                if axis == 0 and overall_deadline is not None:
                    axis_deadline = deadline_share(
                        deadline=overall_deadline,
                        fraction=0.5,
                    )
                self._mirror_axis = axis
                self._run_cost = 0.0
                # Explicit base call: slots=True rebuilds the class, which breaks
                # the zero-argument super()'s captured __class__ cell.
                layout = LayeredStrategy._tile_layout(  # noqa: SLF001
                    self,
                    grid=grid,
                    rng=axis_rng,
                    deadline=axis_deadline,
                )
                candidates.append(
                    _AxisCandidate(
                        cost=self._run_cost,
                        axis=axis,
                        layout=layout,
                        rng=axis_rng,
                    )
                )
            best_cost = min(candidate.cost for candidate in candidates)
            finalists = [
                candidate
                for candidate in candidates
                if math.isclose(
                    candidate.cost,
                    best_cost,
                    rel_tol=_COST_TIE_TOLERANCE,
                    abs_tol=_COST_TIE_TOLERANCE,
                )
            ]
            finalized: list[_Finalist] = []
            for index, candidate in enumerate(finalists):
                self._mirror_axis = candidate.axis
                finalized.append(
                    _Finalist(
                        candidate=candidate,
                        topology=LayeredStrategy._finalize_layout(  # noqa: SLF001
                            self,
                            layout=candidate.layout,
                            grid=grid,
                            rng=candidate.rng,
                            deadline=deadline_share(
                                deadline=overall_deadline,
                                fraction=1 / (len(finalists) - index),
                            ),
                        ),
                    )
                )
            if len(finalized) == 1:
                selected = finalized[0].candidate
            else:
                component_target = grid.filled_component_count
                selected = min(
                    finalized,
                    key=lambda finalist: _candidate_key(
                        finalist,
                        component_target=component_target,
                    ),
                ).candidate
            rng.bit_generator.state = deepcopy(selected.rng.bit_generator.state)
            return selected.layout
        finally:
            self._mirror_x = None
            self._mirror_y = None
            self._mirror_axis = None
            self._run_cost = 0.0

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Search for the min-cost tiling; fall back to a random fill.

        The mirror centre comes from the whole-model footprint when
        :meth:`place` set one; a directly driven ``tile`` call falls back to
        the layer's own bbox, keeping the paper's per-layer behaviour.
        """
        order = sorted(problem.columns)
        mirror_x = (
            self._mirror_x
            if self._mirror_x is not None
            else min(x for x, _ in order) + max(x for x, _ in order)
        )
        mirror_y = (
            self._mirror_y
            if self._mirror_y is not None
            else min(y for _, y in order) + max(y for _, y in order)
        )
        best = self._search_tiling(
            problem,
            below,
            order,
            mirror=(mirror_x, mirror_y),
            deadline=deadline,
        )
        if best is None:
            best = self._fallback_tiling(
                problem,
                below,
                rng,
                mirror_x=mirror_x,
                mirror_y=mirror_y,
            )
        self._run_cost += best[0]
        return list(best[1])

    def _search_tiling(
        self,
        problem: LayerProblem,
        below: LayerContext,
        order: list[Column],
        *,
        mirror: tuple[int, int],
        deadline: float | None,
    ) -> tuple[float, tuple[Rect2D, ...]] | None:
        """Run bounded best-first search and return its best completion."""
        counter = 0
        open_list: list[_Node] = [(0.0, counter, frozenset(), ())]
        best: tuple[float, tuple[Rect2D, ...]] | None = None
        while open_list:
            if deadline is not None and time.monotonic() > deadline:
                break
            priority, _, covered, rects = heapq.heappop(open_list)
            if best is not None and priority >= best[0]:
                continue
            if covered == problem.columns:
                best = self._completed_node(
                    below,
                    rects,
                    priority=priority,
                    mirror_x=mirror[0],
                    mirror_y=mirror[1],
                    best=best,
                )
                continue
            counter = self._expand_node(
                problem,
                below,
                order,
                open_list,
                priority=priority,
                covered=covered,
                rects=rects,
                counter=counter,
            )
            if len(open_list) > self.beam_width:
                open_list = heapq.nsmallest(self.beam_width, open_list)
                heapq.heapify(open_list)
        return best

    def _fallback_tiling(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rng: np.random.Generator,
        *,
        mirror_x: int,
        mirror_y: int,
    ) -> tuple[float, tuple[Rect2D, ...]]:
        """Build and score a random completion when search finds none."""
        fallback = tuple(random_fill(problem, rng, self.catalog))
        fallback_cost = self._completed_node(
            below,
            fallback,
            priority=sum(self._rect_cost(below, rect) for rect in fallback),
            mirror_x=mirror_x,
            mirror_y=mirror_y,
            best=None,
        )
        if fallback_cost is None:  # defensive: best=None always returns a candidate
            msg = "fallback tiling did not produce a completed Beauty node"
            raise RuntimeError(msg)
        return fallback_cost

    def _expand_node(  # noqa: PLR0913 - explicit search state
        self,
        problem: LayerProblem,
        below: LayerContext,
        order: list[Column],
        open_list: list[_Node],
        *,
        priority: float,
        covered: frozenset[Column],
        rects: tuple[Rect2D, ...],
        counter: int,
    ) -> int:
        # Search-node fields remain explicit at this priority-queue mutation seam.
        # lizard forgives(parameter_count)
        seed = next(column for column in order if column not in covered)
        for rect in rects_covering(
            problem,
            seed,
            self.catalog,
            uncovered=problem.columns - covered,
        ):
            counter += 1
            heapq.heappush(
                open_list,
                (
                    priority + self._rect_cost(below, rect),
                    counter,
                    covered | rect.columns(),
                    (*rects, rect),
                ),
            )
        return counter

    def _completed_node(  # noqa: PLR0913 - explicit completion state
        self,
        below: LayerContext,
        rects: tuple[Rect2D, ...],
        *,
        priority: float,
        mirror_x: int,
        mirror_y: int,
        best: tuple[float, tuple[Rect2D, ...]] | None,
    ) -> tuple[float, tuple[Rect2D, ...]] | None:
        axes = (
            ((0, mirror_x), (1, mirror_y))
            if self._mirror_axis is None
            else (
                (self._mirror_axis, mirror_x if self._mirror_axis == 0 else mirror_y),
            )
        )
        balance = min(
            self._axis_balance_cost(rects, mirror_sum=mirror_sum, axis=axis)
            for axis, mirror_sum in axes
        )
        total = priority + balance + self._seam_cost(below, rects)
        return (total, rects) if best is None or total < best[0] else best

    def _rect_cost(self, below: LayerContext, rect: Rect2D) -> float:
        weights = self.beauty
        cost = weights.w_h * (_A_MAX - rect.area) / (_A_MAX - 1)
        columns = rect.columns()
        if below.grounded_below is not None:
            ungrounded_covered = sum(
                1
                for column in columns
                if column in below.support_of and column not in below.grounded_below
            )
            cost += (
                self.w_g * (ungrounded_covered - grounding_gain(rect, below)) / _A_MAX
            )
        exact_stack = below.stackable_footprints.get(columns)
        incompatible_exact_stack = (
            exact_stack is not None and merge_colour(rect.colour, exact_stack) is None
        )
        # A partial overlap is only charged when this rect's colour could
        # have completed the stack; a colour-incompatible rect never had
        # the vertical merge available, so it forfeits nothing by covering
        # part of the footprint.
        incomplete_stack = incompatible_exact_stack or any(
            columns & footprint
            and columns != footprint
            and merge_colour(rect.colour, expected_colour) is not None
            for footprint, expected_colour in below.stackable_footprints.items()
        )
        if incomplete_stack:
            cost += weights.w_v
        return cost

    def _axis_balance_cost(
        self,
        rects: tuple[Rect2D, ...],
        *,
        mirror_sum: int,
        axis: int,
    ) -> float:
        unbalanced = sum(
            not self._balanced(rects, rect, mirror_sum=mirror_sum, axis=axis)
            for rect in rects
        )
        return self.beauty.w_a * unbalanced

    def _balanced(
        self,
        placed: tuple[Rect2D, ...],
        rect: Rect2D,
        *,
        mirror_sum: int,
        axis: int,
    ) -> bool:
        if axis == 0:
            mirrored = (mirror_sum - rect.x1, rect.y0, mirror_sum - rect.x0, rect.y1)
        else:
            mirrored = (rect.x0, mirror_sum - rect.y1, rect.x1, mirror_sum - rect.y0)
        if mirrored == (rect.x0, rect.y0, rect.x1, rect.y1):
            return True  # centred on the axis
        return any(
            (other.x0, other.y0, other.x1, other.y1) == mirrored
            and other.colour == rect.colour
            for other in placed
        )

    def _seam_cost(
        self,
        below: LayerContext,
        rects: tuple[Rect2D, ...],
    ) -> float:
        if not below.seams:
            return 0.0
        cost = 0.0
        column_owner: dict[Column, int] = {}
        for index, rect in enumerate(rects):
            for column in rect.columns():
                column_owner[column] = index
        for (x, y), axis in below.seams:
            other = (x + 1, y) if axis == 0 else (x, y + 1)
            a = column_owner.get((x, y))
            b = column_owner.get(other)
            bridged = a is not None and a == b
            if not bridged:
                cost += self.beauty.w_s * below.seam_priority[((x, y), axis)]
        return cost
