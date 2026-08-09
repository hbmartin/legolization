"""Layout-level beauty metrics shared by the objective and the strategies.

Four terms, all pure functions of a layout, normalized to [0, 1], lower is
better. Two come from the placement papers: alternating brick directions
between layers (SM-GA's perpendicularity count, Bao's direction weight) and
mirror symmetry (Min's balance term g_a, corrected here to use one global
mirror plane). Two are audition terms measured against the external human
corpora before they may carry weight: exposed-surface colour speckle and
silhouette profile roughness. Validation lives in
``scripts/aesthetics_baseline.py`` (population separation) and
``scripts/aesthetics_drift.py`` (permutation drift); the standing verdicts are
recorded in ``docs/reports/aesthetics-validation.md``.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from legolization.graph import GROUND_ID, ConnectionGraph

if TYPE_CHECKING:
    from legolization.layout import Layout, PlacedBrick

_NEIGHBOURS: tuple[tuple[int, int, int], ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)
# One representative per unordered cell pair: the three positive directions.
_FORWARD_NEIGHBOURS: tuple[tuple[int, int, int], ...] = (
    (1, 0, 0),
    (0, 1, 0),
    (0, 0, 1),
)


def perpendicularity_error(layout: Layout) -> float:
    """Fraction of rectangular support pairs whose long axes are parallel.

    Crossing (perpendicular) bricks bond layers like plywood; square or
    1x1 parts carry no direction and are skipped, matching SM-GA's n_p.
    """
    axes = {brick.brick_id: _long_axis(layout, brick) for brick in layout}
    pairs = 0
    parallel = 0
    for below_id, above_id in ConnectionGraph.from_layout(layout).support_edges():
        if below_id == GROUND_ID:
            continue
        below_axis = axes[below_id]
        above_axis = axes[above_id]
        if below_axis is None or above_axis is None:
            continue
        pairs += 1
        if below_axis == above_axis:
            parallel += 1
    return parallel / pairs if pairs else 0.0


def symmetry_error(layout: Layout) -> float:
    """Global-plane mirror symmetry error: unbalanced fraction of all bricks.

    One mirror plane (x or y) and one mirror centre are shared by the whole
    model, both taken from the model's footprint bounding box; the better of
    the two axes is scored. A brick is balanced when it is centred on the
    plane or a same-shape, same-colour partner sits at the mirrored position
    in its own layer (a vertical mirror plane preserves the layer).

    This supersedes :func:`layer_symmetry_error`, whose per-layer axis and
    centre choices let a staircase of individually symmetric layers score a
    perfect 0.0 — validated against the human corpora, see
    ``docs/reports/aesthetics-validation.md``.
    """
    bricks = list(layout)
    if not bricks:
        return 0.0
    footprints = {
        brick.brick_id: frozenset((x, y) for x, y, _ in layout.cells_of(brick))
        for brick in bricks
    }
    layer_shapes: dict[int, set[tuple[frozenset[tuple[int, int]], str, int]]] = {}
    for brick in bricks:
        layer_shapes.setdefault(brick.layer, set()).add(
            (footprints[brick.brick_id], brick.part_key, brick.colour_code)
        )
    xs = [x for columns in footprints.values() for x, _ in columns]
    ys = [y for columns in footprints.values() for _, y in columns]
    unbalanced = min(
        _global_axis_unbalanced(
            bricks,
            footprints,
            layer_shapes,
            axis=axis,
            mirror_sum=mirror_sum,
        )
        for axis, mirror_sum in ((0, min(xs) + max(xs)), (1, min(ys) + max(ys)))
    )
    return unbalanced / len(bricks)


def layer_symmetry_error(layout: Layout) -> float:
    """Min's balance term g_a: mean unbalanced-brick fraction per layer.

    A brick is balanced about a layer's central axis when it is centred on
    the axis or a same-shape, same-colour partner sits at the mirrored
    position; each layer takes its better axis (x or y) and its own mirror
    centre. Superseded by :func:`symmetry_error` as an objective term; kept
    so the drift harness can compare the two formulations side by side.
    """
    layers: dict[int, list[PlacedBrick]] = {}
    for brick in layout:
        layers.setdefault(brick.layer, []).append(brick)
    if not layers:
        return 0.0
    return sum(
        _layer_symmetry_error(layout, bricks) for bricks in layers.values()
    ) / len(layers)


def colour_speckle_error(layout: Layout) -> float:
    """Fraction of exposed brick-to-brick cell adjacencies that change colour.

    A cell is exposed when any of its six neighbours is unoccupied. Every
    unordered pair of exposed, adjacent cells belonging to two different
    bricks is one visible surface junction; the score is the share of those
    junctions where the colour changes. Dithered per-brick colour assignment
    produces many short colour runs (high); coherent colour blocking produces
    large same-colour regions (low). Intentional multi-colour boundaries are
    charged too — the population baseline decides whether that noise floor
    still separates human from machine output.
    """
    occupancy = layout.occupancy
    colour_of = {brick.brick_id: brick.colour_code for brick in layout}
    exposed = {
        cell
        for cell in occupancy
        if any(
            (cell[0] + dx, cell[1] + dy, cell[2] + dz) not in occupancy
            for dx, dy, dz in _NEIGHBOURS
        )
    }
    junctions = 0
    changes = 0
    for x, y, z in exposed:
        for dx, dy, dz in _FORWARD_NEIGHBOURS:
            other = (x + dx, y + dy, z + dz)
            if other not in exposed:
                continue
            brick_a = occupancy[x, y, z]
            brick_b = occupancy[other]
            if brick_a == brick_b:
                continue
            junctions += 1
            if colour_of[brick_a] != colour_of[brick_b]:
                changes += 1
    return changes / junctions if junctions else 0.0


def profile_roughness(layout: Layout) -> float:
    """Mean Jaccard distance between consecutive layers' footprints.

    Smooth tapers change few columns between layers (low); ragged silhouettes
    change many (high). For layouts produced by this pipeline every strategy
    fills the same voxel grid, so the term is placement-invariant there — it
    is a population and shape diagnostic that informs voxelization and
    finishing, never a placement tie-breaker.
    """
    columns_by_layer: dict[int, set[tuple[int, int]]] = {}
    for x, y, z in layout.occupancy:
        columns_by_layer.setdefault(z, set()).add((x, y))
    layers = sorted(columns_by_layer)
    steps = [
        (columns_by_layer[a], columns_by_layer[b])
        for a, b in itertools.pairwise(layers)
        if b == a + 1
    ]
    if not steps:
        return 0.0
    return sum(len(below ^ above) / len(below | above) for below, above in steps) / len(
        steps
    )


def _global_axis_unbalanced(
    bricks: list[PlacedBrick],
    footprints: dict[int, frozenset[tuple[int, int]]],
    layer_shapes: dict[int, set[tuple[frozenset[tuple[int, int]], str, int]]],
    *,
    axis: int,
    mirror_sum: int,
) -> int:
    unbalanced = 0
    for brick in bricks:
        mirrored = frozenset(
            (mirror_sum - x, y) if axis == 0 else (x, mirror_sum - y)
            for x, y in footprints[brick.brick_id]
        )
        key = (mirrored, brick.part_key, brick.colour_code)
        if key not in layer_shapes[brick.layer]:
            unbalanced += 1
    return unbalanced


def _layer_symmetry_error(layout: Layout, bricks: list[PlacedBrick]) -> float:
    footprints = {
        brick.brick_id: frozenset((x, y) for x, y, _ in layout.cells_of(brick))
        for brick in bricks
    }
    shapes = {
        (footprints[brick.brick_id], brick.part_key, brick.colour_code)
        for brick in bricks
    }
    xs = [x for columns in footprints.values() for x, _ in columns]
    ys = [y for columns in footprints.values() for _, y in columns]
    errors = [
        _axis_symmetry_error(
            bricks,
            footprints,
            shapes,
            axis=axis,
            mirror_sum=mirror_sum,
        )
        for axis, mirror_sum in ((0, min(xs) + max(xs)), (1, min(ys) + max(ys)))
    ]
    return min(errors)


def _axis_symmetry_error(
    bricks: list[PlacedBrick],
    footprints: dict[int, frozenset[tuple[int, int]]],
    shapes: set[tuple[frozenset[tuple[int, int]], str, int]],
    *,
    axis: int,
    mirror_sum: int,
) -> float:
    unbalanced = 0
    for brick in bricks:
        mirrored = frozenset(
            (mirror_sum - x, y) if axis == 0 else (x, mirror_sum - y)
            for x, y in footprints[brick.brick_id]
        )
        if (mirrored, brick.part_key, brick.colour_code) not in shapes:
            unbalanced += 1
    return unbalanced / len(bricks)


def _long_axis(layout: Layout, brick: PlacedBrick) -> int | None:
    """0 for x-long, 1 for y-long, None for squares (no direction)."""
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    xs = [x for x, _ in columns]
    ys = [y for _, y in columns]
    x_extent = max(xs) - min(xs)
    y_extent = max(ys) - min(ys)
    if x_extent == y_extent:
        return None
    return 0 if x_extent > y_extent else 1
