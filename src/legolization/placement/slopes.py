"""Surface finishing passes: slope fitting and tile capping.

Slope fitting comes in two modes. ``"preserve"`` matches a slope part's own
``filled_cells`` profile against cells that are both inside the target shape
and exactly covered by existing bricks, then swaps those bricks for the
slope — zero material added or removed, so the exact-cover invariant holds.
``"smooth"`` is the legacy pass: wherever a 1x1 brick column forms a
descending step (its side neighbour empty at those layers, with support or
ground just below), the column is replaced by a 45° slope whose sloped face
fills the step — this *adds* material outside the voxel shape, like the
sculpture papers do. Both are opt-in.

Tile capping swaps exposed top plates for stud-less tiles of the same
footprint for a smooth finish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from legolization.catalog import Category
from legolization.grid import EMPTY
from legolization.placement.carve import covering_donors, refill_tiling

if TYPE_CHECKING:
    from legolization.catalog import Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout, PlacedBrick

type SlopeMode = Literal["preserve", "smooth"]

_BRICK_PLATES = 3

# Preserve-mode candidates, largest filled profile first (mirrors the merge
# philosophy: prefer one big part over several small ones).
_PRESERVE_PARTS = ("slope_45_2x2", "slope_33_3x1", "slope_45_2x1")

# Carving donors frees at most this many cells to refill. Tuned on
# suzanne@16: caps 0/2/4/6/8 place 2/10/29/44/51 slopes; the structure
# stays stable (worst RBE score 0.137) through 4 and collapses at 8 —
# beyond that the carve fragments load-bearing bricks into weak stacks.
_MAX_REMAINDER = 4

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


def apply_slopes(
    layout: Layout,
    grid: VoxelGrid,
    *,
    mode: SlopeMode = "smooth",
) -> int:
    """Fit slope bricks onto staircase surfaces; return the number placed."""
    if mode == "preserve":
        return _apply_preserve(layout, grid)
    replaced = 0
    for brick in list(layout):
        if _try_slope(layout, grid, brick):
            replaced += 1
    return replaced


def _apply_preserve(layout: Layout, grid: VoxelGrid) -> int:
    """Swap exact filled-profile matches for slopes; adds nothing.

    One full sweep per candidate part, largest profile first, so a big
    slope is never pre-empted by a smaller one matching at an earlier
    scan position.
    """
    gx, gy, gz = grid.shape
    margin = 3  # anchors of rotated parts can sit outside the min corner
    placed = 0
    for part_key in _PRESERVE_PARTS:
        part = layout.catalog.parts[part_key]
        for z in range(gz):
            for y in range(-margin, gy + margin):
                for x in range(-margin, gx + margin):
                    if any(
                        _try_preserve(layout, grid, part_key, x, y, z, yaw)
                        for yaw in part.orientations
                    ):
                        placed += 1
    return placed


def _try_preserve(  # noqa: PLR0913 - one candidate placement, locally owned
    layout: Layout,
    grid: VoxelGrid,
    part_key: str,
    x: int,
    y: int,
    z: int,
    yaw: int,
) -> bool:
    """Place one slope whose filled profile the shape and bricks support.

    Donor bricks covering the profile are carved out; cells they covered
    beyond the profile are refilled by an exact-cover tiling computed
    *before* any mutation, so the swap either happens whole or not at
    all — the layout's filled-cell set is unchanged either way.
    """
    part = layout.catalog.parts[part_key]
    filled = set(part.filled_at(x, y, z, yaw))
    void = set(part.cells_at(x, y, z, yaw)) - filled
    if not _profile_in_shape(layout, grid, filled=filled, void=void):
        return False
    if (donors := covering_donors(layout, filled)) is None:
        return False
    # Every donor overlaps the profile (that is how it was found), so the
    # slope's face colour is well-defined only if all donors agree.
    slope_colours = {donor.colour_code for donor in donors.values()}
    if len(slope_colours) != 1:
        return False
    colour_of = {
        cell: donor.colour_code
        for donor in donors.values()
        for cell in layout.cells_of(donor)
    }
    remainder = set(colour_of) - filled
    if len(remainder) > _MAX_REMAINDER:
        return False
    if (tiling := refill_tiling(layout, remainder, colour_of)) is None:
        return False
    layout.remove_many(donors)
    layout.add(part_key, x, y, z, yaw, slope_colours.pop())
    for tile_part, (ax, ay, az), tile_yaw, tile_colour in tiling:
        layout.add(tile_part, ax, ay, az, tile_yaw, tile_colour)
    return True


def _profile_in_shape(
    layout: Layout,
    grid: VoxelGrid,
    *,
    filled: set[Cell],
    void: set[Cell],
) -> bool:
    """Check filled cells are in the shape and void cells are outside it."""
    gx, gy, gz = grid.shape
    for cx, cy, cz in filled:
        if not (0 <= cx < gx and 0 <= cy < gy and 0 <= cz < gz):
            return False
        if grid.codes[cx, cy, cz] == EMPTY:  # not part of the target shape
            return False
    for cx, cy, cz in void:
        inside = 0 <= cx < gx and 0 <= cy < gy and 0 <= cz < gz
        if inside and grid.codes[cx, cy, cz] != EMPTY:  # shape needs this cell
            return False
        if layout.brick_at((cx, cy, cz)) is not None:
            return False
    return True


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
