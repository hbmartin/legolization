"""Shape-preserving slope, tile, and plate-cap finishing passes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.catalog import Category, rotate_offset
from legolization.grid import EMPTY
from legolization.placement.carve import covering_donors, refill_tiling

if TYPE_CHECKING:
    from legolization.catalog import Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

_BRICK_PLATES = 3

# Preserve-mode candidates, largest filled profile first (mirrors the merge
# philosophy: prefer one big part over several small ones).
_PRESERVE_PARTS = (
    "slope_45_2x2",
    "slope_inverted_45_2x2",
    "slope_33_3x1",
    "slope_45_2x1",
    "slope_inverted_45_2x1",
)

# Carving donors frees at most this many cells to refill. Tuned on
# suzanne@16: caps 0/2/4/6/8 place 2/10/29/44/51 slopes; the structure
# stays stable (worst RBE score 0.137) through 4 and collapses at 8 —
# beyond that the carve fragments load-bearing bricks into weak stacks.
_MAX_REMAINDER = 4


@dataclass(frozen=True, slots=True)
class _SlopeCandidate:
    """One exact-profile replacement ranked before layout mutation."""

    part_key: str
    x: int
    y: int
    z: int
    yaw: int
    fill_size: int
    normal_error: float


def apply_slopes(
    layout: Layout,
    grid: VoxelGrid,
) -> int:
    """Swap exact in-shape profiles for slopes without changing occupancy."""
    return _apply_preserve(layout, grid)


def _apply_preserve(layout: Layout, grid: VoxelGrid) -> int:
    """Swap exact filled-profile matches for slopes; adds nothing.

    One full sweep per candidate part, largest profile first, so a big
    slope is never pre-empted by a smaller one matching at an earlier
    scan position.
    """
    placed = 0
    for item in _preserve_candidates(layout, grid):
        if _try_preserve(
            layout,
            grid,
            item.part_key,
            item.x,
            item.y,
            item.z,
            item.yaw,
        ):
            placed += 1
    return placed


def _preserve_candidates(layout: Layout, grid: VoxelGrid) -> list[_SlopeCandidate]:
    gx, gy, gz = grid.shape
    margin = 3
    candidates: list[_SlopeCandidate] = []
    for part_key in _PRESERVE_PARTS:
        part = layout.catalog.parts[part_key]
        for z in range(gz):
            for y in range(-margin, gy + margin):
                for x in range(-margin, gx + margin):
                    for yaw in part.orientations:
                        filled = set(part.filled_at(x, y, z, yaw))
                        void = set(part.cells_at(x, y, z, yaw)) - filled
                        if _profile_in_shape(
                            layout,
                            grid,
                            filled=filled,
                            void=void,
                        ):
                            candidates.append(
                                _SlopeCandidate(
                                    part_key=part_key,
                                    x=x,
                                    y=y,
                                    z=z,
                                    yaw=yaw,
                                    fill_size=len(filled),
                                    normal_error=_normal_error(
                                        grid,
                                        filled=filled,
                                        part_key=part_key,
                                        yaw=yaw,
                                    ),
                                )
                            )
    return sorted(
        candidates,
        key=lambda item: (
            -item.fill_size,
            item.normal_error,
            item.part_key,
            item.z,
            item.y,
            item.x,
            item.yaw,
        ),
    )


def _normal_error(
    grid: VoxelGrid,
    *,
    filled: set[Cell],
    part_key: str,
    yaw: int,
) -> float:
    features = grid.mesh_features
    if features is None:
        return 1.0
    normals = dict(features.local_normals)
    vectors = [normals[cell] for cell in filled if cell in normals]
    if not vectors:
        return 1.0
    # The signed-distance gradient points into the filled region; negate it
    # to compare with the exposed slope face's outward normal.
    observed = tuple(-sum(row[axis] for row in vectors) for axis in range(3))
    length = math.sqrt(sum(value * value for value in observed))
    if length == 0:
        return 1.0
    observed = tuple(value / length for value in observed)
    angle = math.radians(33.0 if "_33_" in part_key else 45.0)
    horizontal = rotate_offset((0, -1, 0), yaw)
    vertical = -1.0 if "inverted" in part_key else 1.0
    expected = (
        horizontal[0] * math.sin(angle),
        horizontal[1] * math.sin(angle),
        vertical * math.cos(angle),
    )
    dot = sum(left * right for left, right in zip(observed, expected, strict=True))
    return 1.0 - dot


def _try_preserve(  # noqa: PLR0913, PLR0917 - one candidate placement, locally owned
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
    """Exact-cover exposed top plates with available tiles; return tile count."""
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
        cells = set(layout.cells_of(brick))
        colour_of = dict.fromkeys(cells, brick.colour_code)
        tiling = refill_tiling(
            layout,
            cells,
            colour_of,
            categories=(Category.TILE,),
        )
        if tiling is None:
            continue
        layout.remove(brick.brick_id)
        for part_key, (x, y, z), yaw, colour in tiling:
            layout.add(part_key, x, y, z, yaw, colour)
        swapped += len(tiling)
    return swapped


def apply_plate_caps(layout: Layout) -> int:
    """Split exposed rectangular bricks into plate stacks without deformation."""
    added = 0
    for brick in list(layout):
        part = layout.part_of(brick)
        if part.category is not Category.BRICK or part.height_plates != _BRICK_PLATES:
            continue
        if any(
            layout.brick_at(
                (connector.cell[0], connector.cell[1], connector.cell[2] + 1)
            )
            is not None
            for connector in layout.connectors_of(brick, top=True)
        ):
            continue
        xs = [x for x, _, _ in layout.cells_of(brick)]
        ys = [y for _, y, _ in layout.cells_of(brick)]
        plate_key = layout.catalog.rect_key(
            max(xs) - min(xs) + 1,
            max(ys) - min(ys) + 1,
            1,
            category=Category.PLATE,
        )
        if plate_key is None:
            continue
        layout.remove(brick.brick_id)
        for layer_offset in range(_BRICK_PLATES):
            layout.add(
                plate_key,
                brick.x,
                brick.y,
                brick.layer + layer_offset,
                brick.yaw,
                brick.colour_code,
            )
        added += _BRICK_PLATES - 1
    return added
