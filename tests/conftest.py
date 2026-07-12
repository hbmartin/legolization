"""Shared pytest fixtures."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout


@pytest.fixture
def bad_bridge() -> tuple[Layout, VoxelGrid]:
    """Build a bridge whose deck and load courses butt at mid-span."""
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 10, 0, 0, 0, 4)
    for level in (3, 6, 9):
        layout.add("brick_1x6", 0, 0, level, 0, 4)
        layout.add("brick_1x4", 6, 0, level, 0, 4)
        layout.add("brick_1x1", 10, 0, level, 0, 4)
    codes = np.full((11, 1, 12), EMPTY, dtype=np.int16)
    codes[0, 0, :3] = 4
    codes[10, 0, :3] = 4
    codes[:, 0, 3:] = 4
    return layout, VoxelGrid(codes=codes)
