"""Carve-and-refill surgery shared by the finishing passes.

A finishing pass that wants to claim a set of cells removes the rect
bricks covering them ("donors") and must put the cells the donors
covered *beyond* the claim back exactly. The refill tiling is computed
before any mutation — a failed candidate costs nothing — with colours
inherited per cell from the carved donors (the passes run after
``resolve_ignore_colours``, so grid codes may still be IGNORE).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from legolization.catalog import Category, rotate_offset

if TYPE_CHECKING:
    from legolization.catalog import Cell
    from legolization.layout import Layout, PlacedBrick


def covering_donors(
    layout: Layout,
    cells: set[Cell],
) -> dict[int, PlacedBrick] | None:
    """Rect bricks covering ``cells`` between them, or None.

    Only plain bricks and plates may be carved — slopes, tiles, and
    sideways parts placed by an earlier pass keep their cells.
    """
    donors: dict[int, PlacedBrick] = {}
    for cell in cells:
        if (donor := layout.brick_at(cell)) is None:
            return None
        if layout.part_of(donor).category not in (Category.BRICK, Category.PLATE):
            return None
        donors[donor.brick_id] = donor
    return donors


def refill_candidates(
    layout: Layout,
    remainder: set[Cell],
    colour_of: dict[Cell, int],
) -> list[tuple[str, Cell, int, int, tuple[Cell, ...]]]:
    """Enumerate rect placements inside ``remainder``, one colour each."""
    candidates: list[tuple[str, Cell, int, int, tuple[Cell, ...]]] = []
    seen: set[tuple[str, Cell, int]] = set()
    for part in layout.catalog.by_category(Category.BRICK, Category.PLATE):
        for yaw in part.orientations:
            offsets = [rotate_offset(cell, yaw) for cell in sorted(part.occupied_cells)]
            for seed in remainder:
                for ox, oy, oz in offsets:
                    anchor = (seed[0] - ox, seed[1] - oy, seed[2] - oz)
                    if anchor[2] < 0 or (part.key, anchor, yaw) in seen:
                        continue
                    seen.add((part.key, anchor, yaw))
                    cells = tuple(
                        (anchor[0] + dx, anchor[1] + dy, anchor[2] + dz)
                        for dx, dy, dz in offsets
                    )
                    if not all(cell in remainder for cell in cells):
                        continue
                    colours = {colour_of[cell] for cell in cells}
                    if len(colours) != 1:
                        continue
                    candidates.append((part.key, anchor, yaw, colours.pop(), cells))
    return candidates


def refill_tiling(
    layout: Layout,
    remainder: set[Cell],
    colour_of: dict[Cell, int],
) -> list[tuple[str, Cell, int, int]] | None:
    """Exact-cover ``remainder`` with rect parts, one colour per part."""
    if not remainder:
        return []
    candidates = refill_candidates(layout, remainder, colour_of)
    if not candidates:
        return None
    cells_sorted = sorted(remainder)
    cell_index = {cell: i for i, cell in enumerate(cells_sorted)}
    rows: list[int] = []
    cols: list[int] = []
    for col, (_, _, _, _, covered) in enumerate(candidates):
        for cell in covered:
            rows.append(cell_index[cell])
            cols.append(col)
    matrix = coo_matrix(
        (np.ones(len(rows)), (rows, cols)),
        shape=(len(cells_sorted), len(candidates)),
    ).tocsc()
    # Tiny rank term keeps the chosen cover deterministic among ties.
    costs = 1.0 + 1e-6 * np.arange(len(candidates))
    result = milp(
        c=costs,
        constraints=LinearConstraint(matrix, lb=1.0, ub=1.0),
        integrality=np.ones(len(candidates)),
        bounds=Bounds(0, 1),
    )
    if not result.success or result.x is None:
        return None
    return [
        (part_key, anchor, yaw, colour)
        for chosen, (part_key, anchor, yaw, colour, _) in zip(
            result.x, candidates, strict=True
        )
        if chosen > 0.5
    ]
