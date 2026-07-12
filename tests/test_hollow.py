"""Hollowing and fill restoration."""

import numpy as np

from legolization.grid import EMPTY, IGNORE, VoxelGrid
from legolization.hollow import hollow_grid, restore_columns


def _solid_cube(n: int = 5) -> VoxelGrid:
    return VoxelGrid(codes=np.full((n, n, n), 4, dtype=np.int16))


def test_hollow_removes_interior_only():
    grid = _solid_cube()
    hollowed = hollow_grid(grid)
    assert hollowed.filled_count == grid.filled_count - 3 * 3 * 3
    # The shell is untouched.
    assert hollowed.code_at(0, 2, 2) == 4
    assert hollowed.code_at(2, 2, 2) == EMPTY


def test_restore_columns_refills():
    grid = _solid_cube()
    hollowed = hollow_grid(grid)
    restored = restore_columns(grid, hollowed, {(2, 2, 2)}, radius=0)
    # Restored cells were interior (invisible), so they come back as the
    # colour-free IGNORE label that merges with any brick colour.
    assert restored.code_at(2, 2, 2) == IGNORE
    assert restored.code_at(2, 2, 1) == IGNORE  # whole column comes back
    assert restored.filled_count == hollowed.filled_count + 3 * 3 * 3 // 9


def test_restore_columns_noop_returns_same_grid():
    grid = _solid_cube()
    hollowed = hollow_grid(grid)
    same = restore_columns(grid, hollowed, {(0, 0, 0)}, radius=0)
    assert same is hollowed  # column (0,0) is shell, nothing to restore
