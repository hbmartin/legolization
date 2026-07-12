"""Lee, Kim & Myung's split-and-merge genetic algorithm (IEEE Access 2018).

Each layer is solved by a GA over whole-layer chromosomes (lists of rects,
always a feasible exact cover). Fitness maximizes
``f = c1/n_b + c2*(1 - 1/(1+n_u)) + c3*(1 - 1/(1+n_p))`` — few bricks, many
distinct lower-layer bricks connected, many perpendicular coverings — with
the paper's weight discipline ``c1 > 2*(c2+c3)`` guaranteeing that dropping
a brick always beats any connectivity gain. Rank selection, one-point
directional crossover with delete-and-refill conflict resolution, and the
split-and-merge mutation (split one rect to 1x1s, grow another to its
largest mergeable union) with linearly decaying probability. Termination:
generation cap, fitness plateau, or the layer deadline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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

Chromosome = tuple[Rect2D, ...]


@dataclass(frozen=True, slots=True)
class SmGaConfig:
    """GA knobs; defaults keep ``c1 > 2 * (c2 + c3)`` (the paper's eq. 7)."""

    population: int = 50
    max_generations: int = 200  # the paper ran 1000; the plateau stop covers it
    patience: int = 30
    c1: float = 5.0
    c2: float = 1.0
    c3: float = 1.0
    p_mut_hi: float = 0.7
    p_mut_lo: float = 0.1

    def __post_init__(self) -> None:
        if self.c1 <= 2 * (self.c2 + self.c3):
            msg = "SM-GA requires c1 > 2*(c2 + c3) so brick count dominates"
            raise ValueError(msg)


@dataclass(slots=True)
class SmGaStrategy(LayeredStrategy):
    """Per-layer GA with the split-and-merge mutation."""

    config: SmGaConfig = field(default_factory=SmGaConfig)

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Evolve whole-layer tilings; return the fittest."""
        cfg = self.config
        population = [
            tuple(random_fill(problem, rng, self.catalog))
            for _ in range(cfg.population)
        ]
        fitnesses = [self._fitness(below, chromosome) for chromosome in population]
        best_fitness, best_index = max(
            zip(fitnesses, range(len(population)), strict=True)
        )
        best_chromosome = population[best_index]
        stale = 0
        for generation in range(cfg.max_generations):
            if deadline is not None and time.monotonic() > deadline:
                break
            p_mut = cfg.p_mut_hi - (cfg.p_mut_hi - cfg.p_mut_lo) * (
                generation / max(cfg.max_generations - 1, 1)
            )
            population, fitnesses = self._next_generation(
                problem, below, rng, population, fitnesses, p_mut
            )
            generation_best = max(zip(fitnesses, range(len(population)), strict=True))
            if generation_best[0] > best_fitness:
                best_fitness, best_index = generation_best
                best_chromosome = population[best_index]
                stale = 0
            else:
                stale += 1
                if stale >= cfg.patience:
                    break
        return list(best_chromosome)

    def _next_generation(  # noqa: PLR0913 - GA state is naturally wide
        self,
        problem: LayerProblem,
        below: LayerContext,
        rng: np.random.Generator,
        population: list[Chromosome],
        fitnesses: list[float],
        p_mut: float,
    ) -> tuple[list[Chromosome], list[float]]:
        order = sorted(range(len(population)), key=lambda i: fitnesses[i])
        ranks = [float(rank + 1) for rank in range(len(order))]
        total = sum(ranks)
        probabilities = [rank / total for rank in ranks]
        elite = population[order[-1]]

        children: list[Chromosome] = [elite]  # elitism keeps the best
        while len(children) < len(population):
            i, j = rng.choice(len(order), size=2, p=probabilities)
            parent_a = population[order[int(i)]]
            parent_b = population[order[int(j)]]
            child = self._crossover(problem, rng, parent_a, parent_b)
            if float(rng.random()) < p_mut:
                child = self._split_and_merge(problem, rng, child)
            children.append(child)
        return children, [self._fitness(below, child) for child in children]

    def _fitness(self, below: LayerContext, chromosome: Chromosome) -> float:
        cfg = self.config
        supports: set[int] = set()
        perpendicular = 0
        for rect in chromosome:
            rect_axis = rect.long_axis
            for column in rect.columns():
                if (support := below.support_of.get(column)) is not None:
                    supports.add(support)
                    if (
                        rect_axis is not None
                        and below.long_axis_of.get(support) is not None
                        and below.long_axis_of[support] != rect_axis
                    ):
                        perpendicular += 1
                        break
        n_b = len(chromosome)
        n_u = len(supports)
        n_p = perpendicular
        return (
            cfg.c1 / n_b
            + cfg.c2 * (1.0 - 1.0 / (1 + n_u))
            + cfg.c3 * (1.0 - 1.0 / (1 + n_p))
        )

    def _crossover(
        self,
        problem: LayerProblem,
        rng: np.random.Generator,
        parent_a: Chromosome,
        parent_b: Chromosome,
    ) -> Chromosome:
        """One-point directional crossover with delete-and-refill repair."""
        axis = int(rng.integers(2))
        values = sorted({column[axis] for column in problem.columns})
        cut = values[int(rng.integers(len(values)))]

        def low_side(rect: Rect2D) -> bool:
            return (rect.x1 if axis == 0 else rect.y1) < cut

        def high_side(rect: Rect2D) -> bool:
            return (rect.x0 if axis == 0 else rect.y0) >= cut

        kept = [rect for rect in parent_a if low_side(rect)]
        kept += [rect for rect in parent_b if high_side(rect)]
        covered: set[tuple[int, int]] = set()
        for rect in kept:
            covered |= rect.columns()
        holes = problem.columns - covered
        refill = random_fill(problem, rng, self.catalog, holes=holes) if holes else []
        return tuple(kept) + tuple(refill)

    def _split_and_merge(
        self,
        problem: LayerProblem,
        rng: np.random.Generator,
        chromosome: Chromosome,
    ) -> Chromosome:
        """Split one random rect to 1x1s, grow another to its largest union."""
        rects = list(chromosome)
        victim = rects.pop(int(rng.integers(len(rects))))
        rects.extend(
            Rect2D(x0=x, y0=y, x1=x, y1=y, colour=problem.colour_of[(x, y)])
            for x, y in sorted(victim.columns())
        )
        target_index = int(rng.integers(len(rects)))
        grown = True
        while grown:
            grown = False
            target = rects[target_index]
            best: tuple[int, int, Rect2D] | None = None
            for index, other in enumerate(rects):
                if index == target_index:
                    continue
                union = mergeable_union(target, other, problem, self.catalog)
                if union is not None and (best is None or union.area > best[0]):
                    best = (union.area, index, union)
            if best is not None:
                _, other_index, union = best
                for index in sorted((target_index, other_index), reverse=True):
                    rects.pop(index)
                rects.append(union)
                target_index = len(rects) - 1
                grown = True
        return tuple(rects)
