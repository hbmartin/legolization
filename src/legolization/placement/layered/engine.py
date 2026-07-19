"""Shared machinery for per-layer 2D tiling strategies.

The four literature strategies (Kollsker bond, Bao fast, Lee SM-GA, Min
beauty) are all per-layer tilers conditioned on the layer below. Everything
they share lives here: slab decomposition at plate resolution (mirroring
:func:`legolization.placement.merge.atomize`'s absolute 3-plate policy),
2D rectangle enumeration against the catalog, below-layer context (supports,
seams, brick directions), conversion to :class:`~legolization.layout.Layout`
via :func:`~legolization.placement.merge.place_rect`, and the template
:class:`LayeredStrategy` that iterates problems bottom-up. Global physics is
deliberately absent — the pipeline-level repair engine owns it.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from legolization import telemetry
from legolization.catalog import Catalog, Category, default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.grid import EMPTY, merge_colour
from legolization.layout import Layout
from legolization.placement.base import ObjectiveWeights
from legolization.placement.merge import (
    BRIDGE_DRAWS,
    compact_vertical,
    improve_connectivity,
    place_rect,
)
from legolization.stability.solver import SolverConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import numpy as np

    from legolization.grid import VoxelGrid
    from legolization.layout import PlacedBrick

Column = tuple[int, int]

_BRICK_PLATES = 3


@dataclass(frozen=True, slots=True)
class Rect2D:
    """An inclusive footprint rectangle within one layer problem."""

    x0: int
    y0: int
    x1: int
    y1: int
    colour: int

    @property
    def width(self) -> int:
        """Extent along x."""
        return self.x1 - self.x0 + 1

    @property
    def length(self) -> int:
        """Extent along y."""
        return self.y1 - self.y0 + 1

    @property
    def area(self) -> int:
        """Covered column count."""
        return self.width * self.length

    @property
    def long_axis(self) -> int | None:
        """0 for x-long, 1 for y-long, None for squares."""
        if self.width == self.length:
            return None
        return 0 if self.width > self.length else 1

    def columns(self) -> frozenset[Column]:
        """All covered columns."""
        return frozenset(
            (x, y)
            for x in range(self.x0, self.x1 + 1)
            for y in range(self.y0, self.y1 + 1)
        )


@dataclass(frozen=True, slots=True)
class LayerProblem:
    """One 2D exact-cover tiling instance."""

    layer: int
    height_plates: int
    columns: frozenset[Column]
    colour_of: dict[Column, int]


@dataclass(frozen=True, slots=True)
class LayerContext:
    """What the partially built layout looks like below a layer problem.

    ``seam_priority`` carries Min's stability priority p per below-seam:
    1.0 when the two bricks are in different components (bridging is
    urgent), 0.5 when connected but without a shared direct support, 0.1
    when they already share a supporter.
    """

    support_of: dict[Column, int]
    gap_columns: frozenset[Column]
    seams: dict[tuple[Column, int], tuple[int, int]]
    seam_priority: dict[tuple[Column, int], float]
    long_axis_of: dict[int, int | None]
    stackable_footprints: dict[frozenset[Column], int]


@dataclass(slots=True)
class LayeredStrategy:
    """Template for per-layer tilers; subclasses implement :meth:`tile`."""

    catalog: Catalog = field(default_factory=default_catalog)
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    solver_config: SolverConfig = field(default_factory=SolverConfig)
    fail_max: int = 30
    time_budget_s: float | None = None
    progress: Callable[[str], None] | None = None

    def place(self, grid: VoxelGrid, *, rng: np.random.Generator) -> Layout:
        """Tile every layer problem bottom-up, then repair topology."""
        layout = Layout(catalog=self.catalog)
        problems = slab_decompose(grid)
        total = sum(len(problem.columns) for problem in problems) or 1
        deadline = time.monotonic() + self.time_budget_s if self.time_budget_s else None
        done = 0
        for index, problem in enumerate(problems):
            context = build_context(layout, problem)
            share = len(problem.columns) / total
            sub_deadline = (
                None
                if deadline is None
                else min(
                    deadline,
                    time.monotonic() + share * max(deadline - time.monotonic(), 0.0),
                )
            )
            rects = self.tile(problem, context, rng=rng, deadline=sub_deadline)
            _assert_cover(problem, rects)
            realize(layout, problem, rects)
            done += len(problem.columns)
            if self.progress is not None:
                self.progress(
                    f"layer {index + 1}/{len(problems)} "
                    f"({100 * done // total}% of cells)"
                )
        recording = telemetry.current() is not None
        telemetry.value("place.tiled.bricks", len(layout))
        if recording:  # graph builds only when a session is recording
            telemetry.value(
                "place.tiled.components",
                ConnectionGraph.from_layout(layout).component_count(),
            )
        compact_vertical(layout)
        telemetry.value("place.compacted.bricks", len(layout))
        if recording:
            telemetry.value(
                "place.compacted.components",
                ConnectionGraph.from_layout(layout).component_count(),
            )
        # Layered tilings are per-layer minima worth defending: best-of-k
        # bridging resists the count inflation measured in
        # docs/kollsker-drift-report.md. The greedy path keeps the
        # historical single draw (shipped goldens pin its exact bytes).
        improve_connectivity(
            layout,
            grid,
            rng,
            fail_max=self.fail_max,
            bridge_draws=BRIDGE_DRAWS,
            deadline=deadline,
        )
        telemetry.value("place.connected.bricks", len(layout))
        if telemetry.current() is not None:  # graph build only when recording
            telemetry.value(
                "place.connected.components",
                ConnectionGraph.from_layout(layout).component_count(),
            )
        return layout

    def tile(
        self,
        problem: LayerProblem,
        below: LayerContext,
        *,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> list[Rect2D]:
        """Produce an exact cover of ``problem.columns``."""
        raise NotImplementedError


def slab_decompose(grid: VoxelGrid) -> list[LayerProblem]:
    """Split the grid into brick problems (3-plate slabs) + plate problems.

    Mirrors ``atomize``: columns filled and colour-compatible across a full
    absolute 3-plate slab form one brick problem; leftover filled cells form
    per-layer plate problems. Problems are ordered bottom-up.
    """
    nx, ny, nz = grid.shape
    problems: list[LayerProblem] = []
    for slab_base in range(0, nz, _BRICK_PLATES):
        brick_columns: dict[Column, int] = {}
        plate_columns: dict[int, dict[Column, int]] = {}
        for x in range(nx):
            for y in range(ny):
                layers = range(slab_base, min(slab_base + _BRICK_PLATES, nz))
                codes = [int(grid.codes[x, y, z]) for z in layers]
                colour = (
                    merge_colour(*codes)
                    if len(codes) == _BRICK_PLATES and EMPTY not in codes
                    else None
                )
                if colour is not None:
                    brick_columns[(x, y)] = colour
                    continue
                for z, code in zip(layers, codes, strict=True):
                    if code != EMPTY:
                        plate_columns.setdefault(z, {})[(x, y)] = code
        if brick_columns:
            problems.append(
                LayerProblem(
                    layer=slab_base,
                    height_plates=_BRICK_PLATES,
                    columns=frozenset(brick_columns),
                    colour_of=brick_columns,
                )
            )
        for z in sorted(plate_columns):
            columns = plate_columns[z]
            problems.append(
                LayerProblem(
                    layer=z,
                    height_plates=1,
                    columns=frozenset(columns),
                    colour_of=columns,
                )
            )
    problems.sort(key=lambda problem: (problem.layer, -problem.height_plates))
    return problems


def build_context(layout: Layout, problem: LayerProblem) -> LayerContext:
    """Extract the below-layer facts a tiler conditions on."""
    base = problem.layer
    support_of: dict[Column, int] = {}
    gaps: set[Column] = set()
    for column in problem.columns:
        x, y = column
        if base == 0:
            support_of[column] = GROUND_ID
            continue
        below = layout.brick_at((x, y, base - 1))
        if below is None:
            gaps.add(column)
        else:
            support_of[column] = below.brick_id

    seams = _seams_of(problem, support_of)
    long_axis_of = {
        brick_id: _brick_long_axis(layout, layout.bricks[brick_id])
        for brick_id in set(support_of.values())
        if brick_id != GROUND_ID
    }
    return LayerContext(
        support_of=support_of,
        gap_columns=frozenset(gaps),
        seams=seams,
        seam_priority=_seam_priorities(layout, seams),
        long_axis_of=long_axis_of,
        stackable_footprints=_stackable_footprints(layout, problem),
    )


def _seams_of(
    problem: LayerProblem,
    support_of: dict[Column, int],
) -> dict[tuple[Column, int], tuple[int, int]]:
    seams: dict[tuple[Column, int], tuple[int, int]] = {}
    for column in problem.columns:
        x, y = column
        for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
            a = support_of.get(column)
            b = support_of.get((x + dx, y + dy))
            if a is not None and b is not None and a != b and GROUND_ID not in (a, b):
                seams[(column, axis)] = (a, b)
    return seams


def _seam_priorities(
    layout: Layout,
    seams: dict[tuple[Column, int], tuple[int, int]],
) -> dict[tuple[Column, int], float]:
    if not seams:
        return {}
    graph = ConnectionGraph.from_layout(layout)
    components = graph.brick_components()
    supporters: dict[int, set[int]] = {}
    for below_id, above_id in graph.support_edges():
        supporters.setdefault(above_id, set()).add(below_id)
    priorities: dict[tuple[Column, int], float] = {}
    for key, (a, b) in seams.items():
        if components.get(a) != components.get(b):
            priorities[key] = 1.0
        elif supporters.get(a, set()) & supporters.get(b, set()):
            priorities[key] = 0.1
        else:
            priorities[key] = 0.5
    return priorities


def rect_dims(catalog: Catalog, height_plates: int) -> tuple[tuple[int, int], ...]:
    """Distinct catalog footprints ``(width, length)`` for a part height."""
    return _rect_dims_cached(catalog, height_plates)


@lru_cache(maxsize=8)
def _rect_dims_cached(
    catalog: Catalog,
    height_plates: int,
) -> tuple[tuple[int, int], ...]:
    dims: set[tuple[int, int]] = set()
    for part in catalog.by_category(Category.BRICK, Category.PLATE):
        if part.height_plates != height_plates:
            continue
        xs = [dx for dx, _ in part.footprint]
        ys = [dy for _, dy in part.footprint]
        width = max(xs) - min(xs) + 1
        length = max(ys) - min(ys) + 1
        dims.add((width, length))
        dims.add((length, width))
    return tuple(sorted(dims, key=lambda d: (-d[0] * d[1], d)))


def rects_covering(
    problem: LayerProblem,
    column: Column,
    catalog: Catalog,
    *,
    uncovered: frozenset[Column] | set[Column] | None = None,
) -> list[Rect2D]:
    """All catalog-feasible colour-compatible rects containing ``column``.

    ``uncovered`` restricts the footprint to still-free columns (defaults
    to the whole problem).
    """
    free = problem.columns if uncovered is None else uncovered
    cx, cy = column
    results: list[Rect2D] = []
    for width, length in rect_dims(catalog, problem.height_plates):
        for x0 in range(cx - width + 1, cx + 1):
            for y0 in range(cy - length + 1, cy + 1):
                rect_columns = [
                    (x, y)
                    for x in range(x0, x0 + width)
                    for y in range(y0, y0 + length)
                ]
                if not all(col in free for col in rect_columns):
                    continue
                colour = merge_colour(*(problem.colour_of[col] for col in rect_columns))
                if colour is None:
                    continue
                results.append(
                    Rect2D(
                        x0=x0,
                        y0=y0,
                        x1=x0 + width - 1,
                        y1=y0 + length - 1,
                        colour=colour,
                    )
                )
    return results


def enumerate_layer_rects(
    problem: LayerProblem,
    columns: Iterable[Column],
    catalog: Catalog,
) -> list[Rect2D]:
    """Every catalog-feasible rect inside ``columns``, deterministic order.

    The order is load-bearing: MILP consumers add rank-based tiebreak
    costs indexed by position in this list.
    """
    free = frozenset(columns)
    seen: set[tuple[int, int, int, int]] = set()
    rects: list[Rect2D] = []
    for column in sorted(free):
        for rect in rects_covering(problem, column, catalog, uncovered=free):
            key = (rect.x0, rect.y0, rect.x1, rect.y1)
            if key not in seen:
                seen.add(key)
                rects.append(rect)
    return rects


def mergeable_union(
    a: Rect2D,
    b: Rect2D,
    problem: LayerProblem,
    catalog: Catalog,
) -> Rect2D | None:
    """Return the union rect if ``a`` + ``b`` tile it exactly, else None."""
    x0, y0 = min(a.x0, b.x0), min(a.y0, b.y0)
    x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
    width, length = x1 - x0 + 1, y1 - y0 + 1
    if width * length != a.area + b.area:
        return None
    if catalog.rect_key(width, length, problem.height_plates) is None:
        return None
    colour = merge_colour(a.colour, b.colour)
    if colour is None:
        return None
    return Rect2D(x0=x0, y0=y0, x1=x1, y1=y1, colour=colour)


def random_fill(
    problem: LayerProblem,
    rng: np.random.Generator,
    catalog: Catalog,
    *,
    holes: Iterable[Column] | None = None,
    bias_large: bool = True,
) -> list[Rect2D]:
    """Feasible random exact cover, larger rects weighted higher (SM-GA)."""
    free: set[Column] = set(problem.columns if holes is None else holes)
    ordered = sorted(free)  # kept in lockstep with ``free`` for O(1) picks
    rects: list[Rect2D] = []
    while ordered:
        column = ordered[int(rng.integers(len(ordered)))]
        candidates = rects_covering(problem, column, catalog, uncovered=free)
        if bias_large:
            weights = [float(rect.area) for rect in candidates]
            total = sum(weights)
            pick = candidates[
                int(rng.choice(len(candidates), p=[w / total for w in weights]))
            ]
        else:
            pick = candidates[int(rng.integers(len(candidates)))]
        rects.append(pick)
        free -= pick.columns()
        for covered in pick.columns():
            del ordered[bisect_left(ordered, covered)]
    return rects


def realize(
    layout: Layout,
    problem: LayerProblem,
    rects: Iterable[Rect2D],
) -> list[PlacedBrick]:
    """Place every rect into the layout at the problem's layer and height."""
    return [
        place_rect(
            layout,
            rect.x0,
            rect.y0,
            rect.x1,
            rect.y1,
            problem.layer,
            problem.height_plates,
            rect.colour,
        )
        for rect in rects
    ]


