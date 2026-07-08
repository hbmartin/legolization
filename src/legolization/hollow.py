"""Stability-aware auto-hollowing.

Hollowing erodes the filled volume to a 1-cell shell (cheaper, lighter
models). The pipeline runs placement + the RBE on the hollowed grid and, if
physics disagrees, calls :func:`restore_columns` to put interior fill back
underneath the offending bricks and tries again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from legolization.grid import EMPTY, VoxelGrid

if TYPE_CHECKING:
    from legolization.catalog import Cell


def hollow_grid(grid: VoxelGrid) -> VoxelGrid:
    """Remove interior cells, keeping a 1-cell-thick shell."""
    codes = grid.codes.copy()
    codes[grid.interior_mask()] = EMPTY
    return grid.with_codes(codes)


def restore_columns(
    original: VoxelGrid,
    hollowed: VoxelGrid,
    trouble_cells: set[Cell],
    *,
    radius: int = 1,
) -> VoxelGrid:
    """Restore original interior fill in columns around trouble cells.

    Every grid column within ``radius`` (Chebyshev) of a trouble cell's
    column gets its full original content back, giving unstable regions
    solid material to bear on. Returns the (possibly identical) new grid.
    """
    codes = hollowed.codes.copy()
    nx, ny, _ = hollowed.shape
    columns = {(x, y) for x, y, _ in trouble_cells}
    for cx, cy in columns:
        x_lo, x_hi = max(cx - radius, 0), min(cx + radius + 1, nx)
        y_lo, y_hi = max(cy - radius, 0), min(cy + radius + 1, ny)
        region = codes[x_lo:x_hi, y_lo:y_hi, :]
        source = original.codes[x_lo:x_hi, y_lo:y_hi, :]
        region[region == EMPTY] = source[region == EMPTY]
    if np.array_equal(codes, hollowed.codes):
        return hollowed
    return hollowed.with_codes(codes)
