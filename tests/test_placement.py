"""Placement invariants: exact cover, colour fidelity, connectivity."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout
from legolization.placement.base import evaluate
from legolization.placement.greedy import GreedyStrategy
from legolization.placement.luo import LuoStrategy
from legolization.placement.merge import (
    atomize,
    maximal_random_merge,
    merged_rect,
    place_rect,
)
from legolization.placement.slopes import apply_slopes, apply_tiles


def _pyramid_grid() -> VoxelGrid:
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4 if z % 2 == 0 else 14
    return VoxelGrid.from_array(codes, plates_per_voxel=3)


def _random_grid(seed: int) -> VoxelGrid:
    rng = np.random.default_rng(seed)
    codes = np.full((5, 5, 6), EMPTY, dtype=np.int16)
    codes[:, :, 0] = 4  # solid grounded base
    mask = rng.random((5, 5, 5)) < 0.6
    for z in range(1, 6):
        column_ok = mask[:, :, z - 1] & (codes[:, :, z - 1] != EMPTY)
        codes[:, :, z][column_ok] = rng.choice([4, 14, 15])
    return VoxelGrid(codes=codes)


def _assert_exact_cover(layout: Layout, grid: VoxelGrid) -> None:
    covered: dict[tuple[int, int, int], int] = {}
    for brick in layout:
        for cell in layout.filled_cells_of(brick):
            assert cell not in covered, f"cell {cell} covered twice"
            covered[cell] = brick.colour_code
    filled = {
        (int(x), int(y), int(z))
        for x, y, z in zip(*grid.filled_mask.nonzero(), strict=True)
    }
    assert set(covered) == filled
    for cell, colour in covered.items():
        assert colour == grid.code_at(*cell), f"colour mismatch at {cell}"


@pytest.mark.parametrize("strategy_cls", [GreedyStrategy, LuoStrategy])
def test_strategies_cover_exactly(strategy_cls):
    grid = _pyramid_grid()
    strategy = strategy_cls()
    layout = strategy.place(grid, rng=np.random.default_rng(7))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


@pytest.mark.parametrize("seed", [1, 2])
def test_strategies_on_random_grid(seed):
    grid = _random_grid(seed)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(seed))
    _assert_exact_cover(layout, grid)


def test_atomize_slab_alignment():
    codes = np.full((1, 1, 7), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = atomize(grid, default_catalog())
    keys = sorted(b.part_key for b in layout)
    # 7 plates = 2 aligned brick slabs + 1 leftover plate.
    assert keys == ["brick_1x1", "brick_1x1", "plate_1x1"]
    _assert_exact_cover(layout, grid)


def test_maximal_merge_reduces_bricks():
    grid = _pyramid_grid()
    layout = atomize(grid, default_catalog())
    atoms = len(layout)
    maximal_random_merge(layout, np.random.default_rng(3))
    assert len(layout) < atoms
    _assert_exact_cover(layout, grid)


def test_merged_rect_rejects_colour_mismatch():
    layout = Layout(catalog=default_catalog())
    a = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    b = layout.add("brick_1x1", 1, 0, 0, 0, 14)
    assert merged_rect(layout, layout.bricks[a.brick_id], b) is None


def test_place_rect_yaw90_anchor():
    layout = Layout(catalog=default_catalog())
    brick = place_rect(layout, 0, 0, 1, 3, 0, 3, 4)
    assert brick.part_key == "brick_2x4"
    assert brick.yaw == 90
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    assert columns == {(x, y) for x in range(2) for y in range(4)}


def test_apply_slopes_on_step():
    codes = np.full((1, 3, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4  # one full-height column at y=0
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    replaced = apply_slopes(layout, grid)
    assert replaced == 1
    (brick,) = list(layout)
    assert brick.part_key == "slope_45_2x1"
    # The stud column stays where the 1x1 brick was.
    assert (0, 0, 0) in layout.filled_cells_of(brick)


def test_apply_tiles_caps_top_plates():
    layout = Layout(catalog=default_catalog())
    layout.add("plate_2x2", 0, 0, 0, 0, 4)
    swapped = apply_tiles(layout)
    assert swapped == 1
    (brick,) = list(layout)
    assert brick.part_key == "tile_2x2"


def test_apply_tiles_skips_supporting_plates():
    layout = Layout(catalog=default_catalog())
    layout.add("plate_2x2", 0, 0, 0, 0, 4)
    layout.add("plate_1x1", 0, 0, 1, 0, 4)
    assert apply_tiles(layout) == 1  # only the top 1x1 becomes a tile
    keys = sorted(b.part_key for b in layout)
    assert keys == ["plate_2x2", "tile_1x1"]


def test_greedy_bridges_straight_seams():
    # A 7-wide, two-brick-tall slab: the longest brick is 6, so largest-first
    # fill repeats a straight vertical seam on both layers and strands the
    # 7th column as its own tower. Reinforcement must remerge with staggered
    # seams into one component.
    codes = np.full((7, 2, 9), EMPTY, dtype=np.int16)
    codes[3, :, :3] = 4  # narrow grounded stem
    codes[:, :, 3:] = 4  # 7-wide slab on top, two brick layers
    grid = VoxelGrid(codes=codes)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(5))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_greedy_rebonds_phase_mismatched_columns():
    # Column x0 runs from the ground; column x1 starts at layer 4, so greedy
    # bricks the two columns at incompatible layer phases (0/3/6 vs 4/7) and
    # no brick merge can ever bridge them. Repair must re-phase via plates.
    codes = np.full((2, 2, 10), EMPTY, dtype=np.int16)
    codes[0, :, :] = 4
    codes[1, :, 4:] = 4
    grid = VoxelGrid(codes=codes)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(11))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_compact_vertical_reforms_bricks():
    from legolization.placement.merge import compact_vertical

    layout = Layout(catalog=default_catalog())
    for z in range(3):
        layout.add("plate_2x4", 0, 0, z, 0, 4)
    layout.add("plate_1x1", 0, 0, 3, 0, 4)  # lone plate on top stays
    assert compact_vertical(layout) == 1
    keys = sorted(b.part_key for b in layout)
    assert keys == ["brick_2x4", "plate_1x1"]


def test_evaluate_reports_terms():
    grid = _pyramid_grid()
    layout = GreedyStrategy(refine=False).place(grid, rng=np.random.default_rng(0))
    report = evaluate(layout, grid)
    assert 0 < report.cost <= 1
    assert report.colour_error == 0.0
    assert report.total >= 0
