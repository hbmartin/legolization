"""Analytic RBE validation: cases with known physical outcomes."""

from typing import Any

import pytest
from scipy.optimize import OptimizeResult

import legolization.stability.solver as solver_module
from legolization.catalog import default_catalog
from legolization.layout import Layout
from legolization.stability import (
    T_CAPACITY_N,
    SolverConfig,
    analyze,
    build_model,
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
    tower = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    result = analyze(layout)
    assert result.stable
    assert result.scores[beam.brick_id].drag_max > 1e-4
    assert result.weakest_pair == (tower.brick_id, beam.brick_id)
    assert result.min_capacity < T_CAPACITY_N


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


def test_milp_fallback_skips_non_optimal_statuses():
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001

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


def test_milp_fallback_does_not_chain_stale_solver_error():
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001

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
