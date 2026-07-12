"""Kollsker & Malaguti's constructive brick-bonding heuristic (EJOR 2021).

Fills each layer in a random scan order, choosing the candidate that
minimizes ``c = 1 + h3(r) + alpha1*exp(-alpha2*d) + U[0, e_max)``: the
remainder lookahead ``h3`` estimates how many parts the rest of the run
still needs (exact equality-knapsack under rho = 25, 8-stud peeling above),
``d`` is the stagger distance from the candidate's leading border to the
nearest gap in the layer below along the scan axis, and the jitter
diversifies ties. After each layer an incomplete-construction repair
removes adjacent short pairs and refills them in the inverted direction,
keeping the result only when it uses fewer parts. The paper's 1D rows are
generalized per-row along the layer's scan axis (documented adaptation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.grid import colour_matches
from legolization.placement.greedy import _h_lookahead
from legolization.placement.layered.engine import (
    Column,
    LayerContext,
    LayeredStrategy,
    LayerProblem,
    Rect2D,
    rects_covering,
)

if TYPE_CHECKING:
    import numpy as np

_LENGTH_MAX = 8
_STAGGER_WINDOW = 4  # studs scanned for the nearest below-gap


@dataclass(slots=True)
class BondStrategy(LayeredStrategy):
    """Constructive filler with remainder lookahead and stagger reward."""

    e_max: float = 1.0
    repair_layers: bool = True

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Fill the layer in a random scan order by Kollsker's cost."""
        del deadline  # the constructive filler is single-pass and fast
        axis = int(rng.integers(2))
        flip_primary = bool(rng.integers(2))
        flip_secondary = bool(rng.integers(2))

        def scan_key(column: Column) -> tuple[float, float]:
            primary = column[axis]
            secondary = column[1 - axis]
            return (
                -primary if flip_primary else primary,
                -secondary if flip_secondary else secondary,
            )

        rects = self._fill(
            problem,
            below,
            rng,
            axis=axis,
            order=sorted(problem.columns, key=scan_key),
            columns=set(problem.columns),
        )
        if self.repair_layers:
            rects = self._repair(problem, below, rng, rects, axis=axis)
        return rects

    def _fill(  # noqa: PLR0913 - scan state is naturally wide
        self,
        problem: LayerProblem,
        below: LayerContext,
        rng: np.random.Generator,
        *,
        axis: int,
        order: list[Column],
        columns: set[Column],
    ) -> list[Rect2D]:
        uncovered = columns
        rects: list[Rect2D] = []
        for column in order:
            if column not in uncovered:
                continue
            best: tuple[float, Rect2D] | None = None
            for rect in rects_covering(
                problem, column, self.catalog, uncovered=uncovered
            ):
                cost = (
                    1.0
                    + self._lookahead(problem, uncovered, rect, axis)
                    + self._stagger_penalty(below, rect, axis)
                    + float(rng.random()) * self.e_max
                )
                if best is None or cost < best[0]:
                    best = (cost, rect)
            assert best is not None  # a 1x1 always fits  # noqa: S101
            rects.append(best[1])
            uncovered -= best[1].columns()
        return rects

    def _lookahead(
        self,
        problem: LayerProblem,
        uncovered: set[Column],
        rect: Rect2D,
        axis: int,
    ) -> float:
        """h3 of the remaining colour-compatible runs beyond both ends."""
        if axis == 0:
            starts = ((rect.x0 - 1, rect.y0), (rect.x1 + 1, rect.y0))
            step = (1, 0)
        else:
            starts = ((rect.x0, rect.y0 - 1), (rect.x0, rect.y1 + 1))
            step = (0, 1)
        total = 0
        for start, sign in zip(starts, (-1, 1), strict=True):
            run = 0
            x, y = start
            while (x, y) in uncovered and colour_matches(
                problem.colour_of[(x, y)], rect.colour
            ):
                run += 1
                x += sign * step[0]
                y += sign * step[1]
            total += _h_lookahead(run)
        return float(total)

    def _stagger_penalty(
        self,
        below: LayerContext,
        rect: Rect2D,
        axis: int,
    ) -> float:
        """Kollsker's d-term at the candidate's two borders along the axis."""
        penalty = 0.0
        for leading in (True, False):
            if (
                distance := self._gap_distance(below, rect, axis, leading=leading)
            ) is not None:
                penalty += self.weights.bond_alpha1 * math.exp(
                    -self.weights.bond_alpha2 * distance
                )
        return penalty

    def _gap_distance(
        self,
        below: LayerContext,
        rect: Rect2D,
        axis: int,
        *,
        leading: bool,
    ) -> int | None:
        """Distance from a border to the nearest below-seam along the axis."""
        # A seam between below-columns p and p+1 is keyed by p, so the
        # trailing border (between x0-1 and x0) probes from x0 - 1.
        if axis == 0:
            edge = rect.x1 if leading else rect.x0 - 1
            transverse = range(rect.y0, rect.y1 + 1)
        else:
            edge = rect.y1 if leading else rect.y0 - 1
            transverse = range(rect.x0, rect.x1 + 1)
        sign = 1 if leading else -1
        for distance in range(_STAGGER_WINDOW + 1):
            for t in transverse:
                position = edge + sign * distance
                column = (position, t) if axis == 0 else (t, position)
                if ((column, axis) in below.seams) or (column in below.gap_columns):
                    return distance
        return None

    def _repair(
        self,
        problem: LayerProblem,
        below: LayerContext,
        rng: np.random.Generator,
        rects: list[Rect2D],
        *,
        axis: int,
    ) -> list[Rect2D]:
        """Kollsker's incomplete-construction repair: refill short pairs.

        Adjacent pairs along the scan axis whose combined length is below
        the longest part get removed and refilled in the inverted
        direction; the result is kept only when it uses fewer parts.
        """
        short_pairs = [
            (a, b)
            for i, a in enumerate(rects)
            for b in rects[i + 1 :]
            if _adjacent_along(a, b, axis)
            and _axis_length(a, axis) + _axis_length(b, axis) < _LENGTH_MAX
        ]
        if not short_pairs:
            return rects
        removed: set[Column] = set()
        victims: list[Rect2D] = []
        for a, b in short_pairs:
            for rect in (a, b):
                if rect not in victims:
                    victims.append(rect)
                    removed |= rect.columns()
        survivors = [rect for rect in rects if rect not in victims]
        refill = self._fill(
            problem,
            below,
            rng,
            axis=axis,
            order=sorted(removed, key=lambda c: (-c[axis], -c[1 - axis])),
            columns=set(removed),
        )
        if len(survivors) + len(refill) < len(rects):
            return survivors + refill
        return rects


def _axis_length(rect: Rect2D, axis: int) -> int:
    return rect.width if axis == 0 else rect.length


def _adjacent_along(a: Rect2D, b: Rect2D, axis: int) -> bool:
    """Check that the rects touch along the scan axis with aligned rows."""
    if axis == 0:
        touching = a.x1 + 1 == b.x0 or b.x1 + 1 == a.x0
        aligned = not (a.y1 < b.y0 or b.y1 < a.y0)
    else:
        touching = a.y1 + 1 == b.y0 or b.y1 + 1 == a.y0
        aligned = not (a.x1 < b.x0 or b.x1 < a.x0)
    return touching and aligned
