"""ALNS stability repair: QP localization, destroy/refill, convergence."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout
from legolization.placement.repair import (
    RepairConfig,
    _milp_fill,
    repair_stability,
)
from legolization.stability import analyze
from legolization.stability.links import localize_instability


def _bad_bridge() -> tuple[Layout, VoxelGrid]:
    """Build the collapsing butt-jointed bridge (see test_placement)."""
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


def test_localize_stable_structure_has_zero_q():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    report = localize_instability(layout)
    assert report.stable
    assert report.q == pytest.approx(0.0, abs=1e-9)


def test_localize_pinpoints_overloaded_seams():
    layout, _ = _bad_bridge()
    report = localize_instability(layout)
    assert not report.stable
    assert report.q > 0
    # The strongest artificial links sit exactly at the mid-span butt
    # joints where real bricks cannot transmit the shear.
    strongest = report.links[0]
    assert strongest.magnitude > 0
    assert abs(strongest.a_id - strongest.b_id) == 1


def test_localize_infeasible_for_unpatchable_collapse():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x4", 0, 0, 9, 0, 4)  # floating, no neighbours
    report = localize_instability(layout)
    assert not report.stable
    assert report.q == float("inf")


def test_repair_stabilizes_bridge():
    layout, grid = _bad_bridge()
    assert not analyze(layout).stable

    report = repair_stability(
        layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
    )

    assert report.stable
    assert analyze(layout).stable
    assert report.bricks_rebuilt > 0
    # Accepted rounds strictly improve the deficit.
    accepted = [q for q in report.q_history if q is not None]
    assert accepted[-1] < accepted[0]


def test_repair_rbe_localizer_also_converges():
    layout, grid = _bad_bridge()
    report = repair_stability(
        layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
        config=RepairConfig(localizer="rbe"),
    )
    assert report.stable


def test_repair_noop_on_stable_layout():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    before = {(b.part_key, b.x, b.y, b.layer) for b in layout}
    report = repair_stability(
        layout,
        VoxelGrid(codes=np.full((2, 4, 3), 4, dtype=np.int16)),
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
    )
    assert report.stable
    assert report.rounds == 0
    assert {(b.part_key, b.x, b.y, b.layer) for b in layout} == before


def test_milp_fill_exact_covers_region():
    grid = VoxelGrid(codes=np.full((4, 2, 3), 4, dtype=np.int16))
    layout = Layout(catalog=default_catalog())
    freed = {(x, y, z) for x in range(4) for y in range(2) for z in range(3)}
    placements = _milp_fill(layout, freed, grid, default_catalog())
    assert placements is not None
    assert len(placements) == 1  # a single brick_2x4 covers everything
    part_key, _anchor, _yaw, colour = placements[0]
    assert part_key == "brick_2x4"
    assert colour == 4


def test_milp_filler_through_repair():
    layout, grid = _bad_bridge()
    report = repair_stability(
        layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
        config=RepairConfig(filler="milp"),
    )
    assert report.stable
