"""Min et al.'s objective-driven tiling with balance terms (KSII TIIS 2018).

Each layer is tiled by bounded best-first search (the paper's A*, honestly
a beam search: the OPEN list is capped and the guidance heuristic is not
admissible). Nodes expand only through placements covering the first
uncovered column in scan order, which collapses permutations of the same
tiling into one path. The cost accumulates per placed rect:

- efficiency ``g_h``: small rects cost ``(A_MAX - area) / (A_MAX - 1)``;
- balance ``g_a``: a rect not centred on the layer's central axis and
  without an already-placed mirror partner costs 1 (evaluated for both
  axes, the finished tiling takes the better one);
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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self

from legolization.grid import merge_colour
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

if TYPE_CHECKING:
    import numpy as np

_A_MAX = 16  # largest catalog footprint (2x8)

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

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Search for the min-cost tiling; fall back to a random fill."""
        order = sorted(problem.columns)
        mirror_x = min(x for x, _ in order) + max(x for x, _ in order)
        mirror_y = min(y for _, y in order) + max(y for _, y in order)
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
                    mirror_x=mirror_x,
                    mirror_y=mirror_y,
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
        if best is not None:
            return list(best[1])
        return random_fill(problem, rng, self.catalog)

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
        balance = min(
            self._axis_balance_cost(rects, mirror_sum=mirror_x, axis=0),
            self._axis_balance_cost(rects, mirror_sum=mirror_y, axis=1),
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
