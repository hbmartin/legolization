"""Reduced-QP screen: physics fixtures, direction bands, fallbacks."""

import time
from types import SimpleNamespace

import numpy as np
import osqp
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import legolization.stability.screen as screen_module
from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.layout import CollisionError, Layout
from legolization.stability import SolverConfig, analyze
from legolization.stability.screen import ReducedScreen, screen_layout


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def _overload_tower() -> Layout:
    lay = Layout(catalog=default_catalog())
    lay.add("brick_2x2", 0, 0, 0, 0, 4)
    lay.add("brick_1x6", 1, 0, 3, 0, 4)
    for level in range(24):
        lay.add("brick_2x2", 5, 0, 6 + 3 * level, 0, 4)
    return lay


def test_grounded_brick_is_relaxed(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    report = screen_layout(layout, SolverConfig())
    assert report.status == "ok"
    assert report.stable
    assert report.q == pytest.approx(0.0, abs=1e-3)
    assert report.confident


def test_cantilever_is_stable_and_conservative(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    cold = analyze(layout)
    report = screen_layout(layout, SolverConfig())
    assert report.status == "ok"
    assert report.stable
    # Restriction property: the reduced polytope can only need MORE
    # drag than the exact optimum.
    assert report.q >= cold.max_score - 1e-3
    assert report.q < 1.0


def test_floating_brick_flagged(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    floater = layout.add("brick_2x4", 20, 20, 9, 0, 4)
    report = screen_layout(layout, SolverConfig())
    assert report.status == "ok"
    assert not report.stable
    assert report.unstable_ids == {floater.brick_id}


def test_brick_on_tile_flagged(layout):
    layout.add("tile_2x2", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x2", 0, 0, 1, 0, 4)
    report = screen_layout(layout, SolverConfig())
    assert report.status == "ok"
    assert upper.brick_id in report.unstable_ids


def test_overloaded_cantilever_localizes_like_cold():
    lay = _overload_tower()
    cold = analyze(lay)
    report = screen_layout(lay, SolverConfig())
    assert report.status == "ok"
    assert not report.stable
    assert report.unstable_ids == cold.unstable_ids
    # The overloaded connection is the cold solve's weakest pair.
    assert cold.weakest_pair in report.overloaded


def test_scores_on_max_score_scale(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    report = screen_layout(layout, SolverConfig())
    assert report.scores is not None
    assert set(report.scores) == set(layout.bricks)
    assert all(0.0 <= s <= 1.0 for s in report.scores.values())
    assert report.q == max(report.scores.values())


def _random_layout(seed_bricks: list[tuple[str, int, int, int]]) -> Layout:
    lay = Layout(catalog=default_catalog())
    for part, x, y, layer in seed_bricks:
        try:
            lay.add(part, x, y, 3 * layer, 0, 4)
        except CollisionError:
            continue
    return lay


@settings(max_examples=15, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["brick_1x1", "brick_1x2", "brick_2x2", "brick_1x4"]),
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=3),
            st.integers(min_value=0, max_value=3),
        ),
        min_size=2,
        max_size=8,
    )
)
def test_screen_direction_bands(seed_bricks):
    # One-directional claims only — QP and LP never bit-match:
    # cold-unstable implies screen-unstable-or-near (conservatism), and
    # a confidently-stable screen implies the cold verdict is stable.
    lay = _random_layout(seed_bricks)
    if not len(lay):
        return
    config = SolverConfig()
    report = screen_layout(lay, config)
    if report.status != "ok":
        return
    cold = analyze(lay, config)
    if not cold.stable:
        assert report.q >= 1.0 - config.screen_margin
    if report.stable and report.confident:
        assert cold.stable
    if report.status == "ok":
        # Conservatism is a tendency, not a theorem: soft equilibrium
        # permits small undershoots (measured up to ~0.06 at n≈1200).
        assert report.q >= cold.max_score - config.screen_margin


def test_snot_layout_screens_and_agrees_with_cold(layout):
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    config = SolverConfig()
    cold = analyze(layout, config)
    with telemetry.record() as session:
        report = screen_layout(layout, config)
    assert report.status == "ok"
    assert report.stable == cold.stable
    assert report.q >= cold.max_score - config.screen_margin
    assert "stability.screen.decline" not in session.values_dict()


def test_snot_rotation_equivalence():
    # The reduced lateral field lives in the mating plane, so the same
    # clad tower rotated through every yaw must screen to the same q
    # (up to OSQP tolerance; the cold q is exactly invariant).
    config = SolverConfig(torque_z=True, rotate_contact_pattern=True)
    values = []
    for yaw in (0, 90, 180, 270):
        layout = Layout(catalog=default_catalog())
        for level in range(3):
            layout.add("brick_2x2", 3, 3, 3 * level, 0, 4)
        layout.add("brick_1x1_side_stud", 4, 4, 9, yaw, 4)
        connector = next(
            c
            for c in layout.catalog["brick_1x1_side_stud"].connectors_at(
                4, 4, 9, yaw, top=True
            )
            if c.direction[2] == 0
        )
        dx, dy, _ = connector.direction
        layout.add("tile_1x1_snot", 4 + dx, 4 + dy, 9, yaw, 14)
        report = screen_layout(layout, config)
        assert report.status == "ok"
        values.append(report.q)
    assert max(values) - min(values) < 1e-3


def test_expired_deadline_skips(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    report = screen_layout(layout, SolverConfig(), deadline=time.monotonic() - 1.0)
    assert report.status == "deadline"
    assert not report.confident


def test_iteration_starvation_reports_nonconverged():
    lay = _overload_tower()
    report = screen_layout(lay, SolverConfig(screen_max_iter=1))
    assert report.status == "nonconverged"
    assert not report.confident


def test_inaccurate_status_reports_nonconverged(layout, monkeypatch):
    """OSQP's "solved inaccurate" is a fallthrough, not a solve.

    It is reported when the iteration or time limit is hit with
    residuals meeting only the loosened tolerances; scoring one would
    let equilibrium leakage flag bricks unstable and drive a
    *confident* rejection that skips the cold solve.
    """
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)

    class _Inaccurate:
        def setup(self, **kwargs: object) -> None:
            """Accept whatever the screen assembles."""

        def solve(self, **_kwargs: object) -> object:
            """Return a plausible but under-converged solution."""
            return SimpleNamespace(
                info=SimpleNamespace(
                    status="solved inaccurate",
                    status_val=osqp.SolverStatus.OSQP_SOLVED_INACCURATE,
                ),
                x=np.zeros(64),
            )

    monkeypatch.setattr(screen_module.osqp, "OSQP", _Inaccurate)
    with telemetry.record() as session:
        report = screen_layout(layout, SolverConfig())
    assert report.status == "nonconverged"
    assert not report.confident
    assert "stability.screen.nonconverged" in session.values_dict()


def test_build_error_reports_error(layout, monkeypatch):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)

    def boom(*args: object, **kwargs: object) -> object:
        msg = "synthetic failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(screen_module, "build_reduced_model", boom)
    with telemetry.record() as session:
        report = screen_layout(layout, SolverConfig())
    assert report.status == "error"
    assert "stability.screen.error" in session.values_dict()
    assert report.detail is not None
    assert "synthetic failure" in report.detail


def test_reduced_screen_baseline_and_rebase(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    screen = ReducedScreen.create(layout, SolverConfig())
    assert screen is not None
    assert 0.0 < screen.baseline_q < 1.0

    candidate = Layout(catalog=default_catalog())
    candidate.add("brick_1x1", 0, 0, 0, 0, 4)
    candidate.add("brick_1x2", 0, 0, 3, 0, 4)
    report = screen.evaluate(candidate)
    assert report.status == "ok"
    assert report.q < screen.baseline_q  # shorter beam is less stressed
    screen.rebase(report)
    assert screen.baseline_q == report.q


def test_reduced_screen_creates_on_snot(layout):
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    screen = ReducedScreen.create(layout, SolverConfig())
    assert screen is not None
    assert screen.baseline.status == "ok"


def test_screen_emits_span(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    with telemetry.record() as session:
        screen_layout(layout, SolverConfig())
    assert "stability.screen" in session.spans
    assert "stability.screen.qp" in session.spans
    assert "stability.reduced.build" in session.spans
