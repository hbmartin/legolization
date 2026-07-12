"""Layout-level beauty metrics shared by the objective and the strategies.

Two terms the papers quantify beyond seam bonding: alternating brick
directions between layers (SM-GA's perpendicularity count, Bao's direction
weight) and per-layer mirror symmetry (Min's balance term g_a). Both are
pure functions of a layout, normalized to [0, 1], lower is better.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.graph import GROUND_ID, ConnectionGraph

if TYPE_CHECKING:
    from legolization.layout import Layout, PlacedBrick


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
    """Min's balance term g_a: mean unbalanced-brick fraction per layer.

    A brick is balanced about a layer's central axis when it is centred on
    the axis or a same-shape, same-colour partner sits at the mirrored
    position; each layer takes its better axis (x or y).
    """
    layers: dict[int, list[PlacedBrick]] = {}
    for brick in layout:
        layers.setdefault(brick.layer, []).append(brick)
    if not layers:
        return 0.0
    total = 0.0
    for bricks in layers.values():
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
        errors = []
        for axis, mirror_sum in ((0, min(xs) + max(xs)), (1, min(ys) + max(ys))):
            unbalanced = 0
            for brick in bricks:
                mirrored = frozenset(
                    (mirror_sum - x, y) if axis == 0 else (x, mirror_sum - y)
                    for x, y in footprints[brick.brick_id]
                )
                if (mirrored, brick.part_key, brick.colour_code) not in shapes:
                    unbalanced += 1
            errors.append(unbalanced / len(bricks))
        total += min(errors)
    return total / len(layers)


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
