"""Shared pytest fixtures."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the explicit gate for tests that are too slow for the inner loop."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run slow benchmark, sweep, and real-render integration tests",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip slow tests unless the caller explicitly opts into them."""
    if config.getoption("--run-slow"):
        return
    skip = pytest.mark.skip(reason="slow test; pass --run-slow to include")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


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
