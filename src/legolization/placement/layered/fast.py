"""Bao et al.'s greedy per-layer merge with proactive connectivity (2024).

Each layer starts as all-1x1 rects and hill-climbs three actions — pair
merge, multi-neighbour bounding-box merge, split-and-remerge escape — on
the cost ``C = w_s*size + w_n*count + w_d*parallel`` where the size term
must dominate and the direction term rewards bricks laid perpendicular to
the layer below. After tiling, the layer is regenerated with a fresh rng
substream (up to ``retry_max`` times) until every rect is supported,
keeping the best-connected attempt (Bao's regenerate-on-disconnect, without
the full DFS since the engine's connectivity repair backstops the rest).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.placement.layered.engine import (
    LayerContext,
    LayeredStrategy,
    LayerProblem,
    Rect2D,
    mergeable_union,
    random_fill,
)

if TYPE_CHECKING:
    import numpy as np

_A_MAX = 16  # largest catalog footprint (2x8)


@dataclass(slots=True)
class FastStrategy(LayeredStrategy):
    """Greedy merge with dominant size weight and connectivity retries."""

    w_s: float = 0.6
    w_n: float = 0.2
    w_d: float = 0.2
    retry_max: int = 10

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Greedy-merge the layer; retry when rects end up unsupported."""
        best: tuple[int, float, list[Rect2D]] | None = None
        for _ in range(max(self.retry_max, 1)):
            rects = self._merge_layer(problem, below, rng)
            unsupported = sum(1 for rect in rects if not self._supported(below, rect))
            cost = self._cost(problem, below, rects)
            if best is None or (unsupported, cost) < (best[0], best[1]):
                best = (unsupported, cost, rects)
            if best[0] == 0 or (deadline is not None and time.monotonic() > deadline):
                break
        assert best is not None  # noqa: S101 - loop runs at least once
        return best[2]

    def _merge_layer(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rng: np.random.Generator,
    ) -> list[Rect2D]:
        rects = [
            Rect2D(x0=x, y0=y, x1=x, y1=y, colour=problem.colour_of[(x, y)])
            for x, y in sorted(problem.columns)
        ]
        while True:
            improved = self._best_action(problem, below, rects)
            if improved is None:
                break
            rects = improved
        # Escape: split a random rect and remerge locally, keep if better.
        for _ in range(3):
            if len(rects) < 2:
                break
            candidate = self._split_and_remerge(problem, below, rects, rng)
            if candidate is not None and self._cost(
                problem, below, candidate
            ) < self._cost(problem, below, rects):
                rects = candidate
            else:
                break
        return rects

    def _best_action(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
    ) -> list[Rect2D] | None:
        """Apply the single merge with the best cost improvement, if any."""
        base_cost = self._cost(problem, below, rects)
        best: tuple[float, list[Rect2D]] | None = None
        for i, a in enumerate(rects):
            for j in range(i + 1, len(rects)):
                union = mergeable_union(a, rects[j], problem, self.catalog)
                if union is None:
                    continue
                candidate = [rect for k, rect in enumerate(rects) if k not in (i, j)]
                candidate.append(union)
                cost = self._cost(problem, below, candidate)
                if cost < base_cost and (best is None or cost < best[0]):
                    best = (cost, candidate)
        return None if best is None else best[1]

    def _split_and_remerge(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
        rng: np.random.Generator,
    ) -> list[Rect2D] | None:
        victim = rects[int(rng.integers(len(rects)))]
        survivors = [rect for rect in rects if rect is not victim]
        refill = random_fill(
            problem,
            rng,
            self.catalog,
            holes=victim.columns(),
        )
        candidate = survivors + refill
        while True:
            improved = self._best_action(problem, below, candidate)
            if improved is None:
                return candidate
            candidate = improved

    def _cost(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
    ) -> float:
        size = sum((_A_MAX - rect.area) / _A_MAX for rect in rects) / len(rects)
        count = len(rects) / len(problem.columns)
        parallel = sum(
            1 for rect in rects if self._parallel_to_support(below, rect)
        ) / len(rects)
        return self.w_s * size + self.w_n * count + self.w_d * parallel

    def _parallel_to_support(self, below: LayerContext, rect: Rect2D) -> bool:
        if (axis := rect.long_axis) is None:
            return False
        return any(
            below.long_axis_of.get(below.support_of.get(column, -1)) == axis
            for column in rect.columns()
        )

    def _supported(self, below: LayerContext, rect: Rect2D) -> bool:
        return any(column in below.support_of for column in rect.columns())