def _assert_cover(problem: LayerProblem, rects: list[Rect2D]) -> None:
    covered: set[Column] = set()
    for rect in rects:
        columns = rect.columns()
        if covered & columns:
            msg = f"tiler double-covered columns at layer {problem.layer}"
            raise RuntimeError(msg)
        covered |= columns
    if covered != problem.columns:
        msg = f"tiler missed columns at layer {problem.layer}"
        raise RuntimeError(msg)


def _brick_long_axis(layout: Layout, brick: PlacedBrick) -> int | None:
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    xs = [x for x, _ in columns]
    ys = [y for _, y in columns]
    x_extent = max(xs) - min(xs)
    y_extent = max(ys) - min(ys)
    if x_extent == y_extent:
        return None
    return 0 if x_extent > y_extent else 1


def _stackable_footprints(
    layout: Layout,
    problem: LayerProblem,
) -> dict[frozenset[Column], int]:
    """Footprints that would complete a 3-plate stack under this problem.

    Only meaningful for plate problems: a rect matching one of these
    footprints closes a stack ``compact_vertical`` can turn into a brick
    (Min's vertical-merge reward g_v, reinterpreted at plate resolution).
    """
    if problem.height_plates != 1:
        return {}
    footprints: dict[frozenset[Column], int] = {}
    seen: set[int] = set()
    for column in problem.columns:
        below = layout.brick_at((*column, problem.layer - 1))
        if below is None or below.brick_id in seen:
            continue
        seen.add(below.brick_id)
        if layout.part_of(below).height_plates != 1:
            continue
        if below.layer != problem.layer - 1:
            continue
        footprint = frozenset((x, y) for x, y, _ in layout.cells_of(below))
        deeper = layout.brick_at((*column, problem.layer - 2))
        if (
            deeper is not None
            and deeper.layer == problem.layer - 2
            and layout.part_of(deeper).height_plates == 1
            and frozenset((x, y) for x, y, _ in layout.cells_of(deeper)) == footprint
            and (merged_colour := merge_colour(below.colour_code, deeper.colour_code))
            is not None
        ):
            footprints[footprint] = merged_colour
    return footprints
