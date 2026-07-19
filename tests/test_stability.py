"""Analytic RBE validation: cases with known physical outcomes."""

from typing import Any

import pytest
from scipy.optimize import OptimizeResult

import legolization.stability.solver as solver_module
from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.layout import Layout
from legolization.stability import (
    T_CAPACITY_N,
    SolverConfig,
    analyze,
    build_model,
    solve_maximin,
    solve_model,
)


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def test_single_grounded_brick_is_relaxed(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    result = analyze(layout)
    assert result.stable
    score = next(iter(result.scores.values()))
    assert score.in_equilibrium
    assert score.drag_max == pytest.approx(0.0, abs=1e-6)
    assert score.score == pytest.approx(0.0, abs=1e-6)


def test_well_bonded_wall_is_stable(layout):
    # Two-layer running-bond wall of 1x4 bricks.
    for x in (0, 4):
        layout.add("brick_1x4", x, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    layout.add("brick_1x4", 2, 0, 3, 0, 4)
    layout.add("brick_1x2", 6, 0, 3, 0, 4)
    result = analyze(layout)
    assert result.stable
    assert all(s.score < 1.0 for s in result.scores.values())


def test_floating_brick_collapses(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    floater = layout.add("brick_2x4", 20, 20, 9, 0, 4)
    result = analyze(layout)
    assert not result.stable
    assert result.unstable_ids == {floater.brick_id}
    assert not result.scores[floater.brick_id].in_equilibrium


def test_brick_resting_on_tile_collapses(layout):
    layout.add("tile_2x2", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x2", 0, 0, 1, 0, 4)
    result = analyze(layout)
    assert upper.brick_id in result.unstable_ids


def test_single_stud_cantilever_holds_with_drag(layout):
    # A 1x1 tower with a 1x4 brick attached by its end stud: the knob
    # friction couple must carry the overhang torque — stressed but stable.
    # Closed form: the beam's weight W acts 1.5 studs from the knob, whose
    # rear contact pair (two points at -0.25 pitch, torque arm 0.5 studs to
    # the front pair) must drag 2.5 W split two ways: drag_max = 1.25 W.
    tower = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    result = analyze(layout)
    beam_weight_n = 1.57e-3 * 9.8
    assert result.stable
    assert result.scores[beam.brick_id].drag_max == pytest.approx(
        1.25 * beam_weight_n, rel=1e-4
    )
    assert result.scores[beam.brick_id].score == pytest.approx(0.019625, rel=1e-4)
    assert result.weakest_pair == (tower.brick_id, beam.brick_id)
    assert result.min_capacity < T_CAPACITY_N


def test_maximin_capacity_on_cantilever(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    result = solve_maximin(build_model(layout))
    assert result.feasible
    assert result.capacity == pytest.approx(T_CAPACITY_N - 1.25 * 1.57e-3 * 9.8)


def test_maximin_infeasible_for_floating_brick(layout):
    layout.add("brick_2x4", 0, 0, 9, 0, 4)
    result = solve_maximin(build_model(layout))
    assert not result.feasible


def test_maximin_orders_layouts_by_stress(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    short = solve_maximin(build_model(layout))

    long_layout = Layout(catalog=default_catalog())
    long_layout.add("brick_1x1", 0, 0, 0, 0, 4)
    long_layout.add("brick_1x6", 0, 0, 3, 0, 4)
    long = solve_maximin(build_model(long_layout))
    assert long.feasible
    assert short.feasible
    assert long.capacity < short.capacity < T_CAPACITY_N


def test_longer_cantilever_is_more_stressed(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    short = analyze(layout).max_score

    long_layout = Layout(catalog=default_catalog())
    long_layout.add("brick_1x1", 0, 0, 0, 0, 4)
    long_layout.add("brick_1x6", 0, 0, 3, 0, 4)
    long = analyze(long_layout).max_score
    assert 0.0 < short < long < 1.0


def test_overloaded_cantilever_collapses(layout):
    # Load the free end of a single-stud cantilever with a tall heavy
    # column until the knob friction capacity (T = 0.98 N) is exceeded.
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x6", 1, 0, 3, 0, 4)
    for level in range(24):
        layout.add("brick_2x2", 5, 0, 6 + 3 * level, 0, 4)
    result = analyze(layout)
    assert not result.stable
    assert result.scores[beam.brick_id].score == 1.0


def test_milp_matches_lp_on_stable_stack(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    lp = analyze(layout, SolverConfig(mode="lp"))
    milp = analyze(layout, SolverConfig(mode="milp"))
    assert lp.stable
    assert milp.stable


def _assert_lp_milp_agree(layout: Layout) -> None:
    """Assert the MILP reproduces the (exact) LP relaxation everywhere."""
    lp = analyze(layout, SolverConfig(mode="lp"))
    milp = analyze(layout, SolverConfig(mode="milp"))
    assert lp.stable == milp.stable
    assert lp.unstable_ids == milp.unstable_ids
    for brick_id, lp_score in lp.scores.items():
        assert milp.scores[brick_id].score == pytest.approx(lp_score.score, abs=1e-4)


def test_milp_matches_lp_on_stressed_cantilever(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    _assert_lp_milp_agree(layout)


def test_milp_matches_lp_on_overloaded_cantilever(layout):
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x6", 1, 0, 3, 0, 4)
    for level in range(24):
        layout.add("brick_2x2", 5, 0, 6 + 3 * level, 0, 4)
    _assert_lp_milp_agree(layout)


def test_model_shape(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    model = build_model(layout)
    assert model.brick_count == 2
    assert model.a_matrix.shape[0] == 10  # 5 equations per brick
    # 8 ground knobs + 8 interface knobs, 3 points each (2x4 is 2 wide),
    # each with normal+drag, plus 4 knob-press vars per knob.
    knobs = 16
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_lp_fallback_requires_successful_linprog(layout, monkeypatch):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    model = build_model(layout)
    original_linprog = solver_module.linprog
    calls = 0

    def fake_linprog(**kwargs) -> OptimizeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return OptimizeResult(
                success=False,
                status=2,
                message="failed with iterate",
                x=[0.0] * len(kwargs["c"]),
                fun=0.0,
            )
        return original_linprog(**kwargs)

    monkeypatch.setattr(solver_module, "linprog", fake_linprog)

    result = solve_model(model, SolverConfig(mode="lp"))

    assert result.status == "optimal"
    assert calls == 2


def test_milp_fallback_skips_non_optimal_statuses(monkeypatch):
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001
    monkeypatch.setattr(solver_module, "_MILP_SOLVERS", ("HIGHS", "SCIP"))

    class FakeProblem:
        """Fake cvxpy problem that succeeds only on the second solver."""

        def __init__(self) -> None:
            self.status = "not-started"
            self.solvers: list[str] = []

        def solve(self, *, solver: str) -> None:
            """Record solver attempts and expose a cvxpy-like status."""
            self.solvers.append(solver)
            self.status = "optimal" if len(self.solvers) == 2 else "infeasible"

    problem: Any = FakeProblem()

    assert solve_with_fallback(problem, SolverConfig(mode="milp")) == "optimal"
    assert problem.solvers == ["HIGHS", "SCIP"]


def test_milp_fallback_does_not_chain_stale_solver_error(monkeypatch):
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001
    monkeypatch.setattr(solver_module, "_MILP_SOLVERS", ("HIGHS", "SCIP"))

    class FakeProblem:
        """Fake cvxpy problem where a later solver returns a final status."""

        def __init__(self) -> None:
            self.status = "not-started"
            self.solvers: list[str] = []

        def solve(self, *, solver: str) -> None:
            """Raise once, then expose a cvxpy-like non-optimal status."""
            self.solvers.append(solver)
            if len(self.solvers) == 1:
                raise solver_module.cp.SolverError
            self.status = "infeasible"

    problem: Any = FakeProblem()

    with pytest.raises(RuntimeError, match="last status: infeasible") as exc_info:
        solve_with_fallback(problem, SolverConfig(mode="milp"))

    assert exc_info.value.__cause__ is None
    assert problem.solvers == ["HIGHS", "SCIP"]


def test_empty_layout_is_stable(layout):
    assert analyze(layout).stable


def _bridge(gap: int) -> Layout:
    """Two cantilevered 1x4 beams on end towers, ``gap`` studs apart."""
    layout = Layout(catalog=default_catalog())
    span = 8 + gap
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", span - 1, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    layout.add("brick_1x4", 4 + gap, 0, 3, 0, 4)
    return layout


def test_side_presses_shed_cantilever_load():
    # Luo §6: beams butted mid-span lean on each other through their shared
    # vertical face; the side presses at the face's vertical extremes carry
    # torque, so each half is far less stressed than a free cantilever.
    butted = analyze(_bridge(gap=0))
    gapped = analyze(_bridge(gap=1))
    assert butted.stable
    assert gapped.stable
    assert butted.max_score < 0.5 * gapped.max_score


def test_mirrored_layout_scores_identically(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)  # extends toward +x

    mirrored = Layout(catalog=default_catalog())
    mirrored.add("brick_1x1", 10, 0, 0, 0, 4)
    mirrored.add("brick_1x4", 7, 0, 3, 0, 4)  # extends toward -x
    assert analyze(mirrored).max_score == pytest.approx(analyze(layout).max_score)


def test_side_contacts_add_two_variables_per_pair(layout):
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 1, 0, 0, 4)
    model = build_model(layout)
    # 4 ground knobs x 4 points (1-wide cavities) x (normal + drag) plus
    # 4 knob presses per knob, plus 2 side presses at the shared face's
    # vertical extremes (was 1 torque-inert press before F1).
    knobs = 4
    assert model.var_count == knobs * 4 * 2 + knobs * 4 + 2


def test_cavity_pattern_var_counts(layout):
    # 2-wide cavities pinch studs at three points, 1-wide at four.
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_2x2", 0, 0, 3, 0, 4)
    model = build_model(layout)
    knobs = 8  # 4 ground + 4 interface, all under 2x2 bricks
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_ground_pull_keeps_tipping_column_stable(layout):
    # A 1x6 beam off a grounded 1x2 with a column at the beam tip: the
    # load's line of action falls outside the base footprint, so the far
    # ground contacts must pull down (StableLego baseplate-style ground).
    base = layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x6", 0, 0, 3, 0, 4)
    for level in range(3):
        layout.add("brick_1x1", 5, 0, 6 + 3 * level, 0, 4)
    result = analyze(layout)
    assert result.stable
    assert result.scores[base.brick_id].drag_max > 1e-3


# --- yaw torque (torque_z) ---


def test_torque_z_adds_sixth_row_per_brick(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    model = build_model(layout, torque_z=True)
    assert model.rows_per_brick == 6
    assert model.a_matrix.shape[0] == 12
    # Contact-variable census is unchanged: the row grows, not the vars.
    knobs = 16
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_torque_z_side_contacts_use_four_corner_generators(layout):
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 1, 0, 0, 4)
    model = build_model(layout, torque_z=True)
    # Same knob census as the 5-row pin, but the side pair now carries
    # 4 corner presses (2 vertical extremes x 2 transverse extremes)
    # so lateral load can express yaw torque.
    knobs = 4
    assert model.var_count == knobs * 4 * 2 + knobs * 4 + 4


def test_torque_z_preserves_untwisted_verdicts(layout):
    # Gravity never loads the yaw row (its lever is identically zero),
    # so verdicts and scores on gravity-only classics must match the
    # 5-row physics exactly.
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x2", 1, 0, 3, 0, 4)
    del beam
    base = analyze(layout, SolverConfig())
    yaw = analyze(layout, SolverConfig(torque_z=True))
    assert yaw.stable == base.stable
    assert yaw.max_score == pytest.approx(base.max_score, rel=1e-6)


def test_side_contact_transverse_extent_recorded(layout):
    # A 1x4 beside a 1x4: shared faces along y have centers at x 0..3.
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 1, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    (side,) = graph.side_contacts
    assert side.axis == 1
    assert (side.t_lo, side.t_hi) == (0.0, 3.0)
