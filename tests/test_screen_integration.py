"""Reduced-QP screen wired into certify/accept, Luo, and configuration."""

import numpy as np
import pytest

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.configuration import project_config_from_mapping
from legolization.corpus.generators import cantilever
from legolization.grid import VoxelGrid
from legolization.layout import Layout
from legolization.placement.luo import LuoStrategy
from legolization.stability import SolverConfig
from legolization.stability.incremental import FrozenBoundaryAnalyzer
from legolization.stability.screen import ScreenReport, screen_layout


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def _cantilever_layout() -> Layout:
    lay = Layout(catalog=default_catalog())
    lay.add("brick_1x1", 0, 0, 0, 0, 4)
    lay.add("brick_1x4", 0, 0, 3, 0, 4)
    return lay


_ON = SolverConfig(screen="bricksim")
_OFF = SolverConfig()


def test_screen_off_builds_no_reduced_screen():
    analyzer = FrozenBoundaryAnalyzer.create(_cantilever_layout(), config=_OFF)
    assert analyzer.reduced is None


def test_screen_on_builds_reduced_screen():
    analyzer = FrozenBoundaryAnalyzer.create(_cantilever_layout(), config=_ON)
    assert analyzer.reduced is not None
    assert 0.0 < analyzer.reduced.baseline_q < 1.0


def test_certify_confident_reject_skips_cold_solve():
    baseline = _cantilever_layout()
    analyzer = FrozenBoundaryAnalyzer.create(baseline, config=_ON)
    # Candidate stacks a heavy tower on the beam's free end: strictly
    # more unstable bricks than the stable baseline.
    candidate = baseline.copy()
    for level in range(12):
        candidate.add("brick_2x2", 2, 0, 6 + 3 * level, 0, 4)
    with telemetry.record() as session:
        certification = analyzer.certify(candidate)
    assert certification.reduced_report is not None
    assert certification.cold_result is None
    assert not certification.cold_certified
    assert "stability.screen.reject" in session.values_dict()
    with pytest.raises(ValueError, match="without full cold certification"):
        analyzer.accept(candidate, certification)


def test_certify_pass_cold_solves_and_accept_rebases():
    baseline = _cantilever_layout()
    analyzer = FrozenBoundaryAnalyzer.create(baseline, config=_ON)
    assert analyzer.reduced is not None
    # Candidate removes the beam's overhang stress: strictly better.
    candidate = Layout(catalog=default_catalog())
    candidate.add("brick_1x1", 0, 0, 0, 0, 4)
    candidate.add("brick_1x2", 0, 0, 3, 0, 4)
    with telemetry.record() as session:
        certification = analyzer.certify(candidate, changed_ids=set(baseline.bricks))
    assert certification.cold_certified
    assert certification.reduced_report is not None
    assert "stability.screen.reject" not in session.values_dict()
    analyzer.accept(candidate, certification)
    assert analyzer.reduced.baseline_q == certification.reduced_report.q


def test_certify_matches_screen_off_when_not_rejecting():
    baseline = _cantilever_layout()
    candidate = Layout(catalog=default_catalog())
    candidate.add("brick_1x1", 0, 0, 0, 0, 4)
    candidate.add("brick_1x2", 0, 0, 3, 0, 4)
    on = FrozenBoundaryAnalyzer.create(baseline, config=_ON).certify(
        candidate, changed_ids=set(baseline.bricks)
    )
    off = FrozenBoundaryAnalyzer.create(baseline, config=_OFF).certify(
        candidate, changed_ids=set(baseline.bricks)
    )
    assert on.screen == off.screen
    assert on.cold_result is not None
    assert off.cold_result is not None
    assert on.cold_result.stable == off.cold_result.stable
    assert on.cold_result.scores == off.cold_result.scores
    assert on.cold_result.objective == off.cold_result.objective


def test_snot_baseline_degrades_to_heuristic_only(layout):
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    analyzer = FrozenBoundaryAnalyzer.create(layout, config=_ON)
    assert analyzer.reduced is None  # declined -> screen-off behaviour


def test_should_reject_semantics():
    baseline = _cantilever_layout()
    screen_report = screen_layout(baseline, _ON)
    analyzer = FrozenBoundaryAnalyzer.create(baseline, config=_ON)
    assert analyzer.reduced is not None
    margin = _ON.screen_margin
    worse_count = ScreenReport(
        status="ok",
        stable=False,
        q=1.0,
        confident=True,
        unstable_ids=frozenset({1}),
        q_raw=2.0,
    )
    assert analyzer.reduced.should_reject(worse_count, margin)
    much_higher_stress = ScreenReport(
        status="ok",
        stable=True,
        q=min(1.0, screen_report.q_raw * 2.0),
        confident=True,
        q_raw=screen_report.q_raw * 2.0 + 0.1,
    )
    assert analyzer.reduced.should_reject(much_higher_stress, margin)
    unconfident = ScreenReport(
        status="ok",
        stable=False,
        q=1.0,
        confident=False,
        unstable_ids=frozenset({1, 2}),
        q_raw=5.0,
    )
    assert not analyzer.reduced.should_reject(unconfident, margin)
    near_equal = ScreenReport(
        status="ok",
        stable=True,
        q=screen_report.q,
        confident=True,
        q_raw=screen_report.q_raw,
    )
    assert not analyzer.reduced.should_reject(near_equal, margin)
    declined = ScreenReport(status="declined")
    assert not analyzer.reduced.should_reject(declined, margin)


def test_luo_place_completes_with_screen_on():
    grid = VoxelGrid.from_array(cantilever(), plates_per_voxel=3)
    strategy = LuoStrategy(solver_config=SolverConfig(screen="bricksim"))
    layout = strategy.place(grid, rng=np.random.default_rng(0))
    assert len(layout) > 0


def test_toml_surface_round_trip():
    config = project_config_from_mapping(
        {"stability": {"solver": {"screen": "bricksim", "screen_margin": 0.2}}}
    )
    solver = config.stability.effective_solver()
    assert solver.screen == "bricksim"
    assert solver.screen_margin == 0.2


def test_invalid_screen_values_rejected():
    with pytest.raises(ValueError, match="screen must be"):
        SolverConfig(screen="qp")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="screen_fields must be"):
        SolverConfig(screen_fields="paper")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite and positive"):
        SolverConfig(screen_margin=0.0)
