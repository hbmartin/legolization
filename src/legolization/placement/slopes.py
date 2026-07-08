"""Surface finishing passes: slope fitting and tile capping.

Slope fitting smooths staircase surfaces: wherever a 1x1 brick column forms
a descending step (its side neighbour empty at those layers, with support or
ground just below), the column is replaced by a 45° slope whose sloped face
fills the step. This adds material outside the voxel shape — like the
sculpture papers do — so it is an opt-in pass.

Tile capping swaps exposed top plates for stud-less tiles of the same
footprint for a smooth finish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.catalog import Category

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout, PlacedBrick

_BRICK_PLATES = 3

# World step direction (slope descent) → yaw such that the rotated part's
# slope cell (local (0, 0), stud at (0, 1)) lands on the neighbour column.
_DESCENT_YAW = {
    (0, -1): 0,
    (1, 0): 90,
    (0, 1): 180,
    (-1, 0): 270,
}
# The rotated stud-cell offset for each yaw (rotate_offset((0, 1, 0), yaw)).
_STUD_OFFSET = {
    0: (0, 1),
    90: (-1, 0),
    180: (0, -1),
    270: (1, 0),
}


def apply_slopes(layout: Layout, grid: VoxelGrid) -> int:
    """Replace step-forming 1x1 brick columns with 45° slopes; return count."""
    replaced = 0
    for brick in list(layout):
        if _try_slope(layout, grid, brick):
            replaced += 1
    return replaced


def apply_tiles(layout: Layout) -> int:
    """Swap exposed top plates for same-footprint tiles; return count."""
    swapped = 0
    for brick in list(layout):
        part = layout.part_of(brick)
        if part.category is not Category.PLATE:
            continue
        studs_used = any(
            layout.brick_at((cx, cy, cz + 1)) is not None
            for cx, cy, cz in (c.cell for c in layout.connectors_of(brick, top=True))
        )
        if studs_used:
            continue
        xs = [x for x, _, _ in layout.cells_of(brick)]
        ys = [y for _, y, _ in layout.cells_of(brick)]
        tile_key = layout.catalog.rect_key(
            max(xs) - min(xs) + 1,
            max(ys) - min(ys) + 1,
            1,
            category=Category.TILE,
        )
        if tile_key is None:
            continue
        layout.remove(brick.brick_id)
        layout.add(
            tile_key, brick.x, brick.y, brick.layer, brick.yaw, brick.colour_code
        )
        swapped += 1
    return swapped


def _try_slope(layout: Layout, grid: VoxelGrid, brick: PlacedBrick) -> bool:
    """Replace one 1x1 brick with a slope if it sits on a descending step."""
    if brick.part_key != "brick_1x1":
        return False
    x, y, z = brick.x, brick.y, brick.layer
    for (dx, dy), yaw in _DESCENT_YAW.items():
        nx, ny = x + dx, y + dy
        if not _step_pattern(layout, grid, nx, ny, z):
            continue
        stud_dx, stud_dy = _STUD_OFFSET[yaw]
        anchor = (x - stud_dx, y - stud_dy)
        layout.remove(brick.brick_id)
        layout.add("slope_45_2x1", anchor[0], anchor[1], z, yaw, brick.colour_code)
        return True
    return False


def _step_pattern(
    layout: Layout,
    grid: VoxelGrid,
    nx: int,
    ny: int,
    z: int,
) -> bool:
    """Check the neighbour column can host a sloped face at ``z``."""
    gx, gy, gz = grid.shape
    if not (0 <= nx < gx and 0 <= ny < gy):
        return False
    # The step layers must be empty in the target shape and unoccupied.
    for dz in range(_BRICK_PLATES):
        if z + dz < gz and grid.codes[nx, ny, z + dz] >= 0:
            return False
        if layout.brick_at((nx, ny, z + dz)) is not None:
            return False
    # The slope's toe needs something to rest on: ground or a brick below.
    return z == 0 or layout.brick_at((nx, ny, z - 1)) is not None
