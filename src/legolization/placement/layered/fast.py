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

import heapq
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

    from legolization.placement.layered.engine import Column

_A_MAX = 16  # largest catalog footprint (2x8)

# Heap entries: (merge key, seq_a, seq_b, d_size, d_parallel, union). The
# seq pair is unique per entry, so ordering never compares the Rect2D.
_Merge = tuple[float, int, int, float, int, Rect2D]


def _size_term(rect: Rect2D) -> float:
    return (_A_MAX - rect.area) / _A_MAX


def _adjacent_pairs(owner: dict[Column, int]) -> list[tuple[int, int]]:
    """Distinct edge-adjacent rect pairs ``(seq_a < seq_b)`` in the index."""
    pairs: set[tuple[int, int]] = set()
    for (x, y), seq_a in owner.items():
        for neighbour in ((x + 1, y), (x, y + 1)):
            if (seq_b := owner.get(neighbour)) is not None and seq_b != seq_a:
                pairs.add((seq_a, seq_b) if seq_a < seq_b else (seq_b, seq_a))
    return sorted(pairs)


def _neighbours_of(
    owner: dict[Column, int],
    union: Rect2D,
    seq_union: int,
) -> list[int]:
    """Rect seqs sharing an edge with ``union``, excluding ``union`` itself."""
    found: set[int] = set()
    for x, y in union.columns():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            seq_n = owner.get((x + dx, y + dy))
            if seq_n is not None and seq_n != seq_union:
                found.add(seq_n)
    return sorted(found)


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
            rects = self._merge_layer(problem, below, rng, deadline=deadline)
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
        *,
        deadline: float | None,
    ) -> list[Rect2D]:
        rects = self._merge_to_fixpoint(
            problem,
            below,
            [
                Rect2D(x0=x, y0=y, x1=x, y1=y, colour=problem.colour_of[(x, y)])
                for x, y in sorted(problem.columns)
            ],
            deadline=deadline,
        )
        # Escape: split a random rect and remerge locally, keep if better.
        for _ in range(3):
            if len(rects) < 2 or (deadline is not None and time.monotonic() > deadline):
                break
            candidate = self._split_and_remerge(
                problem,
                below,
                rects,
                rng,
                deadline=deadline,
            )
            if self._cost(problem, below, candidate) < self._cost(
                problem, below, rects
            ):
                rects = candidate
            else:
                break
        return rects

    def _merge_to_fixpoint(  # noqa: C901 - incremental heap state machine
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
        *,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Apply best cost-improving pair merges until none remains.

        Every merge drops the rect count by exactly one, so among the
        current rects the merge minimizing the new cost is the one
        minimizing the pair-local delta ``w_s*d_size + w_d*d_parallel``.
        That delta keys a lazy heap — stale entries (a dead rect) are
        skipped on pop — giving O(1) evaluation per candidate instead of an
        O(N) cost recompute inside an O(N²) pair scan. Only rects tiling
        their joint bounding box can merge, so they share a full edge and
        candidate pairs come from a column-owner adjacency index.
        """
        alive: dict[int, Rect2D] = dict(enumerate(rects))
        if len(alive) < 2:
            return list(alive.values())
        columns_total = len(problem.columns)
        owner: dict[Column, int] = {
            column: seq for seq, rect in alive.items() for column in rect.columns()
        }
        parallel_of: dict[int, int] = {
            seq: int(self._parallel_to_support(below, rect))
            for seq, rect in alive.items()
        }
        size_sum = sum(_size_term(rect) for rect in alive.values())
        parallel_sum = sum(parallel_of.values())
        count = len(alive)
        heap: list[_Merge] = []

        def push_pair(seq_a: int, seq_b: int) -> None:
            union = mergeable_union(alive[seq_a], alive[seq_b], problem, self.catalog)
            if union is None:
                return
            d_size = (
                _size_term(union) - _size_term(alive[seq_a]) - _size_term(alive[seq_b])
            )
            d_parallel = (
                int(self._parallel_to_support(below, union))
                - parallel_of[seq_a]
                - parallel_of[seq_b]
            )
            key = self.w_s * d_size + self.w_d * d_parallel
            heapq.heappush(heap, (key, seq_a, seq_b, d_size, d_parallel, union))

        for pair in _adjacent_pairs(owner):
            push_pair(*pair)

        cost = (
            self.w_s * (size_sum / count)
            + self.w_n * (count / columns_total)
            + self.w_d * (parallel_sum / count)
        )
        next_seq = count
        while heap:
            if deadline is not None and time.monotonic() > deadline:
                break
            _, seq_a, seq_b, d_size, d_parallel, union = heapq.heappop(heap)
            if seq_a not in alive or seq_b not in alive:
                continue
            new_count = count - 1
            new_cost = (
                self.w_s * ((size_sum + d_size) / new_count)
                + self.w_n * (new_count / columns_total)
                + self.w_d * ((parallel_sum + d_parallel) / new_count)
            )
            if new_cost >= cost:
                break  # the best remaining merge no longer improves
            parallel_of[next_seq] = d_parallel + parallel_of[seq_a] + parallel_of[seq_b]
            del alive[seq_a], alive[seq_b]
            alive[next_seq] = union
            for column in union.columns():
                owner[column] = next_seq
            size_sum += d_size
            parallel_sum += d_parallel
            count, cost = new_count, new_cost
            for seq_n in _neighbours_of(owner, union=union, seq_union=next_seq):
                push_pair(seq_n, next_seq)
            next_seq += 1
        return list(alive.values())

    def _split_and_remerge(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
        rng: np.random.Generator,
        *,
        deadline: float | None,
    ) -> list[Rect2D]:
        victim = rects[int(rng.integers(len(rects)))]
        survivors = [rect for rect in rects if rect is not victim]
        refill = random_fill(
            problem,
            rng,
            self.catalog,
            holes=victim.columns(),
        )
        return self._merge_to_fixpoint(
            problem,
            below,
            survivors + refill,
            deadline=deadline,
        )

    def _cost(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rects: list[Rect2D],
    ) -> float:
        size = sum(_size_term(rect) for rect in rects) / len(rects)
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
