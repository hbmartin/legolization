"""ALNS stability repair: QP localization, destroy/refill, convergence."""

import time
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.sparse import issparse

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.grid import VoxelGrid
from legolization.layout import Layout
from legolization.placement.repair import (
    RepairConfig,
    _localize,
    _milp_fill,
    repair_stability,
)
from legolization.stability import analyze
from legolization.stability.links import LinkForce, LinkReport, localize_instability


def test_localize_stable_structure_has_zero_q():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    report = localize_instability(layout)
    assert report.stable
    assert report.q == pytest.approx(0.0, abs=1e-9)


def test_localize_pinpoints_overloaded_seams(bad_bridge):
    layout, _ = bad_bridge
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


def test_repair_falls_back_to_rbe_when_qp_is_infeasible(monkeypatch):
    layout = Layout(catalog=default_catalog())
    fallback = LinkReport(
        q=1.0,
        links=(LinkForce(a_id=0, b_id=0, magnitude=1.0),),
        status="optimal",
    )
    monkeypatch.setattr(
        "legolization.placement.repair.localize_instability",
        lambda _layout, config=None: LinkReport(
            q=float("inf"), links=(), status="infeasible"
        ),
    )
    monkeypatch.setattr(
        "legolization.placement.repair._rbe_report",
        lambda _layout, _config: fallback,
    )

    assert _localize(layout, None, RepairConfig()) is fallback


def test_repair_stabilizes_bridge(bad_bridge):
    layout, grid = bad_bridge
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


def test_repair_expired_deadline_skips_every_round(bad_bridge):
    # v8: repair shares the pipeline's absolute budget. An expired
    # deadline means zero destroy/refill rounds — only the initial
    # localization and the final verdict run.
    layout, grid = bad_bridge
    before = {(b.part_key, b.x, b.y, b.layer) for b in layout}
    with telemetry.record() as session:
        report = repair_stability(
            layout,
            grid,
            catalog=default_catalog(),
            solver_config=None,
            rng=np.random.default_rng(0),
            deadline=0.0,
        )
    assert report.rounds == 0
    assert not report.stable
    assert {(b.part_key, b.x, b.y, b.layer) for b in layout} == before
    assert session.values["repair.deadline_stop"] == [0.0]


def test_repair_future_deadline_is_byte_identical(bad_bridge):
    # A generous deadline must not perturb the rng stream or the result.
    unbounded_layout, grid = bad_bridge
    bounded_layout = unbounded_layout.copy()
    unbounded = repair_stability(
        unbounded_layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
    )
    bounded = repair_stability(
        bounded_layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
        deadline=time.monotonic() + 3_600.0,
    )
    assert bounded == unbounded
    assert {(b.part_key, b.x, b.y, b.layer) for b in bounded_layout} == {
        (b.part_key, b.x, b.y, b.layer) for b in unbounded_layout
    }


def test_repair_rbe_localizer_also_converges(bad_bridge):
    layout, grid = bad_bridge
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


def test_milp_fill_uses_sparse_constraint_matrix(monkeypatch):
    grid = VoxelGrid(codes=np.full((2, 1, 3), 4, dtype=np.int16))
    layout = Layout(catalog=default_catalog())
    freed = {(x, 0, z) for x in range(2) for z in range(3)}

    def fake_milp(*, c, constraints, integrality, bounds) -> SimpleNamespace:
        del c, integrality, bounds
        assert issparse(constraints.A)
        return SimpleNamespace(success=False, x=None)

    monkeypatch.setattr("legolization.placement.repair.milp", fake_milp)

    assert _milp_fill(layout, freed, grid, default_catalog()) is None


def test_milp_filler_through_repair(bad_bridge):
    layout, grid = bad_bridge
    report = repair_stability(
        layout,
        grid,
        catalog=default_catalog(),
        solver_config=None,
        rng=np.random.default_rng(0),
        config=RepairConfig(filler="milp"),
    )
    assert report.stable
